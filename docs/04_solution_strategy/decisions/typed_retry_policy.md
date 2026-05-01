# Decision: Typed Retry Policy in BaseAgent

**Status:** Adopted
**Date:** 2026-05-01
**Context:** Follow-up to NOTIFICATION_DELIVERY_REFACTOR_RFC. The legacy
``AgentConfig.max_retries: int`` was a single number that retried
**every** exception identically — including ``asyncio.TimeoutError``
(structural budget mismatch — retrying inside the same budget cannot
succeed) and programming errors (``ValueError`` / ``KeyError`` /
``TypeError`` — deterministic; retry only delays the failure and
obscures the bug from logs).

## Decision

Replace ``AgentConfig.max_retries`` with a typed ``RetryPolicy`` value
object owned by ``BaseAgent``.

```python
@dataclass(frozen=True)
class RetryPolicy:
    transient_max_attempts: int = 3
    transient_backoff_base_seconds: float = 2.0
    transient_jitter_seconds: float = 1.0
```

Retry behavior is determined by the **type** of error caught in
``BaseAgent.process``:

| Exception                                        | Action                                      |
|--------------------------------------------------|---------------------------------------------|
| ``LLMRateLimitError`` (HTTP 429)                  | retry up to ``transient_max_attempts``       |
| ``LLMUnavailableError`` (HTTP 5xx)                | retry up to ``transient_max_attempts``       |
| ``asyncio.TimeoutError``                          | **never retried** — structural failure       |
| ``asyncio.CancelledError``                        | **never swallowed**, propagated outward      |
| Any other ``Exception``                           | **never retried** — deterministic by assumption |

Backoff: exponential ``base * 2^(attempt-1)`` plus uniform jitter
``[0, jitter)``. Jitter is mandatory for the rate-limit case to defend
against synchronised retry storms — when many agents (or many users on
a single instance) hit the same provider rate-limit window
simultaneously, deterministic backoff guarantees they all retry at the
same instant and trip the same window again.

## Per-agent overrides

Agents declare a class-level ``RETRY_POLICY``; ``BaseAgent``'s default
is ``DEFAULT_RETRY_POLICY``. The constructor also accepts
``retry_policy=`` for instance-level override (used by tests; production
agents should use the class attribute).

| Agent                                 | Policy            | Reason                                                                                  |
|---------------------------------------|-------------------|-----------------------------------------------------------------------------------------|
| ``BaseAgent`` (default)               | ``DEFAULT``        | All standard agents (Smart, Quick, specialists) retry transients up to 3 times.         |
| ``RouterAgent``                       | ``NO_RETRY``       | Router triage must stay fast; retry would defeat the point of having Router at all.     |
| ``DocPlannerAgent``                   | ``NO_RETRY``       | ASYNC document creation; retry re-does the entire generation. Cloud Tasks queue retry covers transients at the right granularity. |
| ``DocGeneratorAgent``                 | ``NO_RETRY``       | Same.                                                                                   |
| ``PdfGeneratorAgent``                 | ``NO_RETRY``       | Same.                                                                                   |
| ``HtmlPageGeneratorAgent``            | ``NO_RETRY``       | Same.                                                                                   |
| ``ClaudeDeepResearchRunnerAgent``     | ``NO_RETRY``       | 10–25 minutes of LLM work in a Cloud Run Job; retry doubles cost.                       |

## What got removed (one-shot migration, no deprecation phase)

- ``AgentConfig.max_retries: int`` — gone. Field deletion is the
  correctness signal: any caller still passing it gets ``TypeError`` at
  construction, not silent legacy behaviour.
- ``QuickAgentConfig.config_max_retries`` — gone.
- ``SmartAgentConfig.config_max_retries`` — gone.
- ``DeepResearchAgentConfig.max_retries`` — gone.
- ``ClaudeDeepResearchRunnerConfig.max_retries`` — gone.
- ``QuickResponseAgent.CONFIG_MAX_RETRIES`` class constant — gone.
- ``SmartResponseAgent.CONFIG_MAX_RETRIES`` class constant — gone.
- ``DeepResearchAgent.MAX_RETRIES`` class constant (unused) — gone.

All call sites in ``src/composition/user_agent_factory.py``,
``src/agents/core/router_agent.py``, ``src/agents/core/quick_response_agent.py``,
``src/agents/core/smart_response_agent.py`` had their ``max_retries=``
arguments deleted.

Test suite: existing tests that pinned the legacy retry-count behavior
were rewritten to pin the new typed-retry contract (``no_retry_on_generic``,
``no_retry_on_timeout``, ``retries_on_transient_rate_limit``,
``retries_on_transient_unavailable``, ``no_retry_when_policy_disables``,
``cancelled_error_propagates_without_retry``). Authorised by user as
intentional behavior change (CLAUDE.md test rule).

## Worst-case retry overhead with defaults

``transient_max_attempts=3, base=2.0, jitter=1.0``:

  backoff sum = 2 + 4 + 8 = 14 s
  jitter sum  ≤ 3 × 1.0 = 3 s
  total       ≤ 17 s

Comfortably below every NotificationKind SLA budget after the
notification refactor — the smallest budget is REMINDER ECO at 180 s.

## Mechanical enforcement

- ``tests/unit/domain/test_retry_policy.py`` (13 tests) — value object
  invariants, ``DEFAULT_RETRY_POLICY`` and ``NO_RETRY_POLICY`` pinned.
- ``tests/unit/agents/test_per_agent_retry_policy.py`` (7 tests) —
  regression guard: each per-agent class-level override is asserted by
  identity. Flipping any of these back silently is impossible without
  also editing this file.
- ``tests/unit/test_base_agent.py`` —
  ``TestBaseAgent::test_no_retry_on_generic_exception``,
  ``test_no_retry_on_timeout``,
  ``test_retries_on_transient_rate_limit``,
  ``test_retries_on_transient_unavailable``,
  ``test_no_retry_when_policy_disables``,
  ``test_cancelled_error_propagates_without_retry``.
- Coverage gate (``make test-coverage``) keeps ``retry_policy.py`` at
  100% (added to ``THRESHOLDS``).
