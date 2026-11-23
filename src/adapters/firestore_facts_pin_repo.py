"""FirestoreFactsPinRepository — facts PIN records in Firestore, and the account lock they may write
(CABINET_DEVICE_SESSIONS_RFC §7.7, §7.8; port `FactsPinRepository`).

Collection `{prefix}facts_pin_v1` (`EnvironmentConfig.facts_pin_collection`), doc id = `user_id`. Stored fields:
`user_id, pin_digest, duress_digest, wrong_in_row, reset_requested_at, reset_due_at, updated_at` (timestamps tz-aware
UTC). Neither code is stored, only its keyed digest. No TTL: a record lives until the PIN is removed or reset.

- `check` is one transaction over two documents: this record and the user's document in the users collection
  (`EnvironmentConfig.domain_users_collection`, doc id = user_id). It reads both (the user's `is_active` only; a missing
  document or field: an absent user reads as inactive, a present one without the field as active, the `UserProfile`
  default), decides with `FactsPinPolicy.decide`, then writes the counter and, for a locking outcome, `is_active`,
  `inactive_reason` and `updated_at` on the user document. A concurrent attempt changes the record, so the loser's
  transaction re-runs against the new count.
- `set_codes`, `request_reset`, `cancel_reset` and `apply_reset` are one transaction each; `clear` deletes in one
  transaction so it can say whether a record was there.
- A document whose stored `user_id` contradicts its id is treated as absent and logged at ERROR.
- `DeviceSessionRepository.revoke_all` (`firestore_device_session_repo.py`) clears `reset_requested_at` and
  `reset_due_at` of this document in its own transaction: those two field names are shared with it.

Never logs a digest: only the user's id prefix and the outcome.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from ..domain.facts_pin import FactsPin
from ..domain.facts_pin_policy import FactsPinPolicy, PinOutcome
from ..ports.facts_pin_repository import FactsPinRepository
from ..utils.logger import logger

_TIME_FIELDS = ("reset_requested_at", "reset_due_at", "updated_at")


def _utc(raw: Any) -> Optional[datetime]:
    """A stored timestamp as a plain tz-aware UTC datetime (never a DatetimeWithNanoseconds)."""
    if raw is None:
        return None
    if raw.tzinfo is not None:
        raw = raw.astimezone(timezone.utc)
    return datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second, raw.microsecond,
                    tzinfo=timezone.utc)


def _from_doc(data: Dict[str, Any]) -> FactsPin:
    return FactsPin(user_id=data["user_id"], pin_digest=data["pin_digest"], duress_digest=data["duress_digest"],
                    wrong_in_row=data.get("wrong_in_row", 0),
                    **{name: _utc(data.get(name)) for name in _TIME_FIELDS})


class FirestoreFactsPinRepository(FactsPinRepository):

    def __init__(self, db_client: Any, collection_name: str, users_collection: str) -> None:
        self._db = db_client
        self._col = db_client.collection(collection_name)
        self._users = db_client.collection(users_collection)
        logger.info(f"🔢 Facts PIN store initialized. Collections: {collection_name}, {users_collection}")

    @staticmethod
    def _owned(snap: Any) -> Optional[Dict[str, Any]]:
        """The stored record when it exists and its stored `user_id` is its id; None otherwise (ERROR on a mismatch)."""
        if not snap.exists:
            return None
        data: Dict[str, Any] = snap.to_dict() or {}
        if data.get("user_id") != snap.id:
            logger.error(f"💥 Facts PIN doc {str(snap.id)[:8]} does not match its stored user_id; treated as absent")
            return None
        return data

    async def get(self, user_id: str) -> Optional[FactsPin]:
        data = self._owned(await self._col.document(user_id).get())
        return None if data is None else _from_doc(data)

    async def set_codes(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime, *,
                        replace: bool) -> bool:
        ref = self._col.document(user_id)
        record = FactsPin(user_id=user_id, pin_digest=pin_digest, duress_digest=duress_digest, updated_at=now)

        @firestore.async_transactional
        async def _set(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if snap.exists != replace or (replace and self._owned(snap) is None):
                return False
            transaction.set(ref, dict(record))
            return True

        written: bool = await _set(self._db.transaction())
        logger.info(f"🔢 Facts PIN {'changed' if replace else 'set'}: user={user_id[:8]} written={written}")
        return written

    async def clear(self, user_id: str) -> bool:
        ref = self._col.document(user_id)

        @firestore.async_transactional
        async def _clear(transaction: Any) -> bool:
            if self._owned(await ref.get(transaction=transaction)) is None:
                return False
            transaction.delete(ref)
            return True

        cleared: bool = await _clear(self._db.transaction())
        logger.info(f"🔢 Facts PIN removed: user={user_id[:8]} cleared={cleared}")
        return cleared

    async def check(self, user_id: str, pin_digest: str, duress_digest: str, now: datetime) -> PinOutcome:
        ref = self._col.document(user_id)
        user_ref = self._users.document(user_id)

        @firestore.async_transactional
        async def _check(transaction: Any) -> PinOutcome:
            data = self._owned(await ref.get(transaction=transaction))
            user = await user_ref.get(transaction=transaction)
            active = user.exists and (user.to_dict() or {}).get("is_active", True) is True
            record = None if data is None else _from_doc(data)
            outcome = FactsPinPolicy.decide(record, active, pin_digest, duress_digest)
            if record is None:
                return outcome
            counter = FactsPinPolicy.counter_after(record, outcome)
            if counter != record.wrong_in_row:
                transaction.update(ref, {"wrong_in_row": counter, "updated_at": now})
            reason = outcome.lock_reason
            if reason is not None:
                transaction.update(user_ref, {"is_active": False, "inactive_reason": reason.value,
                                              "updated_at": now})
            return outcome

        outcome: PinOutcome = await _check(self._db.transaction())
        reason = outcome.lock_reason
        if reason is not None:
            logger.warning(f"🔒 Account locked by the facts PIN: user={user_id[:8]} reason={reason.value}")
        return outcome

    async def request_reset(self, user_id: str, now: datetime) -> bool:
        ref = self._col.document(user_id)

        @firestore.async_transactional
        async def _request(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None or data.get("reset_requested_at") is not None:
                return False
            transaction.update(ref, {"reset_requested_at": now, "reset_due_at": FactsPinPolicy.reset_due(now),
                                     "updated_at": now})
            return True

        started: bool = await _request(self._db.transaction())
        logger.info(f"🔢 Facts PIN reset requested: user={user_id[:8]} started={started}")
        return started

    async def cancel_reset(self, user_id: str, now: datetime) -> bool:
        ref = self._col.document(user_id)

        @firestore.async_transactional
        async def _cancel(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None or data.get("reset_requested_at") is None:
                return False
            transaction.update(ref, {"reset_requested_at": None, "reset_due_at": None, "updated_at": now})
            return True

        cancelled: bool = await _cancel(self._db.transaction())
        logger.info(f"🔢 Facts PIN reset cancelled: user={user_id[:8]} cancelled={cancelled}")
        return cancelled

    async def apply_reset(self, user_id: str, now: datetime) -> bool:
        ref = self._col.document(user_id)

        @firestore.async_transactional
        async def _apply(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None or not FactsPinPolicy.reset_is_due(_from_doc(data), now):
                return False
            transaction.delete(ref)
            return True

        applied: bool = await _apply(self._db.transaction())
        if applied:
            logger.info(f"🔢 Facts PIN reset applied, the PIN is gone: user={user_id[:8]}")
        return applied
