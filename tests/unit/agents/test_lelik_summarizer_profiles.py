"""The summarizer builds the prompt the payload names: the companion profile by default, a file mode's profile with the
owner_language block, and nothing at all for a file mode that came without the owner's language."""
import json
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.lelik_summarizer_agent import COMPANION_PROFILE, LelikSummarizerAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.companion_extraction import EXTRACTION_TASK
from src.domain.language import LanguageCode
from src.domain.owner_language_block import render_owner_language_block
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities

_TURNS = [{"request_text": "Hablamos del fin de semana en Coimbra", "response_text": "¿Y qué hicisteis allí?"}]


class _PromptBuilder:
    def __init__(self):
        self.builds = []

    async def build_for_agent(self, agent_type, **kwargs):
        self.builds.append((agent_type, kwargs))
        return f"prompt of {agent_type}"


def _request(call):
    """The LLMRequest of one provider call, whether it was passed by position or by keyword."""
    return call.args[0] if call.args else next(iter(call.kwargs.values()))


def _agent():
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text=json.dumps({"title": "Weekend in Coimbra", "summary": "We practised the past tense."}),
        usage_metadata=MagicMock(total_tokens=5))
    ctx = AgentExecutionContext(
        agent_type="lelik_summarizer", provider=llm, model_name="claude-sonnet-5", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), provider_name="claude", resilience_port=InMemoryProviderResilience(),
    )
    prompts = _PromptBuilder()
    config = AgentConfig(agent_id="lelik_summarizer_u1", agent_type="lelik_summarizer", timeout_ms=60000,
                         capabilities=["lelik_summarizer"])
    return LelikSummarizerAgent(config=config, execution_context=ctx, prompt_builder=prompts), prompts, llm


def _message(**extra):
    return AgentMessage(task_id="t1", sender="test", recipient="lelik_summarizer_u1", intent=AgentIntent.DELEGATE,
                        payload={"task": EXTRACTION_TASK, "messages": _TURNS, **extra},
                        context={"account_id": "acc-1", "user_id": "u1"})


async def test_no_profile_in_the_payload_is_the_companion_call():
    agent, prompts, _ = _agent()
    response = await agent.execute(_message())
    assert response.status is AgentStatus.SUCCESS
    [(built, kwargs)] = prompts.builds
    assert built == COMPANION_PROFILE == "lelik_summarizer"
    assert "extra_static_blocks" not in kwargs


async def test_a_file_profile_is_built_with_the_owner_language_block():
    agent, prompts, llm = _agent()
    response = await agent.execute(_message(profile="lelik_summarizer_tutor", owner_language=LanguageCode.FR))
    assert response.status is AgentStatus.SUCCESS
    assert response.result["title"] == "Weekend in Coimbra"
    [(built, kwargs)] = prompts.builds
    assert built == "lelik_summarizer_tutor"
    assert kwargs["extra_static_blocks"] == [render_owner_language_block(LanguageCode.FR)]
    assert _request(llm.generate_content.await_args).system_instruction == "prompt of lelik_summarizer_tutor"


async def test_a_file_profile_without_the_owner_language_fails_before_any_prompt_or_model_call():
    agent, prompts, llm = _agent()
    for payload in ({"profile": "lelik_summarizer_interpreter"},
                    {"profile": "lelik_summarizer_interpreter", "owner_language": "en"}):
        response = await agent.execute(_message(**payload))
        assert response.status is AgentStatus.FAILED
        assert "owner_language" in response.error
    assert prompts.builds == []
    assert llm.generate_content.await_count == 0
