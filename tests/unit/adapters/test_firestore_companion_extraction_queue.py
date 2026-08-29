"""
Wire tests for FirestoreCompanionExtractionQueue.

Mock boundary: Firestore SDK (db_client). Never mock at CompanionExtractionQueue level.

Covers the sweep-scheduler additions:
- update_batch_status: PROCESSING stamps processing_started_at; other statuses don't
- reset_recoverable_batches: FAILED always reset; zombie PROCESSING reset; LIVE PROCESSING skipped
- get_stuck_session_ids: distinct session_ids via projection
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_companion_extraction_queue import (
    FirestoreCompanionExtractionQueue,
    _ZOMBIE_THRESHOLD_SECONDS,
)
from src.config.environment import EnvironmentConfig
from src.domain.consolidation import BatchStatus
from src.ports.companion_extraction_queue import CompanionExtractionQueue

_NOW = 1_000_000.0


def _make_env_config() -> EnvironmentConfig:
    env = MagicMock(spec=EnvironmentConfig)
    env.companion_extraction_queue_collection = "test_companion_extraction_queue"
    return env


def _make_doc(data: dict):
    """A Firestore query-result doc snapshot with a writable .reference."""
    doc = MagicMock()
    doc.to_dict = MagicMock(return_value=data)
    doc.reference = MagicMock()
    doc.reference.update = AsyncMock(return_value=None)
    return doc


def _make_queue(docs=None):
    """Build the adapter with a Firestore mock.

    where(...).where(...) and select(...) all resolve to a query whose get()
    returns `docs`. document() returns a doc_ref with async set/update/delete.
    """
    query = MagicMock()
    query.where = MagicMock(return_value=query)
    query.select = MagicMock(return_value=query)
    query.limit = MagicMock(return_value=query)
    query.get = AsyncMock(return_value=docs or [])

    doc_ref = MagicMock()
    doc_ref.set = AsyncMock(return_value=None)
    doc_ref.update = AsyncMock(return_value=None)
    doc_ref.delete = AsyncMock(return_value=None)

    collection = MagicMock()
    collection.where = MagicMock(return_value=query)
    collection.select = MagicMock(return_value=query)
    collection.document = MagicMock(return_value=doc_ref)

    db = MagicMock()
    db.collection = MagicMock(return_value=collection)

    queue = FirestoreCompanionExtractionQueue(db, _make_env_config())
    return queue, collection, query, doc_ref


# =============================================================================
# Port compliance
# =============================================================================


class TestPortCompliance:
    def test_is_companion_extraction_queue_subclass(self):
        assert issubclass(FirestoreCompanionExtractionQueue, CompanionExtractionQueue)


# =============================================================================
# enqueue_batch — basic write operation
# =============================================================================


class TestEnqueueBatch:
    @pytest.mark.asyncio
    async def test_enqueue_batch_writes_and_returns_id(self):
        from src.domain.companion_extraction import CompanionExtractionBatch

        queue, _collection, _query, doc_ref = _make_queue()
        batch = CompanionExtractionBatch(
            session_id="slack:C1", account_id="acc-1", companion_type="tutor",
            created_by_user_id="user-1", messages=[],
        )
        batch_id = await queue.enqueue_batch(batch)
        assert batch_id == batch.batch_id
        doc_ref.set.assert_called_once()


# =============================================================================
# get_pending_batches — filtering and retrieval
# =============================================================================


class TestGetPendingBatches:
    @pytest.mark.asyncio
    async def test_get_pending_batches_filters_by_session_and_status(self):
        docs = [_make_doc({
            "batch_id": "b1", "session_id": "slack:C1", "account_id": "acc-1",
            "companion_type": "tutor", "created_by_user_id": "user-1", "messages": [],
            "status": BatchStatus.PENDING.value,
        })]
        queue, _collection, _query, _doc_ref = _make_queue(docs)
        result = await queue.get_pending_batches(session_id="slack:C1", limit=5)
        assert len(result) == 1
        assert result[0].batch_id == "b1"


# =============================================================================
# update_batch_status — processing_started_at stamping
# =============================================================================


class TestUpdateBatchStatusStamping:
    @pytest.mark.asyncio
    async def test_processing_stamps_started_at(self):
        queue, _coll, _q, doc_ref = _make_queue()
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            await queue.update_batch_status("b1", BatchStatus.PROCESSING)
        update_arg = doc_ref.update.call_args[0][0]
        assert update_arg["status"] == BatchStatus.PROCESSING.value
        assert update_arg["processing_started_at"] == _NOW

    @pytest.mark.asyncio
    async def test_non_processing_does_not_stamp(self):
        queue, _coll, _q, doc_ref = _make_queue()
        await queue.update_batch_status("b1", BatchStatus.FAILED, error="boom")
        update_arg = doc_ref.update.call_args[0][0]
        assert "processing_started_at" not in update_arg
        assert update_arg["last_error"] == "boom"


# =============================================================================
# reset_recoverable_batches — age-guarded zombie reset
# =============================================================================


class TestResetRecoverableBatches:
    @pytest.mark.asyncio
    async def test_failed_batch_always_reset(self):
        doc = _make_doc({"status": BatchStatus.FAILED.value})
        queue, _coll, _q, _ref = _make_queue(docs=[doc])
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            count = await queue.reset_recoverable_batches("slack:C1")
        assert count == 1
        update_arg = doc.reference.update.call_args[0][0]
        assert update_arg["status"] == BatchStatus.RETRY_PENDING.value
        assert update_arg["attempts"] == 0
        assert update_arg["last_error"] is None
        assert update_arg["processing_started_at"] is None

    @pytest.mark.asyncio
    async def test_live_processing_batch_skipped(self):
        # processing_started_at within the threshold → live run, must NOT be touched.
        doc = _make_doc({
            "status": BatchStatus.PROCESSING.value,
            "processing_started_at": _NOW - 10,
        })
        queue, _coll, _q, _ref = _make_queue(docs=[doc])
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            count = await queue.reset_recoverable_batches("slack:C1")
        assert count == 0
        doc.reference.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_zombie_processing_batch_reset(self):
        # processing_started_at older than the threshold → zombie, reset it.
        doc = _make_doc({
            "status": BatchStatus.PROCESSING.value,
            "processing_started_at": _NOW - _ZOMBIE_THRESHOLD_SECONDS - 1,
        })
        queue, _coll, _q, _ref = _make_queue(docs=[doc])
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            count = await queue.reset_recoverable_batches("slack:C1")
        assert count == 1
        doc.reference.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_processing_without_timestamp_reset(self):
        # No processing_started_at (predates stamping) → treated as zombie.
        doc = _make_doc({"status": BatchStatus.PROCESSING.value})
        queue, _coll, _q, _ref = _make_queue(docs=[doc])
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            count = await queue.reset_recoverable_batches("slack:C1")
        assert count == 1
        doc.reference.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_mixed_counts_only_reset_batches(self):
        failed = _make_doc({"status": BatchStatus.FAILED.value})
        live = _make_doc({
            "status": BatchStatus.PROCESSING.value,
            "processing_started_at": _NOW - 5,
        })
        queue, _coll, _q, _ref = _make_queue(docs=[failed, live])
        with patch("src.adapters.firestore_companion_extraction_queue.time.time", return_value=_NOW):
            count = await queue.reset_recoverable_batches("slack:C1")
        assert count == 1
        failed.reference.update.assert_called_once()
        live.reference.update.assert_not_called()


# =============================================================================
# get_stuck_session_ids — distinct via projection
# =============================================================================


class TestGetStuckSessionIds:
    @pytest.mark.asyncio
    async def test_returns_distinct_session_ids(self):
        docs = [
            _make_doc({"session_id": "slack:C1"}),
            _make_doc({"session_id": "slack:C2"}),
            _make_doc({"session_id": "slack:C1"}),  # duplicate
            _make_doc({}),                          # missing session_id → ignored
        ]
        queue, collection, query, _ref = _make_queue(docs=docs)
        result = await queue.get_stuck_session_ids()
        assert sorted(result) == ["slack:C1", "slack:C2"]
        collection.select.assert_called_once_with(["session_id"])

    @pytest.mark.asyncio
    async def test_empty_queue_returns_empty_list(self):
        queue, _coll, _q, _ref = _make_queue(docs=[])
        result = await queue.get_stuck_session_ids()
        assert result == []
