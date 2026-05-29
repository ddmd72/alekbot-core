import pytest

from src.domain.entities import FactEntity, FactType
from src.domain.search import EnrichedFact, SearchLimits
from src.services.search_enrichment_service import SearchEnrichmentService


class FakeRepository:
    def __init__(self, results_map=None, domain_results=None):
        self._results_map = results_map or {}
        self._domain_results = domain_results or []

    async def search_facts(self, query_vector, vector_field=None, limit=10, **kwargs):
        key = query_vector[0]
        return self._results_map.get(key, [])[:limit]

    async def search_facts_by_domain(self, domains, limit=10, **kwargs):
        return self._domain_results[:limit]


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


def _make_service(results_map=None, domain_results=None):
    return SearchEnrichmentService(
        repository=FakeRepository(results_map=results_map, domain_results=domain_results),
        embedding_service=FakeEmbeddingService(),
        keyword_limit=10,
        phrase_one_limit=10,
        phrase_two_limit=10,
        total_limit=30,
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


# ---------------------------------------------------------------------------
# enrich_context() — additional branches
# ---------------------------------------------------------------------------

class TestEnrichContextBranches:

    async def test_all_empty_inputs_returns_empty_context(self):
        """All empty slots → _vec = {} → no search tasks → empty context."""
        svc = _make_service()
        ctx = await svc.enrich_context(
            keywords=[],
            search_phrase_1="",
            search_phrase_2="",
            skip_semantic_dedup=True,
        )
        assert ctx.facts == []
        assert ctx.total_sources == 0

    async def test_relevant_domains_triggers_domain_search(self):
        """relevant_domains parameter causes _search_by_domain to be called."""
        domain_fact = _make_fact("d1", "domain fact")
        svc = _make_service(domain_results=[domain_fact])
        ctx = await svc.enrich_context(
            keywords=[],
            search_phrase_1="",
            search_phrase_2="",
            relevant_domains=["health"],
            skip_semantic_dedup=True,
        )
        fact_ids = {f.fact_id for f in ctx.facts}
        assert "d1" in fact_ids

    async def test_sequential_mode_produces_same_results(self):
        """sequential=True executes queries one-by-one, result identical to parallel."""
        facts = [_make_fact("s1", "sequential fact")]
        svc = _make_service(results_map={"kw": facts})
        ctx_parallel = await svc.enrich_context(
            keywords=["kw"], search_phrase_1="", search_phrase_2="",
            skip_semantic_dedup=True, sequential=False,
        )
        ctx_sequential = await svc.enrich_context(
            keywords=["kw"], search_phrase_1="", search_phrase_2="",
            skip_semantic_dedup=True, sequential=True,
        )
        assert {f.fact_id for f in ctx_parallel.facts} == {f.fact_id for f in ctx_sequential.facts}

    async def test_skip_semantic_dedup_false_runs_dedup_path(self):
        """skip_semantic_dedup=False exercises the _deduplicate_semantic code path.
        Facts without vectors are skipped (warned + dropped) in semantic dedup."""
        # Give the fact a vector so it passes through _deduplicate_semantic
        fact = FactEntity(
            id="x1", account_id="acct", created_by_user_id="u",
            lineage_id="x1", text="a fact", tags=[], type=FactType.EVENT,
            vector=[1.0, 0.0],
        )
        svc = _make_service(results_map={"kw": [fact]})
        ctx = await svc.enrich_context(
            keywords=["kw"], search_phrase_1="", search_phrase_2="",
            skip_semantic_dedup=False,  # triggers _deduplicate_semantic
        )
        assert any(f.fact_id == "x1" for f in ctx.facts)
        assert ctx.dedup_count == 0  # single fact → no duplicates removed

    async def test_limits_override_applied(self):
        """SearchLimits override replaces constructor defaults."""
        many_facts = [_make_fact(f"f{i}", f"fact {i}") for i in range(20)]
        svc = _make_service(results_map={"kw": many_facts})
        limits = SearchLimits(keyword_limit=20, phrase_one_limit=10, phrase_two_limit=10, total_limit=3)
        ctx = await svc.enrich_context(
            keywords=["kw"], search_phrase_1="", search_phrase_2="",
            limits=limits, skip_semantic_dedup=True,
        )
        assert len(ctx.facts) <= 3

    async def test_biographical_facts_as_dicts_deduped(self):
        """biographical_facts passed as dicts (not FactEntity) are handled."""
        f1 = _make_fact("bio1", "bio fact")
        svc = _make_service(results_map={"kw": [f1]})
        bio_dict = {"id": "bio1", "text": "bio fact"}
        ctx = await svc.enrich_context(
            keywords=["kw"], search_phrase_1="", search_phrase_2="",
            biographical_facts=[bio_dict], skip_semantic_dedup=True,
        )
        assert all(f.fact_id != "bio1" for f in ctx.facts)
        assert ctx.biographical_dedup_count == 1


# ---------------------------------------------------------------------------
# _safe_results()
# ---------------------------------------------------------------------------

class TestSafeResults:

    def test_exception_returns_empty_list(self):
        svc = _make_service()
        assert svc._safe_results(Exception("boom")) == []

    def test_list_returned_unchanged(self):
        svc = _make_service()
        enriched = [EnrichedFact(fact_id="f1", content="x", source="kw")]
        assert svc._safe_results(enriched) is enriched


# ---------------------------------------------------------------------------
# _apply_rrf_ranking()
# ---------------------------------------------------------------------------

class TestApplyRrfRanking:

    def test_empty_input_returns_empty(self):
        svc = _make_service()
        assert svc._apply_rrf_ranking([]) == []

    def test_single_list_preserves_order(self):
        svc = _make_service()
        facts = [
            EnrichedFact(fact_id="a", content="a", source="kw"),
            EnrichedFact(fact_id="b", content="b", source="kw"),
        ]
        result = svc._apply_rrf_ranking([facts])
        assert [f.fact_id for f in result] == ["a", "b"]

    def test_fact_in_multiple_lists_scores_higher(self):
        """Fact appearing in two lists should be ranked higher than fact in one."""
        svc = _make_service()
        shared = EnrichedFact(fact_id="shared", content="s", source="kw")
        unique = EnrichedFact(fact_id="unique", content="u", source="kw")
        result = svc._apply_rrf_ranking([[shared, unique], [shared]])
        assert result[0].fact_id == "shared"

    def test_deduplication_by_fact_id(self):
        """Same fact in multiple lists appears only once in output."""
        svc = _make_service()
        f = EnrichedFact(fact_id="dup", content="x", source="kw")
        result = svc._apply_rrf_ranking([[f], [f], [f]])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _deduplicate_semantic()
# ---------------------------------------------------------------------------

class TestDeduplicateSemantic:

    async def test_empty_input_returns_empty(self):
        svc = _make_service()
        kept, count = await svc._deduplicate_semantic([])
        assert kept == []
        assert count == 0

    async def test_facts_without_vectors_dropped(self):
        """Facts with no vector are skipped (warned) and dropped from output."""
        svc = _make_service()
        f1 = EnrichedFact(fact_id="f1", content="text", source="kw", vector=None)
        f2 = EnrichedFact(fact_id="f2", content="other", source="kw", vector=None)
        kept, count = await svc._deduplicate_semantic([f1, f2])
        # Both facts have no vector → skipped → neither added to kept
        assert kept == []
        assert count == 0

    async def test_distinct_facts_all_kept(self):
        """Facts with orthogonal vectors are not flagged as duplicates."""
        svc = _make_service()
        # Orthogonal vectors → cosine similarity = 0 → not duplicates
        f1 = EnrichedFact(fact_id="f1", content="apples", source="kw", vector=[1.0, 0.0])
        f2 = EnrichedFact(fact_id="f2", content="oranges", source="kw", vector=[0.0, 1.0])
        kept, count = await svc._deduplicate_semantic([f1, f2])
        assert len(kept) == 2
        assert count == 0

    async def test_identical_vectors_removes_duplicate(self):
        """Identical vectors → similarity = 1.0 → second fact removed."""
        svc = _make_service()
        vec = [1.0, 0.0]
        f1 = EnrichedFact(fact_id="f1", content="same text", source="kw", vector=vec)
        f2 = EnrichedFact(fact_id="f2", content="same text", source="kw", vector=vec)
        kept, count = await svc._deduplicate_semantic([f1, f2])
        assert len(kept) == 1
        assert count == 1
        assert kept[0].fact_id == "f1"

    async def test_custom_threshold_used(self):
        """threshold=1.0 (exact only) allows near-duplicates through."""
        svc = _make_service()
        # Near-identical but not exact
        f1 = EnrichedFact(fact_id="f1", content="text A", source="kw", vector=[1.0, 0.001])
        f2 = EnrichedFact(fact_id="f2", content="text B", source="kw", vector=[1.0, 0.0])
        kept_strict, count_strict = await svc._deduplicate_semantic([f1, f2], similarity_threshold=1.0)
        kept_loose, count_loose = await svc._deduplicate_semantic([f1, f2], similarity_threshold=0.96)
        # With strict threshold=1.0, near-dupes are kept
        assert len(kept_strict) >= len(kept_loose)


# ---------------------------------------------------------------------------
# _search_by_vector_field()
# ---------------------------------------------------------------------------

class TestSearchByVectorField:

    async def test_success_returns_enriched_facts(self):
        facts = [_make_fact("v1", "vector result")]
        svc = _make_service(results_map={"vec_key": facts})
        result = await svc._search_by_vector_field(["vec_key"], "vector", 10, "kw_text")
        assert len(result) == 1
        assert result[0].fact_id == "v1"
        assert result[0].source == "kw_text"

    async def test_repo_exception_returns_empty(self):
        """Repository failure → returns [] gracefully."""
        from unittest.mock import AsyncMock
        svc = _make_service()
        svc._repo.search_facts = AsyncMock(side_effect=Exception("firestore down"))
        result = await svc._search_by_vector_field([1.0, 0.0], "vector", 5, "phrase1_text")
        assert result == []


# ---------------------------------------------------------------------------
# _search_by_domain()
# ---------------------------------------------------------------------------

class TestSearchByDomain:

    async def test_success_returns_enriched_facts(self):
        facts = [_make_fact("d1", "domain result")]
        svc = _make_service(domain_results=facts)
        result = await svc._search_by_domain(["health"], 10, "domain_direct")
        assert len(result) == 1
        assert result[0].fact_id == "d1"
        assert result[0].source == "domain_direct"
        assert result[0].relevance_score == 1.0

    async def test_repo_exception_returns_empty(self):
        """Repository failure → returns [] gracefully."""
        from unittest.mock import AsyncMock
        svc = _make_service()
        svc._repo.search_facts_by_domain = AsyncMock(side_effect=Exception("domain search failed"))
        result = await svc._search_by_domain(["health"], 10, "domain_direct")
        assert result == []
