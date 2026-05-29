"""
Unit tests for AgentContextBuilder per-agent provider selection (3-level resolution).

Tests the new agent_providers field that allows different providers for different agents.
"""

import pytest
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry
from src.domain.complexity_settings import ComplexitySettings
from src.domain.user import UserBotConfig, PerformanceTier
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from unittest.mock import Mock


@pytest.fixture
def mock_gemini_adapter():
    """Mock Gemini adapter."""
    adapter = Mock(spec=LLMPort)
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
    adapter = Mock(spec=LLMPort)
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
    adapter = Mock(spec=LLMPort)
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
    return AgentContextBuilder(registry, resilience_port=InMemoryProviderResilience())


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

    Scenario: User sets Grok as global preference, but postprocessing only allows Gemini.
    """
    config = UserBotConfig(
        provider_preference="grok",  # NOT allowed for postprocessing
        default_tier=PerformanceTier.BALANCED
    )

    context = builder.build(agent_type="postprocessing", config=config)

    # Should use strategy default (Gemini), NOT invalid Grok preference
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
        default_tier=PerformanceTier.BALANCED
    )

    strategy = AgentProviderStrategy.get_strategy("custom_agent")
    quick_strategy = AgentProviderStrategy.STRATEGIES["quick"]
    assert strategy is quick_strategy


def test_facts_memory_uses_gemini_not_quick_fallback(builder):
    """
    facts_memory must resolve to gemini, not fall through to quick (claude).

    Regression guard: before this strategy was added, facts_memory silently
    inherited quick's default (claude). Claude ignores response_mime_type →
    LLM returns markdown-wrapped JSON → parse failure → degraded search.
    """
    config = UserBotConfig(default_tier=PerformanceTier.ECO)

    context = builder.build(agent_type="facts_memory", config=config)

    # Must be gemini (native_tools=True, context_caching=False), not claude
    caps = context.provider.get_capabilities()
    assert caps.context_caching is False  # gemini, not claude


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


# ============================================================================
# Two-layer override precedence: per-complexity (resolve_for_task) wins over
# per-agent (resolve_provider_name). See decisions/two_layer_provider_override.md.
# ============================================================================

def test_per_complexity_provider_override_wins_over_per_agent(builder):
    """When both layers are configured, ComplexitySettings.provider_override wins.

    Coarse layer: agent_providers["smart"] = "gemini"  (per-agent).
    Fine layer:   settings.provider_override = "claude"  (per-complexity).
    Expected: resolve_for_task returns Claude context. The fine layer overrides
    the coarse layer at task-execution time — the documented antipattern of
    two parallel knobs is intentional; this test pins the precedence so future
    refactors cannot silently invert it.
    """
    config = UserBotConfig(
        agent_providers={"smart": "gemini"},
        default_tier=PerformanceTier.PERFORMANCE,
    )
    settings = ComplexitySettings(
        tier=PerformanceTier.PERFORMANCE,
        provider_override="claude",
    )

    context = builder.resolve_for_task(agent_type="smart", config=config, settings=settings)

    assert context.capabilities.context_caching is True  # Claude capability fingerprint


def test_per_agent_used_when_complexity_override_absent(builder):
    """Without a per-complexity provider_override, per-agent layer applies."""
    config = UserBotConfig(
        agent_providers={"smart": "claude"},
        default_tier=PerformanceTier.PERFORMANCE,
    )
    settings = ComplexitySettings(tier=PerformanceTier.PERFORMANCE)  # no provider_override

    context = builder.resolve_for_task(agent_type="smart", config=config, settings=settings)

    assert context.capabilities.context_caching is True  # Claude (from per-agent)


# ---------------------------------------------------------------------------
# Guard: every agent_type used in UserAgentFactory must have a STRATEGIES entry
# ---------------------------------------------------------------------------

# All agent_types passed to context_builder.build() in UserAgentFactory.
# This list is the single source of truth for the guard test.
# Update when adding a new agent to UserAgentFactory.
_FACTORY_AGENT_TYPES = [
    "router",
    "quick",
    "smart",
    "consolidation",
    "postprocessing",
    "notes",
    "facts_memory",
    "web_search",
    "email_search",
    "maps_search",
    "compute",
    "tasks",
    "doc_generator",
    "doc_planner",
    "pdf_generator",
    "html_page",
    "domain_researcher",
]


def test_all_factory_agent_types_have_strategy():
    """Every agent_type used in UserAgentFactory must have an explicit STRATEGIES entry.

    Silent fallback to 'quick' strategy caused a production regression when quick's
    default_provider changed from gemini to claude (facts_memory agent broke because
    Claude ignores response_mime_type). This test prevents that class of bug.
    """
    missing = [
        t for t in _FACTORY_AGENT_TYPES
        if t not in AgentProviderStrategy.STRATEGIES
    ]
    assert missing == [], (
        f"Agent types used in UserAgentFactory but missing from "
        f"AgentProviderStrategy.STRATEGIES: {missing}. "
        f"Add an explicit strategy entry for each."
    )


def test_strategies_has_no_orphans():
    """Every STRATEGIES entry should be used somewhere (factory, ServiceContainer, or docs).

    Entries that exist only in STRATEGIES are dead code or documentation-only.
    Keep this list explicit — known non-factory entries must be listed.
    """
    # Agent types that are in STRATEGIES but not in UserAgentFactory.build():
    # - email_classifier: created in ServiceContainer (singleton, not per-user)
    # - deep_research: uses DeepResearchPort, not LLMPort; entry is for provider resolution
    _NON_FACTORY_STRATEGIES = {"email_classifier", "deep_research"}

    all_strategies = set(AgentProviderStrategy.STRATEGIES.keys())
    factory_set = set(_FACTORY_AGENT_TYPES)
    orphans = all_strategies - factory_set - _NON_FACTORY_STRATEGIES

    assert orphans == set(), (
        f"STRATEGIES entries not used in factory and not in the known exceptions list: {orphans}. "
        f"Either add to _FACTORY_AGENT_TYPES or _NON_FACTORY_STRATEGIES."
    )
