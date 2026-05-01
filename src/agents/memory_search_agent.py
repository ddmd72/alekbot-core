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
from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..domain.entities import FactType
from ..utils.logger import logger
from ..infrastructure.agent_config import MEMORY_SEARCH


class FactsMemoryAgent(BaseAgent):
    """
    Agent responsible for searching and saving user's personal memory (facts).

    Capabilities:
    - search_memory: Multi-vector RRF search (keywords + 2 semantic vectors)
    - save_to_memory: Attach text to user message for consolidation (no LLM call)
    """

    TEMPERATURE = MEMORY_SEARCH.temperature
    MAX_TOKENS = MEMORY_SEARCH.max_tokens
    RESULT_LIMIT = MEMORY_SEARCH.result_limit

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
            f"🧠 FactsMemoryAgent initialized for account {account_id[:20]}... "
            f"(enrichment={'enabled' if search_enrichment else 'disabled'}, "
            f"llm={'enabled' if self._llm else 'disabled'})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        Determine if this agent can handle the message.

        Accepts:
        - search_memory: intent=QUERY + (3-key format OR legacy query)
        - save_to_memory: intent=QUERY + "text" key in payload
        """
        logger.debug(
            f"🧠 [FactsMemoryAgent] can_handle check: "
            f"intent={message.intent}, payload keys={list(message.payload.keys())}"
        )

        if message.intent != AgentIntent.QUERY:
            logger.debug("🧠 [FactsMemoryAgent] can_handle=False (wrong intent)")
            return False

        # save_to_memory path: explicit intent flag only (not by "text" presence — LLMs
        # may fill context fields for any intent, causing false routing to save path)
        if message.payload.get("intent") == "save_to_memory":
            logger.debug("🧠 [FactsMemoryAgent] can_handle=True (save_to_memory)")
            return True

        # search_memory path: 3-key format OR legacy query
        has_3key = all(k in message.payload for k in ["keywords", "primary_query", "alternative_query"])
        has_legacy = "query" in message.payload

        if not (has_3key or has_legacy):
            logger.debug("🧠 [FactsMemoryAgent] can_handle=False (no search keys in payload)")
            return False

        logger.debug(f"🧠 [FactsMemoryAgent] can_handle=True (format={'3-key' if has_3key else 'legacy'})")
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
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                disable_safety=True,
                response_mime_type="application/json",
            )


            response = await self._call_llm(request)
            raw_response = (response.text or "").strip()
            logger.info("🔍 [FactsMemoryAgent] LLM raw response: %r", raw_response)

            keys = json.loads(raw_response)
            logger.info(
                f"🔍 [FactsMemoryAgent] LLM formulated keys: "
                f"keywords={keys.get('keywords')}, "
                f"primary='{keys.get('primary_query', '')[:60]}', "
                f"domains={keys.get('domains')}"
            )
            return keys

        except Exception as e:
            logger.warning(f"⚠️ [FactsMemoryAgent] Key formulation failed ({e}), falling back to raw query")
            return {}

    async def _handle_save(self, message: AgentMessage) -> AgentResponse:
        """Handle save_to_memory: attach text to user message for consolidation.

        Source priority:
        1. message.payload["text"] — LLM fills context={"text": "..."} → Quick spreads
           context_params via delegation_context["params"] → coordinator spreads into payload.
        2. message.payload["query"] — fallback: the brief task description only.
        """
        text = (message.payload.get("text") or message.payload.get("query", "")).strip()
        if not text:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="save_to_memory: empty text",
            )
        self._on_agent_start(text)
        logger.info(f"💾 [FactsMemoryAgent] save_to_memory: {text[:80]}")
        self._on_agent_success(char_count=len(text), token_count=0, output_text=text)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={"saved": True},
            history_context={"consolidation_text": text},
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute memory search or save.

        Routes by payload content:
        - "text" key present → save_to_memory (LLM filled context={"text":"..."} → spread into payload)
        - "intent"=="save_to_memory" without "text" → save_to_memory (fallback: query only)
        - otherwise → search_memory (multi-vector RRF)
        """
        if message.payload.get("intent") == "save_to_memory":
            return await self._handle_save(message)

        raw_query = message.payload.get("query", "")
        self._on_agent_start(raw_query)

        # --- LLM path: derive keys from query ---
        if self._llm and raw_query:
            logger.info(f"🔍 [FactsMemoryAgent] === LLM key formulation for: '{raw_query}' ===")
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
                f"🔍 [FactsMemoryAgent] === Legacy keys from payload ===\n"
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
                logger.info(f"🔍 [FactsMemoryAgent] Using SearchEnrichmentService (multi-vector RRF)")
                return await self._execute_enriched_search(
                    message=message,
                    keywords=keywords,
                    primary_query=primary_query or raw_query,
                    alternative_query=alternative_query,
                    domains=domains,
                    start_time=start_time,
                )
            else:
                logger.info(f"🔍 [FactsMemoryAgent] Using legacy single-vector search (fallback)")
                return await self._execute_legacy_search(
                    message=message,
                    query=primary_query or raw_query,
                    start_time=start_time,
                )

        except Exception as e:
            self._on_agent_error(e)
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
            f"🔍 [FactsMemoryAgent] Starting multi-vector RRF search:\n"
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

        # Format each fact with supplementary fields (context, reported_date, metadata)
        result_str = "\n---\n".join(self._format_fact_rich(f) for f in enriched_facts)
        
        total_duration = time.time() - start_time
        
        logger.info(
            f"✅ [FactsMemoryAgent] Multi-vector RRF completed:\n"
            f"   → Total facts: {len(enriched_facts)}\n"
            f"   → Duration: {total_duration:.2f}s\n"
            f"   → Enrichment: {enrichment_duration:.2f}s\n"
            f"   → Dedup: {enriched_context.dedup_count}"
        )
        
        # Log top results for debugging
        for i, fact in enumerate(enriched_facts[:3]):
            logger.debug(f"      [{i+1}] {fact.content}")
        
        # Calculate confidence
        fact_count = len(enriched_facts)
        confidence = min(1.0, fact_count / 5.0) if fact_count else 0.0

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result_str,
            confidence=confidence,
            metadata={
                "search_strategy": "multi_vector_rrf",
                "total_duration_ms": int(total_duration * 1000),
                "enrichment_duration_ms": int(enrichment_duration * 1000),
                "result_count": fact_count,
                "total_facts": fact_count,
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
        
        logger.info(f"🔍 [FactsMemoryAgent] Starting legacy search: '{query}'")
        
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
        facts = await self._repo.search_facts(query_vector, limit=self.RESULT_LIMIT)
        search_duration = time.time() - search_start
        logger.info(f"   ✓ Search completed in {search_duration:.2f}s, found {len(facts)} facts")
        
        # 3. Filter and format results
        filtered_facts = [f for f in facts if getattr(f, "type", None) != FactType.PRINCIPLE]
        result_str = "\n---\n".join(self._format_fact_rich(f) for f in filtered_facts)

        total_duration = time.time() - start_time
        fact_count = len(filtered_facts)

        logger.info(
            f"✅ [FactsMemoryAgent] Legacy search completed: {fact_count}/{len(facts)} non-anchor results "
            f"in {total_duration:.2f}s"
        )

        # Log top results
        for i, fact in enumerate(filtered_facts[:3]):
            logger.debug(f"      [{i+1}] {fact.text}")

        confidence = min(1.0, fact_count / 5.0) if fact_count else 0.0

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result_str,
            confidence=confidence,
            metadata={
                "search_strategy": "legacy_single_vector",
                "total_duration_ms": int(total_duration * 1000),
                "embedding_duration_ms": int(emb_duration * 1000),
                "search_duration_ms": int(search_duration * 1000),
                "result_count": fact_count,
                "account_id": self._account_id,
                "query": query
            }
        )
    
    def _format_fact_rich(self, fact) -> str:
        """Format a single fact with supplementary fields for LLM consumption."""
        parts = [fact.content if hasattr(fact, "content") else fact.text]
        context = getattr(fact, "context", None)
        reported_date = getattr(fact, "reported_date", None)
        metadata = getattr(fact, "metadata", None)
        if context:
            parts.append(f"context: {context}")
        if reported_date:
            date_str = str(reported_date)[:10]
            parts.append(f"reported: {date_str}")
        if metadata:
            parts.append(f"metadata: {json.dumps(metadata, ensure_ascii=False)}")
        return "\n".join(parts)

    def _get_alternative_agents(self) -> List[str]:
        """Suggest alternative agents if this one cannot handle the request."""
        return ["web_search_agent", "reasoning_agent"]
