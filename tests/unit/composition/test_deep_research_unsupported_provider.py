"""A stale deep research provider in a user's config fails the request loudly (the OpenAI deep research path is
removed; Claude is the only provider).

`agent_providers["deep_research"] = "openai"` is chosen in one place (`AgentContextBuilder.resolve_async_context`, called
by `UserAgentFactory._build_deep_research`): the agent is not built, one ERROR line names the provider, the delegation
answers a failure, and nothing is submitted to Claude in its place. The control case, the same world without the stale
setting, submits the research to Claude.
"""
import logging
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents.deep_research_agent import DeepResearchAgent
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.agent import AgentStatus
from src.domain.exceptions import UnsupportedProviderError
from src.domain.user import UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.ports.agent_factory_port import AgentFactoryPort
from src.ports.deep_research_port import DeepResearchPort
from src.ports.prompt_builder_port import PromptBuilderPort
from src.services.agent_context_builder import AgentContextBuilder
from src.services.provider_registry import ProviderRegistry

USER = "u1"
QUERY = "Heat pumps in Springfield"


class RecordingClaudeResearch(DeepResearchPort):
    """The Claude deep research boundary: records what was submitted."""

    def __init__(self) -> None:
        self.submitted: List[str] = []

    async def create_interaction(self, query, user_id, account_id, original_query, **kwargs) -> str:  # type: ignore[override]
        self.submitted.append(original_query)
        return "job-1"

    async def get_status(self, job_id: str) -> tuple[str, str]:
        return "in_progress", ""


class DeepResearchOnlyFactory(AgentFactoryPort):
    """Builds the deep research agent through the production builder and registers it, as the factory does."""

    def __init__(self, coordinator: AgentCoordinator, claude: RecordingClaudeResearch, config: UserBotConfig) -> None:
        self._coordinator = coordinator
        self._owner = _owner(claude)
        # A working prompt builder: the agent now fails on a prompt-build failure, and this test is about routing.
        prompt_builder = AsyncMock(spec=PromptBuilderPort)
        prompt_builder.build_for_agent.return_value = "stub system prompt"
        self._ctx = _UserContext(user_profile=SimpleNamespace(config=config), prompt_builder=prompt_builder)

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        agent = UserAgentFactory._build_deep_research(self._owner, user_id, self._ctx)  # type: ignore[arg-type]
        if agent is None:
            return False
        if self._coordinator.get_agent(agent.agent_id) is None:
            self._coordinator.register_agent(agent)
        return True


def _owner(claude: RecordingClaudeResearch) -> SimpleNamespace:
    """What `_build_deep_research` reads from the factory: the deep research registry and the context builder."""
    jobs: ProviderRegistry = ProviderRegistry()
    jobs.register("claude", claude)
    return SimpleNamespace(job_registry=jobs,
                           context_builder=AgentContextBuilder(ProviderRegistry(), InMemoryProviderResilience()))


async def _ask_for_research(config: UserBotConfig):
    claude = RecordingClaudeResearch()
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry, task_queue=None)
    coordinator.set_agent_factory(DeepResearchOnlyFactory(coordinator, claude, config))
    response = await coordinator.handle_delegation(
        Intent.DEEP_RESEARCH, QUERY, {"user_id": USER, "account_id": "a1", "params": {"query": QUERY}},
        calling_agent_id="smart_response_agent")
    return response, claude


def _error_lines(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("stale", ["openai", " OpenAI "])
async def test_a_stale_openai_setting_fails_the_request_with_one_error_line_and_submits_nothing(caplog, stale):
    with caplog.at_level(logging.WARNING):
        response, claude = await _ask_for_research(UserBotConfig(agent_providers={"deep_research": stale}))

    assert response.status == AgentStatus.FAILED
    assert claude.submitted == []
    errors = [line for line in _error_lines(caplog) if "Deep research refused" in line]
    assert errors and all("agent_providers['deep_research']='openai'" in line for line in errors)


async def test_without_the_stale_setting_the_same_request_goes_to_claude(caplog):
    with caplog.at_level(logging.WARNING):
        response, claude = await _ask_for_research(UserBotConfig())

    assert response.status != AgentStatus.FAILED
    assert claude.submitted == [QUERY]
    assert not [line for line in _error_lines(caplog) if "Deep research refused" in line]


def test_the_chooser_raises_for_the_stale_provider_instead_of_resolving_the_default():
    builder = AgentContextBuilder(ProviderRegistry(), InMemoryProviderResilience())
    jobs: ProviderRegistry = ProviderRegistry()
    jobs.register("claude", RecordingClaudeResearch())

    with pytest.raises(UnsupportedProviderError) as raised:
        builder.resolve_async_context("deep_research", jobs, UserBotConfig(agent_providers={"deep_research": "openai"}))

    assert (raised.value.agent_type, raised.value.provider) == ("deep_research", "openai")


def test_the_claude_setting_builds_the_agent_over_the_claude_port():
    claude = RecordingClaudeResearch()
    ctx = _UserContext(user_profile=SimpleNamespace(config=UserBotConfig(agent_providers={"deep_research": "claude"})),  # type: ignore[arg-type]
                       prompt_builder=None)  # type: ignore[arg-type]

    agent = UserAgentFactory._build_deep_research(_owner(claude), USER, ctx)  # type: ignore[arg-type]

    assert isinstance(agent, DeepResearchAgent) and agent._job_port is claude
