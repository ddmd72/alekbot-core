"""``enqueue_companion_consolidation_task`` enqueues a session-keyed companion
extraction task, mirroring ``enqueue_consolidation_task`` but with a shorter
600s dispatch deadline (TutorExtractorAgent's timeout is 5 min, not
ConsolidationAgent's 15 min — no need for the 30-min ceiling).
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gcp_task_queue import GcpTaskQueue


@pytest.fixture
def queue_and_client():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as mock_client_class:
        mock_client = mock_client_class.return_value
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
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


async def test_enqueue_companion_consolidation_task_sets_task_type_and_session_id(queue_and_client):
    queue, client = queue_and_client
    task_name = await queue.enqueue_companion_consolidation_task(session_id="slack:C1")
    assert task_name == "t-1"
    body = json.loads(_task_from(client)["http_request"]["body"])
    assert body == {"task_type": "companion_consolidation", "session_id": "slack:C1"}


async def test_enqueue_companion_consolidation_task_dispatch_deadline_is_600s(queue_and_client):
    queue, client = queue_and_client
    await queue.enqueue_companion_consolidation_task(session_id="slack:C1")
    assert _task_from(client)["dispatch_deadline"].seconds == 600
