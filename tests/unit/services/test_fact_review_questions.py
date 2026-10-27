"""`FactReviewService.run`: sending one question at a time (Facts S4 · T8d1, plan §3.7, §3.9 posting, §3.17, §3.20).

The plain service on the review world: the real review store, session store, task queue, counters and repository over
one evaluating Firestore fake, a real `FactAnswerService`, `FactQuestionTexts` and `OwnerTimezoneResolver`, and the
world's recording interactive port (it keeps what was posted, answers a ref, and fails when told to). No run here routes
to the model: no stale fact is seeded, so a daily run selects nothing. Assertions are on the stored questions, the
state, the session history, the posted messages, the counters and the queued tasks."""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.billing import BillingAccount
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.entities import ExpiryKind
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_frame import NOT_DELIVERED_REPLY, not_delivered_frame, question_text, review_frame
from src.domain.fact_review_language import labels_digest
from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_rules import MAX_QUESTIONS_PER_DAY
from src.domain.fact_review_state import FactReviewState
from src.domain.fact_review_task_keys import FACT_REVIEW_TASK
from src.domain.interactive_message import InteractiveMessage
from src.domain.language import LanguageCode
from src.domain.messaging import MessageRef
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_p2_labels import RU_P2_LABELS
from tests.support.review_fact_updates import RecordingFactUpdates

USER = "u1"
TZ = "Europe/Lisbon"            # summer time until 25 October: the world's NOW, 07:30 UTC, is 08:30 the same day there
CHANNEL = "D1"
SESSION = f"{USER}:{CHANNEL}"
OPTION_IDS = ["still_true", "ended", "never", "dont_know"]
# 23:30 UTC on 23 October is 00:30 on 24 October in Lisbon: the owner's day is not the UTC day.
LATE = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)

FACTS = {
    "q1": "User trains at the Iron Temple gym.",
    "q2": "User's mother Maria likes tea.",
    "q3": "User has a daughter, Ana.",
    "q4": "User works at Acme as a data engineer.",
    "q5": "User borrows books from the Example Library.",
    "q6": "User's sister Hanna lives in Porto.",
}

RU_LABELS = {
    UIMessage.MEMORY_REVIEW_QUESTION.value: "Это всё ещё так?",
    UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Да",
    UIMessage.MEMORY_OPTION_ENDED.value: "Уже нет",
    UIMessage.MEMORY_OPTION_NEVER.value: "Никогда",
    UIMessage.MEMORY_OPTION_DONT_KNOW.value: "Не помню",
    UIMessage.MEMORY_REPLY_HINT.value: "Ответьте на это сообщение цифрой или словами.",
    UIMessage.MEMORY_ANSWER_NOTED.value: "Спасибо, записал.",
    UIMessage.MEMORY_REPLY_NOTED.value: "Принято, учту.",
    UIMessage.MEMORY_ALREADY_ANSWERED.value: "Уже отвечено, спасибо.",
    UIMessage.MEMORY_FACT_CHANGED.value: "Этот факт тем временем изменился, ответ не применён; я к нему ещё вернусь.",
    UIMessage.MEMORY_QUESTION_CLOSED.value: "Этот вопрос закрыт.",
    UIMessage.MEMORY_DIGEST_HEADER.value: "Факты, которые я отложил на этой неделе:",
    UIMessage.MEMORY_DIGEST_FOOTER.value: "Чтобы вернуть один, скажите мне в чате, что это всё ещё так.",
    UIMessage.MEMORY_DIGEST_ARCHIVED.value: "это уже не так",
    UIMessage.MEMORY_DIGEST_INVALIDATED.value: "этого никогда не было",
    **RU_P2_LABELS,  # P2 revised (C11): the set is the 28 label keys
}


class Accounts:
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return BillingAccount(account_id=ACC, iam_policy={USER: "owner"}) if account_id == ACC else None


class Users:
    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return UserProfile(user_id=USER, account_id=ACC, config=UserBotConfig(timezone=TZ)) if user_id == USER else None


class Agents:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


class NoModel:
    """No run here selects a fact, so nothing is routed."""

    async def route_message(self, message: Any) -> Any:
        raise AssertionError("no review pass is routed in these tests")


