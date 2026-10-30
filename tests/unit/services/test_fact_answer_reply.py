"""`FactAnswerService.answer_reply` (Facts S4 · T8f, plan §3.16 "The reply at intake", §3.9): the owner's reply to a
question's message, taken before any model call.

On the review world: the real repository, typed write path, review store, session store, counters, task queue and
localization over one evaluating Firestore fake, the recording interactive-message port (it posts the questions, so a
question's ref is a real one). Assertions are on the stored questions, the fact documents, the session history, the
counters and the created tasks. The service's clock raises when read: a reply is taken at the moment it is given."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from src.domain.billing import BillingAccount
from src.domain.entities import ExpiryKind
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.fact_review_frame import not_delivered_frame, reply_part, review_frame
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from src.domain.llm import Message, MessagePart
from src.domain.messaging import MessageRef
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig, UserProfile
from src.ports.fact_update_port import FactUpdatePort
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_fact_updates import RecordingFactUpdates

USER = "u1"
TZ = "Europe/Lisbon"
CHANNEL = "D1"
SESSION = f"{USER}:{CHANNEL}"
TEXT = "User trains at the Iron Temple gym."
OTHER_TEXT = "User borrows books from the Example Library."


def ack(world: ReviewWorld, message: UIMessage) -> str:
    return world.localization.get_ui_string(LanguageCode.EN, message)


class FixedZones:
    async def resolve(self, account_id: str):
        return ResolvedOwnerZone(USER, TZ)


def no_clock() -> datetime:
    raise AssertionError("the reply path read the clock")


def texts_of(world: ReviewWorld) -> FactQuestionTexts:
    return FactQuestionTexts(world.language, world.localization, world.review_store, world.counters, LanguageCode.EN,
                             clock=world.clock)


class ScriptedWrites(FactUpdatePort):
    """A typed write path that answers `outcome(fact_id)` and writes nothing."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        return self.outcome(fact_id)


def service(world: ReviewWorld, review_store: Any = None, texts: Any = None, fact_updates: Any = None) -> FactAnswerService:
    return FactAnswerService(
        fact_updates=fact_updates or world.store.adapter, repo=world.repo, review_store=review_store or world.review_store,
        counters=world.counters, task_queue=world.queue, session_store=world.session_store,
        texts=texts or texts_of(world), interactive=world.interactive, owner_zones=FixedZones(), clock=no_clock,
    )


async def stale_fact(world: ReviewWorld, text: str = TEXT) -> str:
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))


async def question_on(world: ReviewWorld, fact_id: str, status: QuestionStatus = QuestionStatus.ASKED,
                      question_id: str = "q1", channel_id: str = CHANNEL, **over: Any) -> FactReviewQuestion:
    """A question about `fact_id`, posted on Slack first (so its ref is one the platform gave). A `pending` one is
    delivered: it has the ref, and no move to asked."""
    fields: Dict[str, Any] = dict(question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
                                  quoted_text=world.docs[fact_id]["text"], session_id=SESSION, **over)
    draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive",
                               **{k: v for k, v in fields.items() if k in ("question_id", "account_id", "user_id",
                                                                           "fact_id", "quoted_text")})
    labels = await texts_of(world).labels_for(USER, ACC)
    fields["message_ref"] = await world.interactive.post("slack", channel_id, fact_question_message(draft, labels))
    if status is QuestionStatus.PENDING:
        fields["posted"] = True
    return await world.seed_question(status, **fields)


def ref_of(world: ReviewWorld, question_id: str = "q1") -> MessageRef:
    doc = world.question_docs[question_id]
    return MessageRef(platform=doc["message_platform"], channel_id=doc["message_channel"], message_id=doc["message_id"])


async def reply(world: ReviewWorld, text: str, question_id: str = "q1", svc: Optional[FactAnswerService] = None,
                **ref: str) -> Optional[str]:
    """The owner's reply to the question's message (or to the message `ref` names instead)."""
    found = ref_of(world, question_id)
    return await (svc or service(world)).answer_reply(
        ref.get("user_id", USER), ACC, ref.get("platform", found.platform), ref.get("channel_id", found.channel_id),
        ref.get("message_id", found.message_id), text, world.now)


