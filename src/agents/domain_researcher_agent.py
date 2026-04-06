"""
Domain Researcher Agent
=======================

Interactive multi-turn agent for defining professional domain competency stacks.
Designed for bound channel use — receives conversation history from platform API
(not SessionStore) and maintains multi-turn dialogue without system-level persistence.

Decomposes a domain into sub-domains, identifies 15-20 critical competencies,
classifies each as KNOWLEDGE/ALGORITHM/CONSTRAINT/STYLE, scores by importance,
and produces a structured Domain Manifest for agent construction.

The agent mirrors the user's language for all interactions.
"""

import time
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.llm import Message, MessagePart
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..infrastructure.agent_config import DOMAIN_RESEARCHER


class DomainResearcherAgent(BaseAgent):
    """
    Multi-turn conversational agent for domain competency research.

    Receives conversation history via message.context["history"] (from bound
    channel handler). Each invocation = one LLM call with full history context.
    System prompt loaded via PromptBuilder (agent_type="domain_researcher").
    """

    TEMPERATURE = DOMAIN_RESEARCHER.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

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
        return await self._call_research(message, query)

    async def _call_research(
        self, message: AgentMessage, query: str,
    ) -> AgentResponse:
        self._on_agent_start(query)
        start_time = time.time()

        # Build system prompt via PromptBuilder (mandatory, no fallback)
        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="domain_researcher",
                user_id=self.user_id,
                account_id=message.context.get("account_id"),
                routing_metadata=None,
                include_biographical=False,
            )
        except Exception as exc:
            self._on_agent_error(exc, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"PromptBuilder failed: {exc}",
            )

        # Build conversation history from bound channel context
        history_data = message.context.get("history", [])
        messages = []
        for entry in history_data:
            role = entry.get("role", "user")
            parts_data = entry.get("parts", [])
            parts = [MessagePart(text=p.get("text", "")) for p in parts_data if p.get("text")]
            if parts:
                messages.append(Message(role=role, parts=parts))

        # Append current user message
        messages.append(Message(role="user", parts=[MessagePart(text=query)]))

        try:
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                temperature=self.TEMPERATURE,
                max_tokens=DOMAIN_RESEARCHER.max_tokens,
            )
            response = await self._call_llm(request)

            result_text = response.text or "No response from model."
            token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
            self._on_agent_success(len(result_text), token_count, output_text=result_text)

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
            )
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(e),
            )

    def _get_alternative_agents(self) -> list[str]:
        return []
