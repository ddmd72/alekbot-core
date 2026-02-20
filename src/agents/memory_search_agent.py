"""
Memory Search Agent
===================

Specialized agent for searching user's personal long-term memory.
Uses multi-vector RRF search via SearchEnrichmentService for optimal recall.

SESSION_2026_02_09: Updated to support 3-key search strategy:
- keywords: Domain keywords for tag-based matching
- primary_query: Main semantic search phrase
- alternative_query: Alternative phrasing for diversity
"""

import time
from typing import List, Optional
from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..services.search_enrichment_service import SearchEnrichmentService
from ..domain.entities import FactType
from ..utils.logger import logger


class MemorySearchAgent(BaseAgent):
    """
    Agent responsible for searching user's personal memory archive.
    
    Capabilities:
    - Multi-vector RRF search (keywords + 2 semantic vectors)
    - Personal data retrieval
    - Historical fact lookup
    
    Does NOT require LLM - pure search with enrichment.
    """
    
    def __init__(
        self,
        config: AgentConfig,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        account_id: str,
        search_enrichment: Optional[SearchEnrichmentService] = None
    ):
        """
        Initialize Memory Search Agent.
        
        Args:
            config: Agent configuration
            repository: Fact repository for search
            embedding_service: Service for generating embeddings
            account_id: Account ID for data isolation (OAuth Multi-Tenant V3)
            search_enrichment: Optional enrichment service (if None, falls back to simple search)
        """
        super().__init__(config)
        self._repo = repository
        self._embedding = embedding_service
        self._account_id = account_id
        self._search_enrichment = search_enrichment
        
        logger.info(
            f"🧠 MemorySearchAgent initialized for account {account_id[:20]}... "
            f"(enrichment={'enabled' if search_enrichment else 'disabled'})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        Determine if this agent can handle the message.
        
        MemorySearchAgent is an executor, not a decision maker.
        SmartResponseAgent's LLM already decided to delegate here.
        
        We validate:
        - Intent must be QUERY
        - Payload must have search keys (3-key format or legacy query)
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if agent can process this message
        """
        logger.debug(
            f"🧠 [MemorySearchAgent] can_handle check: "
            f"intent={message.intent}, payload keys={list(message.payload.keys())}"
        )
        
        # Check intent
        if message.intent != AgentIntent.QUERY:
            logger.debug("🧠 [MemorySearchAgent] can_handle=False (wrong intent)")
            return False
        
        # Check 3-key format OR legacy query
        has_3key = all(k in message.payload for k in ["keywords", "primary_query", "alternative_query"])
        has_legacy = "query" in message.payload
        
        if not (has_3key or has_legacy):
            logger.debug("🧠 [MemorySearchAgent] can_handle=False (no search keys in payload)")
            return False
        
        logger.debug(f"🧠 [MemorySearchAgent] can_handle=True (format={'3-key' if has_3key else 'legacy'})")
        return True
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute memory search using 3-key multi-vector strategy.
        
        Args:
            message: Agent message containing search keys
            
        Returns:
            Agent response with search results
        """
        # Extract search keys (support both 3-key and legacy format)
        keywords = message.payload.get("keywords", [])
        primary_query = message.payload.get("primary_query", "")
        alternative_query = message.payload.get("alternative_query", "")
        legacy_query = message.payload.get("query", "")
        domains = message.payload.get("domains", None)

        # Log what SmartAgent sent
        logger.info(
            f"🔍 [MemorySearchAgent] === TOOL CALL FROM SmartAgent ===\n"
            f"   keywords: {keywords}\n"
            f"   primary_query: '{primary_query}'\n"
            f"   alternative_query: '{alternative_query}'\n"
            f"   domains: {domains if domains else 'N/A'}\n"
            f"   legacy_query: '{legacy_query if legacy_query else 'N/A'}'"
        )
        
        # Validate input
        if not (keywords or primary_query or legacy_query):
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No search keys provided (need keywords/primary_query/alternative_query or legacy query)"
            )
        
        start_time = time.time()
        
        try:
            # Use enrichment service if available and 3-key format provided
            if self._search_enrichment and keywords and primary_query:
                logger.info(f"🔍 [MemorySearchAgent] Using SearchEnrichmentService (multi-vector RRF)")
                return await self._execute_enriched_search(
                    message=message,
                    keywords=keywords,
                    primary_query=primary_query,
                    alternative_query=alternative_query,
                    domains=domains,
                    start_time=start_time
                )
            else:
                # Fallback to legacy single-vector search
                logger.info(f"🔍 [MemorySearchAgent] Using legacy single-vector search (fallback)")
                fallback_query = primary_query or legacy_query
                return await self._execute_legacy_search(
                    message=message,
                    query=fallback_query,
                    start_time=start_time
                )
                
        except Exception as e:
            logger.error(f"❌ [MemorySearchAgent] Error: {e}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Memory search failed: {str(e)}"
            )
    
    async def _execute_enriched_search(
        self,
        message: AgentMessage,
        keywords: List[str],
        primary_query: str,
        alternative_query: str,
        domains: Optional[List[str]],
        start_time: float
    ) -> AgentResponse:
        """Execute search using SearchEnrichmentService (multi-vector RRF + optional domain channel)."""

        logger.info(
            f"🔍 [MemorySearchAgent] Starting multi-vector RRF search:\n"
            f"   → Keywords: {keywords}\n"
            f"   → Primary: '{primary_query}'\n"
            f"   → Alternative: '{alternative_query}'\n"
            f"   → Domains: {domains if domains else 'none (vector-only)'}"
        )

        # Use SearchEnrichmentService for multi-vector RRF (+ domain channel if provided)
        enrichment_start = time.time()
        enriched_context = await self._search_enrichment.enrich_context(
            keywords=keywords,
            search_phrase_1=primary_query,
            search_phrase_2=alternative_query,
            relevant_domains=domains,  # Activates domain-direct channel (7th query) if provided
            limits=None  # Use defaults
        )
        enrichment_duration = time.time() - enrichment_start
        
        # Extract facts from enriched context (EnrichedContext dataclass)
        enriched_facts = enriched_context.facts  # List[EnrichedFact]
        
        # EnrichedFact already filtered by SearchEnrichmentService
        # Extract content from EnrichedFact objects
        results = [f.content for f in enriched_facts]
        
        total_duration = time.time() - start_time
        
        logger.info(
            f"✅ [MemorySearchAgent] Multi-vector RRF completed:\n"
            f"   → Total facts: {len(enriched_facts)}\n"
            f"   → Duration: {total_duration:.2f}s\n"
            f"   → Enrichment: {enrichment_duration:.2f}s\n"
            f"   → Dedup: {enriched_context.dedup_count}"
        )
        
        # Log top results for debugging
        for i, fact in enumerate(enriched_facts[:3]):
            logger.debug(f"      [{i+1}] {fact.content[:80]}...")
        
        # Calculate confidence
        confidence = min(1.0, len(results) / 5.0) if results else 0.0
        
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=results,
            confidence=confidence,
            metadata={
                "search_strategy": "multi_vector_rrf",
                "total_duration_ms": int(total_duration * 1000),
                "enrichment_duration_ms": int(enrichment_duration * 1000),
                "result_count": len(results),
                "total_facts": len(enriched_facts),
                "dedup_count": enriched_context.dedup_count,
                "total_sources": enriched_context.total_sources,
                "account_id": self._account_id,
                "keywords": keywords,
                "primary_query": primary_query,
                "alternative_query": alternative_query,
                "domains": domains
            }
        )
    
    async def _execute_legacy_search(
        self,
        message: AgentMessage,
        query: str,
        start_time: float
    ) -> AgentResponse:
        """Execute legacy single-vector search (fallback)."""
        
        logger.info(f"🔍 [MemorySearchAgent] Starting legacy search: '{query[:50]}...'")
        
        # 1. Generate embedding
        emb_start = time.time()
        query_vector = await self._embedding.get_embedding(
            query,
            task_type="RETRIEVAL_QUERY"
        )
        emb_duration = time.time() - emb_start
        logger.debug(f"   ✓ Embedding generated in {emb_duration:.2f}s")
        
        # 2. Perform vector search
        search_start = time.time()
        facts = await self._repo.search_facts(query_vector, limit=10)
        search_duration = time.time() - search_start
        logger.info(f"   ✓ Search completed in {search_duration:.2f}s, found {len(facts)} facts")
        
        # 3. Filter and format results
        filtered_facts = [f for f in facts if getattr(f, "type", None) != FactType.PRINCIPLE]
        results = [f.text for f in filtered_facts]
        
        total_duration = time.time() - start_time
        
        logger.info(
            f"✅ [MemorySearchAgent] Legacy search completed: {len(results)}/{len(facts)} non-anchor results "
            f"in {total_duration:.2f}s"
        )
        
        # Log top results
        for i, fact in enumerate(filtered_facts[:3]):
            logger.debug(f"      [{i+1}] {fact.text[:80]}...")
        
        confidence = min(1.0, len(results) / 5.0) if results else 0.0
        
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=results,
            confidence=confidence,
            metadata={
                "search_strategy": "legacy_single_vector",
                "total_duration_ms": int(total_duration * 1000),
                "embedding_duration_ms": int(emb_duration * 1000),
                "search_duration_ms": int(search_duration * 1000),
                "result_count": len(results),
                "account_id": self._account_id,
                "query": query
            }
        )
    
    def _get_alternative_agents(self) -> List[str]:
        """Suggest alternative agents if this one cannot handle the request."""
        return ["web_search_agent", "reasoning_agent"]
