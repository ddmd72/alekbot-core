# ADR-005: Router-Centric Enrichment Pattern

**Status:** Accepted (2026-01-27) — still in force.

## Context

Each orchestrator (Quick, Smart) used to run its own memory search for the same message: the same Firestore queries
ran more than once per turn, the agents searched differently, and facts already in the biographical baseline were
injected a second time.

## Decision

Memory prefetch happens **once per message, in the Router**. The Router's triage returns the search inputs
(`search_intent`, keywords, a search phrase, `relevant_domains`). Unless `search_intent` is `none`,
`RouterAgent` calls `SearchEnrichmentService.enrich_context` with the user's biographical baseline, so facts already
in the baseline are left out. The result travels on the routed `AgentMessage` as `context["enriched_context"]`, and the
orchestrator renders it as `query_specific_context`. Agents never query the fact store for the prefetch themselves.

Mechanics (channels, limits, RRF, deduplication): `docs/05_building_blocks/memory/README.md` §4.3–§4.4.

## Rejected alternatives

- **Agent-owned search** — the original design; duplicate queries per turn and inconsistent context.
- **Search only on demand** (`search_memory` alone) — every personal question would cost a tool round-trip before
  the first useful answer.

## Consequences

- One prefetch per turn, shared by the whole orchestrator turn; Smart's own `search_memory` calls are extra and
  deliberate (they search all channels and do not exclude the baseline).
- The Router depends on `SearchEnrichmentService` (an application service behind ports), and the prefetch quality is
  bounded by the triage's keywords and phrase.
- The same service backs `search_memory`, the claude.ai connector and the companion context, so all of them rank the
  same way.

## Revisit when

The orchestrator needs per-agent search strategies, or prefetch cost/latency shows up on turns that never use it.
