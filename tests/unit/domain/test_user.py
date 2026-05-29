import pytest

from src.domain.user import PerformanceTier, UserBotConfig, LLMProvider, PromptPreferences


def test_performance_tier_enum_values():
    assert PerformanceTier.ECO == "eco"
    assert PerformanceTier.BALANCED == "balanced"
    assert PerformanceTier.PERFORMANCE == "performance"


def test_user_bot_config_tier_defaults():
    config = UserBotConfig()
    assert config.default_tier == PerformanceTier.ECO
    assert config.agent_tiers


def test_get_tier_for_agent_returns_specific_tier():
    config = UserBotConfig()
    assert config.get_tier_for_agent("smart") == PerformanceTier.PERFORMANCE


def test_get_tier_for_agent_falls_back_to_class_defaults_when_key_missing():
    # Simulates a user with stale stored agent_tiers that lacks a new agent type.
    config = UserBotConfig(agent_tiers={})
    # "quick" has a class-level default of ECO, not default_tier (ECO).
    assert config.get_tier_for_agent("quick") == PerformanceTier.ECO


def test_get_tier_for_agent_returns_default_tier_for_unknown_agent():
    config = UserBotConfig(agent_tiers=None)
    assert config.get_tier_for_agent("unknown_agent_xyz") == PerformanceTier.ECO


def test_get_tier_for_agent_returns_default_when_none():
    config = UserBotConfig(agent_tiers=None)
    # "router" is in _DEFAULT_AGENT_TIERS as ECO — result is the same but via class default
    assert config.get_tier_for_agent("router") == PerformanceTier.ECO


def test_user_bot_config_provider_defaults_intact():
    config = UserBotConfig()
    assert config.provider_preference is None


def test_prompt_preferences_defaults():
    prefs = PromptPreferences()
    assert prefs.custom_kernel_id is None
    assert prefs.custom_kernel_light_id is None
    assert prefs.custom_examples_id is None
    assert prefs.custom_anchors_id is None
    assert prefs.custom_instructions is None
    assert prefs.language == "uk"
    assert prefs.vibe == "friendly"


def test_user_bot_config_prompt_preferences_default():
    config = UserBotConfig()
    assert isinstance(config.prompt_preferences, PromptPreferences)
    assert config.prompt_preferences.language == "uk"


def test_prompt_preferences_custom_kernel():
    prefs = PromptPreferences(custom_kernel_id="custom_kernel_1")
    assert prefs.custom_kernel_id == "custom_kernel_1"