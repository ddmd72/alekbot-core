"""Facts S3 · T9 (FACT_LIFECYCLE_RFC D5, test plan item 4): a staled fact reaches no reader.

One test per read path of D5's "no" cells, each on the real repository over the evaluating fake: the reader sees the due fact
before the sweep and does not see it after; the fact is still stored, in state stale, for the review pass.
"""
import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.request_context import RequestContext
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import JanitorWorld
from tests.support.fact_store import embed

GONE, KEPT = "gone-fact", "kept-fact"


async def cache(world):
    return {item["text"] for item in await world.repo.get_biographical_context_cached(ACC)}


async def active_ordered(world):
    return {f.text for f in await world.repo.get_active_facts_ordered(ACC)}


async def active_ordered_directives(world):
    return {f.text for f in await world.repo.get_active_facts_ordered(ACC, domain="agent_directive")}


async def active(world):
    return {f.text for f in await world.repo.get_active_facts(ACC)}


async def paginated(world):
    facts, _ = await world.repo.get_paginated_facts(ACC)
    return {f.text for f in facts}


async def vector_search(world):
    return {f.text for f in await world.repo.search_facts(embed(GONE), account_id=ACC, user_id=USER)}


async def domain_search(world):
    return {f.text for f in await world.repo.search_facts_by_domain(["health", "agent_directive"], account_id=ACC)}


def enrichment(world):
    return SearchEnrichmentService(world.repo, world.store.embedder, 7, 10, 10, 20)


async def router_enrichment(world):
    with RequestContext(user_id=USER, account_id=ACC):
        result = await enrichment(world).enrich_context(
            keywords=[], search_phrase_1=GONE, search_phrase_2=KEPT, relevant_domains=["health"]
        )
    return {f.content for f in result.facts}


async def consolidator_search(world):
    adapter = FactManagementAdapter(world.repo, world.store.embedder, world.store.write, enrichment(world))
    with RequestContext(user_id=USER, account_id=ACC):
        found = await adapter.search_existing_facts(["x"], GONE, KEPT, timezone="Europe/Paris")
    return {item["content"] for item in found}


READ_PATHS = [
    pytest.param(cache, "health", id="biographical-cache-document"),
    pytest.param(vector_search, "health", id="search_facts-smart-router-search_memory-mcp"),
    pytest.param(domain_search, "health", id="search_facts_by_domain-router"),
    pytest.param(router_enrichment, "health", id="enrichment-with-domains"),
    pytest.param(active_ordered, "health", id="get_active_facts_ordered-lelik-voice-domains"),
    pytest.param(active_ordered_directives, "agent_directive", id="get_active_facts_ordered-stage-2b-directives"),
    pytest.param(active, "health", id="get_active_facts-cabinet-and-cache-build"),
    pytest.param(paginated, "health", id="get_paginated_facts-cabinet-list"),
    pytest.param(consolidator_search, "health", id="search_existing_facts-stage-1-and-2a"),
]


@pytest.mark.parametrize("reader,domain", READ_PATHS)
async def test_a_staled_fact_is_absent_from_the_reader_after_the_sweep(reader, domain):
    world = JanitorWorld()
    gone = await world.seed_due(GONE, domain=domain)
    await world.seed(KEPT, domain=domain)
    await world.repo.refresh_biographical_context_cache(ACC)
    assert await reader(world) >= {GONE, KEPT}                    # the reader does see the due fact until it is staled

    result = await world.service.sweep()

    assert result["staled"] == 1
    seen = await reader(world)
    assert GONE not in seen and KEPT in seen
    assert world.docs[gone]["state"] == "stale" and world.docs[gone]["text"] == GONE      # kept for the review, not lost


async def test_log_only_leaves_every_reader_as_it_was():
    world = JanitorWorld(dry_run=True)
    await world.seed_due(GONE)
    await world.seed(KEPT)
    await world.repo.refresh_biographical_context_cache(ACC)
    readers = [p.values[0] for p in READ_PATHS if p.values[1] == "health"]
    before = [await reader(world) for reader in readers]

    await world.service.sweep()

    after = [await reader(world) for reader in readers]
    assert after == before and all(GONE in texts for texts in after)
