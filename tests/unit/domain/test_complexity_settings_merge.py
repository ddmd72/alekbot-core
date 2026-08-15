"""Unit tests for ``resolve_complexity_settings``.

This merge is the single source of truth for "which ComplexitySettings apply to
this user for this complexity". It exists because two call sites used to answer
that question differently: ``TaskExecutionResolver`` merged the user's
``complexity_settings_overrides``, while ``WorkerHandler`` read the raw defaults
table to size the reminder's SLA budget. On 2026-08-15 a user who had overridden
``simple_analytics`` to PERFORMANCE therefore ran PERFORMANCE work on a BALANCED
clock and was killed at 600s instead of 1500s.
"""
import pytest

from src.domain.complexity_settings import (
    ComplexitySettings,
    DEFAULT_COMPLEXITY_SETTINGS,
    resolve_complexity_settings,
)
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserBotConfig


class TestNoOverride:
    def test_returns_system_default(self):
        settings = resolve_complexity_settings(
            TaskComplexity.SIMPLE_ANALYTICS, UserBotConfig()
        )

        assert settings == DEFAULT_COMPLEXITY_SETTINGS[TaskComplexity.SIMPLE_ANALYTICS]
        assert settings.tier is PerformanceTier.BALANCED
        assert settings.thinking_effort == "low"

    @pytest.mark.parametrize("complexity", list(TaskComplexity))
    def test_every_complexity_resolves(self, complexity):
        assert resolve_complexity_settings(complexity, UserBotConfig()) is not None


class TestOverrideMerge:
    def test_full_override_wins_on_every_field(self):
        """The exact shape of the config that broke the briefing."""
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(
                    tier=PerformanceTier.PERFORMANCE,
                    thinking_effort="medium",
                    provider_override="grok",
                )
            }
        )

        settings = resolve_complexity_settings(TaskComplexity.SIMPLE_ANALYTICS, config)

        assert settings.tier is PerformanceTier.PERFORMANCE
        assert settings.thinking_effort == "medium"
        assert settings.provider_override == "grok"

    def test_partial_override_keeps_defaults_for_unset_fields(self):
        """tier only → the default thinking_effort ("low") must survive."""
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(
                    tier=PerformanceTier.ULTRA
                )
            }
        )

        settings = resolve_complexity_settings(TaskComplexity.SIMPLE_ANALYTICS, config)

        assert settings.tier is PerformanceTier.ULTRA
        assert settings.thinking_effort == "low"

    def test_override_for_another_complexity_does_not_leak(self):
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.DEEP_REASONING: ComplexitySettings(
                    tier=PerformanceTier.ULTRA, provider_override="grok"
                )
            }
        )

        settings = resolve_complexity_settings(TaskComplexity.SIMPLE_ANALYTICS, config)

        assert settings.tier is PerformanceTier.BALANCED
        assert settings.provider_override is None

    def test_does_not_mutate_the_defaults_table(self):
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.SMALL_TALK: ComplexitySettings(
                    tier=PerformanceTier.ULTRA
                )
            }
        )

        resolve_complexity_settings(TaskComplexity.SMALL_TALK, config)

        assert (
            DEFAULT_COMPLEXITY_SETTINGS[TaskComplexity.SMALL_TALK].tier
            is PerformanceTier.ECO
        )


class TestMissingDefault:
    def test_complexity_absent_from_the_table_returns_none(self, monkeypatch):
        from src.domain import complexity_settings as settings_module

        monkeypatch.setattr(settings_module, "DEFAULT_COMPLEXITY_SETTINGS", {})

        assert (
            resolve_complexity_settings(TaskComplexity.SMALL_TALK, UserBotConfig())
            is None
        )
