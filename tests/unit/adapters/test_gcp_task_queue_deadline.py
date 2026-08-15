"""``enqueue_worker_task`` must translate ``deadline_seconds`` into the Cloud Tasks
``dispatch_deadline`` field.

Omitting the field is not neutral: Cloud Tasks then applies its own 600s default,
which silently truncates any longer in-process budget and — because a truncated
dispatch reads as a task failure — retries the whole run. That is how one slow
provider turned a single failed briefing into three full re-runs and an open
circuit breaker on 2026-08-15.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gcp_task_queue import GcpTaskQueue


@pytest.fixture
def queue_and_client():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as mock_client_class:
        mock_client = mock_client_class.return_value
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        # `name` is a MagicMock constructor kwarg, so it must be set after creation.
        created = MagicMock()
        created.name = "t-1"
        mock_client.create_task.return_value = created

        queue = GcpTaskQueue(
            project_id="test-project",
            location="us-central1",
            queue_name="test-queue",
            service_url="https://example.invalid",
        )
        yield queue, mock_client


def _task_from(mock_client):
    return mock_client.create_task.call_args.kwargs["request"]["task"]


class TestDispatchDeadline:
    async def test_deadline_seconds_becomes_dispatch_deadline(self, queue_and_client):
        queue, client = queue_and_client

        await queue.enqueue_worker_task(
            task_type="execute_reminder", payload={"note_id": "n1"},
            deadline_seconds=1620,
        )

        assert _task_from(client)["dispatch_deadline"].seconds == 1620

    async def test_omitted_when_not_requested(self, queue_and_client):
        """No field → Cloud Tasks default. Correct for the short task types."""
        queue, client = queue_and_client

        await queue.enqueue_worker_task(task_type="reindex_task_list", payload={})

        assert "dispatch_deadline" not in _task_from(client)

    async def test_coexists_with_a_scheduled_delay(self, queue_and_client):
        """delay_seconds sets schedule_time; the two must not clobber each other."""
        queue, client = queue_and_client

        await queue.enqueue_worker_task(
            task_type="execute_reminder", payload={}, delay_seconds=30,
            deadline_seconds=1620,
        )

        task = _task_from(client)
        assert task["dispatch_deadline"].seconds == 1620
        assert "schedule_time" in task

    async def test_payload_still_carries_task_type(self, queue_and_client):
        import json

        queue, client = queue_and_client

        await queue.enqueue_worker_task(
            task_type="daily_email_review", payload={"user_id": "u1"},
            deadline_seconds=1620,
        )

        body = json.loads(_task_from(client)["http_request"]["body"])
        assert body == {"task_type": "daily_email_review", "user_id": "u1"}
