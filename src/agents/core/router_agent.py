"""
Router Agent
============

Classifies incoming messages and routes them to appropriate agents.
This is the entry point for all user queries in the agent network.

NO LLM required - uses rule-based classification for speed.
"""

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Set, List
from ..base_agent import BaseAgent
from ...domain.agent import (
    AgentMessage,
    AgentResponse,
    AgentConfig,
    AgentIntent,
    AgentStatus,
    RoutingMetadata
)
from ...domain.tone import UserTone, build_routing_metadata
from ...ports.llm_service import LLMService, Message, MessagePart, LLMRequest
from ...ports.session_store import SessionStore
from ...ports.repository import FactRepository
from ...ports.embedding_service import EmbeddingService
from ...services.search_enrichment_service import SearchEnrichmentService
from ...services.prompt_builder import PromptBuilder
from ...ports.llm_service import AgentExecutionContext
from ...utils.logger import logger
from ...utils.debug_logger import get_debug_logger


class RouterAgent(BaseAgent):
    """
    Router Agent - classifies and routes messages to specialized agents.
    
    Responsibilities:
    - Classify messages as simple vs complex
    - Detect personal data queries (need memory search)
    - Detect external data queries (need web search)
    - Route to QuickResponseAgent or SmartResponseAgent
    
    Does NOT require LLM - pure rule-based classification.
    
    Classification Logic:
    - Simple requests → QuickResponseAgent (fast, cheap)
    - Complex requests → SmartResponseAgent (reasoning, tools)
    
    Example simple requests:
    - Greetings: "Hi", "Hello"
    - Acknowledgments: "Ok", "Thanks"
    - Simple questions with quick answers

    Example complex requests:
    - Personal data queries: "What shoe size do I wear?"
    - External data queries: "What's the weather in Valencia?"
    - Multi-step reasoning: "Compare these two options..."
    """
    
    # Simple phrases that don't require complex processing
    SIMPLE_PHRASES: Set[str] = {
        # Ukrainian
        "привіт", "привет", "прив", 
        "ок", "окей", "добре", "гаразд",
        "дякую", "спасибі", "спасибо",
        "бувай", "до побачення", "пока",
        "доброго ранку", "добрий ранок", "добрий вечір", "добрий день",
        "як справи", "як справи?",
        "ага", "угу", "ясно", "зрозуміло",
        # Russian
        "привет", "приветик",
        "спасибо", "благодарю",
        "до свидания", "пока",
        "доброе утро", "добрый день", "добрый вечер",
        "как дела", "как дела?",
        # English
        "hello", "hi", "hey",
        "ok", "okay",
        "thanks", "thank you",
        "bye", "goodbye",
        "good morning", "good evening",
        "how are you", "what's up"
    }
    
    # Short acknowledgment phrases
    SHORT_ACKNOWLEDGMENTS: Set[str] = {
        "ок", "ага", "угу", "да", "так", 
        "ні", "нет", "no", "yes", "ok",
        "добре", "хорошо", "fine"
    }
    
    # Keywords indicating personal data queries (need memory search)
    PERSONAL_KEYWORDS: Set[str] = {
        # Ukrainian
        "мій", "моя", "моє", "мої", "мене", "мені",
        "у мене", "в мене",
        # Russian  
        "мой", "моя", "моё", "мои", "меня", "мне",
        "у меня",
        # English
        "my", "mine", "me"
    }
    
    # Keywords indicating external search needed
    EXTERNAL_SEARCH_KEYWORDS: Set[str] = {
        # Commands
        "поищи", "найди", "пошукай", "пошук", "поиск",
        "гугл", "гугли", "погугли", "search", "find", "look up",
        # Topics requiring external data
        "погода", "прогноз", "weather", "forecast",
        "новости", "news", "новини",
        "курс", "rate", "price", "ціна", "цена", "стоимость", "вартість",
        "купить", "заказать", "delivery", "доставка",
        "рейс", "flight", "отель", "hotel", "готель",
        "ресторан", "restaurant",
        "затмение", "eclipse", "солнечное", "лунное"
    }

    # Session 2026-02-17: Router v3 - Domain-based search + simplified schema
    TRIAGE_RESPONSE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "target_agent": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
            "reasoning": {"type": "STRING"},
            "search_intent": {"type": "STRING"},
            "relevant_domains": {
                "type": "ARRAY",
                "items": {"type": "STRING"}
            },
            "semantic_lens": {
                "type": "ARRAY",
                "items": {"type": "STRING"}
            },
            "search_phrase": {"type": "STRING"},
            "metadata": {
                "type": "OBJECT",
                "properties": {
                    "user_tone": {"type": "STRING"},
                    "complexity_score": {"type": "INTEGER"}
                },
                "required": ["user_tone", "complexity_score"]
            }
        },
        "required": [
            "target_agent",
            "confidence",
            "reasoning",
            "search_intent",
            "relevant_domains",
            "semantic_lens",
            "search_phrase",
            "metadata"
        ]
    }
    
    def __init__(
        self,
        config: AgentConfig,
        execution_context: Optional[AgentExecutionContext] = None,
        coordinator: "AgentCoordinator" = None,  # type: ignore
        quick_agent_id: str = "quick_response_agent",
        smart_agent_id: str = "smart_response_agent",
        user_id: Optional[str] = None,
        # ============================================================================
        # LEGACY Provider Refactor Session 20: Direct LLM dependency
        # Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md
        # Reason: Replaced by AgentExecutionContext for hexagonal compliance
        # Removal: After Session 26 validation + production deployment
        # ============================================================================
        # llm_service: Optional[LLMService] = None,
        session_store: Optional[SessionStore] = None,
        repository: Optional[FactRepository] = None,
        embedding_service: Optional[EmbeddingService] = None,
        search_enrichment_service: Optional[SearchEnrichmentService] = None,
        # ============================================================================
        # LEGACY Provider Refactor Session 20: Hardcoded model name
        # Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md
        # Reason: Model selection delegated to AgentContextBuilder via tiers
        # Removal: After Session 26 validation + production deployment
        # ============================================================================
        # classifier_model: str = "gemini-3-flash-preview",
        triage_prompt_path: Optional[Path] = None,
        prompt_builder: Optional[PromptBuilder] = None
    ):
        """
        Initialize Router Agent.

        Args:
            config: Agent configuration
            execution_context: Agent execution context with provider and model
            coordinator: AgentCoordinator for routing messages
            quick_agent_id: ID of the quick response agent
            smart_agent_id: ID of the smart response agent
            user_id: Optional user id for per-user router naming
            session_store: Session store for conversation history
        """
        super().__init__(config)
        self.execution_context = execution_context
        self.coordinator = coordinator
        self.quick_agent_id = quick_agent_id
        self.smart_agent_id = smart_agent_id

        self.user_id = user_id
        self.llm = execution_context.provider if execution_context else None
        self.model_name = execution_context.model_name if execution_context else "gemini-3-flash-preview"
        self.session_store = session_store
        self.repository = repository
        self.embedding_service = embedding_service
        self.search_enrichment_service = search_enrichment_service
        self.prompt_builder = prompt_builder
        
        # Use absolute path relative to this file to avoid issues in Docker
        base_dir = Path(__file__).parent.parent.parent.parent
        self.triage_prompt_path = triage_prompt_path or (
            base_dir / "src/agents/prompts/triage_router_v1.prompt"
        )
        
        self._cached_triage_prompt: Optional[str] = None
        if self.user_id and self.agent_id == "router_agent":
            self.agent_id = f"router_agent_{self.user_id}"

        logger.info(
            "🎯 RouterAgent initialized "
            "(quick=%s, smart=%s, llm=%s, history=%s, prompt=%s, enrichment=%s)",
            quick_agent_id,
            smart_agent_id,
            "enabled" if self.llm else "disabled",
            "enabled" if self.session_store else "disabled",
            self.triage_prompt_path,
            "enabled" if self.search_enrichment_service else "disabled"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        RouterAgent can handle QUERY intents with text or attachments.
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if this is a query with text content or attachments
        """
        if message.intent != AgentIntent.QUERY:
            return False
        
        # Must have text or attachments in payload
        has_text = "text" in message.payload and bool(message.payload["text"])
        has_attachments = bool(message.payload.get("attachments"))
        
        return has_text or has_attachments
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Classify and route message to appropriate agent.
        
        Args:
            message: Agent message to process
            
        Returns:
            AgentResponse from the target agent
        """
        text = message.payload.get("text", "")
        session_id = message.context.get("session_id")
        current_parts = message.context.get("current_message_parts", [])
        
        # ====================================================================
        # ADMIN COMMAND: $admin_cache_reset
        # RFC: docs/10_rfcs/PROMPT_ASSEMBLY_CACHING_RFC.md
        # Purpose: Clear all prompt assembly caches for debugging
        # ====================================================================
        if text.strip() == "$admin_cache_reset":
            return await self._handle_admin_cache_reset()
        
        logger.info(
            f"🎯 [RouterAgent] Classifying: '{text[:50]}...'" 
            if len(text) > 50 else f"🎯 [RouterAgent] Classifying: '{text}'"
        )

        # Load conversation history for context-aware triage (last 5 messages)
        history = []
        if self.session_store and session_id:
            history = await self._load_conversation_context(
                self.session_store,
                session_id,
                current_parts,
                context_window=5
            )
        
        # Classify the request (LLM triage preferred)
        classification = await self._classify_request_with_fallback(text, message, history)
        routing_metadata = build_routing_metadata(classification)
        
        # Vision override: if message has attachments, it's complex and needs Smart agent
        if any(p.file_data for p in current_parts):
            logger.info("📸 [RouterAgent] Vision detected, forcing complexity=7")
            routing_metadata.complexity_score = max(routing_metadata.complexity_score, 7)

        logger.info(
            "🎯 [RouterAgent] Triage result: complexity=%s, confidence=%.2f, tone=%s, lens=%s",
            routing_metadata.complexity_score,
            routing_metadata.confidence,
            routing_metadata.user_tone,
            routing_metadata.semantic_lens
        )

        target_agent = self._apply_routing_rules(routing_metadata)

        # Session 2026-02-17: Router v3 - Check search_intent before enriching
        enriched_context = None
        search_intent = classification.get("search_intent", "topic")
        
        if search_intent == "none":
            logger.info("🎯 [RouterAgent] search_intent=none, skipping enrichment")
        elif self.search_enrichment_service and self.user_id:
            biographical_facts = None
            if self.repository:
                try:
                    # SESSION_27: Auto-resolve from RequestContext (set by ConversationHandler)
                    biographical_facts = await self.repository.get_biographical_context_cached(
                        limit=100  # owner_id auto-resolved from RequestContext
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️ [RouterAgent] Failed to load biographical cache: %s",
                        exc
                    )

            # Session 2026-02-17: Router v3 - New parameters
            # SESSION_27: enrich_context uses RequestContext automatically
            enriched_context = await self.search_enrichment_service.enrich_context(
                keywords=classification.get("semantic_lens", []),
                search_phrase_1=classification.get("search_phrase", ""),  # Renamed
                search_phrase_2="",  # Deprecated, always empty
                relevant_domains=classification.get("relevant_domains", []),  # NEW
                biographical_facts=biographical_facts
            )
        
        # If no coordinator, return classification result
        # (for testing without full agent network)
        if self.coordinator is None:
            is_simple = classification.get("is_simple")
            if is_simple is None:
                is_simple = target_agent == self.quick_agent_id
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={
                    "routed_to": target_agent,
                    "classification": classification
                },
                confidence=1.0,
                metadata={
                    "target_agent": target_agent,
                    "is_simple": is_simple,
                    "is_personal": classification.get("is_personal"),
                    "needs_external": classification.get("needs_external")
                }
            )
        
        # Create message for target agent
        routed_message = AgentMessage.create(
            sender=self.agent_id,
            recipient=target_agent,
            intent=AgentIntent.QUERY,
            payload=message.payload,
            context={
                **message.context,
                "classification": classification,
                "routing": routing_metadata.to_dict(),
                "enriched_context": asdict(enriched_context) if enriched_context else None,
                "routed_by": self.agent_id
            },
            priority=message.priority,
            timeout_ms=message.timeout_ms
        )
        
        logger.info(f"🎯 [RouterAgent] Routing to {target_agent}")
        
        # Route to target agent
        response = await self.coordinator.route_message(routed_message)
        
        return response
    
    async def _classify_request_with_fallback(self, text: str, message: AgentMessage, history: List[Message] = None) -> dict:
        """Run LLM triage when available; fallback to rule-based logic."""
        if not self.llm:
            return self._classify_request(text)

        try:
            return await self._classify_with_llm(text, message, history)
        except Exception as exc:
            logger.warning(
                "⚠️ [RouterAgent] LLM triage failed: %s. Falling back to rule-based.",
                exc
            )
            return self._classify_request(text)

    async def _load_triage_prompt(self, message: AgentMessage) -> str:
        if self._cached_triage_prompt is None:
            prompt = None
            if self.prompt_builder:
                try:
                    # SESSION_2026_02_08: Extract account_id from message context (same as QuickAgent)
                    account_id = message.context.get("account_id")
                    
                    prompt = await self.prompt_builder.build_for_agent(
                        agent_type="router",
                        user_id=self.user_id,
                        account_id=account_id,  # FIX: Extract from message.context
                        routing_metadata=None
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️ [RouterAgent] Failed to build v3 prompt, falling back to static: %s",
                        exc
                    )

            if prompt:
                self._cached_triage_prompt = prompt
            else:
                if not self.triage_prompt_path.exists():
                    import os
                    cwd = os.getcwd()
                    parent_dir = self.triage_prompt_path.parent
                    exists_parent = parent_dir.exists()
                    files_in_parent = os.listdir(parent_dir) if exists_parent else "N/A"
                    
                    logger.error(
                        f"❌ [RouterAgent] Triage prompt NOT FOUND at {self.triage_prompt_path}. "
                        f"CWD: {cwd}, Parent exists: {exists_parent}, Files in parent: {files_in_parent}"
                    )
                    raise FileNotFoundError(f"Triage prompt not found at {self.triage_prompt_path}")
                self._cached_triage_prompt = self.triage_prompt_path.read_text()
        return self._cached_triage_prompt

    async def _classify_with_llm(self, text: str, message: AgentMessage, history: List[Message] = None) -> dict:
        """Use LLM triage to classify tone/complexity/tool needs."""
        user_id = message.context.get("user_id", "unknown")
        prompt = await self._load_triage_prompt(message)

        # IMPORTANT: Triage agent should NOT see file_data (images) to avoid 
        # trying to answer the user's question instead of classifying.
        # We only send text parts.
        clean_messages = []
        
        source_messages = history if history else [Message(role="user", parts=[MessagePart(text=text)])]
        
        for msg in source_messages:
            text_parts = [p for p in msg.parts if p.text]
            if text_parts:
                clean_messages.append(Message(role=msg.role, parts=text_parts))

        # Ensure we don't send empty messages to LLM
        if not clean_messages:
             return self._classify_request(text)

        # DEBUG: Log prompt before LLM call
        debug_logger = get_debug_logger()
        history_str = "\n".join([
            f"{msg.role}: {' | '.join([p.text[:100] if p.text else f'[{type(p).__name__}]' for p in msg.parts])}"
            for msg in clean_messages[-5:]  # Last 5 messages
        ])
        debug_logger.log_prompt(
            agent_name="router_agent",
            prompt=history_str,
            system_instruction=prompt,
            metadata={"user_id": user_id[:8] if user_id else "unknown"}
        )

        # ============================================================================
        # NEW Provider Refactor Session 20: Use LLMRequest for unified interface
        # Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md
        # ============================================================================
        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=prompt,
            messages=clean_messages,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=self.TRIAGE_RESPONSE_SCHEMA
        )
        
        response = await self.llm.generate_content(request=request)

        raw_text = (response.text or "").strip()
        
        # DEBUG: Log response
        debug_logger.log_response(
            agent_name="router_agent",
            response=raw_text,
            metadata={
                "user_id": user_id[:8] if user_id else "unknown",
                "tokens": response.usage_metadata.total_tokens if response.usage_metadata else 0
            }
        )
        
        # Robust JSON extraction
        json_match = re.search(r"(\{.*\})", raw_text, re.DOTALL)
        if json_match:
            clean_json = json_match.group(1)
            try:
                classification = json.loads(clean_json)
                metadata = classification.get("metadata", {})
                metadata["user_tone"] = UserTone.validate(metadata.get("user_tone", "friendly"))
                classification["metadata"] = metadata
                return classification
            except json.JSONDecodeError as e:
                logger.error(f"❌ [RouterAgent] Failed to parse extracted JSON: {clean_json[:100]}...")
                raise e
        
        logger.error(f"❌ [RouterAgent] No JSON found in LLM response: {raw_text[:200]}...")
        raise ValueError("No valid JSON found in triage response")

    def _apply_routing_rules(self, routing_metadata: RoutingMetadata) -> str:
        """Apply explicit routing rules. LLM recommendation is the primary signal."""
        # Complexity threshold — disabled, LLM routing preferred
        # if routing_metadata.complexity_score >= 6:
        #     return self.smart_agent_id

        if routing_metadata.confidence < 0.75:
            return self.smart_agent_id

        if routing_metadata.llm_target_agent and self.smart_agent_id.startswith(routing_metadata.llm_target_agent):
            return self.smart_agent_id

        return self.quick_agent_id  # fallback

    def _classify_request(self, text: str) -> dict:
        """
        Classify the request into categories.
        
        Returns:
            dict with classification results:
            - is_simple: Simple greeting/acknowledgment
            - is_personal: References user's personal data
            - needs_external: Requires external search
            - semantic_lens: Basic keywords for search
        """
        is_simple = self._is_simple_request(text)
        is_personal = self._is_personal_request(text)
        needs_external = self._requires_external_search(text)
        
        # Override: if it needs personal or external data, it's NOT simple
        if is_personal or needs_external:
            is_simple = False
        
        # Basic keyword extraction for fallback
        clean_text = re.sub(r'[^\w\s]', ' ', text).lower()
        words = clean_text.split()
        lens = [w for w in words if len(w) > 3][:5]
        
        return {
            "is_simple": is_simple,
            "is_personal": is_personal,
            "needs_external": needs_external,
            "semantic_lens": lens
        }
    
    def _is_simple_request(self, text: str) -> bool:
        """
        Check if request is simple (greeting, acknowledgment, etc.)
        
        Ported from BrainService.is_simple_request()
        
        Simple requests:
        - Greetings: "Hi", "Hello"
        - Acknowledgments: "Ok", "Thanks"
        - Very short phrases (1-2 words)
        
        Args:
            text: User message text
            
        Returns:
            True if request is simple
        """
        # Normalize text
        normalized = re.sub(r'[^\w\s]', '', text).lower().strip()
        
        if not normalized:
            return True
        
        # Check exact match with simple phrases
        if normalized in self.SIMPLE_PHRASES:
            return True
        
        # Check short acknowledgments
        if normalized in self.SHORT_ACKNOWLEDGMENTS:
            return True
        
        # Check if very short (1-3 tokens) with acknowledgment words
        tokens = normalized.split()
        if len(tokens) <= 3:
            if any(t in self.SHORT_ACKNOWLEDGMENTS for t in tokens):
                return True
        
        return False
    
    def _is_personal_request(self, text: str) -> bool:
        """
        Check if request references user's personal data.
        
        Ported from BrainService.is_personal_request()
        
        Personal requests need memory search to retrieve facts.
        
        Examples:
        - "What's my shoe size?"
        - "Remind me about the meeting"
        - "What's my car model?"
        
        Args:
            text: User message text
            
        Returns:
            True if request is about personal data
        """
        normalized = re.sub(r"[^\w\s]", " ", text.lower()).strip()
        
        if not normalized:
            return False
        
        return any(keyword in normalized for keyword in self.PERSONAL_KEYWORDS)
    
    def _requires_external_search(self, text: str) -> bool:
        """
        Check if request requires external (web) search.
        
        Ported from BrainService.requires_external_search()
        
        External search needed for:
        - Current events (news, weather)
        - Real-time data (prices, flights)
        - General knowledge not in memory
        
        Args:
            text: User message text
            
        Returns:
            True if external search is needed
        """
        normalized = re.sub(r"[^\w\s]", " ", text.lower()).strip()
        
        if not normalized:
            return False
        
        # Personal requests don't need external search
        if self._is_personal_request(text):
            return False
        
        return any(keyword in normalized for keyword in self.EXTERNAL_SEARCH_KEYWORDS)
    
    async def _handle_admin_cache_reset(self) -> AgentResponse:
        """
        Handle $admin_cache_reset admin command.
        
        RFC: docs/10_rfcs/PROMPT_ASSEMBLY_CACHING_RFC.md
        
        Clears all prompt assembly caches in the system.
        Used for debugging and after prompt/token updates.
        
        Returns:
            AgentResponse with confirmation message
        """
        logger.warning("🔥 ADMIN: Cache reset command received")
        
        # Access assembly service via prompt_builder
        if self.prompt_builder and hasattr(self.prompt_builder, 'assembly_service'):
            assembly_service = self.prompt_builder.assembly_service
            
            if assembly_service and hasattr(assembly_service, 'invalidate_cache'):
                try:
                    assembly_service.invalidate_cache()
                    
                    return AgentResponse.success(
                        task_id="admin_cache_reset",
                        agent_id=self.agent_id,
                        result=(
                            "✅ **Cache reset complete**\n\n"
                            "All prompt assembly caches have been cleared. "
                            "Next requests will rebuild prompts from Firestore.\n\n"
                            "_Note: This is a global operation affecting all users in this worker process._"
                        ),
                        confidence=1.0,
                        metadata={"command": "admin_cache_reset", "cache_cleared": True}
                    )
                except Exception as e:
                    logger.error(f"❌ ADMIN: Cache reset failed: {e}")
                    return AgentResponse.failure(
                        task_id="admin_cache_reset",
                        agent_id=self.agent_id,
                        error=f"Cache reset failed: {str(e)}",
                        metadata={"command": "admin_cache_reset", "error": str(e)}
                    )
        
        # Assembly service not available
        return AgentResponse.success(
            task_id="admin_cache_reset",
            agent_id=self.agent_id,
            result=(
                "⚠️ **Assembly service not available**\n\n"
                "Prompt assembly caching is not enabled in this environment."
            ),
            confidence=1.0,
            metadata={"command": "admin_cache_reset", "cache_cleared": False}
        )
    
    def _get_alternative_agents(self) -> Optional[list[str]]:
        """RouterAgent has no alternatives - it's the entry point."""
        return None


