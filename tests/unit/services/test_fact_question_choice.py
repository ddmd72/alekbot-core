"""`FactAnswerService.handle` (Facts S4 · T8g, plan §3.20 "The fact-question handler", §3.16 "A button"): the owner's click
on a button of a review question's message.

On the review world: the real repository, typed write path, review store, session store, counters, task queue and
localization over one evaluating Firestore fake, and the recording interactive-message port (it posts the questions, so
a question's ref is a real one). Assertions are on the stored questions, the fact documents, the counters, the created
tasks and what the message was told to show. The service's clock raises when read: a click is taken at its own time."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.entities import ExpiryKind
from src.domain.fact_review_language import LABEL_KEYS, fact_question_message
from src.domain.fact_review_options import OPTIONS
from src.domain.fact_review_question import FACT_QUESTION_KIND, ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.messaging import MessageRef
from src.domain.ui_messages import UIMessage
from src.domain.owner_timezone import ResolvedOwnerZone
from src.ports.fact_update_port import FactUpdatePort
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.domain.language import LanguageCode
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as build_question
from tests.unit.domain.review_p2_texts import TEXTS

USER = "u1"
TZ = "Europe/Lisbon"
CHANNEL = "D1"
SESSION = f"{USER}:{CHANNEL}"
SLACK_USER = "U0CLICK"
TEXT = "User trains at the Iron Temple gym."
OTHER_TEXT = "User borrows books from the Example Library."


class FixedZones:
    async def resolve(self, account_id: str):
        return ResolvedOwnerZone(USER, TZ)


def no_clock() -> datetime:
    raise AssertionError("the click path read the clock")


def texts_of(world: ReviewWorld) -> FactQuestionTexts:
    return FactQuestionTexts(world.language, world.localization, world.review_store, world.counters, LanguageCode.EN,
                             clock=world.clock)


class ScriptedWrites(FactUpdatePort):
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def apply_fact_update(self, fact_id, update, account_id, *, context, evidence_at=None, reason=None,
                                history=None, expected_id=None):
        return self.outcome(fact_id)


class StaleFirstRead:
    """A review store whose first `get_questions` answers the snapshot it was given (a read taken before the question
    closed elsewhere); every later read is the real store's."""

    def __init__(self, real: Any, snapshot: FactReviewQuestion) -> None:
        self._real = real
        self._snapshot: Optional[FactReviewQuestion] = snapshot

    async def get_questions(self, ids):
        if self._snapshot is not None:
            snapshot, self._snapshot = self._snapshot, None
            return [snapshot]
        return await self._real.get_questions(ids)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def service(world: ReviewWorld, review_store: Any = None, fact_updates: Any = None) -> FactAnswerService:
    return FactAnswerService(
        fact_updates=fact_updates or world.store.adapter, repo=world.repo, review_store=review_store or world.review_store,
        counters=world.counters, task_queue=world.queue, session_store=world.session_store,
        texts=texts_of(world), interactive=world.interactive, owner_zones=FixedZones(), clock=no_clock,
    )


async def stale_fact(world: ReviewWorld, text: str = TEXT) -> str:
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60))


async def question_on(world: ReviewWorld, fact_id: str, status: QuestionStatus = QuestionStatus.ASKED,
                      question_id: str = "q1", **over: Any) -> FactReviewQuestion:
    """A question about `fact_id`, posted on Slack first (so its ref is one the platform gave). A `pending` one is
    delivered: it has the ref, and no move to asked."""
    fields: Dict[str, Any] = dict(question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
                                  quoted_text=world.docs[fact_id]["text"], session_id=SESSION, **over)
    draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive",
                               **{k: v for k, v in fields.items() if k in ("question_id", "account_id", "user_id",
                                                                           "fact_id", "quoted_text")})
    labels = await texts_of(world).labels_for(USER, ACC)
    fields["message_ref"] = await world.interactive.post("slack", CHANNEL, fact_question_message(draft, labels))
    if status is QuestionStatus.PENDING:
        fields["posted"] = True
    return await world.seed_question(status, **fields)


def ref_of(world: ReviewWorld, question_id: str = "q1") -> MessageRef:
    doc = world.question_docs[question_id]
    return MessageRef(platform=doc["message_platform"], channel_id=doc["message_channel"], message_id=doc["message_id"])


