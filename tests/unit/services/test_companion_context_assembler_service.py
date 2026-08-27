from unittest.mock import AsyncMock

import pytest

from src.domain.companion import CompanionRecord
from src.domain.entities import FactDomain, FactEntity, FactType
from src.domain.search import EnrichedContext, EnrichedFact
from src.domain.settings import SearchConfig
from src.ports.companion_cache_repository import CompanionCacheRepository
from src.ports.companion_memory_repository import CompanionMemoryRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.repository import FactRepository
from src.ports.search_enrichment_port import SearchEnrichmentPort
from src.services.companion_context_assembler_service import (
    CompanionContextAssemblerService,
)


def _record(id_="r1"):
    return CompanionRecord(
        id=id_, session_id="slack:C1", account_id="acc1",
        created_by_user_id="u1", text="text", domain="grammar_error",
    )


@pytest.fixture
def companion_repo():
    return AsyncMock(spec=CompanionMemoryRepository)


@pytest.fixture
def cache_repo():
    return AsyncMock(spec=CompanionCacheRepository)


@pytest.fixture
def embedding_service():
    return AsyncMock(spec=EmbeddingService)


@pytest.fixture
def enrichment_port():
    return AsyncMock(spec=SearchEnrichmentPort)


@pytest.fixture
def fact_repo():
    return AsyncMock(spec=FactRepository)


@pytest.fixture
def service(companion_repo, cache_repo, embedding_service, enrichment_port, fact_repo):
    return CompanionContextAssemblerService(
        companion_repo=companion_repo,
        cache_repo=cache_repo,
        embedding_service=embedding_service,
        enrichment_port=enrichment_port,
        fact_repo=fact_repo,
    )


class TestSessionSummary:
    async def test_always_fetched_regardless_of_toggles(self, service, cache_repo):
        cache_repo.get_summary = AsyncMock(return_value="cached summary")

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=[],
            include_own_records=False, include_biographical=False,
            include_standing_directives=False,
        )

        cache_repo.get_summary.assert_called_once_with("slack:C1")
        assert ctx.session_summary == "cached summary"


class TestOwnRecords:
    async def test_disabled_by_default_flag_skips_fetch(
        self, service, cache_repo, embedding_service, companion_repo
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1",
            query_phrases=["mixed up preterite"],
            include_own_records=False,
        )

        embedding_service.get_embeddings_batch.assert_not_called()
        companion_repo.find_nearest.assert_not_called()
        assert ctx.own_records == []

    async def test_no_query_phrases_skips_fetch_even_when_enabled(
        self, service, cache_repo, embedding_service
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=[],
            include_own_records=True,
        )

        embedding_service.get_embeddings_batch.assert_not_called()
        assert ctx.own_records == []

    async def test_embeds_and_merges_own_records_via_rrf(
        self, service, cache_repo, embedding_service, companion_repo
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)
        embedding_service.get_embeddings_batch = AsyncMock(
            return_value=[[0.1, 0.2], [0.3, 0.4]]
        )
        rec_a = _record("a")
        rec_b = _record("b")
        # Query 1 ranks [a, b]; query 2 ranks [b] only — b appears in both, ranks first.
        companion_repo.find_nearest = AsyncMock(side_effect=[[rec_a, rec_b], [rec_b]])

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1",
            query_phrases=["mixed up preterite", "asked about ser vs estar"],
            include_own_records=True, own_records_limit=10,
        )

        embedding_service.get_embeddings_batch.assert_called_once_with(
            ["mixed up preterite", "asked about ser vs estar"], "RETRIEVAL_QUERY"
        )
        assert companion_repo.find_nearest.call_count == 2
        assert [r.id for r in ctx.own_records] == ["b", "a"]

    async def test_one_failed_query_does_not_lose_the_others(
        self, service, cache_repo, embedding_service, companion_repo
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)
        embedding_service.get_embeddings_batch = AsyncMock(return_value=[[0.1], [0.2]])
        rec_a = _record("a")
        companion_repo.find_nearest = AsyncMock(
            side_effect=[RuntimeError("firestore blip"), [rec_a]]
        )

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1",
            query_phrases=["p1", "p2"], include_own_records=True,
        )

        assert [r.id for r in ctx.own_records] == ["a"]


class TestBiographical:
    async def test_disabled_by_default(self, service, cache_repo, enrichment_port):
        cache_repo.get_summary = AsyncMock(return_value=None)

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=False, include_biographical=False,
        )

        enrichment_port.enrich_context.assert_not_called()
        assert ctx.biographical_facts == []

    async def test_calls_enrich_context_with_session_domains(
        self, service, cache_repo, enrichment_port
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)
        fact = EnrichedFact(fact_id="f1", content="likes spanish soap operas", source="phrase1_text")
        enrichment_port.enrich_context = AsyncMock(
            return_value=EnrichedContext(
                facts=[fact], total_sources=1, dedup_count=0, biographical_dedup_count=0
            )
        )

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1",
            query_phrases=["mixed up preterite", "asked about soap operas"],
            include_own_records=False, include_biographical=True,
            session_domains=[FactDomain.ENTERTAINMENT],
            user_id="user-42",
        )

        enrichment_port.enrich_context.assert_called_once_with(
            keywords=[],
            search_phrase_1="mixed up preterite",
            search_phrase_2="asked about soap operas",
            relevant_domains=["entertainment"],
        )
        assert ctx.biographical_facts == [fact]

    async def test_missing_second_phrase_passes_empty_string(
        self, service, cache_repo, enrichment_port
    ):
        cache_repo.get_summary = AsyncMock(return_value=None)
        enrichment_port.enrich_context = AsyncMock(
            return_value=EnrichedContext(facts=[], total_sources=0, dedup_count=0, biographical_dedup_count=0)
        )

        await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["only one"],
            include_own_records=False, include_biographical=True,
            user_id="user-42",
        )

        enrichment_port.enrich_context.assert_called_once_with(
            keywords=[], search_phrase_1="only one", search_phrase_2="",
            relevant_domains=None,
        )


