"""
DocPlannerAgent
===============

Specialist agent that creates professional DOCX documents from natural language requests.

Pipeline (single intent, two phases):
  1. LLM call (PERFORMANCE tier, Claude default) — produces a JSON layout spec
     using the Doc Planner system prompt.
  2. Enqueues DocGeneratorAgent as a separate ASYNC Cloud Task (fire and forget)
     via coordinator.handle_delegation(GENERATE_DOCX_CODE, raw_spec_text, ...).
     Returns AgentResponse.success immediately — does not wait for generation.

The raw LLM output (JSON string) is forwarded to DocGeneratorAgent as-is.
DocGeneratorAgent writes a Node.js script using the docx npm library and executes it.
DOCX delivery is handled by AgentWorkerHandler when the generator Cloud Task completes.
AgentWorkerHandler calls notification_service.notify_file_bytes() to deliver the file.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import DOC_PLANNER
from ..infrastructure.agent_manifest import Intent
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..infrastructure.agent_coordinator import AgentCoordinator


class DocPlannerAgent(BaseAgent):
    """
    Specialist agent for document creation.

    Accepts a natural-language query, generates a structured JSON layout spec
    via LLM (phase 1), then delegates DOCX generation to DocGeneratorAgent
    via coordinator (phase 2).
    Returns the file as a DeliveryItem for Cloud Task delivery.
    """

    TEMPERATURE = DOC_PLANNER.temperature
    MAX_TOKENS = DOC_PLANNER.max_tokens
    THINKING_EFFORT = DOC_PLANNER.thinking_effort


    # Enforces top-level JSON structure on Gemini (routed to response_json_schema).
    # ClaudeAdapter silently ignores this when there are no delegation tools — Claude
    # relies on the OUTPUT_FORMAT token in the system prompt instead.
    # doc_spec declared as flat object — Gemini has a hard nesting depth limit.
    _RESPONSE_SCHEMA = {
        "type": "object",
        "required": ["status", "task_summary", "doc_spec"],
        "properties": {
            "status":       {"type": "string"},
            "task_summary": {"type": "string"},
            "doc_spec":     {"type": "object"},
        },
    }

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        coordinator: AgentCoordinator,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._coordinator = coordinator
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        # Accepts DELEGATE (AgentWorkerHandler Cloud Task) and QUERY (direct routing / tests).
        return (
            message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE)
            and bool(message.payload.get("query", ""))
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            self._on_agent_error(ValueError("No query provided"), "empty_query")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )

        # Append any extra string fields from the payload (e.g. report_content spread by
        # AgentWorkerHandler from context["params"]) — skip "query" and "intent" which are
        # routing metadata, not content. Non-string values are silently ignored.
        _excluded = {"query", "intent"}
        for key, val in message.payload.items():
            if key not in _excluded and isinstance(val, str) and val:
                query = f"{query}\n\n{val}"

        self._on_agent_start(query)
        start_time = time.time()
        account_id = message.context.get("account_id")

        try:
            system_prompt = await self._build_system_prompt(account_id)
        except Exception as exc:
            self._on_agent_error(exc, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Failed to build system prompt: {exc}",
            )

        messages = [
            Message(role="user", parts=[MessagePart(text=query)])
        ]

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=messages,
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            response_mime_type="application/json",
            response_schema=self._RESPONSE_SCHEMA,
            thinking=self.THINKING_EFFORT or None,
        )
        response = await self._call_llm(request, turn=0)
        raw = (response.text or "").strip()

        # Enqueue generator as a separate async Cloud Task — fire and forget.
        # Generator delivers the DOCX file directly via AgentWorkerHandler.
        await self._coordinator.handle_delegation(
            intent=Intent.GENERATE_DOCX_CODE,
            query=raw,
            context=message.context,
            calling_agent_id=self.agent_id,
        )

        token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
        duration_ms = int((time.time() - start_time) * 1000)
        self._on_agent_success(0, token_count, output_text="Document spec ready, generation started")
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="Document spec ready, generation started",
            confidence=1.0,
            metadata={"duration_ms": duration_ms, "model": self.model_name},
        )

    async def _build_system_prompt(self, account_id: Optional[str]) -> str:
        return await self.prompt_builder.build_for_agent(
            agent_type="doc_planner",
            user_id=self.user_id,
            account_id=account_id,
            routing_metadata=None,
            include_biographical=False,
        )

    def _get_alternative_agents(self) -> list[str]:
        return []
