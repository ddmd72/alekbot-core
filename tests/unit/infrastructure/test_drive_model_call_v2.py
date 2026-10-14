"""Search v2 additions to the drive model calls (USER_DRIVE_INDEXING_RFC §25.4.4, §25.4.6): the fallback's refusal is a
charged attempt (never a refusal of the input), every agent failure maps to one ModelReplyStatus, and a ledger's
spend splits into the model that served, the whole cost and the fallback's share."""
import pytest

from src.domain.billing import TokenLedger, calculate_cost
from src.domain.drive_enrichment_calls import ModelReplyStatus as S
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.exceptions import (
    LLMAccountError, LLMClientError, LLMFallbackClientError, LLMRateLimitError, LLMServerError,
)
from src.infrastructure.drive_model_call import (
    CallCounter, DriveInputRefused, DriveInputTooLarge, DriveModelCallFailed, DriveModelTimedOut, DriveOutputInvalid,
    classify_model_error, ledger_spend, reply_status, with_usage,
)


class TestFallbackRefusal:
    @pytest.mark.parametrize("status", [400, 413, 422, None])
    def test_the_fallbacks_refusal_is_a_charged_outage_never_a_refusal(self, status):
        mapped = classify_model_error(LLMFallbackClientError("x", http_status=status, provider_name="gemini"),
                                      "drive_profiler")
        assert isinstance(mapped, DriveModelUnavailable) and not isinstance(mapped, DriveModelCallFailed)
        assert mapped.charges_file is True

    def test_the_primarys_refusal_is_still_a_refusal(self):
        assert isinstance(classify_model_error(LLMClientError("x", http_status=400), "drive_profiler"),
                          DriveInputRefused)


class TestReplyStatus:
    def _used(self, exc, calls):
        return with_usage(exc, CallCounter(calls=calls), 0.001)

    @pytest.mark.parametrize("exc", [DriveInputRefused("r"), DriveInputTooLarge("t")])
    def test_refusals(self, exc):
        assert reply_status(self._used(exc, 1)) is S.REFUSED

    def test_invalid_output_is_charged_without_backoff(self):
        assert reply_status(self._used(DriveOutputInvalid("bad"), 2)) is S.INVALID

    def test_another_per_input_failure_is_invalid(self):
        """A refusal raised before any HTTP exchange (an adapter's own) is charged, but backs nothing off."""
        assert reply_status(self._used(DriveModelCallFailed("pre-http"), 1)) is S.INVALID

    @pytest.mark.parametrize("exc", [DriveModelUnavailable("5xx", charges_file=True), DriveModelTimedOut("t")])
    def test_a_charging_outage_after_a_call_is_failed(self, exc):
        assert reply_status(self._used(exc, 1)) is S.FAILED

    def test_a_charging_outage_without_a_call_is_unavailable(self):
        """A missing prompt, a user or provider that cannot be built: nothing was sent, nothing is charged."""
        assert reply_status(self._used(DriveModelUnavailable("prompt", charges_file=True), 0)) is S.UNAVAILABLE

    @pytest.mark.parametrize("cause", [LLMRateLimitError("429", http_status=429),
                                       LLMAccountError("billing", http_status=400)])
    def test_an_outage_for_everyone_is_unavailable(self, cause):
        assert reply_status(self._used(classify_model_error(cause, "drive_passages"), 1)) is S.UNAVAILABLE

    def test_the_fallbacks_refusal_is_failed(self):
        mapped = classify_model_error(LLMFallbackClientError("x", http_status=400, provider_name="gemini"), "p")
        assert reply_status(self._used(mapped, 2)) is S.FAILED

    def test_a_server_error_after_failover_is_failed(self):
        assert reply_status(self._used(classify_model_error(LLMServerError("500", http_status=500), "p"), 1)) \
            is S.FAILED


class TestLedgerSpend:
    def test_primary_only(self):
        ledger = TokenLedger()
        ledger.add("gpt-6-luna", 1000, 200)
        spend = ledger_spend(ledger, primary_model="gpt-6-luna", fallback_model="gemini-3.8-flash")
        assert spend.model == "gpt-6-luna" and spend.fallback_usd == 0.0
        assert spend.usd == pytest.approx(calculate_cost("gpt-6-luna", 1000, 200))

    def test_the_fallbacks_share_is_in_dollars_and_names_the_model_when_it_served_alone(self):
        ledger = TokenLedger()
        ledger.add("gemini-3.8-flash", 1000, 200)
        spend = ledger_spend(ledger, primary_model="gpt-6-luna", fallback_model="gemini-3.8-flash")
        assert spend.model == "gemini-3.8-flash"
        assert spend.usd == spend.fallback_usd == pytest.approx(calculate_cost("gemini-3.8-flash", 1000, 200))

    def test_mixed_names_the_primary_and_splits_the_cost(self):
        ledger = TokenLedger()
        ledger.add("gpt-6-luna", 1000, 200)
        ledger.add("gemini-3.8-flash", 500, 100)
        spend = ledger_spend(ledger, primary_model="gpt-6-luna", fallback_model="gemini-3.8-flash")
        assert spend.model == "gpt-6-luna"
        assert spend.fallback_usd == pytest.approx(calculate_cost("gemini-3.8-flash", 500, 100))
        assert spend.usd == pytest.approx(ledger.cost())

    def test_no_fallback_configured(self):
        ledger = TokenLedger()
        spend = ledger_spend(ledger, primary_model="gpt-6-luna", fallback_model=None)
        assert (spend.model, spend.usd, spend.fallback_usd) == ("gpt-6-luna", 0.0, 0.0)

    def test_with_usage_carries_the_fallback_share_and_model(self):
        exc = with_usage(DriveOutputInvalid("x"), CallCounter(calls=2), 0.5, fallback_cost=0.2, model="m")
        assert (exc.model_calls, exc.model_cost, exc.model_fallback_cost, exc.model_name) == (2, 0.5, 0.2, "m")
