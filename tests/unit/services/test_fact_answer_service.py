"""`FactAnswerService` (Facts S4 · T8e, plan §3.16, §3.7): the owner's answers on one path, and the one close path.

On the review world: the real repository, typed write path (the adapter), review store, counters, task queue and
localization over one evaluating Firestore fake, the recording interactive-message port, and the real router with the
plain-text adapter for a Telegram question. Assertions are on the stored documents, the stored questions and state, the
counters, the created tasks and what the platform shows."""
import logging
from datetime import timedelta
from typing import Any, List

import pytest

from src.adapters.firestore_fact_review_store import FirestoreFactReviewStore
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_expiry import Expiry
from src.domain.exceptions import FactBornDueError, FactWriteConflict
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import LABEL_KEYS, fact_question_message
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_review_rules import fallback_window_days
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.ui_messages import UIMessage
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from src.ports.fact_update_port import FactUpdatePort
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld

TZ = "Europe/Lisbon"
TEXT = "drinks green tea every morning"
KINDS = list(AnswerKind)
OWNER_SOURCES = {QuestionStatus.QUEUED: "cabinet", QuestionStatus.ASKED: "digit", QuestionStatus.UNANSWERED: "button"}


class FixedZones:
    """The owner-zone resolver's answer for every account: the owner's zone."""

    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(USER, TZ)


class ScriptedWrites(FactUpdatePort):
    """A typed write path that answers `outcome(fact_id)` and writes nothing."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        return self.outcome(fact_id)


class KeptWrites(FactUpdatePort):
    """The real typed write path, keeping each update sent and its evidence moment."""

    def __init__(self, inner: FactUpdatePort) -> None:
        self._inner = inner
        self.sent: List[Any] = []

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        self.sent.append((update, evidence_at))
        return await self._inner.apply_fact_update(fact_id, update, account_id, context=context,
                                                   evidence_at=evidence_at, reason=reason, history=history)


def counting_refreshes(world: ReviewWorld) -> List[str]:
    """Keep each biographical-cache refresh of the world's repository (it still refreshes)."""
    seen: List[str] = []
    real = world.repo.refresh_biographical_context_cache

    async def refresh(owner_id, *args: Any, **kwargs: Any) -> None:
        seen.append(owner_id)
        await real(owner_id, *args, **kwargs)

    world.repo.refresh_biographical_context_cache = refresh  # type: ignore[method-assign]
    return seen


def texts_of(world: ReviewWorld) -> FactQuestionTexts:
    return FactQuestionTexts(world.language, world.localization, world.review_store, world.counters, LanguageCode.EN,
                             clock=world.clock)


def no_clock() -> Any:
    """The clock of a service whose `apply` is under test: `apply` takes its moment from `at`, never from here."""
    raise AssertionError("apply read the clock")


def service(world: ReviewWorld, fact_updates: Any = None, interactive: Any = None, clock: Any = no_clock,
            review_store: Any = None) -> FactAnswerService:
    """The service on the world. Its clock raises unless one is given: only `close_question` may read it."""
    return FactAnswerService(
        fact_updates=fact_updates or world.store.adapter, repo=world.repo, review_store=review_store or world.review_store,
        counters=world.counters, task_queue=world.queue, session_store=world.session_store, texts=texts_of(world),
        interactive=interactive or world.interactive, owner_zones=FixedZones(), clock=clock,
    )


async def stale_fact(world: ReviewWorld, text: str = TEXT) -> str:
    """A standing fact (a horizon) the janitor staled."""
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))


