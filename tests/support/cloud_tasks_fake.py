"""A recording stand-in for `google.cloud.tasks_v2.CloudTasksClient`, patched in where `GcpTaskQueue` reads it.

The queue adapter stays real; only the SDK client is replaced. It records the queue calls and every `create_task`
request with its decoded JSON body, and a queue that was never created answers `get_queue` like the service does
(`NotFound`), so `create_queue_if_not_exists` takes its real path.
"""
import json
from types import SimpleNamespace
from typing import Any, Dict, List

from google.api_core.exceptions import NotFound


class FakeCloudTasksClient:
    """One instance per `GcpTaskQueue`; `instances` lists them in construction order."""

    instances: List["FakeCloudTasksClient"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.created_queues: List[str] = []
        self.get_queue_calls: List[str] = []
        self.create_task_requests: List[Dict[str, Any]] = []
        FakeCloudTasksClient.instances.append(self)

    @staticmethod
    def queue_path(project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    @staticmethod
    def task_path(project: str, location: str, queue: str, task: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task}"

    @staticmethod
    def common_location_path(project: str, location: str) -> str:
        return f"projects/{project}/locations/{location}"

    def get_queue(self, name: str = "", **kwargs: Any) -> Any:
        self.get_queue_calls.append(name)
        if name not in self.created_queues:
            raise NotFound(f"queue {name} not found")
        return SimpleNamespace(name=name)

    def create_queue(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        self.created_queues.append(request["queue"]["name"])
        return SimpleNamespace(name=request["queue"]["name"])

    def create_task(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        self.create_task_requests.append(request)
        task = request["task"]
        return SimpleNamespace(name=task.get("name") or f"{request['parent']}/tasks/fake-{len(self.create_task_requests)}")

    def purge_queue(self, name: str = "", **kwargs: Any) -> Any:
        return SimpleNamespace(name=name)

    def created_bodies(self) -> List[Dict[str, Any]]:
        """The decoded JSON body of every task created through this client."""
        return [json.loads(r["task"]["http_request"]["body"]) for r in self.create_task_requests]

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
