"""
Unit tests for AgentContextBuilder per-agent provider selection (3-level resolution).

Tests the new agent_providers field that allows different providers for different agents.
"""

import pytest
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry
from src.domain.user import UserBotConfig, PerformanceTier
from src.ports.llm_service import LLMService, ProviderCapabilities
from unittest.mock import Mock


@pytest.fixture
def mock_gemini_adapter():
    """Mock Gemini adapter."""
    adapter = Mock(spec=LLMService)
    adapter.get_model_for_tier.return_value = "gemini-flash-lite-latest"
    adapter.get_capabilities.return_value = ProviderCapabilities(
        native_tools=True,
        context_caching=False,
        vision=True,
        max_context_window=1000000
    )
    return adapter


@pytest.fixture
def mock_claude_adapter():
    """Mock Claude adapter."""
    adapter = Mock(spec=LLMService)
    adapter.get_model_for_tier.return_value = "claude-sonnet-4-5"
    adapter.get_capabilities.return_value = ProviderCapabilities(
        native_tools=False,
        context_caching=True,
        vision=True,
        max_context_window=200000
    )
    return adapter


@pytest.fixture
def mock_grok_adapter():
    """Mock Grok adapter."""
    adapter = Mock(spec=LLMService)
    adapter.get_model_for_tier.return_value = "grok-2-latest"
    adapter.get_capabilities.return_value = ProviderCapabilities(
        native_tools=True,
        context_caching=False,
        vision=False,
        max_context_window=128000
    )
    return adapter


@pytest.fixture
def registry(mock_gemini_adapter, mock_claude_adapter, mock_grok_adapter):
    """Provider registry with all mocked adapters."""
    reg = ProviderRegistry()
    reg.register("gemini", mock_gemini_adapter)
    reg.register("claude", mock_claude_adapter)
    reg.register("grok", mock_grok_adapter)
    return reg


@pytest.fixture
def builder(registry):
    """AgentContextBuilder with mocked registry."""
    return AgentContextBuilder(registry)


# ============================================================================
# Test 3-Level Resolution: agent_providers > provider_preference > strategy default
# ============================================================================

def test_level_1_per_agent_provider_overrides_global_preference(builder):
    """
    Level 1 (highest priority): agent_providers overrides provider_preference.
    
    Scenario: User wants Gemini globally, but Claude specifically for smart agent.
    """
    config = UserBotConfig(
        provider_preference="gemini",  # Global preference
        agent_providers={
            "smart": "claude"  # Per-agent override
        },
        default_tier=PerformanceTier.PERFORMANCE
    )
    
    context = builder.build(agent_type="smart", config=config)
    
    # Should use Claude (per-agent override), NOT Gemini (global preference)
    assert context.provider.get_model_for_tier.return_value == "claude-sonnet-4-5"
    assert context.capabilities.context_caching is True  # Claude capability


def test_level_2_global_preference_overrides_strategy_default(builder):
    """
    Level 2: provider_preference overrides strategy default when no per-agent override.
    
    Scenario: User wants Claude globally, router would normally use Gemini (default).
    """
    config = UserBotConfig(
        provider_preference="gemini",  # Global preference (router allows both gemini and grok)
        default_tier=PerformanceTier.ECO
    )
    
    context = builder.build(agent_type="router", config=config)
    
    # Should use Gemini (global preference), NOT strategy default
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


def test_level_3_strategy_default_when_no_overrides(builder):
    """
    Level 3 (lowest priority): strategy default when no overrides.
    
    Scenario: No user preferences, should use strategy default.
    """
    config = UserBotConfig(
        default_tier=PerformanceTier.PERFORMANCE
    )
    
    context = builder.build(agent_type="smart", config=config)
    
    # Should use Gemini (strategy default for smart agent — changed from Claude)
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


def test_per_agent_provider_ignored_if_not_in_allowed_providers(builder):
    """
    Per-agent provider must be in allowed_providers list.
    
    Scenario: User tries to set Claude for router (not allowed), should fallback.
    """
    config = UserBotConfig(
        provider_preference="gemini",
        agent_providers={
            "router": "claude"  # NOT in allowed_providers for router
        },
        default_tier=PerformanceTier.ECO
    )
    
    context = builder.build(agent_type="router", config=config)
    
    # Should fallback to global preference (Gemini), NOT use invalid Claude
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


def test_global_preference_ignored_if_not_in_allowed_providers(builder):
    """
    Global preference must be in allowed_providers list.
    
    Scenario: User sets Claude as global preference, but web_search only allows Gemini.
    """
    config = UserBotConfig(
        provider_preference="claude",  # NOT allowed for web_search
        default_tier=PerformanceTier.BALANCED
    )
    
    context = builder.build(agent_type="web_search", config=config)
    
    # Should use strategy default (Gemini), NOT invalid Claude preference
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


