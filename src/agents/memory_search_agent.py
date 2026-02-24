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

import json
import time
from typing import List, Optional
from ..utils.debug_logger import get_debug_logger
from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..ports.llm_service import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
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

    # Structured output schema for LLM key formulation.
    # Gemini enforces this at API level; Claude will need separate handling when its adapter is fixed.
    # keywords: API-enforced 3-5 items (minItems/maxItems).
    # domains: API-enforced enum — only exact domain values accepted, max 2.
    MEMORY_SEARCH_RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "keywords": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": 3,
                "maxItems": 5,
            },
            "primary_query": {"type": "STRING", "maxLength": 50},
            "alternative_query": {"type": "STRING", "maxLength": 50},
            "domains": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": [
                        "biographical", "possession", "health", "medical_records",
                        "location", "work", "network", "preference", "skill",
                        "project", "finance", "education", "legal",
                        "entertainment", "communication",
                    ],
                },
                "maxItems": 2,
            },
        },
        "required": ["keywords", "primary_query", "alternative_query", "domains"],
    }
    
    def __init__(
        self,
        config: AgentConfig,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        account_id: str,
        search_enrichment: Optional[SearchEnrichmentPort] = None,
        execution_context: Optional[AgentExecutionContext] = None,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ):
        """
        Initialize Memory Search Agent.

        Args:
            config: Agent configuration
            repository: Fact repository for search
            embedding_service: Service for generating embeddings
            account_id: Account ID for data isolation (OAuth Multi-Tenant V3)
            search_enrichment: Optional enrichment service (if None, falls back to simple search)
            execution_context: LLM execution context for key formulation (Flash).
                               If provided, agent derives 3-key params from raw query via LLM.
            prompt_builder: Prompt builder for loading memorysearch system prompt from Firestore.
            user_id: User ID for prompt building.
        """
        super().__init__(config)
        self._repo = repository
        self._embedding = embedding_service
        self._account_id = account_id
        self._search_enrichment = search_enrichment
        self._llm = execution_context.provider if execution_context else None
        self._model_name = execution_context.model_name if execution_context else None
        self._prompt_builder = prompt_builder
        self._user_id = user_id

        logger.info(
            f"🧠 MemorySearchAgent initialized for account {account_id[:20]}... "
            f"(enrichment={'enabled' if search_enrichment else 'disabled'}, "
            f"llm={'enabled' if self._llm else 'disabled'})"
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
    
    async def _formulate_search_keys(self, query: str) -> dict:
        """
        Use Flash LLM to derive 3-key search parameters from a raw user query.

        Returns a dict with keys: keywords, primary_query, alternative_query, domains (optional).
        Falls back to minimal single-key dict on any error.
        """
        try:
            system_prompt = ""
            if self._prompt_builder:
                system_prompt = await self._prompt_builder.build_for_agent(
                    agent_type="memorysearch",
                    user_id=self._user_id,
                    account_id=self._account_id,
                    routing_metadata=None,
                    include_biographical=False,
                )

            user_text = f'SEARCH_REQUEST "{query}"'
            request = LLMRequest(
                model_name=self._model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=user_text)])],
                tools=[],
                temperature=0.0,
                max_tokens=150,
                disable_safety=True,
                response_mime_type="application/json",
                response_schema=self.MEMORY_SEARCH_RESPONSE_SCHEMA,
            )

            debug_logger = get_debug_logger()
            debug_logger.log_prompt(
                agent_name="memory_search",
                prompt=user_text,
                system_instruction=system_prompt,
                metadata={"user_id": (self._user_id or "")[:8], "query": query[:80]}
            )

            response = await self._llm.generate_content(request=request)
            raw_response = (response.text or "").strip()
            logger.info("🔍 [MemorySearchAgent] LLM raw response: %r", raw_response[:300])

            debug_logger.log_response(
                agent_name="memory_search",
                response=raw_response,
                metadata={"user_id": (self._user_id or "")[:8]}
            )

            keys = json.loads(raw_response)
            logger.info(
                f"🔍 [MemorySearchAgent] LLM formulated keys: "
                f"keywords={keys.get('keywords')}, "
                f"primary='{keys.get('primary_query', '')[:60]}', "
                f"domains={keys.get('domains')}"
            )
            return keys

        except Exception as e:
            logger.warning(f"⚠️ [MemorySearchAgent] Key formulation failed ({e}), falling back to raw query")
            return {}

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute memory search using 3-key multi-vector strategy.

        If an LLM is configured, derives search keys from raw query automatically.
        Otherwise expects pre-formulated keys in payload (legacy behaviour).
        """
        raw_query = message.payload.get("query", "")

        # --- LLM path: derive keys from query ---
        if self._llm and raw_query:
            logger.info(f"🔍 [MemorySearchAgent] === LLM key formulation for: '{raw_query[:80]}' ===")
            keys = await self._formulate_search_keys(raw_query)
            keywords = keys.get("keywords", [])
            primary_query = keys.get("primary_query", "")
            alternative_query = keys.get("alternative_query", "")
            domains = keys.get("domains") or None
        else:
            # --- Legacy path: keys pre-formulated by SmartAgent ---
            keywords = message.payload.get("keywords", [])
            primary_query = message.payload.get("primary_query", "")
            alternative_query = message.payload.get("alternative_query", "")
            domains = message.payload.get("domains", None)
            logger.info(
                f"🔍 [MemorySearchAgent] === Legacy keys from payload ===\n"
                f"   keywords: {keywords}\n"
                f"   primary_query: '{primary_query}'\n"
                f"   alternative_query: '{alternative_query}'\n"
                f"   domains: {domains if domains else 'N/A'}"
            )

        if not (keywords or primary_query or raw_query):
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No search keys provided"
            )

        start_time = time.time()

        try:
            if self._search_enrichment and (keywords or primary_query):
                logger.info(f"🔍 [MemorySearchAgent] Using SearchEnrichmentService (multi-vector RRF)")
                return await self._execute_enriched_search(
                    message=message,
                    keywords=keywords,
                    primary_query=primary_query or raw_query,
                    alternative_query=alternative_query,
                    domains=domains,
                    start_time=start_time,
                )
            else:
                logger.info(f"🔍 [MemorySearchAgent] Using legacy single-vector search (fallback)")
                return await self._execute_legacy_search(
                    message=message,
                    query=primary_query or raw_query,
                    start_time=start_time,
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
