"""Retry after an effect (P15 part 2, card K): the first attempt makes its user-visible effect, then answers 5xx or
raises; the claim is released and Cloud Tasks delivers the same task name again. The retry must not repeat the effect.

Everything except the review question (item 9) runs on the redelivery world (`tests/support/redelivery_world.py`): the
real `WorkerHandler`, claim store, note store, `UserNotificationService`, `RemindersService` and `GcpTaskQueue` on one
`EvalDb`, a scripted agent turn and recording channels. A retry is modelled as `worker_handler.py:235-245` decides it: a
raise, or an answer `>= 500` / `429`, releases the claim (`needs_retry`, `deliver_with_retry`). The live agent queue
allows 5 attempts; two are enough to show a repeat.

Every strict xfail checks its precondition with `require` (a failed one reports FAILED, never the expected failure);
only its outcome is a bare `assert`. Defects pinned as strict xfails (arbiter-confirmed): P15-D1 (a reminder or daily review whose turn already posted or
enqueued is answered 500, so the retry repeats it), P15-D3 (a lost `execute_reminder` enqueue is never enqueued again:
the fire stays claimed), P15-D11 (a review question whose post succeeded but whose ref write failed is posted again the
next day, with a false "not delivered" note). P15-D2 and P15-D4 are overstated: not xfailed here (item 7 pins the
right behaviour next to D4).
"""
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.api_core.exceptions import DeadlineExceeded

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.auth import IAMDecision
from src.domain.billing import BillingAccount
from src.domain.entities import ExpiryKind
from src.domain.fact_review_frame import not_delivered_frame, review_frame
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from src.domain.messaging import RichContent
from src.domain.user import UserBotConfig, UserProfile
from src.ports.video_generation_port import VideoPollResult
from src.services import reminders_service
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from src.services.provider_registry import ProviderRegistry
from tests.support.clock_instants import pin_datetime
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.redelivery_world import (
    ACCOUNT, CHANNEL, T0, USER, RedeliveryWorld, ScriptedReview, deliver, deliver_with_retry, failed, needs_retry, ok,
    require,
)

TEXT = "Time to call dad"
REVIEW_TEXT = "Two emails today: the parents' meeting moved to Saturday."
TABLE = RichContent(content_type="table",
                    data={"headers": ["Activity", "Time"], "rows": [{"cells": ["Yoga", "Morning"]}]},
                    fallback_text="Yoga: Morning")
EMAILS = [
    {"id": "m1", "from": "school@example.com", "subject": "Parents' meeting moved to Saturday", "body": "See you there."},
    {"id": "m2", "from": "shop@example.com", "subject": "Your AirPods order shipped", "body": "Tracking inside."},
]
D1 = "P15-D1: a background turn that already posted or enqueued is answered 500, so the retry repeats it; owner decision"
D3 = "P15-D3: a lost execute_reminder enqueue leaves the fire claimed and is never enqueued again; owner decision"
D11 = ("P15-D11: a question posted whose ref write failed is treated as never posted the next day and sent again; "
       "owner decision")


def _fire_payload(world: RedeliveryWorld, note_id: str) -> dict:
    return world.payload("execute_reminder", note_id=note_id, user_id=USER, due_at=T0.isoformat())


async def _claimed_reminder(world: RedeliveryWorld, monkeypatch: Any) -> dict:
    """A one-time note due `T0`, claimed as `RemindersService.fire_due_reminders` leaves it (`last_fired` set, the note
    kept for the delivery); returns its `execute_reminder` payload."""
    note_id = await world.seed_note(monkeypatch, due=T0, last_fired=T0 + timedelta(seconds=30),
                                    created_at=T0 - timedelta(days=1))
    return _fire_payload(world, note_id)


def _review_payload(world: RedeliveryWorld) -> dict:
    return world.payload("daily_email_review", user_id=USER, account_id=ACCOUNT)


# ------------------------------------------------------------------------------------------------ 1-5: P15-D1

