"""
Delegation Engine
=================

Reusable tool-calling loop for multi-turn LLM ↔ specialist delegation.

Owns loop mechanics, tool dispatch, and history management.
Does NOT own: LLM parameters (agent builds LLMRequest), response parsing
(agent post-processes DelegationResult), prompt assembly, or history loading.

Used by SmartResponseAgent, QuickResponseAgent, and bound channel agents.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from ..domain.agent import AgentStatus, DeliveryItem
from ..domain.messaging import SmartResponse
from ..ports.llm_port import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from ..domain.llm import build_tool_turn
from ..utils.logger import logger

if TYPE_CHECKING:
    from .agent_coordinator import AgentCoordinator


# ------------------------------------------------------------------ #
# Data structures                                                      #
# ------------------------------------------------------------------ #

@dataclass
class ToolResult:
    """Single tool call result collected during the loop."""
    name: str
    result_str: str
    structured_data: Any = None
    history_context: Optional[Dict[str, Any]] = None
    delivery_items: List[DeliveryItem] = field(default_factory=list)
    file_data: Optional[Dict[str, Any]] = None


@dataclass
class DelegationResult:
    """Complete result from the delegation loop."""
    text: str
    total_tokens: int
    terminal_tool_args: Optional[Dict[str, Any]] = None
    delivery_items: List[DeliveryItem] = field(default_factory=list)
    history_contexts: Optional[Dict[str, List[Any]]] = field(default=None)
    structured_data: Any = None
    messages: List[Message] = field(default_factory=list)
    failed: bool = False


# ------------------------------------------------------------------ #
# Result formatting                                                    #
# ------------------------------------------------------------------ #

def _format_result(intent: str, result: Any) -> str:
    """Format AgentResponse.result into a string for the LLM tool_response."""
    if intent == "search_emails":
        return _format_email_search_compact(result)
    if isinstance(result, SmartResponse):
        return result.text
    if isinstance(result, list):
        return "\n".join(str(item) for item in result)
    return str(result)


def _format_email_search_compact(result: Any) -> str:
    """Compact text representation of email search results for the LLM."""
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
    lines.append(
        "\nTo read email body: delegate get_email_details with context={\"email_id\": \"<id from above>\"}"
        "\nTo read attachment: delegate get_email_attachment with context={\"email_id\": \"<id>\", \"filename\": \"<filename>\"}"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Engine                                                               #
# ------------------------------------------------------------------ #

class DelegationEngine:
    """Reusable multi-turn tool-calling loop.

    The engine controls iteration, tool dispatch, and history management.
    The agent controls LLM parameters (via base_request) and result parsing
    (via post-processing DelegationResult).
    """

    def __init__(self, coordinator: AgentCoordinator) -> None:
        self._coordinator = coordinator

    async def execute(
        self,
        call_llm: Callable[[LLMRequest, int], Awaitable[LLMResponse]],
        base_request: LLMRequest,
        context: Dict[str, Any],
        max_turns: int,
        terminal_tool: Optional[str] = None,
        intent_remap: Optional[Dict[str, str]] = None,
        calling_agent_id: str = "delegation_engine",
        max_retries: int = 1,
        retry_backoff: float = 1.0,
    ) -> DelegationResult:
        """Run the delegation loop.

        Args:
            call_llm: Agent's _call_llm method (handles billing + debug).
            base_request: LLMRequest built by the agent. Engine updates only
                          ``messages`` each turn — temperature, schema, thinking
                          etc. stay as the agent configured them.
            context: Message context dict (from message.context). Passed through
                     to coordinator on each delegation call.
            max_turns: Maximum delegation iterations.
            terminal_tool: Optional tool name that signals loop termination
                           (e.g. "deliver_response" for Smart).
            intent_remap: Optional dispatch-time intent substitution
                          (e.g. {"search_web": "search_web_light"} for Quick).
            calling_agent_id: For coordinator logging.
            max_retries: Retries per individual tool dispatch.
            retry_backoff: Seconds between retries.
        """
        history = list(base_request.messages)
        total_tokens = 0
        accumulated_contexts: Dict[str, List[Any]] = {}
        all_delivery_items: List[DeliveryItem] = []
        accumulated_structured: Any = None
        remap = intent_remap or {}

        for turn in range(max_turns):
            # Build request with current history
            request = base_request.model_copy(update={"messages": history})

            logger.info(
                "🔄 [DelegationEngine] Turn %s/%s (history=%s msgs, caller=%s)",
                turn + 1, max_turns, len(history), calling_agent_id,
            )

            response: LLMResponse = await call_llm(request, turn + 1)

            if response.usage_metadata:
                total_tokens += response.usage_metadata.total_tokens

            # --- Terminal tool check ---
            if terminal_tool and response.tool_calls:
                terminal_call = next(
                    (tc for tc in response.tool_calls if tc.name == terminal_tool),
                    None,
                )
                if terminal_call:
                    logger.info(
                        "🔄 [DelegationEngine] Turn %s — terminal tool '%s' received",
                        turn + 1, terminal_tool,
                    )
                    return DelegationResult(
                        text="",
                        total_tokens=total_tokens,
                        terminal_tool_args=terminal_call.args or {},
                        delivery_items=all_delivery_items,
                        history_contexts=accumulated_contexts or None,
                        structured_data=accumulated_structured,
                        messages=history,
                    )

            # --- No tool calls → final text response ---
            if not response.tool_calls:
                logger.info(
                    "🔄 [DelegationEngine] Turn %s — no tool calls, returning text (%s chars)",
                    turn + 1, len(response.text or ""),
                )
                return DelegationResult(
                    text=response.text or "",
                    total_tokens=total_tokens,
                    delivery_items=all_delivery_items,
                    history_contexts=accumulated_contexts or None,
                    structured_data=accumulated_structured,
                    messages=history,
                )

            # --- Execute tool calls (memory-first parallel) ---
            logger.info(
                "🔄 [DelegationEngine] Turn %s — dispatching %s tool call(s)",
                turn + 1, len(response.tool_calls),
            )
            tool_results = await self._execute_tool_calls(
                tool_calls=response.tool_calls,
                context=context,
                intent_remap=remap,
                calling_agent_id=calling_agent_id,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )

            # --- Accumulate metadata ---
            for tr in tool_results:
                if tr.structured_data and accumulated_structured is None:
                    accumulated_structured = tr.structured_data
                if tr.history_context:
                    for key, value in tr.history_context.items():
                        accumulated_contexts.setdefault(key, []).append(value)
                all_delivery_items.extend(tr.delivery_items)

            # --- Append model message + tool responses to history ---
            turn_entries = [
                (tc, tr.result_str, tr.file_data)
                for tc, tr in zip(response.tool_calls, tool_results)
            ]
            history.extend(build_tool_turn(response, turn_entries))

        # Max turns exhausted
        logger.warning(
            "⚠️ [DelegationEngine] Max turns (%s) exhausted for %s",
            max_turns, calling_agent_id,
        )
        return DelegationResult(
            text="",
            total_tokens=total_tokens,
            delivery_items=all_delivery_items,
            history_contexts=accumulated_contexts or None,
            structured_data=accumulated_structured,
            messages=history,
            failed=True,
        )

    # ------------------------------------------------------------------ #
    # Tool execution — memory-first parallel                              #
    # ------------------------------------------------------------------ #

    async def _execute_tool_calls(
        self,
        tool_calls: List[ToolCall],
        context: Dict[str, Any],
        intent_remap: Dict[str, str],
        calling_agent_id: str,
        max_retries: int,
        retry_backoff: float,
    ) -> List[ToolResult]:
        """Execute tool calls with memory-first ordering.

        search_memory calls execute first (sequential), then remaining
        calls execute in parallel via asyncio.gather.
        """
        results: List[Optional[ToolResult]] = [None] * len(tool_calls)
        memory_context: List[str] = []

        def _is_memory(tc: ToolCall) -> bool:
            return (
                tc.name == "delegate_to_specialist"
                and (tc.args or {}).get("intent") == "search_memory"
            )

        memory_calls = [(i, tc) for i, tc in enumerate(tool_calls) if _is_memory(tc)]
        other_calls = [(i, tc) for i, tc in enumerate(tool_calls) if not _is_memory(tc)]

        # Phase 1: memory searches first (sequential)
        for idx, tc in memory_calls:
            logger.info("🔄 [DelegationEngine] Priority execution: search_memory")
            result = await self._dispatch_single(
                tc, context, intent_remap, calling_agent_id,
                max_retries, retry_backoff, memory_context,
            )
            results[idx] = result
            if result.result_str:
                memory_context.append(result.result_str)

        # Phase 2: other calls in parallel
        if other_calls:
            logger.info(
                "⚡ [DelegationEngine] Parallel execution: %s call(s)",
                len(other_calls),
            )
            tasks = [
                self._dispatch_single(
                    tc, context, intent_remap, calling_agent_id,
                    max_retries, retry_backoff, memory_context,
                )
                for _, tc in other_calls
            ]
            parallel_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (idx, tc), result in zip(other_calls, parallel_results):
                if isinstance(result, Exception):
                    logger.error("❌ [DelegationEngine] Tool call failed: %s", result)
                    results[idx] = ToolResult(
                        name=tc.name,
                        result_str=f"AGENT ERROR: {result}",
                    )
                else:
                    results[idx] = result

        return [r for r in results if r is not None]

    # ------------------------------------------------------------------ #
    # Single tool dispatch                                                #
    # ------------------------------------------------------------------ #

    async def _dispatch_single(
        self,
        tool_call: ToolCall,
        context: Dict[str, Any],
        intent_remap: Dict[str, str],
        calling_agent_id: str,
        max_retries: int,
        retry_backoff: float,
        memory_context: Optional[List[str]] = None,
    ) -> ToolResult:
        """Dispatch a single delegate_to_specialist call to the coordinator."""
        args = tool_call.args or {}
        intent = args.get("intent", "")
        query = args.get("query", "")
        context_params = args.get("context", {})

        # LLM may pass context as free-form string — wrap as reasoning
        if isinstance(context_params, str) and context_params:
            context_params = {"reasoning": context_params}
        elif not isinstance(context_params, dict):
            context_params = {}

        if not intent:
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM ERROR: delegate_to_specialist called without 'intent'. args={args}",
            )

        # Apply intent remap
        original_intent = intent
        intent = intent_remap.get(intent, intent)
        if intent != original_intent:
            logger.debug(
                "[DelegationEngine] Intent remap: %s → %s", original_intent, intent,
            )

        delegation_context: Dict[str, Any] = {
            **context,
            "memory_context": memory_context or [],
            "params": context_params,
        }

        for attempt in range(max_retries + 1):
            response = await self._coordinator.handle_delegation(
                intent=intent,
                query=query,
                context=delegation_context,
                calling_agent_id=calling_agent_id,
            )

            if response.status == AgentStatus.SUCCESS:
                result_str = _format_result(intent, response.result)
                logger.info(
                    "✅ [DelegationEngine] delegation result: intent=%s, %s chars",
                    intent, len(result_str),
                )
                file_data = response.metadata.get("file_data") if response.metadata else None
                return ToolResult(
                    name=tool_call.name,
                    result_str=result_str,
                    structured_data=response.metadata.get("structured_data") if response.metadata else None,
                    history_context=response.history_context,
                    delivery_items=response.delivery_items,
                    file_data=file_data,
                )

            # Failed — return immediately so LLM can self-correct
            logger.warning(
                "⚠️ [DelegationEngine] Delegation intent='%s' rejected: %s",
                intent, response.error,
            )
            return ToolResult(
                name=tool_call.name,
                result_str=(
                    f"SYSTEM: Specialist agent rejected the request. "
                    f"Error: {response.error} "
                    f"Correct your input and try again."
                ),
            )

        return ToolResult(
            name=tool_call.name,
            result_str="AGENT ERROR: Max retries exceeded",
        )
