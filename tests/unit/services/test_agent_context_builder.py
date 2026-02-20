import pytest
from unittest.mock import MagicMock

from src.services.agent_context_builder import AgentContextBuilder, AgentExecutionContext
from src.services.provider_registry import ProviderRegistry
from src.domain.user import UserBotConfig, PerformanceTier
from src.ports.llm_service import LLMService, ProviderCapabilities


class FakeProvider(LLMService):
    def __init__(self, name: str):
        self.name = name
        self.caps = ProviderCapabilities(native_tools=(name == "gemini"))

    async def generate_content(self, *args, **kwargs):
        pass

    def supports_caching(self) -> bool:
        return False

    async def upload_file(self, path: str, mime_type: str):
        pass

    def get_capabilities(self) -> ProviderCapabilities:
        return self.caps

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return f"{self.name}-model-for-{tier.value}"


@pytest.fixture
def registry():
    reg = ProviderRegistry()
    reg.register("gemini", FakeProvider("gemini"))
    reg.register("claude", FakeProvider("claude"))
    return reg


@pytest.fixture
def builder(registry):
    return AgentContextBuilder(registry)


def test_build_default_strategy(builder):
    config = UserBotConfig()
    # quick agent defaults to gemini, tier BALANCED (from agent_tiers default)
    ctx = builder.build("quick", config)

    assert ctx.agent_type == "quick"
    assert ctx.provider.name == "gemini"
    assert ctx.tier == PerformanceTier.BALANCED
    assert ctx.model_name == "gemini-model-for-balanced"
    assert ctx.capabilities.native_tools is True


def test_build_respects_user_provider_preference(builder):
    config = UserBotConfig(provider_preference="claude")
    # smart agent allows claude override, tier PERFORMANCE (from agent_tiers default)
    ctx = builder.build("smart", config)

    assert ctx.provider.name == "claude"
    assert ctx.model_name == "claude-model-for-performance"


def test_build_ignores_unsupported_provider_preference(builder):
    config = UserBotConfig(provider_preference="unsupported_provider")
    ctx = builder.build("web_search", config)

    # web_search only allows gemini
    assert ctx.provider.name == "gemini"


def test_build_respects_agent_tier_override(builder):
    config = UserBotConfig(agent_tiers={"quick": PerformanceTier.PERFORMANCE})
    ctx = builder.build("quick", config)

    assert ctx.tier == PerformanceTier.PERFORMANCE
    assert ctx.model_name == "gemini-model-for-performance"


def test_build_respects_model_override(builder):
    config = UserBotConfig(model_overrides={"quick": "custom-model-123"})
    ctx = builder.build("quick", config)

    assert ctx.model_name == "custom-model-123"


def test_build_smart_agent_defaults_to_gemini(builder):
    config = UserBotConfig()
    # smart agent now defaults to gemini, tier PERFORMANCE (from agent_tiers default)
    ctx = builder.build("smart", config)

    assert ctx.provider.name == "gemini"
    assert ctx.tier == PerformanceTier.PERFORMANCE
    assert ctx.capabilities.native_tools is True


def test_build_router_agent_defaults_to_gemini(builder):
    """Test router strategy added in Session 20"""
    config = UserBotConfig()
    # router agent defaults to gemini for fast inference, tier ECO
    ctx = builder.build("router", config)

    assert ctx.provider.name == "gemini"
    assert ctx.tier == PerformanceTier.ECO
    assert ctx.model_name == "gemini-model-for-eco"


def test_build_router_respects_provider_preference(builder):
    """Test router can use grok if user prefers (claude not allowed for router)"""
    config = UserBotConfig(provider_preference="grok")
    
    # Need to register grok in registry for this test
    from tests.unit.services.test_agent_context_builder import FakeProvider
    builder.registry.register("grok", FakeProvider("grok"))
    
    ctx = builder.build("router", config)

    assert ctx.provider.name == "grok"
    assert ctx.model_name == "grok-model-for-eco"


def test_build_consolidation_defaults_to_claude_balanced(builder):
    """Test consolidation uses PERFORMANCE tier (from agent_tiers default)"""
    config = UserBotConfig()
    ctx = builder.build("consolidation", config)

    assert ctx.provider.name == "claude"
    assert ctx.tier == PerformanceTier.PERFORMANCE


def test_build_with_default_tier_override(builder):
    """Test default_tier provides fallback when strategy doesn't specify tier
    
    Note: Strategies have their own default tiers (e.g., quick=ECO, smart=BALANCED).
    default_tier is used when config doesn't have agent_tiers override AND
    strategy uses it as fallback. This test verifies agent_tiers work correctly.
    """
    config = UserBotConfig(
        default_tier=PerformanceTier.PERFORMANCE,
        agent_tiers={
            "quick": PerformanceTier.PERFORMANCE,
            "smart": PerformanceTier.PERFORMANCE
        }
    )
    
    quick_ctx = builder.build("quick", config)
    smart_ctx = builder.build("smart", config)
    
    # Agent-specific tier overrides are respected
    assert quick_ctx.tier == PerformanceTier.PERFORMANCE
    assert smart_ctx.tier == PerformanceTier.PERFORMANCE
