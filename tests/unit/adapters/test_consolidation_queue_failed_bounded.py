"""A FAILED consolidation batch is revived only by the sweep, at most MAX_FAILED_REVIVALS times.

Real FirestoreConsolidationQueue over the evaluating Firestore fake (H1): assertions are on the stored documents.
Decision: docs/04_solution_strategy/decisions/consolidation_failed_batch_bounded.md
"""
import time
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_consolidation_queue import (
    FirestoreConsolidationQueue,
    _ZOMBIE_THRESHOLD_SECONDS,
)
from src.domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from tests.support.firestore_eval import EvalDb

USER = "user-1"


@pytest.fixture
def queue():
    env = MagicMock()
    env.consolidation_queue_collection = "development_consolidation_queue"
    return FirestoreConsolidationQueue(EvalDb(), env)


async def _put(queue, batch_id, status, created_at=1.0, user=USER, **extra):
    batch = ConsolidationBatch(
        batch_id=batch_id, user_id=user, session_id="s", messages=[{"role": "user"}],
        status=status, created_at=created_at, attempts=3 if status == BatchStatus.FAILED else 0,
        last_error="boom" if status == BatchStatus.FAILED else None, **extra,
    )
    await queue.enqueue_batch(batch)


async def _doc(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()


async def test_default_reset_leaves_failed_alone(queue):
    await _put(queue, "f", BatchStatus.FAILED)
    assert await queue.reset_recoverable_batches(USER) == 0
    doc = await _doc(queue, "f")
    assert doc["status"] == BatchStatus.FAILED.value
    assert doc["attempts"] == 3 and doc["revivals"] == 0 and doc["last_error"] == "boom"


async def test_failed_batch_is_not_pending_so_newer_batches_proceed(queue):
    await _put(queue, "old-failed", BatchStatus.FAILED, created_at=1.0)
    await _put(queue, "new", BatchStatus.PENDING, created_at=2.0)
    await queue.reset_recoverable_batches(USER)
    pending = await queue.get_pending_batches(USER, limit=1)
    assert [b.batch_id for b in pending] == ["new"]


async def test_sweep_revives_failed_and_counts_the_revival(queue):
    await _put(queue, "f", BatchStatus.FAILED)
    assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 1
    doc = await _doc(queue, "f")
    assert doc["status"] == BatchStatus.RETRY_PENDING.value
    assert doc["attempts"] == 0 and doc["last_error"] is None and doc["revivals"] == 1
    assert doc["messages"] == [{"role": "user"}]


async def test_revivals_survive_a_new_failure_and_stop_at_the_cap(queue):
    await _put(queue, "f", BatchStatus.FAILED)
    for expected in range(1, MAX_FAILED_REVIVALS + 1):
        assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 1
        assert (await _doc(queue, "f"))["revivals"] == expected
        await queue.update_batch_status("f", BatchStatus.FAILED, error="again")   # the round failed
    before = await _doc(queue, "f")
    assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 0
    assert await _doc(queue, "f") == before
    assert before["status"] == BatchStatus.FAILED.value and before["messages"] == [{"role": "user"}]
    assert await queue.get_pending_batches(USER) == []


async def test_legacy_failed_without_revivals_field_is_revived(queue):
    await queue.collection.document("legacy").set({
        "batch_id": "legacy", "user_id": USER, "session_id": "s", "messages": [],
        "created_at": 1.0, "status": "failed", "attempts": 3,
    })
    assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 1
    assert (await _doc(queue, "legacy"))["revivals"] == 1


@pytest.mark.parametrize("revive_failed", [False, True])
async def test_zombie_processing_revived_on_both_paths_without_counting(queue, revive_failed):
    stale = time.time() - _ZOMBIE_THRESHOLD_SECONDS - 5
    await _put(queue, "z", BatchStatus.PROCESSING, processing_started_at=stale)
    assert await queue.reset_recoverable_batches(USER, revive_failed=revive_failed) == 1
    doc = await _doc(queue, "z")
    assert doc["status"] == BatchStatus.RETRY_PENDING.value
    assert doc["processing_started_at"] is None and doc["revivals"] == 0


@pytest.mark.parametrize("revive_failed", [False, True])
async def test_live_processing_untouched_on_both_paths(queue, revive_failed):
    await _put(queue, "live", BatchStatus.PROCESSING, processing_started_at=time.time() - 10)
    assert await queue.reset_recoverable_batches(USER, revive_failed=revive_failed) == 0
    assert (await _doc(queue, "live"))["status"] == BatchStatus.PROCESSING.value


async def test_other_users_batches_are_not_touched(queue):
    await _put(queue, "mine", BatchStatus.FAILED)
    await _put(queue, "theirs", BatchStatus.FAILED, user="user-2")
    await queue.reset_recoverable_batches(USER, revive_failed=True)
    assert (await _doc(queue, "theirs"))["status"] == BatchStatus.FAILED.value
