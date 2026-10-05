"""Shared transient-retry executor.

Single backoff/retry mechanism used by every path that calls a flaky external
provider: the LLM path (``BaseAgent``) and the embedding path
(``GeminiEmbeddingAdapter``). The *policy* lives in ``domain.retry_policy``
(``RetryPolicy``) and the *classification* in ``domain.exceptions``
(``TRANSIENT_RETRY_TYPES``); this module is the *executor* that ties them
together so neither is reimplemented per call site.

It deliberately does NOT own:
  - which errors are transient (caller passes ``retryable``),
  - failover / circuit-breaking (LLM-path orchestration, stays in BaseAgent),
  - error translation (adapters map SDK errors to typed domain errors first).
"""

import asyncio
import random
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

from ..domain.retry_policy import RetryPolicy
from .logger import logger

T = TypeVar("T")

# Called once per retry (not on the final failure) with (error, attempt, backoff_seconds).
OnRetry = Callable[[BaseException, int, float], None]


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    retryable: Tuple[Type[BaseException], ...],
    on_retry: Optional[OnRetry] = None,
) -> T:
    """Call ``fn`` with exponential backoff + jitter, retrying only ``retryable`` errors.

    Total attempts = ``policy.transient_max_attempts + 1`` (one initial call plus
    N retries). Backoff before retry k (1-indexed) is
    ``base * 2^(k-1) + uniform(0, jitter)``. Non-``retryable`` exceptions propagate
    immediately (no retry, no swallowing — same contract as the prior inline loops).
    A ``policy`` with ``transient_max_attempts == 0`` means "never retry".
    """
    max_attempts = policy.transient_max_attempts + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except retryable as e:
            if attempt >= max_attempts:
                raise
            backoff = policy.transient_backoff_base_seconds * (2 ** (attempt - 1))
            if policy.transient_jitter_seconds > 0:
                backoff += random.uniform(0, policy.transient_jitter_seconds)
            if on_retry is not None:
                on_retry(e, attempt, backoff)
            await asyncio.sleep(backoff)
    # Unreachable: the loop either returns or raises on the final attempt.
    raise AssertionError("retry_async: exhausted loop without return or raise")


# gRPC status markers a Firestore session write raises on a transient failure.
TRANSIENT_STORE_MARKERS = ("RST_STREAM", "UNAVAILABLE", "INTERNAL")


async def retry_store_write(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str,
    max_attempts: int = 3,
) -> T:
    """Run one session-store write, retrying transient gRPC errors (linear 0.5 s backoff).

    Classification is by message marker (``TRANSIENT_STORE_MARKERS``), not by type: the
    Firestore client surfaces these as assorted exception classes. Every chat-turn session
    write goes through here — the normal turn pair, the 90 s mark pair and the late-answer
    pair — so they share one retry behaviour. Non-transient errors and the last failed
    attempt raise.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn()
            if attempt > 1:
                logger.info(f"✅ {label} succeeded after {attempt} attempts")
            return result
        except Exception as exc:
            if attempt < max_attempts and any(t in str(exc) for t in TRANSIENT_STORE_MARKERS):
                delay = 0.5 * attempt
                logger.warning(f"⚠️ {label} attempt {attempt} failed ({exc}), retrying in {delay}s…")
                await asyncio.sleep(delay)
            else:
                raise
    # Unreachable: the loop either returns or raises on the final attempt.
    raise AssertionError("retry_store_write: exhausted loop without return or raise")