def choice_of(option: str, ref: MessageRef, question_id: str = "q1", at: Optional[datetime] = None) -> InteractiveChoice:
    return InteractiveChoice(
        binding=InteractiveBinding(kind=FACT_QUESTION_KIND, id=question_id), option_id=option, platform="slack",
        platform_user_id=SLACK_USER, message_ref=ref, at=at or NOW + timedelta(hours=1),
    )


async def click(world: ReviewWorld, option: str, question_id: str = "q1", svc: Optional[FactAnswerService] = None,
                ref: Optional[MessageRef] = None, user_id: str = USER, account_id: str = ACC,
                at: Optional[datetime] = None):
    return await (svc or service(world)).handle(
        choice_of(option, ref or ref_of(world, question_id), question_id, at), user_id, account_id)


def closer(world: ReviewWorld) -> FactAnswerService:
    """The service as the review and the timeout use it: the world's clock, for `close_question`."""
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store, texts=texts_of(world),
        interactive=world.interactive, owner_zones=FixedZones(), clock=world.clock)


def stored(world: ReviewWorld, question_id: str = "q1") -> dict:
    return world.question_docs[question_id]


def next_tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review" and body.get("mode") == "next"]


async def day_row(world: ReviewWorld):
    rows = [row for row in await world.counters.for_day(world.now.date()) if row.account_id == ACC]
    return rows[0] if rows else None


def snapshot(world: ReviewWorld):
    return ({k: dict(v) for k, v in world.docs.items()}, {k: dict(v) for k, v in world.question_docs.items()},
            list(world.enqueued))


def label_of(world: ReviewWorld, option: str) -> str:
    [(_, message)] = [(r, m) for r, m in world.interactive.posted][:1]
    return message.option(option).label


# ------------------------------------------------------------------------------------------------ an open question

class TestOpenQuestion:
    @pytest.mark.parametrize("option, fact_state", [
        ("still_true", "current"), ("ended", "archived"), ("never", "invalidated"), ("dont_know", "current"),
    ])
    async def test_each_button_is_applied_like_a_digit_and_the_message_shows_the_chosen_answer(self, option, fact_state):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        ref = ref_of(world)

        outcome = await click(world, option)

        assert world.docs[fact]["state"] == fact_state
        done = stored(world)
        assert (done["status"], done["closed_by"], done["answer"], done["answer_source"]) == (
            "closed", "owner", option, "button")
        assert outcome is not None and outcome.chosen_option_id == option and outcome.note == ""
        assert outcome.chosen_option.label == label_of(world, option)
        assert world.interactive.shown[ref].chosen_option_id == option          # the close re-rendered it, too
        assert not world.interactive.has_controls(ref)
        row = await day_row(world)
        assert (row.answered_by_button, row.answered_by_digit, row.answered_in_cabinet) == (1, 0, 0)

    async def test_the_answer_is_taken_at_the_time_of_the_click(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))
        at = NOW + timedelta(hours=5, minutes=3)

        await click(world, "still_true", at=at)

        [closed] = await world.review_store.get_questions(["q1"])
        assert closed.closed_at == at

    async def test_the_button_ends_a_fact_the_way_a_digit_does_with_its_history_fact(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        before = set(world.docs)

        await click(world, "ended")

        [history_fact] = set(world.docs) - before
        assert world.docs[history_fact]["text"].startswith("No longer true (the owner said so in Oct 2026): ")

    async def test_the_current_question_closing_releases_the_next_once(self):
        world = ReviewWorld()
        await world.seed_state(account_id=ACC, current_question_id="q1")
        await question_on(world, await stale_fact(world))
        await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                                  fact_id="f-q2", quoted_text=OTHER_TEXT)

        await click(world, "never")

        assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
        assert (await world.review_store.get_state(ACC)).current_question_id is None

    @pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
    async def test_an_asked_or_unanswered_question_takes_a_click(self, status):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, status)

        outcome = await click(world, "still_true")

        assert outcome.chosen_option_id == "still_true" and stored(world)["status"] == "closed"

    async def test_the_message_is_in_the_message_language(self):
        world = ReviewWorld()
        world.language.preferences[USER] = (LanguageCode.FR, False)
        await question_on(world, await stale_fact(world))

        outcome = await click(world, "ended")

        assert outcome.message.prompt == world.localization.get_ui_string(LanguageCode.FR, UIMessage.MEMORY_REVIEW_QUESTION)
        assert outcome.chosen_option.label == world.interactive.shown[ref_of(world)].chosen_option.label


