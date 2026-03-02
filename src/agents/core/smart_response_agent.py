"""
Smart Response Agent
====================

Handles complex requests with full LLM reasoning and agent delegation.
This agent orchestrates specialist agents (memory/web) via AgentCoordinator.

Note on terminology:
- LLM APIs (Gemini) require "tools" in the request/response schema.
- In Full Actor Model, these "tools" are actually specialist agents.
- We keep the technical "tools" format for API compatibility and add
  comments to avoid confusion for future readers.
"""

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Iterable

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
from ...domain.messaging import SmartResponse, RichContent
from ...ports.llm_service import (
    LLMService,
    LLMResponse,
    ToolCall,
    Message,
    MessagePart,
    LLMRequest
)
from ...ports.session_store import SessionStore
from ...ports.prompt_builder_port import PromptBuilderPort
from ...ports.llm_service import AgentExecutionContext
from ...services.history_summary_service import HistorySummaryService
from ...utils.logger import logger
from ...utils.debug_logger import get_debug_logger
from ...utils.llm_response_parser import parse_llm_response


@dataclass
class ToolResponse:
    """Internal container for delegated agent results."""
    name: str
    result_str: str
    structured_data: Optional[RichContent] = None


@dataclass
class AgentLoopResult:
    """Result from the agent delegation loop."""
    smart_response: SmartResponse
    total_tokens: int
    history_summary: Optional[str] = None


