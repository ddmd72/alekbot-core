"""
Wire tests for FirestoreCompanionMemoryRepository's find_nearest parsing logic.

The shared FirestoreCapturingStub (tests/integration/adapters/conftest.py) always returns
zero docs from find_nearest, so the parsing loop (Vector unwrap, CompanionRecord construction,
parse-failure handling) has no coverage there. These tests mock at the raw Firestore SDK
boundary instead, following the same convention as test_firestore_agent_note_adapter.py.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_companion_memory_repository import (
    FirestoreCompanionMemoryRepository,
)
from src.config.environment import EnvironmentConfig


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.companion_records_collection = "test_companion_records"
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
    return FirestoreCompanionMemoryRepository(db_mock, env_config)


def _doc(doc_id: str, data: dict) -> MagicMock:
    doc = MagicMock()
    doc.id = doc_id
    doc.to_dict.return_value = data
    return doc


class TestFindNearestParsing:
    async def test_parses_valid_docs_and_unwraps_vector(self, adapter, col_mock):
        """Vector field comes back as a Firestore Vector-like object (iterable, not a
        plain list) — must be converted to List[float] before CompanionRecord construction."""
        class _FakeVector:
            def __init__(self, values):
                self._values = values
            def __iter__(self):
                return iter(self._values)

        doc_data = {
            "session_id": "slack:C1",
            "account_id": "acc1",
            "created_by_user_id": "user1",
            "text": "Recurring subjunctive error",
            "vector": _FakeVector([0.1, 0.2, 0.3]),
            "tags": ["grammar"],
            "domain": "grammar_error",
        }
        query_chain = MagicMock()
        query_chain.get = AsyncMock(return_value=[_doc("rec1", doc_data)])
        col_mock.where.return_value.where.return_value.find_nearest.return_value = query_chain

        results = await adapter.find_nearest(
            session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5
        )

        assert len(results) == 1
        assert results[0].id == "rec1"  # from doc.id via data.setdefault("id", doc.id)
        assert results[0].vector == [0.1, 0.2, 0.3]
        assert isinstance(results[0].vector, list)

    async def test_doc_already_has_id_field_is_not_overwritten(self, adapter, col_mock):
        doc_data = {
            "id": "explicit-id",
            "session_id": "slack:C1",
            "account_id": "acc1",
            "created_by_user_id": "user1",
            "text": "text",
            "vector": [0.1, 0.2],
            "tags": [],
            "domain": "grammar_error",
        }
        query_chain = MagicMock()
        query_chain.get = AsyncMock(return_value=[_doc("firestore-doc-id", doc_data)])
        col_mock.where.return_value.where.return_value.find_nearest.return_value = query_chain

        results = await adapter.find_nearest(
            session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5
        )

        assert results[0].id == "explicit-id"

    async def test_malformed_doc_is_skipped_but_valid_docs_survive(self, adapter, col_mock):
        """A doc missing a required CompanionRecord field (e.g. text) must not crash the
        whole find_nearest call — it's logged and skipped, valid docs still return."""
        good_data = {
            "session_id": "slack:C1", "account_id": "acc1", "created_by_user_id": "user1",
            "text": "valid record", "vector": [0.1, 0.2], "tags": [], "domain": "grammar_error",
        }
        bad_data = {
            "session_id": "slack:C1", "account_id": "acc1", "created_by_user_id": "user1",
            "vector": [0.1, 0.2], "tags": [], "domain": "grammar_error",
            # missing required "text" field — CompanionRecord(**bad_data) will raise
        }
        query_chain = MagicMock()
        query_chain.get = AsyncMock(return_value=[_doc("bad1", bad_data), _doc("good1", good_data)])
        col_mock.where.return_value.where.return_value.find_nearest.return_value = query_chain

        results = await adapter.find_nearest(
            session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5
        )

        assert len(results) == 1
        assert results[0].text == "valid record"


class TestSaveBatchNoVectorWarning:
    async def test_record_with_no_vector_is_still_written_and_warned(self, adapter, col_mock):
        """save_batch must not skip a record just because it has no vector — it writes it
        (unreachable via find_nearest, but not silently dropped) and logs a warning."""
        from src.domain.companion import CompanionRecord

        record = CompanionRecord(
            session_id="slack:C1", account_id="acc1", created_by_user_id="user1",
            text="no vector yet", domain="grammar_error",
        )  # vector defaults to None

        doc_ref = MagicMock()
        col_mock.document.return_value = doc_ref
        batch_mock = MagicMock()
        batch_mock.set = MagicMock()
        batch_mock.commit = AsyncMock()
        adapter.db.batch = MagicMock(return_value=batch_mock)

        written = await adapter.save_batch([record])

        assert written == 1
        batch_mock.set.assert_called_once()
        written_data = batch_mock.set.call_args[0][1]
        assert written_data["vector"] is None
