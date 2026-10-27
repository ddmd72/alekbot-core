"""Facts P2 · card B: the store's two new moves, on the review world's evaluating Firestore fake."""
from datetime import timedelta

import pytest

from src.domain.fact_review_question import ClosedBy, QuestionStatus
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW


@pytest.fixture
def world():
    return ReviewWorld()


HAPPENED_TEXTS = {"happened": "Took place: the pottery class", "not_happened": "Did not take place: the pottery class",
                  "unknown": "Planned; unknown whether it took place: the pottery class"}


def _shape(fallback):
    """A history question is a `happened` one with its texts; a revive one is the plain still_true."""
    if fallback == "history":
        return dict(question="happened", texts=HAPPENED_TEXTS, fallback="history")
    return dict(fallback=fallback)


async def _closed(world, question_id, closed_by, fallback, account="acc"):
    asked = await world.seed_question(
        QuestionStatus.ASKED, question_id=question_id, account_id=account, **_shape(fallback))
    return await world.put_question(asked.closed(closed_by, NOW + timedelta(hours=1)))


async def test_timeout_histories_returns_only_this_accounts_timeout_history_questions(world):
    await _closed(world, "hit", ClosedBy.TIMEOUT, "history")
    await _closed(world, "by-elsewhere", ClosedBy.RESOLVED_ELSEWHERE, "history")
    await _closed(world, "revive", ClosedBy.TIMEOUT, "revive")
    await _closed(world, "other-account", ClosedBy.TIMEOUT, "history", account="acc2")
    await world.seed_question(QuestionStatus.ASKED, question_id="open", **_shape("history"))

    found = await world.review_store.timeout_histories("acc")

    assert [q.question_id for q in found] == ["hit"]


async def test_timeout_histories_refuses_an_empty_account(world):
    with pytest.raises(ValueError):
        await world.review_store.timeout_histories("")


async def test_timeout_histories_skips_an_unreadable_document(world):
    await _closed(world, "good", ClosedBy.TIMEOUT, "history")
    doc = dict(world.question_docs["good"])
    doc["question_id"] = "bad"
    doc["status"] = "no-such-status"
    world.question_docs["bad"] = doc

    found = await world.review_store.timeout_histories("acc")

    assert [q.question_id for q in found] == ["good"]


async def test_mark_fallback_failed_counts_one_attempt_and_leaves_the_question_open(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q", **_shape("history"))

    assert await world.review_store.mark_fallback_failed("q") is True
    assert await world.review_store.mark_fallback_failed("q") is True

    [back] = await world.review_store.get_questions(["q"])
    assert back.fallback_attempts == 2
    assert back.is_open
    assert world.question_docs["q"]["fallback_attempts"] == 2


async def test_mark_fallback_failed_is_false_for_a_missing_or_closed_question(world):
    await _closed(world, "done", ClosedBy.TIMEOUT, "history")

    assert await world.review_store.mark_fallback_failed("nope") is False
    assert await world.review_store.mark_fallback_failed("done") is False
    assert world.question_docs["done"]["fallback_attempts"] == 0
