import pytest

from src.domain.user import (
    PerformanceTier, UserBotConfig, LLMProvider, PromptPreferences, _DEFAULT_AGENT_TIERS,
)
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS

# Agents that make no LLM call at all — a tier is meaningless for them, so they are
# deliberately absent from _DEFAULT_AGENT_TIERS. Verified by grep: neither module
# references _call_llm / self.llm / self._llm / LLMPort.
_ZERO_LLM_AGENT_TYPES = {
    "help",             # HelpAgent — static capabilities reference
    "file_management",  # FileManagementAgent — GCS download/delete only
}


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


def test_maps_search_defaults_to_balanced_tier():
    # maps_search pinned to BALANCED → gpt-5.4-mini on OpenAI. Resolved via class default
    # even for users whose stored agent_tiers predates the entry.
    assert UserBotConfig(agent_tiers={}).get_tier_for_agent("maps_search") == PerformanceTier.BALANCED
    assert UserBotConfig(agent_tiers=None).get_tier_for_agent("maps_search") == PerformanceTier.BALANCED


def test_user_bot_config_provider_defaults_intact():
    config = UserBotConfig()
    assert config.provider_preference is None


def test_every_llm_agent_has_a_default_tier():
    """
    Regression guard: every LLM-calling specialist in agent_manifest.ALL_DESCRIPTORS must
    have an entry in _DEFAULT_AGENT_TIERS, or get_tier_for_agent() silently falls through to
    self.default_tier (ECO unless the user configured otherwise) — no error, no log line.
    This exact gap shipped unnoticed for "compute", "tasks", and "image_generation" until
    2026-08-23 (NEW_AGENT_PLAYBOOK.md's "Which PerformanceTier?" question never told
    implementers to add the answer to this dict). See
    docs/04_solution_strategy/decisions/agent_tier_default_enforcement.md.
    """
    manifest_agent_types = {descriptor.agent_type for descriptor in ALL_DESCRIPTORS}
    llm_agent_types = manifest_agent_types - _ZERO_LLM_AGENT_TYPES

    missing = llm_agent_types - _DEFAULT_AGENT_TIERS.keys()
    assert not missing, (
        f"Agent type(s) {missing} make LLM calls but have no _DEFAULT_AGENT_TIERS entry in "
        "src/domain/user.py — their tier silently falls through to the user's default_tier. "
        "Add an entry there, or to _ZERO_LLM_AGENT_TYPES here if genuinely zero-LLM."
    )


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


def test_max_video_duration_s_defaults_to_none():
    config = UserBotConfig()
    assert config.max_video_duration_s is None


def test_max_video_duration_s_accepts_override():
    config = UserBotConfig(max_video_duration_s=20)
    assert config.max_video_duration_s == 20