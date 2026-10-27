"""Chains delivered more than once (P15 part 2, card G): the companion extraction chain (G1), the Telegram intake (G2) and
an owner's click on a review question (G3). The email-indexing chain (G4) is in `test_worker_redelivery_email_indexing.py`
(it builds the production graph, so it is `slow`).

Every delivery goes through the redelivery world (`tests/support/redelivery_world.py`): the real `WorkerHandler` and its
claim store on an `EvalDb`, two names the claim does not merge (`t-1`, `t-2`), one after the other or under
`asyncio.gather`. The service under test sits on a second `EvalDb` (the companion data in G1, the review world's in G3):
the world builds its own database inside `RedeliveryWorld.__init__`, before the service can be built on it. Two
databases are harmless here: the claims and the service's data never meet in one transaction.

Defects pinned as strict xfails (arbiter-confirmed): P15-D5 (a companion batch is extracted by two concurrent deliveries,
and a batch that always fails is revived and extracted without bound) and P15-D12 (a concurrent double click applies the
answer twice: a second owner-trail entry on the fact and doubled answer counters; the message the owner sees is
correct). Every strict xfail checks its precondition with `require` (a failed one reports FAILED, never the expected
failure); only its outcome is a bare `assert`.
"""
import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional, Union
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_companion_cache_repository import FirestoreCompanionCacheRepository
from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.adapters.firestore_companion_memory_repository import FirestoreCompanionMemoryRepository
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.config.environment import EnvironmentConfig
from src.domain.auth import IAMDecision
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.entities import ExpiryKind
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import option_id
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FACT_QUESTION_KIND, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_reasons import answer_reason
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.user import UserProfile
from src.ports.companion_extractor_port import CompanionExtractorPort
from src.ports.embedding_service import EmbeddingService
from src.ports.platform_auth_port import PlatformAuthPort
from src.services.companion_extraction_service import CompanionExtractionService
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.fact_kit import ACC, USER as REVIEW_USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue
from tests.support.redelivery_world import (
    ACCOUNT, CHANNEL, USER, RedeliveryWorld, Rendezvous, deliver, deliver_twice, require,
)

D5 = ("P15-D5: a companion batch is extracted again by a concurrent or later delivery (no per-session lease, revival "
      "without bound); owner decision")
D12 = ("P15-D12: a concurrent double click applies the answer twice (a second owner-trail entry, doubled answer "
       "counters); owner decision")


# ================================================================================================ G1: companion chain

SESSION = f"{USER}:{CHANNEL}"
TURNS = [
    {"role": "user", "parts": [{"text": "How do I say 'the conference starts at nine' in German?"}]},
    {"role": "model", "parts": [{"text": "Die Konferenz beginnt um neun Uhr."}]},
]
THREE = {
    "records": [
        {"text": "Uses 'beginnen' correctly with a time phrase.", "domain": "progress_note", "tags": ["verbs"]},
        {"text": "Did not know the word 'Konferenz'.", "domain": "vocabulary_gap", "tags": ["work"]},
        {"text": "Practised describing a conference.", "domain": "topic_covered", "tags": []},
    ],
    "summary": "A short exchange about a conference.",
}
TWO = {
    "records": [
        {"text": "Drops the article before 'Konferenz'.", "domain": "grammar_error", "tags": []},
        {"text": "Asked how to talk about giving a talk.", "domain": "topic_covered", "tags": []},
    ],
    "summary": "Conference vocabulary.",
}
Step = Union[Dict[str, Any], BaseException]


