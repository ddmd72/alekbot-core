"""The real `GcpTaskQueue` over a Cloud Tasks client that refuses a task name it has already seen.

`FakeCloudTasksClient` (cloud_tasks_fake.py) records every request and never refuses a name; Cloud Tasks answers a second
task of one name with ALREADY_EXISTS, which `GcpTaskQueue.enqueue_worker_task` turns into `None`. The subclass here adds
that one refusal and nothing else, so a service that names its tasks (`dedup_key`) meets the real adapter's naming and
dedup path in a test.
"""
from typing import Any, Dict, List, Set, Tuple
from unittest import mock

from google.api_core.exceptions import AlreadyExists

from src.adapters.gcp_task_queue import GcpTaskQueue
from tests.support.cloud_tasks_fake import FakeCloudTasksClient

SERVICE_URL = "https://svc.test.example"


class DedupCloudTasksClient(FakeCloudTasksClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        FakeCloudTasksClient.instances.remove(self)     # the graph tests count the clients they built; this one is not theirs
        self.names: Set[str] = set()

    def create_task(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        name = request["task"].get("name")
        if name and name in self.names:
            raise AlreadyExists(f"task {name} already exists")
        if name:
            self.names.add(name)
        return super().create_task(request, **kwargs)

    def worker_tasks(self) -> List[Tuple[str, Dict[str, Any]]]:
        """(task_type, decoded body) of every created worker task, in creation order."""
        return [(body["task_type"], body) for body in self.created_bodies() if "task_type" in body]


def make_task_queue() -> Tuple[GcpTaskQueue, DedupCloudTasksClient]:
    """A `GcpTaskQueue` whose client is a `DedupCloudTasksClient`, and that client."""
    with mock.patch("google.cloud.tasks_v2.CloudTasksClient", DedupCloudTasksClient):
        queue = GcpTaskQueue("p", "us-central1", "q", SERVICE_URL)
    client = queue.client
    assert isinstance(client, DedupCloudTasksClient)
    return queue, client
