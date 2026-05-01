"""
Unit tests for LLM provider exception hierarchy + FAILOVER_TRIGGER_TYPES const.
"""
import pytest

from src.domain.exceptions import (
    FAILOVER_TRIGGER_TYPES,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
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
