from typing import Dict, FrozenSet, Optional, Type


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


class LLMClientError(LLMError):
    """4xx HTTP response that is NOT 429 (rate limit) — typically 400.

    Covers all non-rate-limit client errors: provider credit/billing exhaustion
    (Anthropic returns HTTP 400 "credit balance too low"), malformed requests,
    unsupported parameters, content-policy rejections. All are DETERMINISTIC —
    retrying the same request or failing over to another provider will not help —
    so this type is intentionally absent from ``FAILOVER_TRIGGER_TYPES``: it
    propagates immediately to the agent as a hard failure.

    It is, however, an operator-actionable signal (top up the provider, fix the
    broken request type), so ``AlertingLLMProxy`` watches for it and pushes a
    Slack alert. ``http_status`` carries the actual code.
    """


class LLMServerError(LLMError):
    """5xx HTTP response that is NOT 503.

    Captured separately from ``LLMUnavailableError`` because 500/502/504 indicate
    different upstream conditions (internal error, bad gateway, gateway timeout)
    and warrant distinct circuit-breaker counting from a clean 503 maintenance
    response. ``http_status`` carries the actual code.
    """


class ProviderBreakerOpenError(LLMError):
    """Raised by ``BaseAgent._call_llm`` when the resilience port reports that
    the primary provider's breaker is open. Distinct from ``LLMUnavailableError``
    (real upstream 503) — this signals "we are not even trying because past
    failures opened the breaker". Internal: callers should never see it
    directly; ``_call_llm`` catches it and routes to fallback.
    """

    def __init__(self, provider_name: str) -> None:
        super().__init__(f"provider {provider_name!r} breaker open", http_status=None)
        self.provider_name = provider_name


class BothProvidersUnavailableError(LLMError):
    """Terminal error: primary failed (or short-circuited) AND fallback is
    unavailable (open breaker, missing, or also failed).

    NOT in ``FAILOVER_TRIGGER_TYPES`` — fallback is exhausted, no further
    routing is possible. Downstream MUST treat as non-retry'able until at
    least ``cooldown_seconds`` elapse — same-process immediate retry will
    hit the same open breakers. Carries ``primary_cause`` for forensics.
    """

    def __init__(
        self,
        primary_name: str,
        fallback_name: Optional[str],
        primary_cause: LLMError,
    ) -> None:
        super().__init__(
            f"both providers unavailable: primary={primary_name!r} "
            f"fallback={fallback_name!r} primary_cause={type(primary_cause).__name__}",
            http_status=None,
        )
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        self.primary_cause = primary_cause


class TranscriptLockedError(LLMError):
    """Terminal error: a primary FAILOVER error hit mid-transcript and
    same-provider retries were exhausted (or the breaker is open), so we
    DELIBERATELY decline cross-provider failover to protect the transcript.

    Enforces the invariant **one delegation transcript = one provider**. A
    multi-turn transcript is provider-specific (``tool_use`` ids, thinking
    blocks, ``raw_content``, cache); re-serving a turn on the fallback provider
    mid-loop produces a mixed transcript that corrupts the next turn (the
    2026-06-29 orphan ``tool_use_id`` → HTTP 400). Distinct from
    ``BothProvidersUnavailableError``: there the fallback is exhausted; HERE the
    fallback is **healthy** and we choose not to use it. NOT in
    ``FAILOVER_TRIGGER_TYPES`` or ``TRANSIENT_RETRY_TYPES`` — it is terminal by
    design and flows to the existing Smart→Quick fallback (clean transcript).
    Carries ``cause`` (the primary FAILOVER error) and ``turn`` for forensics.
    See decisions/transcript_integrity_one_provider.md.
    """

    def __init__(
        self,
        provider_name: str,
        cause: LLMError,
        turn: int,
    ) -> None:
        super().__init__(
            f"transcript locked to provider {provider_name!r}: declined "
            f"cross-provider failover at turn {turn}, "
            f"cause={type(cause).__name__}",
            http_status=None,
        )
        self.provider_name = provider_name
        self.cause = cause
        self.turn = turn


# Stateless policy data: error types that warrant a switch to the fallback
# provider on the first encounter. Lives in the domain, not on a port —
# the decision is pure ``isinstance`` and has no system boundary.
# Read by ``BaseAgent._call_llm``; counted by ``ProviderResiliencePort``.
# ``BothProvidersUnavailableError`` is intentionally absent: it is the
# terminal result of failover exhaustion, not a trigger for further routing.
FAILOVER_TRIGGER_TYPES: FrozenSet[Type[LLMError]] = frozenset({
    LLMRateLimitError,
    LLMUnavailableError,
    LLMTimeoutError,
    LLMNetworkError,
    LLMServerError,
    ProviderBreakerOpenError,
})


# Errors that warrant retrying the SAME provider call before giving up (with
# backoff). Narrower than FAILOVER_TRIGGER_TYPES on purpose: a 429/503 is a
# transient blip that usually clears within seconds, whereas 5xx/network/timeout
# either need a different provider (failover, LLM path) or are not worth a same-
# call retry. This is the SINGLE source of "what is retryable" — shared by the LLM
# path (BaseAgent) and the embedding path (GeminiEmbeddingAdapter) via
# utils.retry.retry_async, so the policy is defined once, not per call site.
# Matches the retry set adopted in docs/.../decisions/typed_retry_policy.md.
TRANSIENT_RETRY_TYPES: FrozenSet[Type[LLMError]] = frozenset({
    LLMRateLimitError,
    LLMUnavailableError,
})


# Log label per failover-trigger type. Co-located with the trigger set so a
# new trigger type without a label fails the invariant test in
# tests/unit/agents/core/test_base_agent_fallback.py loudly. Keeps the
# ``_call_llm`` log path branchless: ``error_type=_ERROR_TYPE_LOG_LABEL[type(e)]``.
_ERROR_TYPE_LOG_LABEL: Dict[Type[LLMError], str] = {
    LLMRateLimitError: "rate_limit",
    LLMUnavailableError: "unavailable",
    LLMTimeoutError: "timeout",
    LLMNetworkError: "network",
    LLMServerError: "server_error",
    ProviderBreakerOpenError: "breaker_open",
}

