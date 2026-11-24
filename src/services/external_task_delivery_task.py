"""The `external_task_deliver` `/worker` task: the result or the end of one external task reaches the owner
(EXTERNAL_TASK_QUEUE_RFC §5.9, §6.1 "Worker -> bot", §7).

No model reads anything here. A done task's result is written to the workspace as a copy under an outside-content header; one
literal notice is posted to the task's origin channel and a code-written pair is appended to that channel's history;
then the delivery is recorded. A failed or expired task gets its notice the same way and no copy. The task is
idempotent through `delivery.state`: Cloud Tasks may deliver it twice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple

from ..domain.exceptions import WorkspaceRefused
from ..domain.external_task import DeliveryState, ExternalTask, ExternalTaskStatus, TaskDelivery, WorkerDestination
from ..domain.external_task_delivery import (
    NoticeKind, choose_notice, copy_text, external_task_origin, history_pair, render_notice,
    result_path,
)
from ..domain.external_task_jobs import TaskNotice
from ..domain.workspace import BackgroundWrite
from ..utils.logger import logger

if TYPE_CHECKING:
    from .external_task_service import ExternalTaskService
    from .user_notification_service import UserNotificationService
    from .workspace_service import WorkspaceService


class ExternalTaskDeliveryTask:
    """Delivers one final task; a `WorkerTask`. `workspace` None: no copy is ever written (the notice says so)."""

    def __init__(self, *, external_tasks: "ExternalTaskService", workspace: Optional["WorkspaceService"],
                 notification_service: "UserNotificationService",
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._tasks = external_tasks
        self._workspace = workspace
        self._notification = notification_service
        self._clock = clock

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """Outcomes: 400 a body that is not a delivery payload; `gone` no such final task (200); `already` delivered
        before (200); `no_notice` a task that gets none (a cancelled one, 200); `delivered` (200). A workspace or post
        failure that is not a refusal raises, so Cloud Tasks retries; a retry reuses the copy an earlier attempt wrote."""
        try:
            user_id = str(payload["user_id"])
            account_id = str(payload["account_id"])
            task_id = str(payload["task_id"])
            TaskNotice(payload["notice"])
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(f"[TaskQueue] delivery payload refused: {type(exc).__name__}")
            return {"error": "not a delivery payload"}, 400
        load = await self._tasks.load_for_delivery(user_id, task_id)
        if load is None:
            return {"status": "gone"}, 200
        task = load.task
        if task.delivery is not None and task.delivery.state is DeliveryState.DELIVERED:
            return {"status": "already"}, 200
        path = await self._write_copy(task, load.result) if load.result is not None else None
        try:
            kind = choose_notice(task, copied=path is not None)
        except ValueError:
            return {"status": "no_notice"}, 200
        posted = await self._post(task, account_id, kind, path)
        delivery = TaskDelivery(
            state=DeliveryState.DELIVERED, destination=WorkerDestination.WORKSPACE if path else None, path=path,
            delivered_at=self._clock())
        await self._tasks.record_delivery(user_id, task_id, delivery)
        logger.info(f"[TaskQueue] delivered user={user_id[:8]} task={task_id} notice={kind.value} posted={posted} "
                    f"copied={path is not None}")
        return {"status": "delivered"}, 200

    async def _write_copy(self, task: ExternalTask, result: str) -> Optional[str]:
        """The workspace path of the copy, None when there is no copy: a refusal (caps, size) or no workspace is no
        loss, the result stays in the task record. The short name first; the full id when a file of another provenance
        holds it; a file with this task's origin is an earlier attempt and is reused."""
        if self._workspace is None or task.status is not ExternalTaskStatus.DONE or task.result_meta is None:
            return None
        submitted_at = task.result_meta.submitted_at
        text = copy_text(task, result, submitted_at)
        for full_id in (False, True):
            path = result_path(task.task_id, submitted_at, full_id=full_id)
            try:
                outcome = await self._workspace.write_background(
                    task.user_id, path, text, origin=external_task_origin(task.task_id))
            except WorkspaceRefused as exc:
                logger.warning(f"[TaskQueue] result copy refused task={task.task_id} reason={exc.reason}")
                return None
            if outcome is not BackgroundWrite.TAKEN:
                return path
        return None

    async def _post(self, task: ExternalTask, account_id: str, kind: NoticeKind, path: Optional[str]) -> bool:
        strings = await self._notification.ui_strings(task.user_id)
        if strings is None:
            logger.error(f"[TaskQueue] no localization wired: notice of task={task.task_id} not posted")
            return False
        event_line, kept_line = history_pair(kind, task, path)
        posted = await self._notification.notify_external_task(
            task.user_id, account_id, text=render_notice(kind, task, path, strings), event_line=event_line,
            kept_line=kept_line, channel_id_override=task.origin.channel_id, platform_override=task.origin.platform)
        if not posted:
            logger.error(f"[TaskQueue] notice of task={task.task_id} not posted: no channel; "
                         f"the result is readable through list_external_tasks")
        return posted
