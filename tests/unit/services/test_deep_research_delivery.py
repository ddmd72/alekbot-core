"""
Unit tests for deep_research_delivery.deliver_deep_research().
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ports.task_queue import TaskQueue
from src.ports.media_storage_port import MediaStoragePort
from src.services.deep_research_delivery import (
    deliver_deep_research,
    _build_html_page_query,
    upload_html_report,
)


# ---------------------------------------------------------------------------
# _build_html_page_query
# ---------------------------------------------------------------------------

def test_build_html_page_query_includes_topic_and_findings():
    result = _build_html_page_query("quantum computing trends", "Finding A\nFinding B")
    assert "quantum computing trends" in result
    assert "Finding A" in result
    assert "Finding B" in result


def test_build_html_page_query_empty_topic():
    result = _build_html_page_query("", "Some findings")
    assert "Some findings" in result


# ---------------------------------------------------------------------------
# deliver_deep_research — happy path: enqueues html_page_generator task
# ---------------------------------------------------------------------------

async def test_delivers_enqueues_html_page_task():
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
    assert kwargs["agent_id"] == "html_page_generator_agent"
    assert kwargs["intent"] == "create_html_page"
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

    await deliver_deep_research(
        result_text="Report",
        user_id="u1",
        account_id="a1",
        query="topic",
        task_queue=task_queue,
    )


# ---------------------------------------------------------------------------
# deliver_deep_research — single-pass: uploads one "report" round
# ---------------------------------------------------------------------------

async def test_single_pass_uploads_report_round():
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.return_value = "https://storage/report.md"
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="Final report",
        user_id="u1",
        account_id="a1",
        query="topic",
        task_queue=task_queue,
        round1_text="",
        media_storage=media_storage,
        notification=notification,
    )

    media_storage.store.assert_called_once()
    key_arg = media_storage.store.call_args.kwargs["key"]
    assert key_arg.endswith("-report.md")
    notification.notify_document_link.assert_called_once()
    assert notification.notify_document_link.call_args.kwargs["label"] == "Research report (raw)"


# ---------------------------------------------------------------------------
# deliver_deep_research — two-pass: uploads round1 + round2
# ---------------------------------------------------------------------------

async def test_two_pass_uploads_both_rounds():
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.side_effect = [
        "https://storage/round1.md",
        "https://storage/round2.md",
    ]
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="Round 2 verified",
        user_id="u1",
        account_id="a1",
        query="topic",
        task_queue=task_queue,
        round1_text="Round 1 raw",
        media_storage=media_storage,
        notification=notification,
    )

    assert media_storage.store.call_count == 2
    labels = [c.kwargs["label"] for c in notification.notify_document_link.call_args_list]
    assert "Round 1 — raw research" in labels
    assert "Round 2 — verified report" in labels


# ---------------------------------------------------------------------------
# upload_html_report()
# ---------------------------------------------------------------------------

async def test_upload_html_report_no_storage_returns_none():
    result = await upload_html_report("# Report", "u1", media_storage=None)
    assert result is None


async def test_upload_html_report_uploads_html():
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.return_value = "https://storage/report.html"
    result = await upload_html_report("# Deep Research", "u1", media_storage=media_storage)
    assert result == "https://storage/report.html"
    kwargs = media_storage.store.call_args.kwargs
    assert kwargs["content_type"] == "text/html; charset=utf-8"
    assert "deep_research/u1/" in kwargs["key"]
    assert kwargs["key"].endswith(".html")


async def test_upload_html_report_escapes_html():
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.return_value = "https://storage/x.html"
    await upload_html_report("<script>alert('xss')</script>", "u1", media_storage=media_storage)
    data = media_storage.store.call_args.kwargs["data"]
    assert b"<script>" not in data
    assert b"&lt;script&gt;" in data


async def test_upload_html_report_exception_returns_none():
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.side_effect = Exception("GCS down")
    result = await upload_html_report("report", "u1", media_storage=media_storage)
    assert result is None


# ---------------------------------------------------------------------------
# deliver_deep_research — exception paths in notify_document_link
# ---------------------------------------------------------------------------

async def test_single_pass_notify_exception_does_not_raise():
    """notify_document_link failure in single-pass path must not propagate."""
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.return_value = "https://storage/report.md"
    notification = AsyncMock()
    notification.notify_document_link.side_effect = RuntimeError("notify failed")

    await deliver_deep_research(
        result_text="Final report",
        user_id="u1", account_id="a1", query="topic",
        task_queue=task_queue,
        round1_text="",
        media_storage=media_storage,
        notification=notification,
    )
    # Still enqueues HTML page task despite notify error
    task_queue.enqueue_agent_task.assert_called_once()


async def test_two_pass_notify_round1_exception_does_not_raise():
    """notify_document_link failure for round1 must not stop round2 or HTML task."""
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.side_effect = ["https://r1.md", "https://r2.md"]
    notification = AsyncMock()
    notification.notify_document_link.side_effect = [
        RuntimeError("notify failed"),  # round1
        None,                           # round2 succeeds
    ]

    await deliver_deep_research(
        result_text="Round 2", user_id="u1", account_id="a1", query="topic",
        task_queue=task_queue,
        round1_text="Round 1",
        media_storage=media_storage,
        notification=notification,
    )
    task_queue.enqueue_agent_task.assert_called_once()


async def test_two_pass_notify_round2_exception_does_not_raise():
    """notify_document_link failure for round2 must not stop HTML task."""
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.side_effect = ["https://r1.md", "https://r2.md"]
    notification = AsyncMock()
    notification.notify_document_link.side_effect = [
        None,                           # round1 succeeds
        RuntimeError("notify r2 fail"), # round2 throws
    ]

    await deliver_deep_research(
        result_text="Round 2", user_id="u1", account_id="a1", query="topic",
        task_queue=task_queue,
        round1_text="Round 1",
        media_storage=media_storage,
        notification=notification,
    )
    task_queue.enqueue_agent_task.assert_called_once()


async def test_upload_round_exception_does_not_raise():
    """_upload_round failure (GCS down) → url is None → notify_document_link skipped."""
    task_queue = AsyncMock(spec=TaskQueue)
    media_storage = AsyncMock(spec=MediaStoragePort)
    media_storage.store.side_effect = Exception("GCS down")
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="report", user_id="u1", account_id="a1", query="topic",
        task_queue=task_queue,
        media_storage=media_storage,
        notification=notification,
    )
    notification.notify_document_link.assert_not_called()
    task_queue.enqueue_agent_task.assert_called_once()


async def test_no_media_storage_skips_upload():
    """media_storage=None → skips round uploads, still enqueues HTML task."""
    task_queue = AsyncMock(spec=TaskQueue)
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="report", user_id="u1", account_id="a1", query="topic",
        task_queue=task_queue,
        media_storage=None,
        notification=notification,
    )
    notification.notify_document_link.assert_not_called()
    task_queue.enqueue_agent_task.assert_called_once()
