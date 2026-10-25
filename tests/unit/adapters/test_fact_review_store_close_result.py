"""Facts S4 · T8e fix round (F1): `close_and_release` reports what its one transaction did, so a caller never decides
"was it the current question" from a read taken outside it. The real adapter on the review world's evaluating Firestore
fake; assertions on the returned `QuestionClose` and the stored question and state."""
from datetime import date, timedelta

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_question_close import QuestionClose
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

TODAY = date(2026, 10, 24)
AT = NOW + timedelta(minutes=1)


def status(world, question_id):
    return world.question_docs[question_id]["status"]


async def test_closing_the_current_question_is_released_and_clears_the_slot():
    world = ReviewWorld()
    await world.seed_question(question_id="q1")
    await world.review_store.claim_next("acc", TODAY, "u1:D1")

    result = await world.review_store.close_and_release("q1", ClosedBy.OWNER, AnswerKind.NEVER, "cabinet", AT)

    assert result is QuestionClose.RELEASED
    assert world.state_doc("acc")["current_question_id"] is None and status(world, "q1") == "closed"


async def test_closing_a_question_the_slot_does_not_name_is_closed_and_leaves_the_slot():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.seed_state(current_question_id="q-other", sent=1, sent_on=TODAY)

    result = await world.review_store.close_and_release("q1", ClosedBy.TIMEOUT, None, None, AT)

    assert result is QuestionClose.CLOSED
    assert world.state_doc("acc")["current_question_id"] == "q-other" and status(world, "q1") == "closed"


async def test_closing_with_no_state_document_is_closed_and_creates_none():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")

    result = await world.review_store.close_and_release("q1", ClosedBy.REVIEW, None, None, AT)

    assert result is QuestionClose.CLOSED and world.state_doc("acc") is None and status(world, "q1") == "closed"


async def test_a_refused_close_is_refused_and_writes_nothing():
    world = ReviewWorld()
    await world.seed_question(question_id="q1")
    await world.review_store.claim_next("acc", TODAY, "u1:D1")
    before = dict(world.question_docs["q1"])

    timed_out = await world.review_store.close_and_release("q1", ClosedBy.TIMEOUT, None, None, AT)   # never asked
    missing = await world.review_store.close_and_release("nope", ClosedBy.REVIEW, None, None, AT)

    assert timed_out is QuestionClose.REFUSED and missing is QuestionClose.REFUSED
    assert world.state_doc("acc")["current_question_id"] == "q1" and world.question_docs["q1"] == before


async def test_a_second_close_of_the_current_question_is_refused():
    world = ReviewWorld()
    await world.seed_question(question_id="q1")
    await world.review_store.claim_next("acc", TODAY, "u1:D1")
    store = world.review_store

    first = await store.close_and_release("q1", ClosedBy.OWNER, AnswerKind.ENDED, "cabinet", AT)
    second = await store.close_and_release("q1", ClosedBy.OWNER, AnswerKind.ENDED, "cabinet", AT)

    assert (first, second) == (QuestionClose.RELEASED, QuestionClose.REFUSED)
