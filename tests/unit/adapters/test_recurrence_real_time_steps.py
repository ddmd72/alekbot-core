"""DateutilRecurrenceAdapter: the real-time walk of HOURLY and MINUTELY rules (P16-D4), beyond the pins in
`test_recurrence_instants.py`. Those cover rules aligned to the anchor's minute; these cover a rule whose next
occurrence reads as an EARLIER wall time than the anchor, a skipped wall time picked by BYHOUR, the rule's phase, the
spring day's count and a rule that matches only months away."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from tests.support.clock_instants import BERLIN, SKIPPED_HOUR, SUMMER_MIDNIGHT, ZONES, transition

UTC = timezone.utc


@pytest.fixture
def adapter() -> DateutilRecurrenceAdapter:
    return DateutilRecurrenceAdapter()


def test_the_second_pass_of_an_earlier_wall_minute_is_the_next_occurrence(adapter):
    """Berlin 02:50 CEST (00:50Z), hourly at :10: the next real :10 is 02:10 CET (01:10Z), a wall time before 02:50."""
    anchor = transition(BERLIN, "fall", "T").utc - timedelta(minutes=10)
    got = adapter.next_occurrence("FREQ=HOURLY;BYMINUTE=10", anchor, BERLIN)
    assert got == transition(BERLIN, "fall", "T").utc + timedelta(minutes=10)


@pytest.mark.parametrize("zone", ZONES)
def test_a_skipped_wall_time_of_an_hourly_rule_is_read_one_hour_later(adapter, zone):
    """Hourly restricted to the skipped hour: the occurrence is read with the offset before the jump (one local hour
    later), never with the offset after it (one local hour earlier, a wall time the rule does not name)."""
    skipped = SKIPPED_HOUR[zone].naive
    z = ZoneInfo(zone)
    anchor = transition(zone, "spring", "T").utc - timedelta(hours=2)
    rule = f"FREQ=HOURLY;BYHOUR={skipped.hour};BYMINUTE={skipped.minute}"
    got = adapter.next_occurrence(rule, anchor, zone)
    before_jump = transition(zone, "spring", "T-1m").utc.astimezone(z).utcoffset()
    assert got == skipped.replace(tzinfo=UTC) - before_jump
    assert got.astimezone(z).replace(tzinfo=None) == skipped + timedelta(hours=1)


def test_the_phase_of_an_interval_comes_from_the_anchor(adapter):
    """Every 7 minutes from the anchor: the walk starts earlier, but on the anchor's own grid."""
    anchor = SUMMER_MIDNIGHT.utc + timedelta(minutes=3)
    assert adapter.first_occurrence("FREQ=MINUTELY;INTERVAL=7", anchor, BERLIN) == anchor
    assert adapter.next_occurrence("FREQ=MINUTELY;INTERVAL=7", anchor, BERLIN) == anchor + timedelta(minutes=7)
    assert adapter.next_occurrence("FREQ=HOURLY;INTERVAL=5", anchor, BERLIN) == anchor + timedelta(hours=5)


@pytest.mark.parametrize("zone", ZONES)
def test_hourly_fires_23_times_over_the_spring_day(adapter, zone):
    z = ZoneInfo(zone)
    day = transition(zone, "spring", "T").utc.astimezone(z).date()
    start = datetime.combine(day, time(0), tzinfo=z).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=z).astimezone(UTC)
    fires, t = [], adapter.first_occurrence("FREQ=HOURLY", start, zone)
    while t is not None and t < end and len(fires) < 30:
        fires.append(t)
        t = adapter.next_occurrence("FREQ=HOURLY", t, zone)
    assert len(fires) == 23
    assert all(b - a == timedelta(hours=1) for a, b in zip(fires, fires[1:]))


def test_an_hourly_rule_that_matches_months_away_is_found(adapter):
    """The walk is bounded by the best instant found, not by a window: a match eleven months on is still returned."""
    anchor = SUMMER_MIDNIGHT.utc
    got = adapter.next_occurrence("FREQ=HOURLY;BYMONTH=6;BYMINUTE=0", anchor, BERLIN)
    assert got == datetime(anchor.year + 1, 6, 1, 0, 0, tzinfo=ZoneInfo(BERLIN)).astimezone(UTC)
