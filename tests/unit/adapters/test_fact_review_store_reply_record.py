"""A reply in words gives up the current slot in the transaction that records it: `record_reply` reports what its one
transaction did (`ReplyRecord`). The real adapter on the review world's evaluating Firestore fake; assertions on the
returned member and the stored question and state."""
from datetime import date, timedelta

import pytest

from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reply_record import ReplyRecord
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

TODAY = date(2026, 10, 24)
AT = NOW + timedelta(minutes=1)


async def test_words_to_the_current_question_are_recorded_and_clear_the_slot():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)

    result = await world.review_store.record_reply("q1", "still there, twice a week", AT)

    assert result is ReplyRecord.RELEASED
    assert world.state_doc("acc")["current_question_id"] is None
    kept = world.question_docs["q1"]
    assert kept["status"] == "asked" and kept["reply_text"] == "still there, twice a week" and kept["reply_at"] == AT


async def test_words_to_an_unanswered_question_are_recorded_and_leave_the_slot_alone():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.UNANSWERED, question_id="q1")
    await world.seed_state(current_question_id="q2", sent=1, sent_on=TODAY)

    result = await world.review_store.record_reply("q1", "it ended", AT)

    assert result is ReplyRecord.RECORDED
    assert world.state_doc("acc")["current_question_id"] == "q2"
    assert world.question_docs["q1"]["reply_text"] == "it ended" and world.question_docs["q1"]["status"] == "unanswered"


async def test_words_on_an_asked_question_when_the_slot_names_another_are_recorded_only():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.seed_state(current_question_id="q9", sent=1, sent_on=TODAY)

    assert await world.review_store.record_reply("q1", "no", AT) is ReplyRecord.RECORDED
    assert world.state_doc("acc")["current_question_id"] == "q9"


async def test_words_with_no_state_document_are_recorded():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")

    assert await world.review_store.record_reply("q1", "no", AT) is ReplyRecord.RECORDED


@pytest.mark.parametrize("status,over", [
    (QuestionStatus.QUEUED, {}),
    (QuestionStatus.PENDING, {"posted": True}),
    (QuestionStatus.CLOSED, {}),
])
async def test_a_refused_reply_leaves_the_question_and_the_slot_untouched(status, over):
    world = ReviewWorld()
    await world.seed_question(status, question_id="q1", **over)
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)
    question_before, state_before = dict(world.question_docs["q1"]), dict(world.state_doc("acc"))

    assert await world.review_store.record_reply("q1", "words", AT) is ReplyRecord.REFUSED
    assert world.question_docs["q1"] == question_before and world.state_doc("acc") == state_before


async def test_words_with_no_text_or_for_a_missing_question_are_refused_and_keep_the_slot():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)

    assert await world.review_store.record_reply("q1", "   ", AT) is ReplyRecord.REFUSED
    assert await world.review_store.record_reply("nope", "words", AT) is ReplyRecord.REFUSED
    assert world.state_doc("acc")["current_question_id"] == "q1"


async def test_a_reply_after_the_review_was_shown_the_first_is_refused_and_keeps_the_slot():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.review_store.record_reply("q1", "first", AT)
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)
    assert await world.review_store.mark_offered("q1", AT) is True

    assert await world.review_store.record_reply("q1", "more", AT) is ReplyRecord.REFUSED
    assert world.state_doc("acc")["current_question_id"] == "q1"


def test_a_reply_record_has_no_truth_value():
    for member in ReplyRecord:
        with pytest.raises(TypeError):
            bool(member)
