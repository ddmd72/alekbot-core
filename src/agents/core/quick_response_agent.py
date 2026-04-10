"""
Quick Response Agent
====================

Handles complexity 1–5 requests (≈70% of traffic) with specialist delegation.
Remaps search_web → search_web_light at dispatch time.
"""

from __future__ import annotations

import json
import re
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Dict, Any, List
from ..base_agent import BaseAgent
from ...infrastructure.agent_config import QUICK, ENABLE_HISTORY_OPTIMIZATION
from ...infrastructure.agent_manifest import QUICK_RESPONSE
from ...infrastructure.delegation_engine import DelegationEngine, DelegationResult
from ...domain.agent import (
    AgentMessage,
    AgentResponse,
    AgentConfig,
    AgentIntent,
    AgentStatus,
    RoutingMetadata,
    DeliveryItem,
)
from ...domain.tone import UserTone
from ...ports.llm_port import (
    LLMPort,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    AutomaticFunctionCallingConfig,
    LLMRequest
)
from ...ports.session_store import SessionStore
from ...ports.prompt_builder_port import PromptBuilderPort
from ...ports.llm_port import AgentExecutionContext
from ...utils.logger import logger
from ...utils.llm_response_parser import parse_llm_response
from ...domain.messaging import SmartResponse

if TYPE_CHECKING:
    from ...services.history_summary_service import HistorySummaryService


