from datetime import datetime, timedelta, timezone

from src.domain.fact_lifecycle_run_guard import RUN_GUARD_SECONDS, run_guard_held, run_guard_until

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)


def test_a_guard_lasts_five_minutes():
    assert RUN_GUARD_SECONDS == 300
    assert run_guard_until(NOW) == NOW + timedelta(minutes=5)


def test_a_guard_blocks_until_its_expiry_instant():
    assert run_guard_held(NOW + timedelta(seconds=1), NOW) is True
    assert run_guard_held(NOW, NOW) is False
    assert run_guard_held(NOW - timedelta(seconds=1), NOW) is False


def test_no_guard_blocks_nothing():
    assert run_guard_held(None, NOW) is False