class TestReminderRetryAfterAnEffect:
    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D1)
    async def test_a_reminder_whose_rich_part_failed_after_its_text_is_not_posted_again(self, monkeypatch):
        """The text posts, the rich part raises: `notify` answers `delivered=False, sent=True`, the handler 500
        (`worker_handler.py:1024-1037`), the retry runs the turn and posts the text again.

        Precondition (today and after the fix): after the first attempt the text is on the channel and the rich part is
        not. Flipping fix: `_handle_execute_reminder` answers 5xx only when the agent never ran (`result.error` is
        `no_channel` or `channel_factory_returned_none:*`), else stamps the fire and answers 200 with one ops line, as
        `_send_deep_research_notice` does (`worker_handler.py:612-622`)."""
        world = RedeliveryWorld()
        await world.set_primary_channel()
        world.router.always(ok(TEXT, rich=TABLE))
        world.channels.fail_post(2)                         # the text is post 1, the rich part post 2
        payload = await _claimed_reminder(world, monkeypatch)

        first = await deliver(world.handler, payload, "t-1")
        require(world.texts == [TEXT] and [p.kind for p in world.posts] == ["text"],
                "the first attempt posted the text and not the rich part")
        if needs_retry(first):
            await deliver(world.handler, payload, "t-1")

        assert len(world.texts) == 1

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D1)
    async def test_a_reminder_turn_that_enqueued_a_report_then_failed_does_not_enqueue_it_again(self, monkeypatch):
        """The turn enqueues its ASYNC `create_html_page`, then fails: the handler answers 500 and the retry re-runs the
        turn, which enqueues the report a second time (a real Smart turn repeats its dispatch the same way).

        Precondition: after the first attempt the turn ran once and exactly one `agent_execution` body exists. Flipping
        fix: the same as the test above (5xx only when the agent never ran)."""
        world = RedeliveryWorld()
        await world.set_primary_channel()
        world.router.always(failed("provider down", enqueue=("create_html_page",)))
        payload = await _claimed_reminder(world, monkeypatch)

        first = await deliver(world.handler, payload, "t-1")
        require(len(world.router.messages) == 1 and len(world.bodies("agent_execution")) == 1,
                "the first attempt ran the turn once and it enqueued one report")
        if needs_retry(first):
            await deliver(world.handler, payload, "t-1")

        assert len(world.bodies("agent_execution")) == 1

    async def test_a_reminder_that_reached_no_channel_answers_500_and_the_retry_posts_once(self, monkeypatch):
        """Control for the two above: nothing ran (no primary channel), so the 500 and its retry are right. The retry
        under the same name posts once and the claim ends `done`.

        Regression: a fix that stops retrying when nothing was sent (the reminder is lost)."""
        world = RedeliveryWorld()
        world.router.always(ok(TEXT))
        payload = await _claimed_reminder(world, monkeypatch)

        body, status = await deliver(world.handler, payload, "t-1")
        assert (status, body["error"]) == (500, "no_channel")
        assert world.router.messages == [] and world.claim_docs == []

        await world.set_primary_channel()
        assert await deliver(world.handler, payload, "t-1") == ({"status": "ok"}, 200)
        assert world.texts == [TEXT]
        assert [d["state"] for d in world.claim_docs] == ["done"]


