"""
Wire tests for FirestoreChannelBindingAdapter.
Mock at the Firestore SDK boundary (db_client), NOT at the port level,
per ADAPTER_WIRE_TESTING.md.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.config.environment import EnvironmentConfig
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig, CompanionTextMode
from src.domain.entities import FactDomain


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.firestore_collection_prefix = "test_"
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
    return FirestoreChannelBindingAdapter(db_mock, env_config)


def _doc_snapshot(data: dict, exists: bool = True) -> MagicMock:
    doc = MagicMock()
    doc.exists = exists
    doc.to_dict.return_value = data
    return doc


class TestGet:
    async def test_returns_none_when_doc_missing(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_doc_snapshot({}, exists=False))
        col_mock.document.return_value = doc_ref

        result = await adapter.get("C123")
        assert result is None

    async def test_deserializes_binding_without_companion_config(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_doc_snapshot({
            "channel_id": "C123",
            "agent_type": "doc_generator",
            "intent": "create_document",
            "created_by": "user1",
            "companion_config": None,
        }))
        col_mock.document.return_value = doc_ref

        result = await adapter.get("C123")
        assert result == ChannelBinding(
            channel_id="C123", agent_type="doc_generator", intent="create_document",
            created_by="user1",
        )
        assert result.companion_config is None

    async def test_deserializes_binding_with_companion_config(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_doc_snapshot({
            "channel_id": "C123",
            "agent_type": "language_tutor",
            "intent": "tutor_session",
            "created_by": "user1",
            "companion_config": {
                "window_threshold": 100,
                "batch_size": 50,
                "text_mode": "full",
                "include_biographical": True,
                "session_domains": ["education", "skill"],
                "include_standing_directives": True,
                "include_own_records": False,
            },
        }))
        col_mock.document.return_value = doc_ref

        result = await adapter.get("C123")
        assert result.companion_config == CompanionConfig(
            window_threshold=100,
            batch_size=50,
            text_mode=CompanionTextMode.FULL,
            include_biographical=True,
            session_domains=[FactDomain.EDUCATION, FactDomain.SKILL],
            include_standing_directives=True,
            include_own_records=False,
        )


class TestDelete:
    async def test_deletes_document(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.delete = AsyncMock()
        col_mock.document.return_value = doc_ref

        await adapter.delete("C123")

        col_mock.document.assert_called_once_with("C123")
        doc_ref.delete.assert_called_once_with()


class TestSave:
    async def test_writes_companion_config_as_none_when_absent(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref

        binding = ChannelBinding(
            channel_id="C123", agent_type="doc_generator", intent="create_document",
            created_by="user1",
        )
        await adapter.save(binding)

        col_mock.document.assert_called_once_with("C123")
        doc_ref.set.assert_called_once_with({
            "channel_id": "C123",
            "agent_type": "doc_generator",
            "intent": "create_document",
            "created_by": "user1",
            "companion_config": None,
        })

    async def test_writes_serialized_companion_config(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref

        binding = ChannelBinding(
            channel_id="C123", agent_type="language_tutor", intent="tutor_session",
            created_by="user1",
            companion_config=CompanionConfig(
                window_threshold=100,
                batch_size=50,
                text_mode=CompanionTextMode.FULL,
                include_biographical=True,
                session_domains=[FactDomain.EDUCATION, FactDomain.SKILL],
                include_standing_directives=True,
                include_own_records=False,
            ),
        )
        await adapter.save(binding)

        doc_ref.set.assert_called_once_with({
            "channel_id": "C123",
            "agent_type": "language_tutor",
            "intent": "tutor_session",
            "created_by": "user1",
            "companion_config": {
                "window_threshold": 100,
                "batch_size": 50,
                "text_mode": "full",
                "include_biographical": True,
                "session_domains": ["education", "skill"],
                "include_standing_directives": True,
                "include_own_records": False,
            },
        })