async def question_on(world: ReviewWorld, fact_id: str, status: QuestionStatus = QuestionStatus.ASKED,
                      platform: str = "slack", **over: Any) -> FactReviewQuestion:
    """A question about `fact_id` in `status`; one that was sent was posted on `platform` first, at the ref stored."""
    fields = dict(question_id=over.pop("question_id", "q1"), account_id=ACC, user_id=USER, fact_id=fact_id,
                  quoted_text=world.docs[fact_id]["text"], **over)
    if status in (QuestionStatus.ASKED, QuestionStatus.UNANSWERED, QuestionStatus.CLOSED):
        draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive", **fields)
        labels = await texts_of(world).labels_for(USER, ACC)
        if platform == "slack":
            ref = await world.interactive.post("slack", "D1", fact_question_message(draft, labels))
        else:
            ref = MessageRef(platform=platform, channel_id="777", message_id="55")
        fields["message_ref"] = ref
    return await world.seed_question(status, **fields)


def stored_question(world: ReviewWorld, question_id: str = "q1") -> dict:
    return world.question_docs[question_id]


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def fact_review_tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review"]


# ------------------------------------------------------------------------------------------------ each answer

EXPECTED_STATE = {
    AnswerKind.STILL_TRUE: "current", AnswerKind.ENDED: "archived", AnswerKind.NEVER: "invalidated",
    AnswerKind.DONT_KNOW: "current",
}


@pytest.mark.parametrize("status", list(OWNER_SOURCES))
@pytest.mark.parametrize("kind", KINDS)
async def test_each_answer_on_each_open_question_is_applied_and_closes_it(kind, status):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact, status)
    source = OWNER_SOURCES[status]
    before = set(world.docs)

    result = await service(world).apply(ACC, USER, "q1", kind, source, world.now)

    if kind not in OPTIONS["still_true"]:
        # P2 revised (P2-3 "Answers fit their question"): an answer that is no option of this still_true question is
        # not applied, before any write; the fact, the question and the store stay as they were
        assert result is AnswerResult.NOT_APPLIED
        assert world.docs[fact]["state"] == "stale" and set(world.docs) == before
        stored = stored_question(world)
        assert (stored["status"], stored.get("closed_by"), stored.get("answer")) == (status.value, None, None)
        assert (await day_row(world)).answers_not_applied == 1
        return
    assert result is AnswerResult.APPLIED
    assert world.docs[fact]["state"] == EXPECTED_STATE[kind]
    stored = stored_question(world)
    assert (stored["status"], stored["closed_by"], stored["answer"], stored["answer_source"]) == (
        "closed", "owner", kind.value, source)
    new_docs = set(world.docs) - before
    if kind is AnswerKind.ENDED:   # a standing fact: its history fact is created beside the archive
        [history] = new_docs
        assert world.docs[history]["text"].startswith("No longer true (the owner said so in Oct 2026): ")
    else:
        assert new_docs == set()


@pytest.mark.parametrize("kind", [AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER])
async def test_the_trail_reason_is_answer_reasons_under_the_owner(kind):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)

    await service(world).apply(ACC, USER, "q1", kind, "digit", world.now)

    event = world.docs[fact]["lifecycle_events"][-1]
    assert event["actor"] == "owner" and event["reason"] == answer_reason(kind, "q1", "digit")


async def test_dont_know_uses_the_answers_moment_for_the_horizon_the_event_the_close_and_the_count():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    window = fallback_window_days(await world.repo.get_fact_by_id(fact))
    said = NOW + timedelta(days=1, hours=3)   # the owner's answer; the service's clock raises if read

    await service(world).apply(ACC, USER, "q1", AnswerKind.DONT_KNOW, "digit", said)

    doc = world.docs[fact]
    event = doc["lifecycle_events"][-1]
    assert event["at"] == said and event["actor"] == "owner"
    assert doc["expires_at"] >= said + timedelta(days=window) and doc["stale_since"] is None
    assert doc["expires_at"] < said + timedelta(days=window + 1)
    assert stored_question(world)["closed_at"] == said
    rows = [row for row in await world.counters.for_day(said.date()) if row.account_id == ACC]
    assert [row.answered_by_digit for row in rows] == [1]


