"""`review_slot_date` across every 2026 DST transition of the three clock-test zones (P16-C item 1).

The sweep shape of `test_daily_review_window.py`, extended to Helsinki and New York, started two days before each
transition: hourly ticks at minute 12 UTC for five days, configured hours around midnight and around the skipped and
repeated hours. Every local date gets exactly one slot date, and each one is reached.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.domain.daily_review_window import REVIEW_WINDOW_HOURS, review_slot_date
from tests.support.clock_instants import ZONES, transition

_HOURS = [0, 1, 2, 3, 22, 23]
_DAYS = 5


def _sweep(zone: str, kind: str, hour: int) -> list[tuple[datetime, date]]:
    start = transition(zone, kind, "T").utc
    first_tick = start - timedelta(days=2) + timedelta(minutes=12)
    tz = ZoneInfo(zone)
    slots = []
    for i in range(_DAYS * 24):
        tick = first_tick + timedelta(hours=i)
        slot = review_slot_date(tick, tz, hour)
        if slot is not None:
            slots.append((tick, slot))
    return slots


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("kind", ["spring", "fall"])
@pytest.mark.parametrize("hour", _HOURS)
def test_every_local_date_gets_one_slot_date_and_it_is_reached(zone, kind, hour):
    tz = ZoneInfo(zone)
    slots = _sweep(zone, kind, hour)

    by_date: dict[date, list[datetime]] = {}
    for tick, slot in slots:
        by_date.setdefault(slot, []).append(tick)

    # The slot dates are consecutive local dates: none skipped, none visited twice in separate runs.
    dates = sorted(by_date)
    assert dates, f"{zone} {kind} h{hour}: no slot at all"
    assert [b - a for a, b in zip(dates, dates[1:])] == [timedelta(days=1)] * (len(dates) - 1)
    ordered = [slot for _, slot in slots]
    assert ordered == sorted(ordered)

    # Every tick of a slot is inside the window opened on that date, and the slot is reached at least once.
    for slot, ticks in by_date.items():
        for tick in ticks:
            local = tick.astimezone(tz)
            assert (local.hour - hour) % 24 < REVIEW_WINDOW_HOURS
        # The repeated hour adds at most one tick to the window's three hours.
        assert 1 <= len(ticks) <= REVIEW_WINDOW_HOURS + 1

    # Interior local dates (not the sweep's edges) all have a slot.
    first_local = slots[0][0].astimezone(tz).date()
    last_local = slots[-1][0].astimezone(tz).date()
    cur = first_local + timedelta(days=1)
    while cur < last_local:
        assert cur in by_date, f"{zone} {kind} h{hour}: no slot for {cur}"
        cur += timedelta(days=1)


def _expected_slot(local: datetime, hour: int):
    """Independent oracle from the local wall time: the date on which the window opened (None outside it)."""
    if hour <= local.hour < hour + REVIEW_WINDOW_HOURS:
        return local.date()
    wrapped = hour + REVIEW_WINDOW_HOURS - 24
    if wrapped > 0 and local.hour < wrapped:
        return local.date() - timedelta(days=1)
    return None


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("kind", ["spring", "fall"])
@pytest.mark.parametrize("hour", _HOURS)
def test_every_tick_slot_is_the_local_date_the_window_opened(zone, kind, hour):
    tz = ZoneInfo(zone)
    first_tick = transition(zone, kind, "T").utc - timedelta(days=2) + timedelta(minutes=12)
    for i in range(_DAYS * 24):
        tick = first_tick + timedelta(hours=i)
        assert review_slot_date(tick, tz, hour) == _expected_slot(tick.astimezone(tz), hour), tick


@pytest.mark.parametrize("zone", ZONES)
def test_fall_back_repeated_hour_stays_in_one_slot(zone):
    """With the configured hour equal to the repeated local hour, both passes (two UTC instants) fall in the
    window opened on the same local date: one slot, not two."""
    tz = ZoneInfo(zone)
    fall = transition(zone, "fall", "T").utc
    first_pass = fall - timedelta(minutes=48)    # :12 UTC, local hour H the first time
    second_pass = fall + timedelta(minutes=12)   # :12 UTC, local hour H again
    repeated = first_pass.astimezone(tz).hour
    assert second_pass.astimezone(tz).hour == repeated
    a = review_slot_date(first_pass, tz, repeated)
    b = review_slot_date(second_pass, tz, repeated)
    assert a == b == second_pass.astimezone(tz).date()