class ScriptedExtractor(CompanionExtractorPort):
    """The companion extractor (no LLM): each call is counted, awaits once (`asyncio.sleep(0)`, the model call's await),
    then answers the next scripted step (an exception step is raised); the last step repeats."""

    def __init__(self, *steps: Step) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def extract(self, companion_type: str, account_id: str, created_by_user_id: str,
                      messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0)
        step = self._steps[min(self.calls, len(self._steps)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


class FlatEmbeddings(EmbeddingService):
    """One 768-float vector per text."""

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        return [0.1] * 768

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [[0.1] * 768 for _ in texts]


class Companion:
    """The companion pipeline on its own `EvalDb` (module docstring), with a scripted extractor."""

    def __init__(self, extractor: ScriptedExtractor) -> None:
        self.db = EvalDb()
        self.env = EnvironmentConfig()
        self.extractor = extractor
        self.queue = FirestoreCompanionExtractionQueue(self.db, self.env)
        self.service = companion_service(self.db, self.env, self.queue, extractor)

    async def seed_batch(self) -> None:
        await self.queue.enqueue_batch(CompanionExtractionBatch(
            batch_id="b1", session_id=SESSION, account_id=ACCOUNT, created_by_user_id=USER, companion_type="tutor",
            messages=TURNS,
        ))

    @property
    def batch(self) -> Optional[dict]:
        return self.db.collection(self.env.companion_extraction_queue_collection).docs.get("b1")

    @property
    def record_texts(self) -> List[str]:
        return sorted(d["text"] for d in self.db.collection(self.env.companion_records_collection).docs.values())


def companion_service(db: EvalDb, env: EnvironmentConfig, queue: FirestoreCompanionExtractionQueue,
                      extractor: ScriptedExtractor) -> CompanionExtractionService:
    """The one place `CompanionExtractionService` is built in this file, as production wires it
    (`application_graph.py:307,508-509,571-577`). The P15-D5 fix's PR adds its lease / queue arguments here, as a
    `test(mechanical)` commit."""
    return CompanionExtractionService(
        queue=queue, companion_repo=FirestoreCompanionMemoryRepository(db, env),
        cache_repo=FirestoreCompanionCacheRepository(db, env), embedding_service=FlatEmbeddings(), extractor=extractor,
    )


def _texts(step: Dict[str, Any]) -> List[str]:
    return sorted(r["text"] for r in step["records"])


async def _companion_world(*steps: Step) -> "tuple[RedeliveryWorld, Companion]":
    companion = Companion(ScriptedExtractor(*steps))
    await companion.seed_batch()
    world = RedeliveryWorld(companion_extraction=companion.service)
    return world, companion


class TestCompanionChainTwice:
    """`_handle_companion_consolidation` (`worker_handler.py:515-530`) → `process_session_batches`
    (`companion_extraction_service.py:49-122`): revive (`:59`), read the oldest pending batch (`:66`, a query), mark it
    PROCESSING (`:71`), extract, store records under deterministic ids (`:152`), delete the batch; a failure counts an
    attempt and marks FAILED at 3 (`:100-113`). `reset_recoverable_batches` revives every FAILED batch with `attempts: 0`
    (`firestore_companion_extraction_queue.py:87-110`)."""

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D5)
    async def test_two_gathered_deliveries_extract_one_batch_once(self):
        """Two deliveries under `asyncio.gather` both read batch b1 as pending and both extract it (two model calls; the
        second's records overwrite the first's by id, so the stored set mixes two extractions).

        Interleaving: `Rendezvous.wrap(queue, "get_pending_batches")`: the read is a query, which `EvalDb` never yields
        on, so the deliveries are made to meet right after it (no collection sets `yield_on_get`). Precondition (today
        and after the fix): both deliveries were inside `handle` at once, a delivery reached the read and nobody stalled
        there, and the batch was extracted at least once; not `rv.met`, which the fix removes (its loser never reads the
        batch). Flipping fix: take a per-session memory-write lease in `process_session_batches`, the loser deferring
        by named task, as `ConsolidationService` does (`decisions/fact_versions_one_writer.md`): the second delivery
        never reads the batch."""
        world, companion = await _companion_world(THREE, TWO)
        rv = Rendezvous()
        rv.wrap(companion.queue, "get_pending_batches")

        twice = await deliver_twice(world.handler, world.payload("companion_consolidation", session_id=SESSION),
                                    gathered=True, rendezvous=rv)

        twice.reraise()
        require(twice.overlapped and rv.reached.is_set() and not rv.stalled,
                "both deliveries were inside handle at once and one reached the batch read without a stall")
        require(companion.extractor.calls >= 1, "a delivery read batch b1 as pending and extracted it")
        assert companion.extractor.calls == 1
        assert companion.record_texts == _texts(THREE)

    async def test_two_sequential_deliveries_extract_once_and_the_second_finds_nothing(self):
        """Control for the xfail above: `t-1` extracts and deletes the batch, `t-2` finds no pending batch.

        Regression: a fix that leaves the batch behind after a success (every delivery extracts it again)."""
        world, companion = await _companion_world(THREE, TWO)

        twice = await deliver_twice(world.handler, world.payload("companion_consolidation", session_id=SESSION),
                                    gathered=False)

        assert twice.answers == [({"status": "ok"}, 200), ({"status": "ok"}, 200)]
        assert companion.extractor.calls == 1
        assert companion.batch is None
        assert companion.record_texts == _texts(THREE)
        assert world.bodies("companion_consolidation") == []

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D5)
    async def test_a_batch_that_always_fails_is_not_extracted_without_bound(self):
        """A batch whose extraction always raises reaches FAILED at its third attempt; every later delivery of the session
        revives it with `attempts: 0` and runs the model again, three calls per three deliveries, for ever (30 deliveries,
        30 calls).

        Precondition (today and after the fix): after the third delivery the batch is FAILED with 3 attempts. Outcome:
        the last 6 of 30 deliveries make no extractor call, and the batch is still stored FAILED. Flipping fix: a revival
        cap counted in `reset_recoverable_batches` and/or revival only from `sweep_companion_consolidation`, as
        `firestore_consolidation_queue.py:111-174` does for Alek's batches (pinned there by
        `tests/unit/adapters/test_consolidation_queue_failed_bounded.py:57,66`)."""
        world, companion = await _companion_world(RuntimeError("extractor unavailable"))
        payload = world.payload("companion_consolidation", session_id=SESSION)

        for n in range(1, 4):
            await deliver(world.handler, payload, f"t-{n}")
        batch = companion.batch or {}
        require(batch.get("status") == "failed" and batch.get("attempts") == 3,
                "after three deliveries the batch is FAILED with three attempts")
        for n in range(4, 25):
            await deliver(world.handler, payload, f"t-{n}")
        calls_before_the_last_six = companion.extractor.calls
        for n in range(25, 31):
            await deliver(world.handler, payload, f"t-{n}")

        assert companion.extractor.calls == calls_before_the_last_six
        assert (companion.batch or {}).get("status") == "failed"

    async def test_a_batch_that_fails_twice_then_succeeds_is_stored_and_deleted(self):
        """Control for the xfail above: two failed attempts leave the batch RETRY_PENDING (never FAILED, never revived),
        the third delivery extracts it, stores its records and deletes it.

        Regression: a revival cap that also stops a batch below three attempts (a recoverable batch is lost)."""
        world, companion = await _companion_world(RuntimeError("timeout"), RuntimeError("timeout"), THREE)
        payload = world.payload("companion_consolidation", session_id=SESSION)

        answers = [await deliver(world.handler, payload, f"t-{n}") for n in range(1, 4)]

        assert answers == [({"status": "ok"}, 200)] * 3
        assert companion.extractor.calls == 3
        assert companion.batch is None
        assert companion.record_texts == _texts(THREE)