class Rig:
    def __init__(self, world: ReviewWorld) -> None:
        self.world = world
        accounts, users = Accounts(), Users()
        zones = OwnerTimezoneResolver(accounts, users)
        self.texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                       LanguageCode.EN, clock=world.clock)
        self.answers = FactAnswerService(
            fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
            task_queue=world.queue, session_store=world.session_store, texts=self.texts, interactive=world.interactive,
            owner_zones=zones, clock=world.clock,
        )
        self.refreshes: List[str] = []
        real = world.repo.refresh_biographical_context_cache

        async def refresh(owner_id: str, *args: Any, **kwargs: Any) -> None:
            self.refreshes.append(owner_id)
            await real(owner_id, *args, **kwargs)

        world.repo.refresh_biographical_context_cache = refresh  # type: ignore[method-assign]
        self.service = FactReviewService(
            repo=world.repo, review_store=world.review_store, counters=world.counters, lease=world.lease,
            coordinator=NoModel(), agent_factory=Agents(), user_repo=users, account_repo=accounts,
            texts=self.texts, owner_zones=zones, notification_state=world.notification_state,
            channel_bindings=world.channel_bindings, notifications=world.notifier, session_store=world.session_store,
            task_queue=world.queue, answer_service=self.answers, interactive=world.interactive, alert_sink=world.sink,
            run_timeout_s=600, clock=world.clock,
            fact_updates=RecordingFactUpdates(world.store.adapter), cabinet_url="https://cabinet.example/cabinet?tab=memory",
        )

    async def run(self, mode: str = "daily") -> Dict[str, Any]:
        return await self.service.run(USER, ACC, mode)  # type: ignore[arg-type]

    async def answer(self, question_id: str) -> None:
        """The owner answers in the Cabinet: the close path every answer takes (`close_question`, owner, `cabinet`)."""
        await self.answers.close_question(await stored(self.world, question_id), ClosedBy.OWNER, AnswerKind.STILL_TRUE,
                                          "cabinet")


# ------------------------------------------------------------------------------------------------ seeding and reading

async def world_with(*question_ids: str, primary: bool = True, at: datetime = NOW) -> ReviewWorld:
    """A world at `at` with the owner's primary channel, a review state and the queued questions `question_ids`, queued
    in that order: the first is the longest stale."""
    world = ReviewWorld()
    world.now = at
    if primary:
        await world.notification_state.save_primary(USER, "slack", CHANNEL)
    await world.seed_state(account_id=ACC)
    for number, question_id in enumerate(question_ids):
        await queued(world, question_id, stale_days=60 - number)
    return world


async def queued(world: ReviewWorld, question_id: str, stale_days: float, queued_days: float = 2,
                 fact_id: Optional[str] = None, text: Optional[str] = None) -> FactReviewQuestion:
    return await world.seed_question(
        QuestionStatus.QUEUED, question_id=question_id, account_id=ACC, user_id=USER,
        fact_id=fact_id or f"f-{question_id}", quoted_text=text or FACTS[question_id],
        queue_key=NOW - timedelta(days=stale_days), queued_at=NOW - timedelta(days=queued_days),
    )


async def stored(world: ReviewWorld, question_id: str) -> FactReviewQuestion:
    return (await world.review_store.get_questions([question_id]))[0]


async def state(world: ReviewWorld) -> FactReviewState:
    found = await world.review_store.get_state(ACC)
    assert found is not None
    return found


async def history(world: ReviewWorld) -> List[tuple]:
    return [(m.role, m.parts[0].text) for m in await world.session_store.messages(SESSION)]


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def posted(world: ReviewWorld) -> List[InteractiveMessage]:
    return [message for _, message in world.interactive.posted]


def posted_ids(world: ReviewWorld) -> List[str]:
    return [message.binding.id for message in posted(world)]


def next_tasks(world: ReviewWorld) -> List[Dict[str, Any]]:
    return [body for task_type, body in world.enqueued if task_type == FACT_REVIEW_TASK and body.get("mode") == "next"]


def local_day(moment: datetime) -> date:
    return moment.astimezone(timezone(timedelta(hours=1))).date()     # Lisbon in summer time (WEST, UTC+1)


# ------------------------------------------------------------------------------------------------ the message and the pair

