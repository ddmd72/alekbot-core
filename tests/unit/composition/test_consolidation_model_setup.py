"""The consolidation context pins Claude's model in code; every other agent and provider is built as before."""
import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.composition.consolidation_model_setup import consolidation_context
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.agent_config import CONSOLIDATION
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.services.agent_context_builder import AgentContextBuilder
from src.services.provider_registry import ProviderRegistry


class _Provider(LLMPort):
    def __init__(self, name: str):
        self.name = name

    async def generate_content(self, *args, **kwargs):
        pass

    def supports_caching(self) -> bool:
        return False

    async def upload_file(self, path: str, mime_type: str):
        pass

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_tools=(self.name == "gemini"), context_caching=(self.name == "claude"))

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return f"{self.name}-model-for-{tier.value}"


@pytest.fixture
def builder():
    registry = ProviderRegistry()
    for name in ("claude", "gemini", "openai"):
        registry.register(name, _Provider(name))
    return AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience())


def test_on_claude_consolidation_runs_the_pinned_model(builder):
    context = consolidation_context(builder, UserBotConfig())

    assert context.model_name == CONSOLIDATION.claude_model
    assert context.provider_name == "claude"
    assert context.tier is PerformanceTier.PERFORMANCE
    assert context.fallback_provider is None


def test_the_pin_is_sonnet_5_5():
    assert CONSOLIDATION.claude_model == "claude-sonnet-5-5"


def test_a_users_model_override_still_wins(builder):
    config = UserBotConfig(model_overrides={"consolidation": "x-model"})

    assert consolidation_context(builder, config).model_name == "x-model"


def test_another_provider_keeps_its_tier_model(builder):
    config = UserBotConfig(agent_providers={"consolidation": "gemini"})

    assert consolidation_context(builder, config).model_name == "gemini-model-for-performance"


def test_other_agents_are_built_as_before(builder):
    assert builder.build("tutor_extractor", UserBotConfig()).model_name == "claude-model-for-performance"
    assert builder.build("smart", UserBotConfig()).model_name != CONSOLIDATION.claude_model
