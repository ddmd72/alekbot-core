"""The memory inbox's `/worker` tasks (MCP_MEMORY_SUBMISSIONS_RFC §10): their types, their payload, their Cloud Tasks
dedup keys and their dispatch deadline.

Rhythm lives on the state document, never in a task name: a name dedupes only while its task is queued or running and
for about an hour after (`decisions/worker_task_dedup.md`). The keys only keep near-simultaneous enqueues to one task:
the sweep's triage per user and owner-local day, a final run per confirmation (`final_due_at` is unique per "True"),
and a lease deferral per account and lease-retry bucket, so losers of one bucket share one task.
"""
from datetime import datetime
from typing import Any, Dict, Final

from .memory_inbox import owner_day
from .memory_write_lease import defer_bucket

TRIAGE_TASK: Final[str] = "memory_inbox_triage"
FINAL_TASK: Final[str] = "memory_inbox_final"
SWEEP_TASK: Final[str] = "memory_inbox_sweep"
# The Cloud Tasks dispatch deadline of a triage or final task (a pass may run for most of it): the review's own value,
# `REVIEW_DEADLINE_S` (fact_review_task_keys.py), the Cloud Tasks maximum.
INBOX_DEADLINE_S: Final[int] = 1800


def task_payload(user_id: str) -> Dict[str, Any]:
    """The body of a triage or final task besides its type: the user only; the account comes from the profile."""
    return {"user_id": user_id}


def triage_key(user_id: str, now: datetime, tz: str) -> str:
    """The sweep's triage of `user_id` on the owner-local day of `now` (`tz` the owner's zone)."""
    return f"memory-inbox-triage:{user_id}:{owner_day(now, tz)}"


def triage_defer_key(account_id: str, now: datetime) -> str:
    """A triage deferred at `now` because the account's memory-write lease was held."""
    return f"memory-inbox-triage-defer:{account_id}:{defer_bucket(now)}"


def final_key(user_id: str, final_due_at: datetime) -> str:
    """The final run a confirmation set for `final_due_at` (epoch seconds)."""
    return f"memory-inbox-final:{user_id}:{int(final_due_at.timestamp())}"


def final_defer_key(account_id: str, now: datetime) -> str:
    """A final run deferred at `now` because the account's memory-write lease was held."""
    return f"memory-inbox-final-defer:{account_id}:{defer_bucket(now)}"
