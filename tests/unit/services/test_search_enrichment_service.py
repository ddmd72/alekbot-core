import pytest

from src.domain.entities import FactEntity, FactType
from src.services.search_enrichment_service import SearchEnrichmentService


class FakeRepository:
    def __init__(self, results_map):
        self._results_map = results_map

    async def search_facts(self, query_vector, vector_field=None, limit=10, **kwargs):
        key = query_vector[0]
        return self._results_map.get(key, [])[:limit]

    async def search_facts_by_domain(self, domains, limit=10, **kwargs):
        return []


class FakeEmbeddingService:
    async def get_embedding(self, text, task_type="RETRIEVAL_QUERY"):
        return [text]

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_QUERY"):
        return [[text] for text in texts]


def _make_fact(fact_id: str, text: str) -> FactEntity:
    return FactEntity(
        id=fact_id,
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id=fact_id,
        text=text,
        tags=[],
        type=FactType.EVENT
    )


@pytest.mark.asyncio
async def test_enrich_context_weighted_merge_and_dedup():
    keyword_facts = [
        _make_fact("f1", "keyword 1"),
        _make_fact("f2", "keyword 2")
    ]
    phrase_one_facts = [
        _make_fact("f2", "keyword 2"),
        _make_fact("f3", "phrase 1")
    ]
    phrase_two_facts = [
        _make_fact("f4", "phrase 2")
    ]

    results_map = {
        "kw": keyword_facts,
        "p1": phrase_one_facts,
        "p2": phrase_two_facts
    }

    service = SearchEnrichmentService(
        repository=FakeRepository(results_map),
        embedding_service=FakeEmbeddingService(),
        keyword_limit=10,
        phrase_one_limit=10,
        phrase_two_limit=10,
        total_limit=30
    )

    context = await service.enrich_context(
        keywords=["kw"],
        search_phrase_1="p1",
        search_phrase_2="p2",
        biographical_facts=None,
        skip_semantic_dedup=True  # Facts have no vectors; bypass semantic dedup
    )

    fact_ids = {fact.fact_id for fact in context.facts}
    assert fact_ids == {"f1", "f2", "f3", "f4"}
    assert context.biographical_dedup_count == 0


@pytest.mark.asyncio
async def test_enrich_context_biographical_dedup():
    keyword_facts = [_make_fact("f1", "keyword 1")]
    phrase_one_facts = [_make_fact("f2", "phrase 1")]
    phrase_two_facts = [_make_fact("f3", "phrase 2")]

    results_map = {
        "kw": keyword_facts,
        "p1": phrase_one_facts,
        "p2": phrase_two_facts
    }

    service = SearchEnrichmentService(
        repository=FakeRepository(results_map),
        embedding_service=FakeEmbeddingService(),
        keyword_limit=10,
        phrase_one_limit=10,
        phrase_two_limit=10,
        total_limit=30
    )

    biographical = [_make_fact("f2", "phrase 1")]

    context = await service.enrich_context(
        keywords=["kw"],
        search_phrase_1="p1",
        search_phrase_2="p2",
        biographical_facts=biographical,
        skip_semantic_dedup=True  # Facts have no vectors; bypass semantic dedup
    )

    fact_ids = {fact.fact_id for fact in context.facts}
    assert "f2" not in fact_ids, "f2 should be excluded as biographical duplicate"
    assert "f1" in fact_ids
    assert "f3" in fact_ids
    assert context.biographical_dedup_count == 1
