"""
Unit tests for the ExecutionOverride value object (defined in
services.task_execution_resolver).

Covers:
- Frozen dataclass invariants (no reassignment of attributes)
- Equality semantics (value object: equal by field values)
- Default field values
- Required vs optional fields
- intent_remap mutable-default-factory contract (independent dicts per instance)
- Holds a reference to AgentExecutionContext

ExecutionOverride is co-located with its sole producer
(``TaskExecutionResolver``) because all three layer rules block a
separate file: domain/ cannot import from ports/, ports/ cannot import
from other ports/ (REQ-ARCH-06), and services/ cannot import from
other services/ (REQ-ARCH-22). Consumers (SmartResponseAgent, etc.)
import the value object from this module.

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 4 / § 8.1
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    ProviderCapabilities,
)
from src.services.task_execution_resolver import ExecutionOverride


def _make_ctx(model_name: str = "test-model") -> AgentExecutionContext:
    """Build a minimal AgentExecutionContext for tests.

    Uses MagicMock(spec=LLMPort) for the provider — AgentExecutionContext
    has arbitrary_types_allowed=True specifically so it can carry a port
    reference without instantiating an adapter.
    """
    return AgentExecutionContext(
        agent_type="smart",
        provider=MagicMock(spec=LLMPort),
        model_name=model_name,
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
        provider_name="test-provider",
    )


class TestExecutionOverrideFrozen:
    """Frozen dataclass: cannot reassign declared attributes."""

    def test_is_frozen(self):
        ctx = _make_ctx()
        override = ExecutionOverride(execution_context=ctx)

        with pytest.raises(dataclasses.FrozenInstanceError):
            override.execution_context = _make_ctx("other-model")  # type: ignore[misc]

    def test_thinking_effort_cannot_be_reassigned(self):
        override = ExecutionOverride(execution_context=_make_ctx(), thinking_effort="low")

        with pytest.raises(dataclasses.FrozenInstanceError):
            override.thinking_effort = "high"  # type: ignore[misc]

    def test_intent_remap_cannot_be_reassigned(self):
        override = ExecutionOverride(execution_context=_make_ctx())

        with pytest.raises(dataclasses.FrozenInstanceError):
            override.intent_remap = {"a": "b"}  # type: ignore[misc]


class TestExecutionOverrideDefaults:
    """Defaults: only execution_context is required."""

    def test_only_execution_context_required(self):
        ctx = _make_ctx()
        override = ExecutionOverride(execution_context=ctx)

        assert override.execution_context is ctx
        assert override.thinking_effort is None
        assert override.intent_remap == {}

    def test_thinking_effort_default_is_none(self):
        override = ExecutionOverride(execution_context=_make_ctx())
        assert override.thinking_effort is None

    def test_intent_remap_default_is_empty_dict(self):
        override = ExecutionOverride(execution_context=_make_ctx())
        assert override.intent_remap == {}
        assert isinstance(override.intent_remap, dict)

    def test_intent_remap_default_factory_isolates_instances(self):
        # Critical: two instances created with default values must NOT share
        # the same dict object. default_factory=dict guarantees this; a
        # bare default {} would be a classic mutable-default bug.
        a = ExecutionOverride(execution_context=_make_ctx())
        b = ExecutionOverride(execution_context=_make_ctx())
        assert a.intent_remap is not b.intent_remap


class TestExecutionOverrideConstruction:
    """All fields can be supplied at construction."""

    def test_full_construction(self):
        ctx = _make_ctx()
        override = ExecutionOverride(
            execution_context=ctx,
            thinking_effort="high",
            intent_remap={"search_web": "search_web_light"},
        )
        assert override.execution_context is ctx
        assert override.thinking_effort == "high"
        assert override.intent_remap == {"search_web": "search_web_light"}

    def test_thinking_effort_accepts_known_strings(self):
        # The dataclass does NOT validate the value (resolver / agent does).
        # Frozen value object stays simple — constraints live where they're used.
        for value in ("low", "medium", "high"):
            override = ExecutionOverride(
                execution_context=_make_ctx(),
                thinking_effort=value,
            )
            assert override.thinking_effort == value


class TestExecutionOverrideEquality:
    """Value object equality: two overrides with equal field values are equal."""

    def test_equal_when_all_fields_equal(self):
        # Same provider mock instance to stabilize identity-based equality
        # of AgentExecutionContext (BaseModel uses field equality, but provider
        # is a MagicMock which compares by identity).
        provider = MagicMock(spec=LLMPort)
        ctx_a = AgentExecutionContext(
            agent_type="smart", provider=provider, model_name="m",
            tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(),
        )
        ctx_b = AgentExecutionContext(
            agent_type="smart", provider=provider, model_name="m",
            tier=PerformanceTier.PERFORMANCE, capabilities=ProviderCapabilities(),
        )
        a = ExecutionOverride(execution_context=ctx_a, thinking_effort="low",
                              intent_remap={"x": "y"})
        b = ExecutionOverride(execution_context=ctx_b, thinking_effort="low",
                              intent_remap={"x": "y"})
        assert a == b

    def test_unequal_when_thinking_effort_differs(self):
        ctx = _make_ctx()
        a = ExecutionOverride(execution_context=ctx, thinking_effort="low")
        b = ExecutionOverride(execution_context=ctx, thinking_effort="high")
        assert a != b

    def test_unequal_when_intent_remap_differs(self):
        ctx = _make_ctx()
        a = ExecutionOverride(execution_context=ctx, intent_remap={"a": "b"})
        b = ExecutionOverride(execution_context=ctx, intent_remap={})
        assert a != b


class TestExecutionOverrideContract:
    """Structural contract: dataclass + frozen + correct field set."""

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(ExecutionOverride)

    def test_field_set(self):
        names = {f.name for f in dataclasses.fields(ExecutionOverride)}
        assert names == {"execution_context", "thinking_effort", "intent_remap"}

    def test_field_types(self):
        # Verify the dataclass declares the documented types.
        # We don't import typing at runtime — just confirm presence.
        fields_by_name = {f.name: f for f in dataclasses.fields(ExecutionOverride)}
        assert fields_by_name["execution_context"].default is dataclasses.MISSING
        # thinking_effort defaults to None
        assert fields_by_name["thinking_effort"].default is None
        # intent_remap uses default_factory (not a bare default)
        assert fields_by_name["intent_remap"].default is dataclasses.MISSING
        assert fields_by_name["intent_remap"].default_factory is dict
