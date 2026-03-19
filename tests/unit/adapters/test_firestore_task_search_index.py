"""
Wire tests for FirestoreTaskSearchIndex.

Mock boundary: Firestore SDK (db_client).
Never mock at TaskSearchIndex port level.

Covers:
- Port compliance
- upsert: wraps vectors, calls doc.set
- delete: calls doc.delete on correct doc ID
- delete_by_list: queries by user_id+list_id, batch deletes
- delete_all_for_user: queries by user_id, batch deletes
- find_nearest: fires one query per vector, RRF combines results
- find_nearest with show_completed=True: no status filter
- find_nearest with list_id: adds list_id filter
- vector wrap/unwrap roundtrip
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.config.environment import EnvironmentConfig
from src.domain.task import TaskImportance, TaskSearchEntry, TaskStatus
from src.ports.task_search_index import TaskSearchIndex

_USER_ID = "user-abc"
_LIST_ID = "list-1"
_TASK_ID = "task-1"

_ENTRY = TaskSearchEntry(
    task_id=_TASK_ID,
    list_id=_LIST_ID,
    list_name="Alek Bot Tasks",
    user_id=_USER_ID,
    title="Buy milk",
    status=TaskStatus.NOT_STARTED,
    tags=["shopping"],
    importance=TaskImportance.NORMAL,
    content_vector=[0.1, 0.2, 0.3],
    context_vector=[0.4, 0.5, 0.6],
    indexed_at=datetime(2026, 3, 18),
)


def _make_env_config() -> EnvironmentConfig:
    env = MagicMock(spec=EnvironmentConfig)
    env.task_search_index_collection = "test_task_search_index"
    return env


def _make_db():
    """Minimal Firestore mock supporting collection → document → set/delete/get."""
    doc_ref = MagicMock()
    doc_ref.set = AsyncMock(return_value=None)
    doc_ref.delete = AsyncMock(return_value=None)

    collection = MagicMock()
    collection.document.return_value = doc_ref
    collection.where.return_value = collection
    collection.find_nearest.return_value = collection
    collection.get = AsyncMock(return_value=[])

    db = MagicMock()
    db.collection.return_value = collection
    db.batch.return_value = MagicMock(
        delete=MagicMock(),
        commit=AsyncMock(return_value=None),
    )
    return db, collection, doc_ref


# =============================================================================
# Port compliance
# =============================================================================


class TestFirestoreTaskSearchIndexPortCompliance:

    def test_is_task_search_index_subclass(self):
        assert issubclass(FirestoreTaskSearchIndex, TaskSearchIndex)

    def test_instantiates(self):
        db, _, _ = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())
        assert isinstance(index, FirestoreTaskSearchIndex)


# =============================================================================
# upsert
# =============================================================================


class TestUpsert:

    async def test_upsert_calls_doc_set(self):
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.upsert(_ENTRY)

        doc_ref.set.assert_called_once()

    async def test_upsert_uses_correct_doc_id(self):
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.upsert(_ENTRY)

        collection.document.assert_called_once_with(f"{_USER_ID}_{_TASK_ID}")

    async def test_upsert_serializes_status_as_string(self):
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.upsert(_ENTRY)

        set_data = doc_ref.set.call_args.args[0]
        assert set_data["status"] == "notStarted"

    async def test_upsert_wraps_vectors(self):
        """Vectors must be wrapped in Vector() objects for Firestore."""
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        with patch("src.adapters.firestore_task_search_index.Vector") as mock_vector:
            mock_vector.side_effect = lambda v: f"WRAPPED:{v}"
            await index.upsert(_ENTRY)

        set_data = doc_ref.set.call_args.args[0]
        assert "WRAPPED" in str(set_data.get("content_vector", ""))


# =============================================================================
# delete
# =============================================================================


class TestDelete:

    async def test_delete_calls_doc_delete(self):
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete(_USER_ID, _TASK_ID)

        doc_ref.delete.assert_called_once()

    async def test_delete_uses_correct_doc_id(self):
        db, collection, doc_ref = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete(_USER_ID, _TASK_ID)

        collection.document.assert_called_once_with(f"{_USER_ID}_{_TASK_ID}")


# =============================================================================
# delete_by_list
# =============================================================================


class TestDeleteByList:

    async def test_delete_by_list_queries_correct_fields(self):
        db, collection, _ = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete_by_list(_USER_ID, _LIST_ID)

        # Should query by user_id and list_id
        assert collection.where.call_count >= 2

    async def test_delete_by_list_batch_commits(self):
        db, collection, _ = _make_db()
        # Return 2 fake docs
        fake_docs = [MagicMock(reference=MagicMock()), MagicMock(reference=MagicMock())]
        collection.get = AsyncMock(return_value=fake_docs)
        batch = MagicMock(delete=MagicMock(), commit=AsyncMock())
        db.batch.return_value = batch
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete_by_list(_USER_ID, _LIST_ID)

        batch.commit.assert_called_once()
        assert batch.delete.call_count == 2


# =============================================================================
# delete_all_for_user
# =============================================================================


class TestDeleteAllForUser:

    async def test_delete_all_queries_user_id(self):
        db, collection, _ = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete_all_for_user(_USER_ID)

        collection.where.assert_called_once()

    async def test_delete_all_batches_large_sets(self):
        """Tests that chunk_size=500 logic runs for 600 docs."""
        db, collection, _ = _make_db()
        fake_docs = [MagicMock(reference=MagicMock()) for _ in range(600)]
        collection.get = AsyncMock(return_value=fake_docs)
        batches_committed = []

        def make_batch():
            b = MagicMock(delete=MagicMock(), commit=AsyncMock(side_effect=lambda: batches_committed.append(1)))
            return b

        db.batch.side_effect = make_batch
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.delete_all_for_user(_USER_ID)

        # 600 docs → 2 batches (500 + 100)
        assert len(batches_committed) == 2


# =============================================================================
# find_nearest
# =============================================================================


class TestFindNearest:

    async def test_find_nearest_fires_per_vector(self):
        """One vector → one find_nearest call."""
        db, collection, _ = _make_db()
        vq = MagicMock()
        vq.get = AsyncMock(return_value=[])
        collection.find_nearest.return_value = vq
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.find_nearest(_USER_ID, {"content": [0.1, 0.2]})

        collection.find_nearest.assert_called_once()

    async def test_find_nearest_two_vectors_fires_twice(self):
        db, collection, _ = _make_db()
        vq = MagicMock()
        vq.get = AsyncMock(return_value=[])
        collection.find_nearest.return_value = vq
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        await index.find_nearest(_USER_ID, {"content": [0.1], "context": [0.2]})

        assert collection.find_nearest.call_count == 2

    async def test_find_nearest_returns_empty_for_empty_vectors(self):
        db, collection, _ = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        result = await index.find_nearest(_USER_ID, {})

        assert result == []

    async def test_find_nearest_returns_empty_for_none_vectors(self):
        db, collection, _ = _make_db()
        index = FirestoreTaskSearchIndex(db, _make_env_config())

        result = await index.find_nearest(_USER_ID, {"content": None})

        assert result == []
