"""FirestoreDedupStore claim semantics on the evaluating fake: expired marks are re-claimable, errors raise."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from tests.support.firestore_eval import EvalDb

COLLECTION = "development_worker_task_dedup"


def _make(ttl=1800):
    db = EvalDb()
    return FirestoreDedupStore(db_client=db, collection_name=COLLECTION, ttl_seconds=ttl), db


def _expire(db, event_id, **extra):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    db.collection(COLLECTION).docs[event_id] = {"created_at": time.time() - 7200, "expires_at": past, **extra}


async def test_fresh_claim_writes_mark_with_expiry():
    store, db = _make()
    assert await store.try_mark_processed("t1") is True
    doc = db.collection(COLLECTION).docs["t1"]
    assert doc["expires_at"] > datetime.now(timezone.utc)


async def test_live_duplicate_is_refused():
    store, _ = _make()
    await store.try_mark_processed("t1")
    assert await store.try_mark_processed("t1") is False


async def test_expired_unpurged_mark_is_reclaimed_and_rewritten():
    store, db = _make()
    _expire(db, "t1")
    assert await store.try_mark_processed("t1") is True
    assert db.collection(COLLECTION).docs["t1"]["expires_at"] > datetime.now(timezone.utc)
    assert await store.try_mark_processed("t1") is False
    assert await store.is_duplicate("t1") is True


async def test_concurrent_reclaims_of_one_expired_mark_yield_one_winner():
    store, db = _make()
    _expire(db, "t1")
    db.collection(COLLECTION).yield_on_get = True
    results = await asyncio.gather(*(store.try_mark_processed("t1") for _ in range(4)))
    assert sorted(results) == [False, False, False, True]


async def test_mark_released_between_create_and_get_is_created_again():
    store, db = _make()
    col = db.collection(COLLECTION)
    col.docs["t1"] = {"created_at": time.time(), "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
    real_get = col.document("t1").__class__.get

    async def release_then_get(self, *a, **kw):
        col.docs.pop("t1", None)   # the owner released after our create() lost
        return await real_get(self, *a, **kw)

    col.document("t1").__class__.get = release_then_get
    try:
        assert await store.try_mark_processed("t1") is True
    finally:
        col.document("t1").__class__.get = real_get
    assert "t1" in col.docs


async def test_other_errors_propagate_and_claim_nothing():
    store, db = _make()
    ref_cls = db.collection(COLLECTION).document("t1").__class__
    real_create = ref_cls.create
    ref_cls.create = AsyncMock(side_effect=RuntimeError("unavailable"))
    try:
        with pytest.raises(RuntimeError):
            await store.try_mark_processed("t1")
    finally:
        ref_cls.create = real_create
    assert "t1" not in db.collection(COLLECTION).docs


async def test_liveness_honours_the_docs_own_expires_at():
    store, db = _make(ttl=1800)
    db.collection(COLLECTION).docs["long"] = {
        "created_at": time.time() - 3600,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=47),
    }
    assert await store.is_duplicate("long") is True
    assert await store.try_mark_processed("long") is False


async def test_old_doc_without_expires_at_falls_back_to_created_at_ttl():
    store, db = _make(ttl=1800)
    db.collection(COLLECTION).docs["old"] = {"created_at": time.time() - 3600}
    assert await store.is_duplicate("old") is False
    assert await store.try_mark_processed("old") is True
