"""The fact profiler's routing (FACT_PROFILE_RFC §4.1): Claude only and no cross-provider fallback for any user
configuration (owner ruling 2026-10-23, P9 D8: memory quality over availability), the static prompt cached on Claude
for a single user turn, a default tier that satisfies the invariant."""
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.user import PerformanceTier, UserBotConfig, _DEFAULT_AGENT_TIERS
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.prompt_cache_strategy import PromptCacheStrategy
from src.services.provider_registry import ProviderRegistry


def _provider(name: str) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.get_model_for_tier.return_value = f"{name}-model"
    llm.get_capabilities.return_value = ProviderCapabilities(native_tools=True, context_caching=True)
    return llm


def test_the_strategy_is_claude_only_without_fallback_or_required_capabilities():
    entry = AgentProviderStrategy.STRATEGIES["fact_profiler"]
    assert entry["default_provider"] == "claude" and entry["allowed_providers"] == ["claude"]
    assert entry["required_capabilities"] == [] and entry["fallback"] is None


@pytest.mark.parametrize("provider", ["claude", "gemini", "openai", None])
def test_no_user_configuration_resolves_a_fallback_or_another_provider(provider):
    registry = ProviderRegistry()
    for name in ("claude", "gemini", "openai"):
        registry.register(name, _provider(name))
    builder = AgentContextBuilder(registry, InMemoryProviderResilience())
    config = UserBotConfig(agent_providers={"fact_profiler": provider} if provider else {})

    context = builder.build("fact_profiler", config)

    assert context.provider_name == "claude"
    assert context.fallback_provider is None and context.fallback_provider_name is None


def test_the_cache_is_on_for_claude_with_the_static_part_only():
    config = PromptCacheStrategy().resolve("fact_profiler", ProviderCapabilities(context_caching=True))
    assert config is not None and config.enabled and config.cache_last_message is False


def test_the_cache_is_off_where_the_provider_cannot_cache():
    assert PromptCacheStrategy().resolve("fact_profiler", ProviderCapabilities(context_caching=False)) is None


def test_the_default_tier_is_balanced():
    assert _DEFAULT_AGENT_TIERS["fact_profiler"] is PerformanceTier.BALANCED
