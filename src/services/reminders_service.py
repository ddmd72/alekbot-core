"""
Reminders Service
=================

Owns the lifecycle of proactive self-reminders: firing due reminders,
rescheduling recurrent ones, and deleting one-time ones after firing.

Extracted from WorkerHandler so that the handler delegates through a
service rather than calling AgentNotePort methods directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from ..domain.agent_note import AgentNote, ReminderRecurrence
from ..ports.agent_note_port import AgentNotePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..composition.user_agent_factory import UserAgentFactory
    from ..services.user_notification_service import UserNotificationService


class RemindersService:
    """
    Fires due reminders and manages their lifecycle (reschedule / delete).

    Called by WorkerHandler when task_type == "fire_due_reminders".
    Cloud Scheduler triggers every 15 minutes.
    """

    _CRON_WINDOW_SECONDS = 4 * 60  # 4 min — idempotency guard for 5-min cron overlap

    def __init__(
        self,
        notes_port: AgentNotePort,
        user_repo: Any,
        notification_service: UserNotificationService,
        agent_factory: UserAgentFactory,
    ) -> None:
        self._notes_port = notes_port
        self._user_repo = user_repo
        self._notification = notification_service
        self._agent_factory = agent_factory

    async def fire_due_reminders(
        self, now_utc: Optional[datetime] = None
    ) -> Tuple[dict, int]:
        """
        Fire all reminders with due <= now.

        For each due note:
          1. Resolve user account_id (skip if user not found).
          2. Idempotency: skip if already fired within the current cron window.
          3. Fire: notify() sends instruction to SmartAgent → delivers to user's channel.
          4. Reschedule (recurrent) or delete (one-time).
        """
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        due_notes = await self._notes_port.list_due_reminders(as_of=now)
        logger.info(
            "[Reminders] fire_due_reminders: %d due note(s) at %s",
            len(due_notes), now.isoformat(),
        )

        fired, skipped = 0, 0
        for note in due_notes:
            # Idempotency: skip if fired recently
            if note.last_fired and (now - note.last_fired).total_seconds() < self._CRON_WINDOW_SECONDS:
                skipped += 1
                continue

            user_profile = await self._user_repo.get_user(note.user_id)
            if not user_profile or not user_profile.account_id:
                logger.warning(
                    "[Reminders] fire_due_reminders: user not found or no account_id: %s",
                    note.user_id[:8],
                )
                skipped += 1
                continue

            account_id = user_profile.account_id
            user_tz = ZoneInfo(user_profile.config.timezone or "UTC")

            try:
                await self._agent_factory.ensure_agents_for_user(note.user_id)
                await self._notification.notify(
                    user_id=note.user_id,
                    account_id=account_id,
                    system_alert=_build_reminder_alert(note),
                    agent_id_override=f"smart_response_agent_{note.user_id}",
                )
            except Exception as exc:
                logger.warning(
                    "[Reminders] fire_due_reminders: notify failed for user=%s note=%s: %s",
                    note.user_id[:8], note.note_id, exc,
                )
                # Still reschedule/delete — notification failure is not a reason to skip.

            if note.recurrence:
                next_due = _compute_next_due(note.due, note.recurrence, user_tz)
                await self._notes_port.reschedule(note.note_id, next_due, last_fired=now)
                logger.info(
                    "[Reminders] Rescheduled reminder %s → %s (user=%s)",
                    note.note_id, next_due.isoformat(), note.user_id[:8],
                )
            else:
                await self._notes_port.delete_note(note.note_id, note.user_id)
                logger.info(
                    "[Reminders] Deleted one-time reminder %s (user=%s)",
                    note.note_id, note.user_id[:8],
                )

            fired += 1

        logger.info("[Reminders] fire_due_reminders complete: fired=%d, skipped=%d", fired, skipped)
        return {"fired": fired, "skipped": skipped}, 200


# ---------------------------------------------------------------------------
# Utility: build system alert text for fired reminders
# ---------------------------------------------------------------------------

def _build_reminder_alert(note: AgentNote) -> str:
    if note.recurrence:
        interval = note.recurrence.interval or 1
        schedule = (
            f"this reminder recurs every {interval} {note.recurrence.type} "
            f"— it will fire again on the next cycle."
        )
    else:
        schedule = "this was a one-time reminder, it fires once."

    return (
        f'[SELF-REMINDER] "{note.text}"\n'
        f"note_id: {note.note_id}\n"
        f"This reminder was set by you for yourself at an earlier point.\n"
        f"The instruction below is your own prior intent — you wrote it then so you could act on it now.\n"
        f"Schedule: {schedule}\n"
        f"\n"
        f"{note.instruction}\n"
        f"\n"
        f"---\n"
        f"You have full context on why you set this — the instruction above is yours.\n"
        f"The manage_self_reminders intent is available if you decide to update or delete it.\n"
        f"Having received this execution context, you have an opportunity to act in the user's interest — "
        f"not only on the reminder itself, but on anything you judge valuable right now.\n"
        f"To decide what is worth doing right now, start with conversation history — "
        f"it is the primary signal: patterns, unfinished threads, recurring topics. "
        f"From there you have access to: user memory (search_memory), "
        f"active reminders (manage_self_reminders), user tasks (manage_user_tasks), "
        f"web (search_web), email archive (search_emails)."
    )


# ---------------------------------------------------------------------------
# Utility: compute next due datetime for recurrent reminders
# ---------------------------------------------------------------------------

def _compute_next_due(
    current_due: datetime,
    recurrence: ReminderRecurrence,
    user_tz: ZoneInfo,
) -> datetime:
    """
    Compute next UTC due datetime after firing.

    - hourly: pure UTC arithmetic (DST-safe by definition)
    - daily / weekly / monthly: arithmetic in user timezone to preserve wall-clock time
      (e.g. "every day at 9am" stays at 9am local even across DST transitions)
    """
    interval = recurrence.interval or 1

    if recurrence.type == "hourly":
        return current_due + timedelta(hours=interval)

    local_due = current_due.astimezone(user_tz)

    if recurrence.type == "daily":
        next_local = local_due + timedelta(days=interval)
    elif recurrence.type == "weekly":
        next_local = local_due + timedelta(weeks=interval)
    elif recurrence.type == "monthly":
        next_local = local_due + relativedelta(months=interval)
    else:
        logger.warning("[compute_next_due] Unknown recurrence type %r, defaulting to daily", recurrence.type)
        next_local = local_due + timedelta(days=interval)

    return next_local.astimezone(timezone.utc)
