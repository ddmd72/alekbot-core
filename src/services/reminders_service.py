"""
Reminders Service
=================

Owns the cron-side lifecycle of proactive self-reminders.

Step #7 of NOTIFICATION_DELIVERY_REFACTOR_RFC: this service no longer
calls ``notify()`` synchronously inside the cron HTTP request. It now:

  1. Lists due notes.
  2. For each note, ATOMICALLY claims this fire-time:
       - recurrent → ``reschedule_if_due_at`` (next_due in user TZ,
         DST-safe)
       - one-time  → ``delete_if_due_at``
     A False return means another concurrent cron tick already owns
     this fire — skip silently.
  3. Enqueues an ``execute_reminder`` Cloud Task with the fire-payload
     ``{note_id, user_id, due_at}``. The actual user-facing delivery
     runs there (Step #8) — out of band of the cron handler.

This removes both defects #2 and #3 from the 2026-04-30 incident:
  - Cron HTTP request returns within seconds (no longer blocks on
    Smart's full SLA budget).
  - The ``due``-precondition makes (note_id, due_at) the natural
    idempotency key — no duplicate fire is possible regardless of
    cron interval, Smart latency, or Cloud Tasks retry.
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
    from .task_dispatch_service import TaskDispatchService


class RemindersService:
    """Cron-side reminder dispatcher.

    Called by ``WorkerHandler`` when ``task_type == "fire_due_reminders"``.
    Returns within seconds: enqueues per-fire Cloud Tasks but does NOT
    wait for them.
    """

    def __init__(
        self,
        notes_port: AgentNotePort,
        user_repo: Any,
        task_dispatch: "TaskDispatchService",
    ) -> None:
        self._notes_port = notes_port
        self._user_repo = user_repo
        self._task_dispatch = task_dispatch

    async def fire_due_reminders(
        self, now_utc: Optional[datetime] = None
    ) -> Tuple[dict, int]:
        """Claim every due fire and enqueue ``execute_reminder`` for it.

        For each due note:
          1. Resolve the user's timezone (skip if user gone).
          2. Recurrent → ``reschedule_if_due_at``; one-time →
             ``delete_if_due_at``. The atomic precondition on ``due``
             is the cross-process at-most-once primitive.
          3. On successful claim, enqueue ``execute_reminder``.
          4. On failed claim (concurrent tick won), skip silently.
        """
        now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        due_notes = await self._notes_port.list_due_reminders(as_of=now)
        logger.info(
            "[Reminders] fire_due_reminders: %d due note(s) at %s",
            len(due_notes), now.isoformat(),
        )

        enqueued, claim_lost, skipped = 0, 0, 0
        for note in due_notes:
            user_profile = await self._user_repo.get_user(note.user_id)
            if not user_profile or not user_profile.account_id:
                logger.warning(
                    "[Reminders] fire_due_reminders: user not found or no account_id: %s",
                    note.user_id[:8],
                )
                skipped += 1
                continue

            user_tz = ZoneInfo(user_profile.config.timezone or "UTC")

            # Step 1 — atomic claim of this fire-time.
            if note.recurrence:
                next_due = _compute_next_due(note.due, note.recurrence, user_tz)
                claimed = await self._notes_port.reschedule_if_due_at(
                    note_id=note.note_id,
                    expected_due=note.due,
                    next_due=next_due,
                    last_fired=now,
                )
                if claimed:
                    logger.info(
                        "[Reminders] Claimed (recurrent) %s: due=%s → %s (user=%s)",
                        note.note_id, note.due.isoformat(), next_due.isoformat(),
                        note.user_id[:8],
                    )
            else:
                claimed = await self._notes_port.delete_if_due_at(
                    note_id=note.note_id,
                    user_id=note.user_id,
                    expected_due=note.due,
                )
                if claimed:
                    logger.info(
                        "[Reminders] Claimed (one-time) %s: deleted (user=%s)",
                        note.note_id, note.user_id[:8],
                    )

            if not claimed:
                # Another cron tick already won — silently skip.
                claim_lost += 1
                continue

            # Step 2 — enqueue the per-fire worker task. Payload carries
            # everything the worker needs to operate without re-querying
            # cron-side state. due_at is included for idempotency: the
            # worker checks last_delivered_due == due_at on retry.
            await self._task_dispatch.enqueue_worker_task(
                task_type="execute_reminder",
                payload={
                    "note_id": note.note_id,
                    "user_id": note.user_id,
                    "due_at": note.due.isoformat(),
                },
            )
            enqueued += 1

        logger.info(
            "[Reminders] fire_due_reminders complete: enqueued=%d, claim_lost=%d, skipped=%d",
            enqueued, claim_lost, skipped,
        )
        return {
            "enqueued": enqueued,
            "claim_lost": claim_lost,
            "skipped": skipped,
        }, 200


# ---------------------------------------------------------------------------
# Utility: build system alert text for fired reminders
# ---------------------------------------------------------------------------
#
# Used by the ``execute_reminder`` worker (Step #8) to construct the
# system alert handed to SmartAgent via UserNotificationService.notify.
# Co-located with the cron service for now — future Step may move both
# producer and consumer of the alert into a shared infrastructure module.

def build_reminder_alert_summary(note: AgentNote) -> str:
    """Compact history label for a fired reminder — name only, no body, no id.

    Stored as the user-turn ``text`` while the full alert lives in ``full_text``;
    once the turn ages past the history-tiering window only this line remains.
    No note_id: the live ``active_reminders {}`` prompt block already maps name→id,
    and an aged-out firing marker needs no handle. See ``build_reminder_alert``.
    """
    return f'Received reminder: "{note.text}"'


def build_reminder_alert(note: AgentNote) -> str:
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
