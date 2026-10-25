# 09 Decisions (ADRs)

The older architecture decision records whose decisions still shape the code. New single decisions are written to
[`../04_solution_strategy/decisions/`](../04_solution_strategy/decisions/), multi-step changes to
[`../10_rfcs/`](../10_rfcs/); nothing new is added here.

| ADR | Title | Accepted | Decision in one line |
|-----|-------|----------|----------------------|
| [ADR-004](./adr-004-agent-handoff-search/README.md) | Agent Handoff Pattern for Web Search | 2026-01-23 | Grounded web search is a specialist the orchestrator delegates to. |
| [ADR-005](./adr-005-router-centric-enrichment/README.md) | Router-Centric Enrichment Pattern | 2026-01-27 | The Router prefetches memory once per message for the whole turn. |
| [ADR-006](./adr-006-semantic-collection-naming/README.md) | Semantic Firestore Collection Naming | 2026-02-05 | `domain_*_vN` for business entities, versionless names for infrastructure. |
| [ADR-007](./adr-007-smart-deduplication/README.md) | Smart Deduplication Strategy | 2026-02-08 | Number-aware dedup at 0.96: keep a duplicate rather than lose detail. |
| [ADR-008](./adr-008-local-first-external-provider/README.md) | External Service as Source of Truth with Thin Local Search Index | 2026-03-18 | MS To Do holds task data; Firestore holds only a vector index. |
