"""StartDailyEmailReviewTask: the hourly fan-out, every branch of `__call__` (composition-root refactor S5ef).

The eligible users come from a scripted review service; the marks, the queue and the clock are fakes that record.
"""
import logging
from datetime import date, datetime, timezone
from typing import Any, List, Optional, Set

from src.domain.daily_review_window import EligibleReviewUser
from src.domain.worker_task import WorkerTask
from src.ports.dedup_store import DedupStore
from src.services import start_daily_email_review_task as module
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
from tests.support.clock_instants import pin_datetime

SLOT = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 9, 5, tzinfo=timezone.utc)
DEADLINE = 1620


class Review:
    def __init__(self, users: List[EligibleReviewUser]) -> None:
        self.users = users
        self.asked: List[Any] = []

    async def find_eligible_users(self, user_repo: Any, now_utc: datetime) -> List[EligibleReviewUser]:
        self.asked.append((user_repo, now_utc))
        return self.users


class Dispatch:
    def __init__(self, failing: Set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.failing = set(failing)
        self.calls: List[Any] = []

    async def enqueue_worker_task(self, task_type: str, payload: dict, *, deadline_seconds: int, dedup_key: str) -> None:
        self.calls.append((task_type, payload, deadline_seconds, dedup_key))
        if payload["user_id"] in self.failing:
            raise RuntimeError("queue down")


class Marks(DedupStore):
    def __init__(self, claim_fails: Set[str] = frozenset(), release_fails: bool = False) -> None:  # type: ignore[assignment]
        self.held: Set[str] = set()
        self.claim_fails = set(claim_fails)
        self.release_fails = release_fails

    async def is_duplicate(self, event_id: str) -> bool:
        return event_id in self.held

    async def try_mark_processed(self, event_id: str) -> bool:
        if any(user in event_id for user in self.claim_fails):
            raise RuntimeError("store down")
        if event_id in self.held:
            return False
        self.held.add(event_id)
        return True

    async def release(self, event_id: str) -> None:
        if self.release_fails:
            raise RuntimeError("store down")
        self.held.discard(event_id)


def _user(user_id: str, account_id: str = "acct-1") -> EligibleReviewUser:
    return EligibleReviewUser(user_id, account_id, SLOT)


def _task(users: List[EligibleReviewUser], dispatch: Dispatch, marks: Optional[Marks], repo: Any = None) -> StartDailyEmailReviewTask:
    return StartDailyEmailReviewTask(
        email_review=Review(users), task_dispatch=dispatch, user_repo=repo or object(), marks=marks,  # type: ignore[arg-type]
        deadline_seconds=DEADLINE)


async def test_it_is_a_worker_task_that_asks_the_review_service_with_the_repo_and_the_clock(monkeypatch):
    pin_datetime(monkeypatch, module, NOW)
    repo = object()
    task = _task([], Dispatch(), Marks(), repo)
    runner: WorkerTask = task

    assert await runner({"task_type": "start_daily_email_review"}, {}) == ({"started": 0, "failed": 0}, 200)
    assert task._email_review.asked == [(repo, NOW)]


async def test_each_eligible_user_is_marked_then_enqueued_with_the_mark_as_the_task_name_and_the_deadline():
    dispatch, marks = Dispatch(), Marks()
    task = _task([_user("user-1", "acct-1"), _user("user-2", "acct-2")], dispatch, marks)

    assert await task({}, {}) == ({"started": 2, "failed": 0}, 200)
    assert dispatch.calls == [
        ("daily_email_review", {"user_id": "user-1", "account_id": "acct-1"}, DEADLINE, "daily_review:user-1:2026-10-22"),
        ("daily_email_review", {"user_id": "user-2", "account_id": "acct-2"}, DEADLINE, "daily_review:user-2:2026-10-22"),
    ]
    assert marks.held == {"daily_review:user-1:2026-10-22", "daily_review:user-2:2026-10-22"}


async def test_a_user_already_marked_for_the_slot_is_skipped(caplog):
    dispatch, marks = Dispatch(), Marks()
    task = _task([_user("user-1")], dispatch, marks)
    await task({}, {})

    with caplog.at_level(logging.INFO):
        assert await task({}, {}) == ({"started": 0, "failed": 0}, 200)

    assert len(dispatch.calls) == 1
    assert "[Worker] start_daily_email_review: already started for user-1 on 2026-10-22" in caplog.messages


async def test_a_failing_claim_skips_that_user_only_and_counts_it(caplog):
    dispatch, marks = Dispatch(), Marks(claim_fails={"user-1"})
    task = _task([_user("user-1"), _user("user-2")], dispatch, marks)

    with caplog.at_level(logging.ERROR):
        assert await task({}, {}) == ({"started": 1, "failed": 1}, 200)

    assert [c[1]["user_id"] for c in dispatch.calls] == ["user-2"]
    assert any("claim failed for user-1" in m for m in caplog.messages)


async def test_a_failed_enqueue_releases_the_mark_so_the_next_tick_retries(caplog):
    dispatch, marks = Dispatch(failing={"user-1"}), Marks()
    task = _task([_user("user-1")], dispatch, marks)

    with caplog.at_level(logging.ERROR):
        assert await task({}, {}) == ({"started": 0, "failed": 1}, 200)

    assert marks.held == set()
    assert any("enqueue failed for user-1" in m for m in caplog.messages)
    dispatch.failing.clear()
    assert await task({}, {}) == ({"started": 1, "failed": 0}, 200)


async def test_a_release_that_fails_too_is_logged_and_the_next_user_is_still_enqueued(caplog):
    dispatch, marks = Dispatch(failing={"user-1"}), Marks(release_fails=True)
    task = _task([_user("user-1"), _user("user-2")], dispatch, marks)

    with caplog.at_level(logging.ERROR):
        assert await task({}, {}) == ({"started": 1, "failed": 1}, 200)

    assert [c[1]["user_id"] for c in dispatch.calls] == ["user-1", "user-2"]
    assert any("mark release failed for user-1" in m for m in caplog.messages)


async def test_without_a_marks_store_it_warns_and_enqueues_every_tick(caplog):
    dispatch = Dispatch()
    task = _task([_user("user-1")], dispatch, None)

    with caplog.at_level(logging.WARNING):
        await task({}, {})
        await task({}, {})

    assert len(dispatch.calls) == 2
    assert caplog.messages.count("[Worker] start_daily_email_review: no daily_review_marks store, duplicates possible") == 2


async def test_a_failed_enqueue_without_a_marks_store_is_counted_and_nothing_is_released():
    dispatch = Dispatch(failing={"user-1"})
    task = _task([_user("user-1")], dispatch, None)

    assert await task({}, {}) == ({"started": 0, "failed": 1}, 200)
