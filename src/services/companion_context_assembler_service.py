"""
CompanionContextAssemblerService — assembles what a companion agent should
see for one turn: this session's own records (RRF over its own memory),
a cached session summary, and — governed by an explicit permission
toggle-set — slices of the user's personal store.

RFC: docs/10_rfcs/COMPANION_AGENTS_RFC.md §5 ("the read side is a permission
boundary, not a preference... default is no") and §6 (shared infrastructure,
"structurally parallel to SearchEnrichmentService").
"""
import asyncio
from typing import List, Optional

from ..domain.companion import CompanionRecord
from ..domain.companion_context import CompanionContext
from ..domain.entities import FactDomain
from ..domain.rrf import apply_rrf_ranking
from ..domain.settings import SearchConfig
from ..ports.companion_cache_repository import CompanionCacheRepository
from ..ports.companion_memory_repository import CompanionMemoryRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.repository import FactRepository
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..utils.logger import logger


class CompanionContextAssemblerService:

    def __init__(
        self,
        companion_repo: CompanionMemoryRepository,
        cache_repo: CompanionCacheRepository,
        embedding_service: EmbeddingService,
        enrichment_port: SearchEnrichmentPort,
        fact_repo: FactRepository,
        rrf_k: int = 60,
    ) -> None:
        self._companion_repo = companion_repo
        self._cache_repo = cache_repo
        self._embedding = embedding_service
        self._enrichment = enrichment_port
        self._fact_repo = fact_repo
        self._rrf_k = rrf_k

    async def assemble_context(
        self,
        session_id: str,
        account_id: str,
        query_phrases: List[str],
        include_own_records: bool = True,
        own_records_limit: int = 10,
        include_biographical: bool = False,
        session_domains: Optional[List[FactDomain]] = None,
        include_standing_directives: bool = False,
    ) -> CompanionContext:
        summary = await self._cache_repo.get_summary(session_id)

        own_records: List[CompanionRecord] = []
        if include_own_records and query_phrases:
            own_records = await self._fetch_own_records(
                session_id, query_phrases, own_records_limit
            )

        biographical_facts = []
        if include_biographical:
            biographical_facts = await self._fetch_biographical(query_phrases, session_domains)

        standing_directives = []
        if include_standing_directives:
            standing_directives = await self._fact_repo.get_active_facts_ordered(
                account_id,
                domain=FactDomain.AGENT_DIRECTIVE.value,
                limit=SearchConfig().DEFAULT_DIRECTIVES_CACHE_LIMIT,
            )

        return CompanionContext(
            session_summary=summary,
            own_records=own_records,
            biographical_facts=biographical_facts,
            standing_directives=standing_directives,
        )

    async def _fetch_own_records(
        self, session_id: str, query_phrases: List[str], limit: int
    ) -> List[CompanionRecord]:
        """Embed all phrases in one batch call, fan out find_nearest per phrase,
        RRF-merge. Concurrency is bounded inside FirestoreCompanionMemoryRepository
        itself (Task 1's semaphore), not here — this fans out freely."""
        vectors = await self._embedding.get_embeddings_batch(query_phrases, "RETRIEVAL_QUERY")

        results = await asyncio.gather(
            *(self._companion_repo.find_nearest(session_id, v, limit=limit) for v in vectors),
            return_exceptions=True,
        )

        valid_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("⚠️ [CompanionAssembler] own-records query failed: %s", r)
            else:
                valid_results.append(r)

        merged = apply_rrf_ranking(valid_results, key_fn=lambda rec: rec.id, k=self._rrf_k)
        return merged[:limit]

    async def _fetch_biographical(
        self, query_phrases: List[str], session_domains: Optional[List[FactDomain]]
    ) -> List:
        """Reuses SearchEnrichmentPort.enrich_context unchanged (RFC §6: 'reused unchanged;
        already a generic text -> vector port with zero entity coupling' — same reasoning
        applied here to the enrichment port itself). Maps this service's variable-length
        query_phrases onto enrich_context's fixed phrase_1/phrase_2 slots; extra phrases
        beyond 2 are not used for this slice (existing enrich_context contract, not a gap
        introduced here)."""
        phrase_1 = query_phrases[0] if len(query_phrases) >= 1 else ""
        phrase_2 = query_phrases[1] if len(query_phrases) >= 2 else ""
        domains = [d.value for d in session_domains] if session_domains else None

        enriched = await self._enrichment.enrich_context(
            keywords=[],
            search_phrase_1=phrase_1,
            search_phrase_2=phrase_2,
            relevant_domains=domains,
        )
        return enriched.facts
