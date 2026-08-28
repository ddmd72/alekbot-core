"""
Wire tests for FirestoreCompanionExtractionQueue.

Mock boundary: Firestore SDK (db_client). Never mock at CompanionExtractionQueue level.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_companion_extraction_queue import (
    FirestoreCompanionExtractionQueue,
    _ZOMBIE_THRESHOLD_SECONDS,
)
from src.config.environment import EnvironmentConfig
from src.domain.consolidation import BatchStatus


def _make_env_config() -> EnvironmentConfig:
    env = MagicMock(spec=EnvironmentConfig)
    env.companion_extraction_queue_collection = "test_companion_extraction_queue"
    return env


def _make_doc(data: dict):
    doc = MagicMock()
    doc.to_dict = MagicMock(return_value=data)
    doc.reference = MagicMock()
    doc.reference.update = AsyncMock(return_value=None)
    return doc


def _make_queue(docs=None):
    db = MagicMock()
    collection = MagicMock()
    db.collection = MagicMock(return_value=collection)

    query = MagicMock()
    query.where = MagicMock(return_value=query)
    query.select = MagicMock(return_value=query)
    query.limit = MagicMock(return_value=query)
    query.get = AsyncMock(return_value=docs or [])
    collection.where = MagicMock(return_value=query)
    collection.select = MagicMock(return_value=query)

    doc_ref = MagicMock()
    doc_ref.set = AsyncMock(return_value=None)
    doc_ref.update = AsyncMock(return_value=None)
    doc_ref.get = AsyncMock(return_value=_make_doc({}))
    doc_ref.delete = AsyncMock(return_value=None)
    collection.document = MagicMock(return_value=doc_ref)

    return FirestoreCompanionExtractionQueue(db_client=db, env_config=_make_env_config()), collection, doc_ref


async def test_enqueue_batch_writes_and_returns_id():
    from src.domain.companion_extraction import CompanionExtractionBatch

    queue, collection, doc_ref = _make_queue()
    batch = CompanionExtractionBatch(
        session_id="slack:C1", account_id="acc-1", companion_type="tutor",
        created_by_user_id="user-1", messages=[],
    )
    batch_id = await queue.enqueue_batch(batch)
    assert batch_id == batch.batch_id
    doc_ref.set.assert_called_once()


async def test_get_pending_batches_filters_by_session_and_status():
    docs = [_make_doc({
        "batch_id": "b1", "session_id": "slack:C1", "account_id": "acc-1",
        "companion_type": "tutor", "created_by_user_id": "user-1", "messages": [],
        "status": BatchStatus.PENDING.value,
    })]
    queue, collection, _ = _make_queue(docs)
    result = await queue.get_pending_batches(session_id="slack:C1", limit=5)
    assert len(result) == 1
    assert result[0].batch_id == "b1"


async def test_update_batch_status_processing_stamps_started_at():
    queue, collection, doc_ref = _make_queue()
    await queue.update_batch_status("b1", BatchStatus.PROCESSING)
    call_kwargs = doc_ref.update.call_args[0][0]
    assert "processing_started_at" in call_kwargs


async def test_reset_recoverable_batches_resets_failed_and_stale_processing():
    now = 2_000_000.0
    docs = [
        _make_doc({"status": BatchStatus.FAILED.value}),
        _make_doc({"status": BatchStatus.PROCESSING.value, "processing_started_at": now - _ZOMBIE_THRESHOLD_SECONDS - 1}),
        _make_doc({"status": BatchStatus.PROCESSING.value, "processing_started_at": now - 5}),
    ]
    queue, collection, _ = _make_queue(docs)
    import time as time_mod
    orig_time = time_mod.time
    time_mod.time = lambda: now
    try:
        reset_count = await queue.reset_recoverable_batches("slack:C1")
    finally:
        time_mod.time = orig_time
    assert reset_count == 2  # FAILED + stale PROCESSING; live PROCESSING skipped


async def test_get_stuck_session_ids_returns_distinct_ids():
    docs = [_make_doc({"session_id": "slack:C1"}), _make_doc({"session_id": "slack:C1"}), _make_doc({"session_id": "slack:C2"})]
    queue, collection, _ = _make_queue(docs)
    ids = await queue.get_stuck_session_ids()
    assert set(ids) == {"slack:C1", "slack:C2"}
