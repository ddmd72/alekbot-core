"""Timing of the janitor's run guard (Facts S4, S4-D10): at most one janitor run at a time."""

from datetime import datetime, timedelta
from typing import Optional

from .lease import lease_held

RUN_GUARD_SECONDS = 300      # a run that dies without releasing blocks the next one for at most this long


def run_guard_until(now: datetime) -> datetime:
    """When a guard taken at `now` expires."""
    return now + timedelta(seconds=RUN_GUARD_SECONDS)


def run_guard_held(held_until: Optional[datetime], now: datetime) -> bool:
    """Whether a stored guard still blocks a run at `now` (the lease rule, `domain.lease.lease_held`)."""
    return lease_held(held_until, now)
