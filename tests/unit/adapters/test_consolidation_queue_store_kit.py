"""ConsolidationQueue kit (P11 · G2): the docstring of `src/ports/consolidation_queue.py` checked against
`FirestoreConsolidationQueue` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw
stored documents and on what readers return.

Not duplicated here (they stay the spec): zombies and lost runs (`test_consolidation_queue_zombie_attempts.py`), bounded
FAILED revivals (`test_consolidation_queue_failed_bounded.py`), open batches (`test_firestore_consolidation_queue_open_batches.py`),
the pending order across queues (`test_queue_pending_order.py`). Legacy batches missing a field are not pinned: no writer
produces one (the data audit settles them).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

import src.adapters.firestore_consolidation_queue as queue_module
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
QUEUE = "eval_consolidation_queue"
MESSAGES = [{"role": "user", "text": "When is the car service?"}, {"role": "model", "text": "On 2026-11-05."}]


@pytest.fixture
def queue():
    db = EvalDb(indexes=INDEXES_FILE, groups={QUEUE: "development_consolidation_queue"})
    return FirestoreConsolidationQueue(db, SimpleNamespace(consolidation_queue_collection=QUEUE))


def docs(queue):
    return queue.db.collection(QUEUE).docs


async def put(queue, batch_id, created_at, status=BatchStatus.PENDING, user_id="u1", **extra):
    await queue.enqueue_batch(ConsolidationBatch(batch_id=batch_id, user_id=user_id, session_id=f"{user_id}:C1",
                                                 messages=MESSAGES, created_at=created_at, status=status, **extra))


def ids(batches):
    return [b.batch_id for b in batches]


# --------------------------------------------------------------------------------------------------- pending

async def test_pending_is_oldest_first_and_served_by_the_declared_index(queue):
    await put(queue, "b2", 2.0)
    await put(queue, "b1", 1.0)
    await put(queue, "b3", 3.0, BatchStatus.RETRY_PENDING)
    got = await queue.get_pending_batches("u1")
    assert ids(got) == ["b1", "b2", "b3"]
    assert got[0] == ConsolidationBatch(batch_id="b1", user_id="u1", session_id="u1:C1", messages=MESSAGES,
                                        created_at=1.0)


async def test_pending_respects_limit_after_the_filter(queue):
    await put(queue, "other-old", 0.1, user_id="u2")
    await put(queue, "failed-old", 0.2, BatchStatus.FAILED)
    await put(queue, "processing-old", 0.3, BatchStatus.PROCESSING)
    for n, t in (("b3", 3.0), ("b1", 1.0), ("b2", 2.0)):
        await put(queue, n, t)
    assert ids(await queue.get_pending_batches("u1", limit=2)) == ["b1", "b2"]


async def test_pending_excludes_processing_failed_completed_and_other_users(queue):
    await put(queue, "mine", 5.0)
    for n, status in (("p", BatchStatus.PROCESSING), ("f", BatchStatus.FAILED), ("c", BatchStatus.COMPLETED)):
        await put(queue, n, 1.0, status)
    await put(queue, "theirs", 1.0, user_id="u2")
    assert ids(await queue.get_pending_batches("u1")) == ["mine"]
    assert ids(await queue.get_pending_batches("u9")) == []


async def test_an_unknown_extra_field_is_ignored(queue):
    await put(queue, "b1", 1.0)
    docs(queue)["b1"]["routed_by"] = "overflow-v0"
    assert ids(await queue.get_pending_batches("u1")) == ["b1"]


# --------------------------------------------------------------------------------------------------- writes

async def test_enqueue_twice_is_one_document_and_delete_is_idempotent(queue):
    batch = ConsolidationBatch(batch_id="b1", user_id="u1", session_id="u1:C1", messages=MESSAGES, created_at=1.0)
    assert await queue.enqueue_batch(batch) == "b1"
    assert await queue.enqueue_batch(batch) == "b1"
    assert list(docs(queue)) == ["b1"] and docs(queue)["b1"] == batch.model_dump(mode="python") | {"status": "pending"}
    await queue.delete_batch("b1")
    await queue.delete_batch("b1")
    assert docs(queue) == {}


async def test_update_status_on_a_missing_batch_raises_not_found(queue):
    raised = None
    try:
        await queue.update_batch_status("nope", BatchStatus.COMPLETED)
    except NotFound as exc:
        raised = exc
    assert raised is not None and docs(queue) == {}


async def test_update_status_writes_facts_extracted_and_keeps_last_error_unless_given(queue, monkeypatch):
    await put(queue, "b1", 1.0, last_error="earlier: provider down")
    await queue.update_batch_status("b1", BatchStatus.RETRY_PENDING, facts_extracted=3)
    doc = docs(queue)["b1"]
    assert (doc["status"], doc["facts_extracted"], doc["last_error"]) == ("retry_pending", 3, "earlier: provider down")
    monkeypatch.setattr(queue_module, "time", SimpleNamespace(time=lambda: 1234.5))
    await queue.update_batch_status("b1", BatchStatus.PROCESSING, error="provider down")
    doc = docs(queue)["b1"]
    assert (doc["status"], doc["facts_extracted"], doc["last_error"], doc["processing_started_at"]) == \
        ("processing", 0, "provider down", 1234.5)


async def test_a_non_processing_status_does_not_stamp_the_start(queue):
    await put(queue, "b1", 1.0)
    await queue.update_batch_status("b1", BatchStatus.FAILED, error="x")
    assert docs(queue)["b1"]["processing_started_at"] is None


async def test_increment_attempts_counts_and_a_missing_batch_reads_zero_without_writing(queue):
    await put(queue, "b1", 1.0)
    assert [await queue.increment_attempts("b1") for _ in range(2)] == [1, 2]
    assert docs(queue)["b1"]["attempts"] == 2
    assert await queue.increment_attempts("nope") == 0
    assert "nope" not in docs(queue)


async def test_increment_attempts_on_a_document_without_attempts_starts_at_one(queue):
    await put(queue, "b1", 1.0)
    del docs(queue)["b1"]["attempts"]
    assert await queue.increment_attempts("b1") == 1


# --------------------------------------------------------------------------------------------------- stuck users

async def test_stuck_users_are_distinct_and_include_a_gave_up_batch(queue):
    await put(queue, "b1", 1.0)
    await put(queue, "b2", 2.0, BatchStatus.PROCESSING)
    await put(queue, "b3", 3.0, BatchStatus.FAILED, user_id="u2", revivals=MAX_FAILED_REVIVALS)
    assert sorted(await queue.get_stuck_batch_user_ids()) == ["u1", "u2"]


async def test_no_batches_no_stuck_users(queue):
    assert await queue.get_stuck_batch_user_ids() == []
