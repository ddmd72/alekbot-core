"""
Unit tests for LLM provider exception hierarchy + FAILOVER_TRIGGER_TYPES const.
"""
import pytest

from src.domain.exceptions import (
    _ERROR_TYPE_LOG_LABEL,
    FAILOVER_TRIGGER_TYPES,
    BothProvidersUnavailableError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
    ProviderBreakerOpenError,
)


class TestLLMErrorBase:
    def test_constructs_with_message_only(self):
        err = LLMError("boom")
        assert str(err) == "boom"
        assert err.http_status is None

    def test_constructs_with_http_status(self):
        err = LLMError("boom", http_status=418)
        assert err.http_status == 418

    def test_is_exception(self):
        assert issubclass(LLMError, Exception)


class TestSubclassHierarchy:
    @pytest.mark.parametrize(
        "exc_type, status",
        [
            (LLMRateLimitError, 429),
            (LLMUnavailableError, 503),
            (LLMServerError, 500),
        ],
    )
    def test_http_carrying_subclasses(self, exc_type, status):
        err = exc_type("x", http_status=status)
        assert isinstance(err, LLMError)
        assert err.http_status == status

    def test_timeout_has_no_http_status_by_default(self):
        # No HTTP round-trip ever completed.
        err = LLMTimeoutError("budget exhausted")
        assert err.http_status is None

    def test_network_distinct_from_unavailable(self):
        # 503 reaches the server; network error never does. Hierarchy
        # must let callers distinguish — neither inherits from the other.
        assert not issubclass(LLMNetworkError, LLMUnavailableError)
        assert not issubclass(LLMUnavailableError, LLMNetworkError)

    def test_server_error_distinct_from_unavailable(self):
        assert not issubclass(LLMServerError, LLMUnavailableError)
        assert not issubclass(LLMUnavailableError, LLMServerError)

    @pytest.mark.parametrize(
        "exc_type",
        [
            LLMRateLimitError,
            LLMUnavailableError,
            LLMTimeoutError,
            LLMNetworkError,
            LLMServerError,
        ],
    )
    def test_all_inherit_from_llm_error(self, exc_type):
        assert issubclass(exc_type, LLMError)
        with pytest.raises(LLMError):
            raise exc_type("test")


class TestFailoverTriggerTypes:
    def test_is_frozenset(self):
        # Domain const — must be immutable.
        assert isinstance(FAILOVER_TRIGGER_TYPES, frozenset)

    @pytest.mark.parametrize(
        "exc_type",
        [
            LLMRateLimitError,
            LLMUnavailableError,
            LLMTimeoutError,
            LLMNetworkError,
            LLMServerError,
        ],
    )
    def test_all_concrete_subclasses_trigger_failover(self, exc_type):
        assert exc_type in FAILOVER_TRIGGER_TYPES

    def test_base_llm_error_does_not_trigger_failover(self):
        # Catching a bare LLMError means an unknown subclass — caller
        # must not silently route around the primary provider.
        assert LLMError not in FAILOVER_TRIGGER_TYPES

    def test_unknown_subclass_does_not_trigger_failover(self):
        class CustomLLMError(LLMError):
            pass

        assert CustomLLMError not in FAILOVER_TRIGGER_TYPES

    def test_isinstance_check_works_for_subclasses(self):
        # The intended caller pattern: ``isinstance(error, tuple(FAILOVER_TRIGGER_TYPES))``.
        err = LLMTimeoutError("x")
        assert isinstance(err, tuple(FAILOVER_TRIGGER_TYPES))

    def test_isinstance_check_rejects_unknown(self):
        class CustomLLMError(LLMError):
            pass

        err = CustomLLMError("x")
        assert not isinstance(err, tuple(FAILOVER_TRIGGER_TYPES))


