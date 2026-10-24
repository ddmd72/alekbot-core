"""Facts S4 · T4b: `purge_overdue` counts what is past its expiry and still stored, in the questions and the message
claims, to watch the store's TTL lag."""
from datetime import datetime, timedelta

import pytest

from src.domain.fact_review_question import QUESTION_RETENTION, ClosedBy, QuestionStatus
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW
from tests.support.review_rule_builders import question as build_question


@pytest.fixture
def world():
    return ReviewWorld()


async def closed(world, question_id: str, at: datetime) -> None:
    question = build_question(QuestionStatus.ASKED, question_id=question_id, asked_at=at - timedelta(days=1))
    await world.put_question(question.closed(ClosedBy.REVIEW, at))


async def test_nothing_stored_is_nothing_overdue(world):
    assert await world.review_store.purge_overdue(NOW) == 0


async def test_it_counts_the_closed_questions_past_their_retention_and_no_open_one(world):
    await closed(world, "old-1", NOW - QUESTION_RETENTION - timedelta(days=2))
    await closed(world, "old-2", NOW - QUESTION_RETENTION - timedelta(seconds=1))
    await closed(world, "recent", NOW - timedelta(days=10))
    await world.seed_question(QuestionStatus.ASKED, question_id="open", asked_at=NOW - timedelta(days=400))
    await world.seed_question(QuestionStatus.QUEUED, question_id="queued")

    assert await world.review_store.purge_overdue(NOW) == 2
    assert await world.review_store.purge_overdue(NOW + timedelta(days=90)) == 3


async def test_it_counts_the_digest_claims_too_and_adds_both_collections(world):
    await closed(world, "old", NOW - QUESTION_RETENTION - timedelta(days=1))
    await world.review_store.claim_digest("u1", NOW.date(), NOW)
    await world.review_store.claim_digest("u2", NOW.date(), NOW)
    later = NOW + QUESTION_RETENTION + timedelta(days=1)

    assert await world.review_store.purge_overdue(NOW) == 1
    assert await world.review_store.purge_overdue(later) == 3


async def test_the_bound_is_exclusive(world):
    at = NOW - QUESTION_RETENTION
    await closed(world, "edge", at)
    assert await world.review_store.purge_overdue(NOW) == 0
    assert await world.review_store.purge_overdue(NOW + timedelta(microseconds=1)) == 1


async def test_a_naive_bound_is_refused(world):
    with pytest.raises(ValueError):
        await world.review_store.purge_overdue(datetime(2026, 10, 24, 9, 0))