class TestOneQuestion:
    async def test_one_fact_per_message_with_its_quote_and_the_four_options_in_order(self):
        world = await world_with("q1", "q2")
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        [(ref, message)] = world.interactive.posted
        assert (ref.platform, ref.channel_id) == ("slack", CHANNEL)
        assert message.prompt == "Is this still true?"
        assert message.quote == FACTS["q1"]
        assert [o.id for o in message.options] == OPTION_IDS
        assert [o.label for o in message.options] == ["Yes", "No longer", "Never", "Don't remember"]
        assert (message.binding.kind, message.binding.id) == ("fact_question", "q1")
        sent = await stored(world, "q1")
        assert sent.status is QuestionStatus.ASKED and sent.asked_at == world.now and sent.appended_at == world.now
        assert sent.message_ref == ref and sent.session_id == SESSION
        after = await state(world)
        assert (after.current_question_id, after.sent, after.sent_on) == ("q1", 1, local_day(world.now))
        assert (await stored(world, "q2")).status is QuestionStatus.QUEUED
        assert (await day_row(world)).questions_sent == 1
        assert r.refreshes == []                                    # sending writes no fact

    async def test_the_stored_pair_is_the_frame_and_the_message_as_text(self):
        world = await world_with("q1")
        await Rig(world).run()

        [message] = posted(world)
        assert await history(world) == [("user", review_frame("q1")), ("model", question_text(message))]
        model = (await world.session_store.messages(SESSION))[1].parts[0]
        assert model.full_text == model.text
        assert model.text == f"Is this still true?\n«{FACTS['q1']}»\n1 — Yes  2 — No longer  3 — Never  4 — Don't remember"

    async def test_the_consolidation_reads_the_quoted_fact_only_in_the_model_part(self):
        world = await world_with("q1")
        await Rig(world).run()

        serialized = serialize_messages_for_consolidation(await world.session_store.messages(SESSION))

        user_texts = [p["text"] for m in serialized if m["role"] == "user" for p in m["parts"]]
        model_texts = [p["text"] for m in serialized if m["role"] == "model" for p in m["parts"]]
        assert user_texts == [review_frame("q1")]
        assert all("Iron Temple" not in text for text in user_texts)
        assert any(FACTS["q1"] in text for text in model_texts)


# ------------------------------------------------------------------------------------------------ one at a time, five a day

class TestOneAtATime:
    async def test_no_second_question_goes_while_one_is_current(self):
        world = await world_with("q1", "q2")
        r = Rig(world)

        await r.run()
        await r.run("next")
        await r.run()

        assert posted_ids(world) == ["q1"]
        assert (await state(world)).sent == 1
        assert (await stored(world, "q2")).status is QuestionStatus.QUEUED

    async def test_after_an_answer_the_next_goes_at_once_up_to_five_a_day_and_the_sixth_waits_for_the_next_day(self):
        world = await world_with("q1", "q2", "q3", "q4", "q5", "q6")
        r = Rig(world)

        await r.run()
        for number in range(1, MAX_QUESTIONS_PER_DAY):
            await r.answer(f"q{number}")
            assert len(next_tasks(world)) == number                 # the close released the next one
            await r.run("next")
        assert posted_ids(world) == ["q1", "q2", "q3", "q4", "q5"]
        await r.answer("q5")
        await r.run("next")

        assert posted_ids(world) == ["q1", "q2", "q3", "q4", "q5"]  # five today: the sixth waits
        after = await state(world)
        assert (after.sent, after.current_question_id) == (MAX_QUESTIONS_PER_DAY, None)

        world.advance(days=1)
        await r.run("next")

        assert posted_ids(world) == ["q1", "q2", "q3", "q4", "q5", "q6"]
        after = await state(world)
        assert (after.sent, after.sent_on, after.current_question_id) == (1, local_day(world.now), "q6")

    async def test_a_day_with_no_answer_sends_one_question_and_the_next_goes_the_next_day(self):
        world = await world_with("q1", "q2", "q3")
        r = Rig(world)

        await r.run()
        world.advance(hours=6)
        await r.run()
        assert posted_ids(world) == ["q1"]

        world.advance(days=1)
        await r.run()

        assert posted_ids(world) == ["q1", "q2"]
        assert (await stored(world, "q1")).status is QuestionStatus.UNANSWERED
        assert (await state(world)).current_question_id == "q2"

    async def test_a_redelivered_next_task_sends_nothing_twice(self):
        world = await world_with("q1", "q2", "q3")
        r = Rig(world)
        await r.run()
        await r.answer("q1")

        await r.run("next")
        await r.run("next")                                         # Cloud Tasks delivered the release again

        assert posted_ids(world) == ["q1", "q2"]
        assert (await state(world)).sent == 2
        assert (await stored(world, "q3")).status is QuestionStatus.QUEUED

    async def test_the_queue_goes_longest_stale_first(self):
        world = await world_with()
        await queued(world, "q1", stale_days=5, queued_days=30)    # queued long ago, stale only recently
        await queued(world, "q2", stale_days=90, queued_days=1)    # the longest stale
        await queued(world, "q3", stale_days=40, queued_days=10)
        r = Rig(world)

        for question_id in ("q2", "q3", "q1"):
            await r.run("next")
            assert posted_ids(world)[-1] == question_id
            await r.answer(question_id)


