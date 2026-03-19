"""
Wire tests for FirestoreTaskConfigRepository.

Mock boundary: Firestore SDK (db_client).
Never mock at TaskConfigPort level.

Covers:
- Port compliance
- get_config: missing doc → empty TaskUserConfig
- get_config: existing doc → populated TaskUserConfig
- save_config: serializes and calls doc.set
- set_primary_list_id_if_absent: existing primary_list_id → returns existing, no write
- set_primary_list_id_if_absent: absent primary_list_id → writes and returns new value
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.config.environment import EnvironmentConfig
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from src.ports.task_config_port import TaskConfigPort

_USER_ID = "user-abc"
_LIST_ID = "list-1"
_SUB_ID = "sub-1"
_EXPIRES_AT = datetime(2026, 6, 18, 12, 0, 0)


def _make_env_config() -> EnvironmentConfig:
    env = MagicMock(spec=EnvironmentConfig)
    env.task_config_collection = "test_task_config"
    return env


def _make_db_with_doc(doc_data: dict | None):
    """Build a Firestore mock where collection.document().get() returns a snapshot."""
    snapshot = MagicMock()
    snapshot.exists = doc_data is not None
    snapshot.to_dict = MagicMock(return_value=doc_data or {})

    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=snapshot)
    doc_ref.set = AsyncMock(return_value=None)

    collection = MagicMock()
    collection.document.return_value = doc_ref

    # Transaction factory: db.transaction() returns a mock transaction object.
    # @firestore.async_transactional wraps the fn; calling it with the transaction executes it.
    transaction = MagicMock()
    transaction.set = MagicMock()

    db = MagicMock()
    db.collection.return_value = collection
    db.transaction.return_value = transaction

    return db, collection, doc_ref, transaction, snapshot


# =============================================================================
# Port compliance
# =============================================================================


class TestFirestoreTaskConfigRepositoryPortCompliance:

    def test_is_task_config_port_subclass(self):
        assert issubclass(FirestoreTaskConfigRepository, TaskConfigPort)

    def test_instantiates(self):
        db, _, _, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())
        assert isinstance(repo, FirestoreTaskConfigRepository)


# =============================================================================
# get_config
# =============================================================================


class TestGetConfig:

    async def test_get_config_missing_doc_returns_empty(self):
        db, _, _, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.get_config(_USER_ID)

        assert isinstance(result, TaskUserConfig)
        assert result.primary_list_id is None
        assert result.subscriptions == []

    async def test_get_config_uses_correct_doc_id(self):
        db, collection, doc_ref, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        await repo.get_config(_USER_ID)

        collection.document.assert_called_once_with(_USER_ID)

    async def test_get_config_existing_doc_returns_populated(self):
        doc_data = {
            "primary_list_id": _LIST_ID,
            "subscriptions": [
                {"sub_id": _SUB_ID, "list_id": _LIST_ID, "expires_at": _EXPIRES_AT}
            ],
        }
        db, _, _, _, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.get_config(_USER_ID)

        assert result.primary_list_id == _LIST_ID
        assert len(result.subscriptions) == 1
        assert result.subscriptions[0].sub_id == _SUB_ID

    async def test_get_config_strips_timezone_from_expires_at(self):
        from datetime import timezone

        aware_dt = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)
        doc_data = {
            "primary_list_id": _LIST_ID,
            "subscriptions": [
                {"sub_id": _SUB_ID, "list_id": _LIST_ID, "expires_at": aware_dt}
            ],
        }
        db, _, _, _, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.get_config(_USER_ID)

        assert result.subscriptions[0].expires_at.tzinfo is None


# =============================================================================
# save_config
# =============================================================================


class TestSaveConfig:

    async def test_save_config_calls_doc_set(self):
        db, collection, doc_ref, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())
        config = TaskUserConfig(primary_list_id=_LIST_ID, subscriptions=[])

        await repo.save_config(_USER_ID, config)

        doc_ref.set.assert_called_once()

    async def test_save_config_uses_correct_doc_id(self):
        db, collection, doc_ref, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())
        config = TaskUserConfig(primary_list_id=_LIST_ID, subscriptions=[])

        await repo.save_config(_USER_ID, config)

        collection.document.assert_called_with(_USER_ID)

    async def test_save_config_serializes_primary_list_id(self):
        db, collection, doc_ref, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())
        config = TaskUserConfig(primary_list_id=_LIST_ID, subscriptions=[])

        await repo.save_config(_USER_ID, config)

        saved_data = doc_ref.set.call_args.args[0]
        assert saved_data["primary_list_id"] == _LIST_ID

    async def test_save_config_serializes_subscriptions(self):
        db, collection, doc_ref, _, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())
        sub = TaskSubscriptionConfig(sub_id=_SUB_ID, list_id=_LIST_ID, expires_at=_EXPIRES_AT)
        config = TaskUserConfig(primary_list_id=_LIST_ID, subscriptions=[sub])

        await repo.save_config(_USER_ID, config)

        saved_data = doc_ref.set.call_args.args[0]
        assert len(saved_data["subscriptions"]) == 1
        assert saved_data["subscriptions"][0]["sub_id"] == _SUB_ID


# =============================================================================
# set_primary_list_id_if_absent
# =============================================================================


class TestSetPrimaryListIdIfAbsent:

    @pytest.fixture(autouse=True)
    def patch_async_transactional(self):
        """Make firestore.async_transactional a no-op pass-through in unit tests."""
        with patch(
            "src.adapters.firestore_task_config_repository.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    async def test_returns_existing_if_already_set(self):
        existing_list_id = "existing-list"
        doc_data = {"primary_list_id": existing_list_id, "subscriptions": []}
        db, _, _, transaction, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.set_primary_list_id_if_absent(_USER_ID, _LIST_ID)

        assert result == existing_list_id

    async def test_does_not_write_if_already_set(self):
        existing_list_id = "existing-list"
        doc_data = {"primary_list_id": existing_list_id, "subscriptions": []}
        db, _, _, transaction, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        await repo.set_primary_list_id_if_absent(_USER_ID, _LIST_ID)

        transaction.set.assert_not_called()

    async def test_writes_and_returns_new_if_absent(self):
        doc_data = {"subscriptions": []}  # no primary_list_id key
        db, _, doc_ref, transaction, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.set_primary_list_id_if_absent(_USER_ID, _LIST_ID)

        assert result == _LIST_ID
        transaction.set.assert_called_once()

    async def test_writes_with_merge_true(self):
        """merge=True must be used to preserve existing subscriptions."""
        doc_data = {"subscriptions": []}
        db, _, doc_ref, transaction, _ = _make_db_with_doc(doc_data)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        await repo.set_primary_list_id_if_absent(_USER_ID, _LIST_ID)

        call_kwargs = transaction.set.call_args.kwargs
        assert call_kwargs.get("merge") is True

    async def test_writes_and_returns_new_if_doc_missing(self):
        db, _, doc_ref, transaction, _ = _make_db_with_doc(None)
        repo = FirestoreTaskConfigRepository(db, _make_env_config())

        result = await repo.set_primary_list_id_if_absent(_USER_ID, _LIST_ID)

        assert result == _LIST_ID
        transaction.set.assert_called_once()
