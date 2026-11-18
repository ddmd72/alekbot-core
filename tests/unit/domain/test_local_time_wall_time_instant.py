"""wall_time_instant / known_zone: the one reading of a user's wall time as an instant (P16-D5, owner decision: a
skipped wall time is refused). Shared by NotesAgent's tool arguments and the Cabinet reminder routes."""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.domain.local_time import known_zone, wall_time_instant
from tests.support.clock_instants import REPEATED_HOUR, SKIPPED_HOUR, ZONES, transition

UTC = timezone.utc


@pytest.mark.parametrize("zone", ZONES)
def test_a_skipped_wall_time_raises_value_error_naming_the_zone(zone):
    skipped = SKIPPED_HOUR[zone]
    with pytest.raises(ValueError, match=zone):
        wall_time_instant(skipped.naive.isoformat(timespec="minutes"), ZoneInfo(zone))


@pytest.mark.parametrize("zone", ZONES)
def test_the_hour_after_the_jump_is_its_instant(zone):
    after = SKIPPED_HOUR[zone].naive + timedelta(hours=1)
    offset = transition(zone, "spring", "T+1m").utc.astimezone(ZoneInfo(zone)).utcoffset()
    instant = wall_time_instant(after.isoformat(timespec="minutes"), ZoneInfo(zone))
    assert instant == after.replace(tzinfo=UTC) - offset
    assert instant.utcoffset() == timedelta(0)


@pytest.mark.parametrize("zone", ZONES)
def test_a_repeated_wall_time_is_the_first_pass(zone):
    repeated = REPEATED_HOUR[zone]
    instant = wall_time_instant(repeated.wall.isoformat(timespec="minutes"), ZoneInfo(zone))
    assert instant == repeated.fold0.utc.astimezone(UTC)


@pytest.mark.parametrize("zone", ZONES)
def test_an_explicit_offset_is_kept_even_inside_a_skipped_hour(zone):
    """With an offset the text names an instant; the zone is not consulted."""
    skipped = SKIPPED_HOUR[zone].naive
    instant = wall_time_instant(f"{skipped.isoformat(timespec='minutes')}+00:00", ZoneInfo(zone))
    assert instant == skipped.replace(tzinfo=UTC)


def test_unreadable_text_raises_value_error():
    with pytest.raises(ValueError):
        wall_time_instant("next tuesday 9am", ZoneInfo(ZONES[0]))


def test_known_zone_returns_the_zone():
    assert known_zone(ZONES[0]) == ZoneInfo(ZONES[0])


@pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "", "/etc/passwd", "../x"])
def test_known_zone_refuses_an_unknown_empty_or_malformed_name(name):
    with pytest.raises(ValueError, match="Unknown time zone"):
        known_zone(name)
