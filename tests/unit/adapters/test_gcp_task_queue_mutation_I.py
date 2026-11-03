"""Mutation-survivor kill: a failing queue purge reaches the caller instead of being swallowed."""
import pytest

from src.adapters import gcp_task_queue
from src.adapters.gcp_task_queue import GcpTaskQueue
from tests.support.cloud_tasks_fake import FakeCloudTasksClient


async def test_purge_queue_failure_is_raised_to_the_caller(monkeypatch):
    def _boom(self, name="", **kwargs):
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(FakeCloudTasksClient, "purge_queue", _boom)
    monkeypatch.setattr(gcp_task_queue.tasks_v2, "CloudTasksClient", FakeCloudTasksClient)
    queue = GcpTaskQueue("proj", "us-central1", "q", "https://svc.example")

    with pytest.raises(RuntimeError, match="service unavailable"):
        await queue.purge_queue()
