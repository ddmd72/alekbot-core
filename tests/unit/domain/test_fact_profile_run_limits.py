"""FactProfileRunLimits: the profile step's call cap, call budget and parallelism, as one frozen value."""
import dataclasses

import pytest

from src.domain.fact_profile_run_limits import FactProfileRunLimits


def test_four_calls_run_at_once_unless_told_otherwise():
    assert FactProfileRunLimits(max_per_run=None, call_budget_s=60.0) == FactProfileRunLimits(None, 60.0, concurrency=4)


def test_the_limits_are_frozen():
    limits = FactProfileRunLimits(max_per_run=20, call_budget_s=60.0)

    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_per_run = 21  # type: ignore[misc]
