"""
Maps Search Agent
=================

Specialist agent for location-aware queries via Google Maps AI Grounding Lite (MCP).

Single intent — natural language passthrough:

  maps_query  — any location-based request in natural language.
                payload: {"query": "Find a pharmacy near Khreschatyk open now"}

The orchestrating LLM (Quick/Smart) passes the full natural language task directly.
The agent runs a multi-turn tool loop: LLM selects which MCP tool(s) to call,
agent executes them against the MCP server, LLM formats the final response.

System prompt is assembled via PromptBuilderPort from Firestore profile "maps_search".
Tokens: MAPS_PROPERTIES, MAPS_COGNITIVE_PROCESS, MAPS_OUTPUT_FORMAT.
Biographical facts are injected automatically (include_biographical=True).

Backend is injected via MapsToolsPort. Swapping adapters (e.g., back to Gemini native)
requires no changes to this file — only user_agent_factory.py + agent_manifest.py.

RFC: docs/10_rfcs/MCP_INFRASTRUCTURE_RFC.md
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..infrastructure.agent_config import MAPS_SEARCH
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.maps_tools_port import MapsToolError, MapsToolsPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

_MAX_TURNS = 10


class MapsSearchAgent(BaseAgent):
    """
    Specialist agent for location-aware queries via MCP-backed Maps tools.

    Runs a multi-turn LLM ↔ MCP tool loop. The LLM decides which tool(s)
    to call based on the available tool declarations. The agent executes the
    calls and returns results to the LLM for formatting.

    System prompt is assembled from Firestore profile "maps_search" via PromptBuilderPort,
    including biographical facts. Falls back to empty instruction on assembly failure.
    """

    TEMPERATURE = MAPS_SEARCH.temperature
    THINKING = MAPS_SEARCH.thinking

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        maps_port: MapsToolsPort,
        prompt_builder: PromptBuilderPort,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._maps_port = maps_port
        self._prompt_builder = prompt_builder
        self._account_id = account_id
        self.user_id = user_id

        logger.info(f"🗺️ MapsSearchAgent initialized (model={self.model_name})")

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )
        return await self._run_tool_loop(message, query)

    async def _build_system_instruction(self) -> str:
        try:
            return await self._prompt_builder.build_for_agent(
                agent_type="maps_search",
                account_id=self._account_id,
                user_id=self.user_id,
                include_biographical=True,
            )
        except Exception as exc:
            logger.warning(f"🗺️ [MapsSearchAgent] prompt build failed ({exc}), using empty instruction")
            return ""

    async def _run_tool_loop(self, message: AgentMessage, query: str) -> AgentResponse:
        """
        Multi-turn LLM ↔ MCP tool loop.

        1. Assemble system prompt with biographical facts via PromptBuilderPort.
        2. Fetch tool declarations from MapsToolsPort.
        3. LLM call with tools → model selects tool(s).
        4. Execute each tool call against MCP server.
        5. Append model tool_call message + tool_response message to history.
        6. Repeat until LLM produces a text response (no tool_calls) or max turns.
        """
        self._on_agent_start(query)
        start_time = time.time()

        try:
            system_instruction, tool_declarations = await asyncio.gather(
                self._build_system_instruction(),
                self._maps_port.get_tool_declarations(),
            )

            current_time = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")
            messages: list[Message] = [
                Message(
                    role="user",
                    parts=[MessagePart(text=f"current_date_time: {current_time}\n\n{query}")],
                )
            ]

            final_text = ""

            for turn in range(_MAX_TURNS):
                request = LLMRequest(
                    model_name=self.model_name,
                    system_instruction=system_instruction,
                    messages=messages,
                    tools=tool_declarations,
                    temperature=self.TEMPERATURE,
                    thinking=self.THINKING,
                )
                response = await self._call_llm(request, turn=turn + 1)

                if not response.tool_calls:
                    final_text = response.text or ""
                    break

                # Build model message. raw_content preserves Gemini thought_signatures intact;
                # parts carry tool_calls for non-Gemini adapters.
                messages.append(Message(
                    role="model",
                    raw_content=response.raw_content,
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
                ))

                # Execute tool calls. Multiple calls in a single turn (e.g. several
                # weather lookups for different dates) are independent — run them
                # concurrently. Only cross-turn order is serial (the next LLM turn
                # depends on these results). gather preserves input order, so each
                # tool_response lines up with its originating tool_call.
                tool_result_parts = list(await asyncio.gather(
                    *[self._execute_tool_call(tc) for tc in response.tool_calls]
                ))
                messages.append(Message(role="user", parts=tool_result_parts))

            else:
                # Max turns exhausted — ask LLM to format with what it has
                logger.warning(
                    f"🗺️ [MapsSearchAgent] max turns ({_MAX_TURNS}) reached, forcing format"
                )
                request = LLMRequest(
                    model_name=self.model_name,
                    system_instruction=system_instruction,
                    messages=messages + [
                        Message(
                            role="user",
                            parts=[MessagePart(text="Please summarize the results above.")],
                        )
                    ],
                    temperature=self.TEMPERATURE,
                    thinking=self.THINKING,
                )
                response = await self._call_llm(request, turn=_MAX_TURNS + 1)
                final_text = response.text or ""

            duration_ms = int((time.time() - start_time) * 1000)

            if not final_text:
                logger.warning(f"⚠️ [MapsSearchAgent] No results for: '{query[:80]}'")
                return AgentResponse(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    status="partial",
                    result={"text": "No map results found for this query."},
                    confidence=0.0,
                    metadata={"duration_ms": duration_ms},
                )

            self._on_agent_success(len(final_text), output_text=final_text)

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={"text": final_text},
                confidence=min(1.0, len(final_text) / 300),
                metadata={"duration_ms": duration_ms, "model": self.model_name},
            )

        except Exception as exc:
            self._on_agent_error(exc)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Maps search failed: {exc}",
            )

    async def _execute_tool_call(self, tool_call) -> MessagePart:
        """Execute one MCP tool call → tool_response MessagePart.

        Errors are captured per call ({"error": ...}) so a single failing tool in a
        concurrent batch never fails the whole turn. Safe to run concurrently: the MCP
        client uses an independent HTTP session per call.
        """
        logger.info(
            f"🗺️ [MapsSearchAgent] tool_call: {tool_call.name} "
            f"args={json.dumps(tool_call.args, ensure_ascii=False)[:200]}"
        )
        try:
            result = await self._maps_port.call_tool(tool_call.name, tool_call.args)
        except MapsToolError as exc:
            logger.warning(f"🗺️ [MapsSearchAgent] tool '{tool_call.name}' failed: {exc}")
            result = {"error": str(exc)}
        return MessagePart(tool_response={"name": tool_call.name, "response": result})

    def _get_alternative_agents(self) -> list[str]:
        return ["web_search_agent", "facts_memory_agent"]