class TestDailyReviewRetryAfterAnEffect:
    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D1)
    async def test_a_daily_review_whose_rich_part_failed_after_its_text_is_not_posted_again(self):
        """As the reminder: the review's text posts, its rich part raises, the handler answers 500
        (`worker_handler.py:1219-1231`) and the retry posts the text again.

        Precondition: after the first attempt the text is on the channel and the rich part is not. Flipping fix: the
        same rule in `_handle_daily_email_review` (5xx only when the agent never ran)."""
        world = RedeliveryWorld(email_review=ScriptedReview(EMAILS))
        await world.set_primary_channel()
        world.router.always(ok(REVIEW_TEXT, rich=TABLE))
        world.channels.fail_post(2)
        payload = _review_payload(world)

        first = await deliver(world.handler, payload, "t-1")
        require(world.texts == [REVIEW_TEXT] and [p.kind for p in world.posts] == ["text"],
                "the first attempt posted the text and not the rich part")
        if needs_retry(first):
            await deliver(world.handler, payload, "t-1")

        assert len(world.texts) == 1

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D1)
    async def test_a_daily_review_turn_that_enqueued_its_report_then_failed_does_not_enqueue_it_again(self):
        """The review turn enqueues its HTML report, then fails: 500, and the retry enqueues the report again.

        Precondition: after the first attempt the turn ran once and exactly one `agent_execution` body exists. Flipping
        fix: the same rule in `_handle_daily_email_review`."""
        world = RedeliveryWorld(email_review=ScriptedReview(EMAILS))
        await world.set_primary_channel()
        world.router.always(failed("provider down", enqueue=("create_html_page",)))
        payload = _review_payload(world)

        first = await deliver(world.handler, payload, "t-1")
        require(len(world.router.messages) == 1 and len(world.bodies("agent_execution")) == 1,
                "the first attempt ran the turn once and it enqueued one report")
        if needs_retry(first):
            await deliver(world.handler, payload, "t-1")

        assert len(world.bodies("agent_execution")) == 1

    async def test_a_daily_review_that_reached_no_channel_answers_500_and_the_retry_posts_once(self):
        """Control for the two above: no primary channel, nothing ran; 500, then the retry posts once, claim `done`.

        Regression: a fix that stops retrying when nothing was sent (the day's review is lost)."""
        world = RedeliveryWorld(email_review=ScriptedReview(EMAILS))
        world.router.always(ok(REVIEW_TEXT))
        payload = _review_payload(world)

        body, status = await deliver(world.handler, payload, "t-1")
        assert (status, body["error"]) == (500, "no_channel")
        assert world.router.messages == [] and world.claim_docs == []

        await world.set_primary_channel()
        assert await deliver(world.handler, payload, "t-1") == ({"status": "ok", "emails": 2}, 200)
        assert world.texts == [REVIEW_TEXT]
        assert [d["state"] for d in world.claim_docs] == ["done"]


# ------------------------------------------------------------------------------------------------ 6: P15-D3

class TestLostReminderEnqueue:
    """`fire_due_reminders` is a Scheduler type: no claim, `delivery=None`; its "retry" is the next tick. The fire is
    claimed on the note (`claim_one_time_if_due_at`) before `execute_reminder` is enqueued (`reminders_service.py:151-181`),
    so an enqueue that raises leaves a claimed fire no tick enqueues again."""

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D3)
    async def test_a_lost_reminder_enqueue_is_enqueued_by_the_next_tick(self, monkeypatch):
        """Tick 1 claims the note and its create fails before landing; tick 2 must enqueue the fire.

        Precondition (today and after either fix): the create was attempted once and failed, and tick 1 either answered
        or raised that very `DeadlineExceeded`. Flipping fix (either works): enqueue under a name
        `execute_reminder:<note>@<due>` with a retried create (precedent `src/services/deep_research_delivery.py:282-304`),
        or undo the claim when the enqueue raises."""
        world = RedeliveryWorld()
        note_id = await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1))
        tick = world.payload("fire_due_reminders")
        world.client.fail_next(landed=False)

        pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
        try:
            await deliver(world.handler, tick)
        except DeadlineExceeded:
            pass
        require(world.client.failed_attempts == 1, "tick 1 attempted the create and it failed")

        pin_datetime(monkeypatch, reminders_service, T0 + timedelta(minutes=5))
        await deliver(world.handler, tick)

        fires = world.bodies("execute_reminder")
        assert [{k: b[k] for k in ("note_id", "user_id", "due_at")} for b in fires] == [
            {"note_id": note_id, "user_id": USER, "due_at": T0.isoformat()}]
        assert note_id in world.note_docs

    async def test_two_ticks_without_a_failure_enqueue_the_fire_once(self, monkeypatch):
        """Control: with no failure the first tick claims and enqueues the fire, the second finds it claimed.

        Regression: a fix that re-enqueues every claimed-but-undelivered fire on each tick (one fire, many runs)."""
        world = RedeliveryWorld()
        note_id = await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1))
        tick = world.payload("fire_due_reminders")

        pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
        assert await deliver(world.handler, tick) == ({"enqueued": 1, "claim_lost": 0, "skipped": 0}, 200)
        pin_datetime(monkeypatch, reminders_service, T0 + timedelta(minutes=5))
        await deliver(world.handler, tick)

        fires = world.bodies("execute_reminder")
        assert [(b["note_id"], b["due_at"]) for b in fires] == [(note_id, T0.isoformat())]
        assert note_id in world.note_docs


