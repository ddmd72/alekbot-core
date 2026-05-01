"""
RetryPolicy — typed retry behavior for agent execution.

Replaces the legacy ``AgentConfig.max_retries`` integer that retried EVERY
exception type identically. The legacy behavior produced two anti-patterns:

  1. ``asyncio.TimeoutError`` was retried — but a timeout means the wall-
     clock budget was exhausted; running the same call again inside the
     same budget cannot succeed and only doubles wall time + cost.
  2. Programming errors (TypeError, KeyError, etc.) were retried — but
     they are deterministic; retry only delays the failure and obscures
     the bug from logs.

This policy retries ONLY the two known-transient LLM error types
(``LLMRateLimitError`` and ``LLMUnavailableError``) and uses exponential
backoff with jitter to avoid thundering-herd retry storms when many
agents hit the same upstream rate-limit window.

Fixed (non-configurable) policy applied by ``BaseAgent.process``:

  - ``asyncio.TimeoutError``     → never retried; structural budget mismatch.
  - ``asyncio.CancelledError``   → never retried; honour external cancel.
  - Any other ``Exception``       → never retried; deterministic by assumption.

See: docs/04_solution_strategy/decisions/typed_retry_policy.md
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Per-agent retry behavior for transient LLM provider errors.

    Attributes:
        transient_max_attempts: Number of RETRY attempts after the initial
            try, applied only on ``LLMRateLimitError`` /
            ``LLMUnavailableError``. ``0`` disables retries entirely
            (initial attempt only). Default ``3`` covers the common case
            of a 1–3 minute provider blip.
        transient_backoff_base_seconds: Base for exponential backoff.
            Wait before retry N is ``base * 2**(N-1)``. Default ``2.0``
            gives 2s / 4s / 8s for the first three retries — within
            standard provider rate-limit reset windows.
        transient_jitter_seconds: Maximum random jitter added to each
            backoff. Defends against synchronised retry storms when many
            agents (or many users on the same instance) hit the same
            rate-limit window simultaneously. Set to ``0.0`` to disable
            (deterministic backoff for tests). Default ``1.0``.

    Total worst-case retry overhead with defaults:
      attempts=3, base=2.0, jitter=1.0
      → 2 + 4 + 8 + (up to 3 × jitter) ≤ 17s
    Comfortably within every NotificationKind SLA budget after the
    notification refactor.
    """
    transient_max_attempts: int = 3
    transient_backoff_base_seconds: float = 2.0
    transient_jitter_seconds: float = 1.0


# Pre-baked policies for the common cases. Agents pick one as a class-level
# constant or pass a custom RetryPolicy(...) at construction time.

DEFAULT_RETRY_POLICY = RetryPolicy()
"""Standard 3-retry policy used by SmartAgent, QuickAgent, all specialists."""

NO_RETRY_POLICY = RetryPolicy(transient_max_attempts=0)
"""Use for agents that must not retry: kick-off paths (DeepResearch),
async long-running tasks (DocPlanner / PdfGenerator / HtmlPageGenerator —
retry there would re-do the entire generation), Router (must be fast)."""
