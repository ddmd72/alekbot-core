# Decision: Zombie sweep complete — F3.11 deprecation-without-deletion pattern closed

**Status:** Adopted — F3.11 closed cumulatively
**Date:** 2026-05-30
**Context:** Inspection findings F3.11 / F4.1 / F4.7 / F7.1 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

The deprecation-without-deletion meta-pattern (F3.11) is closed: every named zombie
in the cluster has been deleted rather than left disabled-but-present.

- **F4.1** (`68661d9`) — `LLMPort.generate_content` collapsed to `(self, request: LLMRequest)`.
  The legacy per-kwarg path was a backward-compat shim with no production caller; deleted
  across ABC + 4 adapters + `CachingLLMProxy`.
- **F4.7** (`a92a010`) — the user-facing streaming surface (`LLMRequest.stream`,
  `ProviderCapabilities.streaming`, `stream_callback`) had no consumer and was deleted.
  ClaudeAdapter's SDK-internal `messages.stream()` is unrelated and was kept — the Anthropic
  SDK requires it for >10min requests and the grounding pause_turn loop.
- **F7.1** (`9a03620`) — `_search_by_phrase` + sole helper `_limit_for_label` deleted; git
  archaeology confirmed pure fossil (dead since the 2026-02-07 `_search_by_vector_field` switch),
  no abandoned design intent.

Cumulative trail: F1.1 + F5.5 + F3.5 (prior) + F2.5 `5bc5d8c` + F3.1 (moot via F3.8) +
F6.1 `a2abee3` (last session) + F4.1 + F4.7 + F7.1 (this session).

## Why delete, not keep-declared

Each card's source acceptance hedged toward "implement/decide later." Verification showed no
live consumer and no recoverable design intent for any of the three — the breaking change on a
future need is the correctness signal, not a pre-installed seam. Keeping them declared is the
exact pattern F3.11 names.

## Rejected alternatives

- **Keep F4.7 streaming declared** (source acceptance) — rejected: no use case materialized;
  a future Telegram live-response feature can re-add a purpose-built surface.
- **Keep F4.1 dual path for external implementers** — rejected: single-tenant solo project,
  no out-of-tree `LLMPort` implementations.
