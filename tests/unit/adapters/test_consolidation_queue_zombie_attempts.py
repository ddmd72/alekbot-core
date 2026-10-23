"""A zombie reset counts the lost run, so a batch that kills its worker ends FAILED instead of looping.

The real FirestoreConsolidationQueue on the evaluating fake (H1). Decision: decisions/consolidation_failed_batch_bounded.md
"""
import logging
import time
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.consolidation import MAX_BATCH_ATTEMPTS, MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"
# the two ways a caller asks: the lease holder's 60 s threshold and the default (sweep) one
PATHS = [{"zombie_after_s": 60}, {}]


@pytest.fixture
def queue():
    env = MagicMock()
    env.consolidation_queue_collection = "development_consolidation_queue"
    return FirestoreConsolidationQueue(EvalDb(), env)


def _old(seconds_over=100):
    return time.time() - 3600 - seconds_over       # older than every threshold in use


async def _put(queue, batch_id, status=BatchStatus.PROCESSING, started=None, **extra):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=USER, session_id="s", messages=[{"role": "user", "parts": [{"text": "x"}]}],
        status=status, processing_started_at=_old() if started is None and status == BatchStatus.PROCESSING else started,
        **extra,
    ))


async def _doc(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()


@pytest.mark.parametrize("path", PATHS)
async def test_a_zombie_reset_keeps_and_increments_attempts(queue, path):
    await _put(queue, "b1", attempts=1, last_error="old")

    assert await queue.reset_recoverable_batches(USER, **path) == 1

    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.RETRY_PENDING.value
    assert doc["attempts"] == 2 and doc["processing_started_at"] is None and doc["last_error"] is None


@pytest.mark.parametrize("path", PATHS)
async def test_the_reset_that_reaches_the_bound_fails_the_batch_and_keeps_its_messages(queue, path, caplog):
    await _put(queue, "b1", attempts=MAX_BATCH_ATTEMPTS - 1)

    with caplog.at_level(logging.ERROR):
        assert await queue.reset_recoverable_batches(USER, **path) == 1

    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.FAILED.value and doc["attempts"] == MAX_BATCH_ATTEMPTS
    assert "run lost" in doc["last_error"] and doc["processing_started_at"] is None
    assert doc["messages"] == [{"role": "user", "parts": [{"text": "x"}]}]
    failed = [r.getMessage() for r in caplog.records if "CONSOLIDATION_FAILED" in r.getMessage()]
    assert len(failed) == 1 and "batch_id=b1" in failed[0] and "attempts=3" in failed[0]
    assert not [r for r in caplog.records if "CONSOLIDATION_GAVE_UP" in r.getMessage()]


async def test_a_batch_failed_past_the_revival_cap_logs_gave_up(queue, caplog):
    await _put(queue, "b1", attempts=MAX_BATCH_ATTEMPTS - 1, revivals=MAX_FAILED_REVIVALS)

    with caplog.at_level(logging.ERROR):
        await queue.reset_recoverable_batches(USER, zombie_after_s=60)

    gave_up = [r.getMessage() for r in caplog.records if "CONSOLIDATION_GAVE_UP" in r.getMessage()]
    assert len(gave_up) == 1 and f"revivals={MAX_FAILED_REVIVALS}" in gave_up[0] and USER not in gave_up[0]


async def test_a_legacy_zombie_without_a_stamp_is_counted_too(queue):
    await _put(queue, "b1", attempts=0)
    await queue.collection.document("b1").update({"processing_started_at": None})

    await queue.reset_recoverable_batches(USER, zombie_after_s=60)

    assert (await _doc(queue, "b1"))["attempts"] == 1


@pytest.mark.parametrize("path", PATHS)
async def test_a_live_batch_is_not_counted(queue, path):
    await _put(queue, "b1", started=time.time() - 5, attempts=1)

    assert await queue.reset_recoverable_batches(USER, **path) == 0

    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.PROCESSING.value and doc["attempts"] == 1


async def test_a_batch_failed_by_zombie_resets_is_not_reset_again_and_the_sweep_revives_it_bounded(queue):
    await _put(queue, "b1", attempts=MAX_BATCH_ATTEMPTS - 1)
    await queue.reset_recoverable_batches(USER, zombie_after_s=60)

    assert await queue.reset_recoverable_batches(USER, zombie_after_s=60) == 0       # per-task path never revives FAILED
    assert (await _doc(queue, "b1"))["status"] == BatchStatus.FAILED.value

    assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 1      # the sweep does, once per round
    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.RETRY_PENDING.value and doc["attempts"] == 0 and doc["revivals"] == 1
