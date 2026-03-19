"""
Smart Response Agent
====================

Handles complexity 6–10 requests with multi-turn specialist delegation.
Orchestrates specialists via AgentCoordinator — memory-first, others in parallel.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Iterable

from ..base_agent import BaseAgent
from ...infrastructure.agent_config import SMART, ENABLE_HISTORY_OPTIMIZATION
from ...infrastructure.agent_manifest import SMART_RESPONSE
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
from ...domain.messaging import SmartResponse, RichContent
from ...ports.llm_port import (
    LLMPort,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    LLMRequest
)
from ...ports.session_store import SessionStore
from ...ports.prompt_builder_port import PromptBuilderPort
from ...ports.llm_port import AgentExecutionContext
from ...utils.logger import logger
from ...utils.llm_response_parser import parse_llm_response

if TYPE_CHECKING:
    from ...services.history_summary_service import HistorySummaryService


@dataclass
class ToolResponse:
    """Internal container for delegated agent results."""
    name: str
    result_str: str
    structured_data: Optional[RichContent] = None
    history_context: Optional[Dict[str, Any]] = None
    delivery_items: List[DeliveryItem] = field(default_factory=list)


@dataclass
class AgentLoopResult:
    """Result from the agent delegation loop."""
    smart_response: SmartResponse
    total_tokens: int
    history_summary: Optional[str] = None
    history_contexts: Optional[Dict[str, List[Any]]] = field(default=None)
    delivery_items: List[DeliveryItem] = field(default_factory=list)


class SmartResponseAgent(BaseAgent):
    """
    Handles complexity 6–10 requests (≈30% of traffic).

    Execution ordering:
    1) search_memory first (sequential) to build context
    2) other specialist calls in parallel (asyncio.gather)
    """

    _descriptor = SMART_RESPONSE

    CONTEXT_WINDOW = SMART.context_window
    MAX_DELEGATION_TURNS = SMART.max_delegation_turns
    MAX_AGENT_RETRIES = SMART.max_agent_retries
    RETRY_BACKOFF_SECONDS = SMART.retry_backoff_seconds
    DELEGATION_TEMPERATURE = SMART.delegation_temperature

    # Structured output schema — Gemini Pro experiment: enforce JSON format even with tools active.
    # response_json_schema (dict) is used by GeminiAdapter (SDK 1.64+); ClaudeAdapter ignores it.
    # If this causes 400 on turns with tools → remove and fall back to OUTPUT_FORMAT token only.
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
    TIMEOUT_MS = SMART.timeout_ms
    CONFIG_MAX_RETRIES = SMART.config_max_retries

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
        history_recent_full_turns: int = 5,
        history_summary_service: Optional[HistorySummaryService] = None
    ):
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
            f"🧠 SmartResponseAgent initialized (model={self.model_name}, user={self.user_id[:8] if self.user_id else 'NONE'})"
        )

    async def can_handle(self, message: AgentMessage) -> bool:
        """
        SmartResponseAgent handles complex QUERY intents.

        Returns True when:
        - intent is QUERY
        - classification says is_simple=False OR
        - classification missing (fallback to smart)
        """
        if message.intent != AgentIntent.QUERY:
            return False

        text = message.payload.get("text", "")
        has_attachments = bool(message.payload.get("attachments"))
        parts = message.context.get("current_message_parts", [])
        return bool(text) or has_attachments or bool(parts)

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute complex response generation with agent delegation.
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
                    logger.warning(f"🧠 [SmartResponseAgent] Failed to load biographical: {e}")

            biographical_facts = self.prompt_builder.merge_enriched_context_with_biographical(
                enriched_context=enriched_context,
                cached_biographical=cached_biographical
            )
            
            if enriched_context and enriched_context.get("facts"):
                logger.info(
                    "🧠 [SmartResponseAgent] Merged context: %s biographical + %s semantic = %s total",
                    len(cached_biographical),
                    len(enriched_context.get("facts", [])),
                    len(biographical_facts)
                )

            agent_notes = message.context.get("agent_notes") or []
            prompt_user_id = self.user_id or user_id
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="smart",
                user_id=prompt_user_id,
                account_id=account_id,
                routing_metadata=routing_metadata,
                capabilities=self.execution_context.capabilities,
                biographical_facts=biographical_facts,
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

            clean_history = self._sanitize_tool_history(conversation_history)
            loop_result = await self._execute_agent_delegation_loop(
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,
                system_prompt=system_prompt,
                history=clean_history,
                tool_declarations=self._get_tool_declarations()
            )

            smart_response = loop_result.smart_response
            total_tokens = loop_result.total_tokens
            history_summary = loop_result.history_summary
            delivery_items = loop_result.delivery_items

            # Post-processing: fire-and-forget history summary generation.
            # Launched as background task — does NOT block user response delivery.
            # conversation_handler awaits the task after sending to Slack.
            summary_task = None
            if not history_summary and ENABLE_HISTORY_OPTIMIZATION and smart_response.text:
                summary_task = asyncio.create_task(
                    self._generate_history_summary(smart_response.text)
                )

            self._on_agent_success(len(smart_response.text), total_tokens, output_text=smart_response.text)

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
                    "💾 [SmartResponseAgent] history_contexts set: %s",
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
                error=f"Smart response failed: {str(e)}"
            )

    async def _load_history(self, session_id: str) -> List[Message]:
        """Load session history and return list of messages."""
        if not session_id or not self.session_store:
            return []

        try:
            session = await self.session_store.load_session(session_id)
            history = session.history if session else []
            return history[-self.CONTEXT_WINDOW:]
        except Exception as e:
            logger.warning(f"⚠️ Failed to load session history: {e}")
            return []

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
        raw_history = session.history[-context_window:] if session and session.history else []

        # Apply tiered loading: recent turns use full_text, older turns use summary
        history = self._apply_history_tier(raw_history, self.history_recent_full_turns)

        # Add current message (adapter will handle file upload in _convert_messages)
        current_msg = Message(role="user", parts=current_message_parts)
        history.append(current_msg)

        return self._inject_timestamps(history)

    async def _execute_agent_delegation_loop(
        self,
        session_id: str,
        user_id: str,
        system_prompt: str,
        history: List[Message],
        tool_declarations: List[Dict[str, Any]],
        account_id: Optional[str] = None,
    ) -> AgentLoopResult:
        """
        Agent-based delegation loop with smart ordering:
        1) search_memory first (sequential)
        2) other agent calls in parallel
        """
        structured_data: Optional[RichContent] = None
        total_tokens = 0
        accumulated_history: Dict[str, List[Any]] = {}
        all_delivery_items: List[DeliveryItem] = []

        for turn in range(self.MAX_DELEGATION_TURNS):
            debug_history = history[-self.CONTEXT_WINDOW:]
            self._validate_history(debug_history)

            logger.info(
                "🧠 [SmartResponseAgent] Turn %s/%s - LLM call (history=%s): %s",
                turn + 1,
                self.MAX_DELEGATION_TURNS,
                len(debug_history),
                self._summarize_history(debug_history)
            )

            llm_start = time.time()
            try:
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
                response: LLMResponse = await self._call_llm(request, turn=turn + 1)
            except Exception as e:
                logger.error(
                    "❌ [SmartResponseAgent] LLM call failed: %s (history=%s)",
                    e,
                    self._summarize_history(debug_history),
                    exc_info=True
                )
                raise
            finally:
                logger.debug(
                    "🧠 [SmartResponseAgent] LLM call completed in %.2fs",
                    time.time() - llm_start
                )

            if response.usage_metadata:
                total_tokens += response.usage_metadata.total_tokens

            raw_parts_count = None
            if response.raw_content is not None:
                raw_parts = getattr(response.raw_content, "parts", None)
                if raw_parts is not None:
                    raw_parts_count = len(raw_parts)

            logger.info(
                "🔍 [SmartResponseAgent] LLM response summary: text_len=%s tool_calls=%s raw_content=%s raw_parts=%s",
                len(response.text or ""),
                len(response.tool_calls or []),
                response.raw_content is not None,
                raw_parts_count
            )

            # Check for deliver_response (terminal tool — extract and return immediately)
            deliver_call = next(
                (tc for tc in (response.tool_calls or []) if tc.name == "deliver_response"),
                None
            )
            if deliver_call:
                logger.info(
                    "🧠 [SmartResponseAgent] Turn %s - deliver_response received, finalizing",
                    turn + 1
                )
                args = deliver_call.args
                user_text = args.get("full_response", "")
                summary = args.get("history_summary")
                rich_data = args.get("rich_content")
                rich = (
                    RichContent(
                        content_type=rich_data.get("type", "unknown"),
                        data=rich_data.get("data", {}),
                        fallback_text=rich_data.get("fallback", "")
                    )
                    if isinstance(rich_data, dict) else None
                )
                tool_links = [
                    item for item in (args.get("link_list") or [])
                    if isinstance(item, dict) and "anchor" in item and "title" in item and "url" in item
                ]

                final_rich = rich if rich else structured_data
                smart_response = SmartResponse(text=user_text, structured_data=final_rich, link_list=tool_links)

                history_text = summary if summary else user_text
                history.append(Message(role="model", parts=[MessagePart(text=history_text)]))

                return AgentLoopResult(
                    smart_response=smart_response,
                    total_tokens=total_tokens,
                    history_summary=summary,
                    delivery_items=all_delivery_items,
                )

            if not response.tool_calls:
                logger.info(
                    "🧠 [SmartResponseAgent] Turn %s - No tool calls, LLM raw:\n%s",
                    turn + 1,
                    response.text or ""
                )

                # Parse final response (fallback: LLM skipped deliver_response tool)
                user_text, summary, rich, link_list = parse_llm_response(response.text or "")

                # Prefer parsed rich content, fallback to accumulated
                final_rich = rich if rich else structured_data

                smart_response = SmartResponse(
                    text=user_text or "",
                    structured_data=final_rich,
                    link_list=link_list,
                )

                # Add to history (use summary if available for optimization)
                history_text = summary if summary else (user_text or "")
                history.append(Message(role="model", parts=[MessagePart(text=history_text)]))

                return AgentLoopResult(
                    smart_response=smart_response,
                    total_tokens=total_tokens,
                    history_summary=summary,
                    history_contexts=accumulated_history or None,
                    delivery_items=all_delivery_items,
                )

            # Add model's tool calls to history (preserve thought_signature)
            if response.raw_content:
                history.append(Message(
                    role="model",
                    parts=[],
                    raw_content=response.raw_content
                ))
            else:
                history.append(Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls]
                ))

            logger.info(
                "🧠 [SmartResponseAgent] Turn %s - Delegating to %s agents",
                turn + 1,
                len(response.tool_calls)
            )
            tool_responses = await self._execute_agents_smart_parallel(
                tool_calls=response.tool_calls,
                user_id=user_id,
                session_id=session_id,
                account_id=account_id,
            )
            logger.info(
                "🧠 [SmartResponseAgent] Turn %s - Agent delegation completed",
                turn + 1
            )

            # Capture structured data, history contexts, and delivery items
            for tool_response in tool_responses:
                if tool_response.structured_data and structured_data is None:
                    structured_data = tool_response.structured_data
                if tool_response.history_context:
                    for key, value in tool_response.history_context.items():
                        accumulated_history.setdefault(key, []).append(value)
                all_delivery_items.extend(tool_response.delivery_items)

            history.append(Message(
                role="user",
                parts=[
                    MessagePart(tool_response={
                        "name": tool_response.name,
                        "response": {"result": tool_response.result_str}
                    })
                    for tool_response in tool_responses
                ]
            ))

        smart_response = SmartResponse(
            text="I'm still thinking about this, but let's pause here.",
            structured_data=structured_data
        )
        return AgentLoopResult(
            smart_response=smart_response,
            total_tokens=total_tokens,
            history_contexts=accumulated_history or None,
            delivery_items=all_delivery_items,
        )

    async def _execute_agents_smart_parallel(
        self,
        tool_calls: List[ToolCall],
        user_id: str,
        session_id: str,
        account_id: Optional[str] = None,
    ) -> List[ToolResponse]:
        """
        Execute agent calls with smart ordering:
        - search_memory first (sequential)
        - other agent calls in parallel
        """
        results: List[Optional[ToolResponse]] = [None] * len(tool_calls)
        memory_context: List[str] = []

        def _is_memory_call(tc: ToolCall) -> bool:
            return (
                tc.name == "delegate_to_specialist"
                and (tc.args or {}).get("intent") == "search_memory"
            )

        memory_calls = [(idx, tc) for idx, tc in enumerate(tool_calls) if _is_memory_call(tc)]
        other_calls = [(idx, tc) for idx, tc in enumerate(tool_calls) if not _is_memory_call(tc)]

        # Phase 1: execute memory searches first (sequential)
        for idx, tool_call in memory_calls:
            logger.info("🧠 [SmartResponseAgent] Priority execution: search_memory")
            memory_result = await self._delegate_to_agent_with_retry(
                tool_call=tool_call,
                user_id=user_id,
                session_id=session_id,
                account_id=account_id,
            )
            results[idx] = memory_result
            if memory_result.result_str:
                memory_context.append(memory_result.result_str)

        # Phase 2: execute other agents in parallel
        if other_calls:
            logger.info(
                "⚡ [SmartResponseAgent] Parallel execution: "
                f"{len(other_calls)} agents ({[tc.name for _, tc in other_calls]})"
            )
            tasks = [
                self._delegate_to_agent_with_retry(
                    tool_call=tc,
                    user_id=user_id,
                    session_id=session_id,
                    account_id=account_id,
                    memory_context=memory_context,
                )
                for _, tc in other_calls
            ]

            start_time = asyncio.get_event_loop().time()
            parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = asyncio.get_event_loop().time() - start_time
            logger.info(f"✅ Parallel execution completed in {elapsed:.2f}s")

            for (idx, tool_call), result in zip(other_calls, parallel_results):
                if isinstance(result, Exception):
                    results[idx] = ToolResponse(
                        name=tool_call.name,
                        result_str=f"AGENT ERROR: {str(result)}"
                    )
                else:
                    results[idx] = result

        return [res for res in results if res is not None]

    async def _delegate_to_agent_with_retry(
        self,
        tool_call: ToolCall,
        user_id: str,
        session_id: str,
        account_id: Optional[str] = None,
        memory_context: Optional[List[str]] = None,
    ) -> ToolResponse:
        """
        Handle a delegate_to_specialist tool call via coordinator.handle_delegation().

        Extracts intent, query, and optional params from the tool_call args.
        """
        if not self.coordinator:
            return ToolResponse(
                name=tool_call.name,
                result_str="SYSTEM ERROR: AgentCoordinator not configured."
            )

        args = tool_call.args or {}
        intent = args.get("intent", "")
        query = args.get("query", "")
        context_params = args.get("context", {})  # optional rich params from LLM
        # LLM may pass context as a free-form string (task reasoning/background).
        # Wrap it so the specialist receives it as reasoning appended to the query.
        if isinstance(context_params, str) and context_params:
            context_params = {"reasoning": context_params}
        elif not isinstance(context_params, dict):
            context_params = {}

        if not intent:
            return ToolResponse(
                name=tool_call.name,
                result_str=f"SYSTEM ERROR: delegate_to_specialist called without 'intent'. args={args}"
            )

        # Build delegation context — merge session context with memory and extra params
        delegation_context: Dict[str, Any] = {
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "memory_context": memory_context or [],
            "params": context_params,   # spread into AgentMessage.payload in _execute_sync
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
                structured_data = response.metadata.get("structured_data") if response.metadata else None
                if intent == "search_emails":
                    result_str = self._format_email_search_compact(response.result)
                else:
                    result_str = self._format_agent_result(response.result)
                logger.info(
                    f"✅ [SmartResponseAgent] delegate_to_specialist result: intent={intent}\n"
                    f"   result: {result_str}"
                )
                return ToolResponse(
                    name=tool_call.name,
                    result_str=result_str,
                    structured_data=structured_data,
                    history_context=response.history_context,
                    delivery_items=response.delivery_items,
                )

            # FAILED — validation or business logic rejection. Retrying won't help.
            # Return immediately so the LLM can self-correct on the next turn.
            logger.warning(
                f"⚠️ Delegation intent='{intent}' rejected by specialist: {response.error}"
            )
            return ToolResponse(
                name=tool_call.name,
                result_str=(
                    f"SYSTEM: Specialist agent rejected the request. "
                    f"Error: {response.error} "
                    f"Correct your input and try again."
                )
            )

    def _format_agent_result(self, result: Any) -> str:
        """Format agent result into string for LLM tool_response."""
        if isinstance(result, list):
            return "\n".join(str(item) for item in result)
        if isinstance(result, SmartResponse):
            return result.text
        return str(result)

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

    async def _generate_history_summary(self, response_text: str) -> Optional[str]:
        """
        Post-processing step: generate a compact history summary via HistorySummaryService.

        Delegates to HistorySummaryService (Gemini, BALANCED tier).
        Returns None if service is not configured or call fails — caller uses full text.
        """
        if not self.history_summary_service:
            return None
        return await self.history_summary_service.summarize_model_response(response_text)

    def _get_tool_declarations(self) -> List[Dict[str, Any]]:
        """Build tool declarations for LLM API. Available intents injected from AgentRegistry."""
        available_intents = (
            self.coordinator.get_available_intents_for(self._descriptor)
            if self.coordinator else []
        )

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

    def _summarize_history(self, history: List[Message]) -> str:
        """Summarize message history for debugging."""
        summary: List[str] = []
        for msg in history:
            parts: List[str] = []
            for part in msg.parts:
                if part.text:
                    parts.append("text")
                elif part.tool_call:
                    parts.append(f"tool_call:{part.tool_call.name}")
                elif part.tool_response:
                    parts.append(f"tool_response:{part.tool_response.get('name')}")
                elif part.file_data:
                    parts.append("file")
            summary.append(f"{msg.role}({','.join(parts)})")
        return " -> ".join(summary)

    def _validate_history(self, history: List[Message]) -> None:
        """Validate history consistency before sending to LLM."""
        if not history:
            return

        last_msg = history[-1]
        last_has_call = any(part.tool_call for part in last_msg.parts)
        last_has_response = any(part.tool_response for part in last_msg.parts)
        if last_has_call and not last_has_response:
            raise ValueError("HISTORY_VIOLATION: tool_call without immediate tool_response")

        if len(history) >= 2:
            prev_msg = history[-2]
            if prev_msg.role == last_msg.role:
                raise ValueError(
                    "HISTORY_VIOLATION: consecutive turns with same role "
                    f"({last_msg.role}) detected"
                )

    def _sanitize_tool_history(self, history: List[Message]) -> List[Message]:
        """Sanitize history to remove invalid tool interactions."""
        if not history:
            return history

        cleaned: List[Message] = []
        pending_tool_call = False
        for msg in history:
            has_tool_call = any(part.tool_call for part in msg.parts)
            has_tool_response = any(part.tool_response for part in msg.parts)

            if has_tool_response and not pending_tool_call:
                continue

            if cleaned and cleaned[-1].role == msg.role:
                prev = cleaned[-1]
                prev_has_response = any(part.tool_response for part in prev.parts)

                if prev_has_response:
                    continue

                if has_tool_response:
                    cleaned[-1] = msg
                else:
                    prev.parts.extend(msg.parts)
                continue

            cleaned.append(msg)
            if has_tool_call:
                pending_tool_call = True
            if has_tool_response:
                pending_tool_call = False

        while cleaned:
            last = cleaned[-1]
            has_tool_call = any(part.tool_call for part in last.parts)
            has_tool_response = any(part.tool_response for part in last.parts)
            if has_tool_call and not has_tool_response:
                cleaned.pop()
                continue
            break

        return cleaned


def create_smart_response_agent(
    execution_context: AgentExecutionContext,
    session_store: SessionStore,
    prompt_builder: PromptBuilderPort,
    repository: Optional[Any] = None,
    embedding_service: Optional[Any] = None,
    coordinator: "AgentCoordinator" = None,  # type: ignore
    user_id: Optional[str] = None,
    model_name: Optional[str] = None,
    history_recent_full_turns: int = 5,
    history_summary_service: Optional[HistorySummaryService] = None
) -> SmartResponseAgent:
    """Factory function to create SmartResponseAgent."""
    agent_id = f"smart_response_agent_{user_id}" if user_id else "smart_response_agent"

    config = AgentConfig(
        agent_id=agent_id,
        agent_type="smart_response",
        llm_model=model_name or execution_context.model_name,
        max_retries=SmartResponseAgent.CONFIG_MAX_RETRIES,
        timeout_ms=SmartResponseAgent.TIMEOUT_MS,
        capabilities=["complex_reasoning", "agent_delegation", "tool_use"],
        metadata={
            "description": "Complex LLM responses with agent delegation",
            "user_id": user_id
        }
    )

    return SmartResponseAgent(
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

