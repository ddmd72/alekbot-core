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
from ..domain.entities import FactType, FactState, ContextPriority
from ..config.settings import SearchConfig
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
        # STEP 2: Get ALL CURRENT facts (direct query, no semantic search)
        # SESSION 2026-02-17: Repository filters state == CURRENT (only active facts)
        # ========================================================================
        current_facts = await self._repo.get_active_facts(
            owner_id=account_id,
            tags=None  # Get all facts, no filtering
        )
        
        logger.debug(
            f"📊 [BiographicalContext] Loaded {len(current_facts)} facts "
            f"(SUPERSEDED already filtered in Repository query)"
        )
        
        # ========================================================================
        # STEP 4: Group by context_priority (CRITICAL/HIGH/MEDIUM/LOW)
        # SESSION 2026-02-16: Sort within each priority by created_at DESC (newest first)
        # ========================================================================
        critical = []
        high = []
        medium = []
        low = []
        
        for fact in current_facts:
            priority = fact.context_priority or ContextPriority.MEDIUM  # Default MEDIUM
            
            if priority == ContextPriority.CRITICAL:
                critical.append(fact)
            elif priority == ContextPriority.HIGH:
                high.append(fact)
            elif priority == ContextPriority.MEDIUM:
                medium.append(fact)
            elif priority == ContextPriority.LOW:
                low.append(fact)
            # ARCHIVAL priority excluded automatically
        
        # Sort each priority group by created_at DESC (newest first)
        critical.sort(key=lambda f: f.created_at or "", reverse=True)
        high.sort(key=lambda f: f.created_at or "", reverse=True)
        medium.sort(key=lambda f: f.created_at or "", reverse=True)
        low.sort(key=lambda f: f.created_at or "", reverse=True)
        
        logger.debug(
            f"📊 [BiographicalContext] Priority distribution: "
            f"CRITICAL={len(critical)}, HIGH={len(high)}, "
            f"MEDIUM={len(medium)}, LOW={len(low)}"
        )
        
        # ========================================================================
        # STEP 5: Build combined list with intelligent limits
        # CRITICAL always ALL (over limit if necessary)
        # ========================================================================
        selected_facts = critical[:]  # ALL CRITICAL facts
        remaining_limit = max(0, facts_limit - len(selected_facts))
        
        # Add HIGH until limit
        selected_facts.extend(high[:remaining_limit])
        remaining_limit = max(0, facts_limit - len(selected_facts))
        
        # Add MEDIUM until limit
        selected_facts.extend(medium[:remaining_limit])
        remaining_limit = max(0, facts_limit - len(selected_facts))
        
        # Add LOW if space available
        selected_facts.extend(low[:remaining_limit])
        
        # ========================================================================
        # STEP 6: Separate into facts vs principles + convert to dict format
        # SESSION 2026-02-17: Mindset-based separation (tag "mindset")
        # ========================================================================
        biographical_facts = []
        principles_all = []
        
        # First pass: separate facts from principles (preserve priority order)
        # Principle = any fact with "mindset" tag (domain irrelevant)
        for fact in selected_facts:
            fact_dict = {
                "id": fact.id,
                "text": fact.text,
                "domain": fact.domain.value if fact.domain else "unknown",
                "tags": fact.tags,
                "context_priority": fact.context_priority.value if fact.context_priority else "medium",
                "created_at": fact.created_at.isoformat() if fact.created_at else None,
                "_priority_obj": fact.context_priority or ContextPriority.MEDIUM  # Temp for sorting
            }
            
            # SESSION 2026-02-17: Principle = "mindset" tag (any domain)
            if "mindset" in fact.tags:
                principles_all.append(fact_dict)
            else:
                biographical_facts.append(fact_dict)
        
        # Apply principles limit with same priority logic
        # CRITICAL principles always included (over limit if necessary)
        principles_critical = [p for p in principles_all if p["_priority_obj"] == ContextPriority.CRITICAL]
        principles_high = [p for p in principles_all if p["_priority_obj"] == ContextPriority.HIGH]
        principles_medium = [p for p in principles_all if p["_priority_obj"] == ContextPriority.MEDIUM]
        principles_low = [p for p in principles_all if p["_priority_obj"] == ContextPriority.LOW]
        
        principles_list = principles_critical[:]  # ALL CRITICAL principles
        remaining_limit = max(0, principles_limit - len(principles_list))
        
        principles_list.extend(principles_high[:remaining_limit])
        remaining_limit = max(0, principles_limit - len(principles_list))
        
        principles_list.extend(principles_medium[:remaining_limit])
        remaining_limit = max(0, principles_limit - len(principles_list))
        
        principles_list.extend(principles_low[:remaining_limit])
        
        # Remove temp priority object
        for p in principles_list:
            del p["_priority_obj"]
        for f in biographical_facts:
            del f["_priority_obj"]
        
        critical_facts_count = len([f for f in selected_facts if f.context_priority == ContextPriority.CRITICAL and f.type != FactType.PRINCIPLE])
        critical_principles_count = len([p for p in principles_list if p["context_priority"] == "critical"])
        
        logger.info(
            f"✅ [BiographicalContext] Cache refreshed for {account_id[:8]}: "
            f"{len(biographical_facts)} facts (CRITICAL={critical_facts_count} always included), "
            f"{len(principles_list)} principles (CRITICAL={critical_principles_count} always included)"
        )
        
        return {
            "facts": biographical_facts,
            "principles": principles_list
        }
