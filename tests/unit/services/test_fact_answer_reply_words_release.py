"""A reply in words to the current review question releases the next question at once, as a digit does
(`FactAnswerService.answer_reply`). The review world and helpers of `test_fact_answer_reply`; assertions on the created
tasks, the stored question and state, and the acknowledgement."""
import hashlib
import logging
from typing import Any

from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_reply_record import ReplyRecord
from src.domain.fact_review_task_keys import next_key
from src.domain.ui_messages import UIMessage
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_answer_reply import (
    OTHER_TEXT, USER, ack, day_row, next_tasks, question_on, reply, service, stale_fact, stored,
)

WORDS = "still there, twice a week"


async def current_world(status: QuestionStatus = QuestionStatus.ASKED, current: str = "q1") -> ReviewWorld:
    world = ReviewWorld()
    await world.seed_state(account_id=ACC, current_question_id=current)
    await question_on(world, await stale_fact(world), status)
    await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                              fact_id="f-q2", quoted_text=OTHER_TEXT)
    return world


async def test_words_to_the_current_question_release_the_next_exactly_once_under_its_key():
    world = await current_world()

    text = await reply(world, WORDS)

    assert text == ack(world, UIMessage.MEMORY_REPLY_NOTED)
    assert next_tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]
    digest = hashlib.sha256(next_key("q1").encode()).hexdigest()[:32]
    assert [n for n in world.tasks.names if n.endswith(f"/tasks/fact_review-{digest}")]
    assert (await world.review_store.get_state(ACC)).current_question_id is None
    waiting = stored(world)
    assert waiting["status"] == "asked" and waiting["reply_text"] == WORDS
    assert stored(world, "q2")["status"] == "queued"
    row = await day_row(world)
    assert row.replies_in_words == 1


async def test_words_to_a_question_that_is_not_current_release_nothing():
    world = await current_world(QuestionStatus.UNANSWERED, current="q9")

    assert await reply(world, WORDS) == ack(world, UIMessage.MEMORY_REPLY_NOTED)

    assert next_tasks(world) == []
    assert (await world.review_store.get_state(ACC)).current_question_id == "q9"
    assert stored(world)["reply_text"] == WORDS


async def test_a_refused_record_enqueues_nothing_and_leaves_the_message_to_conversation():
    world = await current_world()

    class Refusing:
        def __getattr__(self, name: str) -> Any:
            return getattr(world.review_store, name)

        async def record_reply(self, *args: Any) -> ReplyRecord:
            return ReplyRecord.REFUSED

    assert await reply(world, WORDS, svc=service(world, review_store=Refusing())) is None

    assert world.enqueued == []
    assert (await world.review_store.get_state(ACC)).current_question_id == "q1"


async def test_an_enqueue_failure_is_logged_and_the_acknowledgement_still_goes(caplog):
    world = await current_world()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("queue unavailable")

    world.queue.enqueue_worker_task = broken  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        text = await reply(world, WORDS)

    assert text == ack(world, UIMessage.MEMORY_REPLY_NOTED)
    assert any(r.levelno == logging.ERROR and "queue unavailable" in r.getMessage() for r in caplog.records)
    assert stored(world)["reply_text"] == WORDS
    row = await day_row(world)
    assert row.replies_in_words == 1 and row.reply_intake_failed == 0


async def test_a_later_close_of_the_released_question_closes_without_a_second_release():
    world = await current_world()
    await reply(world, WORDS)
    assert len(next_tasks(world)) == 1

    closing = service(world)
    closing._clock = world.clock                  # a close outside intake reads the clock
    closed = await closing.close_question(
        (await world.review_store.get_questions(["q1"]))[0], ClosedBy.REVIEW, None, None)

    assert closed is True
    assert stored(world)["status"] == "closed"
    assert len(next_tasks(world)) == 1
