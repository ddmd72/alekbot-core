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
from ..domain.request_context import RequestContext
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
        user_id: Optional[str] = None,
    ) -> CompanionContext:
        async def _safe_summary():
            try:
                return await self._cache_repo.get_summary(session_id)
            except Exception as exc:
                logger.warning("⚠️ [CompanionAssembler] summary fetch failed: %s", exc)
                return None

        async def _safe_own_records():
            if not (include_own_records and query_phrases):
                return []
            try:
                return await self._fetch_own_records(
                    session_id, account_id, query_phrases, own_records_limit
                )
            except Exception as exc:
                logger.warning("⚠️ [CompanionAssembler] own-records fetch failed: %s", exc)
                return []

        async def _safe_biographical():
            if not include_biographical:
                return []
            try:
                return await self._fetch_biographical(
                    query_phrases, session_domains, account_id, user_id
                )
            except Exception as exc:
                logger.warning("⚠️ [CompanionAssembler] biographical fetch failed: %s", exc)
                return []

        async def _safe_directives():
            if not include_standing_directives:
                return []
            try:
                return await self._fact_repo.get_active_facts_ordered(
                    account_id,
                    domain=FactDomain.AGENT_DIRECTIVE.value,
                    limit=SearchConfig().DEFAULT_DIRECTIVES_CACHE_LIMIT,
                )
            except Exception as exc:
                logger.warning("⚠️ [CompanionAssembler] standing-directives fetch failed: %s", exc)
                return []

        summary, own_records, biographical_facts, standing_directives = await asyncio.gather(
            _safe_summary(), _safe_own_records(), _safe_biographical(), _safe_directives(),
        )

        return CompanionContext(
            session_summary=summary,
            own_records=own_records,
            biographical_facts=biographical_facts,
            standing_directives=standing_directives,
        )

    async def _fetch_own_records(
        self, session_id: str, account_id: str, query_phrases: List[str], limit: int
    ) -> List[CompanionRecord]:
        """Embed all phrases in one batch call, fan out find_nearest per phrase,
        RRF-merge. Concurrency is bounded inside FirestoreCompanionMemoryRepository
        itself (Task 1's semaphore), not here — this fans out freely."""
        vectors = await self._embedding.get_embeddings_batch(query_phrases, "RETRIEVAL_QUERY")

        results = await asyncio.gather(
            *(
                self._companion_repo.find_nearest(session_id, account_id, v, limit=limit)
                for v in vectors
            ),
            return_exceptions=True,
        )

        valid_results = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("⚠️ [CompanionAssembler] own-records query failed: %s", r)
            else:
                valid_results.append(r)

        merged = apply_rrf_ranking(valid_results, key_fn=lambda rec: rec.id, k=self._rrf_k)
        return merged[:limit]

    async def _fetch_biographical(
        self,
        query_phrases: List[str],
        session_domains: Optional[List[FactDomain]],
        account_id: str,
        user_id: Optional[str],
    ) -> List:
        """Reuses SearchEnrichmentPort.enrich_context unchanged (RFC §6: 'reused unchanged;
        already a generic text -> vector port with zero entity coupling' — same reasoning
        applied here to the enrichment port itself). Maps this service's variable-length
        query_phrases onto enrich_context's fixed phrase_1/phrase_2 slots; extra phrases
        beyond 2 are not used for this slice (existing enrich_context contract, not a gap
        introduced here).

        enrich_context resolves account_id implicitly from RequestContext (a contextvar),
        not from an explicit parameter — so this wraps the call in RequestContext(user_id,
        account_id) to scope it to the account this turn actually belongs to. RFC §5 frames
        this read path as a permission boundary ('default is no'): without a user_id there
        is no way to scope the call correctly, so this fails CLOSED rather than falling back
        to whatever RequestContext happens to be ambient — a fallback would risk reading a
        different account's facts if some unrelated ambient context were live."""
        if not user_id:
            logger.warning(
                "⚠️ [CompanionAssembler] biographical slice skipped: no user_id to scope the fetch"
            )
            return []

        phrase_1 = query_phrases[0] if len(query_phrases) >= 1 else ""
        phrase_2 = query_phrases[1] if len(query_phrases) >= 2 else ""
        domains = [d.value for d in session_domains] if session_domains else None

        async with RequestContext(user_id=user_id, account_id=account_id):
            enriched = await self._enrichment.enrich_context(
                keywords=[],
                search_phrase_1=phrase_1,
                search_phrase_2=phrase_2,
                relevant_domains=domains,
            )
        return enriched.facts
