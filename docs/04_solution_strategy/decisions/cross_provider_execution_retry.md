# Decision: Cross-provider execution retry (provider rotation) for Smart

**Status:** Adopted (2026-07-12).

## Context

A transient Anthropic 529 (`overloaded_error`) arriving mid-stream on a **locked** Claude delegation
transcript (turn 2+) exhausts the L1 same-provider retry and raises the terminal `TranscriptLockedError`
(see [`transcript_integrity_one_provider.md`](transcript_integrity_one_provider.md)). Today that drops
straight to the Quick fallback — losing Smart's tier and its delegation/re-evaluation. Two incidents
(2026-07-09, 2026-07-12); the 2026-07-12 save was luck (Quick happened to route to a non-Claude model).

Existing retry layers, none of which recover this case:

- **L1** `BaseAgent._call_llm`: same-provider retry (locked) / single-call cross-provider failover (non-locked).
- **L2** `BaseAgent.process` → `retry_async`: retries the whole `execute()` but only on `TRANSIENT_RETRY_TYPES`,
  **same provider** from config. Neutered here — Smart's blanket `except Exception` swallows the lock, it is
  terminal (not in `TRANSIENT_RETRY_TYPES`), and `retry_async` cannot switch provider.
- **L3** `AgentFallbackService.try_quick_fallback`: give up on Smart → Quick → apology.

## Decision

Add a new rung as an **L2 retry with provider rotation, scoped to Smart** — a retry, not a degradation.

- `SmartResponseAgent._run` re-raises `TranscriptLockedError` instead of swallowing it into a failure.
- `SmartResponseAgent.execute` wraps `_run` in a rotation loop: on `TranscriptLockedError`, ask
  `TaskExecutionResolver.next_provider_override` for a fresh execution context on the next allowed provider
  at the **same tier**, then re-run from scratch. Exhausting the provider list returns the identical failure
  as before → the existing Quick fallback (L3) is untouched.
- Provider selection lives in `AgentContextBuilder.resolve_next_provider`: walk
  `AgentProviderStrategy.STRATEGIES["smart"]["allowed_providers"]` in declared order, skip already-tried and
  breaker-open providers, rebuild via `_build` (same tier; each provider resolves its own `get_model_for_tier`).

**Zero domain change** (the typed exception stays live inside Smart — caught before the blanket `except`),
zero `conversation_handler` change, zero `AgentFallbackService` change. Provider knowledge stays in
services/infrastructure; the agent holds no breaker logic or strategy import.

Idempotency: a from-scratch restart re-runs turns 0–1 (potentially re-firing side-effecting delegates) — but
this is **not a new risk**: the Quick fallback already re-runs the prompt from the original `message` with a
clean history. Rotation is bounded by the allowed-provider list (~3 extra attempts), no equivalence map needed.

## Rejected alternatives

- **Hang it on `AgentFallbackService` (a fallback rung).** Semantically wrong: this is a retry (re-run Smart on
  another provider), not a give-up-and-degrade. `AgentFallbackService` is the L3 branch; the failed
  `AgentResponse` there is a flattened string with no provider/tier/error-type, so it would need a domain-level
  failure-cause signal + string-sniffing avoidance. Keeping the retry inside Smart, where the typed exception
  and `eff.ctx` (provider/tier) are live, is cleaner and needs no domain change.
- **Put rotation in `BaseAgent.process` / `_call_llm`.** `_call_llm` is one call — it cannot restart the loop.
  `process` is generic; most agents have a single-provider `allowed_providers`, so rotation there is dead
  machinery and pulls `AgentContextBuilder` into the generic base.
- **This does not contradict `transcript_integrity_one_provider.md`.** That record rejected "restart the whole
  Smart request on the fallback" **as the integrity fix** (it would re-mix a locked transcript). Here the
  transcript integrity is preserved — we do NOT switch mid-transcript; we discard the locked transcript and
  rebuild a clean one on the next provider. Restart is adopted as a *degradation-ladder retry*, a different concern.

## Triggers to revise

- Want rotation on the non-locked terminal (`BothProvidersUnavailableError`) or to generalize L1's single-call
  failover to walk `allowed_providers` → that is the deferred "variant 3" (independent, wider blast radius).
- A cross-provider model pin (`config.get_model_override`) misbehaving on rotation → special-case model
  resolution in `resolve_next_provider` (currently inherits `_build`'s override semantics).
- `execute` timeout budget too tight for a from-scratch restart → make the timeout per-attempt.

## See also

- [`transcript_integrity_one_provider.md`](transcript_integrity_one_provider.md) — the `TranscriptLockedError` this consumes.
- [`per_call_execution_context.md`](per_call_execution_context.md) — `_EffectiveExecution` / no-mutation invariant the loop preserves.
- Implementation: `src/agents/core/smart_response_agent.py` (`execute`, `_run`),
  `src/infrastructure/task_execution_resolver.py` (`next_provider_override`),
  `src/services/agent_context_builder.py` (`resolve_next_provider`).