# ============================================================================
# Test Complex Multi-Agent Scenarios
# ============================================================================

def test_mixed_providers_across_agents(builder):
    """
    Different agents can use different providers based on per-agent overrides.
    
    Scenario: Gemini for router/quick, Claude for smart/consolidation.
    """
    config = UserBotConfig(
        provider_preference="gemini",  # Default
        agent_providers={
            "smart": "claude",
            "consolidation": "claude"
        },
        agent_tiers={
            "router": PerformanceTier.ECO,
            "quick": PerformanceTier.BALANCED,
            "smart": PerformanceTier.PERFORMANCE,
            "consolidation": PerformanceTier.PERFORMANCE
        }
    )
    
    # Router should use Gemini (global preference)
    router_ctx = builder.build(agent_type="router", config=config)
    assert router_ctx.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"
    
    # Quick should use Gemini (global preference)
    quick_ctx = builder.build(agent_type="quick", config=config)
    assert quick_ctx.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"
    
    # Smart should use Claude (per-agent override)
    smart_ctx = builder.build(agent_type="smart", config=config)
    assert smart_ctx.provider.get_model_for_tier.return_value == "claude-sonnet-4-5"
    
    # Consolidation should use Claude (per-agent override)
    cons_ctx = builder.build(agent_type="consolidation", config=config)
    assert cons_ctx.provider.get_model_for_tier.return_value == "claude-sonnet-4-5"


def test_grok_for_router_gemini_for_smart(builder):
    """
    Real-world scenario: Grok for fast routing, Gemini for smart reasoning.
    
    Scenario: Use Grok's speed for router, keep Gemini as default for others.
    """
    config = UserBotConfig(
        provider_preference="gemini",  # Default for most agents
        agent_providers={
            "router": "grok"  # Fast inference for routing
        },
        agent_tiers={
            "router": PerformanceTier.ECO,
            "smart": PerformanceTier.PERFORMANCE
        }
    )
    
    # Router should use Grok (per-agent override)
    router_ctx = builder.build(agent_type="router", config=config)
    assert router_ctx.provider.get_model_for_tier.return_value == "grok-2-latest"
    
    # Smart should use Gemini (global preference, no override)
    smart_ctx = builder.build(agent_type="smart", config=config)
    assert smart_ctx.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


# ============================================================================
# Test Backward Compatibility
# ============================================================================

def test_backward_compatibility_no_agent_providers_field(builder):
    """
    When agent_providers is None (old configs), should work as before.
    
    Scenario: Legacy config without agent_providers field.
    """
    config = UserBotConfig(
        provider_preference="gemini",
        # agent_providers=None (default)
        default_tier=PerformanceTier.BALANCED
    )
    
    context = builder.build(agent_type="smart", config=config)
    
    # Should fallback to global preference or strategy default
    # Smart strategy allows gemini, so preference wins
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


def test_empty_agent_providers_dict(builder):
    """
    Empty agent_providers dict should behave same as None.
    
    Scenario: User explicitly sets agent_providers={} (empty).
    """
    config = UserBotConfig(
        provider_preference="claude",
        agent_providers={},  # Empty dict
        default_tier=PerformanceTier.PERFORMANCE
    )
    
    context = builder.build(agent_type="smart", config=config)
    
    # Should use global preference (Claude)
    assert context.provider.get_model_for_tier.return_value == "claude-sonnet-4-5"


# ============================================================================
# Test Edge Cases
# ============================================================================

def test_unknown_agent_type_uses_quick_strategy(builder):
    """
    Unknown agent types should use 'quick' strategy as fallback.
    
    Scenario: User creates custom agent type not in STRATEGIES.
    """
    config = UserBotConfig(
        provider_preference="gemini",
        default_tier=PerformanceTier.BALANCED
    )
    
    context = builder.build(agent_type="custom_agent", config=config)
    
    # Should use quick strategy (gemini default)
    assert context.provider.get_model_for_tier.return_value == "gemini-flash-lite-latest"


def test_per_agent_provider_with_model_override(builder):
    """
    Per-agent provider + model_override should both work together.
    
    Scenario: Use Claude for smart, but with specific model override.
    """
    config = UserBotConfig(
        provider_preference="gemini",
        agent_providers={
            "smart": "claude"
        },
        model_overrides={
            "smart": "claude-sonnet-4-5"  # Override model
        },
        default_tier=PerformanceTier.PERFORMANCE
    )
    
    context = builder.build(agent_type="smart", config=config)
    
    # Should use Claude provider (per-agent)
    assert context.provider.get_capabilities().context_caching is True  # Claude capability
    # Model should be overridden
    assert context.model_name == "claude-sonnet-4-5"