# ------------------------------------------------------------------------------------------------ the channel

class TestChannel:
    @pytest.mark.parametrize("case", ["no primary channel", "companion-bound primary channel"])
    async def test_without_a_usable_primary_channel_nothing_is_sent_and_the_queue_waits(self, case, caplog):
        world = await world_with("q1", primary=case != "no primary channel")
        await world.notification_state.save(USER, "telegram", "777")      # a last active channel is no primary
        if case == "companion-bound primary channel":
            await world.channel_bindings.save(ChannelBinding(
                channel_id=CHANNEL, agent_type="tutor", intent="tutor_chat", created_by=USER,
                companion_config=CompanionConfig(window_threshold=20, batch_size=10)))
        r = Rig(world)

        with caplog.at_level(logging.INFO):
            assert await r.run() == {"status": "completed"}
            assert await r.run("next") == {"status": "completed"}

        assert posted(world) == []
        assert (await stored(world, "q1")).status is QuestionStatus.QUEUED
        after = await state(world)
        assert (after.current_question_id, after.sent) == (None, 0)
        assert await world.session_store.messages(SESSION) == []
        notes = [rec for rec in caplog.records if "the queue waits" in rec.getMessage()]
        assert len(notes) == 2 and {rec.levelno for rec in notes} == {logging.INFO}   # item 3: INFO on every run

    async def test_a_primary_channel_bound_without_a_companion_gets_the_question(self):
        world = await world_with("q1")
        await world.channel_bindings.save(ChannelBinding(channel_id=CHANNEL, agent_type="smart", intent="chat",
                                                         created_by=USER))

        await Rig(world).run()

        assert posted_ids(world) == ["q1"]


# ------------------------------------------------------------------------------------------------ failures

class TestFailures:
    async def test_an_append_failure_puts_the_question_back_at_the_front_and_posts_nothing(self):
        world = await world_with("q1", "q2")
        seeded = await stored(world, "q1")
        world.session_store.fail = True
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        assert posted(world) == []
        back = await stored(world, "q1")
        assert back.status is QuestionStatus.QUEUED and back.queue_key == seeded.queue_key and back.session_id is None
        after = await state(world)
        assert (after.current_question_id, after.sent, after.delivery_failed_on) == (None, 1, None)   # sent kept

        world.session_store.fail = False
        await r.run("next")                                         # no failed delivery: it may go the same day

        assert posted_ids(world) == ["q1"]
        assert (await state(world)).sent == 2

    async def test_a_post_failure_retracts_the_question_and_retries_it_the_next_owner_day_not_the_same_one(self):
        world = await world_with("q1", "q2", at=LATE)
        world.interactive.fail = True
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        assert posted(world) == []
        assert [role for role, _ in await history(world)] == ["user", "model", "user", "model"]
        assert (await history(world))[2:] == [("user", not_delivered_frame("q1")), ("model", NOT_DELIVERED_REPLY)]
        back = await stored(world, "q1")
        assert back.status is QuestionStatus.QUEUED and back.appended_at is None and back.session_id is None
        after = await state(world)
        assert after.current_question_id is None
        assert after.delivery_failed_on == date(2026, 10, 24)      # the owner's day, not the UTC day (23 October)
        assert (await day_row(world)).not_delivered == 1

        world.interactive.fail = False
        world.advance(hours=22)                                     # 21:30 UTC, 22:30 in Lisbon: the same owner day
        await r.run("next")
        await r.run()
        assert posted(world) == []

        world.advance(hours=2)                                      # 00:30 on 25 October in Lisbon
        await r.run("next")

        assert posted_ids(world) == ["q1"]
        assert (await stored(world, "q1")).status is QuestionStatus.ASKED

    async def test_a_failed_requeue_after_a_post_failure_appends_no_second_not_delivered_pair(self):
        world = await world_with("q1")
        world.interactive.fail = True
        requeue = world.review_store.requeue_front

        async def failing_requeue(question_id: str, failed_today: bool) -> bool:
            raise RuntimeError("store unavailable")

        world.review_store.requeue_front = failing_requeue  # type: ignore[method-assign]
        r = Rig(world)

        with pytest.raises(RuntimeError, match="store unavailable"):
            await r.run()

        stuck = await stored(world, "q1")
        assert stuck.status is QuestionStatus.PENDING and stuck.appended_at is None   # retracted, not requeued
        world.review_store.requeue_front = requeue  # type: ignore[method-assign]
        world.interactive.fail = False
        world.advance(days=1)

        await r.run()                                               # housekeeping requeues it, then it is sent again

        texts = [text for _, text in await history(world)]
        assert texts.count(not_delivered_frame("q1")) == 1
        assert texts.count(review_frame("q1")) == 2
        assert posted_ids(world) == ["q1"]

    async def test_a_stuck_retraction_whose_requeue_failed_is_not_retracted_twice_by_housekeeping(self):
        world = await world_with(primary=False)
        await world.seed_question(QuestionStatus.PENDING, question_id="q1", account_id=ACC, user_id=USER,
                                  fact_id="f-q1", quoted_text=FACTS["q1"], session_id=SESSION,
                                  appended_at=world.now - timedelta(days=1))           # not current: its send is over
        requeue = world.review_store.requeue_front
        calls: List[str] = []

        async def failing_once(question_id: str, failed_today: bool) -> bool:
            calls.append(question_id)
            if len(calls) == 1:
                raise RuntimeError("store unavailable")
            return await requeue(question_id, failed_today)

        world.review_store.requeue_front = failing_once  # type: ignore[method-assign]
        r = Rig(world)

        with pytest.raises(RuntimeError, match="store unavailable"):
            await r.run()
        await r.run()                                               # the Cloud Tasks retry

        assert await history(world) == [("user", not_delivered_frame("q1")), ("model", NOT_DELIVERED_REPLY)]
        assert (await stored(world, "q1")).status is QuestionStatus.QUEUED

    async def test_a_message_that_cannot_be_built_puts_the_question_back_and_raises(self):
        world = await world_with("q1")
        r = Rig(world)

        async def unreadable(*args: Any) -> Any:
            raise RuntimeError("language setting unavailable")

        r.texts.message_for = unreadable  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="language setting unavailable"):
            await r.run("next")

        assert posted(world) == [] and await world.session_store.messages(SESSION) == []
        assert (await stored(world, "q1")).status is QuestionStatus.QUEUED
        assert (await state(world)).current_question_id is None


