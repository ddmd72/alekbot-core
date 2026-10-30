from typing import Dict, FrozenSet, Iterable, Optional, Type


class LLMError(Exception):
    """Base class for LLM provider errors."""

    def __init__(self, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class LLMRateLimitError(LLMError):
    """Provider returned 429 Rate Limit — request should be retried with a different provider."""


class LLMUnavailableError(LLMError):
    """Provider returned 503 Service Unavailable — provider is temporarily down."""


class BatchLookupInconclusive(LLMUnavailableError):
    """A batch lookup that could not decide (the scan bound was reached before the job was found): neither found nor a
    miss. The caller keeps what it holds and asks again later; it is not an outage of the provider."""


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


class LLMAccountError(LLMClientError):
    """A 4xx about the provider account, not the request: credit balance exhausted, billing inactive, a hard
    billing limit reached.

    Status alone cannot tell it apart — Anthropic sends an exhausted credit balance as HTTP 400
    ``invalid_request_error``, the same status and type as a request refused for its content — so the adapter,
    the only layer that sees the provider's error body, recognises it by the provider's error type, code or
    message. Everywhere else it is an ``LLMClientError`` (no failover, ``AlertingLLMProxy`` alerts). Where a 400
    would be read as a refusal of the input (the drive indexer, USER_DRIVE_INDEXING_RFC §24.14) it is an outage
    for everyone instead. An exhausted OpenAI quota arrives as HTTP 429 and stays ``LLMRateLimitError``.
    """


class LLMRefusalError(LLMClientError):
    """The model declined the request for its content (Claude's safety classifier: HTTP 200 with
    ``stop_reason="refusal"``), raised by the adapter before any answer is used. An ``LLMClientError`` for every
    caller (no failover, alerted); the drive indexer reads it as a refusal of the input, not as invalid output
    (USER_DRIVE_INDEXING_RFC §25.4.6, review m6)."""


class LLMFallbackClientError(LLMClientError):
    """A non-failover client error raised by the FALLBACK provider after a failover (USER_DRIVE_INDEXING_RFC §25.4.4,
    review N7), with ``provider_name`` naming it. Every caller that catches ``LLMClientError`` sees it as before; the
    drive indexer reads it as a charged attempt retried on the primary, never as the primary's refusal of the input
    (the same bytes may be fine on the primary). ``BaseAgent._call_llm`` raises it ``from`` the fallback's own error;
    an ``LLMAccountError`` of the fallback is not wrapped (an outage for everyone, not a refusal)."""

    def __init__(self, message: str, http_status: Optional[int] = None, provider_name: str = "") -> None:
        super().__init__(message, http_status=http_status)
        self.provider_name = provider_name


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


class FactExpiryError(ValueError):
    """An `expiry` a fact tool received is missing, malformed, pinned wrongly or born due. The message is the fix the
    model reads; a ValueError, so every tool-error path already maps it to a failed call."""


class FactBornDueError(FactExpiryError):
    """A well-formed `expiry` that resolves already due at write time. The model is told to retire the candidate
    (DISCARD) or rewrite the fact as history, so a refusal of this kind that is never resent is not a loss."""


class SkillValidationError(ValueError):
    """A skill's text does not satisfy the format rules."""


class SkillCapExceeded(Exception):
    """The user already has the maximum number of custom skills."""


class SkillRejected(Exception):
    """A skill's text was flagged by the security check."""


class SkillDraftNotFound(Exception):
    """No pending draft with that save code for this user."""


class SkillNameReserved(Exception):
    """The name belongs to a system skill."""


class SkillFileMissing(Exception):
    """A file a skill or draft refers to is no longer stored (e.g. a draft file removed by TTL)."""


class MemoryFactsRejected(Exception):
    """Proposed memory facts were flagged by the security check."""


class MemoryDraftNotFound(Exception):
    """No pending memory-facts draft with that save code for this user."""


class OAuthGrantRevokedError(ValueError):
    """The provider rejected a refresh token (``invalid_grant``): the user revoked access,
    changed their password, or the grant expired. Retrying cannot help; only a new
    consent (reconnect) does.

    A ValueError so every existing ``except ValueError`` / auth-keyword path that
    handled the untyped refresh failure keeps handling this one unchanged.
    """


class OAuthScopeInsufficientError(OAuthGrantRevokedError):
    """The provider answered 403 because the access token lacks the service scope (granted too narrowly at
    consent, or the access was removed later in the Google account). Same remedy as a revoked grant, so it is
    a subclass: every ``except OAuthGrantRevokedError`` site already flags a reconnect for it."""


class OAuthScopeNotGrantedError(ValueError):
    """A consent finished without the scope the integration needs (Google's granular consent lets the user
    leave the service checkbox unticked). Nothing is stored; the user must reconnect and tick it."""


class AccessDeniedError(Exception):
    """An authenticated identity is not allowed into the system (whitelist gate).

    Deliberately not a ValueError: web handlers map ValueError to 400, and a
    rejected identity must be a 403 with no session issued.
    """


class WorkspaceRefused(Exception):
    """A workspace command broke a rule (path, folder, kind, caps, a missing source). The message is the fix the
    model reads after `SYSTEM: `; `reason` is the short code in the `[Workspace]` log line."""

    def __init__(self, message: str, reason: str = "refused") -> None:
        super().__init__(message)
        self.reason = reason


class WorkspaceConflict(Exception):
    """A generation precondition failed (HTTP 412): another run wrote or deleted the object since it was read."""


class FactResolutionError(Exception):
    """A fact id could not be followed to a fact the caller may write. The message is the fix the model reads;
    `current_id` names the document the walk stopped at, when there is one; it may itself have successors. Not a
    ValueError: a resolution failure is not invalid input; the caller searches again."""

    def __init__(self, message: str, *, current_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.current_id = current_id


class FactWriteConflict(Exception):
    """A fact document changed between the read and the write (precondition failed, or the transaction aborted)."""

    def __init__(self, fact_id: str) -> None:
        super().__init__(f"fact {fact_id} changed concurrently")
        self.fact_id = fact_id


class InPlaceFieldRefused(Exception):
    """An in-place fact write named a field outside IN_PLACE_FIELDS. A programming error, not invalid input, so not a
    ValueError."""

    def __init__(self, fields: Iterable[str]) -> None:
        self.fields = tuple(sorted(fields))
        super().__init__(f"fields not writable in place: {', '.join(self.fields)}")


class QuestionTransitionError(Exception):
    """A memory-review question cannot make that move from its present state (a closed question cannot be asked, a
    question not yet posted cannot be completed as asked). The store applies a transition inside its transaction and
    treats this as "not applied"; it decides conflicts, never whether a move is legal."""