async def history(world: ReviewWorld) -> List[tuple]:
    return [(m.role, m.parts[0].text) for m in await world.session_store.messages(SESSION)]


def stored(world: ReviewWorld, question_id: str = "q1") -> dict:
    return world.question_docs[question_id]


def next_tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review" and body.get("mode") == "next"]


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


# ------------------------------------------------------------------------------------------------ a digit

class TestDigit:
    async def test_a_digit_is_applied_closes_the_question_and_is_noted_with_the_history_fact(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        before = set(world.docs)

        text = await reply(world, "2")

        assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
        assert world.docs[fact]["state"] == "archived"
        [history_fact] = set(world.docs) - before
        assert world.docs[history_fact]["text"].startswith("No longer true (the owner said so in Oct 2026): ")
        done = stored(world)
        assert (done["status"], done["closed_by"], done["answer"], done["answer_source"]) == (
            "closed", "owner", "ended", "digit")
        row = await day_row(world)
        assert row.answered_by_digit == 1 and row.reply_intake_failed == 0

    async def test_the_current_question_closing_releases_the_next_once(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC, current_question_id="q1")
        fact = await stale_fact(world)
        await question_on(world, fact)
        await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                                  fact_id="f-q2", quoted_text=OTHER_TEXT)

        await reply(world, "1")

        assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
        assert (await world.review_store.get_state(ACC)).current_question_id is None
        assert stored(world, "q2")["status"] == "queued"      # released for a later run, not sent here

    async def test_a_question_that_is_not_current_releases_nothing(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC, current_question_id="q9")
        fact = await stale_fact(world)
        await question_on(world, fact)

        await reply(world, "3")

        assert next_tasks(world) == [] and world.docs[fact]["state"] == "invalidated"

    @pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
    async def test_an_asked_or_unanswered_question_takes_a_digit(self, status):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, status)

        assert await reply(world, "1") == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
        assert world.docs[fact]["state"] == "current" and stored(world)["status"] == "closed"

    @pytest.mark.parametrize("wrapped", [" 4 ", "`4`", "4."])
    async def test_a_digit_in_the_chat_clients_punctuation_is_still_a_digit(self, wrapped):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)

        await reply(world, wrapped)

        assert stored(world)["answer"] == "dont_know"


# ------------------------------------------------------------------------------------------------ words

class TestWords:
    async def test_words_to_the_current_question_are_recorded_nothing_is_applied_and_the_next_is_released(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC, current_question_id="q1")
        fact = await stale_fact(world)
        await question_on(world, fact)
        await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                                  fact_id="f-q2", quoted_text=OTHER_TEXT)
        docs_before = {k: dict(v) for k, v in world.docs.items()}

        text = await reply(world, "yes, still the case, twice a week")

        assert text == ack(world, UIMessage.MEMORY_REPLY_NOTED)
        waiting = stored(world)
        assert waiting["status"] == "asked" and waiting["reply_text"] == "yes, still the case, twice a week"
        assert waiting["offered_at"] is None
        assert world.docs == docs_before                          # the fact is the review's to decide
        assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
        assert (await world.review_store.get_state(ACC)).current_question_id is None     # the words gave up the slot
        assert stored(world, "q2")["status"] == "queued"      # released for a later run, not sent here
        row = await day_row(world)
        assert row is None or (row.answered_by_digit, row.reply_intake_failed) == (0, 0)

    async def test_a_second_reply_is_appended_to_the_first_while_the_review_has_not_read_it(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        await reply(world, "it was a gym, not a pool")
        await reply(world, "and twice a week")

        assert stored(world)["reply_text"] == "it was a gym, not a pool\nand twice a week"

    @pytest.mark.parametrize("text", ["5", "12", "two", "2 please", "yes 2", "1\n2"])
    async def test_anything_but_a_lone_digit_of_the_options_is_words(self, text):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)

        assert await reply(world, text) == ack(world, UIMessage.MEMORY_REPLY_NOTED)
        assert stored(world)["status"] == "asked" and stored(world)["reply_text"] == text.strip()
        assert world.docs[fact]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ not an answer

