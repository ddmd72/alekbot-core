"""
Quick Response Agent
====================

Handles simple requests with fast LLM response using flash model.
No tool/agent delegation - just direct LLM response.

Ported from BrainService.generate_quick_response()
"""

import re
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from ..base_agent import BaseAgent
from ...domain.agent import (
    AgentMessage,
    AgentResponse,
    AgentConfig,
    AgentIntent,
    AgentStatus,
    RoutingMetadata
)
from ...domain.tone import UserTone
from ...ports.llm_service import (
    LLMService, 
    LLMResponse, 
    ToolCall, 
    Message, 
    MessagePart,
    AutomaticFunctionCallingConfig,
    LLMRequest
)
from ...ports.session_store import SessionStore
from ...ports.prompt_builder_port import PromptBuilderPort
from ...ports.llm_service import AgentExecutionContext
from ...domain.billing import calculate_cost
from ...utils.logger import logger
from ...utils.debug_logger import get_debug_logger
from ...utils.llm_response_parser import parse_llm_response
from ...domain.messaging import SmartResponse


class QuickResponseAgent(BaseAgent):
    """
    Quick Response Agent for simple queries.
    
    Characteristics:
    - Uses lightweight LLM model (gemini-flash)
    - Small context window (20 messages)
    - Single-turn tool delegation (memory/web)
    - Fast response time (<2s target)
    
    Use Cases:
    - Greetings: "Hello", "Hi"
    - Acknowledgments: "Ok", "Thanks"
    - Simple questions with immediate answers
    
    NOT for:
    - Deep personal data analysis
    - Complex multi-step reasoning
    """
    
    # LEGACY Provider Refactor Session 11: Default model handled by AgentExecutionContext
    # DEFAULT_MODEL = "gemini-3-flash-preview"
    
    # Context window for quick responses (smaller = faster)
    CONTEXT_WINDOW = 20
    
    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        session_store: SessionStore,
        prompt_builder: PromptBuilderPort,
        repository: Optional[Any] = None,
        embedding_service: Optional[Any] = None,
        coordinator: "AgentCoordinator" = None,  # type: ignore
        model_name: Optional[str] = None
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
            model_name: LEGACY override (kept for backwards compatibility)
        """
        super().__init__(config)
        self.execution_context = execution_context
        self.llm = execution_context.provider
        self.session_store = session_store
        self.prompt_builder = prompt_builder
        self.repository = repository
        self.embedding_service = embedding_service
        self.coordinator = coordinator
        self.model_name = model_name or execution_context.model_name
        
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
        return bool(text)
    
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
        account_id = message.context.get("account_id")  # SESSION_26
        routing_metadata = RoutingMetadata.from_dict(message.context.get("routing", {}))
        self.config.metadata["user_tone"] = routing_metadata.user_tone

        logger.info(
            f"⚡ [QuickResponseAgent] Processing: '{text[:50]}...'"
            if len(text) > 50 else f"⚡ [QuickResponseAgent] Processing: '{text}'"
        )

        try:
            # 0. Load biographical facts first, then merge with Router enrichment
            enriched_context = message.context.get("enriched_context")
            
            # Load cached biographical facts (MUST load before merge!)
            cached_biographical = []
            if account_id and self.repository:
                try:
                    logger.info(f"🔍 [TRACE] QuickAgent: calling get_biographical_context_cached() - auto-resolve from RequestContext")
                    # SESSION_27: Auto-resolve owner_id from RequestContext
                    cached_biographical = await self.repository.get_biographical_context_cached(
                        limit=100  # owner_id auto-resolved from RequestContext
                    )
                    logger.info(f"🔍 [TRACE] QuickAgent: got {len(cached_biographical)} biographical facts")
                except Exception as e:
                    logger.warning(f"⚡ [QuickResponseAgent] Failed to load biographical: {e}")
            
            # Now merge with Router semantic enrichment
            biographical_facts = self.prompt_builder.merge_enriched_context_with_biographical(
                enriched_context=enriched_context,
                cached_biographical=cached_biographical  # Pass loaded facts!
            )
            
            if enriched_context and enriched_context.get("facts"):
                logger.info(
                    "⚡ [QuickResponseAgent] Merged context: %s biographical + %s semantic = %s total",
                    len(cached_biographical),
                    len(enriched_context.get("facts", [])),
                    len(biographical_facts)
                )

            # 1. Build system prompt using unified PromptBuilder
            prompt_user_id = self.user_id or user_id
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="quick",
                user_id=prompt_user_id,
                account_id=account_id,  # SESSION_26
                routing_metadata=routing_metadata,
                capabilities=self.execution_context.capabilities,
                biographical_facts=biographical_facts  # Enriched with semantic context
            )
            
            # 2. Load conversation context (previous history + current message)
            current_message_parts = message.context.get("current_message_parts", [])
            conversation_history = await self._load_conversation_context(
                session_store=self.session_store,
                session_id=session_id,
                current_message_parts=current_message_parts,
                context_window=self.CONTEXT_WINDOW
            )
            
            # 3. Filter out tool interactions from history
            clean_history = self._clean_history_for_quick(conversation_history)
            
            logger.debug(
                f"⚡ [QuickResponseAgent] Context: {len(clean_history)} messages, "
                f"prompt size: {len(system_prompt)} chars"
            )
            
            # DEBUG: Log prompt before LLM call
            debug_logger = get_debug_logger()
            # Convert history to readable format for logging
            history_str = "\n".join([
                f"{msg.role}: {' | '.join([p.text[:100] if p.text else f'[{type(p).__name__}]' for p in msg.parts])}"
                for msg in clean_history[-5:]  # Last 5 messages
            ])
            debug_logger.log_prompt(
                agent_name="quick_response",
                prompt=history_str,
                system_instruction=system_prompt,
                metadata={"user_id": user_id[:8] if user_id else "unknown"}
            )
            
            # 4. Generate LLM response (native tools where supported)
            afc = (
                AutomaticFunctionCallingConfig(enabled=True)
                if self.execution_context.capabilities.native_tools
                else None
            )
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=clean_history,
                tools=None,
                temperature=0.7,
                automatic_function_calling=afc
            )
            response = await self.llm.generate_content(request=request)
            
            # DEBUG: Log response
            debug_logger.log_response(
                agent_name="quick_response",
                response=response.text or "",
                metadata={
                    "user_id": user_id[:8] if user_id else "unknown",
                    "tokens": response.usage_metadata.total_tokens if response.usage_metadata else 0
                }
            )

            # 6. Parse response (Unified Protocol)
            user_text, history_summary, rich_content = parse_llm_response(response.text or "")
            
            # Sanitize user text if present
            if user_text:
                user_text = self._sanitize_response(user_text)

            # 7. Handle empty/invalid response
            if not user_text and not rich_content:
                user_text = ""  # Let ConversationHandler handle fallback

            # 8. Track usage (fire-and-forget)
            await self._track_usage(user_id, response)
            
            # Build SmartResponse (Unified Protocol)
            smart_response = SmartResponse(
                text=user_text or "",
                structured_data=rich_content
            )
            
            total_tokens = response.usage_metadata.total_tokens if response.usage_metadata else 0

            logger.info(
                f"✅ [QuickResponseAgent] Response generated "
                f"({len(user_text or '')} chars, {total_tokens} tokens)"
            )
            
            # Prepare metadata with history summary
            metadata = {
                "model": self.model_name,
                "tokens": total_tokens,
                "response_length": len(user_text or "")
            }
            if history_summary:
                metadata["history_summary"] = history_summary

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=smart_response,
                confidence=1.0,
                metadata=metadata
            )
            
        except Exception as e:
            logger.error(f"❌ [QuickResponseAgent] Error: {e}", exc_info=True)
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
        history = self._apply_history_tier(history)

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
        
        Quick responses should not see tool_call/tool_response
        to avoid confusing the model.
        
        Args:
            history: Original message history
            
        Returns:
            Cleaned history without tool interactions
        """
        return [
            msg for msg in history
            if not any(
                part.tool_call or part.tool_response 
                for part in msg.parts
            )
        ]


    
    def _sanitize_response(self, text: str) -> str:
        """
        Sanitize LLM response text.
        
        Removes:
        - tool_code blocks
        - API references
        - Empty lines at start/end
        
        Ported from BrainService._sanitize_quick_text()
        
        Args:
            text: Raw LLM response
            
        Returns:
            Cleaned response text
        """
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
        
        # Clean up empty lines
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines).strip()
    
    async def _track_usage(self, user_id: str, response) -> None:
        """
        Track usage for billing (fire-and-forget).

        Sends usage data to BillingAgent via AgentCoordinator.

        Args:
            user_id: User identifier
            response: LLM response with usage metadata
        """
        if not response.usage_metadata:
            return

        if not self.coordinator:
            logger.debug(
                f"📊 Usage: {response.usage_metadata.total_tokens} tokens "
                f"(user={user_id})"
            )
            return

        asyncio.create_task(
            self.coordinator.route_message(
                AgentMessage.create(
                    sender=self.agent_id,
                    recipient="billing_agent",
                    intent=AgentIntent.INFORM,
                    payload={
                        "user_id": user_id,
                        "tokens": response.usage_metadata.total_tokens,
                        "cost": calculate_cost(
                            model=self.model_name,
                            prompt_tokens=response.usage_metadata.prompt_tokens,
                            completion_tokens=response.usage_metadata.completion_tokens
                        ),
                        "model": self.model_name
                    },
                    context={
                        "session_id": response.metadata.get("session_id") if hasattr(response, "metadata") else None
                    }
                )
            )
        )
    

def create_quick_response_agent(
    execution_context: AgentExecutionContext,
    session_store: SessionStore,
    prompt_builder: PromptBuilderPort,
    repository: Optional[Any] = None,
    embedding_service: Optional[Any] = None,
    coordinator: "AgentCoordinator" = None,  # type: ignore
    user_id: Optional[str] = None,
    model_name: Optional[str] = None
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
        model_name: LEGACY override (optional)
        
    Returns:
        Configured QuickResponseAgent instance
    """
    agent_id = f"quick_response_agent_{user_id}" if user_id else "quick_response_agent"
    
    config = AgentConfig(
        agent_id=agent_id,
        agent_type="quick_response",
        llm_model=model_name or execution_context.model_name,
        max_retries=1,  # Quick responses should be fast
        timeout_ms=60000,  # Worker agent timeout for fast LLM
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
        model_name=model_name
    )
