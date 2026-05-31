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