async def test_still_true_uses_the_answers_moment_for_the_evidence_the_horizon_and_the_close():
    # The write path counts a horizon from its own clock; what this service decides is the update it sends.
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    window = fallback_window_days(await world.repo.get_fact_by_id(fact))
    said = NOW + timedelta(hours=3)
    writes = KeptWrites(world.store.adapter)

    await service(world, fact_updates=writes).apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", said)

    [(update, evidence_at)] = writes.sent
    assert evidence_at == said and update.expiry == Expiry(ExpiryKind.HORIZON, days=window)
    assert world.docs[fact]["reported_date"] == said and world.docs[fact]["state"] == "current"
    assert stored_question(world)["closed_at"] == said


async def test_a_naive_answer_moment_is_utc():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    said = NOW + timedelta(hours=2)

    await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", said.replace(tzinfo=None))

    assert stored_question(world)["closed_at"] == said


# ------------------------------------------------------------------------------------------------ nothing written

async def test_a_rewritten_fact_the_owner_never_saw_is_changed_and_left_alone():
    world = ReviewWorld()
    put_raw(world.store, "v1", TEXT, state="superseded", is_current=False, superseded_by="v2",
            entity={"lineage_id": "lin-v"})
    put_raw(world.store, "v2", "drinks green tea twice a week", state="stale", stale_since=NOW,
            entity={"lineage_id": "lin-v", "version": 2})
    await question_on(world, "v1")
    before = {k: dict(v) for k, v in world.docs.items()}

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.CHANGED
    assert world.docs == before and stored_question(world)["status"] == "asked"


async def test_a_followed_id_with_the_same_text_is_answered_on_the_current_version():
    world = ReviewWorld()
    put_raw(world.store, "v1", TEXT, state="superseded", is_current=False, superseded_by="v2",
            entity={"lineage_id": "lin-v"})
    put_raw(world.store, "v2", TEXT, state="stale", stale_since=NOW, tags=["health", "more"],
            entity={"lineage_id": "lin-v", "version": 2})
    await question_on(world, "v1")

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.APPLIED and world.docs["v2"]["state"] == "invalidated"


async def test_a_fact_closed_meanwhile_is_changed():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    world.docs[fact]["state"] = FactState.ARCHIVED.value

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", world.now)

    assert result is AnswerResult.CHANGED and world.docs[fact]["state"] == "archived"
    assert stored_question(world)["status"] == "asked"


async def test_a_conflict_on_the_typed_write_is_conflict(monkeypatch):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)

    async def conflicted(stored, fields):
        raise FactWriteConflict(stored.fact.id)

    monkeypatch.setattr(world.store.repo, "update_fact_fields", conflicted)

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.CONFLICT
    assert world.docs[fact]["state"] == "stale" and stored_question(world)["status"] == "asked"
    assert fact_review_tasks(world) == []


async def test_a_conflict_on_the_in_place_fallback_is_conflict(monkeypatch):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)

    async def conflicted(stored, fields):
        raise FactWriteConflict(stored.fact.id)

    monkeypatch.setattr(world.repo, "update_fact_fields", conflicted)

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.DONT_KNOW, "digit", world.now)

    assert result is AnswerResult.CONFLICT and stored_question(world)["status"] == "asked"


@pytest.mark.parametrize("account, user", [("acc-2", USER), (ACC, "usr-2")])
async def test_another_account_or_user_is_not_found(account, user):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)

    result = await service(world).apply(account, user, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.NOT_FOUND
    assert world.docs[fact]["state"] == "stale" and stored_question(world)["status"] == "asked"


async def test_a_missing_question_is_not_found():
    world = ReviewWorld()

    assert await service(world).apply(ACC, USER, "nope", AnswerKind.NEVER, "digit", world.now) is AnswerResult.NOT_FOUND


async def test_a_closed_question_is_not_open_and_nothing_is_written():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact, QuestionStatus.CLOSED)
    refreshes = counting_refreshes(world)

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.NOT_OPEN and world.docs[fact]["state"] == "stale" and refreshes == []