class QuickResponseAgent(BaseAgent):
    """
    Handles complexity 1–5 requests. Functionally identical to SmartResponseAgent with
    two differences: no refinement loop; search_web remapped to search_web_light.
    """

    _descriptor = QUICK_RESPONSE

    CONTEXT_WINDOW = QUICK.context_window
    MAX_DELEGATION_TURNS = QUICK.max_delegation_turns
    MAX_AGENT_RETRIES = QUICK.max_agent_retries
    RETRY_BACKOFF_SECONDS = QUICK.retry_backoff_seconds
    DELEGATION_TEMPERATURE = QUICK.delegation_temperature
    TIMEOUT_MS = QUICK.timeout_ms
    CONFIG_MAX_RETRIES = QUICK.config_max_retries

    # Mirrors SmartResponseAgent._RESPONSE_SCHEMA — enforces JSON output on Gemini providers.
    # ClaudeAdapter and GrokAdapter silently ignore these fields.
    _RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["full_response", "response_summary", "rich_content"],
        "properties": {
            "full_response":    {"type": "string"},
            "response_summary": {"type": "string"},
            "rich_content": {
                "type": "object",
                "nullable": True,  # prompt: "type": ["object", "null"]
                "properties": {
                    "type":     {"type": "string", "enum": ["widget", "file", "table"]},
                    "fallback": {"type": "string"},
                    "data":     {"type": "object"},  # no deeper structure — Gemini nesting limit
                },
            },
            "link_list": {"type": "array"},  # flat — Gemini nesting limit; structure enforced by OUTPUT_FORMAT token
        },
    }

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        session_store: SessionStore,
        prompt_builder: PromptBuilderPort,
        repository: Optional[Any] = None,
        embedding_service: Optional[Any] = None,
        coordinator: "AgentCoordinator" = None,  # type: ignore
        model_name: Optional[str] = None,
        history_recent_full_turns: int = 2,
        history_summary_service: Optional[HistorySummaryService] = None,
        user_timezone: str = "UTC",
    ):
        """
        Initialize Quick Response Agent.

        Args:
            config: Agent configuration
            execution_context: Resolved provider/model context
            session_store: Session store for history management
            prompt_builder: Prompt builder for system prompts
            repository: FactRepository for semantic search
            embedding_service: EmbeddingService for semantic search
            coordinator: AgentCoordinator for billing/logging (optional)
            model_name: Model override; defaults to execution_context.model_name.
            history_recent_full_turns: Number of recent model turns to keep at full text.
            history_summary_service: Optional service for generating compact history summaries
            user_timezone: IANA timezone for message timestamps (e.g. "Europe/Madrid")
        """
        super().__init__(config)
        self.execution_context = execution_context
        self.llm = execution_context.provider
        self._set_execution_context(execution_context)
        self.session_store = session_store
        self.prompt_builder = prompt_builder
        self.repository = repository
        self.embedding_service = embedding_service
        self.coordinator = coordinator
        self.model_name = model_name or execution_context.model_name
        self.history_recent_full_turns = history_recent_full_turns
        self.history_summary_service = history_summary_service
        self._user_timezone = user_timezone

        # Extract user_id from config metadata
        self.user_id = config.metadata.get("user_id")

        logger.info(
            f"⚡ QuickResponseAgent initialized (model={self.model_name}, user={self.user_id[:8] if self.user_id else 'NONE'})"
        )
    
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        QuickResponseAgent handles QUERY intents routed as simple.
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if this is a simple query
        """
        if message.intent != AgentIntent.QUERY:
            return False

        text = message.payload.get("text", "")
        parts = message.context.get("current_message_parts", [])
        return bool(text) or bool(parts)
    
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Generate quick response using flash LLM model.
        
        Args:
            message: Agent message containing text query
            
        Returns:
            AgentResponse with LLM response
        """
        text = message.payload.get("text", "")
        session_id = message.context.get("session_id")
        user_id = message.context.get("user_id")
        account_id = message.context.get("account_id")
        routing_metadata = RoutingMetadata.from_dict(message.context.get("routing", {}))
        self.config.metadata["user_tone"] = routing_metadata.user_tone

        self._on_agent_start(text)

        try:
            enriched_context = message.context.get("enriched_context")

            cached_biographical = []
            if account_id and self.repository:
                try:
                    cached_biographical = await self.repository.get_biographical_context_cached(
                        owner_id=account_id,
                        limit=100
                    )
                except Exception as e:
                    logger.warning(f"⚡ [QuickResponseAgent] Failed to load biographical: {e}")

            biographical_facts = self.prompt_builder.merge_enriched_context_with_biographical(
                enriched_context=enriched_context,
                cached_biographical=cached_biographical
            )
            
            if enriched_context and enriched_context.get("facts"):
                logger.info(
                    "⚡ [QuickResponseAgent] Merged context: %s biographical + %s semantic = %s total",
                    len(cached_biographical),
                    len(enriched_context.get("facts", [])),
                    len(biographical_facts)
                )

            agent_notes = message.context.get("agent_notes") or []
            prompt_user_id = self.user_id or user_id
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="quick",
                user_id=prompt_user_id,
                account_id=account_id,
                routing_metadata=routing_metadata,
                capabilities=self.execution_context.capabilities,
                biographical_facts=biographical_facts,  # Enriched with semantic context
                kb_preamble=True,
                agent_notes=agent_notes,
            )
            
            current_message_parts = message.context.get("current_message_parts", [])
            conversation_history = await self._load_conversation_context(
                session_store=self.session_store,
                session_id=session_id,
                current_message_parts=current_message_parts,
                context_window=self.CONTEXT_WINDOW
            )
            # Inject behavioral anchor (information-gap + posture rules) into the
            # latest user message — in-memory only, never persisted to history.
            conversation_history = self._inject_user_turn_anchor(conversation_history)

            clean_history = self._clean_history_for_quick(conversation_history)
            
            logger.debug(
                f"⚡ [QuickResponseAgent] Context: {len(clean_history)} messages, "
                f"prompt size: {len(system_prompt)} chars"
            )
            

            engine = DelegationEngine(self.coordinator)
            base_request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=clean_history,
                tools=self._get_quick_tool_declarations(),
                temperature=self.DELEGATION_TEMPERATURE,
                response_schema=self._RESPONSE_SCHEMA,
            )
            delegation_result = await engine.execute(
                call_llm=self._call_llm,
                base_request=base_request,
                context=message.context,
                max_turns=self.MAX_DELEGATION_TURNS,
                intent_remap=dict(self._descriptor.intent_remap),
                calling_agent_id=self.agent_id,
                max_retries=self.MAX_AGENT_RETRIES,
                retry_backoff=self.RETRY_BACKOFF_SECONDS,
            )

            if delegation_result.failed:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="max_turns_exhausted",
                )

            # Parse response text into SmartResponse
            user_text, history_summary, rich, link_list = parse_llm_response(delegation_result.text)
            final_rich = rich if rich else delegation_result.structured_data
            smart_response = SmartResponse(text=user_text or "", structured_data=final_rich, link_list=link_list)
            total_tokens = delegation_result.total_tokens
            delivery_items = delegation_result.delivery_items

            if smart_response.text:
                smart_response.text = self._sanitize_response(smart_response.text)

            # Post-processing: fire-and-forget history summary (plain-text path).
            summary_task = None
            if not history_summary and ENABLE_HISTORY_OPTIMIZATION and smart_response.text and self.history_summary_service:
                summary_task = asyncio.create_task(
                    self.history_summary_service.summarize_model_response(smart_response.text)
                )

            self._on_agent_success(len(smart_response.text), total_tokens, output_text=delegation_result.text)

            metadata = {
                "model": self.model_name,
                "tokens": total_tokens,
                "response_length": len(smart_response.text)
            }
            if history_summary:
                metadata["response_summary"] = history_summary
            if summary_task:
                metadata["response_summary_task"] = summary_task
            if delegation_result.history_contexts:
                metadata.update(delegation_result.history_contexts)
                logger.info(
                    "💾 [QuickResponseAgent] history_contexts set: %s",
                    {k: len(v) for k, v in delegation_result.history_contexts.items()}
                )

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=smart_response,
                confidence=1.0,
                metadata=metadata,
                delivery_items=delivery_items,
            )
            
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Quick response failed: {str(e)}"
            )
    
    async def _load_conversation_context(
        self,
        session_store: SessionStore,
        session_id: str,
        current_message_parts: List[MessagePart],
        context_window: int
    ) -> List[Message]:
        """Load and prepare conversation context."""
        # Load session and extract history
        session = await session_store.load_session(session_id)
        history = session.history[-context_window:] if session and session.history else []

        # Apply tiered loading: recent turns use full_text, older turns use summary
        history = self._apply_history_tier(history, self.history_recent_full_turns)

        # Add current message
        current_msg = Message(role="user", parts=current_message_parts)
        history.append(current_msg)

        return self._inject_timestamps(history)

    async def _load_history(self, session_id: str) -> List[Message]:
        """
        Load session history from store.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of messages (truncated to context window)
        """
        if not session_id or not self.session_store:
            return []
        
        try:
            session = await self.session_store.load_session(session_id)
            history = session.history if session else []
            
            # Truncate to context window
            return history[-self.CONTEXT_WINDOW:]
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to load session history: {e}")
            return []
    
    def _clean_history_for_quick(self, history: List[Message]) -> List[Message]:
        """
        Remove tool interactions from history.

        !! CURRENTLY A NO-OP IN PRACTICE !!
        ConversationHandler saves only 2 messages per turn (user text + model text).
        Tool call/response pairs from the delegation loop are NEVER written to the session store,
        so this filter never finds anything to remove.

        DO NOT DELETE — reserved for Brainstorm Mode (future feature):
        In that mode, multi-turn reasoning traces will be stored in session for continuity
        across requests, and this filter will need to decide what to expose to the model.
        """
        return [
            msg for msg in history
            if not any(
                part.tool_call or part.tool_response
                for part in msg.parts
            )
        ]

    def _get_quick_tool_declarations(self) -> List[Dict[str, Any]]:
        """Build tool declarations from AgentRegistry (all non-internal intents)."""
        available_intents = self.coordinator.get_available_intents_for(self._descriptor) if self.coordinator else []
        return [self._build_delegate_tool_declaration(available_intents)]

    def _sanitize_response(self, text: str) -> str:
        """Remove tool_code blocks, API references, and empty lines from LLM output."""
        if not text:
            return text
        
        stripped_text = text.strip()
        
        # Reject if just "tool_code"
        if stripped_text.lower() == "tool_code":
            return ""
        
        # Remove tool_code print statements
        text = re.sub(
            r"tool_code\s*\n\s*print\([^\n]+\)", 
            "", 
            text, 
            flags=re.IGNORECASE
        )
        
        # Remove API references
        text = re.sub(
            r"\bdefault_api\.\w+\b", 
            "", 
            text, 
            flags=re.IGNORECASE
        )
        
        # Remove tool references
        text = re.sub(
            r"ask_web_search_agent", 
            "", 
            text, 
            flags=re.IGNORECASE
        )
        
        return text.strip()
    

