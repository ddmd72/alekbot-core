"""A named report task is retried by name; AlreadyExists is a hand-off (S1b task W fix round 1)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded

from src.services.deep_research_delivery import deliver_deep_research


async def _run(queue, dedup="j1"):
    notifier = AsyncMock()
    notifier.notify.return_value = MagicMock(delivered=True, sent=True, error=None)
    with patch("src.services.deep_research_delivery.asyncio.sleep", new=AsyncMock()) as sleep:
        ok = await deliver_deep_research(result_text="t", user_id="u1", account_id="a1", query="q",
                                         task_queue=queue, failure_notification=notifier,
                                         report_dedup_id=dedup)
    return ok, notifier, sleep


async def test_timeout_then_already_exists_is_handed_off_without_notice():
    queue = AsyncMock()
    queue.enqueue_agent_task.side_effect = [DeadlineExceeded("t"), AlreadyExists("x")]
    ok, notifier, sleep = await _run(queue)
    assert ok is True
    notifier.notify.assert_not_awaited()
    sleep.assert_awaited_once_with(0.5)
    keys = {c.kwargs["dedup_key"] for c in queue.enqueue_agent_task.await_args_list}
    assert keys == {"deep_research_report:j1"}


async def test_all_attempts_failing_notifies_once_with_retry_suppressed():
    queue = AsyncMock()
    queue.enqueue_agent_task.side_effect = DeadlineExceeded("t")
    ok, notifier, sleep = await _run(queue)
    assert ok is False
    assert queue.enqueue_agent_task.await_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [0.5, 1.0]
    notifier.notify.assert_awaited_once()
    assert notifier.notify.await_args.kwargs["suppress_transient_retry"] is True


async def test_unnamed_task_is_not_retried():
    queue = AsyncMock()
    queue.enqueue_agent_task.side_effect = DeadlineExceeded("t")
    ok, notifier, _ = await _run(queue, dedup=None)
    assert ok is False
    assert queue.enqueue_agent_task.await_count == 1
    notifier.notify.assert_awaited_once()