class SmartResponseAgent(BaseAgent):
    """
    Smart Response Agent for complex reasoning with agent delegation.

    Characteristics:
    - Uses full LLM model (gemini-3-pro-preview)
    - Large context window (60 messages)
    - Delegates to specialist agents via AgentCoordinator
    - Multi-turn reasoning (max 5 turns)
    - Parallel execution for multiple external agent calls

    Execution ordering:
    1) search_memory ALWAYS first (sequential) to build context
    2) other agent calls run in parallel (asyncio.gather)
    """

    # LEGACY Provider Refactor Session 12: Default model handled by AgentExecutionContext
    # DEFAULT_MODEL = "gemini-3-pro-preview"
    CONTEXT_WINDOW = 60
    MAX_DELEGATION_TURNS = 5
    MAX_AGENT_RETRIES = 2
    RETRY_BACKOFF_SECONDS = 1

    # ACP v2: agent delegation goes through coordinator.handle_delegation()
    # No hardcoded map — registry provides intent → agent routing.

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
        return bool(text) or has_attachments

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute complex response generation with agent delegation.
        """
        text = message.payload.get("text", "")
        session_id = message.context.get("session_id")
        user_id = message.context.get("user_id")
        account_id = message.context.get("account_id")  # SESSION_26
        routing_metadata = RoutingMetadata.from_dict(message.context.get("routing", {}))
        self.config.metadata["user_tone"] = routing_metadata.user_tone

        logger.info(
            f"🧠 [SmartResponseAgent] Processing: '{text[:50]}...'"
            if len(text) > 50 else f"🧠 [SmartResponseAgent] Processing: '{text}'"
        )

        try:
            # 0. Load biographical facts first, then merge with Router enrichment
            enriched_context = message.context.get("enriched_context")
            
            # Load cached biographical facts (MUST load before merge!)
            cached_biographical = []
            if account_id and self.repository:
                try:
                    logger.info(f"🔍 [TRACE] SmartAgent: calling get_biographical_context_cached() - auto-resolve from RequestContext")
                    # SESSION_27: Auto-resolve owner_id from RequestContext
                    cached_biographical = await self.repository.get_biographical_context_cached(
                        limit=100  # owner_id auto-resolved from RequestContext
                    )
                    logger.info(f"🔍 [TRACE] SmartAgent: got {len(cached_biographical)} biographical facts")
                except Exception as e:
                    logger.warning(f"🧠 [SmartResponseAgent] Failed to load biographical: {e}")
            
            # Now merge with Router semantic enrichment
            biographical_facts = self.prompt_builder.merge_enriched_context_with_biographical(
                enriched_context=enriched_context,
                cached_biographical=cached_biographical  # Pass loaded facts!
            )
            
            if enriched_context and enriched_context.get("facts"):
                logger.info(
                    "🧠 [SmartResponseAgent] Merged context: %s biographical + %s semantic = %s total",
                    len(cached_biographical),
                    len(enriched_context.get("facts", [])),
                    len(biographical_facts)
                )

            logger.info("🧠 [SmartResponseAgent] 1. Building system prompt")
            prompt_user_id = self.user_id or user_id
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="smart",
                user_id=prompt_user_id,
                account_id=account_id,  # SESSION_26
                routing_metadata=routing_metadata,
                capabilities=self.execution_context.capabilities,
                biographical_facts=biographical_facts  # Enriched with semantic context
            )
            logger.info("🧠 [SmartResponseAgent] 1.1. System prompt ready")

            logger.info("🧠 [SmartResponseAgent] 2. Loading conversation context")
            current_message_parts = message.context.get("current_message_parts", [])
            conversation_history = await self._load_conversation_context(
                session_store=self.session_store,
                session_id=session_id,
                current_message_parts=current_message_parts,
                context_window=self.CONTEXT_WINDOW
            )
            logger.info("🧠 [SmartResponseAgent] 2.1. Loaded %s messages", len(conversation_history))

            logger.info("🧠 [SmartResponseAgent] 3. Sanitizing history")
            clean_history = self._sanitize_tool_history(conversation_history)
            logger.info(
                "🧠 [SmartResponseAgent] 3.1. Sanitized to %s messages: %s",
                len(clean_history),
                self._summarize_history(clean_history)
            )

            logger.info("🧠 [SmartResponseAgent] 4. Entering delegation loop")
            loop_result = await self._execute_agent_delegation_loop(
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,
                system_prompt=system_prompt,
                history=clean_history,
                tool_declarations=self._get_tool_declarations()
            )
            logger.info("🧠 [SmartResponseAgent] 4.1. Delegation loop completed")

            smart_response = loop_result.smart_response
            total_tokens = loop_result.total_tokens
            history_summary = loop_result.history_summary

            # Post-processing: fire-and-forget history summary generation.
            # Launched as background task — does NOT block user response delivery.
            # conversation_handler awaits the task after sending to Slack.
            enable_history_optimization = os.getenv("ENABLE_HISTORY_OPTIMIZATION", "false").lower() in ("true", "1", "yes")
            summary_task = None
            if not history_summary and enable_history_optimization and smart_response.text:
                summary_task = asyncio.create_task(
                    self._generate_history_summary(smart_response.text)
                )

            logger.info(
                f"✅ [SmartResponseAgent] Response generated "
                f"({len(smart_response.text)} chars, {total_tokens} tokens)"
            )

            # Prepare metadata — pass summary task for async resolution in conversation_handler
            metadata = {
                "model": self.model_name,
                "tokens": total_tokens,
                "response_length": len(smart_response.text)
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
            logger.error(f"❌ [SmartResponseAgent] Error: {e}", exc_info=True)
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

            # DEBUG: Log full history sent to LLM — only on Turn 1 (avoids duplicate files)
            debug_logger = get_debug_logger()
            if turn == 0:
                def _fmt_part(p) -> str:
                    if p.text:
                        return p.text
                    if p.tool_call:
                        return f"[tool_call: {p.tool_call.name} args={p.tool_call.args}]"
                    if p.tool_response:
                        name = p.tool_response.get("name", "?") if isinstance(p.tool_response, dict) else str(p.tool_response)
                        content = p.tool_response.get("response", "") if isinstance(p.tool_response, dict) else ""
                        content_str = str(content) if content else ""
                        preview = content_str[:500] + ("..." if len(content_str) > 500 else "")
                        return f"[tool_response: {name} ({len(content_str)} chars)]\n{preview}"
                    if p.file_data:
                        return "[file_data]"
                    return "[raw_content]"

                history_str = "\n---\n".join([
                    f"[{msg.role.upper()}]\n" + "\n".join([_fmt_part(p) for p in msg.parts])
                    for msg in debug_history
                ])
                debug_logger.log_prompt(
                    agent_name="smart_response",
                    prompt=history_str,
                    system_instruction=system_prompt,
                    metadata={"model": self.model_name, "user_id": user_id[:8] if user_id else "unknown", "turn": turn + 1}
                )
            
            llm_start = time.time()
            try:
                request = LLMRequest(
                    model_name=self.model_name,
                    system_instruction=system_prompt,
                    messages=debug_history,
                    tools=tool_declarations,
                    temperature=0.7
                )
                response: LLMResponse = await self.llm.generate_content(request=request)
                
                # DEBUG: Log response when it's the final answer (no more tool calls)
                if not response.tool_calls:
                    debug_logger = get_debug_logger()
                    debug_logger.log_response(
                        agent_name="smart_response",
                        response=response.text or "",
                        metadata={
                            "model": self.model_name,
                            "user_id": user_id[:8] if user_id else "unknown",
                            "turn": turn + 1,
                            "tokens": response.usage_metadata.total_tokens if response.usage_metadata else 0
                        }
                    )
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

                # Debug log (mirrors the no-tool-call path)
                debug_logger = get_debug_logger()
                debug_logger.log_response(
                    agent_name="smart_response",
                    response=user_text,
                    metadata={
                        "model": self.model_name,
                        "user_id": user_id[:8] if user_id else "unknown",
                        "turn": turn + 1,
                        "tokens": response.usage_metadata.total_tokens if response.usage_metadata else 0,
                        "via": "deliver_response",
                        "history_summary": summary or "(none)"
                    }
                )

                final_rich = rich if rich else structured_data
                smart_response = SmartResponse(text=user_text, structured_data=final_rich)

                history_text = summary if summary else user_text
                history.append(Message(role="model", parts=[MessagePart(text=history_text)]))

                return AgentLoopResult(
                    smart_response=smart_response,
                    total_tokens=total_tokens,
                    history_summary=summary
                )

            if not response.tool_calls:
                logger.info(
                    "🧠 [SmartResponseAgent] Turn %s - No tool calls, returning response",
                    turn + 1
                )

                # Parse final response (fallback: LLM skipped deliver_response tool)
                user_text, summary, rich = parse_llm_response(response.text or "")

                # Prefer parsed rich content, fallback to accumulated
                final_rich = rich if rich else structured_data

                smart_response = SmartResponse(
                    text=user_text or "",
                    structured_data=final_rich
                )

                # Add to history (use summary if available for optimization)
                history_text = summary if summary else (user_text or "")
                history.append(Message(role="model", parts=[MessagePart(text=history_text)]))

                return AgentLoopResult(
                    smart_response=smart_response,
                    total_tokens=total_tokens,
                    history_summary=summary
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

            # Capture structured data from first successful response
            for tool_response in tool_responses:
                if tool_response.structured_data and structured_data is None:
                    structured_data = tool_response.structured_data

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
        return AgentLoopResult(smart_response=smart_response, total_tokens=total_tokens)

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

        # ACP v2: delegate_to_specialist with intent="search_memory" runs first (memory-first ordering).
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

        # All results should be populated now
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

        Extracts intent, query, and optional params from the tool_call args,
        then routes through AgentRegistry (ACP v2).
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

        # Detailed log for search_memory (preserves observability from ACP v1)
        if intent == "search_memory":
            logger.info(
                f"🔍 [SmartResponseAgent] === CALLING search_memory ===\n"
                f"   intent: {intent}\n"
                f"   query: {query}\n"
                f"   context_params: {context_params}"
            )
        else:
            logger.info(
                f"🔄 [SmartResponseAgent] delegate_to_specialist: intent={intent}, query={query}"
            )

        for attempt in range(self.MAX_AGENT_RETRIES + 1):
            response = await self.coordinator.handle_delegation(
                intent=intent,
                query=query,
                context=delegation_context,
                calling_agent_id=self.agent_id,
            )

            if response.status == AgentStatus.SUCCESS:
                result_str = self._format_agent_result(response.result)
                structured_data = response.metadata.get("structured_data") if response.metadata else None
                logger.info(
                    f"✅ [SmartResponseAgent] delegate_to_specialist result: intent={intent}\n"
                    f"   result preview: {result_str[:300]}{'...' if len(result_str) > 300 else ''}"
                )
                return ToolResponse(
                    name=tool_call.name,
                    result_str=result_str,
                    structured_data=structured_data
                )

            if attempt < self.MAX_AGENT_RETRIES:
                logger.warning(
                    f"⚠️ Delegation intent='{intent}' failed (attempt {attempt + 1}/"
                    f"{self.MAX_AGENT_RETRIES + 1}). Retrying..."
                )
                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
                continue

            return ToolResponse(
                name=tool_call.name,
                result_str=f"AGENT ERROR: {response.error}"
            )

        return ToolResponse(
            name=tool_call.name,
            result_str="AGENT ERROR: Max retries exceeded"
        )

    def _format_agent_result(self, result: Any) -> str:
        """Format agent result into string for LLM tool_response."""
        if isinstance(result, list):
            return "\n".join(str(item) for item in result)
        if isinstance(result, SmartResponse):
            return result.text
        return str(result)

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
        """
        Build tool declarations for LLM API (ACP v2).

        SmartAgent has one generic delegation tool: delegate_to_specialist.
        Available intents are injected dynamically from AgentRegistry via coordinator.

        IMPORTANT: Gemini requires "tools" schema in requests. We keep this
        technical format even though these are agent delegation endpoints.

        TODO (owner): update delegate_to_specialist description text in Firestore
        prompt tokens — user will craft final prompt copy.
        """
        available_intents = (
            self.coordinator.get_available_intents()
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
        max_retries=0,       # No retry on timeout: retry doubles wall time to 5min, terrible UX
        timeout_ms=240000,   # 4 min: 150s was boundary (Claude 149.7s case); Cloud Run allows 300s
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

