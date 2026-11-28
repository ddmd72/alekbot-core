"""Query shapes and lifecycle writes of FirestoreFactRepository (fact S1 defects)."""
from datetime import timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig
from src.services.cabinet_fact_service import CabinetFactService
from tests.support.fact_store import NoCacheRefresh


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


def _doc_ref(exists=True, account="acc"):
    ref = MagicMock()
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = {"account_id": account}
    ref.get = AsyncMock(return_value=snap)
    ref.update = AsyncMock()
    return ref


@pytest.mark.asyncio
async def test_invalidate_fact_writes_lifecycle_fields():
    # S2 (RFC D8/D9): invalidate resolves forward and writes in a preconditioned transaction, which a MagicMock
    # database cannot run, so the same written fields are checked on the evaluating fake (stored document).
    from tests.support.fact_kit import ACC, make_fact
    from tests.support.fact_store import FactStore

    store = FactStore()
    await store.repo.add_fact(make_fact("f1"))

    await CabinetFactService(store.repo, bio_cache=NoCacheRefresh()).invalidate("f-f1", ACC)

    written = store.docs["f-f1"]
    assert written["state"] == "invalidated"
    assert type(written["state"]) is str
    assert written["is_current"] is True
    assert written["valid_to"].tzinfo is timezone.utc
    assert written["last_updated"] == written["valid_to"]


@pytest.mark.asyncio
async def test_invalidate_fact_missing_doc(repo, col):
    # Reviewer edit (facts S4, T2c): the Cabinet delete is CabinetFactService over this repository.
    col.document.return_value = _doc_ref(exists=False)
    with pytest.raises(ValueError):
        await CabinetFactService(repo, bio_cache=NoCacheRefresh()).invalidate("f1", "acc")


@pytest.mark.asyncio
async def test_invalidate_fact_other_account():
    # Reviewer edit (facts S4, T2c): the Cabinet delete is CabinetFactService over this repository, which reads the shown
    # document as a FactEntity; a MagicMock snapshot holding only account_id does not validate, so the foreign document is
    # a real one on the evaluating fake, and "no update call" is "the stored document is unchanged".
    import copy

    from tests.support.fact_kit import make_fact
    from tests.support.fact_store import FactStore

    store = FactStore()
    await store.repo.add_fact(make_fact("f1", account="other"))
    before = copy.deepcopy(store.docs)
    with pytest.raises(PermissionError):
        await CabinetFactService(store.repo, bio_cache=NoCacheRefresh()).invalidate("f-f1", "acc")
    assert store.docs == before


@pytest.mark.asyncio
async def test_search_facts_projection_includes_context(repo, col):
    chain = col.where.return_value
    chain.where.return_value = chain
    selected = chain.select.return_value
    selected.find_nearest.return_value.get = AsyncMock(return_value=[])

    await repo.search_facts([0.1, 0.2], account_id="acc", user_id="usr")

    assert "context" in chain.select.call_args.args[0]
