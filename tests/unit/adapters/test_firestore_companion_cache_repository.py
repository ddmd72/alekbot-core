"""
Wire tests for FirestoreCompanionCacheRepository.
Mock at the Firestore SDK boundary (db_client), NOT at the port level,
per ADAPTER_WIRE_TESTING.md.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_companion_cache_repository import (
    FirestoreCompanionCacheRepository,
)
from src.config.environment import EnvironmentConfig


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.companion_context_cache_collection = "test_companion_context_cache"
    return cfg


@pytest.fixture
def col_mock():
    return MagicMock()


@pytest.fixture
def db_mock(col_mock):
    db = MagicMock()
    db.collection.return_value = col_mock
    return db


@pytest.fixture
def adapter(db_mock, env_config):
    return FirestoreCompanionCacheRepository(db_mock, env_config)


def _doc_snapshot(data: dict, exists: bool = True) -> MagicMock:
    doc = MagicMock()
    doc.exists = exists
    doc.to_dict.return_value = data
    return doc


class TestGetSummary:
    async def test_returns_none_when_doc_missing(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_doc_snapshot({}, exists=False))
        col_mock.document.return_value = doc_ref

        result = await adapter.get_summary("slack:C1")
        assert result is None

    async def test_returns_summary_when_present(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(
            return_value=_doc_snapshot({"summary": "Two weeks in, mixing preterite/imperfect"})
        )
        col_mock.document.return_value = doc_ref

        result = await adapter.get_summary("slack:C1")
        assert result == "Two weeks in, mixing preterite/imperfect"


class TestSaveSummary:
    async def test_writes_session_account_and_summary(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref

        await adapter.save_summary("slack:C1", "acc1", "New summary")

        col_mock.document.assert_called_once_with("slack:C1")
        doc_ref.set.assert_called_once_with({
            "session_id": "slack:C1",
            "account_id": "acc1",
            "summary": "New summary",
        })
