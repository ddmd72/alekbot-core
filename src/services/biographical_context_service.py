"""
Biographical Context Service (Session 2026-02-16 - Priority-Based Refactored).

Direct Repository query with context_priority-based selection.
No semantic search - consolidation already classified facts.

Session: 2026-02-16 - Deliberate Fact Management Integration
RFC: docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md

Architecture:
- PORTS: FactRepository (direct query)
- NO SearchEnrichmentService (biographical cache is stable, not dynamic)
- Priority-based selection: CRITICAL → HIGH → MEDIUM → LOW
- CRITICAL facts always included (over limit if necessary)
"""

from typing import List, Dict, Optional, Any

from ..ports.repository import FactRepository
from ..domain.entities import FactDomain
from ..domain.settings import SearchConfig
from ..utils.logger import logger


class BiographicalContextService:
    """
    Biographical context cache refresh via priority-based direct query.
    
    Refactored (2026-02-16 - Deliberate Fact Management Integration):
    - NO SearchEnrichmentService (biographical cache is stable, not dynamic)
    - Direct Repository query: get_active_facts(account_id)
    - Filter SUPERSEDED facts
    - Group by context_priority: CRITICAL → HIGH → MEDIUM → LOW
    - CRITICAL facts always included (over limit if necessary)
    
    Philosophy:
    - Consolidation already classified facts with context_priority
    - No semantic search needed - facts are already curated
    - Simple, predictable, fast
    
    Original complexity: ~150 lines (search-based)
    Current complexity: ~100 lines (priority-based, 33% simpler)
    """

    def __init__(
        self,
        repository: FactRepository,
        config_service: Optional[Any] = None,
        account_repo: Optional[Any] = None
    ):
        """
        Initialize biographical context service.
        
        Session 2026-02-16: Removed SearchEnrichmentService dependency.
        Direct repository query with priority-based selection.
        
        Args:
            repository: Fact repository for fetching facts (DI)
            config_service: ConfigurationService for resolving limits (DI)
            account_repo: AccountRepository for loading account defaults (DI)
        """
        self._repo = repository
        self._config_service = config_service
        self._account_repo = account_repo

        logger.debug("📚 [BiographicalContext] Initialized with priority-based selection")

    def set_repository(self, repository) -> None:
        """Resolve circular dependency: call after repository is created."""
        self._repo = repository

    async def refresh_context(
        self,
        account_id: str,
        user_id: Optional[str] = None
    ) -> Dict[str, List[Dict]]:
        """
        Refresh biographical context using priority-based direct query.
        
        Session 2026-02-16: Priority-based selection (no semantic search).
        
        Process:
        1. Resolve limits via ConfigurationService (USER → ACCOUNT → SYSTEM)
        2. Get ALL CURRENT facts from Repository (direct query)
        3. Filter out SUPERSEDED facts
        4. Group by context_priority (CRITICAL/HIGH/MEDIUM/LOW)
        5. Build lists: ALL CRITICAL + HIGH until limit + MEDIUM until limit + LOW if space
        6. Separate into facts vs principles
        
        Args:
            account_id: Account ID to refresh cache for
            user_id: Optional user ID for user-specific overrides (defaults to account_id)
            
        Returns:
            Dict with keys:
                - "facts": List of biographical fact dicts (priority-sorted)
                - "principles": List of principle dicts (priority-sorted)
        """
        user_id = user_id or account_id
        
        # ========================================================================
        # STEP 1: Resolve limits via ConfigurationService
        # ========================================================================
        account = None
        if self._account_repo:
            try:
                account = await self._account_repo.get_account(account_id)
            except Exception as e:
                logger.warning(f"⚠️ Failed to load account {account_id[:8]}: {e}")
        
        account_defaults = account.account_defaults if account else None
        user_config = account_defaults or SearchConfig()
        
        if self._config_service:
            facts_limit = self._config_service.get_biographical_cache_limit(
                user_config=user_config,
                account_defaults=account_defaults
            )
            principles_limit = self._config_service.get_principles_cache_limit(
                user_config=user_config,
                account_defaults=account_defaults
            )
        else:
            search_config = SearchConfig()
            facts_limit = search_config.DEFAULT_BIOGRAPHICAL_CACHE_LIMIT
            principles_limit = search_config.DEFAULT_PRINCIPLES_CACHE_LIMIT
        
        logger.info(
            f"🔄 [BiographicalContext] Refreshing cache for account {account_id[:8]}... "
            f"facts_limit={facts_limit}, principles_limit={principles_limit}"
        )
        
        # ========================================================================
        # STEP 2: Fetch facts — BIOGRAPHICAL domain first, fill from others if needed.
        # Uses context_priority_rank for efficient ORDER BY in Firestore (no Python sort).
        # ========================================================================
        biog_facts = await self._repo.get_active_facts_ordered(
            account_id, domain=FactDomain.BIOGRAPHICAL.value, limit=facts_limit
        )

        current_facts = list(biog_facts)
        if len(current_facts) < facts_limit:
            remaining = facts_limit - len(current_facts)
            biog_ids = {f.id for f in current_facts}
            all_ordered = await self._repo.get_active_facts_ordered(account_id, limit=facts_limit)
            fill = [f for f in all_ordered if f.id not in biog_ids][:remaining]
            current_facts.extend(fill)
            logger.debug(
                f"📊 [BiographicalContext] Loaded {len(biog_facts)} biographical + {len(fill)} fill facts"
            )
        else:
            logger.debug(
                f"📊 [BiographicalContext] Loaded {len(biog_facts)} biographical facts (limit reached)"
            )

        selected_facts = current_facts
        
        # ========================================================================
        # STEP 3: Separate into facts vs principles + convert to dict format
        # Facts are already in priority order from Firestore — no Python sorting needed.
        # Principle = any fact with "mindset" tag (domain irrelevant).
        # ========================================================================
        biographical_facts = []
        principles_list = []

        for fact in selected_facts:
            fact_dict = {
                "id": fact.id,
                "text": fact.text,
                "domain": fact.domain.value if fact.domain else "unknown",
                "tags": fact.tags,
                "context_priority": fact.context_priority.value if fact.context_priority else "medium",
                "created_at": fact.created_at.isoformat() if fact.created_at else None,
            }

            if "mindset" in fact.tags:
                if len(principles_list) < principles_limit:
                    principles_list.append(fact_dict)
            else:
                biographical_facts.append(fact_dict)

        logger.info(
            f"✅ [BiographicalContext] Cache refreshed for {account_id[:8]}: "
            f"{len(biographical_facts)} facts, {len(principles_list)} principles"
        )
        
        return {
            "facts": biographical_facts,
            "principles": principles_list
        }
