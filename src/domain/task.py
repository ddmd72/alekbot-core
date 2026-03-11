"""
Task domain models.

Used by TasksProviderPort and TasksAgent.
Supports Google Tasks (first provider) and Things 3 / Apple Reminders (future).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TaskStatus(str, Enum):
    NEEDS_ACTION = "needsAction"
    COMPLETED = "completed"


class Task(BaseModel):
    """A task from any task management provider."""

    task_id: str
    title: str
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    status: TaskStatus = TaskStatus.NEEDS_ACTION
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    provider: str = "google_tasks"  # "google_tasks" | "things3" | "apple_reminders"

    def is_completed(self) -> bool:
        return self.status == TaskStatus.COMPLETED

    def to_display_string(self) -> str:
        """Single-line summary for Slack output."""
        status_icon = "✅" if self.is_completed() else "⬜"
        due = f" (due {self.due_date.strftime('%Y-%m-%d')})" if self.due_date else ""
        notes = f" — {self.notes[:80]}" if self.notes else ""
        return f"{status_icon} {self.title}{due}{notes}"


class TaskCreate(BaseModel):
    """Input for creating a new task."""

    title: str
    notes: Optional[str] = None
    due_date: Optional[datetime] = None


class TaskUpdate(BaseModel):
    """Input for updating an existing task. All fields optional."""

    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[TaskStatus] = None
