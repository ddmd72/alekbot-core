# Decision: Close FactRepository port/adapter contract gap (F7.2)

**Status:** Adopted
**Date:** 2026-05-18
**Context:** Inspection finding F7.2 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

`FactRepository` (port) and `FirestoreFactRepository` (adapter) had two divergences. Closed by lifting both to the port:

1. `search_facts` — added `vector_field: str = "vector"` parameter. Default preserves backward compatibility for callers that omit it.
2. Added `search_facts_by_domain` as an `@abstractmethod` on the port. Adapter already implemented it; callers (`SearchEnrichmentService`) already invoked it via the port type.

## Why

This was the first genuine hexagonal-boundary violation in the inspection. Adapter ahead of port = any alternative `FactRepository` implementation fails at runtime, and the port loses its load-bearing function (mock substitutability + cross-impl correctness guard).

REQ-ARCH-01 + the test density of this codebase are the two mechanical guardrails for AI-pair-programmed code (CC.1 / CC.10). A port that doesn't reflect what is actually called through it is a guardrail with a hole.

## Concrete consequences

- `src/ports/repository.py` — `search_facts` signature widened with `vector_field`; new `search_facts_by_domain` abstract.
- Adapter (`src/adapters/firestore_repo.py`) unchanged — already had both.
- `tests/unit/ports/test_core_port_contracts.py` — port-contract test updates (3 test changes, all explicitly approved):
  - `test_all_abstract_methods_count`: 20 → 21.
  - `test_search_facts_signature`: added `vector_field` to expected params list + default assertion.
  - New `test_has_search_facts_by_domain` matching the `test_has_*` pattern.
- No production code changes needed at callsites — they already used the wider shape.
- All existing `AsyncMock(spec=FactRepository)` test mocks (conftest + ~14 files) gain `search_facts_by_domain` automatically via spec.

## Rejected alternatives

- **Leave it alone.** The audit name "first genuine hexagonal boundary violation" is precise — every other §7 finding was internal. Leaving the violation in place undermines REQ-ARCH-01 as a load-bearing rule.
- **Remove the adapter methods to match the port.** Would break `SearchEnrichmentService` for the keyword/phrase/metadata multi-vector search path (active production code) and the router-enrichment domain-search path. Wrong direction.
- **Align `limit` default (5 vs 10) at the same time.** Considered as adjacent cleanup; not in F7.2 scope and may have intentional reasons (consolidation vs search call sites differ). Tracked separately if needed.

## Trigger to revise

- A second `FactRepository` implementation gets added (test double, in-memory variant, alternative backend) — verify both methods are implemented there.
- `vector_field` grows beyond a string discriminator into a richer concept (e.g. weighted multi-vector input) — promote to a value object on the port.
