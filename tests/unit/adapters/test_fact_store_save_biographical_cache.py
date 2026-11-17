"""FirestoreFactRepository.save_biographical_context_cache: one merged write of the account's cache document (the three
lists, their counts, a server refresh time, the version marker), read back by get_biographical_context_cached; a store
failure propagates. The repository's refresh forwards to the injected context service's refresh_cache."""
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config
from tests.support.firestore_eval import EvalDocRef

CACHE = make_env_config().user_context_collection


def item(text: str, tags: Optional[List[str]] = None) -> dict:
    return {"id": f"f-{text}", "text": text, "domain": "biographical", "tags": tags or [], "context_priority": "high",
            "created_at": None}


CONTEXT = {"facts": [item("a fact"), item("b fact")], "principles": [item("a principle", ["mindset"])],
           "directives": [item("a rule")]}


@pytest.fixture
def store() -> FactStore:
    return FactStore(indexes=INDEXES_FILE)


def doc(store: FactStore, account: str = ACC) -> Any:
    return store.db.collection(CACHE).docs.get(account)


async def test_the_document_holds_the_lists_their_counts_the_refresh_time_and_the_version(store):
    started = datetime.now(timezone.utc)

    await store.repo.save_biographical_context_cache(ACC, CONTEXT)

    saved = doc(store)
    assert set(saved) == {"biographical_facts", "principles", "directives", "refreshed_at", "facts_count",
                          "principles_count", "version"}
    assert (saved["biographical_facts"], saved["principles"], saved["directives"]) == (
        CONTEXT["facts"], CONTEXT["principles"], CONTEXT["directives"])
    assert (saved["facts_count"], saved["principles_count"], saved["version"]) == (2, 1, "v3_hexagonal_di")
    assert started <= saved["refreshed_at"] <= datetime.now(timezone.utc)
    assert doc(store, OTHER) is None


async def test_a_context_without_directives_stores_an_empty_list(store):
    await store.repo.save_biographical_context_cache(ACC, {"facts": [item("a fact")], "principles": []})

    assert doc(store)["directives"] == []


async def test_the_write_merges_into_the_document(store):
    store.db.collection(CACHE).docs[ACC] = {"kept": "another writer's field", "directives": [item("an old rule")]}

    await store.repo.save_biographical_context_cache(ACC, CONTEXT)

    assert doc(store)["kept"] == "another writer's field"
    assert doc(store)["directives"] == CONTEXT["directives"]


async def test_the_cached_read_returns_what_was_saved(store):
    await store.repo.save_biographical_context_cache(ACC, CONTEXT)

    got = await store.repo.get_biographical_context_cached(ACC)

    assert [i["text"] for i in got] == ["a fact", "b fact", "a principle", "a rule"]


async def test_a_store_failure_propagates_and_the_previous_cache_stays(store, monkeypatch):
    await store.repo.save_biographical_context_cache(ACC, CONTEXT)
    before = dict(doc(store))
    real_apply = EvalDocRef._apply

    def cache_down(ref: EvalDocRef, *args: Any, **kwargs: Any) -> Any:
        if ref.col.name == CACHE:
            raise RuntimeError("cache store unavailable")
        return real_apply(ref, *args, **kwargs)

    monkeypatch.setattr(EvalDocRef, "_apply", cache_down)

    with pytest.raises(RuntimeError, match="cache store unavailable"):
        await store.repo.save_biographical_context_cache(ACC, {"facts": [], "principles": []})

    assert doc(store) == before


class Recording(BiographicalContextService):
    def __init__(self) -> None:
        super().__init__(None)
        self.calls: List[Tuple[str, Optional[int], Optional[int]]] = []

    async def refresh_cache(self, owner_id: str, facts_limit: Optional[int] = None,
                            principles_limit: Optional[int] = None) -> None:
        self.calls.append((owner_id, facts_limit, principles_limit))


async def test_the_repository_refresh_forwards_to_the_context_services_refresh_cache(store):
    service = Recording()
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder,
                                   biographical_context_service=service)

    await repo.refresh_biographical_context_cache(ACC, facts_limit=5, principles_limit=2)
    await repo.refresh_biographical_context_cache(OTHER)

    assert service.calls == [(ACC, 5, 2), (OTHER, None, None)]
