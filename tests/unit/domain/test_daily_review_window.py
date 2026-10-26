from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.domain.daily_review_window import REVIEW_WINDOW_HOURS, review_slot_date

_ZONES = ["Europe/Madrid", "Australia/Sydney", "UTC"]
_HOURS = [0, 2, 8, 22, 23]


def _ticks(start: datetime, days: int):
    # Hourly fan-out at minute 12 UTC
    t = start
    for _ in range(days * 24):
        yield t
        t += timedelta(hours=1)


def _slots(tz_name, hour, start, days):
    tz = ZoneInfo(tz_name)
    out = []
    for t in _ticks(start, days):
        d = review_slot_date(t, tz, hour)
        if d is not None:
            out.append((t, d))
    return out


@pytest.mark.parametrize("tz_name", _ZONES)
@pytest.mark.parametrize("hour", _HOURS)
@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 6, 10, 0, 12, tzinfo=timezone.utc),
        datetime(2026, 10, 23, 0, 12, tzinfo=timezone.utc),  # spans Europe/Madrid fall-back 10-25
        datetime(2027, 3, 26, 0, 12, tzinfo=timezone.utc),  # spans Europe/Madrid spring-forward 03-28
    ],
)
def test_each_local_date_gets_exactly_one_slot_date_and_it_is_reached(tz_name, hour, start):
    slots = _slots(tz_name, hour, start, 6)
    by_date = {}
    for t, d in slots:
        by_date.setdefault(d, []).append(t)
    tz = ZoneInfo(tz_name)
    first, last = start.astimezone(tz).date(), (start + timedelta(days=6)).astimezone(tz).date()
    # Every interior local date has a slot, and its first tick is inside the window.
    cur = first + timedelta(days=1)
    while cur < last - timedelta(days=1):
        assert cur in by_date, f"{tz_name} h{hour}: no slot for {cur}"
        first_tick = by_date[cur][0].astimezone(tz)
        offset = (first_tick.hour - hour) % 24
        assert offset < REVIEW_WINDOW_HOURS
        cur += timedelta(days=1)
    # A slot date maps to at most window-length ticks (fall-back adds none beyond 3 hours' worth + 1).
    assert all(len(v) <= REVIEW_WINDOW_HOURS + 1 for v in by_date.values())


def test_fall_back_repeated_hour_stays_in_one_slot():
    tz = ZoneInfo("Europe/Madrid")
    # 2026-10-25 02:00-03:00 local happens twice; configured hour 2.
    t1 = datetime(2026, 10, 25, 0, 12, tzinfo=timezone.utc)  # 02:12 CEST
    t2 = datetime(2026, 10, 25, 1, 12, tzinfo=timezone.utc)  # 02:12 CET (repeat)
    assert review_slot_date(t1, tz, 2) == date(2026, 10, 25)
    assert review_slot_date(t2, tz, 2) == date(2026, 10, 25)


def test_spring_forward_missing_hour_still_fires():
    tz = ZoneInfo("Europe/Madrid")
    # 2027-03-28 02:00 does not exist; configured hour 2 fires at 03:12 local (offset 1).
    t = datetime(2027, 3, 28, 1, 12, tzinfo=timezone.utc)  # 03:12 CEST
    assert review_slot_date(t, tz, 2) == date(2027, 3, 28)


def test_hour_23_window_continues_past_midnight_with_previous_date():
    tz = ZoneInfo("UTC")
    assert review_slot_date(datetime(2026, 1, 15, 23, 12, tzinfo=timezone.utc), tz, 23) == date(2026, 1, 15)
    assert review_slot_date(datetime(2026, 1, 16, 0, 12, tzinfo=timezone.utc), tz, 23) == date(2026, 1, 15)
    assert review_slot_date(datetime(2026, 1, 16, 1, 12, tzinfo=timezone.utc), tz, 23) == date(2026, 1, 15)


def test_offset_three_is_outside_the_window():
    tz = ZoneInfo("UTC")
    assert review_slot_date(datetime(2026, 1, 15, 11, 12, tzinfo=timezone.utc), tz, 8) is None
    assert review_slot_date(datetime(2026, 1, 15, 10, 12, tzinfo=timezone.utc), tz, 8) == date(2026, 1, 15)
    assert review_slot_date(datetime(2026, 1, 15, 7, 12, tzinfo=timezone.utc), tz, 8) is None


@pytest.mark.parametrize("bad", [-1, 24, 99])
def test_configured_hour_out_of_range_is_none(bad):
    now = datetime(2026, 1, 15, 8, 12, tzinfo=timezone.utc)
    assert review_slot_date(now, ZoneInfo("UTC"), bad) is None


def _expected_slot(local: datetime, hour: int):
    """Independent oracle from local wall time: the date on which the window opened."""
    if hour <= local.hour < hour + REVIEW_WINDOW_HOURS:
        return local.date()
    wrapped = hour + REVIEW_WINDOW_HOURS - 24
    if wrapped > 0 and local.hour < wrapped:
        return local.date() - timedelta(days=1)
    return None


@pytest.mark.parametrize("tz_name", _ZONES)
@pytest.mark.parametrize("hour", _HOURS)
@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 6, 10, 0, 12, tzinfo=timezone.utc),
        datetime(2026, 10, 23, 0, 12, tzinfo=timezone.utc),
        datetime(2027, 3, 26, 0, 12, tzinfo=timezone.utc),
    ],
)
def test_every_tick_slot_is_the_local_date_the_window_opened(tz_name, hour, start):
    tz = ZoneInfo(tz_name)
    for t in _ticks(start, 6):
        assert review_slot_date(t, tz, hour) == _expected_slot(t.astimezone(tz), hour), t


@pytest.mark.parametrize(
    "tz_name,hour,now_utc,expected",
    [
        # Sydney 08:12 AEDT is 21:12 UTC the previous day: slot date is the local (later) date.
        ("Australia/Sydney", 8, datetime(2026, 1, 14, 21, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Australia/Sydney", 8, datetime(2026, 1, 14, 23, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Australia/Sydney", 8, datetime(2026, 1, 15, 0, 12, tzinfo=timezone.utc), None),
        # Europe/Madrid hour 0 (CET, UTC+1): 00:12 local is 23:12 UTC the previous day.
        ("Europe/Madrid", 0, datetime(2026, 1, 14, 23, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Europe/Madrid", 0, datetime(2026, 1, 15, 1, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        # Europe/Madrid hour 23: window 23:00 local on the 15th runs into the 16th local, UTC is 22:12 on the 15th.
        ("Europe/Madrid", 23, datetime(2026, 1, 15, 22, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Europe/Madrid", 23, datetime(2026, 1, 15, 23, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Europe/Madrid", 23, datetime(2026, 1, 16, 0, 12, tzinfo=timezone.utc), date(2026, 1, 15)),
        ("Europe/Madrid", 23, datetime(2026, 1, 16, 1, 12, tzinfo=timezone.utc), None),
    ],
)
def test_window_crossing_utc_midnight_uses_local_date(tz_name, hour, now_utc, expected):
    assert review_slot_date(now_utc, ZoneInfo(tz_name), hour) == expected
