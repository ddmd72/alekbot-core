"""
LelikSummarizerAgent — the voice companion's (Lelik's) extractor
(RFC docs/10_rfcs/VOICE_COMPANION_RFC.md §4.9). Same architectural slot
TutorExtractorAgent fills for the tutor companion and ConsolidationAgent
fills for Alek: constructed fresh per call by
composition/companion_extractor_runner.py (services/ cannot import agents/
directly — REQ-ARCH-22), never cached or registered with the coordinator,
not manifest-registered (never reached via delegate_to_specialist).

The one thing the summarizer must not be is the participant it summarizes —
this always runs as a separate cheap-tier model, never Lelik itself (RFC
§4.9). Unlike TutorExtractorAgent, this agent's LLM call produces plain text,
not structured JSON: a call summary has no records to extract (Lelik has no
CompanionRecord store per RFC §4.9, so "records" is always an empty list,
fixed in code — never asked of the LLM, never parsed from it). Single LLM
call, no multi-turn tool loop, no dedup-search-before-write.
"""
import json

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.companion_extraction import EXTRACTION_TASK
from ..infrastructure.agent_config import VOICE_SUMMARIZER
from ..ports.llm_port import LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from .base_agent import BaseAgent


class LelikSummarizerAgent(BaseAgent):
    MAX_TOKENS = VOICE_SUMMARIZER.max_tokens
    TEMPERATURE = VOICE_SUMMARIZER.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context,
        prompt_builder: PromptBuilderPort,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.payload.get("task") != EXTRACTION_TASK:
            return False
        return bool(message.payload.get("messages"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        messages = message.payload.get("messages", [])
        if not messages:
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error="No messages provided in payload",
            )
        return await self._summarize(message, messages)

    async def _summarize(self, message: AgentMessage, messages: list) -> AgentResponse:
        self._on_agent_start(f"{len(messages)} turns")

        try:
            # Identity loads the user's overrides - the summary lands in their chat, so it
            # follows their output-language setting, not the language of the call.
            system_prompt = await self.prompt_builder.build_for_agent(
                "lelik_summarizer",
                user_id=message.context.get("user_id"),
                account_id=message.context.get("account_id"),
                include_biographical=False,
                include_directives=False,
            )
        except Exception as e:
            self._on_agent_error(e, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"PromptBuilder failed: {e}",
            )

        batch_text = json.dumps(messages, ensure_ascii=False)

        try:
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=batch_text)])],
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
            )
            response = await self._call_llm(request)
            summary = response.text or ""
            token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
            self._on_agent_success(len(summary), token_count)
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result={"records": [], "summary": summary},
            )
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=str(e),
            )
