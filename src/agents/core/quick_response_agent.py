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
from ...domain.billing import calculate_cost
from ...utils.logger import logger
from ...utils.llm_response_parser import parse_llm_response
from ...domain.messaging import SmartResponse

if TYPE_CHECKING:
    from ...services.history_summary_service import HistorySummaryService


@dataclass
class _QuickLoopResult:
    smart_response: SmartResponse
    total_tokens: int
    history_summary: Optional[str] = None
    history_contexts: Optional[Dict[str, List[Any]]] = field(default=None)
    delivery_items: List[DeliveryItem] = field(default_factory=list)
    raw_text: str = ""  # Raw LLM output before parse_llm_response, for debug logging


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
            model_name: Model override; defaults to execution_context.model_name.
            history_recent_full_turns: Number of recent model turns to keep at full text.
            history_summary_service: Optional service for generating compact history summaries
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
            
            clean_history = self._clean_history_for_quick(conversation_history)
            
            logger.debug(
                f"⚡ [QuickResponseAgent] Context: {len(clean_history)} messages, "
                f"prompt size: {len(system_prompt)} chars"
            )
            

            tool_declarations = self._get_quick_tool_declarations()
            loop_result = await self._execute_quick_delegation_loop(
                session_id=session_id,
                user_id=user_id,
                system_prompt=system_prompt,
                history=clean_history,
                tool_declarations=tool_declarations,
                account_id=account_id,
            )

            smart_response = loop_result.smart_response
            history_summary = loop_result.history_summary
            total_tokens = loop_result.total_tokens
            delivery_items = loop_result.delivery_items

            if smart_response.text:
                smart_response.text = self._sanitize_response(smart_response.text)

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

            # Post-processing: fire-and-forget history summary (plain-text path).
            summary_task = None
            if not history_summary and ENABLE_HISTORY_OPTIMIZATION and smart_response.text and self.history_summary_service:
                summary_task = asyncio.create_task(
                    self.history_summary_service.summarize_model_response(smart_response.text)
                )

            self._on_agent_success(len(smart_response.text), total_tokens, output_text=loop_result.raw_text)

            metadata = {
                "model": self.model_name,
                "tokens": total_tokens,
                "response_length": len(smart_response.text)
            }
            if history_summary:
                metadata["response_summary"] = history_summary
            if summary_task:
                metadata["response_summary_task"] = summary_task
            if loop_result.history_contexts:
                metadata.update(loop_result.history_contexts)
                logger.info(
                    "💾 [QuickResponseAgent] history_contexts set: %s",
                    {k: len(v) for k, v in loop_result.history_contexts.items()}
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
        available_intents = []
        if self.coordinator:
            available_intents = self.coordinator.get_available_intents_for(self._descriptor)

        intents_description = "\n".join(
            f"- {i['name']}: {i['description']}" for i in available_intents
        ) or "(no specialist agents registered)"

        return [
            {
                "name": "delegate_to_specialist",
                "description": (
                    "Send a task to a specialist agent in the network. "
                    "The specialist executes autonomously and returns results.\n\n"
                    f"Available intents:\n{intents_description}\n\n"
                    "See agents_registry in your system prompt for per-intent "
                    "query formulation rules and required context fields."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Target agent intent (from available intents list)"
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "Task for the specialist. "
                                "Formulate per agents_registry rules for the chosen intent."
                            )
                        },
                        "context": {
                            "type": "object",
                            "description": (
                                "Structured parameters for intents that require them "
                                "(e.g. email_id, filename). "
                                "See agents_registry for required fields per intent."
                            )
                        },
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
        accumulated_history: Dict[str, List[Any]] = {}
        all_delivery_items: List[DeliveryItem] = []

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
                temperature=self.DELEGATION_TEMPERATURE,
                # response_mime_type omitted: Gemini rejects JSON mime type + function calling
                # simultaneously. Output format enforced via response_schema + OUTPUT_FORMAT token.
                response_schema=self._RESPONSE_SCHEMA,
            )
            response = await self._call_llm(request, turn=turn + 1)

            if response.usage_metadata:
                total_tokens += response.usage_metadata.total_tokens

            if not response.tool_calls:
                # Final answer — parse and return
                logger.info(
                    "⚡ [QuickResponseAgent] Turn %s - LLM raw:\n%s",
                    turn + 1,
                    response.text or ""
                )
                user_text, summary, rich, link_list = parse_llm_response(response.text or "")
                history.append(Message(
                    role="model",
                    parts=[MessagePart(text=(summary or user_text or ""))]
                ))
                return _QuickLoopResult(
                    smart_response=SmartResponse(text=user_text or "", structured_data=rich, link_list=link_list),
                    total_tokens=total_tokens,
                    history_summary=summary,
                    history_contexts=accumulated_history or None,
                    delivery_items=all_delivery_items,
                    raw_text=response.text or "",
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

            for tr in tool_responses:
                if tr.history_context:
                    for key, value in tr.history_context.items():
                        accumulated_history.setdefault(key, []).append(value)
                all_delivery_items.extend(tr.delivery_items)

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
            history_contexts=accumulated_history or None,
            delivery_items=all_delivery_items,
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
            history_context: Optional[Dict[str, Any]] = None
            delivery_items: List[DeliveryItem] = field(default_factory=list)

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
            result_str, history_ctx, sub_delivery = await self._delegate_quick(
                tc, user_id, session_id, account_id
            )
            results[idx] = ToolResponse(name=tc.name, result_str=result_str, history_context=history_ctx, delivery_items=sub_delivery)
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
                    result_str, history_ctx, sub_delivery = result
                    results[idx] = ToolResponse(name=tc.name, result_str=result_str, history_context=history_ctx, delivery_items=sub_delivery)

        return [r for r in results if r is not None]

    async def _delegate_quick(
        self,
        tool_call: ToolCall,
        user_id: str,
        session_id: str,
        account_id: Optional[str] = None,
        memory_context: Optional[List[str]] = None,
    ) -> tuple:
        """Delegate a single tool call via coordinator, with retry.

        Returns (result_str, history_context, delivery_items).
        history_context is taken directly from the specialist AgentResponse.
        delivery_items carries typed artifacts (e.g. grounding attribution widget).
        """
        if not self.coordinator:
            return "SYSTEM ERROR: AgentCoordinator not configured.", None, []

        args = tool_call.args or {}
        intent = args.get("intent", "")
        query = args.get("query", "")
        context_params = args.get("context", {})

        if not intent:
            return f"SYSTEM ERROR: delegate_to_specialist called without 'intent'. args={args}", None, []

        logger.debug(
            "[QuickResponseAgent] _delegate_quick: intent before remap=%r, remap_dict=%r",
            intent, self._descriptor.intent_remap,
        )
        intent = self._descriptor.intent_remap.get(intent, intent)
        logger.debug("[QuickResponseAgent] _delegate_quick: intent after remap=%r", intent)

        delegation_context: Dict[str, Any] = {
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "memory_context": memory_context or [],
            "params": context_params,
        }

        self._on_delegation(intent, query)

        for attempt in range(self.MAX_AGENT_RETRIES + 1):
            response = await self.coordinator.handle_delegation(
                intent=intent,
                query=query,
                context=delegation_context,
                calling_agent_id=self.agent_id,
            )
            if response.status == AgentStatus.SUCCESS:
                result = response.result
                if intent == "search_emails":
                    result_str = self._format_email_search_compact(result)
                else:
                    result_str = (
                        result.text if isinstance(result, SmartResponse)
                        else "\n".join(str(i) for i in result) if isinstance(result, list)
                        else str(result)
                    )
                logger.info(
                    "✅ [QuickResponseAgent] delegation result: intent=%s, %s chars",
                    intent, len(result_str)
                )
                return result_str, response.history_context, response.delivery_items

            if attempt < self.MAX_AGENT_RETRIES:
                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
                continue

            return f"AGENT ERROR: {response.error}", None, []

        return "AGENT ERROR: Max retries exceeded", None, []

    @staticmethod
    def _format_email_search_compact(result: Any) -> str:
        """Compact text representation of search_emails results for LLM tool_response.

        EmailSearchService.vector_search returns a JSON string:
          {"count": N, "emails": [{"email_id":..., "from":..., "date":..., "text":..., "attachments":[...]}]}
        or a plain "No emails found..." string.
        """
        if not isinstance(result, str):
            return str(result)
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return result
        emails = parsed.get("emails") if isinstance(parsed, dict) else None
        if not emails:
            return result
        lines = [f"Found {len(emails)} email(s):"]
        for e in emails:
            line = f"• [{e.get('email_id', '?')}] {e.get('from', '?')} | {e.get('date', '?')}"
            atts = e.get("attachments") or []
            if atts:
                line += f" | 📎 {', '.join(atts)}"
            lines.append(line)
            text = e.get("text") or ""
            if text:
                lines.append(f"  → {text[:150]}")
        return "\n".join(lines)

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
    history_recent_full_turns: int = 2,
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
        history_summary_service=history_summary_service
    )