class TestStandingDirectives:
    async def test_disabled_by_default(self, service, cache_repo, fact_repo):
        cache_repo.get_summary = AsyncMock(return_value=None)

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=[],
            include_own_records=False, include_standing_directives=False,
        )

        fact_repo.get_active_facts_ordered.assert_not_called()
        assert ctx.standing_directives == []

    async def test_fetches_agent_directive_domain(self, service, cache_repo, fact_repo):
        cache_repo.get_summary = AsyncMock(return_value=None)
        directive = FactEntity(
            account_id="acc1", created_by_user_id="acc1", lineage_id="l1",
            text="Never give partial answers", type=FactType.STATE,
            domain=FactDomain.AGENT_DIRECTIVE,
        )
        fact_repo.get_active_facts_ordered = AsyncMock(return_value=[directive])

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=[],
            include_own_records=False, include_standing_directives=True,
        )

        fact_repo.get_active_facts_ordered.assert_called_once_with(
            "acc1",
            domain=FactDomain.AGENT_DIRECTIVE.value,
            limit=SearchConfig().DEFAULT_DIRECTIVES_CACHE_LIMIT,
        )
        assert ctx.standing_directives == [directive]


class TestRequestContextWiring:
    async def test_wraps_enrich_context_in_request_context_when_user_id_given(
        self, service, cache_repo, enrichment_port
    ):
        from src.domain.request_context import get_current_account_id, get_current_user_id

        cache_repo.get_summary = AsyncMock(return_value=None)
        captured = {}

        async def _capture(**kwargs):
            captured["user_id"] = get_current_user_id()
            captured["account_id"] = get_current_account_id()
            return EnrichedContext(facts=[], total_sources=0, dedup_count=0, biographical_dedup_count=0)

        enrichment_port.enrich_context = AsyncMock(side_effect=_capture)

        await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=False, include_biographical=True,
            user_id="user-42",
        )

        assert captured["user_id"] == "user-42"
        assert captured["account_id"] == "acc1"

    async def test_biographical_fetch_fails_closed_when_user_id_omitted(
        self, service, cache_repo, enrichment_port
    ):
        """Permission-boundary default (RFC §5: 'default is no'): without a user_id to scope
        the fetch, the biographical slice is skipped entirely rather than falling back to
        whatever RequestContext happens to be ambient — a fallback would risk reading a
        different account's facts if some unrelated ambient context were live."""
        cache_repo.get_summary = AsyncMock(return_value=None)

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=False, include_biographical=True,
        )

        enrichment_port.enrich_context.assert_not_called()
        assert ctx.biographical_facts == []


class TestPartialFailureAcrossFetches:
    async def test_one_fetch_failing_does_not_lose_the_others(
        self, service, cache_repo, embedding_service, companion_repo
    ):
        """The review's finding: an embedding failure must not sink the already-fetched
        summary. With the parallel gather + per-branch try/except, summary still comes
        back even though own-records raises."""
        cache_repo.get_summary = AsyncMock(return_value="cached summary")
        embedding_service.get_embeddings_batch = AsyncMock(side_effect=RuntimeError("embed down"))

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=True,
        )

        assert ctx.session_summary == "cached summary"
        assert ctx.own_records == []


class TestPerFetchFailureIsolation:
    """Each of the 4 fetches has its own try/except inside assemble_context's gather —
    one failing must not prevent the other three from returning their real values."""

    async def test_summary_fetch_failure_returns_none_others_unaffected(
        self, service, cache_repo, companion_repo, embedding_service
    ):
        cache_repo.get_summary = AsyncMock(side_effect=RuntimeError("firestore down"))
        embedding_service.get_embeddings_batch = AsyncMock(return_value=[[0.1, 0.2]])
        rec = _record("a")
        companion_repo.find_nearest = AsyncMock(return_value=[rec])

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=True,
        )

        assert ctx.session_summary is None
        assert [r.id for r in ctx.own_records] == ["a"]

    async def test_biographical_fetch_failure_returns_empty_list(
        self, service, cache_repo, enrichment_port
    ):
        cache_repo.get_summary = AsyncMock(return_value="cached summary")
        enrichment_port.enrich_context = AsyncMock(side_effect=RuntimeError("enrichment down"))

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=["p1"],
            include_own_records=False, include_biographical=True,
            user_id="user-42",
        )

        assert ctx.session_summary == "cached summary"
        assert ctx.biographical_facts == []

    async def test_standing_directives_fetch_failure_returns_empty_list(
        self, service, cache_repo, fact_repo
    ):
        cache_repo.get_summary = AsyncMock(return_value="cached summary")
        fact_repo.get_active_facts_ordered = AsyncMock(side_effect=RuntimeError("facts down"))

        ctx = await service.assemble_context(
            session_id="slack:C1", account_id="acc1", query_phrases=[],
            include_own_records=False, include_standing_directives=True,
        )

        assert ctx.session_summary == "cached summary"
        assert ctx.standing_directives == []