# ------------------------------------------------------------------------------------------------ 7: the video poll (pin)

class PendingVideo:
    """The video port as the poll reads it: every `get_status` answers pending."""

    async def get_status(self, request_id: str) -> VideoPollResult:
        return VideoPollResult("pending")


class TestLostVideoPollEnqueue:
    async def test_a_lost_next_poll_enqueue_is_retried_and_the_chain_continues_once(self):
        """The next poll's create fails before landing: the raise propagates (`worker_handler.py:779-786`, outside the
        `try` at `:766-777`), the claim is released, and the retry enqueues exactly one next poll.

        Regression: the enqueue error swallowed into a 200 (the chain dies, the user never gets the video).
        The landed-then-error variant (two chains) is P15-D4, overstated by the arbiter (a rare gRPC deadline after the
        create landed) and deliberately not pinned."""
        registry: ProviderRegistry = ProviderRegistry()
        registry.register("grok", PendingVideo())
        world = RedeliveryWorld(video_registry=registry)
        payload = world.payload("video_generation_polling", request_id="r1", user_id=USER, account_id=ACCOUNT,
                                session_id=f"{USER}:{CHANNEL}", attempt=0)
        world.client.fail_next(landed=False)

        try:
            first: Any = await deliver(world.handler, payload, "t-v")
        except DeadlineExceeded as exc:
            first = exc
        assert isinstance(first, DeadlineExceeded)
        assert world.claim_docs == []

        assert await deliver(world.handler, payload, "t-v") == ({"status": "polling", "attempt": 1}, 200)
        assert [b["attempt"] for b in world.bodies("video_generation_polling")] == [1]
        assert [d["state"] for d in world.claim_docs] == ["done"]


# ------------------------------------------------------------------------------------------------ 8: chat intake (pins)

TELEGRAM_UPDATE = {"update_id": 601, "message": {"message_id": 21, "date": 1700000000,
                   "chat": {"id": 1, "type": "private"}, "from": {"id": 1, "is_bot": False, "first_name": "U"},
                   "text": "hello"}}


