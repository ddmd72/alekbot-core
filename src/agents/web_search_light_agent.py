"""
Web Search Light Agent
======================

Lightweight single-pass web search agent for use by QuickResponseAgent.
Returns plain Slack mrkdwn — no multi-step refinement, no JSON output.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent
from ..ports.llm_service import AgentExecutionContext
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.llm_service import Message, MessagePart, LLMRequest
from ..utils.logger import logger


class WebSearchLightAgent(BaseAgent):
    """
    Lightweight web search agent called as a tool by QuickResponseAgent.

    Single grounded Gemini call → plain Slack mrkdwn result.
    Uses PromptBuilder for system prompt (agent_type="websearch_light").
    """

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        grounding_tool: object,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._grounding_tool = grounding_tool
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

        logger.info(f"🔦 [WebSearchLightAgent] Query: '{query[:60]}'")
        start_time = time.time()

        try:
            current_time_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M %Z")

            # Build prompt via PromptBuilder v3 (agent_type="websearch_light")
            if self.prompt_builder:
                account_id = message.context.get("account_id") if message.context else None
                prompt = await self.prompt_builder.build_for_agent(
                    agent_type="websearch_light",
                    user_id=self.user_id,
                    account_id=account_id,
                    routing_metadata=None,
                )
                augmented_query = (
                    f"// Context Injection\n"
                    f"current_date = '{current_time_str}'\n"
                    f"user_query = '{query}'\n\n"
                    f"{prompt}\n\n"
                    "// Execute\n"
                    "WebSearchLightAgent.run(user_query)"
                )
            else:
                augmented_query = (
                    f"// Context Injection\n"
                    f"current_date = '{current_time_str}'\n"
                    f"user_query = '{query}'\n\n"
                    "cognitive_process {\n"
                    "    instruction: \"Answer the query with a single grounded web search. "
                    "Return only the answer — no preamble, no meta-commentary.\"\n"
                    "    rules: [\n"
                    "        \"Single pass only. No source attribution prose.\",\n"
                    "        \"Structured data → bullet list or table. Single-fact → plain prose.\",\n"
                    "        \"Slack mrkdwn only. No JSON. No code blocks.\"\n"
                    "    ]\n"
                    "}\n\n"
                    "// Execute\n"
                    "WebSearchLightAgent.run(user_query)"
                )

            request = LLMRequest(
                model_name=self.model_name,
                system_instruction="",
                messages=[Message(role="user", parts=[MessagePart(text=augmented_query)])],
                tools=[self._grounding_tool],
                temperature=0.5,
            )
            response = await self._llm.generate_content(request=request)

            result_text = response.text or "No relevant information found."
            total_duration = time.time() - start_time

            logger.info(
                f"✅ [WebSearchLightAgent] Done in {total_duration:.2f}s "
                f"({len(result_text)} chars)"
            )

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
            )

        except Exception as e:
            logger.error(f"❌ [WebSearchLightAgent] Error: {e}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Web search failed: {str(e)}",
            )

    def _get_alternative_agents(self) -> list[str]:
        return ["memory_search_agent"]
