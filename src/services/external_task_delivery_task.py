"""The `external_task_deliver` `/worker` task: a final external task reaches the bot as a system alert
(EXTERNAL_TASK_QUEUE_RFC §5.9, §6.1 "Worker -> bot", §7; a system alert since 2026-11-24).

Delivery is the asynchronous end of a task the bot handed out, so it comes back to the bot the way a fired reminder
does: through `UserNotificationService.notify`, as the user turn of a background Smart run in the task's origin channel,
and Smart tells the owner. First, a done task's result is written to the workspace as an ordinary file under a
code-written header; a task the worker failed with a reason gets that reason written the same way. The alert names the
file and carries only code-made values (`domain/external_task_alert.py`). The run's origin is
`background:external_task_deliver`, which is neither a chat turn nor a reminder fire, so Smart offers no queue tool in
it: a worker's result never starts new work.

The task is idempotent through `delivery.state`: Cloud Tasks may deliver it twice. Every outcome of `notify` is final
and recorded, as for a reminder fire: once the turn started it may have acted, and re-running it would repeat that; a
missing channel is logged (the result stays readable in the task record). A crash between the turn and the record
reruns the turn (the reminder's accepted residual).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple

from ..domain.exceptions import WorkspaceRefused
from ..domain.external_task import (
    DeliveryState, ExternalTask, ExternalTaskStatus, TaskDelivery, TaskFailureCode, WorkerDestination,
)
from ..domain.external_task_alert import alert_summary, choose_alert, render_alert
from ..domain.external_task_delivery import copy_text, external_task_origin, failure_text, result_path
from ..domain.external_task_jobs import TaskNotice
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import NotifyResult, is_empty_answer, is_retryable_failure, is_turn_cap
from ..domain.workspace import BackgroundWrite
from ..utils.logger import logger

if TYPE_CHECKING:
    from .background_empty_answer_reporter import BackgroundEmptyAnswerReporter
    from .external_task_service import ExternalTaskService
    from .user_notification_service import UserNotificationService
    from .workspace_service import WorkspaceService

# The `notify` origin of the alert turn: its context carries run_origin "background:external_task_deliver".
DELIVER_ORIGIN_DETAIL = "external_task_deliver"


class ExternalTaskDeliveryTask:
    """Delivers one final task; a `WorkerTask`. `workspace` None: no file is ever written (the alert says so)."""

    def __init__(self, *, external_tasks: "ExternalTaskService", workspace: Optional["WorkspaceService"],
                 notification_service: "UserNotificationService", ensure_agents: Callable[[str], Awaitable[Any]],
                 empty_answer_reporter: "BackgroundEmptyAnswerReporter",
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._tasks = external_tasks
        self._workspace = workspace
        self._notification = notification_service
        self._ensure_agents = ensure_agents
        self._empty_answer_reporter = empty_answer_reporter
        self._clock = clock

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """Outcomes: 400 a body that is not a delivery payload; `gone` no such final task (200); `already` delivered
        before (200); `no_notice` a task that gets no alert (a cancelled one, 200); `delivered` (200) whatever the
        alert turn did, logged. A workspace failure that is not a refusal raises, so Cloud Tasks retries; a retry reuses
        the file an earlier attempt wrote."""
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
        if task.status is ExternalTaskStatus.CANCELLED:
            return {"status": "no_notice"}, 200
        path = await self._write_file(task, load.result)
        kind = choose_alert(task, path=path)
        await self._ensure_agents(user_id)
        result = await self._notification.notify(
            user_id, account_id, render_alert(kind, task, path),
            system_alert_summary=alert_summary(kind, task, path),
            kind=NotificationKind.EXTERNAL_TASK,
            agent_id_override=f"smart_response_agent_{user_id}",
            channel_id_override=task.origin.channel_id, platform_override=task.origin.platform,
            # A whole-execution retry re-runs every tool call of the turn; `_call_llm`'s per-call retry covers a blip.
            suppress_transient_retry=True,
            origin_detail=DELIVER_ORIGIN_DETAIL,
        )
        outcome = await self._outcome(result, user_id, task_id)
        delivery = TaskDelivery(
            state=DeliveryState.DELIVERED, destination=WorkerDestination.WORKSPACE if path else None, path=path,
            delivered_at=self._clock())
        await self._tasks.record_delivery(user_id, task_id, delivery)
        logger.info(f"[TaskQueue] delivered user={user_id[:8]} task={task_id} alert={kind.value} outcome={outcome} "
                    f"file={path is not None}")
        return {"status": "delivered"}, 200

    async def _outcome(self, result: NotifyResult, user_id: str, task_id: str) -> str:
        """Name and log what the alert turn did; none of it is retried."""
        if result.delivered:
            return "ok"
        if is_retryable_failure(result):
            logger.error(f"[TaskQueue] alert of task={task_id} not posted: {result.error or 'no channel'}; "
                         f"the result stays readable in the task record")
            return "no_channel"
        if is_empty_answer(result):
            await self._empty_answer_reporter.report(DELIVER_ORIGIN_DETAIL, user_id, task_id)
            return "empty_answer_final"
        if is_turn_cap(result):
            return "turn_cap_final"
        logger.error(f"[TaskQueue] alert turn of task={task_id} failed (agent_status={result.agent_status}, "
                     f"error={result.error}, sent={result.sent}); the turn started, not retried")
        return "failed_final"

    async def _write_file(self, task: ExternalTask, result: Optional[str]) -> Optional[str]:
        """The workspace path of the file the alert names, None when there is none: a done task's result, or the reason
        a worker gave when it failed the task. A refusal (caps, size) or no workspace is no loss: the text stays in the
        task record."""
        workspace = self._workspace
        if workspace is None:
            return None
        if task.status is ExternalTaskStatus.DONE and result is not None and task.result_meta is not None:
            submitted_at = task.result_meta.submitted_at
            return await self._write(workspace, task, copy_text(task, result, submitted_at), submitted_at)
        failure = task.failure
        if (task.status is ExternalTaskStatus.FAILED and failure is not None
                and failure.code is TaskFailureCode.WORKER_FAILED and failure.reason):
            return await self._write(workspace, task, failure_text(task, failure.reason), task.updated_at)
        return None

    @staticmethod
    async def _write(workspace: "WorkspaceService", task: ExternalTask, text: str, at: datetime) -> Optional[str]:
        """The short name first; the full id when a file of another provenance holds it; a file with this task's origin
        is an earlier attempt and is reused."""
        for full_id in (False, True):
            path = result_path(task.task_id, at, full_id=full_id)
            try:
                outcome = await workspace.write_background(
                    task.user_id, path, text, origin=external_task_origin(task.task_id))
            except WorkspaceRefused as exc:
                logger.warning(f"[TaskQueue] file of task={task.task_id} refused reason={exc.reason}")
                return None
            if outcome is not BackgroundWrite.TAKEN:
                return path
        return None