# ================================================================================================ G2: Telegram intake

TELEGRAM_UPDATE = {"update_id": 611, "message": {"message_id": 31, "date": 1700000000,
                   "chat": {"id": 1, "type": "private"}, "from": {"id": 1, "is_bot": False, "first_name": "U"},
                   "text": "call dad"}}


class TestTelegramIntakeTwice:
    async def test_the_same_telegram_update_twice_is_one_queued_task(self):
        """Telegram delivers one update twice (its own retry): the webhook names the `telegram_update` task after the
        update id (`webhook_adapter.py:146-156`, `dedup_key=f"telegram::{update_id}"` at `:154`), Cloud Tasks refuses the
        second create (`ALREADY_EXISTS` → `None`), and both answers are 200. Nothing runs inline.

        Regression: `dedup_key=` dropped (no test pinned it): every Telegram retry becomes a second chat turn. The
        gathered case is the same: Cloud Tasks refuses a used name whatever the timing (`recording_task_queue.py:1-10`
        models it, under a lock). The adapter is built as `tests/unit/adapters/test_telegram_queued_updates.py:23-48`
        builds it (`request` / `jsonify` patched: Quart's context-locals); its constructor fillers are never read."""
        queue, client = make_task_queue()
        dedup = MagicMock()
        dedup.try_mark_processed = AsyncMock(return_value=True)
        adapter = TelegramWebhookAdapter(token="1:abc", webhook_secret="s", dedup_store=dedup,
                                         session_store=MagicMock(), conversation_handler=AsyncMock(),
                                         iam_service=AsyncMock(), task_queue=queue)
        adapter._verify_webhook_signature = AsyncMock(return_value=True)  # type: ignore[method-assign]
        inline: List[Any] = []

        async def process(message: Any, update_id: Optional[int] = None) -> None:
            inline.append(update_id)

        adapter._process_message = process  # type: ignore[method-assign]
        req = MagicMock()
        req.get_json = AsyncMock(return_value=TELEGRAM_UPDATE)

        with patch("src.adapters.telegram.webhook_adapter.request", new=req), \
                patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
            first = await adapter._handle_telegram_update()
            second = await adapter._handle_telegram_update()

        assert first == second == ({"ok": True}, 200)
        assert client.worker_tasks() == [("telegram_update", {"task_type": "telegram_update", "update": TELEGRAM_UPDATE})]
        assert inline == []


