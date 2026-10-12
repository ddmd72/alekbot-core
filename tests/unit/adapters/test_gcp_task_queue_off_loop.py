"""Every enqueue runs the blocking Cloud Tasks `create_task` call off the event loop (`asyncio.to_thread`). The
client is synchronous gRPC with a 20 s default timeout; on the loop it froze every request of the 1-vCPU service,
and the bot's drive writes enqueue a nudge each (USER_DRIVE_INDEXING_RFC §7.8)."""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.gcp_task_queue import GcpTaskQueue

_CALLS = {
    "enqueue_slack_event": lambda q: q.enqueue_slack_event({"type": "message"}, "s"),
    "enqueue_agent_task": lambda q: q.enqueue_agent_task("a", "i", "q", {}),
    "enqueue_deep_research_polling": lambda q: q.enqueue_deep_research_polling("i", "u", "a"),
    "enqueue_video_generation_polling": lambda q: q.enqueue_video_generation_polling("r", "u", "a"),
    "enqueue_email_indexing_task": lambda q: q.enqueue_email_indexing_task("job"),
    "enqueue_worker_task": lambda q: q.enqueue_worker_task("drive_sync", {"user_id": "u"}, dedup_key="k"),
    "enqueue_consolidation_task": lambda q: q.enqueue_consolidation_task("u"),
    "enqueue_companion_consolidation_task": lambda q: q.enqueue_companion_consolidation_task("s"),
}


@pytest.fixture
def queue_and_client():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as mock_client_class:
        client = mock_client_class.return_value
        client.queue_path.return_value = "projects/p/locations/l/queues/q"
        client.task_path.return_value = "projects/p/locations/l/queues/q/tasks/t"
        created = MagicMock()
        created.name = "t-1"
        client.create_task.return_value = created
        yield GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://example.invalid"), client


@pytest.mark.parametrize("method", sorted(_CALLS))
async def test_create_task_runs_off_the_event_loop_thread(queue_and_client, method):
    queue, client = queue_and_client
    threads = []
    client.create_task.side_effect = lambda **_: threads.append(threading.get_ident()) or client.create_task.return_value
    await _CALLS[method](queue)
    assert threads and threads[0] != threading.get_ident()


async def test_the_loop_keeps_running_while_cloud_tasks_answers_slowly(queue_and_client):
    queue, client = queue_and_client

    def slow_create(**_):
        time.sleep(0.3)
        return client.create_task.return_value

    client.create_task.side_effect = slow_create
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    await queue.enqueue_worker_task("drive_sync", {"user_id": "u"})
    task.cancel()
    assert ticks >= 10


async def test_a_reserved_name_is_still_a_silent_no_op(queue_and_client):
    queue, client = queue_and_client
    client.create_task.side_effect = AlreadyExists("duplicate")
    assert await queue.enqueue_worker_task("drive_sync", {"user_id": "u"}, dedup_key="k") is None
