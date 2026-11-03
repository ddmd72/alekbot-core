"""enrich_context: dedup_threshold reaches the semantic dedup, and a failing query in sequential mode is survivable."""
import math

import pytest

from src.domain.search import EnrichedFact
from src.services.search_enrichment_service import SearchEnrichmentService


class _Embeddings:
    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_QUERY"):
        return [[1.0] for _ in texts]


def _service(search_impl) -> SearchEnrichmentService:
    service = SearchEnrichmentService(
        repository=object(),
        embedding_service=_Embeddings(),
        keyword_limit=10,
        phrase_one_limit=10,
        phrase_two_limit=10,
        total_limit=30,
    )
    service._search_by_vector_field = search_impl
    return service


def _fact(fact_id, content, vector):
    return EnrichedFact(fact_id=fact_id, content=content, source="phrase1_text", vector=vector)


@pytest.mark.asyncio
async def test_enrich_context_forwards_dedup_threshold():
    sim = 0.97  # between the moderate threshold (0.96) and the default strict one (0.98)
    facts = [
        _fact("fact-1", "alpha", [1.0, 0.0]),
        _fact("fact-2", "alpha beta gamma delta epsilon", [sim, math.sqrt(1 - sim * sim)]),
    ]

    async def search(vector, field, limit, label):
        return list(facts) if label == "phrase1_text" else []

    default_ctx = await _service(search).enrich_context([], "phrase one", "")
    assert sorted(f.fact_id for f in default_ctx.facts) == ["fact-1", "fact-2"]

    strict_ctx = await _service(search).enrich_context([], "phrase one", "", dedup_threshold=0.95)
    assert [f.fact_id for f in strict_ctx.facts] == ["fact-1"]
    assert strict_ctx.dedup_count == 1


@pytest.mark.asyncio
async def test_sequential_mode_survives_a_failing_query():
    async def search(vector, field, limit, label):
        if label == "phrase1_text":
            raise RuntimeError("boom")
        return [_fact("fact-9", "omega", [1.0, 0.0])] if label == "phrase1_tags" else []

    ctx = await _service(search).enrich_context([], "phrase one", "", sequential=True)
    assert [f.fact_id for f in ctx.facts] == ["fact-9"]
    assert ctx.total_sources == 2