@pytest.mark.parametrize("refusal", [r for r in UpdateRefusal if r is not UpdateRefusal.CONFLICT])
async def test_a_refused_write_is_not_applied_counted_and_raises_nothing(refusal, caplog):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    refreshes = counting_refreshes(world)
    writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, refusal, "refused by the rules"))

    with caplog.at_level(logging.WARNING):
        result = await service(world, fact_updates=writes).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert result is AnswerResult.NOT_APPLIED
    assert (await day_row(world)).answers_not_applied == 1
    assert stored_question(world)["status"] == "asked" and refreshes == [] and fact_review_tasks(world) == []
    assert any("refused by the rules" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def test_an_expiry_error_from_the_in_place_write_is_not_applied(monkeypatch):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)

    async def born_due(stored, fields):
        raise FactBornDueError("already due")

    monkeypatch.setattr(world.repo, "update_fact_fields", born_due)

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.DONT_KNOW, "digit", world.now)

    assert result is AnswerResult.NOT_APPLIED and (await day_row(world)).answers_not_applied == 1
    assert stored_question(world)["status"] == "asked"


async def test_a_source_that_is_not_the_owners_is_refused():
    world = ReviewWorld()

    with pytest.raises(ValueError):
        await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "review", world.now)


# ------------------------------------------------------------------------------------------------ cache and counters

async def test_the_cache_is_refreshed_once_after_a_write():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    refreshes = counting_refreshes(world)

    await service(world).apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", world.now)

    assert refreshes == [ACC]


@pytest.mark.parametrize("status", list(OWNER_SOURCES))
async def test_the_answers_are_counted_by_source(status):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact, status)
    source = OWNER_SOURCES[status]

    await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, source, world.now)

    row = await day_row(world)
    counted = {"digit": row.answered_by_digit, "button": row.answered_by_button, "cabinet": row.answered_in_cabinet}
    assert counted == {name: int(name == source) for name in counted}
    assert row.answers_not_applied == 0


# ------------------------------------------------------------------------------------------------ closing

async def test_closing_the_current_question_clears_the_slot_and_releases_the_next_once():
    world = ReviewWorld()
    fact = await stale_fact(world)
    question = await question_on(world, fact)
    await world.seed_state(account_id=ACC, current_question_id="q1", sent=1, sent_on=NOW.date())
    answers = service(world, clock=world.clock)

    assert await answers.close_question(question, ClosedBy.TIMEOUT) is True
    assert await answers.close_question(question, ClosedBy.TIMEOUT) is False

    assert world.state_doc(ACC)["current_question_id"] is None
    assert fact_review_tasks(world) == [
        {"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"},
    ]


async def test_an_applied_answer_to_the_current_question_releases_the_next():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact)
    await world.seed_state(account_id=ACC, current_question_id="q1", sent=1, sent_on=NOW.date())

    await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", world.now)

    assert world.state_doc(ACC)["current_question_id"] is None
    assert [body["mode"] for body in fact_review_tasks(world)] == ["next"]


async def test_closing_another_question_leaves_the_slot_and_releases_nothing():
    world = ReviewWorld()
    fact = await stale_fact(world)
    question = await question_on(world, fact)
    await world.seed_state(account_id=ACC, current_question_id="q-other", sent=1, sent_on=NOW.date())

    assert await service(world, clock=world.clock).close_question(question, ClosedBy.REVIEW) is True

    assert world.state_doc(ACC)["current_question_id"] == "q-other" and fact_review_tasks(world) == []
    assert stored_question(world)["status"] == "closed"