# ================================================================================================ G3: interactive choice

ZONE = "Europe/Berlin"
FACT = "User plays tennis on Sundays."
REVIEW_CHANNEL = "C1"
CLICK = NOW + timedelta(hours=1)
INTERACTIVE = "interactive_choice"


class FixedZones:
    """The owner-zone resolver's answer for every account."""

    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(REVIEW_USER, ZONE)


class AllowU1(PlatformAuthPort):
    """The identity check (fake shape of `tests/unit/services/test_interactive_choice_service.py:36-48`): Slack user `U1`
    is the review world's user; anyone else is rejected."""

    async def authorize(self, platform: str, platform_user_id: Optional[str] = None) -> IAMDecision:
        if (platform, platform_user_id) == ("slack", "U1"):
            return IAMDecision(action="allow", user=UserProfile(user_id=REVIEW_USER, account_id=ACC))
        return IAMDecision(action="reject", message="Register first")


class Click:
    """A stale fact, its question `q1` asked on Slack at `ref` and current in the review state, and the services that
    answer a click: `FactAnswerService` as `tests/unit/services/test_fact_answer_service.py:98-105` builds it (zone
    Europe/Berlin) under `InteractiveChoiceService`, handed to the redelivery world's handler (module docstring: claims on
    `world.db`, review data on `review.store.db`)."""

    def __init__(self, review: ReviewWorld, fact_id: str, ref: MessageRef) -> None:
        self.review = review
        self.fact_id = fact_id
        self.ref = ref
        texts = FactQuestionTexts(review.language, review.localization, review.review_store, review.counters,
                                  LanguageCode.EN, clock=review.clock)
        self.answers = FactAnswerService(
            fact_updates=review.store.adapter, repo=review.repo, review_store=review.review_store,
            counters=review.counters, task_queue=review.queue, session_store=review.session_store, texts=texts,
            interactive=review.interactive, owner_zones=FixedZones(), clock=review.clock,
        )
        self.world = RedeliveryWorld(interactive_choice=InteractiveChoiceService(
            AllowU1(), {self.answers.kind: self.answers}, review.interactive))
        self.payload = self.world.payload(INTERACTIVE, **InteractiveChoice(
            binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id="q1"), option_id=option_id(AnswerKind.STILL_TRUE),
            platform="slack", platform_user_id="U1", message_ref=ref, at=CLICK,
        ).to_payload())

    @property
    def fact(self) -> dict:
        return dict(self.review.docs[self.fact_id])

    @property
    def question(self) -> dict:
        return dict(self.review.question_docs["q1"])

    def next_tasks(self) -> List[dict]:
        return [body for task_type, body in self.review.enqueued if task_type == "fact_review"]

    async def button_answers(self) -> int:
        """The account's `answered_by_button` count on the click's (UTC) day."""
        rows = [row for row in await self.review.counters.for_day(CLICK.date()) if row.account_id == ACC]
        return sum(row.answered_by_button for row in rows)

    def owner_sees_one_answer(self, answers: List[Any]) -> bool:
        """What the owner sees after two deliveries: both `shown`, the question closed at the click, one next question
        released, the message showing the chosen option without its controls."""
        shown = self.review.interactive.shown.get(self.ref)
        return (answers == [({"status": "shown"}, 200)] * 2
                and (self.question["status"], self.question["closed_at"]) == ("closed", CLICK)
                and [t["mode"] for t in self.next_tasks()] == ["next"]
                and shown is not None
                and (shown.chosen_option_id, shown.keep_options) == (option_id(AnswerKind.STILL_TRUE), False))



async def _click() -> Click:
    """As `question_on` (`test_fact_answer_service.py:108-127`, re-declared): the question was posted on Slack first, at
    the ref stored on it."""
    review = ReviewWorld()
    fact_id = await review.seed_stale(FACT, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                      reported_date=NOW - timedelta(days=60))
    await review.seed_state(account_id=ACC, current_question_id="q1", sent=1, sent_on=NOW.date())
    fields: Dict[str, Any] = dict(question_id="q1", account_id=ACC, user_id=REVIEW_USER, fact_id=fact_id,
                                  quoted_text=review.docs[fact_id]["text"])
    draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive", **fields)
    texts = FactQuestionTexts(review.language, review.localization, review.review_store, review.counters,
                              LanguageCode.EN, clock=review.clock)
    labels = await texts.labels_for(REVIEW_USER, ACC)
    ref = await review.interactive.post("slack", REVIEW_CHANNEL, fact_question_message(draft, labels))
    await review.seed_question(QuestionStatus.ASKED, message_ref=ref, **fields)
    return Click(review, fact_id, ref)


