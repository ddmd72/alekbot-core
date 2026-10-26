"""Facts S4 · T4a review fixes (`t4a_review.md` M1, L1, L2, L3): the claim never skips the question the state points at,
a failed delivery is stamped only for the current question, a message two questions share answers none, and a claim
needs a session."""
import logging
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.domain.fact_review_question import QuestionStatus
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW, REF

TODAY = date(2026, 10, 24)
SESSION = "u1:D1"


@pytest.fixture
def world():
    return ReviewWorld()


async def queued(world, question_id, days_stale=30):
    return await world.seed_question(question_id=question_id, queue_key=NOW - timedelta(days=days_stale))


# ------------------------------------------------------------------------------------------------ M1

async def test_a_claim_raises_when_the_current_question_no_longer_validates_and_writes_nothing(world, caplog):
    caplog.set_level(logging.ERROR)
    await world.seed_state(current_question_id="out", sent=1, sent_on=TODAY)
    broken = {"question_id": "out", "account_id": "acc", "status": "asked"}     # the one with the owner, unreadable
    await world.store.db.collection(world.env.fact_review_questions_collection).document("out").set(broken)
    await queued(world, "next")
    state_before = dict(world.state_doc())
    next_before = dict(world.question_docs["next"])

    with pytest.raises(ValidationError):
        await world.review_store.claim_next("acc", TODAY, SESSION)

    assert world.state_doc() == state_before
    assert world.question_docs["next"] == next_before and world.question_docs["out"] == broken
    assert any("out" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


async def test_an_unreadable_question_the_state_does_not_point_at_is_still_skipped(world):
    await world.seed_state(current_question_id="gone", sent=1, sent_on=TODAY)
    await world.store.db.collection(world.env.fact_review_questions_collection).document("broken").set(
        {"question_id": "broken", "account_id": "acc", "status": "queued"})
    await queued(world, "next")

    claimed = await world.review_store.claim_next("acc", TODAY, SESSION)

    assert claimed is not None and claimed.question_id == "next"
    assert (await world.review_store.get_state("acc")).current_question_id == "next"


# ------------------------------------------------------------------------------------------------ L1

async def test_a_failed_delivery_of_a_question_that_is_not_current_sets_no_fail_day(world, caplog):
    caplog.set_level(logging.WARNING)
    await world.seed_state(current_question_id="current", sent=2, sent_on=TODAY)
    await world.seed_question(QuestionStatus.PENDING, question_id="stuck")

    assert await world.review_store.requeue_front("stuck", failed_today=True) is True

    state = await world.review_store.get_state("acc")
    assert (state.current_question_id, state.delivery_failed_on) == ("current", None)
    assert world.question_docs["stuck"]["status"] == "queued"
    assert any("stuck" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_a_failed_delivery_of_the_current_question_sets_the_claim_day(world):
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)
    await world.seed_question(QuestionStatus.PENDING, question_id="q1")

    assert await world.review_store.requeue_front("q1", failed_today=True) is True

    state = await world.review_store.get_state("acc")
    assert (state.current_question_id, state.delivery_failed_on) == (None, TODAY)


# ------------------------------------------------------------------------------------------------ L2

async def test_two_questions_on_one_message_answer_none_and_log(world, caplog):
    caplog.set_level(logging.ERROR)
    await world.seed_question(QuestionStatus.ASKED, question_id="a", message_ref=REF)
    await world.seed_question(QuestionStatus.ASKED, question_id="b", message_ref=REF, fact_id="f2")

    assert await world.review_store.by_message("u1", REF) is None
    assert any("share one message" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


# ------------------------------------------------------------------------------------------------ L3

async def test_a_claim_without_a_session_raises_and_writes_nothing(world):
    await queued(world, "q1")

    with pytest.raises(ValueError):
        await world.review_store.claim_next("acc", TODAY, "")

    assert world.question_docs["q1"]["status"] == "queued" and world.state_doc() is None
