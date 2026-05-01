# Decision: Provider Resilience — Phase 2 caller wire-up

**Status:** Adopted (2026-05-01). Builds on [`provider_resilience_port.md`](provider_resilience_port.md).

## Decision

`BaseAgent._call_llm` consumes the resilience port end-to-end:

1. Pre-call: if `is_provider_open(primary)` → raise `ProviderBreakerOpenError` (in `FAILOVER_TRIGGER_TYPES`, routes through fallback path).
2. On primary success: `record_success(primary)`.
3. On `FAILOVER_TRIGGER_TYPES`: `record_failure(primary)` (skipped for `ProviderBreakerOpenError` — already past consequence).
4. Pre-fallback: if no fallback OR `is_provider_open(fallback)` → raise `BothProvidersUnavailableError(primary_cause=e)`.
5. On fallback success: **NO** `record_success(fallback)` — see asymmetry below.
6. On fallback `FAILOVER_TRIGGER_TYPES`: `record_failure(fallback)` then raise `BothProvidersUnavailableError(primary_cause=e)`.

All four LLM adapters (gemini/claude/openai/grok) translate **both** `asyncio.TimeoutError` (our `wait_for` wrap, set when `request.timeout` is provided) and the SDK-specific timeout class (default httpx ceiling when `request.timeout` is None) to `LLMTimeoutError`. Connection errors → `LLMNetworkError`. Non-503 5xx → `LLMServerError`.

## Asymmetric `record_success` policy

`record_success` is called for primary only, never for fallback. Phase 1 `record_success` is full-reset (`pop` failures + `pop` opened_at — see `in_memory_provider_resilience.py:72-74`). Calling on fallback would erase accumulated failures after one lucky call → flaky fallback (3 fail / 1 success / 3 fail) would never open.

## Rejected alternatives

- **Catch only `asyncio.TimeoutError`** (no SDK-timeout class): default-timeout requests (`request.timeout=None`, the common case) would raise raw SDK timeout — bubbled past `FAILOVER_TRIGGER_TYPES`, silently bypassing the breaker. Slow-failure mode taking hours to diagnose.
- **Single port method `track_call(name, success: bool)`**: collapses the decision to record_failure-or-success into a boolean, making the asymmetric fallback policy unexpressible.
- **Inline 429/503 catch in BaseAgent** (current pre-Phase-2 shape): adapters already translate to typed exceptions; HTTP-code branching at the agent layer was redundant and prevented adding new failure categories without touching every catch site.

## Triggers to revise

- Flaky-fallback masking incident (fallback breaker never opens because intermittent successes keep resetting it) → introduce `decay_failure(name)` semantics on `record_success` and call it on fallback success.
- Remote-state adapter latency observed → extend port with `get_state(*provider_names) -> dict[str, bool]` snapshot to collapse the two `is_provider_open` round-trips per `_call_llm` (primary + pre-fallback) into one.
