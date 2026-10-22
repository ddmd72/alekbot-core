"""The drive specialist's model (DRIVE_SPECIALIST_RFC §5.1, owner decision 2026-10-22): claude-sonnet-5-5 at BALANCED,
failing over to OpenAI resolved at PERFORMANCE (gpt-6.1-sol; OpenAI's BALANCED is luna, rejected). Resolved through
the real adapters' tier maps, so a tier remap that moves the specialist off the chosen models fails here."""
from unittest.mock import MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.user import PerformanceTier, UserBotConfig
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry


def _builder(monkeypatch):
    monkeypatch.delenv("CLAUDE_SMALL_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)
    registry = ProviderRegistry()
    registry.register("claude", ClaudeAdapter(api_key="k"))
    registry.register("openai", OpenAIAdapter(api_key="k"))
    registry.register("gemini", GeminiAdapter(api_key="k"))
    resilience = MagicMock()
    resilience.is_provider_open.return_value = False
    return AgentContextBuilder(registry, resilience_port=resilience)


def test_default_is_claude_sonnet_5_5_at_balanced(monkeypatch):
    ctx = _builder(monkeypatch).build("drive", UserBotConfig())
    assert (ctx.provider_name, ctx.tier, ctx.model_name) == ("claude", PerformanceTier.BALANCED, "claude-sonnet-5-5")


def test_fallback_is_openai_gpt_6_1_sol_not_luna(monkeypatch):
    ctx = _builder(monkeypatch).build("drive", UserBotConfig())
    assert ctx.fallback_provider_name == "openai" and isinstance(ctx.fallback_provider, OpenAIAdapter)
    assert ctx.fallback_model_name == "gpt-6.1-sol"


def test_strategy_entry():
    strategy = AgentProviderStrategy.get_strategy("drive")
    assert strategy["default_provider"] == "claude"
    assert strategy["allowed_providers"] == ["claude", "openai"]
    assert strategy["required_capabilities"] == ["native_tools"]
    assert strategy["fallback"] == "openai" and strategy["fallback_tier"] is PerformanceTier.PERFORMANCE


def test_the_default_tier_is_balanced_for_users_whose_stored_tiers_predate_it():
    assert UserBotConfig().get_tier_for_agent("drive") is PerformanceTier.BALANCED
    assert UserBotConfig(agent_tiers={"smart": PerformanceTier.ECO}).get_tier_for_agent("drive") is PerformanceTier.BALANCED
