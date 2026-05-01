"""
Unit tests for ``domain.retry_policy``.

Covers:
- ``RetryPolicy`` frozen-dataclass invariants and defaults.
- ``DEFAULT_RETRY_POLICY`` is the documented standard (3 / 2.0 / 1.0).
- ``NO_RETRY_POLICY`` disables retries (transient_max_attempts=0).
- The two pre-baked policies are distinct instances (no accidental
  shared mutability — frozen dataclass already prevents this, but
  pin the contract).

Per:
  docs/04_solution_strategy/decisions/typed_retry_policy.md
"""

from __future__ import annotations

import dataclasses

import pytest

from src.domain.retry_policy import (
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
    RetryPolicy,
)


class TestRetryPolicyShape:
    """Frozen dataclass invariants."""

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(RetryPolicy)

    def test_is_frozen(self):
        policy = RetryPolicy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.transient_max_attempts = 99  # type: ignore[misc]

    def test_field_set(self):
        names = {f.name for f in dataclasses.fields(RetryPolicy)}
        assert names == {
            "transient_max_attempts",
            "transient_backoff_base_seconds",
            "transient_jitter_seconds",
        }

    def test_default_values(self):
        """Defaults pin the documented baseline."""
        policy = RetryPolicy()
        assert policy.transient_max_attempts == 3
        assert policy.transient_backoff_base_seconds == 2.0
        assert policy.transient_jitter_seconds == 1.0

    def test_equality_by_value(self):
        a = RetryPolicy(transient_max_attempts=2, transient_backoff_base_seconds=1.0,
                        transient_jitter_seconds=0.0)
        b = RetryPolicy(transient_max_attempts=2, transient_backoff_base_seconds=1.0,
                        transient_jitter_seconds=0.0)
        c = RetryPolicy(transient_max_attempts=3, transient_backoff_base_seconds=1.0,
                        transient_jitter_seconds=0.0)
        assert a == b
        assert a != c


class TestDefaultRetryPolicy:
    def test_is_a_RetryPolicy(self):
        assert isinstance(DEFAULT_RETRY_POLICY, RetryPolicy)

    def test_uses_documented_baseline(self):
        """Pin the values explicitly so a future edit to RetryPolicy
        defaults does not silently weaken every agent's retry envelope."""
        assert DEFAULT_RETRY_POLICY.transient_max_attempts == 3
        assert DEFAULT_RETRY_POLICY.transient_backoff_base_seconds == 2.0
        assert DEFAULT_RETRY_POLICY.transient_jitter_seconds == 1.0

    def test_worst_case_overhead_under_20s(self):
        """Sanity bound on the documented overhead claim. With base=2.0,
        jitter=1.0, attempts=3: backoff sum = 2 + 4 + 8 = 14s; jitter
        adds ≤ 3 × 1.0 = 3s; total ≤ 17s, well below the smallest
        REMINDER ECO budget (180s) and every other SLA budget."""
        p = DEFAULT_RETRY_POLICY
        max_backoff_sum = sum(
            p.transient_backoff_base_seconds * (2 ** i)
            for i in range(p.transient_max_attempts)
        )
        max_jitter_sum = p.transient_jitter_seconds * p.transient_max_attempts
        assert max_backoff_sum + max_jitter_sum < 20.0


class TestNoRetryPolicy:
    def test_is_a_RetryPolicy(self):
        assert isinstance(NO_RETRY_POLICY, RetryPolicy)

    def test_disables_retries(self):
        """Initial attempt only — no retry on any error type."""
        assert NO_RETRY_POLICY.transient_max_attempts == 0

    def test_distinct_from_default(self):
        assert NO_RETRY_POLICY is not DEFAULT_RETRY_POLICY
        assert NO_RETRY_POLICY != DEFAULT_RETRY_POLICY


class TestCustomPolicy:
    """Constructing a custom policy is the intended override path."""

    def test_zero_attempts_zero_backoff_no_jitter(self):
        """Used by tests to disable both retry and sleep determinism."""
        policy = RetryPolicy(
            transient_max_attempts=0,
            transient_backoff_base_seconds=0.0,
            transient_jitter_seconds=0.0,
        )
        assert policy.transient_max_attempts == 0
        assert policy.transient_backoff_base_seconds == 0.0
        assert policy.transient_jitter_seconds == 0.0

    def test_high_attempts_long_backoff(self):
        """Power-user override case (e.g. background batch agent)."""
        policy = RetryPolicy(
            transient_max_attempts=10,
            transient_backoff_base_seconds=5.0,
            transient_jitter_seconds=2.0,
        )
        assert policy.transient_max_attempts == 10
        assert policy.transient_backoff_base_seconds == 5.0
        assert policy.transient_jitter_seconds == 2.0
