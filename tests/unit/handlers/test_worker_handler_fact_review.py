"""Facts S4 · T10a: `/worker` task_type `fact_review` runs the memory review for the payload's owner (plan §3.2, §5 T10a).

The service is a fake with semantics: it records each run it was asked for and answers what a run answers. What is
asserted is the answer and the runs the handler asked for.
"""
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.fact_review_task_keys import FACT_REVIEW_TASK, review_payload
from src.handlers.worker_handler import WorkerHandler


class _FakeReview:
    """The run's contract as the handler sees it: `daily` by default, a mode that is neither raises (as the real
    service), every result 200-shaped."""

    def __init__(self, answer: Dict[str, Any]) -> None:
        self.answer = answer
        self.runs: List[Tuple[str, str, str]] = []

    async def run(self, user_id: str, account_id: str, mode: str = "daily") -> Dict[str, Any]:
        if mode not in ("daily", "next"):
            raise ValueError(f"{mode!r} is not a review mode")
        self.runs.append((user_id, account_id, mode))
        return dict(self.answer)


def _worker(review=None) -> WorkerHandler:
    return WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        fact_review=review,
    )


@pytest.mark.parametrize("mode, answer", [
    ("daily", {"status": "completed"}),
    ("next", {"status": "completed"}),
    ("daily", {"status": "deferred"}),
    ("daily", {"status": "failed"}),
    ("next", {"status": "not_owner"}),
])
async def test_both_modes_reach_the_service_with_the_payloads_ids_and_answer_its_result(mode, answer):
    review = _FakeReview(answer)

    body, status = await _worker(review).handle({"task_type": FACT_REVIEW_TASK, **review_payload("usr-1", "acc-1", mode)})

    assert (body, status) == (answer, 200)
    assert review.runs == [("usr-1", "acc-1", mode)]


async def test_a_payload_without_a_mode_is_a_daily_run():
    review = _FakeReview({"status": "completed"})

    body, status = await _worker(review).handle({"task_type": FACT_REVIEW_TASK, "user_id": "usr-1", "account_id": "acc-1"})

    assert (body, status) == ({"status": "completed"}, 200)
    assert review.runs == [("usr-1", "acc-1", "daily")]


@pytest.mark.parametrize("mode", ["weekly", "", "DAILY", 1, ["daily"]])
async def test_a_mode_that_is_neither_is_a_400_and_nothing_runs(mode):
    review = _FakeReview({"status": "completed"})

    body, status = await _worker(review).handle(
        {"task_type": FACT_REVIEW_TASK, "user_id": "usr-1", "account_id": "acc-1", "mode": mode})

    assert (body, status) == ({"error": "unknown mode"}, 400)
    assert review.runs == []


@pytest.mark.parametrize("payload", [
    {"account_id": "acc-1", "mode": "daily"},
    {"user_id": "usr-1", "mode": "daily"},
    {"user_id": "", "account_id": "acc-1"},
])
async def test_a_payload_without_both_ids_is_a_400_and_nothing_runs(payload):
    review = _FakeReview({"status": "completed"})

    body, status = await _worker(review).handle({"task_type": FACT_REVIEW_TASK, **payload})

    assert (body, status) == ({"error": "missing user_id or account_id"}, 400)
    assert review.runs == []


async def test_a_handler_without_the_service_answers_501():
    body, status = await _worker(None).handle({"task_type": FACT_REVIEW_TASK, **review_payload("usr-1", "acc-1", "daily")})

    assert (body, status) == ({"error": "fact review not configured"}, 501)


async def test_a_service_error_is_not_turned_into_an_answer():
    class _Broken(_FakeReview):
        async def run(self, user_id, account_id, mode="daily"):
            raise RuntimeError("the lease store is down")

    with pytest.raises(RuntimeError):
        await _worker(_Broken({})).handle({"task_type": FACT_REVIEW_TASK, **review_payload("usr-1", "acc-1", "daily")})
