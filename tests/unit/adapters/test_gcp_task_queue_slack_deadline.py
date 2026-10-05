"""``enqueue_slack_event`` must set a 1800s ``dispatch_deadline`` (Cloud Tasks max).

A chat turn may run ~25 min (LONG_RUNNING_TURNS_RFC §5.8); Cloud Tasks' 600s
default would retry a still-running turn, duplicating it.
"""
from unittest.mock import MagicMock, patch

from src.adapters.gcp_task_queue import GcpTaskQueue


async def test_slack_event_task_gets_1800s_deadline():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as cls:
        client = cls.return_value
        client.queue_path.return_value = "q"
        client.create_task.return_value = MagicMock(name="t")
        queue = GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://x")
        await queue.enqueue_slack_event(event_data={"event": {}}, session_id="s")
        task = client.create_task.call_args.kwargs["request"]["task"]
        assert task["dispatch_deadline"].seconds == 1800
