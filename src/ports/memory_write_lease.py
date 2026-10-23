"""
MemoryWriteLeasePort — the per-account lease that serialises memory-writing runs (FACT_LIFECYCLE_RFC D9).

One lease per account. The owner is a fresh token per acquisition, made by the caller: acquiring never succeeds twice
for one account while a lease is unexpired, not even for the same owner, and a release only frees the lease of the
token that took it. Timing (`lease_until`, margins) lives in `domain/memory_write_lease.py`.
"""
from abc import ABC, abstractmethod
from datetime import datetime


class MemoryWriteLeasePort(ABC):
    @abstractmethod
    async def acquire(self, account_id: str, owner: str, now: datetime, until: datetime) -> bool:
        """Take the account's lease until `until` (aware datetimes): True when taken, False when an unexpired lease
        exists, whoever holds it (the same owner included). An absent or expired lease is taken. Raises ValueError
        for a falsy `account_id` or `owner`, a naive datetime, or `until <= now`; store errors raise."""

    @abstractmethod
    async def release(self, account_id: str, owner: str) -> None:
        """Delete the lease only while `owner` still holds it; an absent lease, or one taken over by another owner after
        expiry, is left alone (logged as a WARNING). A falsy `account_id` is a no-op: nothing can be held under it.
        Store errors raise."""
