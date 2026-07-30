"""
Agent Note domain model.

A self-reminder written by the orchestrator to itself.
When the reminder fires, its instruction is run as a new conversation —
exactly as if the user had written it.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .task_complexity import TaskComplexity


@dataclass
class AgentNote:
    """Persisted orchestrator self-reminder."""
    note_id: str
    user_id: str
    text: str                               # Short display label (≤15 words)
    instruction: str                        # Full execution context, run when fired
    created_at: datetime
    due: datetime                           # UTC — when to fire
    # RFC 5545 RRULE without DTSTART ("FREQ=WEEKLY;BYDAY=TU,FR"); None → one-time.
    # The anchor is always ``due``, so the rule is a pure pattern. Evaluated
    # against the user's local wall clock via RecurrencePort.
    recurrence: Optional[str] = None
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
    recurrence: Optional[str] = None        # RRULE, see AgentNote.recurrence
    complexity: Optional[TaskComplexity] = None


@dataclass
class NoteUpdate:
    """Input for updating an existing self-reminder (PATCH semantics)."""
    note_id: str
    user_id: str
    text: Optional[str] = None
    instruction: Optional[str] = None
    due: Optional[datetime] = None          # UTC
    recurrence: Optional[str] = None        # RRULE; None = leave unchanged
    complexity: Optional[TaskComplexity] = None
    # PATCH cannot express "remove" through None — that already means "leave
    # unchanged" — so turning a recurring reminder back into a one-time one needs
    # its own flag. Without it the only way back was delete + recreate.
    clear_recurrence: bool = False