async def test_every_close_of_a_slack_question_shows_its_outcome():
    world = ReviewWorld()
    closed_note = world.localization.get_ui_string(LanguageCode.EN, UIMessage.MEMORY_QUESTION_CLOSED)
    answers = service(world, clock=world.clock)
    refs = {}
    for qid, text in (("q-answer", "a"), ("q-timeout", "b"), ("q-review", "c")):
        question = await question_on(world, await stale_fact(world, text), question_id=qid)
        refs[qid] = question

    await answers.apply(ACC, USER, "q-answer", AnswerKind.ENDED, "digit", world.now)
    await answers.close_question(refs["q-timeout"], ClosedBy.TIMEOUT)
    await answers.close_question(refs["q-review"], ClosedBy.REVIEW)

    shown = {qid: world.interactive.shown[q.message_ref] for qid, q in refs.items()}
    assert shown["q-answer"].chosen_option_id == AnswerKind.ENDED.value and shown["q-answer"].note == ""
    assert shown["q-timeout"].chosen_option_id is None and shown["q-timeout"].note == closed_note
    assert shown["q-review"].chosen_option_id is None and shown["q-review"].note == closed_note
    assert all(not world.interactive.has_controls(q.message_ref) for q in refs.values())
    assert shown["q-review"].message.quote == "c"


async def test_a_show_choice_failure_is_logged_and_the_close_stands(caplog):
    world = ReviewWorld()
    question = await question_on(world, await stale_fact(world))
    world.interactive.fail_show = True

    with caplog.at_level(logging.WARNING):
        closed = await service(world, clock=world.clock).close_question(question, ClosedBy.TIMEOUT)

    assert closed is True and stored_question(world)["status"] == "closed"
    assert any("not re-rendered" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def test_a_telegram_question_closes_through_the_router_and_nothing_changes_on_the_platform():
    world = ReviewWorld()
    question = await question_on(world, await stale_fact(world), platform="telegram")
    channel = world.channels.create("telegram", "777")
    router = InteractiveMessageRouter({"slack": world.interactive,
                                       "telegram": PlainTextInteractiveMessageAdapter(world.channels)})

    closed = await service(world, interactive=router, clock=world.clock).close_question(question, ClosedBy.TIMEOUT)

    assert closed is True and stored_question(world)["status"] == "closed"
    assert channel.texts == [] and world.channels.sent == [] and world.interactive.shown == {}


async def test_a_note_that_is_not_a_review_text_is_refused():
    world = ReviewWorld()
    question = await question_on(world, await stale_fact(world))

    with pytest.raises(ValueError):
        await service(world, clock=world.clock).close_question(question, ClosedBy.TIMEOUT, note=next(m for m in UIMessage if m.value not in LABEL_KEYS))

    assert stored_question(world)["status"] == "asked"


async def test_the_close_takes_the_slots_release_from_its_own_transaction_and_reads_no_state():
    """F1: whether the next question goes out is what `close_and_release` did, not a `get_state` read beside it. The
    texts read the state through their own store; the service's store refuses a state read."""
    world = ReviewWorld()
    question = await question_on(world, await stale_fact(world))
    await world.seed_state(account_id=ACC, current_question_id="q1", sent=1, sent_on=NOW.date())
    store = FirestoreFactReviewStore(world.store.db, world.env)

    async def no_state_read(account_id):
        raise AssertionError("the close read the state outside its transaction")

    store.get_state = no_state_read  # type: ignore[method-assign]

    closed = await service(world, clock=world.clock, review_store=store).close_question(question, ClosedBy.TIMEOUT)

    assert closed is True and world.state_doc(ACC)["current_question_id"] is None
    assert [body["mode"] for body in fact_review_tasks(world)] == ["next"]
    assert world.interactive.shown[question.message_ref].note


async def test_an_answer_reads_no_state_either():
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact, QuestionStatus.QUEUED)
    store = FirestoreFactReviewStore(world.store.db, world.env)

    async def no_state_read(account_id):
        raise AssertionError("apply read the state")

    store.get_state = no_state_read  # type: ignore[method-assign]

    result = await service(world, review_store=store).apply(ACC, USER, "q1", AnswerKind.NEVER, "cabinet", world.now)

    assert result is AnswerResult.APPLIED and stored_question(world)["status"] == "closed"
