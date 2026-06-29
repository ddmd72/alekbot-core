# Decision: Transcript-integrity invariant — one delegation transcript = one provider

**Status:** Adopted (2026-06-29). Resolves TD-2.

## Context

`BaseAgent._call_llm` failed over **per LLM call**: on a `FAILOVER_TRIGGER` error it re-served the
*same turn* on the fallback provider, then the delegation loop continued with the now-mixed
transcript. A multi-turn transcript is **provider-specific** — `tool_use` ids, thinking blocks,
`raw_content`, and cache all differ per provider — so interleaving providers within one transcript
corrupts it.

Confirmed root cause of the 2026-06-29 `tool_use_id` orphan: a **transient mid-stream 529**
(overloaded) arrives as an SSE error *after* HTTP 200, so the Anthropic SDK's built-in 429/5xx retry
never catches it. `ClaudeAdapter` correctly classifies it as `LLMServerError` (in
`FAILOVER_TRIGGER_TYPES`, not in `TRANSIENT_RETRY_TYPES`). On turn 2+, `_call_llm` immediately
cross-provider-failed-over to Gemini; Gemini's tool turn entered the transcript with no Claude
`thought_signature`; the next turn returned to Claude (`eff.ctx.provider`), whose `_convert_messages`
minted a synthetic `tool_use` id → orphan → HTTP 400 → degraded Smart→Quick delivery. `tool_use_id`
is the first symptom; thinking-replay and cache break the same way.

**Root insight:** the right response to a *transient* error mid-loop is **retry the same provider**,
not switch. Cross-provider failover is for a *persistently* down provider, and there it must never
produce a mixed transcript.

## Decision

Enforce **one delegation transcript = one provider**, centralized in `BaseAgent._call_llm` (the single
LLM call site, where failover + breaker policy already live). Inside the existing
`except failover_tuple` branch, before the cross-provider dispatch:

- Compute `transcript_locked` from `request.messages`:
  `any(part.tool_call or part.tool_response …)` **OR** `any(msg.raw_content is not None …)`.
  Tool-part presence catches the orphan-`tool_use_id` bug; `raw_content` presence independently catches
  thinking-replay (a model turn can carry `raw_content` without a tool part). `raw_content` is an SDK
  object never persisted to Firestore session history — set in-loop by `build_tool_turn` — so turn-1 /
  single-call requests are unlocked and behave exactly as before.
- **If locked, never cross-provider-failover.** Retry the **same** provider up to
  `_SAME_PROVIDER_RETRY_ATTEMPTS` (= 2) times with backoff (from `self.retry_policy`) for transient
  errors; `record_failure(primary)` per failed attempt. On success, fall through to the shared success
  tail (billing / span / content-store). On exhaustion — or on `ProviderBreakerOpenError` (breaker
  open, retry is pointless) — log `event="llm_transcript_locked"` and raise the terminal
  `TranscriptLockedError`.
- **If not locked, keep the existing per-call cross-provider failover unchanged.** Single-call agents
  (specialists, Quick formatter, Router) have no transcript to corrupt.

`TranscriptLockedError` is a new `LLMError` subtype, terminal by design — NOT in
`FAILOVER_TRIGGER_TYPES` or `TRANSIENT_RETRY_TYPES`. It propagates up, is absorbed by SmartAgent's
blanket `except Exception` into `AgentResponse.failure`, and reaches the existing
`AgentFallbackService.try_quick_fallback` (`conversation_handler.py`) — Smart→Quick with a **clean**
transcript. No new catch sites, no adapter or delegation-engine changes.

## Rejected alternatives

- **The roadmap's 3-file split** (same-provider retry in `claude_adapter`, gate in `base_agent`,
  boundary in `delegation_engine`): the lock signal already lives in `request.messages` inside
  `_call_llm`. Splitting fragments one resilience policy across three layers, gives the adapter a second
  responsibility (retry policy, not just translation+classification), duplicates retry across four
  adapters, and keeps the retry Claude-specific. Centralizing is hexagonally cleaner *and* smaller, and
  changes zero existing tests.
- **Reuse `BothProvidersUnavailableError` for the locked case:** semantically wrong — there the
  fallback is healthy and we *choose* not to use it. It would falsely report a fallback outage to the
  breaker dashboards / `llm_both_open` alerting and lie in post-mortems. See
  [`both_providers_unavailable_terminal_type.md`](both_providers_unavailable_terminal_type.md).
- **Restart the whole Smart request on the fallback** (clean transcript, but): re-runs already-done
  delegations (cost/latency) and risks double-firing side-effecting delegates (`create_pdf`,
  `save_to_memory`). Also structurally unnecessary here — SmartAgent's `except Exception` already
  prevents a full-agent `retry_async` restart.
- **`raw_content`-only lock signal:** misses the tool-part case on histories where `raw_content` was
  dropped on the tiered-rewrite path; tool-part-only misses thinking-replay. The OR is belt-and-
  suspenders; its only cost is occasionally declining a healthy failover and degrading to Quick — the
  safe direction.

## Triggers to revise

- A non-delegation multi-turn path that grows text-only transcripts with provider-specific state but no
  tool parts and no `raw_content` → broaden the lock signal.
- Per-provider same-provider-retry budgets needed → make `_SAME_PROVIDER_RETRY_ATTEMPTS` provider-aware.

## See also

- [`provider_resilience_port.md`](provider_resilience_port.md) — the breaker + `FAILOVER_TRIGGER_TYPES`.
- [`both_providers_unavailable_terminal_type.md`](both_providers_unavailable_terminal_type.md) — the
  sibling terminal exception this one is deliberately kept distinct from.
- [`typed_retry_policy.md`](typed_retry_policy.md) — `TRANSIENT_RETRY_TYPES` / `RetryPolicy`.
- Implementation: `src/agents/base_agent.py` (`_call_llm`), `src/domain/exceptions.py`
  (`TranscriptLockedError`). Tests: `tests/unit/agents/core/test_base_agent_fallback.py`.
