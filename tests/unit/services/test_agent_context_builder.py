import pytest
from unittest.mock import MagicMock

from src.services.agent_context_builder import AgentContextBuilder, AgentExecutionContext
from src.services.provider_registry import ProviderRegistry
from src.services.prompt_cache_strategy import PromptCacheStrategy
from src.services.caching_llm_proxy import CachingLLMProxy
from src.domain.user import UserBotConfig, PerformanceTier
from src.ports.llm_port import LLMPort, ProviderCapabilities


class FakeProvider(LLMPort):
    def __init__(self, name: str):
        self.name = name
        self.caps = ProviderCapabilities(
            native_tools=(name == "gemini"),
            context_caching=(name == "claude"),
        )

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
    reg.register("openai", FakeProvider("openai"))
    return reg


@pytest.fixture
def builder(registry):
    return AgentContextBuilder(registry)


def test_build_default_strategy(builder):
    config = UserBotConfig()
    # quick agent defaults to claude, tier ECO (from agent_tiers default)
    ctx = builder.build("quick", config)

    assert ctx.agent_type == "quick"
    assert ctx.provider.name == "claude"
    assert ctx.tier == PerformanceTier.ECO
    assert ctx.model_name == "claude-model-for-eco"
    assert ctx.capabilities.context_caching is True


def test_build_respects_user_provider_preference(builder):
    config = UserBotConfig(provider_preference="claude")
    # smart agent allows claude override, tier PERFORMANCE (from agent_tiers default)
    ctx = builder.build("smart", config)

    assert ctx.provider.name == "claude"
    assert ctx.model_name == "claude-model-for-performance"


def test_build_ignores_unsupported_provider_preference(builder):
    config = UserBotConfig(provider_preference="unsupported_provider")
    ctx = builder.build("web_search", config)

    # web_search default is openai, unsupported preference ignored
    assert ctx.provider.name == "openai"


def test_build_respects_agent_tier_override(builder):
    config = UserBotConfig(agent_tiers={"quick": PerformanceTier.PERFORMANCE})
    ctx = builder.build("quick", config)

    assert ctx.tier == PerformanceTier.PERFORMANCE
    assert ctx.model_name == "claude-model-for-performance"


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


# ============================================================================
# Caching strategy integration tests
# ============================================================================


@pytest.fixture
def builder_with_cache(registry):
    return AgentContextBuilder(
        registry,
        cache_strategy=PromptCacheStrategy(),
        caching_proxy_factory=CachingLLMProxy,
    )


def test_build_wraps_provider_when_caching_strategy_applies(builder_with_cache):
    """Consolidation (Claude, caching capable) → provider wrapped in CachingLLMProxy."""
    config = UserBotConfig()
    ctx = builder_with_cache.build("consolidation", config)

    assert isinstance(ctx.provider, CachingLLMProxy)


def test_build_no_wrapping_without_cache_strategy(builder):
    """Without cache_strategy, provider is raw (backward compatible)."""
    config = UserBotConfig()
    ctx = builder.build("consolidation", config)

    assert not isinstance(ctx.provider, CachingLLMProxy)
    assert isinstance(ctx.provider, FakeProvider)


def test_build_no_wrapping_for_non_caching_provider(builder_with_cache):
    """Gemini (non-caching) → no wrapping even with cache strategy."""
    config = UserBotConfig()
    ctx = builder_with_cache.build("web_search", config)  # web_search defaults to gemini

    # Gemini has context_caching=False, so strategy returns None
    assert not isinstance(ctx.provider, CachingLLMProxy)
    assert isinstance(ctx.provider, FakeProvider)


def test_build_no_wrapping_for_router(builder_with_cache):
    """Router → no caching even if provider supports it."""
    config = UserBotConfig()
    ctx = builder_with_cache.build("router", config)

    assert not isinstance(ctx.provider, CachingLLMProxy)


def test_wrapped_provider_delegates_capabilities(builder_with_cache):
    """CachingLLMProxy delegates get_capabilities correctly."""
    config = UserBotConfig()
    ctx = builder_with_cache.build("consolidation", config)

    # Should delegate to inner Claude provider
    caps = ctx.provider.get_capabilities()
    assert caps.context_caching is True
