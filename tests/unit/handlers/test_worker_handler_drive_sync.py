"""WorkerHandler start_drive_sync delegates to DriveSyncSchedulerService (RFC Δ5)."""
from unittest.mock import AsyncMock, MagicMock

from src.handlers.worker_handler import WorkerHandler


def _worker(scheduler=None):
    return WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        drive_sync_scheduler=scheduler,
    )


async def test_start_drive_sync_delegates_to_the_fan_out():
    scheduler = AsyncMock()
    scheduler.fan_out.return_value = {"candidates": 2, "enqueued": 2}
    body, status = await _worker(scheduler).handle({"task_type": "start_drive_sync"})
    assert (body, status) == ({"candidates": 2, "enqueued": 2}, 200)
    scheduler.fan_out.assert_awaited_once_with()


async def test_unconfigured_is_a_quiet_200():
    """No DRIVE_INDEXER_URL yet (first deploy pass): a 5xx every 10 minutes would page the error alert."""
    body, status = await _worker(None).handle({"task_type": "start_drive_sync"})
    assert (body, status) == ({"status": "disabled"}, 200)


async def test_drive_sync_is_not_a_main_service_task():
    """drive_sync runs on the indexer service only; the main /worker falls back for it (None)."""
    assert await _worker(AsyncMock()).handle({"task_type": "drive_sync", "user_id": "u"}) is None
