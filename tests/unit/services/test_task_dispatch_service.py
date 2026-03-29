"""
Unit tests for TaskDispatchService.

Verifies that every dispatch method delegates to the underlying TaskQueue
port with the correct arguments and returns its result.
"""
from unittest.mock import AsyncMock

import pytest

from src.ports.task_queue import TaskQueue
from src.services.task_dispatch_service import TaskDispatchService


@pytest.fixture
def queue():
    q = AsyncMock(spec=TaskQueue)
    q.enqueue_agent_task.return_value = "task-agent-001"
    q.enqueue_email_indexing_task.return_value = "task-email-001"
    q.enqueue_consolidation_task.return_value = "task-consolidation-001"
    q.enqueue_deep_research_polling.return_value = "task-dr-001"
    q.enqueue_worker_task.return_value = "task-worker-001"
    return q


@pytest.fixture
def service(queue):
    return TaskDispatchService(queue)


# ---------------------------------------------------------------------------
# enqueue_agent_task
# ---------------------------------------------------------------------------

class TestEnqueueAgentTask:

    async def test_delegates_all_params(self, service, queue):
        result = await service.enqueue_agent_task(
            agent_id="agent-001",
            intent="search_memory",
            query="find notes about Paris",
            context={"user_id": "u1"},
            deadline_seconds=30,
        )

        queue.enqueue_agent_task.assert_called_once_with(
            agent_id="agent-001",
            intent="search_memory",
            query="find notes about Paris",
            context={"user_id": "u1"},
            deadline_seconds=30,
        )
        assert result == "task-agent-001"

    async def test_deadline_defaults_to_none(self, service, queue):
        await service.enqueue_agent_task(
            agent_id="a", intent="i", query="q", context={}
        )
        _, kwargs = queue.enqueue_agent_task.call_args
        assert kwargs["deadline_seconds"] is None


# ---------------------------------------------------------------------------
# enqueue_email_indexing_task
# ---------------------------------------------------------------------------

class TestEnqueueEmailIndexingTask:

    async def test_delegates_job_id(self, service, queue):
        result = await service.enqueue_email_indexing_task("job-xyz")

        queue.enqueue_email_indexing_task.assert_called_once_with("job-xyz")
        assert result == "task-email-001"


# ---------------------------------------------------------------------------
# enqueue_consolidation_task
# ---------------------------------------------------------------------------

class TestEnqueueConsolidationTask:

    async def test_delegates_user_id(self, service, queue):
        result = await service.enqueue_consolidation_task(user_id="user-abc")

        queue.enqueue_consolidation_task.assert_called_once_with(user_id="user-abc")
        assert result == "task-consolidation-001"


# ---------------------------------------------------------------------------
# enqueue_deep_research_polling
# ---------------------------------------------------------------------------

class TestEnqueueDeepResearchPolling:

    async def test_delegates_all_params(self, service, queue):
        result = await service.enqueue_deep_research_polling(
            interaction_id="inter-001",
            user_id="user-abc",
            account_id="acc-abc",
            query="AI trends",
            attempt=3,
            consecutive_errors=1,
            delay_seconds=120,
            provider="gemini",
            session_id="sess-001",
        )

        queue.enqueue_deep_research_polling.assert_called_once_with(
            interaction_id="inter-001",
            user_id="user-abc",
            account_id="acc-abc",
            query="AI trends",
            attempt=3,
            consecutive_errors=1,
            delay_seconds=120,
            provider="gemini",
            session_id="sess-001",
        )
        assert result == "task-dr-001"

    async def test_defaults_applied(self, service, queue):
        await service.enqueue_deep_research_polling(
            interaction_id="i", user_id="u", account_id="a"
        )
        _, kwargs = queue.enqueue_deep_research_polling.call_args
        assert kwargs["attempt"] == 0
        assert kwargs["consecutive_errors"] == 0
        assert kwargs["delay_seconds"] == 30
        assert kwargs["provider"] == "gemini"
        assert kwargs["session_id"] == ""


# ---------------------------------------------------------------------------
# enqueue_worker_task
# ---------------------------------------------------------------------------

class TestEnqueueWorkerTask:

    async def test_delegates_task_type_and_payload(self, service, queue):
        payload = {"user_id": "u1", "job_id": "j1"}
        result = await service.enqueue_worker_task(
            task_type="email_indexing",
            payload=payload,
            delay_seconds=10,
        )

        queue.enqueue_worker_task.assert_called_once_with(
            task_type="email_indexing",
            payload=payload,
            delay_seconds=10,
        )
        assert result == "task-worker-001"

    async def test_delay_defaults_to_zero(self, service, queue):
        await service.enqueue_worker_task(task_type="t", payload={})
        _, kwargs = queue.enqueue_worker_task.call_args
        assert kwargs["delay_seconds"] == 0
