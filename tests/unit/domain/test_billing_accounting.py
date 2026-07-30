"""Unit tests for the per-execution token ledger and the usage-increment outcome.

Both were introduced 2026-07-28 with the billing over-count fix (see
tests/unit/agents/test_base_agent_billing_isolation.py for the concurrency contract
they exist to support).
"""

import pytest

from src.domain.billing import (
    DEFAULT_DAILY_COST_LIMIT,
    BillingAccount,
    TokenLedger,
    UsageIncrement,
)


class TestTokenLedger:

    def test_starts_empty(self):
        ledger = TokenLedger()
        assert ledger.total_tokens == 0
        assert ledger.is_empty
        assert ledger.account_id is None

    def test_add_accumulates_every_leg(self):
        ledger = TokenLedger()
        ledger.add(
            "gpt-5.6-luna",
            prompt_tokens=10,
            completion_tokens=5,
            cache_read_tokens=4,
            cache_creation_tokens=2,
        )
        leg = ledger.by_model["gpt-5.6-luna"]
        assert (leg.prompt_tokens, leg.completion_tokens) == (10, 5)
        assert (leg.cache_read_tokens, leg.cache_creation_tokens) == (4, 2)

    def test_add_is_cumulative_across_turns(self):
        """One execution, several LLM turns on one model — the ledger sums them."""
        ledger = TokenLedger()
        for _ in range(3):
            ledger.add(
                "gpt-5.6-luna", prompt_tokens=10, completion_tokens=5, cache_read_tokens=1
            )
        leg = ledger.by_model["gpt-5.6-luna"]
        assert leg.prompt_tokens == 30
        assert leg.completion_tokens == 15
        assert leg.cache_read_tokens == 3
        assert ledger.total_tokens == 48

    def test_add_defaults_to_zero_for_omitted_legs(self):
        ledger = TokenLedger()
        ledger.add("gpt-5.6-luna", prompt_tokens=7)
        assert ledger.total_tokens == 7

    def test_total_includes_both_cache_legs(self):
        """Cache reads and writes are billed, so they count toward the total."""
        ledger = TokenLedger()
        ledger.add(
            "gpt-5.6-luna",
            prompt_tokens=1,
            completion_tokens=2,
            cache_read_tokens=4,
            cache_creation_tokens=8,
        )
        assert ledger.total_tokens == 15

    def test_is_empty_false_when_only_cache_accrued(self):
        """A fully cached turn still costs money — it must not be treated as no-op."""
        ledger = TokenLedger()
        ledger.add("gpt-5.6-luna", cache_read_tokens=100)
        assert not ledger.is_empty

    def test_cost_matches_model_pricing(self):
        """claude-sonnet-4-6: $3/M input, $15/M output, cache read 0.1x, write 1.25x."""
        ledger = TokenLedger()
        ledger.add(
            "claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
            cache_read_tokens=1000,
            cache_creation_tokens=200,
        )
        expected = (
            (100 / 1_000_000) * 3.0
            + (50 / 1_000_000) * 15.0
            + (1000 / 1_000_000) * 3.0 * 0.1
            + (200 / 1_000_000) * 3.0 * 1.25
        )
        assert ledger.cost() == pytest.approx(expected, abs=1e-9)

    def test_cost_is_zero_for_unknown_model(self):
        """Unpriced model → 0.0, consistent with calculate_cost."""
        ledger = TokenLedger()
        ledger.add("no-such-model", prompt_tokens=1000, completion_tokens=1000)
        assert ledger.cost() == 0.0

    def test_account_id_travels_with_the_ledger(self):
        """The flush reads account_id off the ledger, not off the agent instance."""
        assert TokenLedger(account_id="acc-9").account_id == "acc-9"

    # --- per-model pricing (TD-7) -------------------------------------------

    def test_cost_prices_each_model_at_its_own_rate(self):
        """The defect TD-7 fixes: an execution spanning two models is the SUM of
        each model's own price — not one model's price applied to all tokens.

        Reproduces the live shape: Smart escalates a turn to sol ($5/$30) while the
        agent default stays luna ($1/$6).
        """
        ledger = TokenLedger()
        ledger.add("gpt-5.6-luna", prompt_tokens=1_000, completion_tokens=1_000)
        ledger.add("gpt-5.6-sol", prompt_tokens=1_000, completion_tokens=1_000)

        luna = (1_000 / 1_000_000) * 1.0 + (1_000 / 1_000_000) * 6.0
        sol = (1_000 / 1_000_000) * 5.0 + (1_000 / 1_000_000) * 30.0
        assert ledger.cost() == pytest.approx(luna + sol, abs=1e-9)
        # Neither single-model reading is right — that is exactly the old behavior.
        assert ledger.cost() != pytest.approx(2 * luna, abs=1e-9)
        assert ledger.cost() != pytest.approx(2 * sol, abs=1e-9)
        assert ledger.total_tokens == 4_000

    def test_cost_prices_a_cheaper_downgraded_leg_at_its_own_rate(self):
        """WebSearch runs `fetch_url` on ECO (nano) while its default tier is luna —
        the cheap leg must not be billed at the expensive default (the over-report leg
        introduced 2026-07-29)."""
        ledger = TokenLedger()
        ledger.add("gpt-5.4-nano", prompt_tokens=10_000, completion_tokens=2_000)
        expected = (10_000 / 1_000_000) * 0.20 + (2_000 / 1_000_000) * 1.25
        assert ledger.cost() == pytest.approx(expected, abs=1e-9)

    def test_unknown_model_leg_does_not_void_the_priced_legs(self):
        """An unpriced model contributes 0.0 and leaves the rest of the sum intact."""
        ledger = TokenLedger()
        ledger.add("gpt-5.6-luna", prompt_tokens=1_000, completion_tokens=1_000)
        ledger.add("no-such-model", prompt_tokens=9_000, completion_tokens=9_000)
        expected = (1_000 / 1_000_000) * 1.0 + (1_000 / 1_000_000) * 6.0
        assert ledger.cost() == pytest.approx(expected, abs=1e-9)
        assert ledger.total_tokens == 20_000  # tokens still counted

    def test_dominant_model_is_the_costliest_leg(self):
        """The label sent to record_usage names the priciest model of the execution,
        even when a cheaper model carried more tokens."""
        ledger = TokenLedger()
        ledger.add("gpt-5.6-luna", prompt_tokens=100_000)
        ledger.add("gpt-5.6-sol", prompt_tokens=50_000)
        assert ledger.dominant_model == "gpt-5.6-sol"

    def test_dominant_model_is_none_when_nothing_accrued(self):
        assert TokenLedger().dominant_model is None


