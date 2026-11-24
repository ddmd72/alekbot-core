"""Where an external task is created from (EXTERNAL_TASK_CONFIRMATION_AND_ALERTS decision record).

The entry decides what a create does, and code sets it, never a model: a task created in a chat turn waits for the
owner's confirmation (`held`, with a code returned once), a task created while one of the owner's reminders runs goes
straight to the queue, because the owner approved that reminder when it was set.

`task_entry(context)` reads the entry from a run's context, in code: the owner's chat turn is `interactive_delivery`, a
reminder's run is its `run_origin`. Any other run (the daily email review, a delivery alert, a voice turn) has no
entry and never reaches the queue.

Public interface:
    TaskEntry.CHAT / TaskEntry.REMINDER
    REMINDER_RUN_ORIGIN: the `run_origin` of a reminder's run
    task_entry(context) -> Optional[TaskEntry]
"""
from enum import Enum
from typing import Any, Mapping, Optional

REMINDER_RUN_ORIGIN = "background:execute_reminder"


class TaskEntry(str, Enum):
    CHAT = "chat"
    REMINDER = "reminder"


def task_entry(context: Mapping[str, Any]) -> Optional[TaskEntry]:
    """CHAT on the owner's own chat turn, REMINDER while one of the owner's reminders runs, otherwise None."""
    if context.get("interactive_delivery"):
        return TaskEntry.CHAT
    if context.get("run_origin") == REMINDER_RUN_ORIGIN:
        return TaskEntry.REMINDER
    return None
