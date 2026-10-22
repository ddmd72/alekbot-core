"""The drive specialist's model (DRIVE_SPECIALIST_RFC §5.1, owner decision 2026-10-22): claude-sonnet-5-5 at BALANCED,
adaptive thinking at medium, failing over to OpenAI at PERFORMANCE (gpt-6.1-sol, not BALANCED's luna). Resolved through
the real adapters' tier maps, so a tier remap that moves the specialist off the decided models fails here."""
from unittest.mock import MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.user import _DEFAULT_AGENT_TIERS, PerformanceTier, UserBotConfig
from src.infrastructure.agent_config import DRIVE
from src.services.agent_context_builder import AgentContextBuilder, AgentProviderStrategy
from src.services.provider_registry import ProviderRegistry


def _builder(monkeypatch):
    for knob in ("CLAUDE_SMALL_MODEL", "CLAUDE_BALANCED_MODEL", "OPENAI_TIER_OVERRIDES", "CLAUDE_PERFORMANCE_MODEL"):
        monkeypatch.delenv(knob, raising=False)
    registry = ProviderRegistry()
    registry.register("claude", ClaudeAdapter(api_key="k"))
    registry.register("openai", OpenAIAdapter(api_key="k"))
    registry.register("gemini", GeminiAdapter(api_key="k"))
    resilience = MagicMock()
    resilience.is_provider_open.return_value = False
    return AgentContextBuilder(registry, resilience_port=resilience)


def test_the_default_is_claude_sonnet_at_balanced_with_the_sol_fallback(monkeypatch):
    ctx = _builder(monkeypatch).build("drive", UserBotConfig())
    assert (ctx.provider_name, ctx.tier, ctx.model_name) == ("claude", PerformanceTier.BALANCED, "claude-sonnet-5-5")
    assert (ctx.fallback_provider_name, ctx.fallback_model_name) == ("openai", "gpt-6.1-sol")


def test_a_user_switch_to_openai_has_no_fallback_to_itself(monkeypatch):
    ctx = _builder(monkeypatch).build("drive", UserBotConfig(agent_providers={"drive": "openai"}))
    assert (ctx.provider_name, ctx.model_name) == ("openai", "gpt-6-luna")
    assert ctx.fallback_provider is None


def test_the_strategy_tier_and_config_entries():
    strategy = AgentProviderStrategy.get_strategy("drive")
    assert strategy["allowed_providers"] == ["claude", "openai"] and strategy["required_capabilities"] == ["native_tools"]
    assert strategy["fallback_tier"] is PerformanceTier.PERFORMANCE
    assert _DEFAULT_AGENT_TIERS["drive"] is PerformanceTier.BALANCED
    assert (DRIVE.max_turns, DRIVE.timeout_ms, DRIVE.soft_deadline_s, DRIVE.thinking_effort) == (6, 180_000, 150.0,
                                                                                                "medium")
