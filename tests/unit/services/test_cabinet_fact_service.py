"""CabinetFactService (S4 T2c, hexagonal review H1): the owner's Cabinet delete, over the real repository on the
evaluating Firestore fake with the real biographical cache (`make_fact_world`). Every assertion reads the stored documents
and the stored cache, not a call."""
import copy
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import ContextPriority, FactDomain, FactState
from src.domain.exceptions import FactResolutionError, FactWriteConflict
from src.services.biographical_context_service import BiographicalContextService
from src.services.cabinet_fact_service import CabinetFactService
from tests.support.fact_kit import ACC, OTHER, T0, make_fact
from tests.support.fact_lifecycle_world import make_fact_world
from tests.support.fact_store import FACTS, make_env_config

CACHE = "eval_user_context"


class CountingRepo(FirestoreFactRepository):
    """The real repository; it keeps the accounts whose biographical cache it refreshed."""

    refreshed: list

    async def refresh_biographical_context_cache(self, owner_id, facts_limit=None, principles_limit=None):
        self.refreshed.append(owner_id)
        await super().refresh_biographical_context_cache(owner_id, facts_limit, principles_limit)


@pytest.fixture
def world():
    store, _, _ = make_fact_world()
    bio = BiographicalContextService(store.repo)
    repo = CountingRepo(store.db, make_env_config(), embedding_service=store.embedder, biographical_context_service=bio)
    repo.refreshed = []
    bio.set_repository(repo)
    return store, repo, CabinetFactService(repo)


def _bio(name: str, **over):
    return make_fact(name, domain=FactDomain.BIOGRAPHICAL, context_priority=ContextPriority.HIGH, **over)


async def _seed(repo, *facts):
    for fact in facts:
        await repo.add_fact(fact)


def _cached_texts(store):
    cache = store.db.collection(CACHE).docs.get(ACC)
    return None if cache is None else [f["text"] for f in cache["biographical_facts"]]


class TestCabinetFactService:
    async def test_the_fact_is_invalidated_with_its_trail_entry_and_leaves_the_cache_once(self, world):
        store, repo, service = world
        await _seed(repo, _bio("lives in Springfield"), _bio("has a cat"))
        started = datetime.now(timezone.utc)

        await service.invalidate("f-lives in Springfield", ACC)

        doc = store.docs["f-lives in Springfield"]
        assert (doc["state"], doc["is_current"], doc["version"]) == ("invalidated", True, 1)
        assert started <= doc["valid_to"] <= datetime.now(timezone.utc) and doc["last_updated"] == doc["valid_to"]
        assert doc["lifecycle_events"] == [{"at": doc["valid_to"], "from": "current", "to": "invalidated",
                                            "actor": "cabinet", "reason": "deleted in the Cabinet"}]
        assert store.docs["f-has a cat"]["state"] == "current"
        assert repo.refreshed == [ACC]
        assert _cached_texts(store) == ["has a cat"]

    async def test_a_stale_row_reaches_the_current_version_with_the_same_text(self, world):
        store, repo, service = world
        await _seed(
            repo,
            _bio("v1", text="lives in Springfield", lineage_id="L", state=FactState.SUPERSEDED, is_current=False,
                 valid_to=T0, superseded_by="f-v2"),
            _bio("v2", text="lives in Springfield", lineage_id="L", version=2),
        )
        v1_before = copy.deepcopy(store.docs["f-v1"])

        await service.invalidate("f-v1", ACC)

        assert store.docs["f-v2"]["state"] == "invalidated" and store.docs["f-v1"] == v1_before

    async def test_another_account_is_refused_with_permission_error_nothing_written_no_refresh(self, world):
        store, repo, service = world
        await _seed(repo, _bio("foreign", account=OTHER))
        before = copy.deepcopy(store.docs)

        with pytest.raises(PermissionError, match="Not authorized to modify this fact"):
            await service.invalidate("f-foreign", ACC)

        assert store.docs == before and repo.refreshed == [] and _cached_texts(store) is None

    async def test_a_missing_id_is_refused_with_value_error_nothing_written_no_refresh(self, world):
        store, repo, service = world
        await _seed(repo, _bio("x"))
        before = copy.deepcopy(store.docs)

        with pytest.raises(ValueError, match="Fact not found: f-missing"):
            await service.invalidate("f-missing", ACC)

        assert store.docs == before and repo.refreshed == []

    async def test_a_rewritten_version_is_refused_for_the_409_nothing_written_no_refresh(self, world):
        store, repo, service = world
        await _seed(
            repo,
            _bio("v1", text="mother is Maria", lineage_id="L", state=FactState.SUPERSEDED, is_current=False,
                 valid_to=T0, superseded_by="f-v2"),
            _bio("v2", text="mother is Maria Doe", lineage_id="L", version=2),
        )
        before = copy.deepcopy(store.docs)

        with pytest.raises(FactResolutionError) as caught:
            await service.invalidate("f-v1", ACC)

        assert str(caught.value) == "fact f-v1 was rewritten after the page loaded; reload"
        assert caught.value.current_id == "f-v2" and not isinstance(caught.value, ValueError)
        assert store.docs == before and repo.refreshed == []

    async def test_one_conflict_is_retried_and_the_retry_writes(self, world):
        store, repo, service = world
        await _seed(repo, _bio("x"))
        store.interfere("f-x", {"last_updated": T0})      # another writer commits once, as the first write reads

        await service.invalidate("f-x", ACC)

        doc = store.docs["f-x"]
        assert doc["state"] == "invalidated" and len(doc["lifecycle_events"]) == 1
        assert repo.refreshed == [ACC]

    async def test_two_conflicts_raise_and_neither_writes_nor_refreshes(self, world):
        store, repo, service = world
        await _seed(repo, _bio("x"))
        store.interfere("f-x", {"last_updated": T0})
        store.interfere("f-x", {"last_updated": T0})

        with pytest.raises(FactWriteConflict):
            await service.invalidate("f-x", ACC)

        doc = store.db.collection(FACTS).docs["f-x"]
        assert doc["state"] == "current" and doc["lifecycle_events"] == []
        assert repo.refreshed == [] and _cached_texts(store) is None

    async def test_a_conflict_whose_writer_rewrote_the_text_is_refused_on_the_retry(self, world):
        store, repo, service = world
        await _seed(repo, _bio("v1", text="works remotely", lineage_id="L"),
                    _bio("v2", text="works remotely from Springfield", lineage_id="L", version=2))
        store.interfere("f-v1", {"state": "superseded", "is_current": False, "superseded_by": "f-v2", "valid_to": T0})

        with pytest.raises(FactResolutionError, match="rewritten after the page loaded"):
            await service.invalidate("f-v1", ACC)

        assert store.docs["f-v2"]["state"] == "current" and repo.refreshed == []
