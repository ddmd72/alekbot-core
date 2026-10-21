"""``enqueue_agent_task(dedup_key=)`` names the task, and an AlreadyExists create counts as success (S1b task W)."""
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.gcp_task_queue import GcpTaskQueue


@pytest.fixture
def queue_and_client():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as cls:
        client = cls.return_value
        client.queue_path.return_value = "projects/p/locations/l/queues/q"
        client.task_path.side_effect = lambda p, loc, q, tid: f"projects/{p}/locations/{loc}/queues/{q}/tasks/{tid}"
        created = MagicMock()
        created.name = "created"
        client.create_task.return_value = created
        yield GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://x.invalid"), client


def _task(client):
    return client.create_task.call_args.kwargs["request"]["task"]


async def test_dedup_key_names_the_task(queue_and_client):
    queue, client = queue_and_client
    await queue.enqueue_agent_task("a", "i", "q", {}, dedup_key="deep_research_report:r1")
    assert _task(client)["name"].split("/tasks/")[1].startswith("agent_execution-")


async def test_same_key_same_name_other_key_other_name(queue_and_client):
    queue, client = queue_and_client
    names = []
    for key in ("k1", "k1", "k2"):
        await queue.enqueue_agent_task("a", "i", "q", {}, dedup_key=key)
        names.append(_task(client)["name"])
    assert names[0] == names[1] != names[2]


async def test_no_key_leaves_the_task_unnamed(queue_and_client):
    queue, client = queue_and_client
    await queue.enqueue_agent_task("a", "i", "q", {})
    assert "name" not in _task(client)


async def test_already_exists_is_success_with_the_existing_name(queue_and_client):
    queue, client = queue_and_client
    client.create_task.side_effect = AlreadyExists("dup")
    name = await queue.enqueue_agent_task("a", "i", "q", {}, dedup_key="k")
    assert name == _task(client)["name"]


async def test_same_name_create_after_a_deadline_that_created_the_task_succeeds(queue_and_client):
    from google.api_core.exceptions import DeadlineExceeded
    queue, client = queue_and_client
    client.create_task.side_effect = [DeadlineExceeded("late"), AlreadyExists("it was created")]
    with pytest.raises(DeadlineExceeded):
        await queue.enqueue_agent_task("a", "i", "q", {}, dedup_key="deep_research_report:j1")
    name = await queue.enqueue_agent_task("a", "i", "q", {}, dedup_key="deep_research_report:j1")
    names = [c.kwargs["request"]["task"]["name"] for c in client.create_task.call_args_list]
    assert names[0] == names[1] == name
