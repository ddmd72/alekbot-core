from typing import FrozenSet, Optional, Type


class LLMError(Exception):
    """Base class for LLM provider errors."""

    def __init__(self, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class LLMRateLimitError(LLMError):
    """Provider returned 429 Rate Limit — request should be retried with a different provider."""


class LLMUnavailableError(LLMError):
    """Provider returned 503 Service Unavailable — provider is temporarily down."""


class LLMTimeoutError(LLMError):
    """Wall-clock budget exhausted before the provider responded.

    Adapters wrap their SDK call in ``asyncio.wait_for(..., timeout=request.timeout)``
    and translate ``asyncio.TimeoutError`` to this. ``http_status`` is ``None`` —
    the request never completed an HTTP round-trip.
    """


class LLMNetworkError(LLMError):
    """TCP/DNS-level failure before any HTTP response was received.

    Distinct from ``LLMUnavailableError`` (HTTP 503 — request reached the server
    and was rejected) and from ``LLMTimeoutError`` (request timed out client-side).
    Maps to provider SDK ``ConnectError`` / DNS failures / SSL handshake errors.
    """


class LLMServerError(LLMError):
    """5xx HTTP response that is NOT 503.

    Captured separately from ``LLMUnavailableError`` because 500/502/504 indicate
    different upstream conditions (internal error, bad gateway, gateway timeout)
    and warrant distinct circuit-breaker counting from a clean 503 maintenance
    response. ``http_status`` carries the actual code.
    """


# Stateless policy data: error types that warrant a switch to the fallback
# provider on the first encounter. Lives in the domain, not on a port —
# the decision is pure ``isinstance`` and has no system boundary.
# Read by ``BaseAgent._call_llm``; counted by ``ProviderResiliencePort``.
FAILOVER_TRIGGER_TYPES: FrozenSet[Type[LLMError]] = frozenset({
    LLMRateLimitError,
    LLMUnavailableError,
    LLMTimeoutError,
    LLMNetworkError,
    LLMServerError,
})

