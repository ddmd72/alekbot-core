# Decision: `BothProvidersUnavailableError` as the single terminal type

**Status:** Adopted (2026-05-01).

## Decision

Two failure-exhaustion code paths in `BaseAgent._call_llm` collapse to one exception type:

- Primary failed (or short-circuited) AND no `fallback_provider` configured.
- Primary failed AND fallback `is_provider_open(fallback_name)` true.
- Primary failed AND fallback also raised a `FAILOVER_TRIGGER_TYPES` error.

All three raise `BothProvidersUnavailableError(primary_name, fallback_name, primary_cause)`. NOT in `FAILOVER_TRIGGER_TYPES` (terminal — fallback exhausted, no further routing possible). `primary_cause` carries the original exception for forensics.

## Breaking change to no-fallback path

Pre-Phase-2 behaviour was bare `raise` of the original exception when `ctx.fallback_provider is None`. Two existing tests (`test_no_fallback_configured_re_raises`, `test_no_context_set_re_raises`, plus `test_base_agent.py::test_rate_limit_error_reraises_without_fallback`) asserted on the original type — updated to assert on `BothProvidersUnavailableError` with `primary_cause` matching.

**Downstream caller impact**: any production code that catches `LLMRateLimitError` / `LLMUnavailableError` *outside* `BaseAgent._call_llm` will silently miss those errors when they propagate from a no-fallback context. If retry / surfacing logic exists at the orchestrator boundary, it must be updated to also catch `BothProvidersUnavailableError` (and inspect `.primary_cause` if the cause type matters).

## Rejected alternatives

- **Re-raise original exception when no fallback** (preserve pre-Phase-2 behaviour): caller surface becomes asymmetric — `LLMRateLimitError` for no-fallback path vs new `BothProvidersUnavailableError` for open-fallback path. Same exhaustion outcome, different exception type → caller must branch on whether fallback was configured. Awkward.
- **Synthesize `LLMUnavailableError(http_status=None)` for breaker-open**: conflates "real upstream 503" with "we short-circuited" and "we exhausted failover". Three distinct semantics → three distinct types is cleaner than one type with magic `http_status` sentinels.
- **`BothProvidersUnavailableError` in `FAILOVER_TRIGGER_TYPES`**: would create infinite loop — terminal exception triggering further failover routing.

## Triggers to revise

- If a third provider tier ever lands (primary → secondary → tertiary): rename to `AllProvidersUnavailableError` and carry a list of `(name, cause)` tuples. Single-name `primary_cause` field becomes insufficient.
