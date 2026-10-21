"""Query shapes and lifecycle writes of FirestoreFactRepository (fact S1 defects)."""
from datetime import timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.cloud import firestore

from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig


@pytest.fixture
def col():
    return MagicMock()


@pytest.fixture
def repo(col):
    db = MagicMock()
    db.collection.return_value = col
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.firestore_collection_prefix = "t_"
    cfg.domain_facts_collection = "t_facts"
    return FirestoreFactRepository(db, cfg)


def _filters(mock_where):
    return [(c.kwargs["filter"].field_path, c.kwargs["filter"].op_string, c.kwargs["filter"].value)
            for c in mock_where.call_args_list]


@pytest.mark.asyncio
async def test_domain_search_newest_first_with_filters(repo, col):
    # where() chain returns the same mock so every call is recorded on col.where
    chain = col.where.return_value
    chain.where.return_value = chain
    ordered = chain.order_by.return_value
    ordered.limit.return_value.get = AsyncMock(return_value=[])

    assert await repo.search_facts_by_domain(["health"], limit=7, account_id="acc") == []

    seen = _filters(col.where) + _filters(chain.where)
    assert ("account_id", "==", "acc") in seen
    assert ("domain", "in", ["health"]) in seen
    assert ("state", "==", "current") in seen
    chain.order_by.assert_called_once_with("created_at", direction=firestore.Query.DESCENDING)
    ordered.limit.assert_called_once_with(7)


@pytest.mark.asyncio
async def test_add_fact_if_unique_prefilters_on_state_not_is_current(repo, col):
    from src.domain.entities import FactEntity

    chain = col.where.return_value
    chain.where.return_value = chain
    chain.find_nearest.return_value.get = AsyncMock(return_value=[])
    repo.add_fact = AsyncMock(return_value="new-id")
    fact = MagicMock(spec=FactEntity)
    fact.vector = [0.1, 0.2]
    fact.account_id = "acc"

    assert await repo.add_fact_if_unique(fact) == (True, "new-id")

    seen = _filters(col.where) + _filters(chain.where)
    assert ("state", "==", "current") in seen
    assert all(f[0] != "is_current" for f in seen)


def _doc_ref(exists=True, account="acc"):
    ref = MagicMock()
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = {"account_id": account}
    ref.get = AsyncMock(return_value=snap)
    ref.update = AsyncMock()
    return ref


@pytest.mark.asyncio
async def test_invalidate_fact_writes_lifecycle_fields(repo, col):
    ref = _doc_ref()
    col.document.return_value = ref

    await repo.invalidate_fact("f1", "acc")

    written = ref.update.call_args.args[0]
    assert written["state"] == "invalidated"
    assert type(written["state"]) is str
    assert written["is_current"] is True
    assert written["valid_to"].tzinfo is timezone.utc
    assert written["last_updated"] == written["valid_to"]


@pytest.mark.asyncio
async def test_invalidate_fact_missing_doc(repo, col):
    col.document.return_value = _doc_ref(exists=False)
    with pytest.raises(ValueError):
        await repo.invalidate_fact("f1", "acc")


@pytest.mark.asyncio
async def test_invalidate_fact_other_account(repo, col):
    ref = _doc_ref(account="other")
    col.document.return_value = ref
    with pytest.raises(PermissionError):
        await repo.invalidate_fact("f1", "acc")
    ref.update.assert_not_called()


@pytest.mark.asyncio
async def test_search_facts_projection_includes_context(repo, col):
    chain = col.where.return_value
    chain.where.return_value = chain
    selected = chain.select.return_value
    selected.find_nearest.return_value.get = AsyncMock(return_value=[])

    await repo.search_facts([0.1, 0.2], account_id="acc", user_id="usr")

    assert "context" in chain.select.call_args.args[0]
