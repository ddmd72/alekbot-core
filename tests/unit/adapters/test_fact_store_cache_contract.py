"""Biographical cache contract on the real Firestore repository over the evaluating fake.

Pins the two fixes of the facts-fix PR: a backend failure on the cached read is logged and re-raised (Lelik fails the
call closed on it; callers that must not fail wrap it themselves), and refresh_biographical_context_cache forwards
the limits it is given to the context service instead of dropping them.
"""

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import ContextPriority, FactDomain
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config

CONTEXT_COLLECTION = "eval_user_context"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def _cache(store):
    return store.db.collection(CONTEXT_COLLECTION).docs.get(ACC)


def _repo_with_cache_service(store):
    service = BiographicalContextService(None)
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder,
                                   biographical_context_service=service)
    service.set_repository(repo)
    return repo


async def test_cached_read_logs_and_reraises_the_backend_error(store, monkeypatch):
    errors = []
    monkeypatch.setattr("src.adapters.firestore_repo.logger.error", lambda msg, *a, **k: errors.append(str(msg)))

    def boom(_doc_id):
        raise RuntimeError("backend down")

    monkeypatch.setattr(store.repo.user_context_col, "document", boom)
    with pytest.raises(RuntimeError, match="backend down"):
        await store.repo.get_biographical_context_cached(ACC)
    assert any("backend down" in message for message in errors)


async def test_cached_read_of_a_missing_or_empty_cache_is_still_empty_not_an_error(store):
    assert await store.repo.get_biographical_context_cached(ACC) == []
    store.db.collection(CONTEXT_COLLECTION).docs[ACC] = {"biographical_facts": [], "principles": [], "directives": []}
    assert await store.repo.get_biographical_context_cached(ACC) == []


async def test_refresh_forwards_facts_limit_to_the_context_service(store):
    for i in range(4):
        await store.repo.add_fact(make_fact(f"bio {i}", domain=FactDomain.BIOGRAPHICAL, age_days=i))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC, facts_limit=3)
    assert _cache(store)["facts_count"] == 3


async def test_refresh_forwards_principles_limit_to_the_context_service(store):
    for i in range(3):
        await store.repo.add_fact(make_fact(f"mindset {i}", domain=FactDomain.BIOGRAPHICAL, tags=["mindset"],
                                            age_days=i))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC, principles_limit=1)
    cache = _cache(store)
    assert (cache["principles_count"], len(cache["principles"])) == (1, 1)


async def test_refresh_without_limits_keeps_the_service_resolution(store):
    for i in range(4):
        await store.repo.add_fact(make_fact(f"bio {i}", domain=FactDomain.BIOGRAPHICAL, age_days=i))
    for i in range(3):
        await store.repo.add_fact(make_fact(f"mindset {i}", domain=FactDomain.BIOGRAPHICAL, tags=["mindset"],
                                            age_days=10 + i))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC)
    cache = _cache(store)
    assert (cache["facts_count"], cache["principles_count"]) == (4, 3)


async def test_refresh_keeps_exactly_principles_limit_principles_highest_priority_first(store):
    for i, priority in enumerate((ContextPriority.LOW, ContextPriority.CRITICAL, ContextPriority.MEDIUM,
                                  ContextPriority.HIGH)):
        await store.repo.add_fact(make_fact(f"mindset {i}", domain=FactDomain.BIOGRAPHICAL, tags=["mindset"],
                                            context_priority=priority, age_days=i))
    await store.repo.add_fact(make_fact("plain", domain=FactDomain.BIOGRAPHICAL, age_days=9))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC, principles_limit=2)
    cache = _cache(store)
    assert [p["text"] for p in cache["principles"]] == ["mindset 1", "mindset 3"]
    assert (cache["principles_count"], cache["facts_count"]) == (2, 1)
