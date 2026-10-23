"""
FirestoreMemoryWriteLease — MemoryWriteLeasePort on Firestore (FACT_LIFECYCLE_RFC D9).

Collection `{prefix}memory_write_leases`, doc id = account id, fields `lease_owner`, `lease_until`, `acquired_at`.
No field is named `expires_at` (nine collections carry a TTL policy on that name) and the collection has no TTL policy:
an expired lease stays until the next acquire overwrites it.
"""
from datetime import datetime
from typing import Any

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..utils.logger import logger


class FirestoreMemoryWriteLease(MemoryWriteLeasePort):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._leases = db_client.collection(env_config.memory_write_leases_collection)

    async def acquire(self, account_id: str, owner: str, now: datetime, until: datetime) -> bool:
        if not account_id:
            raise ValueError("account_id is required to take the memory-write lease")
        if not owner:
            raise ValueError("owner is required to take the memory-write lease")
        if now.tzinfo is None or until.tzinfo is None:
            raise ValueError("now and until must be timezone-aware")
        if until <= now:
            raise ValueError("until must be later than now")
        ref = self._leases.document(account_id)

        @firestore.async_transactional
        async def _take(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            held_until = (snap.to_dict() or {}).get("lease_until") if snap.exists else None
            if held_until is not None and held_until > now:
                return False
            transaction.set(ref, {"lease_owner": owner, "lease_until": until, "acquired_at": now})
            return True

        taken = await _take(self._db.transaction())
        if not taken:
            logger.info(f"[MemoryWriteLease] busy for account {account_id[:8]}")
        return taken

    async def release(self, account_id: str, owner: str) -> None:
        if not account_id:
            return
        ref = self._leases.document(account_id)

        @firestore.async_transactional
        async def _release(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return True
            if (snap.to_dict() or {}).get("lease_owner") != owner:
                return False
            transaction.delete(ref)
            return True

        if not await _release(self._db.transaction()):
            logger.warning(f"[MemoryWriteLease] release found another owner for account {account_id[:8]}; "
                           f"its own lease had expired and was taken over")
