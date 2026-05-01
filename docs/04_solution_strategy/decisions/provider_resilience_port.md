# Decision: Provider Resilience Port

**Status:** Adopted (2026-05-01).

## Decision

Provider-level circuit-breaker is its own port (`src/ports/provider_resilience_port.py`) with three sync methods: `record_failure`, `record_success`, `is_provider_open`. State is per-process, shared across all agents. Failover-trigger error types are a domain constant (`FAILOVER_TRIGGER_TYPES` in `src/domain/exceptions.py`) — not a port method, since the decision is stateless `isinstance`.

## Rejected alternatives

- **One port carrying retry policy + breaker** (original F4.5 sketch): forces `RetryPolicy` as method input on every call. Different lifetimes (per-agent vs per-process) → false coupling.
- **Reuse `src/utils/circuit_breaker.py`**: that one is per-agent for crash isolation (wrapper-style `call()`); semantics distinct from per-provider health tracking. Both stay.
- **`should_failover(error, attempt)` on the port**: stateless data dressed up as I/O. Moved to `FAILOVER_TRIGGER_TYPES` domain const.
- **`error` parameter on `record_failure`**: unused in the implementation; YAGNI. Re-add later iff weighted counting becomes necessary (one-line breaking change for callers).
- **`threading.Lock` in the in-memory adapter**: single-threaded asyncio; mutations are sync and atomic. Lock would be pure overhead.

## Triggers to revise

- Multi-instance deployment → swap `InMemoryProviderResilience` for a Redis- or Firestore-backed adapter (port unchanged).
- Per-error-type weighting required → restore an `error` parameter.

## See also

- [`provider_resilience_phase2_wireup.md`](provider_resilience_phase2_wireup.md) — Phase 2 caller wire-up (BaseAgent + adapter translations + AgentExecutionContext field).
- [`agent_execution_context_resilience_field.md`](agent_execution_context_resilience_field.md) — required-field choice + cross-port-import workaround.
- [`agent_execution_context_eq_excludes_resilience.md`](agent_execution_context_eq_excludes_resilience.md) — custom `__eq__` excluding process-local infra.
- [`both_providers_unavailable_terminal_type.md`](both_providers_unavailable_terminal_type.md) — terminal exception + breaking change to no-fallback path.
