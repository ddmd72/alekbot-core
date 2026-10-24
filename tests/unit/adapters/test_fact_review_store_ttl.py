"""Facts S4 · T4b (S4 inward review L8): the TTL field is the store's, written at serialisation on a closed question and
never part of the domain record. The real adapter on the review world's evaluating fake."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.adapters.firestore_fact_review_store import question_document
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import QUESTION_RETENTION, ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_question_close import QuestionClose
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW
from tests.support.review_rule_builders import question as build_question

AT = NOW + timedelta(hours=1)


@pytest.fixture
def world():
    return ReviewWorld()


def test_the_domain_record_has_no_ttl_field():
    assert "purge_after" not in FactReviewQuestion.model_fields
    closed = build_question(QuestionStatus.ASKED).closed(ClosedBy.TIMEOUT, AT)
    assert "purge_after" not in closed.model_dump()


async def test_a_closed_question_is_stored_with_the_ttl_field_at_its_retention(world):
    await world.seed_question(QuestionStatus.ASKED)

    assert await world.review_store.close("q1", ClosedBy.OWNER, AnswerKind.STILL_TRUE, "digit", AT)

    doc = world.question_docs["q1"]
    assert doc["status"] == "closed" and doc["purge_after"] == AT + QUESTION_RETENTION


@pytest.mark.parametrize("status", [QuestionStatus.QUEUED, QuestionStatus.PENDING, QuestionStatus.ASKED,
                                    QuestionStatus.UNANSWERED])
async def test_an_open_question_is_stored_without_the_ttl_field(world, status):
    await world.seed_question(status)
    assert "purge_after" not in world.question_docs["q1"]


async def test_a_question_written_open_by_a_move_has_no_ttl_field(world):
    await world.seed_question(QuestionStatus.ASKED)
    assert await world.review_store.mark_unanswered("q1")
    assert "purge_after" not in world.question_docs["q1"]


async def test_a_closed_question_read_back_has_no_ttl_field_and_equals_what_was_closed(world):
    closed = await world.put_question(build_question(QuestionStatus.ASKED).closed(ClosedBy.REVIEW, AT))
    assert "purge_after" in world.question_docs["q1"]

    [back] = await world.review_store.get_questions(["q1"])

    assert back == closed and "purge_after" not in back.model_dump()


async def test_a_stored_ttl_from_another_retention_is_ignored_on_read(world):
    closed = build_question(QuestionStatus.ASKED).closed(ClosedBy.REVIEW, AT)
    document = {**question_document(closed), "purge_after": AT + timedelta(days=3)}
    await world.store.db.collection(world.env.fact_review_questions_collection).document("q1").set(document)

    [back] = await world.review_store.get_questions(["q1"])

    assert back == closed


async def test_closing_through_close_and_release_writes_the_ttl_field_too(world):
    await world.seed_state(current_question_id="q1")
    await world.seed_question(QuestionStatus.ASKED)

    # Reviewer edit (S4 T8e re-review): the exact `QuestionClose` member (T8e review F1), not truthiness.
    assert await world.review_store.close_and_release("q1", ClosedBy.TIMEOUT, None, None, AT) is QuestionClose.RELEASED

    assert world.question_docs["q1"]["purge_after"] == AT + QUESTION_RETENTION
    assert world.state_doc()["current_question_id"] is None


def test_the_domain_record_refuses_a_field_it_does_not_have():
    # extra="forbid" (T4b review): a stored field the model does not know is loud, never read and then dropped by the
    # next whole-document write.
    closed = build_question(QuestionStatus.ASKED).closed(ClosedBy.REVIEW, AT)
    with pytest.raises(ValidationError, match="purge_after"):
        FactReviewQuestion.model_validate({**closed.model_dump(), "purge_after": AT})


async def test_a_stored_question_with_an_unknown_field_is_skipped_by_a_list_read_and_raised_by_a_write(world):
    closed = build_question(QuestionStatus.ASKED).closed(ClosedBy.REVIEW, AT)
    document = {**question_document(closed), "renamed_field": "x"}
    await world.store.db.collection(world.env.fact_review_questions_collection).document("q1").set(document)

    assert await world.review_store.get_questions(["q1"]) == []
    with pytest.raises(ValidationError):
        await world.review_store.close("q1", ClosedBy.TIMEOUT, None, None, AT)
    assert world.question_docs["q1"] == document