def _still_true_once(click: Click, before: dict) -> None:
    """The fact after one `still_true` (`fact_review_answers.py:146,165`): current again in place (no new version), the
    stale mark cleared, `reported_date` the click, a horizon ending after the click, one more trail entry, under the
    owner with the button's reason. (The horizon's length is the write path's to count, from its own clock: pinned by
    `test_fact_answer_service.py::test_still_true_uses_the_answers_moment_for_the_evidence_the_horizon_and_the_close`.)"""
    fact = click.fact
    assert set(click.review.docs) == {click.fact_id}
    assert (fact["state"], fact["stale_since"], fact["reported_date"]) == ("current", None, CLICK)
    assert fact["expiry_kind"] == "horizon" and fact["expires_at"] > CLICK
    assert len(fact["lifecycle_events"]) == len(before["lifecycle_events"]) + 1
    event = fact["lifecycle_events"][-1]
    assert (event["actor"], event["reason"]) == ("owner", answer_reason(AnswerKind.STILL_TRUE, "q1", "button"))


class TestInteractiveChoiceTwice:
    """`_handle_interactive_choice` (`worker_handler.py:1339-1353`) → `InteractiveChoiceService.handle`
    (`interactive_choice_service.py:35-49`) → `FactAnswerService.handle` (`fact_answer_service.py:228-272`): an open
    question takes `apply` (`:92-158`; the question read through `_question` → `review_store.get_questions`
    `:396-397`, the fact through `_asked_fact`, a preconditioned write, `close_question`); a closed one shows its answer."""

    async def test_the_same_click_delivered_twice_applies_the_answer_once(self):
        """`t-1` applies `still_true` and closes q1; `t-2` finds q1 closed and shows its answer again, writing nothing.

        Regression: a closed question answered again (a second version or trail entry, a second release of the next
        question), or a redelivery that wipes the shown answer."""
        click = await _click()
        before = click.fact

        first = await deliver(click.world.handler, click.payload, "t-1")
        fact_after_first, question_after_first = click.fact, click.question
        second = await deliver(click.world.handler, click.payload, "t-2")

        assert first == second == ({"status": "shown"}, 200)
        _still_true_once(click, before)
        assert click.fact == fact_after_first
        assert click.question == question_after_first
        assert (click.question["status"], click.question["closed_at"], click.question["answer"]) == (
            "closed", CLICK, AnswerKind.STILL_TRUE.value)
        assert [t["mode"] for t in click.next_tasks()] == ["next"]
        assert await click.button_answers() == 1
        shown = click.review.interactive.shown[click.ref]
        assert (shown.chosen_option_id, shown.keep_options) == (option_id(AnswerKind.STILL_TRUE), False)

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D12)
    async def test_the_same_click_delivered_twice_concurrently_applies_the_answer_once(self):
        """Both deliveries read q1 open and the fact stale, then both write: the second `apply_fact_update` is accepted
        (a second owner-trail entry, `current→current`), its close is refused (`fact_answer_service.py:149`), and `apply`
        still refreshes the cache, counts the answer and returns APPLIED (`:150-153`). What the owner sees is right.

        Interleaving: `Rendezvous.wrap(answers, "_asked_fact")`, right after `is_open` (the question read is a
        `get_all`, which `EvalDb` never yields on). Preconditions (today and after the fix): both applies held an open
        question at once (`rv.met`, no stall), and the owner sees one answer (both `shown`, q1 closed at the click, one
        next question, the chosen option without controls). Outcome: one trail entry more than before, one button
        answer counted. Control: the sequential double click above. Flipping fix: close the question before the fact
        write (or make the fact write conditional on the question still being open, in the same transaction) and stop
        when the close is refused."""
        click = await _click()
        before = click.fact
        rv = Rendezvous()
        rv.wrap(click.answers, "_asked_fact")

        twice = await deliver_twice(click.world.handler, click.payload, gathered=True, rendezvous=rv)

        twice.reraise()
        require(twice.overlapped and rv.met and not rv.stalled, "both applies held an open question at once")
        require(click.owner_sees_one_answer(twice.answers), "the owner sees one answer, shown twice")
        assert len(click.fact["lifecycle_events"]) == len(before["lifecycle_events"]) + 1
        assert await click.button_answers() == 1
