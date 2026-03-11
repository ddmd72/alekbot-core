"""
Compute Agent
=============

Specialist agent for precise calculations via Gemini code_execution.
Receives computation tasks from orchestrators, writes Python code,
executes in Gemini sandbox, returns the result.

Does NOT know about intents or routing — just receives a query and computes.
If the task cannot be solved with Python code execution (e.g. requires live
data, network access, or is fundamentally non-computable), responds honestly
with an explanation of why it cannot be done.

Provider-agnostic: requests code execution via LLMRequest.use_code_execution=True.
GeminiAdapter injects types.Tool(code_execution=...) internally; other adapters ignore the flag.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..ports.llm_port import (
    AgentExecutionContext,
    LLMRequest,
    Message,
    MessagePart,
)
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..infrastructure.agent_config import COMPUTE


class ComputeAgent(BaseAgent):
    """
    Specialist agent for precise calculations via Python code execution.

    Single LLM call with use_code_execution=True per request. The provider
    (Gemini) generates Python code, runs it in a sandbox, and returns the
    result. Uses PromptBuilder for system prompt (agent_type="compute").
    Returns plain text result consumed by the orchestrator.
    """

    TEMPERATURE = COMPUTE.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self.execution_context = execution_context
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

        return await self._call_compute(message, query)

    # ------------------------------------------------------------------
    # Core: single LLM call with code_execution tool
    # ------------------------------------------------------------------

    async def _call_compute(
        self,
        message: AgentMessage,
        query: str,
    ) -> AgentResponse:
        self._on_agent_start(query)
        start_time = time.time()

        try:
            # Build system prompt via PromptBuilder
            system_prompt = ""
            if self.prompt_builder:
                try:
                    account_id = message.context.get("account_id")
                    system_prompt = await self.prompt_builder.build_for_agent(
                        agent_type="compute",
                        user_id=self.user_id,
                        account_id=account_id,
                        routing_metadata=None,
                        include_biographical=False,
                    )
                except Exception as exc:
                    logger.warning(
                        f"ComputeAgent: build_for_agent failed ({exc}), "
                        "proceeding with empty prompt"
                    )

            current_time_str = datetime.now(timezone.utc).strftime(
                "%A, %d %B %Y, %H:%M %Z"
            )
            user_text = (
                f"current_datetime: {current_time_str}\n\n"
                f"TASK: {query}"
            )

            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=user_text)])],
                use_code_execution=True,
                temperature=self.TEMPERATURE,
            )
            response = await self._call_llm(request)

            result_text = response.text or "Computation failed — no result."
            token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
            duration_ms = int((time.time() - start_time) * 1000)

            self._on_agent_success(len(result_text), token_count, output_text=result_text)

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
                confidence=1.0,
                metadata={
                    "duration_ms": duration_ms,
                    "model": self.model_name,
                },
            )

        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Computation failed: {str(e)}",
            )

    def _get_alternative_agents(self) -> list[str]:
        return ["web_search_agent"]
