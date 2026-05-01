"""
Agent Note domain model.

A self-reminder written by the orchestrator to itself.
When the reminder fires, its instruction is run as a new conversation —
exactly as if the user had written it.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .task_complexity import TaskComplexity


@dataclass
class ReminderRecurrence:
    """How to reschedule a reminder after it fires."""
    type: str       # "hourly" | "daily" | "weekly" | "monthly"
    interval: int = 1  # every N units (e.g. interval=2, type="daily" → every 2 days)


@dataclass
class AgentNote:
    """Persisted orchestrator self-reminder."""
    note_id: str
    user_id: str
    text: str                               # Short display label (≤15 words)
    instruction: str                        # Full execution context, run when fired
    created_at: datetime
    due: datetime                           # UTC — when to fire
    recurrence: Optional[ReminderRecurrence] = None
    last_fired: Optional[datetime] = None   # UTC — updated after each fire
    # Execution tier for Smart when this reminder fires.
    # None → default simple_analytics (BALANCED + thinking=low).
    # Set by NotesAgent LLM at creation time based on instruction complexity.
    complexity: Optional[TaskComplexity] = None
    # Idempotency token: due-time of the most recent fire that was
    # actually delivered to the user. Set by the worker on success.
    # Cloud Tasks may retry execute_reminder; the worker checks
    # ``last_delivered_due == due_at`` and short-circuits to avoid
    # delivering the same fire twice.
    # See docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 7 D.3.
    last_delivered_due: Optional[datetime] = None


@dataclass
class NoteCreate:
    """Input for creating a new self-reminder."""
    user_id: str
    text: str                               # Short display label
    instruction: str                        # Full execution context
    due: datetime                           # UTC
    recurrence: Optional[ReminderRecurrence] = None
    complexity: Optional[TaskComplexity] = None


@dataclass
class NoteUpdate:
    """Input for updating an existing self-reminder (PATCH semantics)."""
    note_id: str
    user_id: str
    text: Optional[str] = None
    instruction: Optional[str] = None
    due: Optional[datetime] = None          # UTC
    recurrence: Optional[ReminderRecurrence] = None
    complexity: Optional[TaskComplexity] = None
