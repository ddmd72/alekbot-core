"""The `execute_reminder` `/worker` task: run one claimed reminder fire (Step #8 of NOTIFICATION_DELIVERY_REFACTOR_RFC).

`RemindersService.fire_due_reminders` claims each due note and enqueues one task per fire; `WorkerHandler` dispatches
it here through its task registry (built by `composition/worker_setup.py`).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Tuple

from ..domain.complexity_settings import resolve_complexity_settings
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import is_empty_answer
from ..domain.reminder_alert import build_reminder_alert, build_reminder_alert_summary
from ..domain.workspace import fire_key as workspace_fire_key
from ..ports.agent_note_port import AgentNotePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .background_empty_answer_reporter import BackgroundEmptyAnswerReporter
    from .user_notification_service import UserNotificationService


class ExecuteReminderTask:
    """Delivers one reminder fire through `notify()` and stamps it delivered; a `WorkerTask`."""

    def __init__(
        self,
        *,
        notes_port: AgentNotePort,
        user_repo: UserRepository,
        ensure_agents: Callable[[str], Awaitable[Any]],
        notification_service: "UserNotificationService",
        empty_answer_reporter: "BackgroundEmptyAnswerReporter",
    ) -> None:
        self._notes_port = notes_port
        self._user_repo = user_repo
        self._ensure_agents = ensure_agents
        self._notification = notification_service
        self._empty_answer_reporter = empty_answer_reporter

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """Run a single claimed reminder fire.

        Payload (placed by ``RemindersService.fire_due_reminders``):
            note_id : str          Firestore note id
            user_id : str          owning user (verified via ownership-scoped get_note)
            due_at  : str (ISO)    fire-time this task is delivering — used as
                                   the idempotency token (compared against
                                   ``last_delivered_due``).

        Outcomes (HTTP status drives Cloud Tasks retry policy — 5xx = retry,
        2xx = ack and stop):
            note_gone           note deleted between cron-tick and this run → 200
            no_user             user repo lost the user → 200
            already_delivered   ``last_delivered_due == due_at`` → 200 (Cloud
                                Tasks retry no-op, prevents duplicate Smart run
                                + duplicate user message)
            delivery_failed     notify() returned ``delivered=False`` → 500
                                (Cloud Tasks queue retries with backoff)
            empty_answer_final  the turn ended with no answer → stamped like ok, 200
            ok                  delivered + ``mark_fire_delivered`` stamped → 200
        """
        note_id = payload.get("note_id")
        user_id = payload.get("user_id")
        due_at_raw = payload.get("due_at")
        if not note_id or not user_id or not due_at_raw:
            return {"error": "missing note_id, user_id, or due_at"}, 400

        try:
            due_at = datetime.fromisoformat(due_at_raw)
        except ValueError:
            logger.warning(
                "[Worker] execute_reminder: bad due_at %r for note=%s",
                due_at_raw, note_id,
            )
            return {"error": "bad due_at"}, 400

        note = await self._notes_port.get_note(user_id=user_id, note_id=note_id)
        if note is None:
            # One-time already delivered-and-deleted by a prior worker run, or
            # a note the user has since removed. Either way: nothing to
            # deliver, ack the task.
            logger.info(
                "[Worker] execute_reminder: note_gone note=%s user=%s",
                note_id, user_id[:8],
            )
            return {"status": "note_gone"}, 200

        # Idempotency guard against Cloud Tasks retries.
        if note.last_delivered_due is not None and note.last_delivered_due == due_at:
            # Clean up a one-time note that was stamped but not yet deleted
            # (prior run crashed between mark_fire_delivered and delete_note).
            if note.recurrence is None:
                await self._notes_port.delete_note(note_id=note_id, user_id=user_id)
            logger.info(
                "[Worker] execute_reminder: already_delivered note=%s due=%s",
                note_id, due_at.isoformat(),
            )
            return {"status": "already_delivered"}, 200

        user_profile = await self._user_repo.get_user(user_id)
        if not user_profile or not getattr(user_profile, "account_id", None):
            logger.warning(
                "[Worker] execute_reminder: no_user user=%s note=%s",
                user_id[:8], note_id,
            )
            return {"status": "no_user"}, 200

        await self._ensure_agents(user_id)

        task_complexity = note.complexity.value if note.complexity else "simple_analytics"
        # The SLA tier comes from the SAME merge TaskExecutionResolver applies (user overrides over the defaults):
        # the raw defaults table set the clock for one tier while the work ran at another. ``tier=None`` = the kind's
        # default budget (notify()); only kinds with ``tier_overrides`` consume it.
        sla_tier = None
        if note.complexity is not None:
            settings = resolve_complexity_settings(note.complexity, user_profile.config)
            if settings is not None:
                sla_tier = settings.tier

        fire = workspace_fire_key(note_id, due_at)
        result = await self._notification.notify(
            user_id=user_id,
            account_id=user_profile.account_id,
            system_alert=build_reminder_alert(note),
            system_alert_summary=build_reminder_alert_summary(note),
            kind=NotificationKind.REMINDER,
            tier=sla_tier,
            agent_id_override=f"smart_response_agent_{user_id}",
            task_complexity=task_complexity,
            # This task returns 5xx on failure → Cloud Tasks retries it.
            # Suppress the agent's in-process retry so the two layers don't multiply.
            suppress_transient_retry=True,
            # Who started the run, for the workspace's origin labels; the same due_at gives the same key on every
            # Cloud Tasks re-run of this fire (AGENT_WORKSPACE_RFC §9, §10 row 1).
            origin_detail="execute_reminder",
            fire_key=fire,
        )

        if is_empty_answer(result):
            # Final (owner decision 2026-10-23): a retry would repeat the SYNC actions the turn took, and a
            # conditional reminder may end empty every time. Take the success path below.
            await self._empty_answer_reporter.report("execute_reminder", user_id, note_id)
        elif not result.delivered:
            logger.warning(
                "[Worker] execute_reminder: delivery_failed note=%s user=%s "
                "(agent_status=%s, error=%s)",
                note_id, user_id[:8], result.agent_status, result.error,
            )
            return {
                "error": result.error or "delivery_failed",
                "agent_status": result.agent_status.value,
            }, 500

        # Stamp idempotency FIRST, then remove a fired one-time note. Order
        # matters: if we crash between the two, a Cloud Tasks retry sees
        # last_delivered_due == due_at and short-circuits (already_delivered)
        # rather than re-delivering.
        await self._notes_port.mark_fire_delivered(note_id=note_id, due_at=due_at)
        if note.recurrence is None:
            # One-time reminder: claimed (not deleted) at fire-time so its
            # content survived for this delivery; remove it now that it fired.
            await self._notes_port.delete_note(note_id=note_id, user_id=user_id)
        logger.info(
            "[Worker] execute_reminder: %s note=%s user=%s due=%s",
            "empty_answer_final" if is_empty_answer(result) else "ok", note_id, user_id[:8], due_at.isoformat(),
        )
        return {"status": "empty_answer_final" if is_empty_answer(result) else "ok"}, 200
