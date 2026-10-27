"""DateutilRecurrenceAdapter across DST transitions (P16-B, defect P16-D4).

The adapter evaluates a rule on the user's local wall clock: it turns the anchor into a NAIVE local time
(`src/adapters/dateutil_recurrence_adapter.py:104`, which drops `fold`) and turns the occurrence back with
`replace(tzinfo=zone)` (`:112`, always fold 0). In the repeated fall-back hour that reads every second-pass time as
the first pass, one hour earlier in real time.

Rules per zone: `D` = daily at the repeated HH:30 (Berlin 02, Helsinki 03, New York 01), `H` = HOURLY, `M` = every
15 minutes, and the controls `C9` = daily 09:00 and, New York only, `C2` = daily 02:30 (not repeated there).

The target model a D4 fix must hit (the card's product rule):
- DAILY (and coarser) rules are evaluated on the wall clock and KEEP THE ANCHOR'S FOLD: from a first-pass anchor the
  repeated HH:30 is its first pass (RFC 5545 §3.3.10 sends a repeated local start time to §3.3.5, which reads it as
  the first occurrence, so the second pass of the same day is not a new occurrence); from a second-pass anchor it is
  the second pass, so a daily 02:30 created at 02:10 CET (01:10Z) is due 02:30 CET = 01:30Z, not 00:30Z.
- Sub-daily rules (HOURLY, MINUTELY) step in REAL time: 25 hourly and 100 quarter-hour fires over the 25-hour
  fall-back day. RFC 5545 leaves this open (§3.3.10 resolves only the local start time), so it is a product rule.

Assertions per cell, as the partition in the card states it: `first_occurrence(rule, a) >= a` and
`next_occurrence(rule, a) > a`, then the exact value:
- fold-1 anchors: `_oracle` (step UTC minute by minute from the anchor, the first minute whose local wall time
  matches); in the second pass it is the target for D, H and M alike, so a D4 fix that jumps to the next day does
  not turn those cells green;
- fold-0 anchors, `D`: `_oracle(..., fold_aware=True)` (also skips minutes in the second pass, per §3.3.5);
- fold-0 anchors, `H` and `M`: inequality plus "the result's local wall time matches the rule" only. An exact
  wall-clock value would assert the D4 gap (the skipped second pass) as correct, and the real-time value is what
  the count xfails pin;
- controls: `_oracle`, exact, at every anchor.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from tests.support.clock_instants import (
    BERLIN,
    HELSINKI,
    NEW_YORK,
    REPEATED_HOUR,
    SKIPPED_HOUR,
    ZONES,
    Instant,
    fold0_anchors,
    fold1_anchors,
    transition,
)

UTC = timezone.utc
D4 = "P16-D4: recurrence drops fold (dateutil_recurrence_adapter.py:104,112); owner decision"

_REPEATED_HH = {zone: REPEATED_HOUR[zone].wall.hour for zone in ZONES}
_SKIPPED_HH = {zone: SKIPPED_HOUR[zone].naive.hour for zone in ZONES}


def _daily(hour: int, minute: int) -> str:
    return f"FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute}"


def _rules(zone: str, hh: Dict[str, int]) -> Dict[str, str]:
    rules = {"D": _daily(hh[zone], 30), "H": "FREQ=HOURLY", "M": "FREQ=MINUTELY;INTERVAL=15", "C9": _daily(9, 0)}
    if zone == NEW_YORK:
        rules["C2"] = _daily(2, 30)
    return rules


def _matcher(name: str, zone: str, anchor: datetime, hh: Dict[str, int]) -> Callable[[datetime], bool]:
    z = ZoneInfo(zone)
    anchor_minute = anchor.astimezone(z).minute

    def local(t: datetime) -> datetime:
        return t.astimezone(z)

    return {
        "D": lambda t: (local(t).hour, local(t).minute, local(t).second) == (hh[zone], 30, 0),
        "H": lambda t: (local(t).minute, local(t).second) == (anchor_minute, 0),
        "M": lambda t: local(t).minute % 15 == anchor_minute % 15 and local(t).second == 0,
        "C9": lambda t: (local(t).hour, local(t).minute, local(t).second) == (9, 0, 0),
        "C2": lambda t: (local(t).hour, local(t).minute, local(t).second) == (2, 30, 0),
    }[name]


def _oracle(
    name: str, zone: str, anchor: datetime, inclusive: bool, hh: Dict[str, int], fold_aware: bool = False
) -> datetime:
    """The first UTC minute at (inclusive) or after the anchor whose local wall time matches the rule.

    ``fold_aware`` also skips every minute in the second pass of a repeated hour (``fold == 1``).
    """
    z = ZoneInfo(zone)
    matches = _matcher(name, zone, anchor, hh)
    t = anchor if inclusive else anchor + timedelta(minutes=1)
    for _ in range(3 * 24 * 60):
        if matches(t) and not (fold_aware and t.astimezone(z).fold == 1):
            return t
        t += timedelta(minutes=1)
    raise AssertionError(f"oracle found nothing for {name} in {zone} from {anchor}")


@pytest.fixture
def adapter() -> DateutilRecurrenceAdapter:
    return DateutilRecurrenceAdapter()


def _utc(value: Optional[datetime]) -> datetime:
    assert value is not None, "the rule yielded no occurrence"
    return value.astimezone(UTC)


def _cells(anchors: Callable[[str], List[Instant]], names: List[str]) -> list:
    return [
        pytest.param(zone, anchor, name, id=f"{anchor.name}-{name}")
        for zone in ZONES for anchor in anchors(zone) for name in names
    ]


# =============================================================================
# Fall back: fold-0 anchors (T-1m and the first-pass HH:30) are correct
# =============================================================================


class TestFallBackFoldZeroAnchors:

    @pytest.mark.parametrize("zone, anchor, name", _cells(fold0_anchors, ["D", "H", "M"]))
    def test_first_occurrence_is_at_or_after_the_anchor(self, adapter, zone, anchor, name):
        rule = _rules(zone, _REPEATED_HH)[name]
        got = _utc(adapter.first_occurrence(rule, anchor.utc, zone))
        assert got >= anchor.utc
        assert _matcher(name, zone, anchor.utc, _REPEATED_HH)(got)
        if name == "D":
            assert got == _oracle(name, zone, anchor.utc, True, _REPEATED_HH, fold_aware=True)

    @pytest.mark.parametrize("zone, anchor, name", _cells(fold0_anchors, ["D", "H", "M"]))
    def test_next_occurrence_is_after_the_anchor(self, adapter, zone, anchor, name):
        rule = _rules(zone, _REPEATED_HH)[name]
        got = _utc(adapter.next_occurrence(rule, anchor.utc, zone))
        assert got > anchor.utc
        assert _matcher(name, zone, anchor.utc, _REPEATED_HH)(got)
        if name == "D":
            assert got == _oracle(name, zone, anchor.utc, False, _REPEATED_HH, fold_aware=True)


# =============================================================================
# Fall back: fold-1 anchors (T, T+1m, T+30m) — the second pass of the repeated hour
# =============================================================================

# The measured partition, one literal table: (anchor label, rule, kind) → "xfail" (P16-D4) or "pass".
FOLD1_PARTITION: Dict[Tuple[str, str, str], str] = {
    ("T", "D", "first"): "xfail", ("T", "H", "first"): "xfail", ("T", "M", "first"): "xfail",
    ("T+1m", "D", "first"): "xfail", ("T+1m", "H", "first"): "xfail", ("T+1m", "M", "first"): "xfail",
    ("T+30m", "D", "first"): "xfail", ("T+30m", "H", "first"): "xfail", ("T+30m", "M", "first"): "xfail",
    ("T", "D", "next"): "xfail", ("T", "H", "next"): "pass", ("T", "M", "next"): "xfail",
    ("T+1m", "D", "next"): "xfail", ("T+1m", "H", "next"): "pass", ("T+1m", "M", "next"): "xfail",
    ("T+30m", "D", "next"): "pass", ("T+30m", "H", "next"): "pass", ("T+30m", "M", "next"): "xfail",
}


def _fold1_cells() -> list:
    out = []
    for zone in ZONES:
        for (label, name, kind), verdict in FOLD1_PARTITION.items():
            marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason=D4)] if verdict == "xfail" else []
            out.append(pytest.param(zone, transition(zone, "fall", label), name, kind, marks=marks,
                                    id=f"{zone}-{label}-{name}-{kind}"))
    return out


class TestFallBackFoldOneAnchors:

    @pytest.mark.parametrize("zone, anchor, name, kind", _fold1_cells())
    def test_the_occurrence_is_the_oracle(self, adapter, zone, anchor, name, kind):
        rule = _rules(zone, _REPEATED_HH)[name]
        inclusive = kind == "first"
        call = adapter.first_occurrence if inclusive else adapter.next_occurrence
        got = _utc(call(rule, anchor.utc, zone))
        expected = _oracle(name, zone, anchor.utc, inclusive, _REPEATED_HH)
        detail = f"{kind}={got:%d %H:%MZ}, anchor {anchor.utc:%d %H:%MZ}, oracle {expected:%d %H:%MZ}"
        if inclusive:
            assert got >= anchor.utc, f"before the anchor: {detail}"
        else:
            assert got > anchor.utc, f"not after the anchor: {detail}"
        assert got == expected, f"not the oracle: {detail}"


# =============================================================================
# Controls: rules outside the repeated hour are right at every anchor
# =============================================================================


def _control_cells() -> list:
    out = []
    for zone in ZONES:
        anchors = fold0_anchors(zone) + fold1_anchors(zone)
        names = ["C9", "C2"] if zone == NEW_YORK else ["C9"]
        for anchor in anchors:
            for name in names:
                out.append(pytest.param(zone, anchor, name, id=f"{anchor.name}-{name}"))
    return out


class TestFallBackControls:

    @pytest.mark.parametrize("zone, anchor, name", _control_cells())
    def test_first_occurrence_matches_the_oracle(self, adapter, zone, anchor, name):
        rule = _rules(zone, _REPEATED_HH)[name]
        got = _utc(adapter.first_occurrence(rule, anchor.utc, zone))
        assert got >= anchor.utc
        assert got == _oracle(name, zone, anchor.utc, True, _REPEATED_HH)

    @pytest.mark.parametrize("zone, anchor, name", _control_cells())
    def test_next_occurrence_matches_the_oracle(self, adapter, zone, anchor, name):
        rule = _rules(zone, _REPEATED_HH)[name]
        got = _utc(adapter.next_occurrence(rule, anchor.utc, zone))
        assert got > anchor.utc
        assert got == _oracle(name, zone, anchor.utc, False, _REPEATED_HH)


# =============================================================================
# The user-visible core of D4
# =============================================================================


class TestFallBackUserVisible:

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D4)
    def test_a_reminder_created_in_the_second_pass_gets_the_second_fold_due(self, adapter):
        """Daily 02:30 Berlin, created at 01:10Z on the fall-back day (local 02:10 CET, the second pass): the
        offset-aware due is 02:30 CET = 01:30Z. Today it is 00:30Z, already past, so it fires at the next tick."""
        repeated = REPEATED_HOUR[BERLIN]
        created = transition(BERLIN, "fall", "T").utc + timedelta(minutes=10)
        got = _utc(adapter.first_occurrence(_daily(repeated.wall.hour, repeated.wall.minute), created, BERLIN))
        assert got == repeated.fold1.utc, f"due={got:%H:%MZ}, expected {repeated.fold1.utc:%H:%MZ}"

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D4)
    @pytest.mark.parametrize("zone", ZONES)
    def test_hourly_fires_once_per_real_hour_over_the_fall_back_day(self, adapter, zone):
        """Local midnight to local midnight on the fall-back day is 25 real hours, so 25 hourly occurrences."""
        count = _count_over_the_fall_back_day(adapter, zone, "FREQ=HOURLY", cap=30)
        assert count == 25, f"{count} hourly occurrences for 25 real hours"

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D4)
    @pytest.mark.parametrize("zone", ZONES)
    def test_every_15_minutes_over_the_fall_back_day(self, adapter, zone):
        """25 real hours hold 100 quarter-hours; today the repeated hour's four are lost (96)."""
        count = _count_over_the_fall_back_day(adapter, zone, "FREQ=MINUTELY;INTERVAL=15", cap=110)
        assert count == 100, f"{count} quarter-hour occurrences for 25 real hours"


