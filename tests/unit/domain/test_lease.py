"""`lease_held` (T12c A10): the one liveness rule of the janitor's run guard and the memory-write lease."""
from datetime import datetime, timedelta, timezone

from src.domain.fact_lifecycle_run_guard import run_guard_held
from src.domain.lease import lease_held

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


def test_a_lease_blocks_until_its_expiry_instant():
    assert lease_held(NOW + timedelta(seconds=1), NOW) is True
    assert lease_held(NOW, NOW) is False
    assert lease_held(NOW - timedelta(seconds=1), NOW) is False


def test_an_absent_lease_is_free():
    assert lease_held(None, NOW) is False


def test_the_run_guard_is_the_same_rule():
    for held_until in (None, NOW - timedelta(seconds=1), NOW, NOW + timedelta(seconds=1)):
        assert run_guard_held(held_until, NOW) is lease_held(held_until, NOW)
