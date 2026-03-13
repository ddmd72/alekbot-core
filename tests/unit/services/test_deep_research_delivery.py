"""
Unit tests for deep_research_delivery.deliver_deep_research().
"""

from unittest.mock import AsyncMock, call

import pytest

from src.ports.task_queue import TaskQueue
from src.services.deep_research_delivery import deliver_deep_research, _build_doc_planner_query


# ---------------------------------------------------------------------------
# _build_doc_planner_query
# ---------------------------------------------------------------------------

def test_build_doc_planner_query_includes_topic_and_findings():
    result = _build_doc_planner_query("quantum computing trends", "Finding A\nFinding B")
    assert "quantum computing trends" in result
    assert "Finding A" in result
    assert "Finding B" in result


def test_build_doc_planner_query_empty_topic():
    result = _build_doc_planner_query("", "Some findings")
    assert "Some findings" in result
    # No crash with empty original_query


# ---------------------------------------------------------------------------
# deliver_deep_research — happy path
# ---------------------------------------------------------------------------

async def test_delivers_enqueues_doc_planner_task():
    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_agent_task.return_value = "task-xyz"

    await deliver_deep_research(
        result_text="Research report content",
        user_id="u1",
        account_id="a1",
        query="What is quantum computing?",
        task_queue=task_queue,
        session_id="sess1",
    )

    task_queue.enqueue_agent_task.assert_called_once()
    kwargs = task_queue.enqueue_agent_task.call_args.kwargs
    assert kwargs["agent_id"] == "doc_planner_agent"
    assert kwargs["intent"] == "create_document"
    assert kwargs["context"]["user_id"] == "u1"
    assert kwargs["context"]["account_id"] == "a1"
    assert kwargs["context"]["session_id"] == "sess1"
    assert kwargs["deadline_seconds"] == 720


async def test_delivers_query_contains_research_content():
    task_queue = AsyncMock(spec=TaskQueue)

    await deliver_deep_research(
        result_text="Key findings about AI",
        user_id="u1",
        account_id="a1",
        query="AI research topic",
        task_queue=task_queue,
    )

    query_arg = task_queue.enqueue_agent_task.call_args.kwargs["query"]
    assert "AI research topic" in query_arg
    assert "Key findings about AI" in query_arg


# ---------------------------------------------------------------------------
# deliver_deep_research — no task_queue
# ---------------------------------------------------------------------------

async def test_no_task_queue_skips_gracefully():
    # Should log a warning and return without raising
    await deliver_deep_research(
        result_text="Research report",
        user_id="u1",
        account_id="a1",
        query="topic",
        task_queue=None,
    )
    # No exception = pass


# ---------------------------------------------------------------------------
# deliver_deep_research — enqueue failure
# ---------------------------------------------------------------------------

async def test_enqueue_failure_does_not_raise():
    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_agent_task.side_effect = RuntimeError("Cloud Tasks unavailable")

    # Should log error and return gracefully, not propagate the exception
    await deliver_deep_research(
        result_text="Report",
        user_id="u1",
        account_id="a1",
        query="topic",
        task_queue=task_queue,
    )
