"""
Smart Response Agent
====================

Handles complexity 6–10 requests with multi-turn specialist delegation.
Orchestrates specialists via AgentCoordinator — memory-first, others in parallel.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from ..base_agent import BaseAgent
from ...infrastructure.agent_config import SMART, ENABLE_HISTORY_OPTIMIZATION
from ...infrastructure.agent_manifest import SMART_RESPONSE
from ...infrastructure.delegation_engine import DelegationEngine, DelegationResult
from ...domain.agent import (
    AgentMessage,
    AgentResponse,
    AgentConfig,
    AgentIntent,
    RoutingMetadata,
)
from ...domain.messaging import SmartResponse, RichContent
from ...ports.llm_port import (
    LLMResponse,
    Message,
    MessagePart,
    LLMRequest,
)
from ...ports.session_store import SessionStore
from ...ports.prompt_builder_port import PromptBuilderPort
from ...ports.llm_port import AgentExecutionContext
from ...infrastructure.task_execution_resolver import ExecutionOverride
from ...utils.logger import logger
from ...utils.llm_response_parser import parse_llm_response

if TYPE_CHECKING:
    from ...services.history_summary_service import HistorySummaryService
    from ...infrastructure.task_execution_resolver import TaskExecutionResolver
    from ...domain.user import UserBotConfig
    from ...infrastructure.agent_coordinator import AgentCoordinator


@dataclass(frozen=True)
class _EffectiveExecution:
    """Per-call resolved execution parameters. Private to this module.

    Built once at the top of ``execute()`` and threaded through every
    downstream call site. Replaces the mutate-self-then-restore pattern
    that previously required ``_execute_lock`` to serialize concurrent
    runs.
    """
    ctx: AgentExecutionContext
    thinking_effort: Optional[str]


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

    # Structured output envelope. Enforced by Gemini via response_json_schema and by Claude
    # via output_config.format. OpenAI/Grok react via json_object mode without forwarding the
    # inner schema — actual envelope shape is enforced by the OUTPUT_FORMAT token in the prompt.
    # See CLAUDE.md "Agent Output Format Standards" for the per-provider matrix.
    # Fully describes the response envelope so Gemini's constrained decoding populates
    # every field. Gemini will NOT generate content for an under-specified field: an
    # `{"type": "object"}` with no `properties` comes back as `{}` and a bare
    # `{"type": "array"}` as `[]` — which is how widgets (rich_content.data) and source
    # links (link_list) were silently dropped, especially on Flash. Each object therefore
    # declares its properties and each array its item shape. Dict schemas are routed to
    # Gemini's `responseJsonSchema` (not the stricter `responseSchema`), so nesting that
    # would 400 the OpenAPI subset is accepted here — verified on gemini-flash-latest for
    # widget/table/file/text/search. Structure mirrors the OUTPUT_FORMAT_JSON token.
    _RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["full_response", "response_summary", "rich_content", "link_list"],
        "properties": {
            "full_response":    {"type": "string"},
            "response_summary": {"type": "string", "maxLength": 300},
            "rich_content": {
                "type": "object",
                "nullable": True,  # null for text-only responses
                "properties": {
                    "type":     {"type": "string", "enum": ["widget", "file", "table"]},
                    "fallback": {"type": "string"},
                    "data": {
                        "type": "object",
                        "properties": {
                            # table
                            "title":    {"type": "string"},
                            "headers":  {"type": "array", "items": {"type": "string"}},
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "cells": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                            },
                            "footer":   {"type": "string"},
                            # widget
                            "html":     {"type": "string"},
                            "alt_text": {"type": "string"},
                            # file
                            "filename": {"type": "string"},
                            "content":  {"type": "string"},
                        },
                    },
                },
            },
            # required forces presence; no-source turns correctly yield [] (verified —
            # Gemini does not fabricate URLs to fill it).
            "link_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["anchor", "title", "url"],
                    "properties": {
                        "anchor": {"type": "string"},
                        "title":  {"type": "string"},
                        "url":    {"type": "string"},
                    },
                },
            },
        },
    }
    TIMEOUT_MS = SMART.timeout_ms

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        session_store: SessionStore,
        prompt_builder: PromptBuilderPort,
        resolver: "TaskExecutionResolver",
        user_config: "UserBotConfig",
        repository: Optional[Any] = None,
        embedding_service: Optional[Any] = None,
        coordinator: "AgentCoordinator" = None,  # type: ignore
        model_name: Optional[str] = None,
        history_recent_full_turns: int = 5,
        history_summary_service: Optional[HistorySummaryService] = None,
        user_timezone: str = "UTC",
        thinking_effort: Optional[str] = None,
    ):
        super().__init__(config)
        self.execution_context = execution_context
        self.llm = execution_context.provider
        self._set_execution_context(execution_context)
        self.session_store = session_store
        self.prompt_builder = prompt_builder
        self.resolver = resolver
        self.user_config = user_config
        self.repository = repository
        self.embedding_service = embedding_service
        self.coordinator = coordinator
        self.model_name = model_name or execution_context.model_name
        self.history_recent_full_turns = history_recent_full_turns
        self.history_summary_service = history_summary_service
        self._user_timezone = user_timezone
        self._default_thinking_effort = thinking_effort

        # NOTE: ``self.llm``, ``self.model_name``, ``self.execution_context``,
        # and ``self._agent_execution_context`` (set by _set_execution_context
        # above) are READ-ONLY after construction. Per-call overrides flow
        # through ``_EffectiveExecution`` resolved by ``_resolve_effective``
        # — never written back to ``self.*``. This is what allows concurrent
        # ``execute()`` calls per user to run in parallel without a lock.
        # See docs/04_solution_strategy/decisions/per_call_execution_context.md.

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
        """Resolve effective execution parameters, then run the work.

        No lock, no mutation of ``self.*``. Concurrent ``execute()`` calls
        for the same user instance run in parallel; each carries its own
        ``_EffectiveExecution`` through every downstream call site.
        """
        eff = self._resolve_effective(message)
        return await self._run(message, eff)

    def _resolve_effective(self, message: AgentMessage) -> _EffectiveExecution:
        """Build per-call ``_EffectiveExecution`` from message + agent defaults.

        Priority chain:
          1. Explicit ``ExecutionOverride`` placed on
             ``message.context["execution_override"]`` by the caller.
          2. ``TaskExecutionResolver.resolve(...)`` based on
             ``message.context["task_complexity"]``.
          3. Agent defaults (``self.execution_context`` and
             ``self._default_thinking_effort``).

        ``thinking_effort`` resolution:
          - When an override is present and its ``thinking_effort`` is set,
            that value wins.
          - Otherwise: ``message.context["thinking_effort"]`` →
            ``self._default_thinking_effort``.
        """
        explicit = message.context.get("execution_override")
        override: Optional[ExecutionOverride] = (
            explicit if isinstance(explicit, ExecutionOverride) else None
        )
        if override is None:
            override = self.resolver.resolve(message.context, self.user_config)

        ctx_thinking = message.context.get("thinking_effort") or self._default_thinking_effort

        if override is None:
            return _EffectiveExecution(
                ctx=self.execution_context,
                thinking_effort=ctx_thinking,
            )

        thinking = override.thinking_effort if override.thinking_effort is not None else ctx_thinking
        logger.info(
            f"⚡ [SmartResponseAgent] Complexity override applied: "
            f"model={override.execution_context.model_name}"
        )
        return _EffectiveExecution(
            ctx=override.execution_context,
            thinking_effort=thinking,
        )

    async def _run(
        self, message: AgentMessage, eff: _EffectiveExecution
    ) -> AgentResponse:
        """Execute the delegation loop using a fully-resolved per-call context.

        This method receives ``eff`` explicitly and never reads per-call
        configuration from ``self.*``. ``self.execution_context`` /
        ``self.llm`` / ``self.model_name`` are still used as defaults inside
        ``_resolve_effective``, but every consumer below reads from ``eff``.
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

            email_for_triage = message.context.get("email_for_triage")
            extra_static_blocks = None
            if email_for_triage:
                extra_static_blocks = [
                    "email_for_triage {\n"
                    + json.dumps(email_for_triage, ensure_ascii=False, indent=2)
                    + "\n}"
                ]

            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="smart",
                user_id=prompt_user_id,
                account_id=account_id,
                routing_metadata=routing_metadata,
                capabilities=eff.ctx.capabilities,
                biographical_facts=biographical_facts,
                kb_preamble=True,
                agent_notes=agent_notes,
                extra_static_blocks=extra_static_blocks,
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

            clean_history = self._sanitize_tool_history(conversation_history)

            engine = DelegationEngine(self.coordinator)
            base_request = LLMRequest(
                model_name=eff.ctx.model_name,
                system_instruction=system_prompt,
                messages=clean_history,
                tools=self._get_tool_declarations(),
                temperature=self.DELEGATION_TEMPERATURE,
                response_schema=self._RESPONSE_SCHEMA,
                thinking=eff.thinking_effort,
            )

            # Closure: every LLM call inside the engine routes through the
            # per-call provider in eff.ctx, with eff.ctx as the fallback ctx.
            # No mutation of self.llm / self._agent_execution_context.
            async def call_llm_for_engine(req: "LLMRequest", turn: int = 0) -> "LLMResponse":
                return await self._call_llm(
                    req,
                    turn,
                    llm_override=eff.ctx.provider,
                    fallback_ctx_override=eff.ctx,
                )

            delegation_result = await engine.execute(
                call_llm=call_llm_for_engine,
                base_request=base_request,
                context=message.context,
                max_turns=self.MAX_DELEGATION_TURNS,
                terminal_tool="deliver_response",
                intent_fanout=dict(self._descriptor.intent_fanout),
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

            smart_response, history_summary = self._build_smart_response(delegation_result)
            total_tokens = delegation_result.total_tokens
            delivery_items = delegation_result.delivery_items

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
                "model": eff.ctx.model_name,
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
                    "💾 [SmartResponseAgent] history_contexts set: %s",
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
                error=f"Smart response failed: {str(e)}"
            )

    def _build_smart_response(
        self, result: DelegationResult,
    ) -> tuple[SmartResponse, Optional[str]]:
        """Convert DelegationResult into SmartResponse + optional history_summary.

        Two paths:
        - terminal_tool_args present → extract structured fields from deliver_response
        - text present → parse JSON via parse_llm_response (fallback path)
        """
        if result.terminal_tool_args:
            args = result.terminal_tool_args
            user_text = args.get("full_response", "")
            summary = args.get("history_summary")
            rich_data = args.get("rich_content")
            rich = (
                RichContent(
                    content_type=rich_data.get("type", "unknown"),
                    data=rich_data.get("data", {}),
                    fallback_text=rich_data.get("fallback", ""),
                )
                if isinstance(rich_data, dict) else None
            )
            tool_links = [
                item for item in (args.get("link_list") or [])
                if isinstance(item, dict) and "anchor" in item and "title" in item and "url" in item
            ]
            final_rich = rich if rich else result.structured_data
            return SmartResponse(text=user_text, structured_data=final_rich, link_list=tool_links), summary

        # Fallback: LLM returned text without terminal tool
        user_text, summary, rich, link_list = parse_llm_response(result.text)
        final_rich = rich if rich else result.structured_data
        return SmartResponse(text=user_text or "", structured_data=final_rich, link_list=link_list), summary

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
        available_intents = self.coordinator.get_available_intents_for(self._descriptor) if self.coordinator else []
        return [self._build_delegate_tool_declaration(available_intents)]

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
    resolver: "TaskExecutionResolver",
    user_config: "UserBotConfig",
    repository: Optional[Any] = None,
    embedding_service: Optional[Any] = None,
    coordinator: "AgentCoordinator" = None,  # type: ignore
    user_id: Optional[str] = None,
    model_name: Optional[str] = None,
    history_recent_full_turns: int = 5,
    history_summary_service: Optional[HistorySummaryService] = None,
    user_timezone: str = "UTC",
    thinking_effort: Optional[str] = None,
) -> SmartResponseAgent:
    """Factory function to create SmartResponseAgent."""
    agent_id = f"smart_response_agent_{user_id}" if user_id else "smart_response_agent"

    config = AgentConfig(
        agent_id=agent_id,
        agent_type="smart_response",
        llm_model=model_name or execution_context.model_name,
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
        resolver=resolver,
        user_config=user_config,
        repository=repository,
        embedding_service=embedding_service,
        coordinator=coordinator,
        model_name=model_name,
        history_recent_full_turns=history_recent_full_turns,
        history_summary_service=history_summary_service,
        user_timezone=user_timezone,
        thinking_effort=thinking_effort,
    )

