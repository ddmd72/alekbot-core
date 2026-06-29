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
from ..ports.llm_port import LLMRequest, LLMResponse, Message, ToolCall
from ..domain.llm import build_tool_turn
from ..utils.logger import logger
from ..utils.telemetry import start_span

from .agent_registry import FanoutSpec

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

_FANOUT_LABELS: Dict[str, str] = {
    "search_web": "Web Search",
    "maps_query": "Maps",
}


def _fanout_label(intent: str, primary: bool = False) -> str:
    """Human-readable label for fan-out result sections."""
    name = _FANOUT_LABELS.get(intent, intent)
    return f"Primary specialist: {name}" if primary else f"Additional specialist: {name}"


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
        intent_fanout: Optional[Dict[str, FanoutSpec]] = None,
        calling_agent_id: str = "delegation_engine",
        max_retries: int = 1,
        retry_backoff: float = 1.0,
    ) -> DelegationResult:
        """Run the delegation loop, wrapped in a ``delegation.loop`` tracing span.

        Thin instrumentation wrapper — the validated loop body lives untouched in
        ``_execute_loop`` so the span context is active across every turn. Each
        turn's ``llm.call`` and each specialist ``delegation`` span nest under it.
        """
        with start_span("delegation.loop", {
            "delegation.agent_id": calling_agent_id,
            "delegation.max_turns": max_turns,
            "delegation.terminal_tool": terminal_tool or "none",
        }):
            return await self._execute_loop(
                call_llm=call_llm,
                base_request=base_request,
                context=context,
                max_turns=max_turns,
                terminal_tool=terminal_tool,
                intent_remap=intent_remap,
                intent_fanout=intent_fanout,
                calling_agent_id=calling_agent_id,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )

    async def _execute_loop(
        self,
        call_llm: Callable[[LLMRequest, int], Awaitable[LLMResponse]],
        base_request: LLMRequest,
        context: Dict[str, Any],
        max_turns: int,
        terminal_tool: Optional[str] = None,
        intent_remap: Optional[Dict[str, str]] = None,
        intent_fanout: Optional[Dict[str, FanoutSpec]] = None,
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
            intent_remap: Optional dispatch-time intent substitution.
            intent_fanout: Optional dispatch-time 1:N expansion.
                           e.g. {"search_web": FanoutSpec(intents=["maps_query"],
                           hint="...")} dispatches both in parallel and merges
                           results into one tool response with the hint.
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
        fanout = intent_fanout or {}

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
                intent_fanout=fanout,
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
            # Guard: tool_calls and tool_results must be 1:1. zip() would silently
            # truncate on a skew, leaving a tool_use with no tool_result (or vice
            # versa) — exactly the shape Anthropic rejects with a 400 on the next
            # turn. Surface it loudly instead of corrupting history silently.
            if len(tool_results) != len(response.tool_calls):
                logger.error(
                    "❌ [DelegationEngine] Turn %s — tool_call/tool_result count "
                    "mismatch: %s calls vs %s results (caller=%s). History tool "
                    "turn will be skewed; downstream Claude request may 400.",
                    turn + 1, len(response.tool_calls), len(tool_results),
                    calling_agent_id,
                )
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
    # Tool execution — uniform parallel                                   #
    # ------------------------------------------------------------------ #

    async def _execute_tool_calls(
        self,
        tool_calls: List[ToolCall],
        context: Dict[str, Any],
        intent_remap: Dict[str, str],
        intent_fanout: Dict[str, FanoutSpec],
        calling_agent_id: str,
        max_retries: int,
        retry_backoff: float,
    ) -> List[ToolResult]:
        """Dispatch all tool calls in one parallel batch via asyncio.gather.

        Per-turn ordering does not affect downstream LLM behavior — the LLM
        commits tool-call parameters atomically per turn, and no specialist
        agent consumes intra-turn cross-tool results.
        """
        if not tool_calls:
            return []

        logger.info(
            "⚡ [DelegationEngine] Parallel execution: %s call(s)",
            len(tool_calls),
        )
        tasks = [
            self._dispatch_single(
                tc, context, intent_remap, intent_fanout, calling_agent_id,
                max_retries, retry_backoff,
            )
            for tc in tool_calls
        ]
        parallel_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[ToolResult] = []
        for tc, result in zip(tool_calls, parallel_results):
            if isinstance(result, Exception):
                logger.error("❌ [DelegationEngine] Tool call failed: %s", result)
                results.append(ToolResult(
                    name=tc.name,
                    result_str=f"AGENT ERROR: {result}",
                ))
            else:
                results.append(result)
        return results

    # ------------------------------------------------------------------ #
    # Single tool dispatch                                                #
    # ------------------------------------------------------------------ #

    async def _dispatch_single(
        self,
        tool_call: ToolCall,
        context: Dict[str, Any],
        intent_remap: Dict[str, str],
        intent_fanout: Dict[str, FanoutSpec],
        calling_agent_id: str,
        max_retries: int,
        retry_backoff: float,
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
            "params": context_params,
        }

        # Fan-out: dispatch primary + secondary intents in parallel
        fanout_spec = intent_fanout.get(intent)
        if fanout_spec and fanout_spec.intents:
            return await self._dispatch_with_fanout(
                tool_call, intent, fanout_spec, query,
                delegation_context, calling_agent_id,
            )

        return await self._dispatch_to_coordinator(
            tool_call, intent, query, delegation_context,
            calling_agent_id, max_retries,
        )

    async def _dispatch_to_coordinator(
        self,
        tool_call: ToolCall,
        intent: str,
        query: str,
        delegation_context: Dict[str, Any],
        calling_agent_id: str,
        max_retries: int,
    ) -> ToolResult:
        """Dispatch a single intent to the coordinator and return a ToolResult."""
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

    # ------------------------------------------------------------------ #
    # Fan-out dispatch                                                     #
    # ------------------------------------------------------------------ #

    async def _dispatch_with_fanout(
        self,
        tool_call: ToolCall,
        primary_intent: str,
        spec: FanoutSpec,
        query: str,
        delegation_context: Dict[str, Any],
        calling_agent_id: str,
    ) -> ToolResult:
        """Dispatch primary + secondary intents in parallel, merge results."""
        logger.info(
            "⚡ [DelegationEngine] Fan-out: %s + %s",
            primary_intent, spec.intents,
        )

        async def _delegate(intent: str):
            return await self._coordinator.handle_delegation(
                intent=intent,
                query=query,
                context=delegation_context,
                calling_agent_id=calling_agent_id,
            )

        all_intents = [primary_intent, *spec.intents]
        results = await asyncio.gather(
            *[_delegate(i) for i in all_intents],
            return_exceptions=True,
        )

        return self._merge_fanout_results(tool_call, all_intents, results, spec.hint)

    def _merge_fanout_results(
        self,
        tool_call: ToolCall,
        intents: List[str],
        responses: List[Any],
        hint: str = "",
    ) -> ToolResult:
        """Merge parallel fan-out responses into a single ToolResult.

        First intent is primary (errors surfaced to LLM).
        Subsequent intents are secondary (failures silently skipped).
        """
        preamble = "SYSTEM: This query was automatically dispatched to multiple specialists in parallel."
        if hint:
            preamble += f"\n{hint}"
        sections: List[str] = [preamble]

        all_delivery_items: List[DeliveryItem] = []
        merged_history_context: Dict[str, Any] = {}
        structured_data = None

        for idx, (intent, response) in enumerate(zip(intents, responses)):
            is_primary = idx == 0
            label = _fanout_label(intent, primary=is_primary)

            if isinstance(response, Exception):
                logger.warning(
                    "[DelegationEngine] Fan-out '%s' failed: %s", intent, response,
                )
                if is_primary:
                    sections.append(f"[{label}]\nAGENT ERROR: {response}")
                continue

            if response.status != AgentStatus.SUCCESS:
                logger.warning(
                    "[DelegationEngine] Fan-out '%s' rejected: %s",
                    intent, response.error,
                )
                if is_primary:
                    sections.append(
                        f"[{label}]\nSYSTEM: Specialist rejected: {response.error}"
                    )
                continue

            result_text = _format_result(intent, response.result)
            if result_text:
                sections.append(f"[{label}]\n{result_text}")
            all_delivery_items.extend(response.delivery_items)
            if response.history_context:
                merged_history_context.update(response.history_context)
            if is_primary and response.metadata:
                structured_data = response.metadata.get("structured_data")

        combined_text = "\n\n".join(sections)
        logger.info(
            "✅ [DelegationEngine] Fan-out merged: %s sections, %s chars",
            len(sections), len(combined_text),
        )

        return ToolResult(
            name=tool_call.name,
            result_str=combined_text,
            structured_data=structured_data,
            history_context=merged_history_context or None,
            delivery_items=all_delivery_items,
        )
