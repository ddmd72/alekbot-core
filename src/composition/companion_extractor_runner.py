"""
CompanionExtractorRunner — composition-layer implementation of
CompanionExtractorPort. Constructs the per-companion-type extractor agent
fresh per call and runs it directly (agent.process(), no coordinator
involved — see Task 3's docstring for why construction is on-demand, not
eager/registered). composition/ is the one layer allowed to cross the
service/agent boundary (root CLAUDE.md Layer Semantics).

Two companion types today ("tutor", "voice"). _EXTRACTORS maps
companion_type -> (agent_type string, agent class); _TIMEOUTS_MS maps
companion_type -> that type's own AgentConfig.timeout_ms (each extractor
has a different cost profile — the tutor's 5-min batch JSON extraction vs
the voice summarizer's much cheaper single-call {title, summary} JSON, RFC
docs/10_rfcs/VOICE_COMPANION_RFC.md §4.9). Extend both maps together when a
new companion type ships its own extractor (RFC §6).

A call kept as a file (docs/10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md §4.11) runs the
same summarizer under its mode's companion type (CALL_FILE_SUMMARIZER_TYPES):
_CALL_FILE_PROFILES names the prompt profile for its turns, and the owner's UI
language (USER -> ACCOUNT -> SYSTEM, as the deep-research drive copy resolves
it) travels with it, because the turns are in other languages than the owner's.
"""
from typing import Any, Dict, List, Optional

from ..agents.lelik_summarizer_agent import LelikSummarizerAgent
from ..agents.tutor_extractor_agent import TutorExtractorAgent
from ..domain.agent import AgentIntent, AgentConfig, AgentMessage, AgentStatus
from ..domain.companion_extraction import EXTRACTION_TASK
from ..domain.voice_mode import CALL_FILE_SUMMARIZER_TYPES, VoiceMode
from ..infrastructure.agent_config import TUTOR_EXTRACTOR, VOICE_SUMMARIZER
from ..ports.companion_extractor_port import CompanionExtractorPort
from ..ports.language_service_port import LanguageServicePort
from ..ports.prompt_builder_port import PromptBuilderPort

_CALL_FILE_PROFILES = {
    CALL_FILE_SUMMARIZER_TYPES[VoiceMode.INTERPRETER]: "lelik_summarizer_interpreter",
    CALL_FILE_SUMMARIZER_TYPES[VoiceMode.TUTOR]: "lelik_summarizer_tutor",
}

_EXTRACTORS = {
    "tutor": ("tutor_extractor", TutorExtractorAgent),
    "voice": ("lelik_summarizer", LelikSummarizerAgent),
    **{companion_type: ("lelik_summarizer", LelikSummarizerAgent) for companion_type in _CALL_FILE_PROFILES},
}

_TIMEOUTS_MS = {
    "tutor": TUTOR_EXTRACTOR.timeout_ms,
    "voice": VOICE_SUMMARIZER.timeout_ms,
    **{companion_type: VOICE_SUMMARIZER.timeout_ms for companion_type in _CALL_FILE_PROFILES},
}


class CompanionExtractorRunner(CompanionExtractorPort):

    def __init__(
        self,
        context_builder: Any,
        user_repo: Any,
        prompt_builder: PromptBuilderPort,
        quota_service: Optional[Any] = None,
        prompt_content_store: Optional[Any] = None,
        language_service: Optional[LanguageServicePort] = None,
    ) -> None:
        self._context_builder = context_builder
        # Read only for a call kept as a file (its owner's language); the companion types never touch it.
        self._language = language_service
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        # Mirror the two post-construction attributes UserAgentFactory sets on every
        # agent it builds (see user_agent_factory.py::_create_and_cache_agents). This
        # runner bypasses the factory by design (constructs the agent fresh per batch,
        # not per-user-cached) so nothing else performs that wiring — without it,
        # BaseAgent._flush_billing()/_call_llm() silently no-op (both default to None),
        # making companion extraction's LLM usage invisible to billing + BigQuery.
        self._quota_service = quota_service
        self._prompt_content_store = prompt_content_store

    async def extract(
        self,
        companion_type: str,
        account_id: str,
        created_by_user_id: str,
        messages: List[dict],
    ) -> Dict[str, Any]:
        entry = _EXTRACTORS.get(companion_type)
        if entry is None:
            raise ValueError(f"No extractor registered for companion_type={companion_type!r}")
        agent_type, agent_cls = entry

        user_profile = await self._user_repo.get_user(created_by_user_id)
        if user_profile is None:
            raise ValueError(f"created_by_user_id={created_by_user_id!r} not found")

        execution_context = self._context_builder.build(agent_type, user_profile.config)
        agent = agent_cls(
            config=AgentConfig(
                agent_id=f"{agent_type}_{created_by_user_id}",
                agent_type=agent_type,
                timeout_ms=_TIMEOUTS_MS[companion_type],
                capabilities=[agent_type],
            ),
            execution_context=execution_context,
            prompt_builder=self._prompt_builder,
        )
        # See __init__ comment — mirror UserAgentFactory's post-construction wiring
        # (BaseAgent defaults both to None) so this agent's LLM usage is billed and
        # captured to BigQuery like every other agent's.
        agent._quota_service = self._quota_service
        agent._prompt_content_store = self._prompt_content_store

        payload: Dict[str, Any] = {"task": EXTRACTION_TASK, "messages": messages}
        profile = _CALL_FILE_PROFILES.get(companion_type)
        if profile is not None:
            if self._language is None:
                raise ValueError(f"companion_type={companion_type!r} needs the owner's language; no language service")
            payload["profile"] = profile
            payload["owner_language"] = await self._language.resolve_ui_language(created_by_user_id)

        message = AgentMessage.create(
            sender="companion_extractor_runner",
            recipient=agent.agent_id,
            intent=AgentIntent.DELEGATE,
            payload=payload,
            # user_id lets the extractor load the user's prompt overrides (e.g. the
            # LANG_* output-language slot), exactly as a user-facing agent would.
            context={"account_id": account_id, "user_id": created_by_user_id},
        )
        response = await agent.process(message)
        if response.status != AgentStatus.SUCCESS:
            raise RuntimeError(f"Extraction failed: {response.error}")
        return response.result or {}
