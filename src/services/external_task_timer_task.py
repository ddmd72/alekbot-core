"""The `external_task_timer` `/worker` task: one lease, deadline or hold timer of the external task queue
(EXTERNAL_TASK_QUEUE_RFC §5.8). Cloud Tasks delivers it at the instant the service asked for; the service's
`on_timer` applies the kind's precondition, so a stale timer changes nothing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Tuple

from ..domain.external_task_jobs import TimerKind, TimerPayload
from ..utils.logger import logger

if TYPE_CHECKING:
    from .external_task_service import ExternalTaskService


class ExternalTaskTimerTask:
    """Runs one timer; a `WorkerTask`."""

    def __init__(self, *, external_tasks: "ExternalTaskService") -> None:
        self._tasks = external_tasks

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """200 `{"changed": bool}`; a body that is not a timer payload is 400 (Cloud Tasks does not retry a 4xx into
        success, and a retry could not fix the body). Every key is read literally here; the payload walker of the
        `/worker` contract tests reads these subscripts."""
        try:
            timer = TimerPayload(
                user_id=str(payload["user_id"]), account_id=str(payload["account_id"]),
                task_id=str(payload["task_id"]), kind=TimerKind(payload["kind"]),
                generation=int(payload.get("generation", 0)), renewals=int(payload.get("renewals", 0)))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(f"[TaskQueue] timer payload refused: {type(exc).__name__}")
            return {"error": "not a timer payload"}, 400
        changed = await self._tasks.on_timer(timer)
        return {"changed": changed}, 200
