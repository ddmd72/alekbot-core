"""FakeFactsPinRepository — the in-memory twin of `FactsPinRepository` (CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8).

A fake with semantics, not a mock: it applies the port's rules (the attempt decided by `FactsPinPolicy.decide` on the
stored record and the user's flag, the counter and the lock written together under one lock, `set_codes` by `replace`,
the first pending reset kept, a reset applied only when due). The store kit (`tests/unit/adapters/test_facts_pin_store_kit.py`)
runs the port's rules on both this and the Firestore adapter so the two cannot drift.

`records` is the stored records by user_id; `users` stands for the user documents the lock writes: user_id ->
`{"is_active": bool, "inactive_reason": Optional[str], "updated_at": datetime}` (a user absent here reads as inactive,
as an absent user document does). `cancel_pending_reset` is what `FakeDeviceSessionRepository.revoke_all` calls when
the two are linked, so the kill switch cancels a reset in the same step.
"""
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from src.domain.facts_pin import FactsPin
from src.domain.facts_pin_policy import FactsPinPolicy, PinOutcome
from src.ports.facts_pin_repository import FactsPinRepository


class FakeFactsPinRepository(FactsPinRepository):

    def __init__(self) -> None:
        self.records: Dict[str, FactsPin] = {}
        self.users: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def add_user(self, user_id: str, *, is_active: bool = True) -> None:
        """A user document as the users collection would hold it."""
        self.users[user_id] = {"is_active": is_active, "inactive_reason": None}

    async def get(self, user_id: str) -> Optional[FactsPin]:
        return self.records.get(user_id)

    async def set_codes(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime, *,
                        replace: bool) -> bool:
        async with self._lock:
            if (user_id in self.records) != replace:
                return False
            self.records[user_id] = FactsPin(user_id=user_id, pin_digest=pin_digest, duress_digest=duress_digest,
                                             updated_at=now)
            return True

    async def clear(self, user_id: str) -> bool:
        async with self._lock:
            return self.records.pop(user_id, None) is not None

    async def check(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime) -> PinOutcome:
        async with self._lock:
            record = self.records.get(user_id)
            user = self.users.get(user_id)
            active = user is not None and user.get("is_active", True) is True
            outcome = FactsPinPolicy.decide(record, active, pin_digest, duress_digest)
            if record is None:
                return outcome
            counter = FactsPinPolicy.counter_after(record, outcome)
            if counter != record.wrong_in_row:
                self.records[user_id] = record.model_copy(update={"wrong_in_row": counter, "updated_at": now})
            reason = outcome.lock_reason
            if reason is not None and user is not None:
                user.update({"is_active": False, "inactive_reason": reason.value, "updated_at": now})
            return outcome

    async def request_reset(self, user_id: str, now: datetime) -> bool:
        async with self._lock:
            record = self.records.get(user_id)
            if record is None or record.reset_requested_at is not None:
                return False
            self.records[user_id] = record.model_copy(update={
                "reset_requested_at": now, "reset_due_at": FactsPinPolicy.reset_due(now), "updated_at": now})
            return True

    async def cancel_reset(self, user_id: str, now: datetime) -> bool:
        async with self._lock:
            return self.cancel_pending_reset(user_id, now)

    def cancel_pending_reset(self, user_id: str, now: datetime) -> bool:
        """Clear a pending reset without taking the lock (the caller holds its own, with no await in between)."""
        record = self.records.get(user_id)
        if record is None or record.reset_requested_at is None:
            return False
        self.records[user_id] = record.model_copy(update={
            "reset_requested_at": None, "reset_due_at": None, "updated_at": now})
        return True

    async def apply_reset(self, user_id: str, now: datetime) -> bool:
        async with self._lock:
            record = self.records.get(user_id)
            if record is None or not FactsPinPolicy.reset_is_due(record, now):
                return False
            del self.records[user_id]
            return True