class TestUsageIncrementCrossing:

    def _increment(self, before: float, after: float, limit: float = 5.0) -> UsageIncrement:
        return UsageIncrement(
            daily_cost_before=before, daily_cost_after=after, daily_cost_limit=limit
        )

    def test_crossing_detected(self):
        assert self._increment(4.90, 5.10).crossed_daily_limit

    def test_exactly_reaching_the_limit_counts_as_crossing(self):
        assert self._increment(4.0, 5.0).crossed_daily_limit

    def test_below_limit_is_not_a_crossing(self):
        assert not self._increment(1.0, 2.0).crossed_daily_limit

    def test_already_over_limit_is_not_a_crossing(self):
        """Fires once per day: subsequent increments on an over-budget day stay quiet.

        This is also what keeps the currently inflated counters from alerting on every
        request until the next daily rotation.
        """
        assert not self._increment(9.0, 9.5).crossed_daily_limit

    def test_rotation_resets_the_comparison(self):
        """A new day starts from 0 even if the previous day ended far over budget."""
        assert not self._increment(0.0, 0.20).crossed_daily_limit

    def test_single_increment_can_cross_from_zero(self):
        """One very expensive execution on a fresh day still trips the alert."""
        assert self._increment(0.0, 12.0).crossed_daily_limit


class TestDailyCostLimitDefault:

    def test_account_defaults_to_shared_constant(self):
        assert BillingAccount().daily_cost_limit == DEFAULT_DAILY_COST_LIMIT

    def test_default_is_five_dollars(self):
        """Sized against measured usage: a normal day is ~$3.75."""
        assert DEFAULT_DAILY_COST_LIMIT == 5.0

    def test_limit_is_overridable_per_account(self):
        assert BillingAccount(daily_cost_limit=25.0).daily_cost_limit == 25.0
