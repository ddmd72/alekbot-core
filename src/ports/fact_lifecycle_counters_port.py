"""
FactLifecycleCountersPort — the per-account, per-day numbers of the fact lifecycle (FACT_LIFECYCLE_RFC D10).

The janitor writes a snapshot of its run (`record_sweep`); the consolidator adds the expiry tool errors it meets
(`add`). Days are UTC days. The daily ops line is posted at most once a day (`claim_line`). The numbers are kept until
deleted: they expire by no rule of the store.
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Mapping

from ..domain.fact_lifecycle_day import FactLifecycleDay


class FactLifecycleCountersPort(ABC):
    @abstractmethod
    async def record_sweep(self, account_id: str, day: date, values: Mapping[str, Any]) -> None:
        """Write the janitor's snapshot for the account's day: each field replaces its previous value (the last run of
        the day wins), the counters added by `add` stay. `values` is a whole snapshot: exactly
        `domain.fact_lifecycle_day.SNAPSHOT_FIELDS`, each of its type (`oldest_stale_days` may be None).
        Raises ValueError for a falsy `account_id`, a `datetime` as `day`, or a partial, extended or mistyped snapshot
        (`check_snapshot`); store errors raise."""

    @abstractmethod
    async def add(self, account_id: str, day: date, counts: Mapping[str, int]) -> None:
        """Add to the account's counters for the day; concurrent adds never lose one. Zero counts write nothing.
        Raises ValueError for a falsy `account_id`, a `datetime` as `day`, a key outside
        `domain.fact_lifecycle_day.COUNTER_FIELDS` or a negative or non-integer count; store errors raise."""

    @abstractmethod
    async def for_day(self, day: date) -> List[FactLifecycleDay]:
        """Every account's row for the UTC day, ordered by account id. Never returns the posted-line mark."""

    @abstractmethod
    async def claim_line(self, day: date) -> bool:
        """True for the first caller of the day, False afterwards: the right to post the day's ops line."""
