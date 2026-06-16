"""Unit tests for AccountUsageStats.usage_for_date — the date-resolution logic
behind a correct daily billing report.

The daily counters rotate lazily (only on the first request of a new day), so a
clock-driven report must resolve them against an explicit calendar date. The bug
this guards: a day with NO activity used to report the last *active* day's leftover
total as if it were that day's.
"""

from datetime import date, datetime, timezone

from src.domain.billing import AccountUsageStats


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)


_YESTERDAY = date(2026, 6, 15)
_DAY_BEFORE = date(2026, 6, 14)
_TODAY = date(2026, 6, 16)


class TestUsageForDate:

    def test_returns_prev_daily_when_its_date_matches_target(self):
        # Normal case: today is active, rotation already moved yesterday into prev_daily.
        u = AccountUsageStats(
            daily_tokens=10, daily_cost=0.01, daily_reset_at=_dt(_TODAY),
            prev_daily_tokens=1000, prev_daily_cost=0.05, prev_daily_date=_YESTERDAY.isoformat(),
        )
        assert u.usage_for_date(_YESTERDAY) == (1000, 0.05)

    def test_returns_daily_when_target_is_last_active_day_not_yet_rotated(self):
        # Yesterday was active and there has been no activity since → no rotation yet,
        # yesterday's total still lives in the live daily_* counters.
        u = AccountUsageStats(
            daily_tokens=777, daily_cost=0.03, daily_reset_at=_dt(_YESTERDAY),
            prev_daily_tokens=0, prev_daily_cost=0.0, prev_daily_date=None,
        )
        assert u.usage_for_date(_YESTERDAY) == (777, 0.03)

    def test_returns_zero_when_target_idle_but_earlier_day_was_active(self):
        # THE BUG: yesterday was idle; the day before was active and then today
        # started, rotating the day-before's total into prev_daily. That value must
        # NOT be reported as yesterday's.
        u = AccountUsageStats(
            daily_tokens=5, daily_cost=0.001, daily_reset_at=_dt(_TODAY),
            prev_daily_tokens=9999, prev_daily_cost=0.99, prev_daily_date=_DAY_BEFORE.isoformat(),
        )
        assert u.usage_for_date(_YESTERDAY) == (0, 0.0)

    def test_returns_zero_when_both_counters_miss_target(self):
        u = AccountUsageStats(
            daily_tokens=5, daily_cost=0.001, daily_reset_at=_dt(_TODAY),
            prev_daily_tokens=100, prev_daily_cost=0.01, prev_daily_date=_DAY_BEFORE.isoformat(),
        )
        assert u.usage_for_date(date(2026, 6, 10)) == (0, 0.0)

    def test_prev_daily_takes_precedence_over_live_counter(self):
        # prev_daily_date matches the target; the live counter sits on another day.
        u = AccountUsageStats(
            daily_tokens=42, daily_cost=0.02, daily_reset_at=_dt(_TODAY),
            prev_daily_tokens=1000, prev_daily_cost=0.05, prev_daily_date=_YESTERDAY.isoformat(),
        )
        assert u.usage_for_date(_YESTERDAY) == (1000, 0.05)

    def test_fresh_account_never_rotated_reports_zero_for_yesterday(self):
        # Brand-new account: daily_reset_at defaults to "now" (today), prev_daily_date None.
        u = AccountUsageStats(daily_reset_at=_dt(_TODAY))
        assert u.usage_for_date(_YESTERDAY) == (0, 0.0)
