"""CompanionExtractionQueue kit (P11 · G2): the docstring of `src/ports/companion_extraction_queue.py` checked against
`FirestoreCompanionExtractionQueue` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on
the raw stored documents and on what readers return.

No test asserts the unbounded FAILED revival as correct: it is P15-D5, pinned as a strict xfail in
`tests/unit/handlers/test_worker_redelivery_chains.py`. Legacy batches missing a field are not pinned (data audit).
"""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

import src.adapters.firestore_companion_extraction_queue as queue_module
from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
QUEUE = "eval_companion_extraction_queue"
SID, SID2 = "slack:C1", "slack:C2"
MESSAGES = [{"role": "user", "text": "Como se dice 'car service'?"}, {"role": "model", "text": "Revisão do carro."}]


@pytest.fixture
def queue():
    db = EvalDb(indexes=INDEXES_FILE, groups={QUEUE: "development_companion_extraction_queue"})
    return FirestoreCompanionExtractionQueue(db, SimpleNamespace(companion_extraction_queue_collection=QUEUE))


def docs(queue):
    return queue.db.collection(QUEUE).docs


async def put(queue, batch_id, created_at, status=BatchStatus.PENDING, session_id=SID, **extra):
    await queue.enqueue_batch(CompanionExtractionBatch(
        batch_id=batch_id, session_id=session_id, account_id="a1", companion_type="tutor", created_by_user_id="u1",
        messages=MESSAGES, created_at=created_at, status=status, **extra))


def ids(batches):
    return [b.batch_id for b in batches]


async def test_pending_is_oldest_first_and_served_by_the_declared_index(queue):
    # ids sort against time, so only the ordering by created_at gives this order
    await put(queue, "c1", 2.0)
    await put(queue, "c3", 1.0)
    await put(queue, "c2", 3.0, BatchStatus.RETRY_PENDING)
    got = await queue.get_pending_batches(SID)
    assert ids(got) == ["c3", "c1", "c2"]
    assert (got[0].account_id, got[0].companion_type, got[0].messages) == ("a1", "tutor", MESSAGES)


async def test_pending_limit_after_filter_and_other_sessions_excluded(queue):
    await put(queue, "other", 0.1, session_id=SID2)
    await put(queue, "processing", 0.2, BatchStatus.PROCESSING)
    await put(queue, "failed", 0.3, BatchStatus.FAILED)
    for n, t in (("c1", 3.0), ("c2", 2.0), ("c3", 1.0)):
        await put(queue, n, t)
    assert ids(await queue.get_pending_batches(SID, limit=2)) == ["c3", "c2"]
    assert ids(await queue.get_pending_batches(SID2)) == ["other"]


async def test_enqueue_twice_is_one_document_and_delete_is_idempotent(queue):
    await put(queue, "c1", 1.0)
    await put(queue, "c1", 1.0)
    assert list(docs(queue)) == ["c1"]
    await queue.delete_batch("c1")
    await queue.delete_batch("c1")
    assert docs(queue) == {}


async def test_update_status_on_a_missing_batch_raises_not_found(queue):
    raised = None
    try:
        await queue.update_batch_status("nope", BatchStatus.COMPLETED)
    except NotFound as exc:
        raised = exc
    assert raised is not None and docs(queue) == {}


async def test_update_status_writes_records_and_stamps_processing(queue, monkeypatch):
    await put(queue, "c1", 1.0, last_error="earlier")
    await queue.update_batch_status("c1", BatchStatus.RETRY_PENDING, records_extracted=2)
    doc = docs(queue)["c1"]
    assert (doc["status"], doc["records_extracted"], doc["last_error"], doc["processing_started_at"]) == \
        ("retry_pending", 2, "earlier", None)
    monkeypatch.setattr(queue_module, "time", SimpleNamespace(time=lambda: 99.0))
    await queue.update_batch_status("c1", BatchStatus.PROCESSING, error="tutor extractor down")
    doc = docs(queue)["c1"]
    assert (doc["status"], doc["records_extracted"], doc["last_error"], doc["processing_started_at"]) == \
        ("processing", 0, "tutor extractor down", 99.0)


async def test_increment_attempts_counts_and_a_missing_batch_reads_zero(queue):
    await put(queue, "c1", 1.0)
    assert [await queue.increment_attempts("c1") for _ in range(2)] == [1, 2]
    assert await queue.increment_attempts("nope") == 0
    assert list(docs(queue)) == ["c1"]


async def test_a_live_processing_batch_is_kept_and_a_zombie_reset(queue):
    now = time.time()
    await put(queue, "live", 1.0, BatchStatus.PROCESSING, processing_started_at=now - 60, attempts=1)
    await put(queue, "zombie", 2.0, BatchStatus.PROCESSING, processing_started_at=now - 1801, attempts=2,
              last_error="lost")
    await put(queue, "legacy", 3.0, BatchStatus.PROCESSING, attempts=1)        # never stamped: a zombie
    await put(queue, "other", 4.0, BatchStatus.PROCESSING, session_id=SID2, processing_started_at=now - 1801)
    await put(queue, "pending", 5.0)
    assert await queue.reset_recoverable_batches(SID) == 2
    reset = {k: (d["status"], d["attempts"], d["last_error"], d["processing_started_at"])
             for k, d in docs(queue).items()}
    assert reset["zombie"] == reset["legacy"] == ("retry_pending", 0, None, None)
    assert reset["live"] == ("processing", 1, None, now - 60)
    assert reset["other"][0] == "processing" and reset["pending"][0] == "pending"
    assert ids(await queue.get_pending_batches(SID)) == ["zombie", "legacy", "pending"]


async def test_stuck_session_ids_are_distinct(queue):
    await put(queue, "c1", 1.0)
    await put(queue, "c2", 2.0, BatchStatus.FAILED)
    await put(queue, "c3", 3.0, session_id=SID2)
    assert sorted(await queue.get_stuck_session_ids()) == [SID, SID2]
    for k in ("c1", "c2", "c3"):
        await queue.delete_batch(k)
    assert await queue.get_stuck_session_ids() == []
