"""A question whose post succeeded but whose ref write was lost (P15-D11): `mark_posted` records the post, so the question
is delivered without a message ref and housekeeping's `complete_delivered` makes it asked."""
from datetime import timedelta

import pytest

from src.domain.fact_review_question import QuestionStatus
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW, REF

AT = NOW + timedelta(minutes=1)


@pytest.fixture
def world():
    return ReviewWorld()


async def _appended(world, question_id="q1"):
    await world.seed_question(QuestionStatus.PENDING, question_id=question_id, appended_at=None)
    assert await world.review_store.mark_appended(question_id, AT) is True


async def test_a_recorded_post_without_a_ref_is_completed_as_asked(world):
    await _appended(world)

    assert await world.review_store.mark_posted("q1", AT + timedelta(seconds=1)) is True
    assert await world.review_store.complete_delivered("q1") is True

    stored = world.question_docs["q1"]
    assert stored["status"] == "asked" and stored["asked_at"] == AT == stored["appended_at"]
    assert stored["posted_at"] == AT + timedelta(seconds=1) and stored["message_id"] is None


async def test_the_ref_is_stored_after_the_post_was_recorded(world):
    await _appended(world)
    await world.review_store.mark_posted("q1", AT)

    assert await world.review_store.set_message_ref("q1", REF) is True
    assert await world.review_store.mark_asked("q1", AT) is True
    assert world.question_docs["q1"]["message_id"] == REF.message_id


async def test_a_post_is_recorded_once_and_only_after_the_frame(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="q1", appended_at=None)
    assert await world.review_store.mark_posted("q1", AT) is False          # not appended yet
    await _appended(world, "q2")
    assert await world.review_store.mark_posted("q2", AT) is True
    assert await world.review_store.mark_posted("q2", AT) is False          # already delivered
    assert await world.review_store.mark_posted("nope", AT) is False
