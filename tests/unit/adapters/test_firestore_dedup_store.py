"""FirestoreDedupStore — atomic claim + release (used by Cloud Task redelivery dedup)."""
from unittest.mock import AsyncMock, MagicMock

from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_dedup_store import FirestoreDedupStore


def _store(doc_ref):
    db = MagicMock()
    db.collection.return_value.document.return_value = doc_ref
    return FirestoreDedupStore(db_client=db, collection_name="development_worker_task_dedup", ttl_seconds=1800), db


async def test_try_mark_processed_creates_doc_with_expiry():
    doc_ref = MagicMock()
    doc_ref.create = AsyncMock()
    store, db = _store(doc_ref)

    assert await store.try_mark_processed("task-1") is True

    db.collection.assert_called_with("development_worker_task_dedup")
    db.collection.return_value.document.assert_called_with("task-1")
    created = doc_ref.create.await_args.args[0]
    assert {"created_at", "expires_at"} <= set(created)


async def test_try_mark_processed_existing_fresh_doc_is_duplicate():
    import time
    doc_ref = MagicMock()
    doc_ref.create = AsyncMock(side_effect=AlreadyExists("exists"))
    snap = MagicMock(exists=True)
    snap.to_dict.return_value = {"created_at": time.time()}
    doc_ref.get = AsyncMock(return_value=snap)
    store, _ = _store(doc_ref)

    assert await store.try_mark_processed("task-1") is False


async def test_release_deletes_the_doc():
    doc_ref = MagicMock()
    doc_ref.delete = AsyncMock()
    store, db = _store(doc_ref)

    await store.release("task-1")

    db.collection.return_value.document.assert_called_with("task-1")
    doc_ref.delete.assert_awaited_once()


async def test_release_without_id_is_noop():
    doc_ref = MagicMock()
    doc_ref.delete = AsyncMock()
    store, _ = _store(doc_ref)

    await store.release("")

    doc_ref.delete.assert_not_awaited()
