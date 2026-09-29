"""
WebSearchLightAgent — one fast grounded lookup for the voice companion (VOICE_COMPANION_RFC §4.15.1).

A single provider-native grounded call (Gemini ECO + Google Search by default, ~2 s measured
2026-09-29) that returns a few spoken sentences: no Maps fan-out, no JSON, no refinement. Lelik
uses it for one fact that one search answers; anything needing several sources or judgment goes
to Alek. Resurrected from the agent deleted in 5bc5d8c (2026-05-29), when Quick's remap to it was
switched off and nothing reached it any more.

No biography and no standing directives; only the user's location reaches the prompt. The prompt
is the `websearch_light` profile: blueprint `websearch_light_agent_v1` over the four
`WEBSEARCH_LIGHT_*` tokens.
"""
import time
from datetime import datetime, timezone

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.delegation_timestamp import strip_delegation_timestamp
from ..domain.llm import LLMRequest, Message, MessagePart
from ..infrastructure.agent_config import WEB_SEARCH_LIGHT
from ..ports.llm_port import AgentExecutionContext
from ..ports.prompt_builder_port import PromptBuilderPort
from .base_agent import BaseAgent


class WebSearchLightAgent(BaseAgent):
    """search_web_light → one grounded LLM call → short plain-text answer."""

    TEMPERATURE = WEB_SEARCH_LIGHT.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        user_id: str,
    ):
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY and bool(message.payload.get("query"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error="No query provided in payload")
        self._on_agent_start(query)
        started = time.time()
        try:
            account_id = (message.context or {}).get("account_id")
            # A web lookup needs neither the user's biography nor their standing directives: the
            # biography leaks personal facts into a search model, the directives ("tables, emojis")
            # shaped the spoken answer into chat markup, and both cost latency (UAT 2026-09-29).
            # user_location still comes through — it is user config, not biography.
            system_instruction = await self.prompt_builder.build_for_agent(
                agent_type="websearch_light", user_id=self.user_id, account_id=account_id,
                routing_metadata=None, include_biographical=False, include_directives=False,
            )
        except Exception as exc:
            # No fallback prompt (CLAUDE.md): a missing prompt is a failure, not a degraded call.
            self._on_agent_error(exc)
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error=f"Web search prompt unavailable: {exc}")
        try:
            now = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=f"current_date_time: {now}\n\n{system_instruction}",
                # The date is in the system instruction; the query's own prefix would be a second one.
                messages=[Message(role="user", parts=[MessagePart(text=strip_delegation_timestamp(query))])],
                use_grounding=True,
                temperature=self.TEMPERATURE,
            )
            response = await self._call_llm(request)
            result_text = (response.text or "").strip() or "No relevant information found."
            self._on_agent_success(len(result_text), output_text=result_text)
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
                confidence=min(1.0, len(result_text) / 300),
                metadata={"total_duration_ms": int((time.time() - started) * 1000), "model": self.model_name},
            )
        except Exception as exc:
            self._on_agent_error(exc)
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error=f"Web search failed: {exc}")
