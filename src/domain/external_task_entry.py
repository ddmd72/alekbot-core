"""Where an external task is created from (EXTERNAL_TASK_CONFIRMATION_AND_ALERTS decision record).

The entry decides what a create does, and code sets it, never a model: a task created in a chat turn waits for the
owner's confirmation (`held`, with a code returned once), a task created while one of the owner's reminders runs goes
straight to the queue, because the owner approved that reminder when it was set.

Public interface:
    TaskEntry.CHAT / TaskEntry.REMINDER
"""
from enum import Enum


class TaskEntry(str, Enum):
    CHAT = "chat"
    REMINDER = "reminder"
