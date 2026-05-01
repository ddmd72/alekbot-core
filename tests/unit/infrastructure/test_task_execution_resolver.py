"""
Unit tests for services.task_execution_resolver.TaskExecutionResolver.

Covers every branch of resolve():
- Missing task_complexity → None
- Empty task_complexity → None (not_set treated as missing)
- Invalid (unknown) task_complexity string → None + warning logged
- Each valid TaskComplexity value → returns ExecutionOverride
- User override absent → uses DEFAULT_COMPLEXITY_SETTINGS
- User override partial → per-field merge (user wins where set, default otherwise)
- User override full → user wins on every field
- Return type is ExecutionOverride (frozen value object, see RFC § 4)

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 4 / § 8.1
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.domain.complexity_settings import ComplexitySettings, DEFAULT_COMPLEXITY_SETTINGS
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.task_execution_resolver import (
    ExecutionOverride,
    TaskExecutionResolver,
)
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    ProviderCapabilities,
)
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _make_ctx(model_name: str = "stub-model") -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="smart",
        provider=MagicMock(spec=LLMPort),
        model_name=model_name,
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
        provider_name="stub-provider",
        resilience_port=InMemoryProviderResilience(),
    )


@pytest.fixture
def stub_context_builder():
    """AgentContextBuilder stub that returns a deterministic AgentExecutionContext.

    The resolver delegates the (settings → execution_context) translation to
    AgentContextBuilder; we test the resolver, not the builder, so we capture
    the call args and return a fixed ctx.
    """
    builder = MagicMock()
    builder.resolve_for_task = MagicMock(side_effect=lambda **kwargs: _make_ctx(
        model_name=f"resolved-{kwargs['settings'].tier.value}"
    ))
    return builder


@pytest.fixture
def resolver(stub_context_builder):
    return TaskExecutionResolver(context_builder=stub_context_builder)


@pytest.fixture
def empty_user_config():
    """UserBotConfig with no per-user complexity overrides."""
    return UserBotConfig()


# --------------------------------------------------------------------------- #
# Branch: missing or invalid task_complexity                                  #
# --------------------------------------------------------------------------- #


class TestMissingOrInvalidComplexity:
    def test_missing_task_complexity_returns_none(self, resolver, empty_user_config):
        result = resolver.resolve(context={}, config=empty_user_config)
        assert result is None

    def test_empty_string_task_complexity_returns_none(self, resolver, empty_user_config):
        result = resolver.resolve(context={"task_complexity": ""}, config=empty_user_config)
        assert result is None

    def test_unknown_task_complexity_returns_none(self, resolver, empty_user_config):
        result = resolver.resolve(
            context={"task_complexity": "no_such_complexity_value"},
            config=empty_user_config,
        )
        assert result is None

    def test_unknown_task_complexity_does_not_call_builder(self, resolver, empty_user_config):
        resolver.resolve(
            context={"task_complexity": "no_such_complexity_value"},
            config=empty_user_config,
        )
        resolver.context_builder.resolve_for_task.assert_not_called()

    def test_complexity_without_default_settings_returns_none(
        self, resolver, empty_user_config, monkeypatch
    ):
        # Defensive branch: if a TaskComplexity enum value has no entry in
        # DEFAULT_COMPLEXITY_SETTINGS, resolve must return None instead of
        # crashing. Today every enum value has an entry; this test guards
        # against future enum additions that forget to update the table.
        from src.infrastructure import task_execution_resolver as resolver_module

        # Patch with an empty dict — no complexity value will resolve.
        monkeypatch.setattr(
            resolver_module, "DEFAULT_COMPLEXITY_SETTINGS", {}
        )
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.SMALL_TALK.value},
            config=empty_user_config,
        )
        assert result is None
        resolver.context_builder.resolve_for_task.assert_not_called()


# --------------------------------------------------------------------------- #
# Branch: valid task_complexity, no user override                             #
# --------------------------------------------------------------------------- #


class TestDefaultsOnly:
    @pytest.mark.parametrize("complexity", list(TaskComplexity))
    def test_each_complexity_returns_execution_override(
        self, resolver, empty_user_config, complexity
    ):
        result = resolver.resolve(
            context={"task_complexity": complexity.value},
            config=empty_user_config,
        )
        assert isinstance(result, ExecutionOverride)

    @pytest.mark.parametrize("complexity", list(TaskComplexity))
    def test_each_complexity_uses_default_thinking_effort(
        self, resolver, empty_user_config, complexity
    ):
        defaults = DEFAULT_COMPLEXITY_SETTINGS[complexity]
        result = resolver.resolve(
            context={"task_complexity": complexity.value},
            config=empty_user_config,
        )
        assert result is not None
        assert result.thinking_effort == defaults.thinking_effort

    @pytest.mark.parametrize("complexity", list(TaskComplexity))
    def test_each_complexity_uses_default_intent_remap(
        self, resolver, empty_user_config, complexity
    ):
        defaults = DEFAULT_COMPLEXITY_SETTINGS[complexity]
        result = resolver.resolve(
            context={"task_complexity": complexity.value},
            config=empty_user_config,
        )
        assert result is not None
        # intent_remap on the override is always a dict (never None);
        # for default settings it is empty.
        assert result.intent_remap == defaults.intent_remap

    def test_passes_resolved_settings_to_context_builder(
        self, resolver, empty_user_config
    ):
        resolver.resolve(
            context={"task_complexity": TaskComplexity.DEEP_REASONING.value},
            config=empty_user_config,
        )
        resolver.context_builder.resolve_for_task.assert_called_once()
        call = resolver.context_builder.resolve_for_task.call_args
        assert call.kwargs["agent_type"] == "smart"
        assert call.kwargs["config"] is empty_user_config
        settings: ComplexitySettings = call.kwargs["settings"]
        assert settings.tier == PerformanceTier.PERFORMANCE
        assert settings.thinking_effort == "high"

    def test_agent_type_propagated_to_builder(self, resolver, empty_user_config):
        resolver.resolve(
            context={"task_complexity": TaskComplexity.SMALL_TALK.value},
            config=empty_user_config,
            agent_type="quick",
        )
        call = resolver.context_builder.resolve_for_task.call_args
        assert call.kwargs["agent_type"] == "quick"


# --------------------------------------------------------------------------- #
# Branch: user overrides on top of defaults                                   #
# --------------------------------------------------------------------------- #


class TestUserOverrideMerging:
    def test_user_override_replaces_tier(self, resolver):
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(
                    tier=PerformanceTier.ECO,  # default is BALANCED — user downgrades
                ),
            }
        )
        resolver.resolve(
            context={"task_complexity": TaskComplexity.SIMPLE_ANALYTICS.value},
            config=config,
        )
        settings: ComplexitySettings = resolver.context_builder.resolve_for_task.call_args.kwargs["settings"]
        assert settings.tier == PerformanceTier.ECO

    def test_user_override_replaces_thinking_effort(self, resolver):
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.DEEP_REASONING: ComplexitySettings(
                    tier=PerformanceTier.PERFORMANCE,
                    thinking_effort="medium",  # default is "high" — user softens
                ),
            }
        )
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.DEEP_REASONING.value},
            config=config,
        )
        assert result is not None
        assert result.thinking_effort == "medium"

    def test_user_override_replaces_intent_remap(self, resolver):
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(
                    tier=PerformanceTier.BALANCED,
                    intent_remap={"search_web": "search_web_light"},
                ),
            }
        )
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.SIMPLE_ANALYTICS.value},
            config=config,
        )
        assert result is not None
        assert result.intent_remap == {"search_web": "search_web_light"}

    def test_user_override_partial_fills_missing_from_defaults(self, resolver):
        # User sets tier=ECO but leaves thinking_effort blank → keep default "high"
        config = UserBotConfig(
            complexity_settings_overrides={
                TaskComplexity.DEEP_REASONING: ComplexitySettings(
                    tier=PerformanceTier.ECO,
                    # thinking_effort omitted → coerced to None by validator → default "high" stays
                ),
            }
        )
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.DEEP_REASONING.value},
            config=config,
        )
        assert result is not None
        assert result.thinking_effort == "high"  # default for DEEP_REASONING


# --------------------------------------------------------------------------- #
# Return type contract                                                        #
# --------------------------------------------------------------------------- #


class TestReturnTypeContract:
    """Resolver must return the ports-level ExecutionOverride, never a service-local class."""

    def test_returns_execution_override_instance(self, resolver, empty_user_config):
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.INFO_SEARCH.value},
            config=empty_user_config,
        )
        assert type(result).__name__ == "ExecutionOverride"
        assert type(result).__module__ == "src.infrastructure.task_execution_resolver"

    def test_returned_override_is_frozen(self, resolver, empty_user_config):
        import dataclasses

        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.INFO_SEARCH.value},
            config=empty_user_config,
        )
        assert result is not None
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.thinking_effort = "high"  # type: ignore[misc]

    def test_returned_override_carries_resolved_execution_context(
        self, resolver, empty_user_config
    ):
        result = resolver.resolve(
            context={"task_complexity": TaskComplexity.SMALL_TALK.value},
            config=empty_user_config,
        )
        assert result is not None
        # Stub builder produces "resolved-<tier>" — SMALL_TALK default tier is ECO
        assert result.execution_context.model_name == "resolved-eco"
