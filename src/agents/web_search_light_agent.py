"""
Web Search Light Agent
======================

Lightweight single-pass web search agent for use by QuickResponseAgent.
Returns formatted text — no multi-step refinement, no JSON output.

No biographical context is injected — routing_metadata=None.
System instruction = cognitive process prompt (from PromptBuilder).
User message = raw query.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, DeliveryItem
from ..ports.llm_port import AgentExecutionContext
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.llm_port import Message, MessagePart, LLMRequest
from ..utils.logger import logger
from ..infrastructure.agent_config import WEB_SEARCH_LIGHT, ENABLE_GROUNDING_ATTRIBUTION


class WebSearchLightAgent(BaseAgent):
    """
    Lightweight web search agent called as a tool by QuickResponseAgent.

    Single provider-native grounded call → formatted text result.
    Sets use_grounding=True in LLMRequest — each adapter injects its own native search tool.
    Uses PromptBuilder for system prompt (agent_type="websearch_light").
    No biographical context — routing_metadata=None.
    """

    TEMPERATURE = WEB_SEARCH_LIGHT.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

        logger.info(f"🔦 WebSearchLightAgent initialized (model={self.model_name})")

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

        self._on_agent_start(query)
        start_time = time.time()

        try:
            current_time_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")

            account_id = message.context.get("account_id") if message.context else None
            system_instruction = await self.prompt_builder.build_for_agent(
                agent_type="websearch_light",
                user_id=self.user_id,
                account_id=account_id,
                routing_metadata=None,
            )

            system_instruction = f"current_date_time: {current_time_str}\n\n{system_instruction}"
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_instruction,
                messages=[Message(role="user", parts=[MessagePart(text=query)])],
                use_grounding=True,
                temperature=self.TEMPERATURE,
            )
            response = await self._call_llm(request)

            result_text = response.text or "No relevant information found."
            total_duration = time.time() - start_time

            self._on_agent_success(len(result_text), output_text=result_text)

            rendered_content = None
            if response.grounding_metadata:
                sep = getattr(response.grounding_metadata, "search_entry_point", None)
                if sep:
                    rendered_content = getattr(sep, "rendered_content", None)

            delivery_items = []
            # =====================================================================
            # GROUNDING ATTRIBUTION — DISABLED BY DEFAULT, ENABLE IN PROD (MULTI-USER)
            # =====================================================================
            # What: Google's Terms of Service for Grounding API require showing a
            # search attribution widget to the end user whenever grounded content
            # is displayed. `rendered_content` is that widget — a small HTML chip
            # with 4 clickable search-query links (no actual result content).
            #
            # Why disabled by default: solo-dev usage — the chip adds zero value
            # for a single user who already sees the full answer in Slack.
            #
            # ENABLE before going multi-user:
            #   ENABLE_GROUNDING_ATTRIBUTION=true  (in .env / Secret Manager)
            # =====================================================================
            if rendered_content and ENABLE_GROUNDING_ATTRIBUTION:
                delivery_items.append(DeliveryItem(
                    type="html_gcs_link",
                    data={
                        "html": rendered_content,
                        "filename": "grounding_attribution.html",
                        "link_text": "🔍 Google Search Details",
                    },
                ))

            confidence = min(1.0, len(result_text) / 300) if result_text else 0.0
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
                confidence=confidence,
                metadata={
                    "total_duration_ms": int(total_duration * 1000),
                    "result_length": len(result_text),
                    "model": self.model_name,
                },
                delivery_items=delivery_items,
                history_context={"web_search_context": {"query": query, "result": result_text}},
            )

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Web search failed: {str(e)}",
            )

    def _get_alternative_agents(self) -> list[str]:
        return ["facts_memory_agent"]
