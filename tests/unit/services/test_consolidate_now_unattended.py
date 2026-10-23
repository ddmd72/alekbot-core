"""Facts S2 · T7 review m2: `consolidate_now` marks a run `unattended` when a deferral or continuation had no task queue to
go to (local runs), so `$consolidate` does not promise "in the background". The flag is not part of the run's equality."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.consolidation import ConsolidationBatch
from src.domain.consolidation_run import ConsolidationRun
from src.services.consolidation_service import ConsolidationService

USER = "user-1"


def _service(*, task_queue, lease_free):
    queue = MagicMock()
    queue.get_pending_batches = AsyncMock(
        return_value=[ConsolidationBatch(batch_id="b1", user_id=USER, session_id="s", messages=[{"role": "user"}])]
    )
    queue.has_open_batches = AsyncMock(return_value=True)
    for name in ("reset_recoverable_batches", "update_batch_status", "delete_batch"):
        setattr(queue, name, AsyncMock())
    lease = MagicMock(acquire=AsyncMock(return_value=lease_free), release=AsyncMock())
    coordinator = MagicMock(route_message=AsyncMock(return_value=AgentResponse.success("b1", "agent", {})))
    return ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=MagicMock(ensure_agents_for_user=AsyncMock()),
        lease=lease, run_timeout_s=900.0, task_queue=task_queue,
    )


async def test_a_busy_lease_without_a_task_queue_is_a_deferred_run_marked_unattended():
    run = await _service(task_queue=None, lease_free=False).consolidate_now(USER)

    assert run.deferred and run.unattended
    assert run == ConsolidationRun(deferred=True)


async def test_more_pending_without_a_task_queue_is_a_has_more_run_marked_unattended():
    run = await _service(task_queue=None, lease_free=True).consolidate_now(USER)

    assert run.processed == 1 and run.has_more and run.unattended


async def test_with_a_task_queue_the_run_is_attended_and_the_continuation_is_enqueued():
    tasks = MagicMock(enqueue_consolidation_task=AsyncMock(), enqueue_worker_task=AsyncMock())

    run = await _service(task_queue=tasks, lease_free=True).consolidate_now(USER)

    assert run.has_more and not run.unattended
    tasks.enqueue_consolidation_task.assert_awaited_once_with(user_id=USER)


async def test_a_deferral_with_a_task_queue_is_attended():
    tasks = MagicMock(enqueue_worker_task=AsyncMock())

    run = await _service(task_queue=tasks, lease_free=False).consolidate_now(USER)

    assert run.deferred and not run.unattended
    tasks.enqueue_worker_task.assert_awaited_once()
