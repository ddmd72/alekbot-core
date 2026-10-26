"""
FactLifecycleCountersPort — the per-account, per-day numbers of the fact lifecycle (FACT_LIFECYCLE_RFC D10).

The janitor writes a snapshot of its run (`record_sweep`); whoever meets a counted event adds to its counter (`add`).
Days are UTC days. The daily ops line is posted at most once a day (`claim_line`). The numbers are kept until deleted:
they expire by no rule of the store. The janitor's run guard (`acquire_run`, `release_run`) is the one exception: one
holder at a time, with an expiry; timing lives in `domain/fact_lifecycle_run_guard.py`.
"""
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import List, Mapping

from ..domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot


class FactLifecycleCountersPort(ABC):
    @abstractmethod
    async def record_sweep(self, account_id: str, day: date, snapshot: SweepSnapshot) -> None:
        """Write the janitor's snapshot for the account's day. It replaces the day's earlier snapshot whole (the last
        run of the day wins); the counters stay. Raises ValueError for a falsy `account_id` or a `datetime` as `day`;
        store errors raise."""

    @abstractmethod
    async def add(self, account_id: str, day: date, counts: Mapping[CounterField, int]) -> None:
        """Add to the account's counters for the day; concurrent adds never lose one. Zero counts write nothing.
        Raises ValueError for a falsy `account_id`, a `datetime` as `day`, a name outside `CounterField`, or a count
        that is not a non-negative integer (`check_count`), before anything is written; store errors raise."""

    @abstractmethod
    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        """Every account's row for the UTC day, ordered by account id. Never returns the posted-line mark."""

    @abstractmethod
    async def claim_line(self, day: date) -> bool:
        """True for the first caller of the day, False afterwards: the right to post the day's ops line."""

    @abstractmethod
    async def acquire_run(self, owner: str, now: datetime, until: datetime) -> bool:
        """Take the janitor's run guard until `until` (aware datetimes): True when taken, False while an unexpired guard
        exists, whoever holds it (the same owner included). An absent or expired guard is taken. `owner` is a fresh token
        per run, made by the caller. Raises ValueError for a falsy `owner`, a naive datetime, or `until <= now`; store
        errors raise."""

    @abstractmethod
    async def release_run(self, owner: str) -> None:
        """Free the guard only while `owner` still holds it. An absent guard is left alone silently; one held by another
        owner (taken over after expiry) is left alone and logged as a WARNING. Store errors raise."""
