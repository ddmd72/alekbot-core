"""
TutorExtractorAgent — the tutor's extractor (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md
§6: "the extractor... the same architectural slot ConsolidationAgent fills").

Not manifest-registered — a background writer, never reached via
delegate_to_specialist (same shape as ConsolidationAgent, which has zero
entries in agent_manifest.py). Constructed fresh per batch by
composition/companion_extractor_runner.py (services/ cannot import agents/
directly — REQ-ARCH-22), never cached or registered with the coordinator.

Single LLM call, structured JSON out — no multi-turn tool loop, no
dedup-search-before-write (unlike ConsolidationAgent's 8-step loop; add
dedup later only if repetition proves a real problem). Agents do not access
the database directly (root CLAUDE.md): this agent returns judgment only —
{"records": [...], "summary": str} — persistence is
CompanionExtractionService's job.
"""
import json

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.companion_extraction import EXTRACTION_TASK
from ..infrastructure.agent_config import TUTOR_EXTRACTOR
from ..ports.llm_port import LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from .base_agent import BaseAgent

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["records", "summary"],
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "domain"],
                "properties": {
                    "text": {"type": "string"},
                    "domain": {
                        "type": "string",
                        "enum": ["grammar_error", "vocabulary_gap", "topic_covered", "progress_note"],
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


class TutorExtractorAgent(BaseAgent):
    MAX_TOKENS = TUTOR_EXTRACTOR.max_tokens
    TEMPERATURE = TUTOR_EXTRACTOR.temperature

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
        return await self._extract(message, messages)

    async def _extract(self, message: AgentMessage, messages: list) -> AgentResponse:
        self._on_agent_start(f"{len(messages)} messages")

        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                "tutor_extractor",
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
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
            )
            response = await self._call_llm(request)
            parsed = self._parse_response(response.text or "")
            token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
            self._on_agent_success(len(response.text or ""), token_count)
            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=parsed,
            )
        except ValueError as e:
            self._on_agent_error(e, "parse_response")
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"Invalid extraction output: {e}",
            )
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=str(e),
            )

    @staticmethod
    def _parse_response(text: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Non-JSON extraction output: {e}") from e
        if "records" not in data or "summary" not in data:
            raise ValueError(f"Missing required keys in extraction output: {list(data.keys())}")
        return data
