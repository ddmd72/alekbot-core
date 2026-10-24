"""Timing of the memory-write lease: expiry, the agent's time budget and the deferral bucket."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.consolidation_run import ConsolidationRun
from src.domain.memory_write_lease import (
    LEASE_BUSY_RETRY_DELAY_S,
    LEASE_MARGIN,
    agent_timeout_s,
    defer_bucket,
    lease_until,
)

NOW = datetime(2026, 10, 23, 9, 0, 30, tzinfo=timezone.utc)


def test_constants():
    assert LEASE_MARGIN == timedelta(minutes=1)
    assert LEASE_BUSY_RETRY_DELAY_S == 120


def test_a_900_second_run_holds_the_lease_for_960_seconds():
    assert lease_until(NOW, 900) - NOW == timedelta(seconds=960)


def test_the_agent_gets_the_run_timeout_right_after_acquiring():
    assert agent_timeout_s(lease_until(NOW, 900), NOW) == 900.0


def test_the_agent_time_shrinks_with_the_clock():
    held = lease_until(NOW, 900)
    assert agent_timeout_s(held, NOW + timedelta(seconds=100)) == 800.0


def test_the_agent_gets_nothing_once_only_the_margin_is_left():
    held = lease_until(NOW, 900)
    assert agent_timeout_s(held, NOW + timedelta(seconds=900)) == 0.0


def test_the_agent_time_is_negative_inside_the_margin_and_after_expiry():
    held = lease_until(NOW, 900)
    assert agent_timeout_s(held, NOW + timedelta(seconds=930)) == -30.0
    assert agent_timeout_s(held, NOW + timedelta(seconds=2000)) < 0


def test_the_bucket_is_constant_within_120_seconds_and_moves_at_the_edge():
    start = datetime(2026, 10, 23, 9, 0, 0, tzinfo=timezone.utc)
    first = defer_bucket(start)
    assert defer_bucket(start + timedelta(seconds=119)) == first
    assert defer_bucket(start + timedelta(seconds=120)) == first + 1


def test_the_bucket_is_the_epoch_second_floor_divided_by_the_delay():
    assert defer_bucket(NOW) == int(NOW.timestamp()) // 120


def test_consolidation_run_defaults_and_immutability():
    assert ConsolidationRun() == ConsolidationRun(processed=0, has_more=False, deferred=False)
    assert ConsolidationRun(deferred=True).deferred is True
    with pytest.raises(FrozenInstanceError):
        ConsolidationRun().processed = 1  # type: ignore[misc]