class Recorder:
    """Records each call, then raises: a turn that wrote its effect and then failed."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(args)
        raise RuntimeError("turn failed after its reply")


class AllowU1:
    """The IAM boundary: Slack user `U1` is the user `u1`."""

    async def authorize(self, platform: str, platform_user_id: str, **kwargs: Any) -> IAMDecision:
        return IAMDecision(action="allow", user=UserProfile(user_id=USER, account_id=ACCOUNT, display_name="U1"))


class RecordingConversation:
    """The conversation handler as the Slack adapter calls it: `handle_message` records, then raises."""

    def __init__(self) -> None:
        self.messages: List[Any] = []

    async def handle_message(self, context: Any, response_channel: Any) -> None:
        self.messages.append(context)
        raise RuntimeError("turn failed after posting")


class TestChatIntakeIsNotRunAgain:
    async def test_a_telegram_update_that_failed_after_its_reply_is_not_run_again(self):
        """`handle_queued_update` never answers 5xx (`webhook_adapter.py:168-177`): a turn that raised after its reply
        is acked, the claim ends `done`, and Cloud Tasks does not run it again.

        Regression: `:175-177` turned into a 5xx (Cloud Tasks re-runs a turn already written). The adapter is built as
        `tests/unit/adapters/test_telegram_queued_updates.py` builds it; its constructor fillers are never read here."""
        world = RedeliveryWorld()
        dedup = MagicMock()
        dedup.try_mark_processed = AsyncMock(return_value=True)
        adapter = TelegramWebhookAdapter(token="1:abc", webhook_secret="s", dedup_store=dedup,
                                         session_store=MagicMock(), conversation_handler=AsyncMock(),
                                         iam_service=AsyncMock(), task_queue=world.queue)
        recorder = Recorder()
        adapter._process_message = recorder  # type: ignore[method-assign]
        world.handler.attach_telegram_worker(adapter.handle_queued_update)

        outcomes = await deliver_with_retry(world.handler, {"task_type": "telegram_update", "update": TELEGRAM_UPDATE},
                                            "t-tg")

        assert outcomes == [({"ok": True}, 200)]
        assert len(recorder.calls) == 1
        assert [d["state"] for d in world.claim_docs] == ["done"]

    async def test_a_slack_event_whose_turn_failed_after_posting_is_not_run_again(self):
        """`_process_message_event` swallows a failed turn (`http_adapter.py:536-537`): the queued event is acked, the
        claim ends `done`, and the turn is not run again.

        Regression: the swallow removed → 500 → Cloud Tasks re-runs a turn that already posted. Not a duplicate of
        `test_worker_handler_task_claim.py:300-303`, which pins that a raise from `_process_message_event` itself is a 500.
        The adapter is built as the fixture at `test_worker_handler_task_claim.py:227-237`."""
        world = RedeliveryWorld()
        conversation = RecordingConversation()
        adapter = HTTPModeAdapter(app=AsyncMock(), config={"SLACK_SIGNING_SECRET": "s", "SLACK_BOT_TOKEN": "xoxb-test"},
                                  task_service=AsyncMock(), session_store=AsyncMock(),
                                  conversation_handler=conversation, iam_service=AllowU1())  # type: ignore[arg-type]
        world.handler.attach_slack_worker(adapter.handle_queued_event)
        payload = {"event": {"event_id": "E1", "event": {"type": "message", "text": "hi", "user": "U1",
                                                         "channel": CHANNEL}},
                   "session_id": f"{USER}:{CHANNEL}"}

        outcomes = await deliver_with_retry(world.handler, payload, "t-sl")

        assert outcomes == [({"ok": True}, 200)]
        assert [m.text for m in conversation.messages] == ["hi"]
        assert [d["state"] for d in world.claim_docs] == ["done"]


# ------------------------------------------------------------------------------------------------ 9: P15-D11

ZONE = "Europe/Berlin"
REVIEW_CHANNEL = "C1"
SESSION = f"{USER}:{REVIEW_CHANNEL}"
FACTS = {
    "q1": "User plays tennis on Sundays.",
    "q2": "User does morning yoga for 30 minutes.",
}
LOST = "ref write lost"


class Accounts:
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return BillingAccount(account_id=ACC, iam_policy={USER: "owner"}) if account_id == ACC else None


class Users:
    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return UserProfile(user_id=USER, account_id=ACC, config=UserBotConfig(timezone=ZONE)) if user_id == USER else None


class Agents:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


class NoModel:
    """No stale fact is due, so no review pass is routed."""

    async def route_message(self, message: Any) -> Any:
        # Not an AssertionError: a strict xfail in this file would take it for its expected failure if it escaped.
        raise RuntimeError("no review pass is routed in these tests")


class Rig:
    """`FactReviewService` on the review world, built as the private `Rig` of
    `tests/unit/services/test_fact_review_questions.py:99-129` (re-declared, in Europe/Berlin)."""

    def __init__(self, world: ReviewWorld) -> None:
        self.world = world
        accounts, users = Accounts(), Users()
        zones = OwnerTimezoneResolver(accounts, users)
        texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                  LanguageCode.EN, clock=world.clock)
        answers = FactAnswerService(
            fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
            task_queue=world.queue, session_store=world.session_store, texts=texts, interactive=world.interactive,
            owner_zones=zones, clock=world.clock,
        )
        self.service = FactReviewService(
            repo=world.repo, review_store=world.review_store, counters=world.counters, lease=world.lease,
            coordinator=NoModel(), agent_factory=Agents(), user_repo=users, account_repo=accounts,
            texts=texts, owner_zones=zones, notification_state=world.notification_state,
            channel_bindings=world.channel_bindings, notifications=world.notifier, session_store=world.session_store,
            task_queue=world.queue, answer_service=answers, interactive=world.interactive, alert_sink=world.sink,
            run_timeout_s=600, clock=world.clock,
            fact_updates=world.store.adapter, cabinet_url="https://cabinet.example/cabinet?tab=memory",
        )

    async def run(self, mode: str = "daily") -> Dict[str, Any]:
        return await self.service.run(USER, ACC, mode)  # type: ignore[arg-type]


async def _queued(world: ReviewWorld, question_id: str, stale_days: float,
                  fact_id: Optional[str] = None) -> FactReviewQuestion:
    return await world.seed_question(
        QuestionStatus.QUEUED, question_id=question_id, account_id=ACC, user_id=USER,
        fact_id=fact_id or f"f-{question_id}", quoted_text=FACTS[question_id],
        queue_key=NOW - timedelta(days=stale_days), queued_at=NOW - timedelta(days=2),
    )


async def _two_questions() -> ReviewWorld:
    """As `test_fact_review_questions.py:554-560`: q1 asks about a stored fact and is the longest stale, q2 waits."""
    world = ReviewWorld()
    await world.notification_state.save_primary(USER, "slack", REVIEW_CHANNEL)
    await world.seed_state(account_id=ACC)
    await _queued(world, "q2", stale_days=60)
    fact_id = await world.seed(FACTS["q1"], expiry_kind=ExpiryKind.HORIZON, ttl_days=60,
                               reported_date=NOW - timedelta(days=40), expires_at=NOW + timedelta(days=20))
    await _queued(world, "q1", stale_days=90, fact_id=fact_id)
    return world


def _posted_ids(world: ReviewWorld) -> List[str]:
    return [message.binding.id for _, message in world.interactive.posted]


async def _history(world: ReviewWorld) -> List[str]:
    return [m.parts[0].text for m in await world.session_store.messages(SESSION)]


class TestReviewQuestionPostedOnce:
    """A `fact_review` run that raises is released and retried like any task (`worker_handler.py:237-239`); its
    housekeeping decides what a question left pending became (`fact_review_service.py:355-385`): with a message ref it
    was delivered, without one it was never posted. The post is `:580`, the ref write `:586`, `mark_asked` `:589`. Not a
    duplicate of `test_fact_review_questions.py:554-584`, which crashes `mark_asked`, after the ref is stored."""

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D11)
    async def test_a_question_posted_whose_ref_write_failed_is_not_posted_again(self):
        """The post succeeds and the ref write raises once; the next day's run must not post q1 again nor tell the
        owner it was not delivered.

        Precondition (today and after the fix): the first run raised that very error and q1 was posted once. Flipping
        fix: record that the question was posted (e.g. `posted_at`) in a write right after the post and before
        `set_message_ref`; housekeeping completes a posted question as asked even without a ref (`completed()` and the
        asked invariant accept the mark) and retracts only questions never posted."""
        world = await _two_questions()
        r = Rig(world)
        set_message_ref = world.review_store.set_message_ref

        async def lost(question_id: str, ref: Any) -> bool:
            raise RuntimeError(LOST)

        world.review_store.set_message_ref = lost  # type: ignore[method-assign]
        raised = False
        try:
            await r.run("next")
        except RuntimeError as exc:
            if str(exc) != LOST:
                raise
            raised = True
        finally:
            world.review_store.set_message_ref = set_message_ref  # type: ignore[method-assign]
        require(raised and _posted_ids(world) == ["q1"], "the first run raised the lost ref write and posted q1 once")

        world.advance(days=1)
        await r.run()

        assert _posted_ids(world) == ["q1", "q2"]
        assert not_delivered_frame("q1") not in await _history(world)

    async def test_a_question_posted_and_recorded_is_not_posted_again(self):
        """Control: the same two runs without the crash. Day 1 posts q1; the next day q1 becomes unanswered and q2
        goes; no "not delivered" note.

        Regression: a fix that keeps a delivered question current forever, or resends every unanswered one."""
        world = await _two_questions()
        r = Rig(world)

        await r.run("next")
        require(_posted_ids(world) == ["q1"], "the first run posted q1 once")

        world.advance(days=1)
        await r.run()

        assert _posted_ids(world) == ["q1", "q2"]
        history = await _history(world)
        assert not_delivered_frame("q1") not in history
        assert history.count(review_frame("q1")) == 1
