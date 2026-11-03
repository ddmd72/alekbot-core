"""Mutation survivors of `src/adapters/firestore_dedup_store.py` (p3 · g6): the legacy `mark_processed` write. Observed on the evaluating Firestore fake."""
from datetime import datetime, timezone

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from tests.support.firestore_eval import EvalDb

COLLECTION = "development_worker_task_dedup"


def _make(ttl=1800):
    db = EvalDb()
    return FirestoreDedupStore(db_client=db, collection_name=COLLECTION, ttl_seconds=ttl), db


async def test_mark_processed_stores_a_live_mark_that_blocks_a_claim():
    store, db = _make()
    await store.mark_processed("evt-1")
    doc = db.collection(COLLECTION).docs["evt-1"]
    assert doc["expires_at"] > datetime.now(timezone.utc)
    assert await store.is_duplicate("evt-1") is True
    assert await store.try_mark_processed("evt-1") is False
