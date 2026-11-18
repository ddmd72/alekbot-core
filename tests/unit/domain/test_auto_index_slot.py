"""`auto_index_slot_date`: the local date whose configured hour slot contains a tick (P16-D1)."""
from datetime import date, datetime, timezone

import pytest

from src.domain.auto_index_slot import auto_index_slot_date

UTC = timezone.utc


def test_a_tick_inside_the_hour_belongs_to_that_local_date():
    assert auto_index_slot_date(datetime(2026, 5, 4, 6, 12, tzinfo=UTC), "Europe/Berlin", 8) == date(2026, 5, 4)


def test_a_tick_outside_the_hour_has_no_slot():
    assert auto_index_slot_date(datetime(2026, 5, 4, 7, 12, tzinfo=UTC), "Europe/Berlin", 8) is None


def test_a_late_slot_past_local_midnight_keeps_the_date_it_started_on():
    # 23:00 Berlin on 4 May is 21:00 UTC; the slot is 21:00-22:00 UTC, still the 4th locally.
    assert auto_index_slot_date(datetime(2026, 5, 4, 21, 30, tzinfo=UTC), "Europe/Berlin", 23) == date(2026, 5, 4)


def test_the_skipped_spring_hour_fires_at_the_next_local_hour():
    # 29 Mar 2026 Berlin: 02:00 does not exist; 01:12 UTC reads 03:12 local.
    assert auto_index_slot_date(datetime(2026, 3, 29, 1, 12, tzinfo=UTC), "Europe/Berlin", 2) == date(2026, 3, 29)


def test_the_repeated_fall_hour_is_the_first_pass_only():
    first = datetime(2026, 10, 25, 0, 12, tzinfo=UTC)   # 02:12 CEST
    second = datetime(2026, 10, 25, 1, 12, tzinfo=UTC)  # 02:12 CET
    assert auto_index_slot_date(first, "Europe/Berlin", 2) == date(2026, 10, 25)
    assert auto_index_slot_date(second, "Europe/Berlin", 2) is None


def test_an_unknown_zone_raises():
    with pytest.raises(Exception):
        auto_index_slot_date(datetime(2026, 5, 4, 6, 12, tzinfo=UTC), "Mars/Base", 8)
