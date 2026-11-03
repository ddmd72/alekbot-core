"""Mutation survivors of `src/adapters/firestore_dedup_store.py` (p3 · g6): the legacy `mark_processed` write, and the
`except NotFound` give-way of the preconditioned rewrite. Observed on the evaluating Firestore fake."""
import time
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import NotFound

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from tests.support.firestore_eval import EvalDb, EvalDocRef

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


async def test_not_found_on_the_rewrite_is_a_loss_when_another_caller_holds_a_live_mark(monkeypatch):
    store, db = _make()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    docs = db.collection(COLLECTION).docs
    docs["evt-1"] = {"created_at": time.time() - 7200, "expires_at": past}
    real_update = EvalDocRef.update

    async def update_lost(self, data, option=None):
        # the mark vanishes and another caller claims it live before our rewrite lands
        docs["evt-1"] = {"created_at": time.time(), "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
        raise NotFound("gone")

    monkeypatch.setattr(EvalDocRef, "update", update_lost)
    assert await store.try_mark_processed("evt-1") is False
    monkeypatch.setattr(EvalDocRef, "update", real_update)
    assert docs["evt-1"]["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=30)


async def test_not_found_on_the_rewrite_retries_the_create_once_when_the_mark_is_gone(monkeypatch):
    store, db = _make()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    docs = db.collection(COLLECTION).docs
    docs["evt-1"] = {"created_at": time.time() - 7200, "expires_at": past}

    async def update_gone(self, data, option=None):
        docs.pop("evt-1", None)
        raise NotFound("gone")

    monkeypatch.setattr(EvalDocRef, "update", update_gone)
    assert await store.try_mark_processed("evt-1") is True
    assert docs["evt-1"]["expires_at"] > datetime.now(timezone.utc)
