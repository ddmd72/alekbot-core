"""FactsPinRepository — the store of facts PIN records, and the one transaction that may lock an account through them
(CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8).

A record is addressed by its `user_id`; a user without a record has no PIN. Every write that depends on what is stored is
one transaction. `check` is the load-bearing one: it reads the record and the user's `is_active`, decides with
`FactsPinPolicy.decide`, writes the counter and, on the duress code or the fifth wrong code in a row, writes
`is_active = false` and `inactive_reason` on the user document in the same transaction. So parallel wrong attempts
cannot pass `MAX_WRONG` (a concurrent attempt conflicts and re-runs against the new count), and the counter and the lock
are never seen apart.

The pending reset of a forgotten PIN is three transactions: `request_reset` (keeps the first pending request),
`cancel_reset` and `apply_reset` (deletes the record once the reset is due). `DeviceSessionRepository.revoke_all` also
cancels a pending reset, in its own transaction.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from src.domain.facts_pin import FactsPin
from src.domain.facts_pin_policy import PinOutcome


class FactsPinRepository(ABC):

    @abstractmethod
    async def get(self, user_id: str) -> Optional[FactsPin]:
        """The user's record; None when the user has no PIN."""

    @abstractmethod
    async def set_codes(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime, *,
                        replace: bool) -> bool:
        """Store both digests, with the counter cleared, no pending reset and `updated_at` ← `now`, in one transaction.
        `replace=False` (a first PIN) writes only when the user has no record; `replace=True` (a change) only when the
        user has one. False, and nothing written, otherwise."""

    @abstractmethod
    async def clear(self, user_id: str) -> bool:
        """Delete the user's record (the PIN is removed). False when there was none."""

    @abstractmethod
    async def check(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime) -> PinOutcome:
        """One attempt, in one transaction: read the record and the user's `is_active` (an absent user document reads
        as inactive), decide (`FactsPinPolicy.decide`), write `wrong_in_row` (`FactsPinPolicy.counter_after`) with
        `updated_at` ← `now` when it changes, and, when the outcome is `locked_now`, write `is_active = false`,
        `inactive_reason` (`PinOutcome.lock_reason`) and `updated_at` ← `now` on the user document."""

    @abstractmethod
    async def request_reset(self, user_id: str, now: datetime) -> bool:
        """Start a reset: `reset_requested_at` ← `now`, `reset_due_at` ← `FactsPinPolicy.reset_due(now)`. False, and
        nothing written, when the user has no record or a reset is already pending (the first request is kept)."""

    @abstractmethod
    async def cancel_reset(self, user_id: str, now: datetime) -> bool:
        """Clear a pending reset (`updated_at` ← `now`). False when none is pending."""

    @abstractmethod
    async def apply_reset(self, user_id: str, now: datetime) -> bool:
        """Delete the record when its reset is due at `now` (`FactsPinPolicy.reset_is_due`). False, and nothing
        written, otherwise."""
