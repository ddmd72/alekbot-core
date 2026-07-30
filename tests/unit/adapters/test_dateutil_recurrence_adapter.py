"""
Unit tests for DateutilRecurrenceAdapter — the RRULE evaluator behind RecurrencePort.

Two groups matter most:

- **Schedule arithmetic parity.** The RRULE equivalents of every schedule the
  pre-2026-07-30 type+interval model could express (hourly/daily/weekly/monthly,
  month-end clamping, wall-clock preservation) must still land on the same instants.
  These cases moved here from tests/unit/services/test_reminders_service.py when the
  algebra moved out of the service.
- **The schedules that model could NOT express** — several weekdays, several times a
  day, the last Sunday of the month — which are the reason for the change.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter

_UTC = "UTC"
_MADRID = "Europe/Madrid"
_BASE = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)  # a Sunday


@pytest.fixture
def adapter() -> DateutilRecurrenceAdapter:
    return DateutilRecurrenceAdapter()


# =============================================================================
# normalize
# =============================================================================


class TestNormalize:

    def test_accepts_a_bare_rule(self, adapter):
        assert adapter.normalize("FREQ=DAILY") == "FREQ=DAILY"

    def test_strips_the_rrule_prefix_and_upper_cases(self, adapter):
        assert adapter.normalize("rrule:freq=weekly;byday=tu,fr") == "FREQ=WEEKLY;BYDAY=TU,FR"

    def test_rejects_empty(self, adapter):
        with pytest.raises(ValueError):
            adapter.normalize("   ")

    def test_rejects_missing_freq(self, adapter):
        with pytest.raises(ValueError, match="FREQ"):
            adapter.normalize("INTERVAL=2")

    def test_rejects_sub_hourly_freq(self, adapter):
        """The firing cron ticks every 15 minutes — MINUTELY cannot be honoured."""
        with pytest.raises(ValueError, match="FREQ"):
            adapter.normalize("FREQ=MINUTELY")

    def test_rejects_unknown_freq(self, adapter):
        """Replaces the old 'unknown type silently means daily' behaviour: a rule the
        evaluator does not understand is refused at the door, not guessed at."""
        with pytest.raises(ValueError, match="FREQ"):
            adapter.normalize("FREQ=BIANNUAL")

    @pytest.mark.parametrize("bounded", ["FREQ=DAILY;COUNT=5", "FREQ=DAILY;UNTIL=20270101T090000Z"])
    def test_rejects_bounded_rules(self, adapter, bounded):
        """A reminder ends by being deleted — an expired rule would leave a document
        that can never fire again and never surfaces to the user."""
        with pytest.raises(ValueError, match="COUNT/UNTIL"):
            adapter.normalize(bounded)

    def test_rejects_dtstart(self, adapter):
        """The note's due date is the anchor; a rule carrying its own start would
        silently override it."""
        with pytest.raises(ValueError, match="DTSTART"):
            adapter.normalize("DTSTART=20260315T100000;FREQ=DAILY")

    def test_rejects_malformed_segment(self, adapter):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            adapter.normalize("FREQ=DAILY;NONSENSE")

    def test_rejects_a_rule_that_never_occurs(self, adapter):
        """31 February — syntactically fine, never happens. Caught at validation so
        no expansion can spin looking for it."""
        with pytest.raises(ValueError, match="never occurs"):
            adapter.normalize("FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=31")


# =============================================================================
# Schedule arithmetic — parity with the pre-RRULE model
# =============================================================================


class TestNextOccurrenceParity:

    def test_hourly(self, adapter):
        assert adapter.next_occurrence("FREQ=HOURLY", _BASE, _UTC) == _BASE + timedelta(hours=1)

    def test_hourly_interval_2(self, adapter):
        assert adapter.next_occurrence("FREQ=HOURLY;INTERVAL=2", _BASE, _UTC) == _BASE + timedelta(hours=2)

    def test_daily(self, adapter):
        assert adapter.next_occurrence("FREQ=DAILY", _BASE, _UTC) == _BASE + timedelta(days=1)

    def test_weekly(self, adapter):
        assert adapter.next_occurrence("FREQ=WEEKLY", _BASE, _UTC) == _BASE + timedelta(weeks=1)

    def test_monthly(self, adapter):
        base = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = adapter.next_occurrence("FREQ=MONTHLY", base, _UTC)
        assert (result.month, result.day) == (2, 15)

    def test_monthly_skips_a_month_without_the_anchor_day(self, adapter):
        """Deliberate change from the old arithmetic: `relativedelta` clamped Jan 31 →
        Feb 28, RFC 5545 skips to the next month that HAS a 31st (March). Clamping
        silently moved the reminder to a different day of the month; skipping keeps
        "the 31st" meaning the 31st. A user who wants month-end says BYMONTHDAY=-1."""
        base = datetime(2026, 1, 31, 10, 0, 0, tzinfo=timezone.utc)
        result = adapter.next_occurrence("FREQ=MONTHLY", base, _UTC)
        assert (result.month, result.day) == (3, 31)

    def test_result_is_utc(self, adapter):
        result = adapter.next_occurrence("FREQ=DAILY", _BASE, _MADRID)
        assert result.tzinfo == timezone.utc

    def test_wall_clock_survives_a_dst_transition(self, adapter):
        """Madrid springs forward on 2026-03-29. 09:00 local before the change must
        stay 09:00 local after it — the whole reason recurrence is evaluated in the
        user's zone instead of UTC."""
        from zoneinfo import ZoneInfo
        madrid = ZoneInfo(_MADRID)
        before = datetime(2026, 3, 28, 9, 0, tzinfo=madrid).astimezone(timezone.utc)
        after = adapter.next_occurrence("FREQ=DAILY", before, _MADRID)
        assert after.astimezone(madrid).hour == 9

    def test_unknown_timezone_falls_back_to_utc(self, adapter):
        assert adapter.next_occurrence("FREQ=DAILY", _BASE, "Mars/Olympus") == _BASE + timedelta(days=1)

    def test_unusable_rule_returns_none_instead_of_raising(self, adapter):
        """Expansion is on the firing path: a corrupted rule must degrade to 'cannot
        reschedule' (the service then skips the note), never to an exception."""
        assert adapter.next_occurrence("FREQ=NONSENSE", _BASE, _UTC) is None