# ------------------------------------------------------------------------------------------------ closed meanwhile

class TestClosedWhilePending:
    async def test_a_question_closed_during_its_post_stays_closed_and_the_next_is_released_once(self):
        world = await world_with("q1", "q2")
        r = Rig(world)
        post = world.interactive.post

        async def clicked_in_the_cabinet(platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
            ref = await post(platform, channel_id, message)
            await r.answer("q1")
            return ref

        world.interactive.post = clicked_in_the_cabinet  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        closed = await stored(world, "q1")
        assert closed.status is QuestionStatus.CLOSED and closed.answer is AnswerKind.STILL_TRUE
        assert closed.message_ref is None                           # the close came first: the ref is not kept
        assert len(next_tasks(world)) == 1                          # the close's release; the sender adds none
        # The owner saw it: no not-delivered pair, and it counts as sent (reviewer, T8d1 review: beyond-plan items 3, 4).
        assert [text for _, text in await history(world)] == [review_frame("q1"), question_text(posted(world)[0])]
        assert (await day_row(world)).questions_sent == 1
        world.interactive.post = post  # type: ignore[method-assign]

        await r.run("next")

        assert posted_ids(world) == ["q1", "q2"]

    async def test_a_question_closed_between_its_ref_and_asked_stays_closed_and_the_next_is_released_once(self):
        world = await world_with("q1", "q2")
        r = Rig(world)
        set_ref = world.review_store.set_message_ref

        async def then_clicked(question_id: str, ref: MessageRef) -> bool:
            kept = await set_ref(question_id, ref)
            await r.answer(question_id)
            return kept

        world.review_store.set_message_ref = then_clicked  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        closed = await stored(world, "q1")
        assert closed.status is QuestionStatus.CLOSED and closed.asked_at is None
        assert not world.interactive.has_controls(closed.message_ref)       # the close re-rendered it
        assert len(next_tasks(world)) == 1
        assert [text for _, text in await history(world)] == [review_frame("q1"), question_text(posted(world)[0])]

    async def test_a_question_closed_before_its_post_is_not_sent_and_the_history_says_so(self):
        world = await world_with("q1", "q2")
        r = Rig(world)
        mark = world.review_store.mark_appended

        async def clicked_first(question_id: str, at: datetime) -> bool:
            await r.answer(question_id)
            return await mark(question_id, at)

        world.review_store.mark_appended = clicked_first  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        assert posted(world) == []
        assert (await stored(world, "q1")).status is QuestionStatus.CLOSED
        assert (await history(world))[2:] == [("user", not_delivered_frame("q1")), ("model", NOT_DELIVERED_REPLY)]
        assert (await history(world))[0] == ("user", review_frame("q1"))
        assert len(next_tasks(world)) == 1
        assert await day_row(world) is None          # neither sent nor a failed delivery (reviewer, T8d1 review: item 2)


# ------------------------------------------------------------------------------------------------ a crash after the post

class TestCrashAfterThePost:
    async def test_the_next_run_completes_it_with_no_not_delivered_note_and_its_reply_and_button_count(self):
        world = await world_with("q2")
        fact_id = await world.seed(FACTS["q1"], expiry_kind=ExpiryKind.HORIZON, ttl_days=60,
                                   reported_date=NOW - timedelta(days=40), expires_at=NOW + timedelta(days=20))
        await queued(world, "q1", stale_days=90, fact_id=fact_id)
        r = Rig(world)

        async def crash(question_id: str, at: datetime) -> bool:
            raise RuntimeError("instance stopped")

        mark_asked = world.review_store.mark_asked
        world.review_store.mark_asked = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="instance stopped"):
            await r.run("next")
        world.review_store.mark_asked = mark_asked  # type: ignore[method-assign]
        posted_at = (await stored(world, "q1")).appended_at
        assert (await stored(world, "q1")).delivered

        world.advance(days=1)
        await r.run()

        done = await stored(world, "q1")
        assert done.status is QuestionStatus.UNANSWERED and done.asked_at == posted_at   # completed, then a day old
        texts = [text for _, text in await history(world)]
        assert not_delivered_frame("q1") not in texts
        assert texts.count(review_frame("q1")) == 1
        assert posted_ids(world) == ["q1", "q2"]                   # not sent again; the next one went
        assert await world.review_store.record_reply("q1", "still there, twice a week", world.now)
        assert await r.answers.apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "button", world.now) is AnswerResult.APPLIED


