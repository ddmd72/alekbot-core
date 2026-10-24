"""A horizon counted from now but stored from `reported_date` (Facts S4 · T1b; the review horizon of plan §3.6, m3)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.fact_lifecycle import horizon_from_now

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


def test_an_old_reported_date_stores_a_ttl_that_reaches_now_plus_the_days():
    fields = horizon_from_now(28, NOW - timedelta(days=60), NOW)
    assert fields == {"expiry_kind": ExpiryKind.HORIZON, "ttl_days": 88, "expires_at": NOW + timedelta(days=28)}


def test_a_part_of_a_day_rounds_the_ttl_up_so_the_fact_is_never_due_early():
    reported = NOW - timedelta(days=60, seconds=1)
    fields = horizon_from_now(28, reported, NOW)
    assert fields["ttl_days"] == 89
    assert fields["expires_at"] == reported + timedelta(days=89) == NOW + timedelta(days=28, hours=24) - timedelta(seconds=1)


def test_a_reported_date_after_now_counts_from_itself():
    reported = NOW + timedelta(days=2)
    fields = horizon_from_now(14, reported, NOW)
    assert fields["ttl_days"] == 14 and fields["expires_at"] == reported + timedelta(days=14)


def test_a_reported_date_of_now_is_the_plain_horizon():
    assert horizon_from_now(5, NOW, NOW)["ttl_days"] == 5


def test_the_stored_pair_always_satisfies_expires_at_equals_reported_plus_ttl():
    for hours in (0, 1, 23, 24, 25, 5000):
        reported = NOW - timedelta(hours=hours)
        fields = horizon_from_now(7, reported, NOW)
        assert fields["expires_at"] == reported + timedelta(days=fields["ttl_days"])
        assert NOW + timedelta(days=7) <= fields["expires_at"] < NOW + timedelta(days=8)


def test_naive_times_are_read_as_utc():
    fields = horizon_from_now(3, datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 24, 9, 0))
    assert fields["ttl_days"] == 26 and fields["expires_at"].tzinfo is not None


@pytest.mark.parametrize("days", [0, -1])
def test_a_horizon_of_less_than_a_day_is_refused(days):
    with pytest.raises(ValueError):
        horizon_from_now(days, NOW, NOW)


def test_one_day_is_the_least():
    assert horizon_from_now(1, NOW, NOW)["ttl_days"] == 1
