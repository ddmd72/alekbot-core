"""The drive agents' providers after the owner's live check (2026-10-13, USER_DRIVE_INDEXING_RFC §24.2 D7 rev 3.17):
both default to OpenAI (gpt-6-luna at ECO), and both fail over to Gemini Flash — gemini-3.8-flash, Gemini's
BALANCED pin, not ECO's flash-lite — through the strategy's `fallback_tier`. Resolved through the real adapters' tier
maps, so a tier remap that moves these agents off the checked models fails here."""
from unittest.mock import MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.user import PerformanceTier, UserBotConfig
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry

AGENTS = ["drive_reader", "drive_triage", "drive_profiler", "drive_passages"]   # + Search v2 (§25.4.4)


def _builder(monkeypatch, *, gemini=True):
    monkeypatch.delenv("CLAUDE_SMALL_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)
    registry = ProviderRegistry()
    registry.register("claude", ClaudeAdapter(api_key="k"))
    registry.register("openai", OpenAIAdapter(api_key="k"))
    if gemini:
        registry.register("gemini", GeminiAdapter(api_key="k"))
    resilience = MagicMock()
    resilience.is_provider_open.return_value = False
    return AgentContextBuilder(registry, resilience_port=resilience)


@pytest.mark.parametrize("agent_type", AGENTS)
def test_default_is_openai_gpt_6_luna_at_eco(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert (ctx.provider_name, ctx.tier, ctx.model_name) == ("openai", PerformanceTier.ECO, "gpt-6-luna")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_fallback_is_gemini_flash_not_flash_lite(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig())
    assert ctx.fallback_provider_name == "gemini"
    assert isinstance(ctx.fallback_provider, GeminiAdapter)
    assert ctx.fallback_model_name == "gemini-3.8-flash"
    assert ctx.tier is PerformanceTier.ECO   # the primary keeps its own tier


@pytest.mark.parametrize("agent_type", AGENTS)
def test_a_user_switch_to_claude_keeps_the_gemini_fallback(monkeypatch, agent_type):
    ctx = _builder(monkeypatch).build(agent_type, UserBotConfig(agent_providers={agent_type: "claude"}))
    assert (ctx.provider_name, ctx.model_name) == ("claude", "claude-haiku-5-5")
    assert (ctx.fallback_provider_name, ctx.fallback_model_name) == ("gemini", "gemini-3.8-flash")


@pytest.mark.parametrize("agent_type", AGENTS)
def test_no_gemini_key_means_no_fallback_and_the_primary_still_builds(monkeypatch, agent_type):
    ctx = _builder(monkeypatch, gemini=False).build(agent_type, UserBotConfig())
    assert ctx.provider_name == "openai"
    assert ctx.fallback_provider is None and ctx.fallback_model_name is None


@pytest.mark.parametrize("agent_type", AGENTS)
def test_strategy_entry(agent_type):
    strategy = AgentProviderStrategy.get_strategy(agent_type)
    assert strategy["default_provider"] == "openai"
    assert strategy["fallback"] == "gemini" and strategy["fallback"] not in strategy["allowed_providers"]
    assert strategy["fallback_tier"] is PerformanceTier.BALANCED
    assert strategy["allowed_providers"] == ["claude", "openai"]   # Gemini is the fallback only, never a primary


def test_fallback_tier_is_the_primary_tier_when_the_strategy_names_none(monkeypatch):
    """Every other strategy keeps today's behaviour: the fallback resolves at the primary's tier."""
    assert "fallback_tier" not in AgentProviderStrategy.get_strategy("tutor")
    ctx = _builder(monkeypatch).build("tutor", UserBotConfig())
    assert ctx.fallback_provider_name == "gemini"
    assert ctx.fallback_model_name == GeminiAdapter.MODEL_TIERS[ctx.tier]


def test_only_the_drive_agents_name_a_fallback_tier():
    named = {name for name, s in AgentProviderStrategy.STRATEGIES.items() if "fallback_tier" in s}
    assert named == set(AGENTS) | {"drive"}
