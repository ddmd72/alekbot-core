"""Forwarding of tuning arguments inside SearchEnrichmentService (mutation survivors).

Each test makes a dropped keyword visible: the fake repository records exactly what it
received, and the dedup / RRF cases choose inputs where the forwarded value changes the outcome.
"""
import math

import pytest

from src.domain.entities import FactEntity, FactType
from src.domain.search import EnrichedFact
from src.services.search_enrichment_service import SearchEnrichmentService


class RecordingRepository:
    """Keeps the keyword arguments of every call, with no defaults of its own."""

    def __init__(self, facts):
        self._facts = facts
        self.vector_calls = []
        self.domain_calls = []

    async def search_facts(self, **kwargs):
        self.vector_calls.append(kwargs)
        return self._facts

    async def search_facts_by_domain(self, **kwargs):
        self.domain_calls.append(kwargs)
        return self._facts


class _NoEmbeddings:
    async def get_embedding(self, text, task_type="RETRIEVAL_QUERY"):
        return [0.0]

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_QUERY"):
        return [[0.0] for _ in texts]


def _fact(fact_id: str, text: str) -> FactEntity:
    return FactEntity(
        id=fact_id,
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id=fact_id,
        text=text,
        tags=[],
        type=FactType.EVENT,
    )


def _service(repo=None) -> SearchEnrichmentService:
    return SearchEnrichmentService(
        repository=repo or RecordingRepository([]),
        embedding_service=_NoEmbeddings(),
        keyword_limit=10,
        phrase_one_limit=10,
        phrase_two_limit=10,
        total_limit=30,
    )


def _enriched(fact_id: str, content: str = "alpha", vector=None) -> EnrichedFact:
    return EnrichedFact(fact_id=fact_id, content=content, source="keyword", vector=vector)


@pytest.mark.asyncio
async def test_search_by_vector_field_forwards_field_and_limit():
    repo = RecordingRepository([_fact("fact-1", "alpha")])
    result = await _service(repo)._search_by_vector_field(
        query_vector=[0.5, 0.5], vector_field="tags_vector", limit=3, source_label="keyword_tags"
    )
    assert repo.vector_calls == [
        {"query_vector": [0.5, 0.5], "vector_field": "tags_vector", "limit": 3}
    ]
    assert [f.fact_id for f in result] == ["fact-1"]


@pytest.mark.asyncio
async def test_search_by_domain_forwards_domains_and_limit():
    repo = RecordingRepository([_fact("fact-1", "alpha")])
    result = await _service(repo)._search_by_domain(
        domains=["domain-a", "domain-b"], limit=7, source_label="domain_direct"
    )
    assert repo.domain_calls == [{"domains": ["domain-a", "domain-b"], "limit": 7}]
    assert [f.fact_id for f in result] == ["fact-1"]


def test_rrf_forwards_k():
    lists = [
        [_enriched("a"), _enriched("p"), _enriched("b")],
        [_enriched("c"), _enriched("q"), _enriched("b")],
    ]
    service = _service()
    # k=60: "b" appears twice and wins; k=0: top ranks dominate and "a" leads.
    assert service._apply_rrf_ranking(lists)[0].fact_id == "b"
    assert service._apply_rrf_ranking(lists, k=0)[0].fact_id == "a"


@pytest.mark.asyncio
async def test_semantic_dedup_forwards_similarity_threshold():
    sim = 0.97  # between the fixed moderate threshold (0.96) and the default strict one (0.98)
    kept_vec = [1.0, 0.0]
    new_vec = [sim, math.sqrt(1 - sim * sim)]
    # The second fact is much longer, so below the strict threshold it counts as "more detailed".
    facts = [
        _enriched("fact-1", "alpha", kept_vec),
        _enriched("fact-2", "alpha beta gamma delta epsilon", new_vec),
    ]
    service = _service()

    default_kept, default_removed = await service._deduplicate_semantic(facts)
    assert [f.fact_id for f in default_kept] == ["fact-1", "fact-2"]
    assert default_removed == 0

    custom_kept, custom_removed = await service._deduplicate_semantic(facts, similarity_threshold=0.95)
    assert [f.fact_id for f in custom_kept] == ["fact-1"]
    assert custom_removed == 1
