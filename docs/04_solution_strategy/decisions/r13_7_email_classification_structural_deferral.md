# Decision: R13.7 — EmailClassificationAgent kept as agent-shaped service

**Status:** Adopted — R13.7 closed
**Date:** 2026-05-29
**Context:** Inspection finding R13.7 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

R13.7 named three classes for migration from "agent-shaped service" to
chain-participating agent posture. Verification against current source
(2026-05-29) shows three of the four named/implied classes have already
been migrated:

- **ConsolidationAgent** — real `can_handle` (intent router), registered in
  `agent_manifest.py`, called via `coordinator.route_message`.
- **MemorySearchAgent (FactsMemoryAgent)** — real `can_handle`, registered,
  participates in chain via `Intent.SEARCH_MEMORY`.
- **EmailSearchAgent** — real `can_handle`, registered, participates via
  `Intent.SEARCH_EMAILS` / `get_email_details` / `get_email_attachment`.

The fourth class, **EmailClassificationAgent**, is intentionally kept as an
agent-shaped service. It inherits `BaseAgent` for infrastructure conveniences
(prompt-builder integration, `_call_llm` instrumentation, provider resilience)
but is called directly by `EmailIndexingService.classify_batch()` and not
registered for chain participation.

## Why we don't migrate EmailClassificationAgent

The other three classes participate in conversational request routing: an
orchestrator LLM (Smart/Quick) decides at runtime to delegate to them.
EmailClassification is dispatched by a batch indexing pipeline that
classifies every email mechanically — there is no LLM-driven dispatch decision
to model in the chain.

Migrating to chain-participating would force an awkward conceptual mapping:
deterministic batch dispatch wrapped as orchestrator-routed delegation,
purely to satisfy the architectural uniformity of "everything is a chain
participant". The conceptual mismatch is real — the indexing service IS the
dispatcher; routing it through `coordinator.delegate` would add an empty
wrapper that the runtime traverses with no decision to make.

## Infrastructure benefits NOT lost

The audit's stated benefits ("uniform billing / circuit-breaking / debug-logging
applied to all three") are largely already in place for EmailClassification
because `BaseAgent._call_llm` is the single entry point for LLM calls and it
applies:

- **Provider resilience** (F4.5 wire-up) — `_call_llm` consults
  `ExecutionContext.resilience_port` on every call.
- **Debug logging** — `_call_llm` calls `PromptDebugLogger` before and after.
- **Billing / token tracking** — `UsageMetadata` flows through `LLMResponse`.

The only piece NOT applied is **agent-level circuit-breaker**, because
`BaseAgent.process_message` is the layer that consults `self.circuit_breaker`,
and `classify_batch` bypasses `process_message`. Agent-level CB is not
load-bearing here: failed classification batches are retried by Cloud Tasks
(outer retry layer), and per-call provider failures are already covered by
provider resilience.

## Rejected alternatives

- **Migrate EmailClassificationAgent to chain participant** (audit's
  recommendation). Forces an empty `delegate → execute` round-trip on every
  batch, models a non-existent routing decision, and complicates batch
  performance reasoning. No correctness benefit (`_call_llm` already
  instrumented).
- **Re-house as `EmailClassificationService` (drop BaseAgent inheritance,
  use LLMPort directly with composed billing/debug-logging decorators).**
  Architecturally honest but requires reimplementing the `_call_llm`
  contract (resilience, debug, token tracking) via composition. High cost,
  low marginal value — the BaseAgent inheritance is a thin convenience.
- **Status-quo with no decision record.** Forbidden — `feedback_clean_or_explain.md`
  disallows the third lane. Explicit deferral with rationale is required.

## Trigger to revisit

- EmailClassification develops a need for runtime LLM-driven dispatch (e.g.
  conversational user override of indexing decisions). Then it becomes a
  real chain participant.
- A new agent-shaped service is added with structurally identical dispatch
  (batch loop, deterministic). At that point the recurring pattern justifies
  formalizing as a dedicated `BatchAgent` or service-with-LLM-port abstraction.
- Cloud Tasks retry pattern changes such that batch retries depend on
  agent-level circuit-breaker signal.

## Verified state — 2026-05-29

| Class | can_handle | In agent_manifest | Call-site | R13.7 status |
|---|---|---|---|---|
| ConsolidationAgent | real intent router (lines 170-177) | yes (`Intent.CONSOLIDATE` etc) | `coordinator.route_message` via ConsolidationService | ✅ migrated |
| MemorySearchAgent | real intent router | yes (`Intent.SEARCH_MEMORY`) | chain participant | ✅ migrated |
| EmailSearchAgent | real intent router (lines 81-88) | yes (`Intent.SEARCH_EMAILS`) | chain participant | ✅ migrated |
| EmailClassificationAgent | `return False` (line 78) | not registered | `EmailIndexingService.classify_batch()` | 🟡 deferred (this record) |
