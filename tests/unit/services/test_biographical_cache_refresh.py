"""BiographicalContextService.refresh_cache (BiographicalCacheRefresher): it saves, through the repository's
`save_biographical_context_cache`, the context `refresh_context` selects, with the caller's limits; it never raises, and on a
failed read or a failed save the account's previous cache stays."""
import copy
import logging
from typing import Any

import pytest

from src.domain.entities import ContextPriority, FactDomain
from src.ports.biographical_cache_port import BiographicalCacheRefresher
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, make_env_config

CACHE = make_env_config().user_context_collection


@pytest.fixture
def store() -> FactStore:
    return FactStore(indexes=INDEXES_FILE)


async def seed(store: FactStore, account: str = ACC) -> None:
    for text, priority, tags, age in (("a fact", ContextPriority.HIGH, [], 1), ("b fact", ContextPriority.LOW, [], 2),
                                      ("a principle", ContextPriority.MEDIUM, ["mindset"], 3),
                                      ("b principle", ContextPriority.MEDIUM, ["mindset"], 4)):
        await store.repo.add_fact(make_fact(f"{account} {text}", account=account, domain=FactDomain.BIOGRAPHICAL,
                                            context_priority=priority, tags=tags, age_days=age))
    await store.repo.add_fact(make_fact(f"{account} a rule", account=account, domain=FactDomain.AGENT_DIRECTIVE))


def cache(store: FactStore, account: str = ACC) -> Any:
    return store.db.collection(CACHE).docs.get(account)


def test_the_service_is_the_refresher_port():
    assert isinstance(BiographicalContextService(None), BiographicalCacheRefresher)


async def test_the_cache_holds_what_refresh_context_selects_with_the_callers_limits(store):
    await seed(store)
    await seed(store, OTHER)
    service = BiographicalContextService(store.repo)

    assert await service.refresh_cache(ACC, facts_limit=3, principles_limit=1) is None

    selected = await service.refresh_context(ACC, facts_limit=3, principles_limit=1)
    stored = cache(store)
    assert (stored["biographical_facts"], stored["principles"], stored["directives"]) == (
        selected["facts"], selected["principles"], selected["directives"])
    # Three places in priority order (the high fact, then both principles, newest first); one principle kept, the low
    # fact left out.
    assert [f["text"] for f in stored["biographical_facts"]] == [f"{ACC} a fact"]
    assert [f["text"] for f in stored["principles"]] == [f"{ACC} b principle"]
    assert [f["text"] for f in stored["directives"]] == [f"{ACC} a rule"]
    assert cache(store, OTHER) is None


async def test_without_limits_the_services_own_resolution_holds(store):
    await seed(store)
    service = BiographicalContextService(store.repo)

    await service.refresh_cache(ACC)

    selected = await service.refresh_context(ACC)
    assert (cache(store)["facts_count"], cache(store)["principles_count"]) == (
        len(selected["facts"]), len(selected["principles"])) == (2, 2)


async def test_a_failed_read_never_raises_and_the_previous_cache_stays(store, caplog):
    await seed(store)
    service = BiographicalContextService(store.repo)
    await service.refresh_cache(ACC)
    before = copy.deepcopy(cache(store))
    store.db.collection(FACTS).fail = RuntimeError("facts read down")

    with caplog.at_level(logging.ERROR):
        assert await service.refresh_cache(ACC) is None

    assert cache(store) == before
    assert any("[BiographicalContext]" in r.getMessage() and "facts read down" in r.getMessage() for r in caplog.records)


async def test_a_failed_save_never_raises_and_the_previous_cache_stays(store, monkeypatch, caplog):
    await seed(store)
    service = BiographicalContextService(store.repo)
    await service.refresh_cache(ACC)
    before = copy.deepcopy(cache(store))
    await store.repo.add_fact(make_fact("a newer fact", domain=FactDomain.BIOGRAPHICAL,
                                        context_priority=ContextPriority.CRITICAL))

    async def save_down(owner_id: str, context: Any) -> None:
        raise RuntimeError("cache write down")

    monkeypatch.setattr(store.repo, "save_biographical_context_cache", save_down)

    with caplog.at_level(logging.ERROR):
        assert await service.refresh_cache(ACC) is None

    assert cache(store) == before
    assert any("cache write down" in r.getMessage() for r in caplog.records)
