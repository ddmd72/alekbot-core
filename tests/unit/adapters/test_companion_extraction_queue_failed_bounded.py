"""A FAILED companion batch is revived at most MAX_FAILED_REVIVALS times (the `revivals` counter, never reset).

Real FirestoreCompanionExtractionQueue over the evaluating Firestore fake with the index refusal on; assertions are on
the stored documents. Mirror of tests/unit/adapters/test_consolidation_queue_failed_bounded.py.
"""
import time

from src.domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus
from tests.unit.adapters.test_companion_extraction_queue_store_kit import SID, SID2, docs, put, queue  # noqa: F401


async def test_a_failed_batch_is_revived_and_the_revival_counted(queue):  # noqa: F811
    await put(queue, "c1", 1.0, BatchStatus.FAILED, attempts=3, last_error="extractor unavailable")
    assert await queue.reset_recoverable_batches(SID) == 1
    doc = docs(queue)["c1"]
    assert (doc["status"], doc["attempts"], doc["revivals"], doc["last_error"]) == ("retry_pending", 0, 1, None)


async def test_revivals_survive_a_new_failure_and_stop_at_the_cap(queue):  # noqa: F811
    await put(queue, "c1", 1.0, BatchStatus.FAILED, attempts=3, last_error="extractor unavailable")
    for expected in range(1, MAX_FAILED_REVIVALS + 1):
        assert await queue.reset_recoverable_batches(SID) == 1
        assert docs(queue)["c1"]["revivals"] == expected
        await queue.update_batch_status("c1", BatchStatus.FAILED, error="extractor unavailable")
    assert await queue.reset_recoverable_batches(SID) == 0
    doc = docs(queue)["c1"]
    assert (doc["status"], doc["revivals"], doc["last_error"]) == ("failed", MAX_FAILED_REVIVALS, "extractor unavailable")
    assert len(doc["messages"]) == 2


async def test_a_legacy_failed_batch_without_revivals_is_revived(queue):  # noqa: F811
    await put(queue, "c1", 1.0, BatchStatus.FAILED, attempts=3)
    del docs(queue)["c1"]["revivals"]
    assert await queue.reset_recoverable_batches(SID) == 1
    assert docs(queue)["c1"]["revivals"] == 1


async def test_a_zombie_reset_counts_no_revival(queue):  # noqa: F811
    await put(queue, "z", 1.0, BatchStatus.PROCESSING, processing_started_at=time.time() - 1801, attempts=2)
    assert await queue.reset_recoverable_batches(SID) == 1
    doc = docs(queue)["z"]
    assert (doc["status"], doc["revivals"]) == ("retry_pending", 0)


async def test_other_sessions_are_not_touched(queue):  # noqa: F811
    await put(queue, "other", 1.0, BatchStatus.FAILED, session_id=SID2, attempts=3)
    assert await queue.reset_recoverable_batches(SID) == 0
    doc = docs(queue)["other"]
    assert (doc["status"], doc["revivals"]) == ("failed", 0)
