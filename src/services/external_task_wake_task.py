"""The `external_task_wake` `/worker` task: one content-free ping to a worker's wake-up target
(EXTERNAL_TASK_QUEUE_RFC §5.8, §7).

The service's `on_wake` re-reads the worker and pings through the outbound-request guard; this runner turns its outcome
into the answer Cloud Tasks acts on:
- `skipped` / `sent`: 200;
- `retry` (the target is down or slow): 503, so Cloud Tasks retries with the queue's backoff, until this task's own
  bound `WAKE_MAX_ATTEMPTS` (counted from `X-CloudTasks-TaskRetryCount`): the last attempt answers 200 `gave_up`. The
  task stays queued for polling or the next ping either way;
- `refused` (the guard refused the target, or it answered 3xx/4xx): final, 200, logged once, and one literal notice to
  the origin channel of the worker's oldest queued task naming the worker's label and the closed code. No agent turn,
  no history line: the notice carries no task text, and nothing of the target's answer.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Tuple

from ..domain.cloud_task_delivery import RETRY_COUNT_HEADER
from ..domain.external_task import WAKE_MAX_ATTEMPTS
from ..domain.external_task_jobs import WakeOutcome, WakePayload, WakeRun
from ..domain.secret_digest import fingerprint
from ..domain.ui_messages import UIMessage
from ..utils.logger import logger

if TYPE_CHECKING:
    from .external_task_service import ExternalTaskService
    from .user_notification_service import UserNotificationService


def _retry_count(headers: Mapping[str, Any]) -> int:
    """Cloud Tasks' retry count of this delivery (0 on the first, and outside Cloud Tasks); header case varies."""
    wanted = RETRY_COUNT_HEADER.lower()
    for name, value in headers.items():
        if str(name).lower() == wanted:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


class ExternalTaskWakeTask:
    """Runs one wake-up; a `WorkerTask`."""

    def __init__(self, *, external_tasks: "ExternalTaskService",
                 notification_service: "UserNotificationService") -> None:
        self._tasks = external_tasks
        self._notification = notification_service

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """400 on a body that is not a wake payload (a retry could not fix it). Every key is read literally here; the
        payload walker of the `/worker` contract tests reads these subscripts."""
        try:
            wake = WakePayload(user_id=str(payload["user_id"]), account_id=str(payload["account_id"]),
                               worker_id=str(payload["worker_id"]), generation=int(payload["generation"]))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(f"[TaskQueue] wake payload refused: {type(exc).__name__}")
            return {"error": "not a wake payload"}, 400
        run = await self._tasks.on_wake(wake)
        if run.outcome is WakeOutcome.RETRY:
            attempt = _retry_count(headers) + 1
            if attempt >= WAKE_MAX_ATTEMPTS:
                logger.warning(f"[TaskQueue] wake gave up worker={fingerprint(wake.worker_id)} attempts={attempt} "
                               f"code={run.refusal.value if run.refusal else '-'}")
                return {"status": "gave_up"}, 200
            return {"status": "retry"}, 503
        if run.outcome is WakeOutcome.REFUSED:
            code = run.refusal.value if run.refusal else "-"
            logger.warning(f"[TaskQueue] wake refused worker={fingerprint(wake.worker_id)} code={code}; not retried")
            await self._post_refusal(wake, run)
            return {"status": "refused", "code": code}, 200
        return {"status": run.outcome.value}, 200

    async def _post_refusal(self, wake: WakePayload, run: WakeRun) -> None:
        """The owner's notice; a missing channel or localization is logged, never retried (the refusal is final)."""
        if run.origin is None or run.refusal is None or run.label is None:
            return
        strings = await self._notification.ui_strings(wake.user_id)
        if strings is None:
            logger.error(f"[TaskQueue] no localization wired: wake refusal of worker={fingerprint(wake.worker_id)} "
                         "not posted")
            return
        text = strings(UIMessage.EXTERNAL_TASK_WAKE_REFUSED).format(label=run.label, code=run.refusal.value)
        posted = await self._notification.notify_raw(
            wake.user_id, wake.account_id, text, channel_id_override=run.origin.channel_id,
            platform_override=run.origin.platform, literal=True)
        if not posted:
            logger.error(f"[TaskQueue] wake refusal of worker={fingerprint(wake.worker_id)} not posted: no channel")
