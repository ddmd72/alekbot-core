"""`parse_local_stamp` reads back exactly the stamp `local_stamp` renders in a zone, and nothing looser (Facts S3, T6)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.local_time import local_stamp, parse_local_stamp

ZONES = ["Europe/Paris", "UTC", "America/New_York", "Asia/Kolkata", "Europe/Istanbul", "Australia/Lord_Howe"]


@pytest.mark.parametrize("zone", ZONES)
def test_every_rendered_stamp_reads_back_to_its_minute(zone):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for step in range(0, 365 * 24 * 4, 37):                    # a year in 37-quarter-hour steps, both DST changes included
        moment = start + timedelta(minutes=15 * step)
        read = parse_local_stamp(local_stamp(moment, zone), zone)
        assert read == moment, (zone, moment)


def test_the_repeated_autumn_hour_is_told_apart_by_the_abbreviation():
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)      # 02:30 CEST
    second = first + timedelta(hours=1)                             # 02:30 CET
    assert local_stamp(first, "Europe/Paris").endswith("02:30 CEST") and local_stamp(second, "Europe/Paris").endswith("02:30 CET")
    assert parse_local_stamp("2026-10-25 Sun 02:30 CEST", "Europe/Paris") == first
    assert parse_local_stamp("2026-10-25 Sun 02:30 CET", "Europe/Paris") == second


@pytest.mark.parametrize("text", [
    "2026-10-22 Thu 12:00 CET",             # CEST is the abbreviation on that date
    "2026-10-22 Fri 12:00 CEST",            # wrong weekday
    "2026-10-22 12:00 CEST",                # no weekday
    "2026-10-22 Thu 12:00",                 # no abbreviation
    "2026-10-22 Thu 12:00 CEST later",
    "2026-02-30 Mon 12:00 CET",             # no such day
    "2026-03-29 Sun 02:30 CET",             # the skipped spring hour: never rendered
    "Thu 12:00 CEST",
    "",
])
def test_anything_but_the_exact_stamp_is_not_read(text):
    assert parse_local_stamp(text, "Europe/Paris") is None


def test_the_stamp_is_read_in_the_zone_given_not_in_another():
    assert parse_local_stamp("2026-10-22 Thu 12:00 CEST", "America/New_York") is None
    assert parse_local_stamp("2026-10-22 Thu 12:00 UTC", "UTC") == datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
