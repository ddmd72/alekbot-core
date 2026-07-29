"""Unit tests for the pricing consensus rule (scripts/validation/price_consensus.py).

This logic decides whether a `billing.py` price is trustworthy, and it exists because the
previous audit trusted OpenRouter — a reseller — and produced 6 wrong verdicts out of 8 on
2026-07-29. The rule is therefore pinned here: agreement between two provider-list catalogs
confirms, anything less goes to review, and a dated schedule overrides consensus because
the catalogs publish today's price with no expiry.

Imported as a namespace package (`scripts/` has no __init__.py but the repo root is on
sys.path). `price_consensus` is deliberately side-effect free so importing it here does not
drag in truststore/dotenv the way check_pricing.py would.
"""

from datetime import date

import pytest

from scripts.validation import price_consensus as pc

TODAY = date(2026, 7, 29)
LITE, MDEV = "LiteLLM", "models.dev"


def verdict(ours, litellm, modelsdev, model="some-model", today=TODAY):
    return pc.resolve_verdict(model, ours, {LITE: litellm, MDEV: modelsdev}, today)


class TestConsensus:

    def test_both_agree_with_ours_is_confirmed(self):
        v = verdict((1.0, 6.0), (1.0, 6.0), (1.0, 6.0))
        assert v.status == pc.CONFIRMED
        assert not v.needs_review

    def test_both_agree_against_ours_needs_review(self):
        """The gemini-flash-lite-latest case: catalogs said $0.30/$2.50, we held $0.10/$0.40."""
        v = verdict((0.10, 0.40), (0.30, 2.50), (0.30, 2.50))
        assert v.status == pc.CONSENSUS_DIFFERS
        assert v.needs_review
        assert v.consensus == (0.30, 2.50)
        assert "0.3" in v.detail and "0.1" in v.detail

    def test_sources_disagree_never_picks_a_winner(self):
        """Priced a moving alias: each catalog resolved it to a different generation."""
        v = verdict((1.5, 9.0), (0.30, 2.50), (1.5, 9.0))
        assert v.status == pc.SOURCES_DISAGREE
        assert v.needs_review
        assert v.consensus is None, "a disagreement must not yield a consensus price"

    def test_single_source_is_a_lead_not_a_fact(self):
        v = verdict((10.0, 40.0), (10.0, 40.0), None)
        assert v.status == pc.SINGLE_SOURCE
        assert v.needs_review, "one catalog agreeing is not confirmation"
        assert LITE in v.detail

    def test_no_coverage_does_not_read_as_fine(self):
        v = verdict((2.0, 12.0), None, None)
        assert v.status == pc.UNCOVERED
        assert v.needs_review

    def test_output_only_difference_is_still_a_difference(self):
        """gemini-flash-latest differed on output alone ($9.00 vs $7.50)."""
        v = verdict((1.5, 9.0), (1.5, 7.5), (1.5, 7.5))
        assert v.status == pc.CONSENSUS_DIFFERS


class TestSchedule:

    def test_price_in_force_is_the_latest_arrived_entry(self):
        assert pc.scheduled_price("claude-sonnet-5", date(2026, 7, 29)) == (2.0, 10.0)

    def test_price_in_force_after_the_change(self):
        assert pc.scheduled_price("claude-sonnet-5", date(2026, 9, 1)) == (3.0, 15.0)

    def test_unscheduled_model_has_no_scheduled_price(self):
        assert pc.scheduled_price("gpt-5.6-luna", TODAY) is None

    def test_next_change_is_reported_while_future(self):
        assert pc.next_scheduled_change("claude-sonnet-5", TODAY) == (
            date(2026, 9, 1), (3.0, 15.0))

    def test_next_change_is_none_once_passed(self):
        assert pc.next_scheduled_change("claude-sonnet-5", date(2026, 9, 2)) is None

    def test_upcoming_change_is_surfaced_on_the_verdict(self):
        v = verdict((3.0, 15.0), (2.0, 10.0), (2.0, 10.0), model="claude-sonnet-5")
        assert v.upcoming == (date(2026, 9, 1), (3.0, 15.0))


class TestHoldFinalPricePolicy:
    """billing.py deliberately holds Sonnet 5's post-promo price so spend is never
    under-reported. The audit must recognise that as intentional, not as drift."""

    def test_holding_the_final_price_is_confirmed(self):
        v = verdict((3.0, 15.0), (2.0, 10.0), (2.0, 10.0), model="claude-sonnet-5")
        assert v.status == pc.CONFIRMED
        assert not v.needs_review

    def test_the_over_report_is_stated_not_hidden(self):
        v = verdict((3.0, 15.0), (2.0, 10.0), (2.0, 10.0), model="claude-sonnet-5")
        assert "1.50x high" in v.detail

    def test_neither_scheduled_price_is_drift(self):
        """Holding today's promo price instead of the final one violates the policy."""
        v = verdict((2.0, 10.0), (2.0, 10.0), (2.0, 10.0), model="claude-sonnet-5")
        assert v.status == pc.SCHEDULE_DRIFT
        assert v.needs_review

    def test_after_the_change_date_no_over_report_is_claimed(self):
        v = verdict((3.0, 15.0), (3.0, 15.0), (3.0, 15.0),
                    model="claude-sonnet-5", today=date(2026, 9, 1))
        assert v.status == pc.CONFIRMED
        assert "high until then" not in v.detail

    def test_catalogs_contradicting_our_schedule_flags_the_schedule(self):
        """If Anthropic changes the promo, the schedule — not billing.py — is what is stale."""
        v = verdict((3.0, 15.0), (2.5, 12.0), (2.5, 12.0), model="claude-sonnet-5")
        assert v.status == pc.SCHEDULE_STALE
        assert v.needs_review


class TestReviewClassification:

    @pytest.mark.parametrize("status", sorted(pc.NEEDS_REVIEW))
    def test_every_non_confirmed_status_needs_review(self, status):
        assert pc.Verdict(status, "x").needs_review

    def test_confirmed_is_the_only_clean_status(self):
        assert not pc.Verdict(pc.CONFIRMED, "x").needs_review
        assert pc.CONFIRMED not in pc.NEEDS_REVIEW
