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
§4.9). Its output is one JSON object {"title", "summary"} (OUTPUT_FORMAT_LELIK_SUMMARIZER):
summary is the note posted to the caller's chat or opening a saved call file,
title names that file (RFC docs/10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md §4.11). A
call summary has no records to extract (Lelik has no CompanionRecord store per
RFC §4.9), so "records" is always an empty list, fixed in code — never asked of
the LLM, never parsed from it. No multi-turn tool loop, no
dedup-search-before-write; the only repeat is the parse retry.

One agent, one output contract, one prompt per call mode: the companion call
reads COMPANION_PROFILE; a call kept as a file names its own mode's profile in
the payload ("profile") together with the owner's language ("owner_language"),
rendered as the `owner_language` block, because its turns are in other
languages than the owner's (VOICE_CALL_LIVE_TEXT_RFC §4.11). The runner picks
both (composition/companion_extractor_runner.py).
"""
import json
from typing import List

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.companion_extraction import EXTRACTION_TASK
from ..domain.language import LanguageCode
from ..domain.owner_language_block import render_owner_language_block
from ..infrastructure.agent_config import VOICE_SUMMARIZER
from ..ports.llm_port import LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

MAX_PARSE_RETRIES = VOICE_SUMMARIZER.max_parse_retries

# The companion call's profile: its note goes to the caller's chat, and an absent payload "profile" means this call.
COMPANION_PROFILE = "lelik_summarizer"

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["title", "summary"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
}


class LelikSummarizerAgent(BaseAgent):
    MAX_TOKENS = VOICE_SUMMARIZER.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context,
        prompt_builder: PromptBuilderPort,
    ) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
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

        profile = message.payload.get("profile", COMPANION_PROFILE)
        owner_language = message.payload.get("owner_language")
        if profile != COMPANION_PROFILE and not isinstance(owner_language, LanguageCode):
            # A file mode's prompt names its language only through the owner_language block; without it the
            # note would fall back to whatever language the transcript leans to.
            logger.error(f"💥 lelik_summarizer: profile {profile} came without an owner language")
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"owner_language missing for profile {profile}",
            )
        try:
            # Identity loads the user's overrides: the companion note lands in their chat, so it follows their
            # output-language setting. A file mode's language comes from the owner_language block instead.
            prompt_kwargs = {}
            if profile != COMPANION_PROFILE:
                prompt_kwargs["extra_static_blocks"] = [render_owner_language_block(owner_language)]
            system_prompt = await self.prompt_builder.build_for_agent(
                profile,
                user_id=message.context.get("user_id"),
                account_id=message.context.get("account_id"),
                include_biographical=False,
                include_directives=False,
                **prompt_kwargs,
            )
        except Exception as e:
            self._on_agent_error(e, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"PromptBuilder failed: {e}",
            )

        batch_text = json.dumps(messages, ensure_ascii=False)
        history: List[Message] = [Message(role="user", parts=[MessagePart(text=batch_text)])]

        try:
            for attempt in range(1, MAX_PARSE_RETRIES + 2):
                request = LLMRequest(
                    model_name=self.model_name,
                    system_instruction=system_prompt,
                    messages=history,
                    max_tokens=self.MAX_TOKENS,
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                )
                response = await self._call_llm(request, turn=attempt)
                raw = response.text or ""
                try:
                    parsed = self._parse_response(raw)
                except ValueError as parse_err:
                    logger.warning(f"⚠️ lelik_summarizer: invalid output (attempt {attempt}/{MAX_PARSE_RETRIES + 1}): "
                                   f"{parse_err}")
                    history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                    history.append(Message(role="user", parts=[MessagePart(text=(
                        f"Your previous output was not valid JSON. Error: {parse_err}. "
                        "Output ONLY the JSON object output_format describes."
                    ))]))
                    continue
                token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
                self._on_agent_success(len(parsed["summary"]), token_count)
                return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=parsed)
            logger.error(f"💥 lelik_summarizer: parse_error after {MAX_PARSE_RETRIES + 1} attempts")
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error="parse_error")
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=str(e),
            )

    @staticmethod
    def _parse_response(text: str) -> dict:
        """The output object -> {"records": [], "title", "summary"}; ValueError when it is not that object."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Non-JSON summarizer output: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("Summarizer output is not a JSON object")
        title, summary = data.get("title"), data.get("summary")
        if not isinstance(title, str) or not isinstance(summary, str):
            raise ValueError(f"title and summary must both be strings; keys present: {sorted(data.keys())}")
        return {"records": [], "title": title.strip(), "summary": summary.strip()}
