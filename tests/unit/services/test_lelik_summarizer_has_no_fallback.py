"""The end-of-call summarizer has no cross-provider fallback (owner ruling 2026-10-23, P9 D8).

A lost call note is the accepted trade-off: the failure posts an ops alert (voice_control_plane_app) and a second
summarizer model is not worth its prompt and quality work. Before its own strategy entry, `lelik_summarizer` inherited
`quick` (claude, falls back to gemini); the entry keeps quick's providers so the primary model does not change.
"""
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.user import UserBotConfig
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry

PROVIDERS = ("claude", "gemini", "openai", "grok")


def _builder() -> AgentContextBuilder:
    registry = ProviderRegistry()
    for name in PROVIDERS:
        llm = MagicMock(spec=LLMPort)
        llm.get_model_for_tier.return_value = f"{name}-model"
        llm.get_capabilities.return_value = ProviderCapabilities(supports_native_tools=True)
        registry.register(name, llm)
    return AgentContextBuilder(registry, InMemoryProviderResilience())


def test_the_summarizer_has_its_own_strategy_without_a_fallback():
    assert AgentProviderStrategy.get_strategy("lelik_summarizer") is AgentProviderStrategy.STRATEGIES["lelik_summarizer"]
    assert AgentProviderStrategy.STRATEGIES["lelik_summarizer"]["fallback"] is None


def test_the_primary_providers_are_the_ones_it_inherited_from_quick():
    own, quick = AgentProviderStrategy.STRATEGIES["lelik_summarizer"], AgentProviderStrategy.STRATEGIES["quick"]
    for key in ("default_provider", "allowed_providers", "required_capabilities"):
        assert own[key] == quick[key]


def test_the_default_config_runs_on_claude_with_no_fallback():
    context = _builder().build("lelik_summarizer", UserBotConfig())

    assert context.provider_name == "claude"
    assert context.fallback_provider is None and context.fallback_provider_name is None


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_provider_choice_resolves_a_fallback(provider):
    context = _builder().build("lelik_summarizer", UserBotConfig(agent_providers={"lelik_summarizer": provider}))

    assert context.provider_name == provider
    assert context.fallback_provider is None and context.fallback_provider_name is None