class TestNotAnAnswer:
    async def test_a_reply_to_another_message_is_ordinary_conversation(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        assert await reply(world, "2", message_id="not-the-question") is None
        assert stored(world)["status"] == "asked" and await history(world) == []

    @pytest.mark.parametrize("other", [{"channel_id": "D2"}, {"platform": "telegram"}, {"user_id": "u2"}])
    async def test_the_same_message_id_in_another_channel_platform_or_user_is_not_the_question(self, other):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        assert await reply(world, "2", **other) is None
        assert stored(world)["status"] == "asked" and await history(world) == []

    async def test_a_reply_with_no_words_is_ordinary_conversation(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        assert await reply(world, "  \n ") is None
        assert stored(world)["reply_text"] is None

    async def test_a_question_that_was_never_delivered_has_no_message_to_reply_to(self):
        world = ReviewWorld()
        await world.seed_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=USER,
                                  fact_id="f1", quoted_text=TEXT)

        assert await service(world).answer_reply(USER, ACC, "slack", CHANNEL, "m1", "2", world.now) is None

    async def test_a_digit_on_a_fact_that_changed_is_told_so_and_leaves_the_question_open(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.docs[fact]["state"] = "archived"                     # the fact was closed meanwhile: `changed`
        docs = {k: dict(v) for k, v in world.docs.items()}

        text = await reply(world, "1")

        assert text == ack(world, UIMessage.MEMORY_FACT_CHANGED)
        assert stored(world)["status"] == "asked" and world.docs == docs
        assert await history(world) == [("user", reply_part("q1", "1")), ("model", text)]
        row = await day_row(world)
        assert row is None or (row.reply_intake_failed, row.answered_by_digit) == (0, 0)

    async def test_a_digit_whose_write_conflicted_is_told_so_and_leaves_the_question_open(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.CONFLICT, "changed meanwhile"))

        text = await reply(world, "2", svc=service(world, fact_updates=writes))

        assert text == ack(world, UIMessage.MEMORY_FACT_CHANGED)
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"
        assert await history(world) == [("user", reply_part("q1", "2")), ("model", text)]
        row = await day_row(world)
        assert row is None or row.reply_intake_failed == 0

    async def test_the_changed_fact_line_is_in_the_message_language(self):
        world = ReviewWorld()
        world.language.preferences[USER] = (LanguageCode.FR, False)
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.docs[fact]["state"] = "archived"

        text = await reply(world, "1")

        assert text == world.localization.get_ui_string(LanguageCode.FR, UIMessage.MEMORY_FACT_CHANGED)
        assert text != ack(world, UIMessage.MEMORY_FACT_CHANGED)

    async def test_a_digit_the_write_rules_refused_is_left_to_conversation_and_counted_as_not_applied(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.INVALID, "refused by the rules"))

        assert await reply(world, "3", svc=service(world, fact_updates=writes)) is None

        assert stored(world)["status"] == "asked" and await history(world) == []
        row = await day_row(world)
        assert (row.answers_not_applied, row.reply_intake_failed) == (1, 0)

    async def test_words_the_review_has_already_been_shown_are_not_recorded_and_go_to_conversation(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world), reply_text="still true", reply_at=NOW,
                          offered_at=NOW + timedelta(minutes=1))

        assert await reply(world, "and more") is None
        assert stored(world)["reply_text"] == "still true" and await history(world) == []


# ------------------------------------------------------------------------------------------------ a closed question

class TestClosedQuestion:
    async def test_a_reply_to_a_closed_question_is_already_answered_and_changes_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        await reply(world, "1")
        docs = {k: dict(v) for k, v in world.docs.items()}
        closed = dict(stored(world))

        text = await reply(world, "3")

        assert text == ack(world, UIMessage.MEMORY_ALREADY_ANSWERED)
        assert world.docs == docs and stored(world) == closed
        assert (await history(world))[-2:] == [
            ("user", reply_part("q1", "3")), ("model", ack(world, UIMessage.MEMORY_ALREADY_ANSWERED))]
        assert next_tasks(world) == []                           # the pointer never named it: nothing to release


# ------------------------------------------------------------------------------------------------ the history pair

