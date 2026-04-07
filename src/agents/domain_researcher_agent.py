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

Supports specialist delegation via DelegationEngine (search_memory, search_web,
open_file, etc.) — tools are available when coordinator is configured.
"""

import time
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import DOMAIN_RESEARCHER
from ..infrastructure.agent_manifest import DOMAIN_RESEARCHER as DOMAIN_RESEARCHER_DESCRIPTOR
from ..infrastructure.delegation_engine import DelegationEngine, DelegationContext
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger


class DomainResearcherAgent(BaseAgent):
    """
    Multi-turn conversational agent for domain competency research.

    Receives conversation history via message.context["history"] (from bound
    channel handler). Uses DelegationEngine for tool calling when coordinator
    is configured; falls back to single LLM call otherwise.
    """

    _descriptor = DOMAIN_RESEARCHER_DESCRIPTOR

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
                include_datetime=False,
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
                kwargs = {}
                if "created_at" in entry:
                    kwargs["created_at"] = entry["created_at"]
                messages.append(Message(role=role, parts=parts, **kwargs))

        # Append current user message with attachments (file_data etc.)
        current_parts = message.context.get("current_message_parts", [])
        if current_parts:
            messages.append(Message(role="user", parts=current_parts))
        else:
            messages.append(Message(role="user", parts=[MessagePart(text=query)]))

        # No _inject_timestamps — bound channel is real-time conversation,
        # and the LLM has no prompt token explaining the timestamp format.

        try:
            # Build tool declarations (empty if no coordinator/registry)
            tools = None
            if self.coordinator:
                available = self.coordinator.get_available_intents_for(self._descriptor)
                if available:
                    tools = [self._build_delegate_tool_declaration(available)]

            base_request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                tools=tools,
                temperature=self.TEMPERATURE,
                max_tokens=DOMAIN_RESEARCHER.max_tokens,
                thinking=DOMAIN_RESEARCHER.thinking_effort,
            )

            if tools and self.coordinator:
                # Multi-turn delegation loop
                engine = DelegationEngine(self.coordinator)
                result = await engine.execute(
                    call_llm=self._call_llm,
                    base_request=base_request,
                    context=DelegationContext(
                        user_id=self.user_id,
                        account_id=message.context.get("account_id"),
                        session_id=message.context.get("session_id"),
                    ),
                    max_turns=DOMAIN_RESEARCHER.max_delegation_turns,
                    calling_agent_id=self.agent_id,
                )
                if result.failed:
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        error="max_turns_exhausted",
                    )
                result_text = result.text or "No response from model."
            else:
                # Single LLM call (no tools available)
                response = await self._call_llm(base_request)
                result_text = response.text or "No response from model."

            token_count = 0
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
