"""The shape of one `/worker` task: what the worker's registry maps a task type to.

A `typing.Protocol`, not a port: the worker handler holds the registry, and handlers do not import `ports/`.
"""

from typing import Any, Protocol


class WorkerTask(Protocol):
    """Runs one delivery of its task type; answers the HTTP body and status `/worker` returns."""

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> tuple[dict[str, Any], int]: ...
