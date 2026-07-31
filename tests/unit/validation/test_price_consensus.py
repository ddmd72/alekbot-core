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


class TestScheduleOverridesCatalogs:
    """OpenAI cut the GPT-5.6 prices on 2026-07-30 and the catalogs still quoted the pre-cut
    numbers the next day (LiteLLM luna $1/$6, models.dev $0.1/$0.6; both terra $2.50/$15).
    A dated entry is a price verified at the provider, so it must outrank them — otherwise the
    audit demands a revert to the stale price, or never reaches the schedule at all."""

    AFTER_CUT = date(2026, 7, 31)

    def test_schedule_carries_the_announced_cut(self):
        assert pc.scheduled_price("gpt-5.6-luna", self.AFTER_CUT) == (0.20, 1.20)
        assert pc.scheduled_price("gpt-5.6-terra", self.AFTER_CUT) == (2.00, 12.00)

    def test_schedule_is_silent_before_the_effective_date(self):
        """The cut must not retro-apply: on 2026-07-29 those models were unscheduled."""
        assert pc.scheduled_price("gpt-5.6-luna", date(2026, 7, 29)) is None
        v = verdict((1.0, 6.0), (1.0, 6.0), (1.0, 6.0), model="gpt-5.6-luna")
        assert v.status == pc.CONFIRMED

    def test_schedule_wins_when_catalogs_cannot_agree(self):
        """The luna case — no consensus exists, so coverage rules would have stranded it."""
        v = verdict((0.20, 1.20), (1.0, 6.0), (0.1, 0.6),
                    model="gpt-5.6-luna", today=self.AFTER_CUT)
        assert v.status == pc.CONFIRMED
        assert not v.needs_review
        assert "no catalog consensus" in v.detail

    def test_lagging_catalogs_do_not_demand_a_revert(self):
        """The terra case — the answer must never be `consensus_differs` on the corrected price."""
        v = verdict((2.00, 12.00), (2.5, 15.0), (2.5, 15.0),
                    model="gpt-5.6-terra", today=self.AFTER_CUT)
        assert v.status == pc.SCHEDULE_STALE
        assert v.status != pc.CONSENSUS_DIFFERS
        assert "have not caught up" in v.detail

    def test_billing_left_on_the_pre_cut_price_is_drift(self):
        v = verdict((2.5, 15.0), (2.5, 15.0), (2.5, 15.0),
                    model="gpt-5.6-terra", today=self.AFTER_CUT)
        assert v.status == pc.SCHEDULE_DRIFT
        assert v.needs_review

    def test_catalogs_catching_up_makes_it_plain_confirmed(self):
        v = verdict((2.00, 12.00), (2.0, 12.0), (2.0, 12.0),
                    model="gpt-5.6-terra", today=self.AFTER_CUT)
        assert v.status == pc.CONFIRMED
        assert "no catalog consensus" not in v.detail

    def test_uncovered_scheduled_model_is_still_judged_by_the_schedule(self):
        v = verdict((0.20, 1.20), None, None, model="gpt-5.6-luna", today=self.AFTER_CUT)
        assert v.status == pc.CONFIRMED
        assert "no coverage" in v.detail


class TestReviewClassification:

    @pytest.mark.parametrize("status", sorted(pc.NEEDS_REVIEW))
    def test_every_non_confirmed_status_needs_review(self, status):
        assert pc.Verdict(status, "x").needs_review

    def test_confirmed_is_the_only_clean_status(self):
        assert not pc.Verdict(pc.CONFIRMED, "x").needs_review
        assert pc.CONFIRMED not in pc.NEEDS_REVIEW