class TestPair:
    async def test_the_pair_is_the_marker_with_the_owners_text_verbatim_and_the_acknowledgement(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))
        words = "  it's a gym,\nnot a pool `really`  "

        text = await reply(world, words)

        assert await history(world) == [("user", f"[Reply to memory review q=q1] {words}"), ("model", text)]

    async def test_a_digit_pair_keeps_the_text_as_typed_too(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        text = await reply(world, "`2`")

        assert await history(world) == [("user", "[Reply to memory review q=q1] `2`"), ("model", text)]

    async def test_the_pair_follows_the_questions_frame_in_the_questions_own_session(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        await world.session_store.append_messages_batch(
            SESSION, [Message(role="user", parts=[MessagePart(text=review_frame("q1"))]),
                      Message(role="model", parts=[MessagePart(text="Is it still so?")])], owner_id=USER)

        text = await reply(world, "1")

        assert await history(world) == [("user", review_frame("q1")), ("model", "Is it still so?"),
                                        ("user", reply_part("q1", "1")), ("model", text)]

    async def test_the_acknowledgement_is_in_the_message_language(self):
        world = ReviewWorld()
        world.language.preferences[USER] = (LanguageCode.UK, False)
        await question_on(world, await stale_fact(world))

        text = await reply(world, "every Tuesday")

        assert text == world.localization.get_ui_string(LanguageCode.UK, UIMessage.MEMORY_REPLY_NOTED)
        assert text != ack(world, UIMessage.MEMORY_REPLY_NOTED)


# ------------------------------------------------------------------------------------------------ delivered but pending

class TestDeliveredPending:
    async def test_a_digit_to_a_posted_question_not_marked_asked_completes_it_and_applies(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, QuestionStatus.PENDING)
        appended = stored(world)["appended_at"]
        assert stored(world)["status"] == "pending" and stored(world)["asked_at"] is None

        assert await reply(world, "2") == ack(world, UIMessage.MEMORY_ANSWER_NOTED)

        done = stored(world)
        assert done["status"] == "closed" and done["answer"] == "ended"
        assert done["asked_at"] == appended                      # P3: asked at the time the frame was appended
        assert world.docs[fact]["state"] == "archived"

    async def test_words_to_such_a_question_complete_it_and_are_recorded(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, QuestionStatus.PENDING)
        appended = stored(world)["appended_at"]

        assert await reply(world, "still true, mostly") == ack(world, UIMessage.MEMORY_REPLY_NOTED)

        waiting = stored(world)
        assert waiting["status"] == "asked" and waiting["asked_at"] == appended
        assert waiting["reply_text"] == "still true, mostly" and world.docs[fact]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ errors

class TestErrors:
    async def test_a_store_error_gives_none_logs_an_error_and_counts_the_failure(self, caplog):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        class Broken:
            def __getattr__(self, name: str) -> Any:
                return getattr(world.review_store, name)

            async def by_message(self, *args: Any) -> Any:
                raise RuntimeError("store unavailable")

        with caplog.at_level(logging.ERROR):
            assert await reply(world, "2", svc=service(world, review_store=Broken())) is None

        assert any(r.levelno == logging.ERROR and "store unavailable" in r.getMessage() for r in caplog.records)
        assert (await day_row(world)).reply_intake_failed == 1
        assert stored(world)["status"] == "asked" and await history(world) == []

    async def test_a_labels_resolution_failure_falls_through_and_is_counted(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        class NoLanguage:
            async def resolve_ui_language(self, user_id: str) -> Any:
                raise RuntimeError("language service down")

            async def get_preference(self, user_id: str) -> Any:
                raise RuntimeError("language service down")

        broken = FactQuestionTexts(NoLanguage(), world.localization, world.review_store, world.counters, LanguageCode.EN,
                                   clock=world.clock)
        assert await reply(world, "2", svc=service(world, texts=broken)) is None

        assert (await day_row(world)).reply_intake_failed == 1
        assert stored(world)["status"] == "asked" and await history(world) == []

    async def test_a_pair_that_cannot_be_stored_gives_none_and_counts_the_failure(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.session_store.fail = True

        assert await reply(world, "yes, still true") is None

        assert (await day_row(world)).reply_intake_failed == 1
        assert await history(world) == []

    async def test_a_digit_whose_pair_cannot_be_stored_stays_applied_and_the_message_goes_on(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        world.session_store.fail = True

        assert await reply(world, "3") is None

        assert (await day_row(world)).reply_intake_failed == 1
        assert world.docs[fact]["state"] == "invalidated" and stored(world)["status"] == "closed"

    async def test_an_unreadable_counter_does_not_hide_the_fallthrough(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        class Broken:
            def __getattr__(self, name: str) -> Any:
                return getattr(world.review_store, name)

            async def by_message(self, *args: Any) -> Any:
                raise RuntimeError("store unavailable")

        async def failing_add(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("counters down")

        world.counters.add = failing_add  # type: ignore[method-assign]
        assert await reply(world, "2", svc=service(world, review_store=Broken())) is None


# ------------------------------------------------------------------------------------------------ T8d1 review: the crash

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
    async def route_message(self, message: Any) -> Any:
        raise AssertionError("no review pass is routed in these tests")


def review_service(world: ReviewWorld, answers: FactAnswerService) -> FactReviewService:
    accounts, users = Accounts(), Users()
    return FactReviewService(
        repo=world.repo, review_store=world.review_store, counters=world.counters, lease=world.lease,
        coordinator=NoModel(), agent_factory=Agents(), user_repo=users, account_repo=accounts,
        texts=texts_of(world), owner_zones=OwnerTimezoneResolver(accounts, users),
        notification_state=world.notification_state, channel_bindings=world.channel_bindings,
        notifications=world.notifier, session_store=world.session_store, task_queue=world.queue,
        answer_service=answers, interactive=world.interactive, alert_sink=world.sink, run_timeout_s=600,
        clock=world.clock, fact_updates=RecordingFactUpdates(world.store.adapter),
        cabinet_url="https://cabinet.example/cabinet?tab=memory",
    )


async def crashed_after_the_post(world: ReviewWorld) -> FactReviewQuestion:
    """A question sent, posted and then the instance stopped before it was marked asked: pending, with its ref."""
    await world.notification_state.save_primary(USER, "slack", CHANNEL)
    await world.seed_state(account_id=ACC)
    fact = await world.seed(TEXT, expiry_kind=ExpiryKind.HORIZON, ttl_days=60, reported_date=NOW - timedelta(days=40),
                            expires_at=NOW + timedelta(days=20))
    await world.seed_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=USER, fact_id=fact,
                              quoted_text=TEXT, queue_key=NOW - timedelta(days=90), queued_at=NOW - timedelta(days=2))
    run = review_service(world, service(world))

    async def crash(question_id: str, at: datetime) -> bool:
        raise RuntimeError("instance stopped")

    world.review_store.mark_asked = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="instance stopped"):
        await run.run(USER, ACC, "next")
    del world.review_store.mark_asked
    pending = (await world.review_store.get_questions(["q1"]))[0]
    assert pending.status is QuestionStatus.PENDING and pending.delivered
    return pending


class TestCrashBetweenPostedAndAsked:
    async def test_a_thread_reply_in_words_to_the_posted_message_is_taken_through_the_intake(self):
        world = ReviewWorld()
        pending = await crashed_after_the_post(world)

        text = await service(world).answer_reply(USER, ACC, pending.message_ref.platform, pending.message_ref.channel_id,
                                                 pending.message_ref.message_id, "still there, twice a week", world.now)

        assert text == ack(world, UIMessage.MEMORY_REPLY_NOTED)
        taken = stored(world)
        assert taken["status"] == "asked" and taken["asked_at"] == pending.appended_at
        assert taken["reply_text"] == "still there, twice a week"
        assert [t for _, t in await history(world)] == [
            review_frame("q1"), (await history(world))[1][1], reply_part("q1", "still there, twice a week"), text]
        assert not_delivered_frame("q1") not in [t for _, t in await history(world)]

    async def test_a_digit_to_the_posted_message_is_applied_through_the_intake(self):
        world = ReviewWorld()
        pending = await crashed_after_the_post(world)
        fact = pending.fact_id

        text = await service(world).answer_reply(USER, ACC, pending.message_ref.platform, pending.message_ref.channel_id,
                                                 pending.message_ref.message_id, "1", world.now)

        assert text == ack(world, UIMessage.MEMORY_ANSWER_NOTED)
        done = stored(world)
        assert done["status"] == "closed" and done["answer"] == "still_true" and done["asked_at"] == pending.appended_at
        assert world.docs[fact]["state"] == "current"