class TestProviderBreakerOpenError:
    def test_inherits_from_llm_error(self):
        assert issubclass(ProviderBreakerOpenError, LLMError)

    def test_carries_provider_name(self):
        err = ProviderBreakerOpenError("gemini")
        assert err.provider_name == "gemini"

    def test_message_includes_provider_name(self):
        err = ProviderBreakerOpenError("claude")
        assert "claude" in str(err)
        assert "breaker open" in str(err)

    def test_no_http_status(self):
        err = ProviderBreakerOpenError("gemini")
        assert err.http_status is None

    def test_in_failover_trigger_types(self):
        # Short-circuit MUST route to fallback — same path as adapter-translated errors.
        assert ProviderBreakerOpenError in FAILOVER_TRIGGER_TYPES

    def test_isinstance_via_failover_tuple(self):
        err = ProviderBreakerOpenError("gemini")
        assert isinstance(err, tuple(FAILOVER_TRIGGER_TYPES))


class TestBothProvidersUnavailableError:
    def test_inherits_from_llm_error(self):
        assert issubclass(BothProvidersUnavailableError, LLMError)

    def test_carries_all_context(self):
        cause = LLMRateLimitError("primary 429", http_status=429)
        err = BothProvidersUnavailableError("gemini", "claude", primary_cause=cause)
        assert err.primary_name == "gemini"
        assert err.fallback_name == "claude"
        assert err.primary_cause is cause

    def test_accepts_none_fallback_name(self):
        # When ctx.fallback_provider is None — uniform terminal type.
        cause = LLMUnavailableError("primary 503", http_status=503)
        err = BothProvidersUnavailableError("gemini", None, primary_cause=cause)
        assert err.fallback_name is None

    def test_message_includes_provider_names_and_cause_type(self):
        cause = LLMTimeoutError("budget exhausted")
        err = BothProvidersUnavailableError("gemini", "claude", primary_cause=cause)
        msg = str(err)
        assert "gemini" in msg
        assert "claude" in msg
        assert "LLMTimeoutError" in msg

    def test_no_http_status(self):
        cause = LLMRateLimitError("x", http_status=429)
        err = BothProvidersUnavailableError("g", "c", primary_cause=cause)
        assert err.http_status is None

    def test_NOT_in_failover_trigger_types(self):
        # Terminal error — exhausted, not a trigger for further routing.
        # Catching this in FAILOVER tuple would create infinite loops.
        assert BothProvidersUnavailableError not in FAILOVER_TRIGGER_TYPES

    def test_isinstance_via_failover_tuple_returns_false(self):
        cause = LLMRateLimitError("x", http_status=429)
        err = BothProvidersUnavailableError("g", "c", primary_cause=cause)
        assert not isinstance(err, tuple(FAILOVER_TRIGGER_TYPES))


class TestErrorTypeLogLabel:
    def test_covers_all_failover_trigger_types(self):
        # Invariant: every FAILOVER_TRIGGER_TYPES member has a log label.
        # Catches drift if someone adds a new trigger type without label.
        missing = FAILOVER_TRIGGER_TYPES - set(_ERROR_TYPE_LOG_LABEL.keys())
        assert not missing, f"FAILOVER_TRIGGER_TYPES missing labels: {missing}"

    def test_no_extra_entries(self):
        # Reverse invariant: no labels for types that aren't triggers.
        # Keeps the dict tightly coupled to the trigger set.
        extra = set(_ERROR_TYPE_LOG_LABEL.keys()) - FAILOVER_TRIGGER_TYPES
        assert not extra, f"_ERROR_TYPE_LOG_LABEL has stale entries: {extra}"

    def test_labels_are_unique(self):
        labels = list(_ERROR_TYPE_LOG_LABEL.values())
        assert len(labels) == len(set(labels)), f"Duplicate labels: {labels}"

    @pytest.mark.parametrize(
        "exc_type, expected_label",
        [
            (LLMRateLimitError, "rate_limit"),
            (LLMUnavailableError, "unavailable"),
            (LLMTimeoutError, "timeout"),
            (LLMNetworkError, "network"),
            (LLMServerError, "server_error"),
            (ProviderBreakerOpenError, "breaker_open"),
        ],
    )
    def test_specific_labels(self, exc_type, expected_label):
        assert _ERROR_TYPE_LOG_LABEL[exc_type] == expected_label

    def test_lookup_by_exception_type_works(self):
        # The intended caller pattern in BaseAgent._call_llm:
        # error_type=_ERROR_TYPE_LOG_LABEL[type(e)]
        err = LLMTimeoutError("x")
        assert _ERROR_TYPE_LOG_LABEL[type(err)] == "timeout"
