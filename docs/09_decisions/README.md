# 09 Decisions (ADRs)

## 📖 HowTo: Using This Document

### Purpose
Tracks architecture decisions (ADRs) and their rationale.

### When to Read
- **For AI Agents:** Before proposing changes that impact architecture.
- **For Developers:** When reasoning about why a design exists.

### When to Update
This document MUST be updated when:
- [ ] A new ADR is added.
- [ ] An ADR is superseded or deprecated.
- [ ] Decision references change.

### Cross-References
- **RFC Index:** [../10_rfcs/README.md](../10_rfcs/README.md)
- **Building Blocks:** [../05_building_blocks/README.md](../05_building_blocks/README.md)

---

## 📂 Contents

[`../10_rfcs/`](../10_rfcs/); nothing new is added here.
|-----|-------|--------|------|------------------------|
| ADR | Title | Accepted | Decision in one line |
|-----|-------|----------|----------------------|
| [ADR-004](./adr-004-agent-handoff-search/README.md) | Agent Handoff Pattern for Web Search | 2026-01-23 | Grounded web search is a specialist the orchestrator delegates to. |
| [ADR-005](./adr-005-router-centric-enrichment/README.md) | Router-Centric Enrichment Pattern | 2026-01-27 | The Router prefetches memory once per message for the whole turn. |
| [ADR-006](./adr-006-semantic-collection-naming/README.md) | Semantic Firestore Collection Naming | 2026-02-05 | `domain_*_vN` for business entities, versionless names for infrastructure. |

---











