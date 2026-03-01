"""
Quick Response Agent
====================

Handles simple requests with fast LLM response using flash model.
No tool/agent delegation - just direct LLM response.

Ported from BrainService.generate_quick_response()
"""

import os
import re
import asyncio
from dataclasses import dataclass, field
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
from ...services.history_summary_service import HistorySummaryService


@dataclass
class _QuickLoopResult:
    smart_response: SmartResponse
    total_tokens: int
    history_summary: Optional[str] = None


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

    # Delegation loop limits
    MAX_DELEGATION_TURNS = 2
    MAX_AGENT_RETRIES = 1
    RETRY_BACKOFF_SECONDS = 0.5

    # Intents exposed to Quick (subset of all registered intents)
    QUICK_INTENTS = {"search_memory", "search_web", "search_emails", "get_email_details", "get_email_attachment"}

    # Remap intents before passing to coordinator — Quick uses lightweight implementations
    _INTENT_REMAP = {"search_web": "search_web_light"}
    
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
        history_summary_service: Optional[HistorySummaryService] = None
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
            history_summary_service: Optional service for generating compact history summaries
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
        self.history_summary_service = history_summary_service
        
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
            history_str = "\n".join([
                f"{msg.role}: {' | '.join([p.text[:100] if p.text else f'[{type(p).__name__}]' for p in msg.parts])}"
                for msg in clean_history[-5:]
            ])
            debug_logger.log_prompt(
                agent_name="quick_response",
                prompt=history_str,
                system_instruction=system_prompt,
                metadata={"model": self.model_name, "user_id": user_id[:8] if user_id else "unknown"}
            )

            # 4. Run delegation loop (tool calling replaces raw grounding)
            tool_declarations = self._get_quick_tool_declarations()
            loop_result = await self._execute_quick_delegation_loop(
                session_id=session_id,
                user_id=user_id,
                system_prompt=system_prompt,
                history=clean_history,
                tool_declarations=tool_declarations,
                account_id=account_id,
            )

            user_text = loop_result.smart_response.text
            rich_content = loop_result.smart_response.structured_data
            history_summary = loop_result.history_summary
            total_tokens = loop_result.total_tokens

            # Sanitize user text if present
            if user_text:
                user_text = self._sanitize_response(user_text)

            # 7. Handle empty/invalid response
            if not user_text and not rich_content:
                user_text = ""  # Let ConversationHandler handle fallback

            # 8. Track usage via a synthetic response object (total_tokens from loop)
            class _FakeUsage:
                def __init__(self, tokens):
                    self.total_tokens = tokens
                    self.prompt_tokens = 0
                    self.completion_tokens = 0

            class _FakeResponse:
                def __init__(self, tokens):
                    self.usage_metadata = _FakeUsage(tokens)
                    self.metadata = {}

            await self._track_usage(user_id, _FakeResponse(total_tokens))

            # 9. Post-processing: fire-and-forget history summary (plain-text path).
            enable_history_optimization = os.getenv("ENABLE_HISTORY_OPTIMIZATION", "false").lower() in ("true", "1", "yes")
            summary_task = None
            if not history_summary and enable_history_optimization and user_text and self.history_summary_service:
                summary_task = asyncio.create_task(
                    self.history_summary_service.summarize_model_response(user_text)
                )

            # Build SmartResponse (Unified Protocol)
            smart_response = SmartResponse(
                text=user_text or "",
                structured_data=rich_content
            )

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
                metadata["response_summary"] = history_summary
            if summary_task:
                metadata["response_summary_task"] = summary_task

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

    def _get_quick_tool_declarations(self) -> List[Dict[str, Any]]:
        """Build tool declarations restricted to Quick-allowed intents."""
        available_intents = []
        if self.coordinator:
            for intent in self.coordinator.get_available_intents():
                if intent["name"] in self.QUICK_INTENTS:
                    available_intents.append(intent)

        intents_description = "\n".join(
            f"- {i['name']}: {i['description']}" for i in available_intents
        ) or "(no specialist agents registered)"

        return [
            {
                "name": "delegate_to_specialist",
                "description": (
                    "Delegate a task to a specialist agent.\n\n"
                    f"Available intents:\n{intents_description}\n\n"
                    "Parameters:\n"
                    "- intent: intent name from the list above\n"
                    "- query: the user's question or command\n"
                    "- context: optional dict with extra parameters for the specialist"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Intent name (from available intents list)"
                        },
                        "query": {
                            "type": "string",
                            "description": "User question or command"
                        },
                        "context": {
                            "type": "object",
                            "description": "Optional extra parameters for the specialist agent"
                        }
                    },
                    "required": ["intent", "query"]
                }
            },
        ]

    async def _execute_quick_delegation_loop(
        self,
        session_id: str,
        user_id: str,
        system_prompt: str,
        history: List[Message],
        tool_declarations: List[Dict[str, Any]],
        account_id: Optional[str] = None,
    ) -> _QuickLoopResult:
        """Agent delegation loop for Quick: max 2 turns, memory-first ordering."""
        total_tokens = 0

        for turn in range(self.MAX_DELEGATION_TURNS):
            debug_history = history[-self.CONTEXT_WINDOW:]

            logger.info(
                "⚡ [QuickResponseAgent] Turn %s/%s - LLM call (history=%s)",
                turn + 1, self.MAX_DELEGATION_TURNS, len(debug_history)
            )

            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=debug_history,
                tools=tool_declarations,
                temperature=0.7,
            )
            response = await self.llm.generate_content(request=request)

            if response.usage_metadata:
                total_tokens += response.usage_metadata.total_tokens

            if not response.tool_calls:
                # Final answer — parse and return
                user_text, summary, rich = parse_llm_response(response.text or "")
                debug_logger = get_debug_logger()
                debug_logger.log_response(
                    agent_name="quick_response",
                    response=response.text or "",
                    metadata={
                        "model": self.model_name,
                        "user_id": user_id[:8] if user_id else "unknown",
                        "turn": turn + 1,
                        "tokens": response.usage_metadata.total_tokens if response.usage_metadata else 0
                    }
                )
                history.append(Message(
                    role="model",
                    parts=[MessagePart(text=(summary or user_text or ""))]
                ))
                return _QuickLoopResult(
                    smart_response=SmartResponse(text=user_text or "", structured_data=rich),
                    total_tokens=total_tokens,
                    history_summary=summary,
                )

            # Append model tool calls to history
            if response.raw_content:
                history.append(Message(role="model", parts=[], raw_content=response.raw_content))
            else:
                history.append(Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls]
                ))

            logger.info(
                "⚡ [QuickResponseAgent] Turn %s - delegating to %s agents",
                turn + 1, len(response.tool_calls)
            )

            tool_responses = await self._execute_quick_parallel(
                tool_calls=response.tool_calls,
                user_id=user_id,
                session_id=session_id,
                account_id=account_id,
            )

            history.append(Message(
                role="user",
                parts=[
                    MessagePart(tool_response={
                        "name": tr.name,
                        "response": {"result": tr.result_str}
                    })
                    for tr in tool_responses
                ]
            ))

        # Max turns exhausted — return empty response
        return _QuickLoopResult(
            smart_response=SmartResponse(text=""),
            total_tokens=total_tokens,
        )

    async def _execute_quick_parallel(
        self,
        tool_calls: List[ToolCall],
        user_id: str,
        session_id: str,
        account_id: Optional[str] = None,
    ) -> List[Any]:
        """Execute tool calls: memory-first, others in parallel."""
        from dataclasses import dataclass as _dc

        @_dc
        class ToolResponse:
            name: str
            result_str: str

        results: List[Optional[ToolResponse]] = [None] * len(tool_calls)
        memory_context: List[str] = []

        def _is_memory(tc: ToolCall) -> bool:
            return (
                tc.name == "delegate_to_specialist"
                and (tc.args or {}).get("intent") == "search_memory"
            )

        memory_calls = [(i, tc) for i, tc in enumerate(tool_calls) if _is_memory(tc)]
        other_calls = [(i, tc) for i, tc in enumerate(tool_calls) if not _is_memory(tc)]

        for idx, tc in memory_calls:
            result_str = await self._delegate_quick(
                tc, user_id, session_id, account_id
            )
            results[idx] = ToolResponse(name=tc.name, result_str=result_str)
            if result_str:
                memory_context.append(result_str)

        if other_calls:
            tasks = [
                self._delegate_quick(tc, user_id, session_id, account_id, memory_context)
                for _, tc in other_calls
            ]
            parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (idx, tc), result in zip(other_calls, parallel_results):
                if isinstance(result, Exception):
                    results[idx] = ToolResponse(name=tc.name, result_str=f"AGENT ERROR: {result}")
                else:
                    results[idx] = ToolResponse(name=tc.name, result_str=result)

        return [r for r in results if r is not None]

    async def _delegate_quick(
        self,
        tool_call: ToolCall,
        user_id: str,
        session_id: str,
        account_id: Optional[str] = None,
        memory_context: Optional[List[str]] = None,
    ) -> str:
        """Delegate a single tool call via coordinator, with retry."""
        if not self.coordinator:
            return "SYSTEM ERROR: AgentCoordinator not configured."

        args = tool_call.args or {}
        intent = args.get("intent", "")
        query = args.get("query", "")
        context_params = args.get("context", {})

        if not intent:
            return f"SYSTEM ERROR: delegate_to_specialist called without 'intent'. args={args}"

        intent = self._INTENT_REMAP.get(intent, intent)

        delegation_context: Dict[str, Any] = {
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "memory_context": memory_context or [],
            "params": context_params,
        }

        logger.info("⚡ [QuickResponseAgent] delegate: intent=%s query='%s'", intent, query[:60])

        for attempt in range(self.MAX_AGENT_RETRIES + 1):
            response = await self.coordinator.handle_delegation(
                intent=intent,
                query=query,
                context=delegation_context,
                calling_agent_id=self.agent_id,
            )
            if response.status == AgentStatus.SUCCESS:
                result = response.result
                result_str = (
                    result.text if isinstance(result, SmartResponse)
                    else "\n".join(str(i) for i in result) if isinstance(result, list)
                    else str(result)
                )
                logger.info(
                    "✅ [QuickResponseAgent] delegation result: intent=%s, %s chars",
                    intent, len(result_str)
                )
                return result_str

            if attempt < self.MAX_AGENT_RETRIES:
                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
                continue

            return f"AGENT ERROR: {response.error}"

        return "AGENT ERROR: Max retries exceeded"

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
    model_name: Optional[str] = None,
    history_summary_service: Optional[HistorySummaryService] = None
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
        model_name=model_name,
        history_summary_service=history_summary_service
    )
