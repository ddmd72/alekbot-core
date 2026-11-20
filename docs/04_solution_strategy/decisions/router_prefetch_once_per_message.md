# Decision: memory prefetch happens once per message, in the Router (formerly ADR-005)

**Status:** Accepted (2026-01-27) — the principle holds; the mechanics have moved on.
**Mechanics today:** `docs/05_building_blocks/memory/README.md` §4.4.

## Context

Each orchestrator (Quick, Smart) used to run its own memory search for the same message. The same Firestore queries ran
more than once per turn, the agents searched differently, and facts already in the biographical baseline were injected
a second time.

## Decision

Memory is prefetched **once per message, in the Router**, from the search inputs its triage produces, minus the facts
already in the biographical baseline. The result travels on the routed `AgentMessage` and the orchestrator renders it as
`query_specific_context`. Agents never run the prefetch search themselves.

## Rejected alternatives

- **Agent-owned search** (the original design): duplicate queries per turn and inconsistent context.
- **Search only on demand** (`search_memory` alone): every personal question would cost a tool round-trip before the
  first useful answer.

## Consequences

- One prefetch per turn shared by the whole orchestrator turn. Smart's own `search_memory` calls are extra and
  deliberate: they search all channels and do not exclude the baseline.
- The prefetch quality is bounded by what triage extracts from the message.
- `search_memory`, the claude.ai connector and the companion context share the search path, so all rank the same way.

## Revisit when

An orchestrator needs its own search strategy, or the prefetch cost or latency shows up on turns that never use it.
