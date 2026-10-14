"""Provider and tier resolution of the two drive indexer agents (USER_DRIVE_INDEXING_RFC §24.2, D7 rev 3.17): OpenAI
by default, Claude by a user's switch, ECO tier, Gemini Flash (BALANCED) as the fallback only — never a primary, even
when its adapter is registered and the user's global preference names it. Resolved through the real adapters' tier
maps, so a tier remap that moves these agents off the checked models fails here."""
from unittest.mock import MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.agent_config import DRIVE_READER, DRIVE_TRIAGE
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry

AGENTS = ["drive_reader", "drive_triage"]


def _builder(monkeypatch):
    monkeypatch.delenv("CLAUDE_SMALL_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)
    registry = ProviderRegistry()
    registry.register("claude", ClaudeAdapter(api_key="k"))
    registry.register("openai", OpenAIAdapter(api_key="k"))
    registry.register("gemini", GeminiAdapter(api_key="k"))   # available, so only the strategy keeps it off primary
    resilience = MagicMock()
    resilience.is_provider_open.return_value = False
    return AgentContextBuilder(registry, resilience_port=resilience)


@pytest.mark.parametrize("agent_type", AGENTS)
def test_default_is_openai_gpt_6_luna_at_eco(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert (ctx.provider_name, ctx.tier, ctx.model_name) == ("openai", PerformanceTier.ECO, "gpt-6-luna")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_a_user_switch_to_openai_resolves_gpt_6_luna(monkeypatch, agent_type):
    config = UserBotConfig(agent_providers={agent_type: "openai"})
    ctx = _builder(monkeypatch).build(agent_type, config)
    assert (ctx.provider_name, ctx.model_name) == ("openai", "gpt-6-luna")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_gemini_is_never_used_even_as_a_global_preference(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig(provider_preference="gemini"))
    assert (ctx.provider_name, ctx.model_name) == ("openai", "gpt-6-luna")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_gemini_is_the_fallback_only_at_balanced(monkeypatch, agent_type):
    strategy = AgentProviderStrategy.get_strategy(agent_type)
    assert strategy["allowed_providers"] == ["claude", "openai"] and strategy["fallback"] == "gemini"
    assert strategy["fallback_tier"] is PerformanceTier.BALANCED
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert (ctx.fallback_provider_name, ctx.fallback_model_name) == ("gemini", "gemini-3.8-flash")
    assert isinstance(ctx.fallback_provider, GeminiAdapter)


def test_thinking_defaults_to_low_on_both():
    assert DRIVE_READER.thinking == "low" and DRIVE_TRIAGE.thinking == "low"
