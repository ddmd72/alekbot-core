"""Unit tests for AccountUsageStats.usage_for_date — the date-resolution logic
behind a correct daily billing report.

The daily counters rotate lazily (only on the first request of a new day), so a
clock-driven report must resolve them against an explicit calendar date. The bug
this guards: a day with NO activity used to report the last *active* day's leftover
total as if it were that day's.
"""

from datetime import date, datetime, timezone

from src.domain.billing import AccountUsageStats, calculate_external_cost


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


class TestCalculateExternalCost:
    """calculate_external_cost() prices non-token REST-API services (image/video
    generation) — companion to calculate_cost() (LLM token pricing). See
    docs/10_rfcs/VIDEO_GENERATION_RFC.md §3.11 decision #11.
    """

    def test_video_cost_scales_with_duration(self):
        assert calculate_external_cost("grok-imagine-video-1.5", duration_s=5) == 0.40
        assert calculate_external_cost("grok-imagine-video-1.5", duration_s=10) == 0.80

    def test_video_without_duration_returns_zero(self):
        # duration_s is required for a per-second-priced service — omitting it
        # must never silently bill $0.08 for "one second," it must bill nothing.
        assert calculate_external_cost("grok-imagine-video-1.5") == 0.0

    def test_image_generate_cost_defaults_to_1k_medium_tier(self):
        # No resolution/quality passed -> defaults to "1k"/"medium", matching
        # ImageGenerationAgent's own defaults. NOT flat $0.04 — that was this
        # PR's own earlier (wrong) assumption, corrected 2026-08-24 after a live
        # API probe showed quality genuinely changes the billed price.
        assert calculate_external_cost("grok-imagine-image-2.0") == 0.06

    def test_image_generate_cost_all_four_tiers(self):
        # Live-verified against docs.x.ai's pricing catalog + a direct API probe
        # (2 real calls, compared response.usage.cost_in_usd_ticks).
        assert calculate_external_cost("grok-imagine-image-2.0", resolution="1k", quality="low") == 0.04
        assert calculate_external_cost("grok-imagine-image-2.0", resolution="2k", quality="low") == 0.06
        assert calculate_external_cost("grok-imagine-image-2.0", resolution="1k", quality="medium") == 0.06
        assert calculate_external_cost("grok-imagine-image-2.0", resolution="2k", quality="medium") == 0.08

    def test_image_generate_unknown_tier_returns_zero(self):
        # Same fail-open contract as an unpriced model — an invalid combination
        # must never silently fall back to a guessed price.
        assert calculate_external_cost("grok-imagine-image-2.0", resolution="4k", quality="low") == 0.0

    def test_image_edit_cost_adds_input_surcharge_to_tier_price(self):
        # Default (1k/medium) tier $0.06 + $0.01 per-input-image surcharge = $0.07.
        assert calculate_external_cost("grok-imagine-image-2.0-edit") == 0.07
        assert calculate_external_cost(
            "grok-imagine-image-2.0-edit", resolution="2k", quality="medium"
        ) == 0.09

    def test_image_cost_ignores_duration_kwarg(self):
        # duration_s is meaningless for an image service — passing it must not
        # change the result (guards against a future refactor accidentally
        # multiplying image services too, the way per-second video pricing does).
        assert calculate_external_cost("grok-imagine-image-2.0", duration_s=999) == 0.06

    def test_unknown_service_returns_zero(self):
        # Same fail-open contract as calculate_cost() for an unpriced model.
        assert calculate_external_cost("unknown-service-xyz") == 0.0
