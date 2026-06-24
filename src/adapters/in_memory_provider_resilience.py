"""
InMemoryProviderResilience — process-local ``ProviderResiliencePort``.

Single-instance Cloud Run; replace with a Redis- or Firestore-backed
adapter when horizontal scaling lands (port unchanged).

State per provider:
- Bounded deque of failure timestamps (rolling window).
- Scalar ``opened_at`` set eagerly when the threshold-th failure lands.

No locks: all mutations are sync and atomic in single-threaded asyncio.
``time_source`` injected for deterministic tests; defaults to
``time.monotonic`` (correct for measuring durations across NTP jumps).
"""

from __future__ import annotations

from collections import deque
from time import monotonic
from typing import Callable, Deque, Dict, Optional

from ..ports.provider_resilience_port import ProviderResiliencePort


class InMemoryProviderResilience(ProviderResiliencePort):
    """Process-local rolling-window circuit breaker.

    Args:
        failure_threshold: Failures within ``window_seconds`` that trip
            the breaker. Default ``5``.
        window_seconds: Rolling-window length. Default ``60.0``.
        cooldown_seconds: How long the breaker stays OPEN before
            transitioning to HALF-OPEN on the next query. Default ``30.0``.
        time_source: Monotonic clock injection for tests; production uses
            ``time.monotonic``.
        on_open: Optional sync callback invoked with the provider name exactly
            once per CLOSED→OPEN transition. The escalation hook: failover keeps
            the user served on transient faults, while a tripped breaker means
            the failures are chronic and warrant a human signal. Must NOT raise
            (it runs inside the hot-path failure handler) and must NOT block —
            schedule any async delivery and return. Default ``None`` (no-op).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 30.0,
        time_source: Optional[Callable[[], float]] = None,
        on_open: Optional[Callable[[str], None]] = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")

        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._now = time_source if time_source is not None else monotonic
        self._on_open = on_open

        self._failures: Dict[str, Deque[float]] = {}
        self._opened_at: Dict[str, float] = {}

    def record_failure(self, provider_name: str) -> None:
        now = self._now()
        window = self._failures.setdefault(provider_name, deque())
        window.append(now)
        self._evict(window, now)
        if (
            provider_name not in self._opened_at
            and len(window) >= self._failure_threshold
        ):
            self._opened_at[provider_name] = now
            # Fire AFTER the OPEN marker is set so the breaker state is already
            # consistent if the callback re-enters. Once per transition: the
            # marker guard above ensures further failures while OPEN do not
            # re-fire; a re-open after HALF-OPEN fires again (a sustained outage
            # should keep nagging — the sink throttles).
            if self._on_open is not None:
                self._on_open(provider_name)

    def record_success(self, provider_name: str) -> None:
        self._failures.pop(provider_name, None)
        self._opened_at.pop(provider_name, None)

    def is_provider_open(self, provider_name: str) -> bool:
        opened_at = self._opened_at.get(provider_name)
        if opened_at is None:
            return False
        now = self._now()
        if now - opened_at >= self._cooldown_seconds:
            # Cooldown elapsed → HALF-OPEN. Clear OPEN marker and the
            # failure window so a still-down provider must accumulate a
            # fresh threshold of failures to re-open. A success closes
            # cleanly via record_success.
            self._opened_at.pop(provider_name, None)
            self._failures.pop(provider_name, None)
            return False
        return True

    def _evict(self, window: Deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while window and window[0] < cutoff:
            window.popleft()
