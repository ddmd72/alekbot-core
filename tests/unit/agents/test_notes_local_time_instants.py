"""NotesAgent's local-time input at DST transitions (P16-B, defect P16-D5).

`notes_agent._parse_dt` reads a naive ISO time as the user's local time with `replace(tzinfo=user_tz)`
(`src/agents/notes_agent.py:225`). A skipped spring time therefore gets the pre-jump offset and is stored as a real
instant (Berlin 02:30 on 2026-03-29 → 01:30Z, i.e. 03:30 CEST), where `src/domain/local_time.py:62-67` refuses a
time that does not round-trip. An ambiguous fall time resolves to fold 0, the first pass (pinned as current
behaviour: refusing or asking is an owner choice). An unknown zone falls back to UTC (`_resolve_tz`), so there is no
refusal case here.
"""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.agents.notes_agent import _parse_dt, _resolve_tz
from tests.support.clock_instants import REPEATED_HOUR, SKIPPED_HOUR, ZONES, transition

UTC = timezone.utc
D5 = "P16-D5: a skipped local time is accepted as an instant (notes_agent.py:225); owner decision"


class TestSkippedSpringTime:

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D5)
    @pytest.mark.parametrize("zone", ZONES)
    def test_a_skipped_wall_time_is_refused(self, zone):
        skipped = SKIPPED_HOUR[zone]
        parsed = _parse_dt(skipped.naive.isoformat(timespec="minutes"), ZoneInfo(zone))
        assert parsed is None, f"{skipped.naive:%Y-%m-%d %H:%M} {zone} accepted as {parsed:%Y-%m-%dT%H:%MZ}"

    @pytest.mark.parametrize("zone", ZONES)
    def test_the_hour_after_the_jump_still_parses(self, zone):
        """Control: one hour after the skipped time is a real local time and parses to its instant."""
        skipped = SKIPPED_HOUR[zone]
        after = skipped.naive + timedelta(hours=1)
        parsed = _parse_dt(after.isoformat(timespec="minutes"), ZoneInfo(zone))
        offset_after_jump = transition(zone, "spring", "T+1m").utc.astimezone(ZoneInfo(zone)).utcoffset()
        assert parsed == after.replace(tzinfo=UTC) - offset_after_jump


class TestRepeatedFallTime:

    @pytest.mark.parametrize("zone", ZONES)
    def test_an_ambiguous_wall_time_resolves_to_the_first_pass(self, zone):
        repeated = REPEATED_HOUR[zone]
        parsed = _parse_dt(repeated.wall.isoformat(timespec="minutes"), ZoneInfo(zone))
        assert parsed is not None and parsed.astimezone(UTC) == repeated.fold0.utc

    @pytest.mark.parametrize("zone", ZONES)
    def test_an_explicit_offset_picks_the_second_pass(self, zone):
        """Control: with an offset the input is no longer ambiguous."""
        repeated = REPEATED_HOUR[zone]
        offset = repeated.fold1.utc.astimezone(ZoneInfo(zone)).isoformat(timespec="minutes")
        parsed = _parse_dt(offset, ZoneInfo(zone))
        assert parsed is not None and parsed.astimezone(UTC) == repeated.fold1.utc


class TestUnknownZone:

    def test_an_unknown_zone_reads_a_naive_time_as_utc(self):
        zone = _resolve_tz("Mars/Olympus_Mons")
        assert zone == ZoneInfo("UTC")
        wall = REPEATED_HOUR[ZONES[0]].wall
        parsed = _parse_dt(wall.isoformat(timespec="minutes"), zone)
        assert parsed == wall.replace(tzinfo=UTC)