def create_router_agent(
    execution_context: Optional[AgentExecutionContext] = None,
    coordinator: "AgentCoordinator" = None,  # type: ignore
    quick_agent_id: str = "quick_response_agent",
    smart_agent_id: str = "smart_response_agent",
    user_id: Optional[str] = None,
    # ============================================================================
    # LEGACY Provider Refactor Session 20: Direct LLM parameters
    # Plan: docs/architecture/provider_refactor/POST_AUDIT_EXECUTION_PLAN.md
    # Reason: Replaced by execution_context parameter
    # Removal: After Session 26 validation + production deployment
    # ============================================================================
    # llm_service: Optional[LLMService] = None,
    session_store: Optional[SessionStore] = None,
    repository: Optional[FactRepository] = None,
    embedding_service: Optional[EmbeddingService] = None,
    search_enrichment_service: Optional[SearchEnrichmentService] = None,
    # classifier_model: str = "gemini-3-flash-preview",
    triage_prompt_path: Optional[Path] = None,
    prompt_builder: Optional[PromptBuilder] = None
) -> RouterAgent:
    """
    Factory function to create RouterAgent with default config.
    
    Args:
        execution_context: Agent execution context with provider and model
        coordinator: AgentCoordinator for routing
        quick_agent_id: ID of quick response agent
        smart_agent_id: ID of smart response agent
        user_id: Optional user id
        session_store: Session store for history
        triage_prompt_path: Path to triage prompt
        
    Returns:
        Configured RouterAgent instance
    """
    agent_id = f"router_agent_{user_id}" if user_id else "router_agent"
    config = AgentConfig(
        agent_id=agent_id,
        agent_type="router",
        llm_model=None,  # Model managed by execution_context
        max_retries=1,  # Router should be fast, no retries
        timeout_ms=None,  # Routing only, no timeout ownership
        capabilities=["classification", "routing"],
        metadata={
            "description": "Classifies and routes messages to specialized agents"
        }
    )
    
    return RouterAgent(
        config=config,
        execution_context=execution_context,
        coordinator=coordinator,
        quick_agent_id=quick_agent_id,
        smart_agent_id=smart_agent_id,
        user_id=user_id,
        session_store=session_store,
        repository=repository,
        embedding_service=embedding_service,
        search_enrichment_service=search_enrichment_service,
        triage_prompt_path=triage_prompt_path,
        prompt_builder=prompt_builder
    )
