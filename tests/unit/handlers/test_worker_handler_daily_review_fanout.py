"""Daily email review fan-out: one enqueue per user per local date (S1b task D)."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import dispatch_deadline_s
from src.domain.daily_review_window import EligibleReviewUser
from src.ports.dedup_store import DedupStore
from src.services.email_review_service import EmailReviewService
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask

_SLOT = date(2026, 10, 22)


class _MemoryDedup(DedupStore):
    def __init__(self, fail_for=()):
        self.marks = set()
        self.fail_for = set(fail_for)

    async def is_duplicate(self, event_id):
        return event_id in self.marks

    async def try_mark_processed(self, event_id):
        if any(f in event_id for f in self.fail_for):
            raise RuntimeError("store down")
        if event_id in self.marks:
            return False
        self.marks.add(event_id)
        return True

    async def release(self, event_id):
        self.marks.discard(event_id)


def _worker(users, marks, enqueue=None):
    review = MagicMock(spec=EmailReviewService)
    review.find_eligible_users = AsyncMock(return_value=users)
    dispatch = AsyncMock()
    if enqueue is not None:
        dispatch.enqueue_worker_task = enqueue
    worker = StartDailyEmailReviewTask(
        email_review=review,
        task_dispatch=dispatch,
        user_repo=MagicMock(),
        marks=marks,
        deadline_seconds=dispatch_deadline_s(NotificationKind.DAILY_DIGEST),
    )
    return worker, dispatch


def _u(uid, acc="acc"):
    return EligibleReviewUser(uid, acc, _SLOT)


async def test_three_ticks_inside_window_enqueue_once():
    worker, dispatch = _worker([_u("user-1")], _MemoryDedup())
    for _ in range(3):
        result, status = await worker({}, {})
        assert status == 200
    assert dispatch.enqueue_worker_task.call_count == 1


async def test_scheduler_recall_in_same_hour_does_not_enqueue_twice():
    worker, dispatch = _worker([_u("user-1")], _MemoryDedup())
    first, _ = await worker({}, {})
    second, _ = await worker({}, {})
    assert first["started"] == 1 and second["started"] == 0
    assert dispatch.enqueue_worker_task.call_count == 1


async def test_next_local_date_enqueues_again():
    marks = _MemoryDedup()
    worker, dispatch = _worker([_u("user-1")], marks)
    await worker({}, {})
    worker._email_review.find_eligible_users.return_value = [
        EligibleReviewUser("user-1", "acc", date(2026, 10, 23))
    ]
    await worker({}, {})
    assert dispatch.enqueue_worker_task.call_count == 2


async def test_enqueue_failure_releases_mark_and_next_tick_enqueues():
    marks = _MemoryDedup()
    enqueue = AsyncMock(side_effect=[RuntimeError("tasks down"), None])
    worker, dispatch = _worker([_u("user-1")], marks, enqueue)

    result, status = await worker({}, {})
    assert status == 200 and result["started"] == 0 and result["failed"] == 1
    assert marks.marks == set()

    result, _ = await worker({}, {})
    assert result["started"] == 1
    assert enqueue.call_count == 2


async def test_enqueue_failure_for_one_user_does_not_stop_the_next():
    enqueue = AsyncMock(side_effect=[RuntimeError("boom"), None])
    worker, _ = _worker([_u("user-1"), _u("user-2")], _MemoryDedup(), enqueue)
    result, _ = await worker({}, {})
    assert result["started"] == 1 and result["failed"] == 1


async def test_claim_store_raises_skips_that_user_only():
    marks = _MemoryDedup(fail_for=["user-1"])
    worker, dispatch = _worker([_u("user-1"), _u("user-2")], marks)
    result, status = await worker({}, {})
    assert status == 200 and result["started"] == 1 and result["failed"] == 1
    dispatch.enqueue_worker_task.assert_awaited_once()
    assert dispatch.enqueue_worker_task.call_args.args[1]["user_id"] == "user-2"


async def test_no_store_configured_enqueues_every_tick_as_before():
    worker, dispatch = _worker([_u("user-1")], None)
    await worker({}, {})
    await worker({}, {})
    assert dispatch.enqueue_worker_task.call_count == 2


async def test_mark_key_names_user_and_slot_date():
    marks = _MemoryDedup()
    worker, _ = _worker([_u("user-1")], marks)
    await worker({}, {})
    assert marks.marks == {"daily_review:user-1:2026-10-22"}


async def test_mark_is_held_when_enqueue_is_called():
    marks = _MemoryDedup()
    seen = []

    async def enqueue(*args, **kwargs):
        seen.append("daily_review:user-1:2026-10-22" in marks.marks)

    worker, _ = _worker([_u("user-1")], marks, AsyncMock(side_effect=enqueue))
    await worker({}, {})
    assert seen == [True]


async def test_enqueue_passes_dedup_key_and_deadline():
    worker, dispatch = _worker([_u("user-1", "acc-9")], _MemoryDedup())
    await worker({}, {})
    dispatch.enqueue_worker_task.assert_awaited_once_with(
        "daily_email_review",
        {"user_id": "user-1", "account_id": "acc-9", "slot_date": "2026-10-22"},
        deadline_seconds=dispatch_deadline_s(NotificationKind.DAILY_DIGEST),
        dedup_key="daily_review:user-1:2026-10-22",
    )
