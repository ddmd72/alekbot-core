"""
FactLifecycleCountersPort — the per-account, per-day numbers of the fact lifecycle (FACT_LIFECYCLE_RFC D10).

The janitor writes a snapshot of its run (`record_sweep`); whoever meets a counted event adds to its counter (`add`).
Days are UTC days. The daily ops line is posted at most once a day (`claim_line`). The numbers are kept until deleted:
they expire by no rule of the store.
"""
from abc import ABC, abstractmethod
from datetime import date
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
