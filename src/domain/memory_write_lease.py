"""Timing of the per-account memory-write lease (docs/10_rfcs/FACT_LIFECYCLE_RFC.md D9)."""

from datetime import datetime, timedelta

LEASE_MARGIN = timedelta(minutes=1)         # lease_until = start + run timeout + margin
LEASE_BUSY_RETRY_DELAY_S = 120              # a loser's deferral delay, and the width of its dedup bucket


def lease_until(now: datetime, run_timeout_s: float) -> datetime:
    """When a lease taken at `now` for a run of at most `run_timeout_s` seconds expires."""
    return now + timedelta(seconds=run_timeout_s) + LEASE_MARGIN


def agent_timeout_s(lease_until: datetime, now: datetime) -> float:
    """Seconds an agent run may use before the margin starts; zero or less means no time is left."""
    return (lease_until - LEASE_MARGIN - now).total_seconds()


def defer_bucket(now: datetime) -> int:
    """The retry-delay-wide bucket `now` falls in; losers of one bucket share one deferral task."""
    return int(now.timestamp()) // LEASE_BUSY_RETRY_DELAY_S
