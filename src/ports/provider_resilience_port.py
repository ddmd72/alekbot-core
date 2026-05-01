"""
ProviderResiliencePort — provider-level circuit-breaker contract.

Tracks failure history per provider name and exposes one query
(:meth:`is_provider_open`) the hot path consults before issuing an LLM
request. Scope is **per-process, shared across all agents**, distinct
from per-agent ``RetryPolicy`` (transient retry budget) and per-agent
``CircuitBreaker`` (agent crash isolation, ``src/utils/circuit_breaker.py``).

Stateless data lives outside this port:
- Which error types trigger a fallback switch is encoded as a domain
  constant — :data:`src.domain.exceptions.FAILOVER_TRIGGER_TYPES` — read
  directly by callers via ``isinstance``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderResiliencePort(ABC):
    """Provider-level circuit breaker.

    All methods are sync and atomic in single-threaded asyncio; state
    mutations between awaits cannot interleave at the port level.
    """

    @abstractmethod
    def record_failure(self, provider_name: str) -> None:
        """Register a failure for ``provider_name`` at the current time.

        The threshold-th failure within the implementation's rolling
        window opens the breaker eagerly — no separate trigger call.
        """

    @abstractmethod
    def record_success(self, provider_name: str) -> None:
        """Register a successful response for ``provider_name``.

        Strong health signal: clears any failure window AND any OPEN
        marker so the caller proceeds normally on the next request.
        """

    @abstractmethod
    def is_provider_open(self, provider_name: str) -> bool:
        """Return True iff the breaker for ``provider_name`` is OPEN.

        Three logical states collapsed to one boolean:

        - **CLOSED** / **HALF-OPEN** → ``False`` (caller proceeds; in
          HALF-OPEN exactly one probe is permitted, with multi-probe
          races acceptable per the contract — concurrency bounded by
          ``RetryPolicy`` at the agent layer).
        - **OPEN** → ``True`` (caller skips this provider; uses fallback
          or fails fast).
        """
