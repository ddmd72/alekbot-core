"""The memory-writing agents have no cross-provider fallback (owner ruling 2026-10-23, P9 D8).

Memory quality over availability: a Claude outage must fail and retry later, not write facts or companion records with
another model. `AgentContextBuilder.build` resolves a fallback only from the strategy's `fallback`, so `None` there means
`AgentExecutionContext.fallback_provider` is `None` for every user config.
"""
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.user import UserBotConfig
from src.ports.llm_port import LLMPort, ProviderCapabilities
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry

MEMORY_WRITERS = ("consolidation", "tutor_extractor")


def _provider(name: str) -> MagicMock:
    llm = MagicMock(spec=LLMPort)
    llm.get_model_for_tier.return_value = f"{name}-model"
    llm.get_capabilities.return_value = ProviderCapabilities(supports_native_tools=True, supports_context_caching=True)
    return llm


@pytest.mark.parametrize("agent_type", MEMORY_WRITERS)
def test_the_strategy_declares_no_fallback(agent_type):
    assert AgentProviderStrategy.STRATEGIES[agent_type]["fallback"] is None


@pytest.mark.parametrize("provider", ["claude", "gemini", "openai"])
@pytest.mark.parametrize("agent_type", MEMORY_WRITERS)
def test_no_provider_choice_resolves_a_fallback(agent_type, provider):
    registry = ProviderRegistry()
    for name in ("claude", "gemini", "openai"):
        llm = _provider(name)
        registry.register(name, llm)
    builder = AgentContextBuilder(registry, InMemoryProviderResilience())
    config = UserBotConfig(agent_providers={agent_type: provider})

    context = builder.build(agent_type, config)

    assert context.provider_name == provider
    assert context.fallback_provider is None and context.fallback_provider_name is None


def test_an_agent_that_does_declare_a_fallback_still_resolves_one():
    registry = ProviderRegistry()
    for name in ("openai", "gemini", "claude"):
        llm = _provider(name)
        registry.register(name, llm)
    builder = AgentContextBuilder(registry, InMemoryProviderResilience())

    context = builder.build("tutor", UserBotConfig())

    assert context.provider_name == "openai" and context.fallback_provider_name == "gemini"