def _count_over_the_fall_back_day(adapter: DateutilRecurrenceAdapter, zone: str, rule: str, cap: int) -> int:
    """Occurrences from local midnight to the next local midnight of the fall-back day, stepping with
    ``next_occurrence``; at most ``cap`` steps, so an adapter that stops advancing cannot hang the test."""
    z = ZoneInfo(zone)
    day = transition(zone, "fall", "T").utc.astimezone(z).date()
    start = datetime.combine(day, time(0), tzinfo=z).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=z).astimezone(UTC)
    assert end - start == timedelta(hours=25)
    count = 0
    t = _utc(adapter.first_occurrence(rule, start, zone))
    for _ in range(cap):
        if t >= end:
            break
        count += 1
        t = _utc(adapter.next_occurrence(rule, t, zone))
    return count


# =============================================================================
# Spring forward: a skipped time shifts forward (documented behaviour)
# =============================================================================


class TestSpringForward:

    @pytest.mark.parametrize("zone", ZONES)
    def test_next_daily_at_the_skipped_time_lands_one_hour_later_in_local_time(self, adapter, zone):
        """Berlin 02:30 and Helsinki 03:30 on 2026-03-29 → 01:30Z; New York 02:30 on 2026-03-08 → 07:30Z: the
        skipped wall time is read with the offset before the jump, i.e. one local hour later (Berlin 03:30 CEST)."""
        skipped = SKIPPED_HOUR[zone]
        z = ZoneInfo(zone)
        anchor = datetime.combine(skipped.naive.date() - timedelta(days=1), time(12), tzinfo=UTC)
        got = _utc(adapter.next_occurrence(_daily(skipped.naive.hour, skipped.naive.minute), anchor, zone))
        before_jump = transition(zone, "spring", "T-1m").utc.astimezone(z).utcoffset()
        assert got == skipped.naive.replace(tzinfo=UTC) - before_jump
        assert got.astimezone(z).replace(tzinfo=None) == skipped.naive + timedelta(hours=1)

    def test_the_documented_spring_values(self, adapter):
        def utc_at(zone: str, h: int, m: int) -> datetime:
            return transition(zone, "spring", "T").utc.replace(hour=h, minute=m)

        cases = {BERLIN: utc_at(BERLIN, 1, 30), HELSINKI: utc_at(HELSINKI, 1, 30), NEW_YORK: utc_at(NEW_YORK, 7, 30)}
        for zone, expected in cases.items():
            skipped = SKIPPED_HOUR[zone]
            anchor = datetime.combine(skipped.naive.date() - timedelta(days=1), time(12), tzinfo=UTC)
            got = _utc(adapter.next_occurrence(_daily(skipped.naive.hour, skipped.naive.minute), anchor, zone))
            assert got == expected, zone

    @pytest.mark.parametrize("zone", ZONES)
    @pytest.mark.parametrize("label", ["T-1m", "T", "T+1m"])
    @pytest.mark.parametrize("name", ["D", "H", "M", "C9"])
    def test_next_occurrence_is_after_every_spring_anchor(self, adapter, zone, label, name):
        anchor = transition(zone, "spring", label).utc
        got = _utc(adapter.next_occurrence(_rules(zone, _SKIPPED_HH)[name], anchor, zone))
        assert got > anchor
