"""The email card writer's routing (slice 3): Gemini only and no cross-provider fallback for any user configuration (a
fallback model would write cards of another measured quality; an outage leaves the card pending), a default tier that
satisfies the invariant."""
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.user import PerformanceTier, UserBotConfig, _DEFAULT_AGENT_TIERS
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry


def _provider(name: str) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.get_model_for_tier.return_value = f"{name}-model"
    llm.get_capabilities.return_value = ProviderCapabilities(native_tools=True, context_caching=True)
    return llm


def test_the_strategy_is_gemini_only_without_fallback_or_required_capabilities():
    entry = AgentProviderStrategy.STRATEGIES["email_card"]
    assert entry["default_provider"] == "gemini" and entry["allowed_providers"] == ["gemini"]
    assert entry["required_capabilities"] == [] and entry["fallback"] is None


@pytest.mark.parametrize("provider", ["claude", "gemini", "openai", None])
def test_no_user_configuration_resolves_a_fallback_or_another_provider(provider):
    registry = ProviderRegistry()
    for name in ("claude", "gemini", "openai"):
        registry.register(name, _provider(name))
    config = UserBotConfig(agent_providers={"email_card": provider} if provider else {})

    context = AgentContextBuilder(registry, InMemoryProviderResilience()).build("email_card", config)

    assert context.provider_name == "gemini"
    assert context.fallback_provider is None and context.fallback_provider_name is None


def test_the_default_tier_is_eco():
    assert _DEFAULT_AGENT_TIERS["email_card"] is PerformanceTier.ECO