# =============================================================================
# Schedules the old model could not express
# =============================================================================


class TestComplexSchedules:

    def test_two_weekdays_are_one_reminder(self, adapter):
        """Tue+Fri: the case that used to force the agent to create duplicates."""
        rule = "FREQ=WEEKLY;BYDAY=TU,FR"
        tuesday = datetime(2026, 3, 17, 9, 0, tzinfo=timezone.utc)
        friday = adapter.next_occurrence(rule, tuesday, _UTC)
        assert (friday.month, friday.day) == (3, 20)
        assert adapter.next_occurrence(rule, friday, _UTC).day == 24  # next Tuesday

    def test_twice_a_day(self, adapter):
        rule = "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0"
        morning = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        evening = adapter.next_occurrence(rule, morning, _UTC)
        assert (evening.day, evening.hour) == (15, 20)
        assert adapter.next_occurrence(rule, evening, _UTC).day == 16

    def test_last_sunday_of_the_month(self, adapter):
        rule = "FREQ=MONTHLY;BYDAY=-1SU"
        march = datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc)
        april = adapter.next_occurrence(rule, march, _UTC)
        assert (april.month, april.day) == (4, 26)

    def test_every_other_week(self, adapter):
        rule = "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"
        monday = datetime(2026, 3, 16, 9, 0, tzinfo=timezone.utc)
        assert adapter.next_occurrence(rule, monday, _UTC).day == 30

    def test_specific_days_of_month(self, adapter):
        rule = "FREQ=MONTHLY;BYMONTHDAY=1,15"
        first = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
        assert adapter.next_occurrence(rule, first, _UTC).day == 15


# =============================================================================
# first_occurrence — the creation-time snap
# =============================================================================


class TestFirstOccurrence:

    def test_keeps_a_due_that_already_matches(self, adapter):
        tuesday = datetime(2026, 3, 17, 9, 0, tzinfo=timezone.utc)
        assert adapter.first_occurrence("FREQ=WEEKLY;BYDAY=TU,FR", tuesday, _UTC) == tuesday

    def test_moves_a_due_that_does_not_match(self, adapter):
        """A Wednesday due under a Tue/Fri rule must fire Friday — otherwise the first
        fire happens on a day the schedule never repeats on."""
        wednesday = datetime(2026, 3, 18, 9, 0, tzinfo=timezone.utc)
        snapped = adapter.first_occurrence("FREQ=WEEKLY;BYDAY=TU,FR", wednesday, _UTC)
        assert (snapped.month, snapped.day, snapped.hour) == (3, 20, 9)

    def test_snaps_to_the_next_hour_slot_of_the_same_day(self, adapter):
        noon = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        snapped = adapter.first_occurrence("FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0", noon, _UTC)
        assert (snapped.day, snapped.hour) == (15, 20)


# =============================================================================
# describe
# =============================================================================


class TestDescribe:

    @pytest.mark.parametrize("rule,expected", [
        ("FREQ=DAILY", "every day"),
        ("FREQ=DAILY;INTERVAL=2", "every 2 days"),
        ("FREQ=WEEKLY;BYDAY=TU,FR", "every week on Tue, Fri"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", "every 2 weeks on Mon"),
        ("FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0", "every day at 08:00, 20:00"),
        ("FREQ=MONTHLY;BYDAY=-1SU", "every month on last Sun"),
        ("FREQ=MONTHLY;BYMONTHDAY=1,15", "every month on day 1, day 15"),
        # Negative month days count back from the end — "day -1" reached the user
        # verbatim in the first live test (2026-07-30).
        ("FREQ=MONTHLY;BYMONTHDAY=-1", "every month on the last day"),
        ("FREQ=MONTHLY;BYMONTHDAY=-2", "every month on the 2nd-to-last day"),
    ])
    def test_phrases_common_shapes(self, adapter, rule, expected):
        assert adapter.describe(rule) == expected

    def test_falls_back_to_the_raw_rule(self, adapter):
        """User-facing text, so it must never raise — an unphraseable rule is shown
        verbatim rather than swallowed."""
        assert adapter.describe("FREQ=YEARLY;BYWEEKNO=13") == "every year"
        assert adapter.describe("nonsense") == "nonsense"
