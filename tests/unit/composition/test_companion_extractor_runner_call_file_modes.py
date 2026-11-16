"""A call kept as a file is summarised under its mode's companion type (CALL_FILE_SUMMARIZER_TYPES): the runner runs the
real LelikSummarizerAgent with that mode's prompt profile and the owner's UI language as the owner_language block, on
the summarizer's own agent type (its tier and provider), while "voice" stays the companion call
(VOICE_CALL_LIVE_TEXT_RFC §4.11)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.companion_extractor_runner import CompanionExtractorRunner
from src.domain.language import LanguageCode
from src.domain.owner_language_block import render_owner_language_block
from src.domain.user import PerformanceTier
from src.domain.voice_mode import CALL_FILE_SUMMARIZER_TYPES, VoiceMode
from src.ports.language_service_port import LanguageServicePort
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities

_TURNS = [{"request_text": "We have a booking under RM-55120.", "response_text": "Abbiamo una prenotazione con il codice RM-55120."},
          {"request_text": "Is this Albergo Immaginario?",
           "response_text": "È questo l'Albergo Immaginario?"}]
_ANSWER = json.dumps({"title": "Check-in at Albergo Immaginario", "summary": "Alex Doe checked in at Albergo Immaginario with booking RM-55120."})


class _PromptBuilder:
    """Records each build and answers with a prompt, as the assembled Firestore prompt would."""

    def __init__(self):
        self.builds = []

    async def build_for_agent(self, agent_type, **kwargs):
        self.builds.append((agent_type, kwargs))
        return f"prompt of {agent_type}"


class _ContextBuilder:
    def __init__(self):
        self.agent_types = []
        self.llm = AsyncMock(spec=LLMPort)
        self.llm.generate_content.return_value = MagicMock(text=_ANSWER, usage_metadata=MagicMock(total_tokens=9))

    def build(self, agent_type, config):
        self.agent_types.append(agent_type)
        return AgentExecutionContext(
            agent_type=agent_type, provider=self.llm, model_name="claude-sonnet-5", tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities(), provider_name="claude", resilience_port=InMemoryProviderResilience(),
        )


class _Language(LanguageServicePort):
    def __init__(self, language):
        self.language, self.asked = language, []

    async def resolve_ui_language(self, user_id):
        self.asked.append(user_id)
        return self.language

    async def get_preference(self, user_id):
        return self.language, True


def _request(call):
    """The LLMRequest of one provider call, whether it was passed by position or by keyword."""
    return call.args[0] if call.args else next(iter(call.kwargs.values()))


def _runner(language_service):
    users = MagicMock()
    users.get_user = AsyncMock(return_value=MagicMock(config=MagicMock()))
    contexts, prompts = _ContextBuilder(), _PromptBuilder()
    runner = CompanionExtractorRunner(context_builder=contexts, user_repo=users, prompt_builder=prompts,
                                      language_service=language_service)
    return runner, contexts, prompts


@pytest.mark.parametrize("mode,profile", [(VoiceMode.INTERPRETER, "lelik_summarizer_interpreter"),
                                          (VoiceMode.TUTOR, "lelik_summarizer_tutor")])
async def test_a_file_mode_runs_the_summarizer_with_its_profile_and_the_owners_language(mode, profile):
    language = _Language(LanguageCode.EN)
    runner, contexts, prompts = _runner(language)

    result = await runner.extract(CALL_FILE_SUMMARIZER_TYPES[mode], "acc-1", "user-1", _TURNS)

    assert result == {"records": [], "title": "Check-in at Albergo Immaginario",
                      "summary": "Alex Doe checked in at Albergo Immaginario with booking RM-55120."}
    assert contexts.agent_types == ["lelik_summarizer"]
    assert language.asked == ["user-1"]
    [(built, kwargs)] = prompts.builds
    assert built == profile
    assert kwargs["extra_static_blocks"] == [render_owner_language_block(LanguageCode.EN)]
    assert kwargs["user_id"] == "user-1" and kwargs["account_id"] == "acc-1"
    request = _request(contexts.llm.generate_content.await_args)
    assert request.system_instruction == f"prompt of {profile}"
    assert json.loads(request.messages[0].parts[0].text) == _TURNS


async def test_the_companion_call_keeps_its_profile_and_never_reads_the_language():
    language = _Language(LanguageCode.EN)
    runner, contexts, prompts = _runner(language)

    result = await runner.extract("voice", "acc-1", "user-1", _TURNS)

    assert result["summary"] == "Alex Doe checked in at Albergo Immaginario with booking RM-55120."
    [(built, kwargs)] = prompts.builds
    assert built == "lelik_summarizer"
    assert "extra_static_blocks" not in kwargs
    assert language.asked == []


async def test_a_file_mode_without_a_language_service_is_refused_before_any_model_call():
    runner, contexts, prompts = _runner(None)

    with pytest.raises(ValueError, match="owner's language"):
        await runner.extract(CALL_FILE_SUMMARIZER_TYPES[VoiceMode.TUTOR], "acc-1", "user-1", _TURNS)

    assert prompts.builds == []
    assert contexts.llm.generate_content.await_count == 0
