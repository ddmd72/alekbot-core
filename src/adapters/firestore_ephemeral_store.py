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

from google.cloud import firestore

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

    async def get_and_delete(self, key: str) -> Optional[dict]:
        """Read-and-consume in one Firestore transaction.

        `get()` followed by `delete()` is two round trips with nothing between
        them: two concurrent `/voice/session-config` requests for the same
        ticket can both complete their `get()` before either `delete()` lands,
        and both receive the caller's assembled persona. A transaction closes
        that window, and this is precisely the case Firestore's contention
        detection genuinely covers - a read and a write of the SAME document.
        (Contrast `FirestoreUserRepository.link_platform_identity`, whose
        residual gap is a *query*-based phantom read; there is no query here.)
        When two transactions collide, Firestore aborts one and
        `@firestore.async_transactional` retries it; the retry re-reads, finds
        the document gone, and returns None. Exactly one caller wins.

        An expired document is deleted and reported as absent, same
        housekeeping as `get()`.

        Call shape mirrors `link_platform_identity` (the already-verified
        precedent in this codebase): `await doc_ref.get(transaction=...)`
        returns the `DocumentSnapshot` directly, whereas `AsyncTransaction.get`
        returns an async generator of snapshots - both enlist in the
        transaction identically, so this takes the one that does not need
        unwrapping.
        """
        doc_ref = self._db.collection(self._collection).document(key)

        @firestore.async_transactional
        async def _transaction(transaction) -> Optional[dict]:
            snap = await doc_ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict()
            # Consume either way: the document is now spent or stale.
            transaction.delete(doc_ref)
            if data["expires_at"] < time.time():
                return None
            return data["value"]

        transaction = self._db.transaction()
        return await _transaction(transaction)
