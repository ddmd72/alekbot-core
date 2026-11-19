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
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from ..domain.companion import CompanionRecord
from ..domain.companion_config import session_domain_keys
from ..domain.companion_context import CompanionContext
from ..domain.entities import FactEntity
from ..domain.fact_lifecycle import DIRECTIVE_DOMAIN
from ..domain.fact_query import FactQuery
from ..domain.rrf import apply_rrf_ranking
from ..domain.settings import SearchConfig
from ..ports.companion_cache_repository import CompanionCacheRepository
from ..ports.companion_memory_repository import CompanionMemoryRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_search_port import FactSearchPort
from ..ports.repository import FactRepository
from ..utils.logger import logger

PATH = "companion"


class CompanionContextAssemblerService:

    def __init__(
        self,
        companion_repo: CompanionMemoryRepository,
        cache_repo: CompanionCacheRepository,
        embedding_service: EmbeddingService,
        fact_search: FactSearchPort,
        fact_repo: FactRepository,
        rrf_k: int = 60,
        biographical_limit: int = 10,
    ) -> None:
        self._companion_repo = companion_repo
        self._cache_repo = cache_repo
        self._embedding = embedding_service
        self._fact_search = fact_search
        self._fact_repo = fact_repo
        self._rrf_k = rrf_k
        self._biographical_limit = biographical_limit

    async def assemble_context(
        self,
        session_id: str,
        account_id: str,
        query_phrases: List[str],
        include_own_records: bool = True,
        own_records_limit: int = 10,
        include_biographical: bool = False,
        session_domains: Optional[Sequence[str]] = None,
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
                # One read whose condition is the directive marker `is_directive` reads (both move together, RFC §6).
                return await self._fact_repo.get_active_facts_ordered(
                    account_id,
                    domain=DIRECTIVE_DOMAIN,
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
        session_domains: Optional[Sequence[str]],
        account_id: str,
        user_id: Optional[str],
    ) -> List[FactEntity]:
        """The biographical slice through the fact search (fact profile RFC §5.4): `subject` and `message` from the
        first phrase, `domains` from the session's domains, the first `biographical_limit` facts.

        `session_domains` are registry keys; an old stored value is mapped through the registry and one that maps to
        nothing is dropped with a warning (a channel saved before the registry may still hold one).

        RFC §5 frames this read path as a permission boundary ('default is no'): without the owner's user_id the slice
        is skipped (fails CLOSED) rather than read on the turn's behalf."""
        if not user_id:
            logger.warning(
                "⚠️ [CompanionAssembler] biographical slice skipped: no user_id to scope the fetch"
            )
            return []

        domains, dropped = session_domain_keys(session_domains or ())
        if dropped:
            logger.warning(f"⚠️ [CompanionAssembler] unknown session domains dropped: {sorted(dropped)}")
        phrase = query_phrases[0] if query_phrases else ""
        query = FactQuery(
            today=datetime.now(timezone.utc).date(), subject=phrase, message=phrase, domains=tuple(domains), path=PATH,
        )
        facts = await self._fact_search.search(account_id, query)
        return facts[:self._biographical_limit]
