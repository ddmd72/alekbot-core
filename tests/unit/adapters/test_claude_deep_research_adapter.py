"""
Unit tests for ClaudeDeepResearchAdapter.

Pattern: mock TaskQueue port (AsyncMock), call real adapter method, assert on
what was enqueued and what came back.

SDK boundary: task_queue.enqueue_agent_task (the only external call this adapter makes).
"""
import uuid
import pytest
from unittest.mock import AsyncMock

from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.domain.user import PerformanceTier
from src.ports.task_queue import TaskQueue


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def task_queue():
    q = AsyncMock(spec=TaskQueue)
    q.enqueue_agent_task.return_value = "task-name-123"
    return q


# ============================================================================
# Tier → model mapping
# ============================================================================

def test_resolve_model_eco():
    adapter = ClaudeDeepResearchAdapter(task_queue=AsyncMock())
    assert adapter._resolve_model(PerformanceTier.ECO) == "claude-haiku-4-5-20251001"


def test_resolve_model_balanced():
    adapter = ClaudeDeepResearchAdapter(task_queue=AsyncMock())
    assert adapter._resolve_model(PerformanceTier.BALANCED) == "claude-sonnet-4-6"


def test_resolve_model_performance():
    adapter = ClaudeDeepResearchAdapter(task_queue=AsyncMock())
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "claude-opus-4-6"


def test_model_override_wins():
    adapter = ClaudeDeepResearchAdapter(task_queue=AsyncMock(), model_override="claude-special")
    assert adapter._resolve_model(PerformanceTier.PERFORMANCE) == "claude-special"


# ============================================================================
# create_interaction — task queue wire tests
# ============================================================================

@pytest.mark.asyncio
async def test_create_interaction_enqueues_agent_task(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Research question",
        user_id="u1",
        account_id="acc1",
        original_query="Original Q",
        tier=PerformanceTier.BALANCED,
    )

    task_queue.enqueue_agent_task.assert_awaited_once()
    call_kwargs = task_queue.enqueue_agent_task.await_args.kwargs
    assert call_kwargs["agent_id"] == "claude_deep_research_runner"
    assert call_kwargs["query"] == "Research question"


@pytest.mark.asyncio
async def test_create_interaction_context_carries_model(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Original Q",
        tier=PerformanceTier.PERFORMANCE,
    )

    ctx = task_queue.enqueue_agent_task.await_args.kwargs["context"]
    assert ctx["model"] == "claude-opus-4-6"
    assert ctx["user_id"] == "u1"
    assert ctx["account_id"] == "acc1"
    assert ctx["original_query"] == "Original Q"


@pytest.mark.asyncio
async def test_create_interaction_returns_uuid_matching_context_job_id(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    job_id = await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
    )

    ctx = task_queue.enqueue_agent_task.await_args.kwargs["context"]
    assert ctx["job_id"] == job_id
    # Must be a valid UUID
    uuid.UUID(job_id)


@pytest.mark.asyncio
async def test_create_interaction_deadline_is_1800(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
    )

    deadline = task_queue.enqueue_agent_task.await_args.kwargs.get("deadline_seconds")
    assert deadline == 1800


@pytest.mark.asyncio
async def test_create_interaction_session_id_forwarded(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
        session_id="sess-abc",
    )

    ctx = task_queue.enqueue_agent_task.await_args.kwargs["context"]
    assert ctx["session_id"] == "sess-abc"


@pytest.mark.asyncio
async def test_create_interaction_system_prompt_forwarded(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
        system_prompt="You are a researcher.",
    )

    ctx = task_queue.enqueue_agent_task.await_args.kwargs["context"]
    assert ctx["system_prompt"] == "You are a researcher."


@pytest.mark.asyncio
async def test_create_interaction_none_system_prompt_stored_as_empty_string(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)

    await adapter.create_interaction(
        query="Q",
        user_id="u1",
        account_id="acc1",
        original_query="Q",
        system_prompt=None,
    )

    ctx = task_queue.enqueue_agent_task.await_args.kwargs["context"]
    assert ctx["system_prompt"] == ""


# ============================================================================
# get_status — always in_progress (delivery via runner agent)
# ============================================================================

@pytest.mark.asyncio
async def test_get_status_always_returns_in_progress(task_queue):
    adapter = ClaudeDeepResearchAdapter(task_queue=task_queue)
    status, payload = await adapter.get_status("some-job-id")
    assert status == "in_progress"
    assert payload == ""