# ------------------------------------------------------------------------------------------------ labels and language

def ui(language: LanguageCode, message: UIMessage) -> str:
    return FileLocalizationAdapter().get_ui_string(language, message)


class TestLabels:
    async def test_a_fixed_ukrainian_owner_gets_the_ukrainian_ui_strings(self):
        world = await world_with("q1")
        world.language.preferences[USER] = (LanguageCode.UK, False)

        await Rig(world).run()

        [message] = posted(world)
        assert message.prompt == ui(LanguageCode.UK, UIMessage.MEMORY_REVIEW_QUESTION)
        assert message.options[0].label == ui(LanguageCode.UK, UIMessage.MEMORY_OPTION_STILL_TRUE)
        assert message.reply_hint == ui(LanguageCode.UK, UIMessage.MEMORY_REPLY_HINT)
        assert (await day_row(world)).labels_fallback == 0

    async def test_a_mirror_owner_writing_russian_gets_the_cached_model_labels(self):
        world = await world_with("q1")
        await world.seed_state(account_id=ACC, message_language="ru", labels={"ru": RU_LABELS},
                               label_digests={"ru": labels_digest()})

        await Rig(world).run()

        [message] = posted(world)
        assert message.prompt == RU_LABELS[UIMessage.MEMORY_REVIEW_QUESTION.value]
        assert [o.label for o in message.options] == ["Да", "Уже нет", "Никогда", "Не помню"]
        assert (await history(world))[1][1].startswith("Это всё ещё так?")
        assert (await day_row(world)).labels_fallback == 0

    async def test_a_mirror_owner_writing_russian_with_bad_labels_gets_the_ui_language_and_it_is_counted(self):
        world = await world_with("q1")
        bad = {**RU_LABELS, UIMessage.MEMORY_OPTION_STILL_TRUE.value: "Да, 1"}
        await world.seed_state(account_id=ACC, message_language="ru", labels={"ru": bad},
                               label_digests={"ru": labels_digest()})

        await Rig(world).run()

        [message] = posted(world)
        assert message.prompt == ui(LanguageCode.EN, UIMessage.MEMORY_REVIEW_QUESTION)
        assert (await day_row(world)).labels_fallback == 1
