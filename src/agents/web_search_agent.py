"""
Web Search Agent
================

Specialist agent for web search and URL fetching.

Two intents, routed through one agent instance:

  search_web   — grounded web search.
                 payload: {"query": "<natural language search query>"}

  fetch_url    — fetch a specific URL and return its content in detail.
                 payload: {"url": "<URL to fetch>"}

Routing: execute() dispatches on payload keys:
  url present  →  _handle_fetch_url
  query only   →  _handle_search_web

Sets use_grounding=True in LLMRequest — each adapter injects its own native
search tool (Gemini: Google Search, OpenAI: web_search,
Claude: web_search_20250305 + web_fetch_20250910).

No biographical context is injected — routing_metadata=None.
System instruction = cognitive process prompt (from PromptBuilder or fallback).
User message = raw query / URL.
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
from ..infrastructure.agent_config import WEB_SEARCH, ENABLE_GROUNDING_ATTRIBUTION


class WebSearchAgent(BaseAgent):
    """
    Agent responsible for web search queries and URL fetching.

    Capabilities:
    - search_web: Web search via provider-native grounding
    - fetch_url:  Fetch specific URL content via provider-native grounding
    """

    TEMPERATURE = WEB_SEARCH.temperature

    _FALLBACK_SEARCH_SYSTEM = (
        "class SearchAgent extends GoogleSearchAgent {\n"
        "  archetype: 'Meticulous Researcher. Loves exhaustive lists. Hates ambiguity.'\n\n"
        "  cognitive_process {\n"
        "    steps: [\n"
        "      '1. ANALYZE: Extract Object and Criteria from user_query.',\n"
        "      '2. EXECUTE: Perform grounding search using Google Search.',\n"
        "      '3. VERIFY: Check results against Criteria.',\n"
        "      '4. REFINE: If insufficient, refine search and retry.',\n"
        "      '5. COMPILE: Aggregate ALL non-contradictory results.',\n"
        "      '6. DELIVER: Present final list with summary.'\n"
        "    ]\n"
        "  }\n\n"
        "  output_format {\n"
        "    style: 'Slack mrkdwn (no headers, use *bold*)'\n"
        "    structure: 'List of Options -> Summary'\n"
        "  }\n"
        "}"
    )

    _FALLBACK_FETCH_SYSTEM = (
        "Fetch the provided URL and return its full content in detail. "
        "Return the complete page text without omissions. "
        "Slack mrkdwn only. No JSON. No code blocks."
    )

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None
    ):
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

        logger.info(f"🌐 WebSearchAgent initialized (model={self.model_name})")

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query") or message.payload.get("url"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        url = message.payload.get("url", "")
        query = message.payload.get("query", "")

        if url:
            return await self._handle_fetch_url(message, url)
        elif query:
            return await self._handle_search_web(message, query)
        else:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query or url provided in payload",
            )

    async def _handle_search_web(self, message: AgentMessage, query: str) -> AgentResponse:
        self._on_agent_start(query)
        start_time = time.time()

        try:
            current_time_str = datetime.now(timezone.utc).strftime('%A, %d %B %Y, %H:%M %Z')

            if self.prompt_builder:
                account_id = message.context.get("account_id") if message.context else None
                system_instruction = await self.prompt_builder.build_for_agent(
                    agent_type="websearch",
                    user_id=self.user_id,
                    account_id=account_id,
                    routing_metadata=None,
                )
            else:
                system_instruction = self._FALLBACK_SEARCH_SYSTEM

            system_instruction = f"current_date_time: {current_time_str}\n\n{system_instruction}"
            return await self._call_grounded_llm(message, system_instruction, query, start_time, context=query)

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Web search failed: {str(e)}",
            )

    async def _handle_fetch_url(self, message: AgentMessage, url: str) -> AgentResponse:
        self._on_agent_start(url)
        start_time = time.time()

        try:
            query = message.payload.get("query", "")
            user_content = f"{query}\n\n{url}" if query else url
            return await self._call_grounded_llm(
                message, self._FALLBACK_FETCH_SYSTEM, user_content, start_time, context=url
            )

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"URL fetch failed: {str(e)}",
            )

    async def _call_grounded_llm(
        self,
        message: AgentMessage,
        system_instruction: Optional[str],
        user_content: str,
        start_time: float,
        context: str,
    ) -> AgentResponse:
        """Shared grounded LLM call + response packaging for both intents."""
        logger.debug("   → Calling LLM with grounding...")
        llm_start = time.time()

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_instruction,
            messages=[Message(role="user", parts=[MessagePart(text=user_content)])],
            use_grounding=True,
            temperature=self.TEMPERATURE,
        )
        response = await self._call_llm(request)

        llm_duration = time.time() - llm_start
        logger.debug(f"   ✓ LLM responded in {llm_duration:.2f}s")

        result_text = response.text

        rendered_content = None
        if response.grounding_metadata:
            sep = getattr(response.grounding_metadata, "search_entry_point", None)
            if sep:
                rendered_content = getattr(sep, "rendered_content", None)

        if not result_text or result_text == "No relevant information found on the web.":
            logger.warning(f"⚠️ [WebSearchAgent] No results for: '{context}'")
            return AgentResponse(
                task_id=message.task_id,
                agent_id=self.agent_id,
                status="partial",
                result=result_text or "No relevant information found on the web.",
                confidence=0.0,
                metadata={
                    "total_duration_ms": int((time.time() - start_time) * 1000),
                    "llm_duration_ms": int(llm_duration * 1000),
                },
            )

        total_duration = time.time() - start_time
        self._on_agent_success(len(result_text), output_text=result_text)
        confidence = min(1.0, len(result_text) / 500) if result_text else 0.0

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

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result_text,
            confidence=confidence,
            metadata={
                "total_duration_ms": int(total_duration * 1000),
                "llm_duration_ms": int(llm_duration * 1000),
                "result_length": len(result_text),
                "model": self.model_name,
            },
            delivery_items=delivery_items,
            history_context={"web_search_context": {"query": context, "result": result_text}},
        )

    def _get_alternative_agents(self) -> list[str]:
        return ["memory_search_agent", "reasoning_agent"]
