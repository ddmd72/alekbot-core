"""``enqueue_worker_task`` must translate ``dedup_key`` into a namespaced Cloud Tasks
task name and treat Cloud Tasks' ``AlreadyExists`` rejection as a silent no-op.

All GCP-specific knowledge (legal task-name characters, ``task_path`` construction,
``AlreadyExists`` handling) lives inside this adapter — callers only ever pass a raw
``dedup_key`` (e.g. a session_id) and never construct or reason about a task name.
"""
import json
import pytest
from unittest.mock import MagicMock, patch
from google.api_core.exceptions import AlreadyExists

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


class TestDedupKey:
    async def test_dedup_key_derives_namespaced_task_name(self, queue_and_client):
        queue, client = queue_and_client
        client.task_path.return_value = (
            "projects/test-project/locations/us-central1/queues/test-queue/"
            "tasks/smart_timeout_retry-abc123"
        )

        await queue.enqueue_worker_task(
            task_type="smart_timeout_retry", payload={"user_id": "u1"},
            dedup_key="user-1:C0123456",
        )

        # namespaced by task_type so different task types never collide on the same key
        task_id_arg = client.task_path.call_args.args[3]
        assert task_id_arg.startswith("smart_timeout_retry-")
        assert _task_from(client)["name"] == client.task_path.return_value

    async def test_same_dedup_key_produces_same_task_name(self, queue_and_client):
        queue, client = queue_and_client

        await queue.enqueue_worker_task(task_type="t", payload={}, dedup_key="session-A")
        first_call_id = client.task_path.call_args.args[3]
        await queue.enqueue_worker_task(task_type="t", payload={}, dedup_key="session-A")
        second_call_id = client.task_path.call_args.args[3]

        assert first_call_id == second_call_id

    async def test_different_dedup_keys_produce_different_task_names(self, queue_and_client):
        queue, client = queue_and_client

        await queue.enqueue_worker_task(task_type="t", payload={}, dedup_key="session-A")
        first_call_id = client.task_path.call_args.args[3]
        await queue.enqueue_worker_task(task_type="t", payload={}, dedup_key="session-B")
        second_call_id = client.task_path.call_args.args[3]

        assert first_call_id != second_call_id

    async def test_same_key_different_task_types_do_not_collide(self, queue_and_client):
        """Namespacing by task_type: same dedup_key, different task_type -> different name."""
        queue, client = queue_and_client

        await queue.enqueue_worker_task(task_type="smart_timeout_retry", payload={}, dedup_key="same-key")
        first_call_id = client.task_path.call_args.args[3]
        await queue.enqueue_worker_task(task_type="other_task_type", payload={}, dedup_key="same-key")
        second_call_id = client.task_path.call_args.args[3]

        assert first_call_id != second_call_id
        assert first_call_id.startswith("smart_timeout_retry-")
        assert second_call_id.startswith("other_task_type-")

    async def test_duplicate_dedup_key_is_silent_noop_not_an_error(self, queue_and_client):
        queue, client = queue_and_client
        client.create_task.side_effect = AlreadyExists("duplicate")

        result = await queue.enqueue_worker_task(
            task_type="smart_timeout_retry", payload={"user_id": "u1"},
            dedup_key="user-1:C0123456",
        )

        assert result is None  # explicit no-op signal, not an exception

    async def test_without_dedup_key_behavior_unchanged(self, queue_and_client):
        """No dedup_key -> no "name" key on the task dict at all (today's exact behavior)."""
        queue, client = queue_and_client

        await queue.enqueue_worker_task(task_type="consolidation", payload={"user_id": "u1"})

        assert "name" not in _task_from(client)
        client.task_path.assert_not_called()

    async def test_without_dedup_key_returns_task_name(self, queue_and_client):
        queue, client = queue_and_client

        result = await queue.enqueue_worker_task(task_type="consolidation", payload={"user_id": "u1"})

        assert result == "t-1"

    async def test_dedup_key_payload_still_carries_task_type(self, queue_and_client):
        queue, client = queue_and_client

        await queue.enqueue_worker_task(
            task_type="smart_timeout_retry", payload={"user_id": "u1"},
            dedup_key="user-1:C0123456",
        )

        body = json.loads(_task_from(client)["http_request"]["body"])
        assert body == {"task_type": "smart_timeout_retry", "user_id": "u1"}


# ---------------------------------------------------------------------------
# enqueue_video_generation_polling
# ---------------------------------------------------------------------------

class TestVideoGenerationPolling:

    async def test_enqueue_video_generation_polling_creates_task(self, queue_and_client):
        """Test basic enqueue with explicit duration_s."""
        queue, client = queue_and_client

        result = await queue.enqueue_video_generation_polling(
            request_id="req-abc",
            user_id="user1",
            account_id="acc1",
            session_id="user1:C123",
            duration_s=8,
            attempt=0,
            delay_seconds=30,
        )

        client.create_task.assert_called_once()
        call_kwargs = client.create_task.call_args.kwargs
        task = call_kwargs["request"]["task"]
        assert "schedule_time" in task  # delay_seconds > 0
        body = json.loads(task["http_request"]["body"])
        assert body["task_type"] == "video_generation_polling"
        assert body["request_id"] == "req-abc"
        assert body["user_id"] == "user1"
        assert body["account_id"] == "acc1"
        assert body["session_id"] == "user1:C123"
        assert body["duration_s"] == 8
        assert body["attempt"] == 0
        assert result == "t-1"

    async def test_enqueue_video_generation_polling_uses_default_duration_s(self, queue_and_client):
        """Test that duration_s defaults to 5 when not provided."""
        queue, client = queue_and_client

        result = await queue.enqueue_video_generation_polling(
            request_id="req-xyz",
            user_id="user2",
            account_id="acc2",
            session_id="user2:C456",
        )

        body = json.loads(_task_from(client)["http_request"]["body"])
        assert body["duration_s"] == 5
        assert body["attempt"] == 0
        assert body["task_type"] == "video_generation_polling"
        assert result == "t-1"

    async def test_enqueue_video_generation_polling_no_delay_when_zero(self, queue_and_client):
        """Test that schedule_time is not set when delay_seconds=0."""
        queue, client = queue_and_client

        await queue.enqueue_video_generation_polling(
            request_id="req-123",
            user_id="user3",
            account_id="acc3",
            delay_seconds=0,
        )

        task = _task_from(client)
        assert "schedule_time" not in task
