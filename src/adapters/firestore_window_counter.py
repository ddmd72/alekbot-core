"""Firestore fixed-window counters (EXTERNAL_TASK_QUEUE_RFC §5.11).

One transaction per hit on the document `xtrate:<key>:<window_start>` of the TTL collection
`worker_task_dedup_collection`, holding `{count, expires_at}`. No new collection, policy or index: the collection's TTL
policy on `expires_at` deletes the record a full window after its window ends (`config/ttl_registry.json`). The key
always contains `:`, which a Cloud Tasks claim key of the same collection never does (`firestore_dedup_store.py`).
"""
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..ports.window_counter import WindowCounter

DOC_PREFIX = "xtrate"


def window_start(now: datetime, window_s: int) -> int:
    """Epoch seconds of the start of the window holding `now`."""
    return int(now.timestamp()) // window_s * window_s


class FirestoreWindowCounter(WindowCounter):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._collection = db_client.collection(env_config.worker_task_dedup_collection)

    async def increment(self, key: str, window_s: int, now: datetime) -> int:
        start = window_start(now, window_s)
        ref = self._collection.document(f"{DOC_PREFIX}:{key}:{start}")
        expires_at = datetime.fromtimestamp(start + 2 * window_s, tz=timezone.utc)

        @firestore.async_transactional
        async def _run(transaction: Any) -> int:
            snap = await ref.get(transaction=transaction)
            count = int((snap.to_dict() or {}).get("count", 0)) + 1 if snap.exists else 1
            transaction.set(ref, {"count": count, "expires_at": expires_at})
            return count

        return await _run(self._db.transaction())
