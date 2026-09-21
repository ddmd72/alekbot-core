"""
FirestoreEphemeralStore — Firestore implementation of EphemeralStore.

Firestore is the only shared, multi-instance store already in this codebase
(the main service is horizontally scaled — RFC §4.5), so it backs both the
call ticket and the §3 one-call-per-user marker via separate collections.

Client API mirrors FirestoreDedupStore (src/adapters/firestore_dedup_store.py)
and FirestoreCompanionMemoryRepository: `db_client` is a
`google.cloud.firestore_v1.async_client.AsyncClient`; `.collection(name).document(key)`
then `.get()` / `.set()` / `.delete()` are awaited coroutines, and `.get()`
returns a snapshot exposing `.exists` / `.to_dict()`.
"""
import time
from typing import Optional

from ..ports.ephemeral_store import EphemeralStore


class FirestoreEphemeralStore(EphemeralStore):
    def __init__(self, db_client, collection: str) -> None:
        self._db = db_client
        self._collection = collection

    async def set(self, key: str, value: dict, ttl_s: int) -> None:
        doc = self._db.collection(self._collection).document(key)
        await doc.set({"value": value, "expires_at": time.time() + ttl_s})

    async def get(self, key: str) -> Optional[dict]:
        doc = self._db.collection(self._collection).document(key)
        snap = await doc.get()
        if not snap.exists:
            return None
        data = snap.to_dict()
        if data["expires_at"] < time.time():
            await doc.delete()
            return None
        return data["value"]

    async def delete(self, key: str) -> None:
        await self._db.collection(self._collection).document(key).delete()
