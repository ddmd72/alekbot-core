"""
PdfPlannerAgent
===============

Specialist agent that creates professional PDF documents from natural language requests.

Pipeline (single intent, two phases):
  1. LLM call (PERFORMANCE tier, Claude default) — produces a JSON layout spec
     with CSS units (mm/pt) and a short filename.
  2. Enqueues PdfGeneratorAgent as a separate ASYNC Cloud Task (fire and forget)
     via coordinator.handle_delegation(GENERATE_PDF_CODE). Returns success immediately.

PdfGeneratorAgent renders HTML → PDF and delivers both HTML and PDF as
"document" DeliveryItems. AgentWorkerHandler stores each to GCS and sends
named links to the user.

See docs/10_rfcs/DOCUMENT_DELIVERY_RFC.md.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import PDF_PLANNER
from ..infrastructure.agent_manifest import Intent
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..infrastructure.agent_coordinator import AgentCoordinator


class PdfPlannerAgent(BaseAgent):
    """
    Specialist agent for PDF document creation.

    Accepts a natural-language query, generates a structured JSON layout spec
    via LLM (CSS units, filename field), then enqueues PdfGeneratorAgent as a
    separate ASYNC Cloud Task (fire and forget).
    """

    TEMPERATURE = PDF_PLANNER.temperature
    MAX_TOKENS = PDF_PLANNER.max_tokens
    THINKING_EFFORT = PDF_PLANNER.thinking_effort

    # Enforces top-level JSON structure on Gemini. ClaudeAdapter ignores this.
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
        coordinator: "AgentCoordinator",
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
        return (
            message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE)
            and bool(message.payload.get("query", ""))
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        extra_content = [
            v for k, v in message.payload.items()
            if k not in ("query", "intent") and isinstance(v, str) and v
        ]
        if extra_content:
            query = query + "\n\n---\n\n" + "\n\n---\n\n".join(extra_content)
        if not query:
            self._on_agent_error(ValueError("No query provided"), "empty_query")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )

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

        messages = [Message(role="user", parts=[MessagePart(text=query)])]
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
        llm_response = await self._call_llm(request, turn=0)
        raw = (llm_response.text or "").strip()

        # Phase 2: Enqueue PdfGenerator as async Cloud Task — fire and forget.
        await self._coordinator.handle_delegation(
            intent=Intent.GENERATE_PDF_CODE,
            query=raw,
            context=message.context,
            calling_agent_id=self.agent_id,
        )

        token_count = llm_response.usage_metadata.total_tokens if llm_response.usage_metadata else 0
        duration_ms = int((time.time() - start_time) * 1000)
        self._on_agent_success(0, token_count, output_text="PDF spec ready, generation started")
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="PDF spec ready, generation started",
            confidence=1.0,
            metadata={"duration_ms": duration_ms, "model": self.model_name},
        )

    async def _build_system_prompt(self, account_id: Optional[str]) -> str:
        return await self.prompt_builder.build_for_agent(
            agent_type="doc_planner_pdf",
            user_id=self.user_id,
            account_id=account_id,
            routing_metadata=None,
            include_biographical=False,
        )

    def _get_alternative_agents(self) -> list[str]:
        return []
