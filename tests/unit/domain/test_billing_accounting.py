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
            prompt_tokens=10,
            completion_tokens=5,
            cache_read_tokens=4,
            cache_creation_tokens=2,
        )
        assert (ledger.prompt_tokens, ledger.completion_tokens) == (10, 5)
        assert (ledger.cache_read_tokens, ledger.cache_creation_tokens) == (4, 2)

    def test_add_is_cumulative_across_turns(self):
        """One execution, several LLM turns — the ledger sums them."""
        ledger = TokenLedger()
        for _ in range(3):
            ledger.add(prompt_tokens=10, completion_tokens=5, cache_read_tokens=1)
        assert ledger.prompt_tokens == 30
        assert ledger.completion_tokens == 15
        assert ledger.cache_read_tokens == 3
        assert ledger.total_tokens == 48

    def test_add_defaults_to_zero_for_omitted_legs(self):
        ledger = TokenLedger()
        ledger.add(prompt_tokens=7)
        assert ledger.total_tokens == 7

    def test_total_includes_both_cache_legs(self):
        """Cache reads and writes are billed, so they count toward the total."""
        ledger = TokenLedger(
            prompt_tokens=1,
            completion_tokens=2,
            cache_read_tokens=4,
            cache_creation_tokens=8,
        )
        assert ledger.total_tokens == 15

    def test_is_empty_false_when_only_cache_accrued(self):
        """A fully cached turn still costs money — it must not be treated as no-op."""
        ledger = TokenLedger(cache_read_tokens=100)
        assert not ledger.is_empty

    def test_cost_matches_model_pricing(self):
        """claude-sonnet-4-6: $3/M input, $15/M output, cache read 0.1x, write 1.25x."""
        ledger = TokenLedger(
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
        assert ledger.cost("claude-sonnet-4-6") == pytest.approx(expected, abs=1e-9)

    def test_cost_is_zero_for_unknown_model(self):
        """Unpriced model → 0.0, consistent with calculate_cost."""
        ledger = TokenLedger(prompt_tokens=1000, completion_tokens=1000)
        assert ledger.cost("no-such-model") == 0.0

    def test_account_id_travels_with_the_ledger(self):
        """The flush reads account_id off the ledger, not off the agent instance."""
        assert TokenLedger(account_id="acc-9").account_id == "acc-9"


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
