"""
Unit tests for SmartResponseAgent per-call execution context resolution.

Covers Step #2 of NOTIFICATION_DELIVERY_REFACTOR_RFC:
- ``_resolve_effective`` priority chain:
    1. Explicit ``ExecutionOverride`` on ``message.context["execution_override"]``
    2. ``TaskExecutionResolver.resolve`` (task_complexity)
    3. Agent defaults (``self.execution_context``, ``self._default_thinking_effort``)
- ``thinking_effort`` resolution sub-rules (override wins; otherwise context;
  otherwise default).
- Negative invariants: ``_execute_lock`` is gone, ``self.llm`` /
  ``self.model_name`` / ``self._agent_execution_context`` are not mutated
  by ``execute()``.

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 4 / § 8.1
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.core.smart_response_agent import (
    SmartResponseAgent,
    _EffectiveExecution,
)
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
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


def _make_ctx(
    *,
    model_name: str = "default-model",
    tier: PerformanceTier = PerformanceTier.BALANCED,
) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="smart",
        provider=MagicMock(spec=LLMPort),
        model_name=model_name,
        tier=tier,
        capabilities=ProviderCapabilities(),
        provider_name="stub",
        resilience_port=InMemoryProviderResilience(),
    )


@pytest.fixture
def default_ctx():
    return _make_ctx(model_name="default-model", tier=PerformanceTier.BALANCED)


@pytest.fixture
def smart(default_ctx):
    """SmartResponseAgent with stubbed dependencies and a mock resolver."""
    config = AgentConfig(
        agent_id="smart_response_agent",
        agent_type="smart_response",
        llm_model="default-model",
        timeout_ms=300_000,
        capabilities=["complex_reasoning"],
        metadata={"user_id": "user-123"},
    )

    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None

    session_store = MagicMock()
    session_store.load_session = AsyncMock(return_value=None)

    prompt_builder = MagicMock()
    prompt_builder.build_for_agent = AsyncMock(return_value="SYSTEM")

    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()

    return SmartResponseAgent(
        config=config,
        execution_context=default_ctx,
        session_store=session_store,
        prompt_builder=prompt_builder,
        resolver=resolver,
        user_config=UserBotConfig(),
        coordinator=coordinator,
        thinking_effort="medium",
    )


def _make_message(
    *,
    execution_override: Optional[ExecutionOverride] = None,
    task_complexity: Optional[str] = None,
    thinking_effort: Optional[str] = None,
) -> AgentMessage:
    context: dict = {
        "session_id": "s",
        "user_id": "user-123",
        "account_id": "acc-123",
    }
    if execution_override is not None:
        context["execution_override"] = execution_override
    if task_complexity is not None:
        context["task_complexity"] = task_complexity
    if thinking_effort is not None:
        context["thinking_effort"] = thinking_effort
    return AgentMessage.create(
        sender="notification_service",
        recipient="smart_response_agent",
        intent=AgentIntent.QUERY,
        payload={"text": "hi"},
        context=context,
    )


# --------------------------------------------------------------------------- #
# Priority chain                                                              #
# --------------------------------------------------------------------------- #


class TestResolveEffectivePriorityChain:
    """The three sources of execution params, in strict priority order."""

    def test_explicit_override_wins_over_resolver(self, smart):
        # Explicit override carries provider P1 / model M1.
        explicit_ctx = _make_ctx(model_name="explicit-model")
        explicit = ExecutionOverride(
            execution_context=explicit_ctx, thinking_effort="high"
        )

        # Resolver would have proposed a different one — must be IGNORED.
        resolver_ctx = _make_ctx(model_name="resolver-model")
        smart.resolver.resolve.return_value = ExecutionOverride(
            execution_context=resolver_ctx, thinking_effort="low"
        )

        message = _make_message(
            execution_override=explicit, task_complexity="deep_reasoning"
        )

        eff = smart._resolve_effective(message)

        assert eff.ctx is explicit_ctx
        assert eff.thinking_effort == "high"
        # Resolver MUST NOT be called when an explicit override is present:
        # a caller that knows the answer should not pay the resolution cost
        # AND should not be silently overridden by user config.
        smart.resolver.resolve.assert_not_called()

    def test_resolver_used_when_no_explicit_override(self, smart):
        resolver_ctx = _make_ctx(model_name="resolver-model")
        smart.resolver.resolve.return_value = ExecutionOverride(
            execution_context=resolver_ctx, thinking_effort="high"
        )
        message = _make_message(task_complexity="deep_reasoning")

        eff = smart._resolve_effective(message)

        assert eff.ctx is resolver_ctx
        assert eff.thinking_effort == "high"
        smart.resolver.resolve.assert_called_once()

    def test_falls_back_to_agent_defaults(self, smart, default_ctx):
        smart.resolver.resolve.return_value = None
        message = _make_message()  # no override, no task_complexity

        eff = smart._resolve_effective(message)

        assert eff.ctx is default_ctx
        # Agent default thinking_effort is "medium" (set in fixture).
        assert eff.thinking_effort == "medium"

    def test_non_override_value_in_context_is_ignored(self, smart, default_ctx):
        # Defensive: if someone places a non-ExecutionOverride object on
        # ``execution_override``, the resolver-or-default path must run.
        message = _make_message()
        message.context["execution_override"] = "not-an-override"

        eff = smart._resolve_effective(message)

        assert eff.ctx is default_ctx
        smart.resolver.resolve.assert_called_once()


# --------------------------------------------------------------------------- #
# thinking_effort resolution                                                  #
# --------------------------------------------------------------------------- #


class TestThinkingEffortResolution:
    """thinking_effort: override > context > default."""

    def test_override_thinking_effort_wins_over_context(self, smart):
        explicit = ExecutionOverride(
            execution_context=_make_ctx(), thinking_effort="high"
        )
        message = _make_message(execution_override=explicit, thinking_effort="low")

        eff = smart._resolve_effective(message)

        assert eff.thinking_effort == "high"

    def test_context_thinking_effort_wins_over_default_when_no_override(self, smart):
        message = _make_message(thinking_effort="low")

        eff = smart._resolve_effective(message)

        assert eff.thinking_effort == "low"

    def test_default_thinking_effort_when_nothing_else(self, smart):
        message = _make_message()

        eff = smart._resolve_effective(message)

        assert eff.thinking_effort == "medium"

    def test_override_with_none_thinking_falls_back_to_context(self, smart):
        # Override with thinking_effort=None means "no thinking preference".
        # Caller may still have set message.context["thinking_effort"].
        explicit = ExecutionOverride(
            execution_context=_make_ctx(), thinking_effort=None
        )
        message = _make_message(execution_override=explicit, thinking_effort="low")

        eff = smart._resolve_effective(message)

        assert eff.thinking_effort == "low"

    def test_override_with_none_thinking_falls_back_to_default(self, smart):
        explicit = ExecutionOverride(
            execution_context=_make_ctx(), thinking_effort=None
        )
        message = _make_message(execution_override=explicit)

        eff = smart._resolve_effective(message)

        assert eff.thinking_effort == "medium"


# --------------------------------------------------------------------------- #
# Effective return type                                                       #
# --------------------------------------------------------------------------- #


class TestEffectiveExecutionShape:
    def test_returns_effective_execution_instance(self, smart):
        message = _make_message()
        eff = smart._resolve_effective(message)
        assert isinstance(eff, _EffectiveExecution)

    def test_effective_is_frozen(self, smart):
        import dataclasses

        message = _make_message()
        eff = smart._resolve_effective(message)
        with pytest.raises(dataclasses.FrozenInstanceError):
            eff.thinking_effort = "different"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Negative invariants — proves the lock + mutation are gone                   #
# --------------------------------------------------------------------------- #


class TestNoLockNoMutation:
    """Invariants that codify Step #2's correctness contract."""

    def test_execute_lock_attribute_is_gone(self, smart):
        # Direct guard: attribute must not exist any more. If it does,
        # someone re-introduced serialization.
        assert not hasattr(smart, "_execute_lock")

    def test_execute_locked_method_is_gone(self, smart):
        # Wrapper method `_execute_locked` was removed — `execute` runs
        # `_run` directly.
        assert not hasattr(smart, "_execute_locked")

    def test_resolve_effective_does_not_mutate_self(self, smart, default_ctx):
        explicit = ExecutionOverride(
            execution_context=_make_ctx(model_name="other-model"),
            thinking_effort="high",
        )
        message = _make_message(execution_override=explicit)

        # Capture before-state.
        before_llm = smart.llm
        before_model = smart.model_name
        before_ctx = smart._agent_execution_context
        before_exec_ctx = smart.execution_context

        eff = smart._resolve_effective(message)

        # _resolve_effective MUST NOT touch self.*. The override flows
        # exclusively through the returned _EffectiveExecution.
        assert smart.llm is before_llm
        assert smart.model_name == before_model
        assert smart._agent_execution_context is before_ctx
        assert smart.execution_context is before_exec_ctx
        # And the override IS reflected in the returned eff:
        assert eff.ctx is explicit.execution_context
