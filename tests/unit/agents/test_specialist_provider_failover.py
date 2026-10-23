"""The declared provider fallback runs for the agents fixed in P9 (D8; decisions/html_page_fallback_context_wiring.md).

Each agent is built with its real constructor and an execution context whose primary provider is down. Before the
fix none of them called `_set_execution_context`, so `_call_llm` read `ctx = None` and the first 503 or 429 raised
`BothProvidersUnavailableError` instead of running the fallback the strategy table declares.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.domain_researcher_agent import DomainResearcherAgent
from src.agents.lelik_summarizer_agent import LelikSummarizerAgent
from src.agents.tasks_agent import TasksAgent
from src.agents.tutor_agent import TutorAgent
from src.agents.web_search_agent import WebSearchAgent
from src.domain.agent import AgentConfig
from src.domain.exceptions import LLMRateLimitError, LLMUnavailableError
from src.domain.user import PerformanceTier
from src.ports.llm_port import LLMPort, LLMRequest, LLMResponse, Message, MessagePart, ProviderCapabilities, UsageMetadata
from src.services.agent_context_builder import AgentExecutionContext


def _response(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], raw_content=None,
                       usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=3, total_tokens=8))


def _provider(answer=None, error=None) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock(side_effect=error) if error else AsyncMock(return_value=_response(answer))
    return llm


def _context(primary: LLMPort, fallback: LLMPort) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="test", provider=primary, model_name="primary-model", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), provider_name="openai",
        fallback_provider=fallback, fallback_model_name="fallback-model", fallback_provider_name="gemini",
        resilience_port=InMemoryProviderResilience(),
    )


def _config(agent_type: str) -> AgentConfig:
    return AgentConfig(agent_id=f"{agent_type}_agent", agent_type=agent_type)


BUILDERS = {
    "web_search": lambda ctx: WebSearchAgent(_config("web_search"), ctx),
    "tasks": lambda ctx: TasksAgent(_config("tasks"), ctx, MagicMock(), MagicMock(), MagicMock()),
    "domain_researcher": lambda ctx: DomainResearcherAgent(_config("domain_researcher"), ctx),
    "tutor": lambda ctx: TutorAgent(_config("tutor"), ctx),
    "lelik_summarizer": lambda ctx: LelikSummarizerAgent(_config("lelik_summarizer"), ctx, MagicMock()),
}
REQUEST = LLMRequest(model_name="primary-model", system_instruction="test", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


@pytest.mark.parametrize("error", [LLMUnavailableError("down", http_status=503), LLMRateLimitError("slow down", http_status=429)],
                         ids=["unavailable", "rate_limited"])
@pytest.mark.parametrize("agent_type", sorted(BUILDERS))
async def test_a_provider_outage_runs_the_declared_fallback(agent_type, error):
    primary, fallback = _provider(error=error), _provider(answer="fallback ok")
    agent = BUILDERS[agent_type](_context(primary, fallback))

    response = await agent._call_llm(REQUEST)

    assert response.text == "fallback ok"
    primary.generate_content.assert_called_once()
    fallback.generate_content.assert_called_once()
    assert fallback.generate_content.call_args.kwargs["request"].model_name == "fallback-model"


@pytest.mark.parametrize("agent_type", sorted(BUILDERS))
async def test_repeated_failures_of_the_primary_open_the_providers_breaker(agent_type):
    primary, fallback = _provider(error=LLMUnavailableError("down", http_status=503)), _provider(answer="fallback ok")
    context = _context(primary, fallback)
    agent = BUILDERS[agent_type](context)
    assert context.resilience_port.is_provider_open("openai") is False

    for _ in range(5):  # InMemoryProviderResilience opens after failure_threshold = 5 failures
        await agent._call_llm(REQUEST)

    assert agent._agent_execution_context is context
    assert context.resilience_port.is_provider_open("openai") is True


@pytest.mark.parametrize("agent_type", sorted(BUILDERS))
async def test_an_open_primary_breaker_skips_the_primary_call(agent_type):
    primary, fallback = _provider(answer="primary ok"), _provider(answer="fallback ok")
    context = _context(primary, fallback)
    for _ in range(10):
        context.resilience_port.record_failure("openai")
    agent = BUILDERS[agent_type](context)

    response = await agent._call_llm(REQUEST)

    assert response.text == "fallback ok"
    primary.generate_content.assert_not_called()
