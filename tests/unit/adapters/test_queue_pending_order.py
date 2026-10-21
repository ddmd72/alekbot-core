"""get_pending_batches returns a backlog oldest-first (the queues' composite indexes back the order_by)."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from tests.support.firestore_eval import EvalDb

INDEXES = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"


def _env() -> MagicMock:
    env = MagicMock()
    env.consolidation_queue_collection = "development_consolidation_queue"
    env.companion_extraction_queue_collection = "development_companion_extraction_queue"
    return env


def _batch(cls, key_field, key, batch_id, created_at, status=BatchStatus.PENDING):
    extra = {"session_id": "s"} if cls is ConsolidationBatch else {
        "account_id": "a", "companion_type": "tutor", "created_by_user_id": "u"}
    return cls(**{key_field: key}, **{k: v for k, v in extra.items() if k != key_field},
               batch_id=batch_id, messages=[], status=status, created_at=created_at)


@pytest.fixture
def cons():
    return FirestoreConsolidationQueue(EvalDb(), _env())


@pytest.fixture
def comp():
    return FirestoreCompanionExtractionQueue(EvalDb(), _env())


@pytest.mark.asyncio
async def test_consolidation_returns_oldest_first_across_pending_and_retry(cons):
    for bid, ts, st in [("zz", 3.0, BatchStatus.PENDING), ("aa", 1.0, BatchStatus.RETRY_PENDING),
                        ("mm", 2.0, BatchStatus.PENDING)]:
        await cons.enqueue_batch(_batch(ConsolidationBatch, "user_id", "u1", bid, ts, st))
    first = await cons.get_pending_batches(user_id="u1", limit=1)
    assert [b.created_at for b in first] == [1.0]
    await cons.delete_batch(first[0].batch_id)
    second = await cons.get_pending_batches(user_id="u1", limit=1)
    assert [b.created_at for b in second] == [2.0]
    both = await cons.get_pending_batches(user_id="u1", limit=10)
    assert [b.created_at for b in both] == [2.0, 3.0]


@pytest.mark.asyncio
async def test_consolidation_filters_other_user_and_completed(cons):
    await cons.enqueue_batch(_batch(ConsolidationBatch, "user_id", "u2", "o", 0.5))
    await cons.enqueue_batch(_batch(ConsolidationBatch, "user_id", "u1", "c", 0.6, BatchStatus.COMPLETED))
    await cons.enqueue_batch(_batch(ConsolidationBatch, "user_id", "u1", "p", 5.0))
    got = await cons.get_pending_batches(user_id="u1", limit=5)
    assert [b.batch_id for b in got] == ["p"]


@pytest.mark.asyncio
async def test_companion_returns_oldest_first_across_pending_and_retry(comp):
    for bid, ts, st in [("zz", 3.0, BatchStatus.PENDING), ("aa", 1.0, BatchStatus.RETRY_PENDING),
                        ("mm", 2.0, BatchStatus.PENDING)]:
        await comp.enqueue_batch(_batch(CompanionExtractionBatch, "session_id", "s1", bid, ts, st))
    first = await comp.get_pending_batches(session_id="s1", limit=1)
    assert [b.created_at for b in first] == [1.0]
    await comp.delete_batch(first[0].batch_id)
    second = await comp.get_pending_batches(session_id="s1", limit=1)
    assert [b.created_at for b in second] == [2.0]


@pytest.mark.asyncio
async def test_companion_filters_other_session_and_completed(comp):
    await comp.enqueue_batch(_batch(CompanionExtractionBatch, "session_id", "s2", "o", 0.5))
    await comp.enqueue_batch(_batch(CompanionExtractionBatch, "session_id", "s1", "c", 0.6, BatchStatus.COMPLETED))
    await comp.enqueue_batch(_batch(CompanionExtractionBatch, "session_id", "s1", "p", 5.0))
    got = await comp.get_pending_batches(session_id="s1", limit=5)
    assert [b.batch_id for b in got] == ["p"]


@pytest.mark.parametrize("collection,key", [
    ("development_consolidation_queue", "user_id"),
    ("development_companion_extraction_queue", "session_id"),
])
def test_composite_index_declared_with_valid_field_order(collection, key):
    indexes = json.loads(INDEXES.read_text())
    wanted = [(key, "ASCENDING"), ("status", "ASCENDING"), ("created_at", "ASCENDING")]
    found = [
        [(f["fieldPath"], f.get("order")) for f in i["fields"]]
        for i in indexes
        if i.get("collectionGroup") == collection and i.get("queryScope") == "COLLECTION" and "fields" in i
    ]
    assert wanted in found
