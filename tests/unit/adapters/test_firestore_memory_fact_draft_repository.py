from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_memory_fact_draft_repository import (
    DRAFT_TTL,
    FirestoreMemoryFactDraftRepository,
)
from src.config.environment import EnvironmentConfig
from src.domain.memory_fact import MemoryFactDraft, ProposedFact

_DRAFT = MemoryFactDraft(facts=[ProposedFact(text="User was born in 1980.", source="passport.pdf")])


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.memory_fact_drafts_collection = "test_memory_fact_drafts"
    return cfg


@pytest.fixture
def doc():
    d = MagicMock()
    d.create = AsyncMock()
    d.get = AsyncMock()
    d.delete = AsyncMock()
    return d


@pytest.fixture
def db(doc):
    client = MagicMock()
    client.collection.return_value.document.return_value = doc
    return client


@pytest.fixture
def repo(db, env_config):
    return FirestoreMemoryFactDraftRepository(db, env_config)


def _snap(data, exists=True):
    s = MagicMock()
    s.exists = exists
    s.to_dict.return_value = data
    return s


def test_collection_and_doc_id(db, repo):
    db.collection.assert_called_with("test_memory_fact_drafts")


def test_env_config_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.memory_fact_drafts_collection == f"{cfg.firestore_collection_prefix}memory_fact_drafts"


async def test_create_writes_facts_and_expiry(db, repo, doc):
    before = datetime.now(timezone.utc)

    assert await repo.create_draft("u1", "a1b2", _DRAFT) is True

    db.collection.return_value.document.assert_called_with("u1:a1b2")
    data = doc.create.call_args.args[0]
    assert data["user_id"] == "u1"
    assert data["facts"] == [{"text": "User was born in 1980.", "source": "passport.pdf"}]
    assert data["expires_at"] - data["created_at"] == DRAFT_TTL
    assert data["created_at"] >= before


async def test_create_on_taken_code_returns_false(repo, doc):
    doc.create.side_effect = AlreadyExists("taken")
    assert await repo.create_draft("u1", "a1b2", _DRAFT) is False


async def test_get_returns_the_draft(repo, doc):
    doc.get.return_value = _snap({
        "facts": [{"text": "User was born in 1980.", "source": "passport.pdf"}],
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    })
    assert await repo.get_draft("u1", "a1b2") == _DRAFT


async def test_get_missing_is_none(repo, doc):
    doc.get.return_value = _snap(None, exists=False)
    assert await repo.get_draft("u1", "a1b2") is None


async def test_get_expired_is_none_even_before_ttl_deletion(repo, doc):
    doc.get.return_value = _snap({
        "facts": [{"text": "User was born in 1980.", "source": "passport.pdf"}],
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    })
    assert await repo.get_draft("u1", "a1b2") is None


async def test_get_naive_expiry_is_read_as_utc(repo, doc):
    doc.get.return_value = _snap({
        "facts": [{"text": "User was born in 1980.", "source": "passport.pdf"}],
        "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    })
    assert await repo.get_draft("u1", "a1b2") is None


async def test_get_corrupt_is_none(repo, doc):
    doc.get.return_value = _snap({"facts": [{"text": "", "source": "x"}]})
    assert await repo.get_draft("u1", "a1b2") is None


async def test_delete(db, repo, doc):
    await repo.delete_draft("u1", "a1b2")
    db.collection.return_value.document.assert_called_with("u1:a1b2")
    doc.delete.assert_awaited_once()


async def test_origin_session_id_is_stored_and_read_back(repo, doc):
    draft = _DRAFT.model_copy(update={"origin_session_id": "u1:C1"})
    await repo.create_draft("u1", "ab12", draft)
    assert doc.create.call_args.args[0]["origin_session_id"] == "u1:C1"

    doc.get.return_value = _snap({"facts": [f.model_dump() for f in draft.facts], "origin_session_id": "u1:C1"})
    assert await repo.get_draft("u1", "ab12") == draft


async def test_draft_without_origin_session_id_reads_as_none(repo, doc):
    doc.get.return_value = _snap({"facts": [f.model_dump() for f in _DRAFT.facts]})
    assert (await repo.get_draft("u1", "ab12")).origin_session_id is None