def create_quick_response_agent(
    execution_context: AgentExecutionContext,
    session_store: SessionStore,
    prompt_builder: PromptBuilderPort,
    repository: Optional[Any] = None,
    embedding_service: Optional[Any] = None,
    coordinator: "AgentCoordinator" = None,  # type: ignore
    user_id: Optional[str] = None,
    model_name: Optional[str] = None,
    history_recent_full_turns: int = 2,
    history_summary_service: Optional[HistorySummaryService] = None,
    user_timezone: str = "UTC",
) -> QuickResponseAgent:
    """
    Factory function to create QuickResponseAgent.
    
    Args:
        execution_context: Resolved provider/model context
        session_store: Session store for history
        prompt_builder: Prompt builder for system prompts
        repository: FactRepository for semantic search
        embedding_service: EmbeddingService for semantic search
        coordinator: AgentCoordinator (optional)
        user_id: User ID for agent naming (optional)
        model_name: Model override (optional).
    """
    agent_id = f"quick_response_agent_{user_id}" if user_id else "quick_response_agent"
    
    config = AgentConfig(
        agent_id=agent_id,
        agent_type="quick_response",
        llm_model=model_name or execution_context.model_name,
        max_retries=QuickResponseAgent.CONFIG_MAX_RETRIES,
        timeout_ms=QuickResponseAgent.TIMEOUT_MS,
        capabilities=["fast_response", "simple_queries"],
        metadata={
            "description": "Fast LLM responses for simple queries",
            "user_id": user_id
        }
    )
    
    return QuickResponseAgent(
        config=config,
        execution_context=execution_context,
        session_store=session_store,
        prompt_builder=prompt_builder,
        repository=repository,
        embedding_service=embedding_service,
        coordinator=coordinator,
        model_name=model_name,
        history_recent_full_turns=history_recent_full_turns,
        history_summary_service=history_summary_service,
        user_timezone=user_timezone,
    )
