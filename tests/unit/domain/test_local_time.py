"""local_stamp / local_day: the one timestamp format Smart and the consolidator share (Facts S3, plan §3.9)."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.local_time import local_date, local_day, local_stamp, zone_or_utc

CREATED_AT = 1700000000  # 2023-11-14 22:13:20 UTC, a Tuesday


def test_stamp_has_year_weekday_time_and_the_zones_abbreviation():
    assert local_stamp(CREATED_AT, "Europe/Madrid") == "2023-11-14 Tue 23:13 CET"
    assert local_stamp(float(CREATED_AT), "Pacific/Auckland") == "2023-11-15 Wed 11:13 NZDT"


def test_the_abbreviation_is_the_moments_own_not_the_current_one():
    july = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    january = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    assert local_stamp(july, "Europe/Madrid") == "2026-07-10 Fri 14:00 CEST"
    assert local_stamp(january, "Europe/Madrid") == "2026-01-10 Sat 13:00 CET"


@pytest.mark.parametrize("zone", ["Not/AZone", "", "/etc/passwd", "../x"])
def test_an_unknown_or_malformed_zone_is_utc(zone):
    assert local_stamp(CREATED_AT, zone) == "2023-11-14 Tue 22:13 UTC"
    assert zone_or_utc(zone).key == "UTC"


def test_a_naive_datetime_is_utc():
    assert local_stamp(datetime(2026, 7, 10, 12, 0), "Europe/Madrid") == "2026-07-10 Fri 14:00 CEST"


def test_an_aware_datetime_is_converted_from_its_own_offset():
    moment = datetime(2026, 7, 10, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert local_stamp(moment, "UTC") == "2026-07-10 Fri 12:00 UTC"


def test_the_epoch_and_the_datetime_of_one_moment_give_one_stamp():
    moment = datetime.fromtimestamp(CREATED_AT, tz=timezone.utc)
    assert local_stamp(moment, "Europe/Paris") == local_stamp(CREATED_AT, "Europe/Paris")


def test_the_stamp_shape_is_pinned():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2} [A-Za-z0-9+\-]+", local_stamp(CREATED_AT, "Europe/Paris"))


def test_local_day_is_the_date_and_weekday_in_the_zone():
    assert local_day(CREATED_AT, "Europe/Madrid") == "2023-11-14 Tue"
    assert local_day(CREATED_AT, "Pacific/Auckland") == "2023-11-15 Wed"


def test_local_date_is_the_calendar_date_in_the_zone():
    late = datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc)
    assert local_date(late, "Europe/Paris").isoformat() == "2026-10-24"
    assert local_date(late, "UTC").isoformat() == "2026-10-23"