# ------------------------------------------------------------------------------------------------ the crash scenario

class TestPostedButNotMarkedAsked:
    async def test_a_click_on_a_posted_pending_question_completes_it_and_applies(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, QuestionStatus.PENDING)
        appended = stored(world)["appended_at"]
        assert stored(world)["status"] == "pending" and stored(world)["asked_at"] is None

        outcome = await click(world, "ended")

        done = stored(world)
        assert done["status"] == "closed" and done["answer"] == "ended" and done["answer_source"] == "button"
        assert done["asked_at"] == appended                       # P3: asked at the time the frame was appended
        assert world.docs[fact]["state"] == "archived"
        assert outcome.chosen_option_id == "ended"

    async def test_a_pending_question_that_was_never_posted_is_not_answerable(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await world.seed_question(QuestionStatus.PENDING, question_id="q1", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=TEXT, session_id=SESSION)
        ref = await world.interactive.post("slack", CHANNEL, fact_question_message(
            build_question(account_id=ACC, user_id=USER, fact_id=fact, quoted_text=TEXT),
            await texts_of(world).labels_for(USER, ACC)))
        before = snapshot(world)

        assert await click(world, "still_true", ref=ref) is None
        assert snapshot(world) == before


# ------------------------------------------------------------------------------------------------ a closed question

class TestClosedQuestion:
    async def test_a_click_on_an_answered_question_shows_its_answer_and_writes_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        await click(world, "ended")
        before = snapshot(world)
        world.interactive.shown.clear()

        outcome = await click(world, "still_true")                  # a different button, on the answered message

        assert outcome.chosen_option_id == "ended"
        assert snapshot(world) == before
        assert world.docs[fact]["state"] == "archived"
        assert world.interactive.shown == {}                        # showing it is the choice service's step

    async def test_a_question_closed_without_an_answer_shows_the_closed_note(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        [open_question] = await world.review_store.get_questions(["q1"])
        assert await closer(world).close_question(open_question, ClosedBy.RESOLVED_ELSEWHERE)
        before = snapshot(world)

        outcome = await click(world, "never")

        assert outcome.chosen_option_id is None
        assert outcome.note == world.localization.get_ui_string(LanguageCode.EN, UIMessage.MEMORY_QUESTION_CLOSED)
        assert snapshot(world) == before

    async def test_a_question_closed_with_no_stored_ref_is_shown_on_the_clicked_message_and_writes_nothing(self):
        world = ReviewWorld()                                      # R4: closed during its own post, so no ref was stored
        fact = await stale_fact(world)
        labels = await texts_of(world).labels_for(USER, ACC)
        pending = build_question(QuestionStatus.PENDING, question_id="q1", account_id=ACC, user_id=USER, fact_id=fact,
                                 quoted_text=TEXT, session_id=SESSION)
        await world.put_question(pending.closed(ClosedBy.RESOLVED_ELSEWHERE, NOW))
        assert stored(world)["message_id"] is None
        clicked = await world.interactive.post("slack", CHANNEL, fact_question_message(pending, labels))
        before = snapshot(world)

        outcome = await click(world, "still_true", ref=clicked)

        assert outcome is not None and outcome.chosen_option_id is None and outcome.note
        assert snapshot(world) == before

    async def test_a_closed_question_whose_stored_message_is_another_one_shows_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        await click(world, "never")
        other = MessageRef(platform="slack", channel_id=CHANNEL, message_id="m-elsewhere")

        assert await click(world, "never", ref=other) is None

    async def test_a_question_that_closes_while_the_click_is_applied_is_shown_as_closed(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        [seen_open] = await world.review_store.get_questions(["q1"])
        assert await closer(world).close_question(seen_open, ClosedBy.RESOLVED_ELSEWHERE)
        before = snapshot(world)

        outcome = await click(world, "ended", svc=service(world, review_store=StaleFirstRead(world.review_store, seen_open)))

        assert outcome.chosen_option_id is None and outcome.note       # not applied: the question was closed already
        assert snapshot(world) == before and world.docs[fact]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ not the owner's click

class TestNothingHappens:
    async def test_another_users_click_does_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        before = snapshot(world)

        assert await click(world, "never", user_id="u2") is None
        assert snapshot(world) == before

    async def test_another_accounts_click_does_nothing(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))
        before = snapshot(world)

        assert await click(world, "never", account_id="acc-other") is None
        assert snapshot(world) == before

    async def test_a_message_that_is_not_the_questions_does_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        before = snapshot(world)

        for other in (MessageRef(platform="slack", channel_id=CHANNEL, message_id="not-the-question"),
                      MessageRef(platform="slack", channel_id="D2", message_id=ref_of(world).message_id)):
            assert await click(world, "never", ref=other) is None
        assert snapshot(world) == before and world.docs[fact]["state"] == "stale"

    async def test_a_question_that_was_never_posted_does_nothing(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await world.seed_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=TEXT)
        before = snapshot(world)

        assert await click(world, "never", ref=MessageRef(platform="slack", channel_id=CHANNEL, message_id="m1")) is None
        assert snapshot(world) == before

    async def test_a_question_that_does_not_exist_does_nothing(self):
        world = ReviewWorld()

        assert await click(world, "never", question_id="q-missing",
                           ref=MessageRef(platform="slack", channel_id=CHANNEL, message_id="m1")) is None

    async def test_an_option_that_is_no_answer_does_nothing_and_is_a_warning(self, caplog):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))
        before = snapshot(world)

        with caplog.at_level(logging.WARNING):
            assert await click(world, "maybe") is None

        assert snapshot(world) == before
        assert any("'maybe' is no answer" in record.getMessage() for record in caplog.records)

    async def test_a_fact_that_changed_leaves_the_question_open_with_its_buttons(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        ref = ref_of(world)
        world.docs[fact]["state"] = "archived"                      # closed meanwhile: `changed`
        docs = {k: dict(v) for k, v in world.docs.items()}

        outcome = await click(world, "still_true")

        assert outcome is not None and outcome.keep_options and outcome.chosen_option_id is None
        assert outcome.note == world.localization.get_ui_string(LanguageCode.EN, UIMessage.MEMORY_FACT_CHANGED)
        assert stored(world)["status"] == "asked" and world.docs == docs
        assert world.interactive.has_controls(ref)

    async def test_a_write_that_conflicted_leaves_the_question_open(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.CONFLICT, "changed meanwhile"))

        outcome = await click(world, "ended", svc=service(world, fact_updates=writes))

        assert outcome is not None and outcome.keep_options and outcome.chosen_option_id is None
        assert outcome.note == world.localization.get_ui_string(LanguageCode.EN, UIMessage.MEMORY_FACT_CHANGED)
        assert stored(world)["status"] == "asked" and world.docs[fact]["state"] == "stale"

    async def test_a_write_the_rules_refused_is_counted_and_leaves_the_question_open(self):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact)
        writes = ScriptedWrites(lambda fid: FactUpdateOutcome.refused(fid, UpdateRefusal.INVALID, "refused by the rules"))

        assert await click(world, "never", svc=service(world, fact_updates=writes)) is None

        assert stored(world)["status"] == "asked"
        assert (await day_row(world)).answers_not_applied == 1

    async def test_a_store_error_is_raised_so_the_task_is_retried(self):
        world = ReviewWorld()
        await question_on(world, await stale_fact(world))

        class Broken:
            async def get_questions(self, ids):
                raise RuntimeError("store unavailable")

        with pytest.raises(RuntimeError, match="store unavailable"):
            await click(world, "never", svc=service(world, review_store=Broken()))


def test_the_handler_answers_the_fact_question_kind():
    assert service(ReviewWorld()).kind == FACT_QUESTION_KIND == "fact_question"


def test_every_answer_is_a_button_of_the_message():
    # P2 revised (C11): a message's buttons are its question kind's options; together the kinds offer every answer
    message = fact_question_message(build_question(), {key: key for key in LABEL_KEYS})
    assert [option.id for option in message.options] == [kind.value for kind in OPTIONS["still_true"]]
    offered = set()
    for qkind in ("happened", "renewed", "paid"):
        asked = build_question(question=qkind, fallback="history", texts=TEXTS[qkind])
        ids = [option.id for option in fact_question_message(asked, {key: key for key in LABEL_KEYS}).options]
        assert ids == [kind.value for kind in OPTIONS[qkind]]
        offered.update(ids)
    assert offered | {o.id for o in message.options} == {kind.value for kind in AnswerKind}
