"""
NotificationKind — classification of background notifications by purpose.

Drives the per-call SLA (timeout budget primarily) applied by
``UserNotificationService.notify()``. Each kind corresponds to a distinct
operational shape — interactive turn, recurring reminder, batch digest,
async result delivery — with a different acceptable wall-clock budget.

The enum values are stable strings (``(str, Enum)``) so the kind can be
serialized into Cloud Task payloads, log records, and metrics tags
without converting through an integer surrogate.

See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 5
"""

from __future__ import annotations

from enum import Enum


class NotificationKind(str, Enum):
    """Purpose-classification for any background ``notify()`` call."""

    # Synchronous user-facing reply (Slack/Telegram conversation handler).
    # Tightest budget — the user is waiting on the other end.
    INTERACTIVE = "interactive"

    # Self-reminder fire (NotesAgent → SmartAgent execution as a new
    # conversation). Asynchronous from the user's perspective; budget
    # accommodates one delegation hop plus a short specialist call.
    REMINDER = "reminder"

    # Daily/periodic digest (email review, news briefing). Multi-turn
    # analytical work over a structured payload — needs the longest
    # in-process budget.
    DAILY_DIGEST = "daily_digest"

    # Async document/PDF/HTML callback (AgentWorkerHandler delivers the
    # result of an ASYNC delegation). Formatting-only on Smart's side.
    DOCUMENT_DELIVERY = "document_delivery"

    # Deep research result delivery (long-running Cloud Run Job → callback).
    # The agent call itself is short — it just renders the prepared report.
    DEEP_RESEARCH = "deep_research"
