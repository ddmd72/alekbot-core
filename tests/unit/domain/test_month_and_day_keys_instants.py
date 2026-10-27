"""Month keys and local-day keys at the instants that move them (P16). Pure functions: `now` is passed in."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.domain.drive_index_planner import month_key, next_month_start
from src.domain.drive_mirror_plan import local_day
from src.domain.fact_review_task_keys import daily_key
from tests.support.clock_instants import (
    BERLIN, HELSINKI, MONTH_ENDS, NEW_YORK, REPEATED_HOUR, ZONES, transition,
)

UTC = timezone.utc


_EXPECTED_MONTH = {  # instant name -> (month_key, next_month_start)
    "2026-02 last second": ("2026-02", datetime(2026, 3, 1, tzinfo=UTC)),
    "2026-02 next second": ("2026-03", datetime(2026, 4, 1, tzinfo=UTC)),
    "2026-10 last second": ("2026-10", datetime(2026, 11, 1, tzinfo=UTC)),
    "2026-10 next second": ("2026-11", datetime(2026, 12, 1, tzinfo=UTC)),
    "2026-12 last second": ("2026-12", datetime(2027, 1, 1, tzinfo=UTC)),
    "2026-12 next second": ("2027-01", datetime(2027, 2, 1, tzinfo=UTC)),
}


@pytest.mark.parametrize("instant", MONTH_ENDS, ids=lambda i: i.name)
def test_month_key_and_next_month_start_at_month_ends(instant):
    key, boundary = _EXPECTED_MONTH[instant.name]
    assert month_key(instant.utc) == key
    assert next_month_start(instant.utc) == boundary


def test_month_key_is_utc_even_for_a_local_aware_value():
    local = datetime(2026, 11, 1, 0, 30, tzinfo=UTC).astimezone(ZoneInfo(BERLIN))
    assert local.month == 11 and month_key(local) == "2026-11"
    late = datetime(2026, 10, 31, 23, 30, tzinfo=UTC).astimezone(ZoneInfo(HELSINKI))
    assert late.month == 11 and month_key(late) == "2026-10"


def test_next_month_start_rolls_the_year_and_the_february_length():
    assert next_month_start(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)
    assert next_month_start(datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC)) == datetime(2026, 3, 1, tzinfo=UTC)


def _local_midnight_utc(zone: str, y: int, m: int, d: int) -> datetime:
    """The UTC instant of local 00:00 on a date with no transition at midnight."""
    return datetime(y, m, d, tzinfo=ZoneInfo(zone)).astimezone(UTC)


@pytest.mark.parametrize("zone,day_before,day,kind", [
    (BERLIN, (2026, 3, 28), (2026, 3, 29), "spring"),
    (BERLIN, (2026, 10, 24), (2026, 10, 25), "fall"),
    (HELSINKI, (2026, 3, 28), (2026, 3, 29), "spring"),
    (HELSINKI, (2026, 10, 24), (2026, 10, 25), "fall"),
    (NEW_YORK, (2026, 3, 7), (2026, 3, 8), "spring"),
    (NEW_YORK, (2026, 10, 31), (2026, 11, 1), "fall"),
])
def test_local_day_flips_at_local_midnight_around_each_transition(zone, day_before, day, kind):
    midnight = _local_midnight_utc(zone, *day)
    assert local_day(midnight - timedelta(seconds=1), zone) == datetime(*day_before).date().isoformat()
    assert local_day(midnight, zone) == datetime(*day).date().isoformat()
    # the transition itself does not move the date
    t = transition(zone, kind, "T").utc
    assert local_day(t - timedelta(minutes=1), zone) == local_day(t + timedelta(minutes=1), zone)


@pytest.mark.parametrize("zone", ZONES)
def test_local_day_in_the_repeated_hour_is_one_day_for_both_passes(zone):
    rep = REPEATED_HOUR[zone]
    assert local_day(rep.fold0.utc, zone) == local_day(rep.fold1.utc, zone) == rep.wall.date().isoformat()


def test_local_day_unknown_zone_and_naive_value_fall_back_to_utc():
    assert local_day(datetime(2026, 10, 27, 23, 30), "Nowhere/Land") == "2026-10-27"
    assert local_day(datetime(2026, 10, 27, 23, 30, tzinfo=UTC), "") == "2026-10-27"


_TRANSITION_DAYS = {  # zone -> (spring day, fall day)
    BERLIN: ((2026, 3, 29), (2026, 10, 25)),
    HELSINKI: ((2026, 3, 29), (2026, 10, 25)),
    NEW_YORK: ((2026, 3, 8), (2026, 11, 1)),
}


@pytest.mark.parametrize("zone,kind", [(z, k) for z in ZONES for k in ("spring", "fall")])
def test_daily_key_flips_at_local_midnight_on_both_transition_days(zone, kind):
    y, m, d = _TRANSITION_DAYS[zone][0 if kind == "spring" else 1]
    day = datetime(y, m, d)
    for start, label in ((day, day.date()), (day + timedelta(days=1), (day + timedelta(days=1)).date())):
        midnight = datetime(start.year, start.month, start.day, tzinfo=ZoneInfo(zone)).astimezone(UTC)
        before = daily_key("a1", midnight - timedelta(seconds=1), zone)
        after = daily_key("a1", midnight + timedelta(seconds=1), zone)
        assert after == f"fact-review:a1:{label.isoformat()}"
        assert before == f"fact-review:a1:{(label - timedelta(days=1)).isoformat()}"


@pytest.mark.parametrize("zone,kind", [(z, k) for z in ZONES for k in ("spring", "fall")])
def test_daily_key_is_the_owner_local_day_hour_by_hour_around_the_switch(zone, kind):
    t = transition(zone, kind, "T").utc
    for h in range(-30, 31):
        at = t + timedelta(hours=h)
        assert daily_key("a1", at, zone) == f"fact-review:a1:{local_day(at, zone)}"
        assert daily_key("a1", at, zone) == f"fact-review:a1:{at.astimezone(ZoneInfo(zone)).date().isoformat()}"


def test_daily_key_follows_the_owner_zone_not_utc():
    at = datetime(2026, 10, 26, 23, 30, tzinfo=UTC)
    assert daily_key("a1", at, BERLIN) == "fact-review:a1:2026-10-27"
    assert daily_key("a1", at, NEW_YORK) == "fact-review:a1:2026-10-26"
