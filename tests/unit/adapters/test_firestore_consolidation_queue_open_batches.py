"""FirestoreConsolidationQueue: `has_open_batches` and the zombie threshold a lease holder passes (FACT_LIFECYCLE_RFC D9).

The real adapter on the evaluating fake (H1); assertions are on what a reader sees and on the stored documents.
"""
import time
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_consolidation_queue import _ZOMBIE_THRESHOLD_SECONDS, FirestoreConsolidationQueue
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from tests.support.fact_store import INDEXES_FILE
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"
OTHER = "user-other-456"


@pytest.fixture
def queue():
    env = MagicMock()
    env.consolidation_queue_collection = "development_consolidation_queue"
    return FirestoreConsolidationQueue(EvalDb(), env)


async def _put(queue, batch_id, status=BatchStatus.PENDING, user_id=USER, **extra):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=user_id, session_id="s", messages=[{"role": "user"}], status=status, **extra,
    ))


async def _status(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()["status"]


class TestHasOpenBatches:
    async def test_the_query_is_served_with_the_declared_indexes_only(self):
        db = EvalDb(indexes=INDEXES_FILE, groups={"development_consolidation_queue": "development_consolidation_queue"})
        env = MagicMock()
        env.consolidation_queue_collection = "development_consolidation_queue"
        queue = FirestoreConsolidationQueue(db, env)
        await _put(queue, "b1", BatchStatus.PROCESSING, processing_started_at=time.time())
        await _put(queue, "b2", BatchStatus.FAILED, user_id=OTHER)

        assert await queue.has_open_batches(USER) is True
        assert await queue.has_open_batches(OTHER) is False
        assert [b.batch_id for b in await queue.get_pending_batches(USER, limit=1)] == []   # still index-served here

    @pytest.mark.parametrize("status,open_", [
        (BatchStatus.PENDING, True),
        (BatchStatus.RETRY_PENDING, True),
        (BatchStatus.PROCESSING, True),
        (BatchStatus.FAILED, False),
        (BatchStatus.COMPLETED, False),
    ])
    async def test_has_open_batches_per_status(self, queue, status, open_):
        await _put(queue, "b1", status)

        assert await queue.has_open_batches(USER) is open_

    async def test_no_batch_at_all_is_not_open(self, queue):
        assert await queue.has_open_batches(USER) is False

    async def test_another_users_batch_is_not_open_for_this_user(self, queue):
        await _put(queue, "b1", BatchStatus.PENDING, user_id=OTHER)

        assert await queue.has_open_batches(USER) is False
        assert await queue.has_open_batches(OTHER) is True

    async def test_one_open_batch_among_closed_ones_is_enough(self, queue):
        await _put(queue, "failed", BatchStatus.FAILED)
        await _put(queue, "processing", BatchStatus.PROCESSING, processing_started_at=time.time())

        assert await queue.has_open_batches(USER) is True


class TestZombieThreshold:
    async def test_a_batch_59_s_old_is_kept_and_one_61_s_old_is_reset_at_60_s(self, queue):
        now = time.time()
        await _put(queue, "young", BatchStatus.PROCESSING, processing_started_at=now - 59)
        await _put(queue, "old", BatchStatus.PROCESSING, processing_started_at=now - 61)

        reset = await queue.reset_recoverable_batches(USER, zombie_after_s=60)

        assert reset == 1
        assert await _status(queue, "young") == BatchStatus.PROCESSING.value
        assert await _status(queue, "old") == BatchStatus.RETRY_PENDING.value
        old = (await queue.collection.document("old").get()).to_dict()
        # A zombie reset counts the lost run (decisions/consolidation_failed_batch_bounded.md, "Lost runs count"):
        # the batch was stored with attempts 0, so the reset leaves 1, not 0.
        assert old["processing_started_at"] is None and old["attempts"] == 1

    async def test_the_default_threshold_is_unchanged(self, queue):
        assert _ZOMBIE_THRESHOLD_SECONDS == 1800
        now = time.time()
        await _put(queue, "sixty-one", BatchStatus.PROCESSING, processing_started_at=now - 61)
        await _put(queue, "just-under", BatchStatus.PROCESSING, processing_started_at=now - 1790)
        await _put(queue, "over", BatchStatus.PROCESSING, processing_started_at=now - 1810)

        reset = await queue.reset_recoverable_batches(USER)

        assert reset == 1
        assert await _status(queue, "sixty-one") == BatchStatus.PROCESSING.value
        assert await _status(queue, "just-under") == BatchStatus.PROCESSING.value
        assert await _status(queue, "over") == BatchStatus.RETRY_PENDING.value

    async def test_a_batch_without_a_start_stamp_is_a_zombie_under_any_threshold(self, queue):
        await _put(queue, "legacy", BatchStatus.PROCESSING)

        assert await queue.reset_recoverable_batches(USER, zombie_after_s=60) == 1

    async def test_the_threshold_applies_to_processing_batches_only(self, queue):
        await _put(queue, "failed", BatchStatus.FAILED, attempts=3)

        assert await queue.reset_recoverable_batches(USER, zombie_after_s=60) == 0
        assert await _status(queue, "failed") == BatchStatus.FAILED.value

    async def test_the_sweep_keeps_the_default_threshold_while_reviving_failed_batches(self, queue):
        now = time.time()
        await _put(queue, "live", BatchStatus.PROCESSING, processing_started_at=now - 600)
        await _put(queue, "failed", BatchStatus.FAILED, attempts=3)

        assert await queue.reset_recoverable_batches(USER, revive_failed=True) == 1
        assert await _status(queue, "live") == BatchStatus.PROCESSING.value
        assert await _status(queue, "failed") == BatchStatus.RETRY_PENDING.value
