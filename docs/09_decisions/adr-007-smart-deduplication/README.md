# ADR-007: Smart Deduplication Strategy

**Status:** Accepted (2026-02-08) — still in force on the paths listed under Scope.
**Deciders:** Dmytro Deleur, AI Assistant

## Context

Fact deduplication at cosine similarity 0.85 was too aggressive: it dropped about two thirds of the facts, and most
of the "duplicates" were complementary — the same statement with a place, a date or an interpretation added, or
with a different value. LLM-written facts are phrased very consistently, so high similarity alone says little.

## Decision

> **Better to keep a duplicate than to lose information.**

`SmartDeduplication` (`src/domain/deduplication_service.py`) judges a pair in four steps:

1. similarity < 0.96 (`moderate_threshold`) → not a duplicate;
2. the numbers in the two texts differ (compared as sorted lists) → not a duplicate;
3. similarity ≥ the strict threshold (0.98 by default) → duplicate;
4. the existing text is shorter than 85 % of the new one → not a duplicate (the new one is more detailed);
   otherwise → duplicate.

Examples (invented):
- "Gym membership costs 40 EUR a month" vs "Gym membership costs 45 EUR a month" → numbers differ → kept.
- "Prefers window seats" vs "Prefers window seats on long-haul flights" → the new text is much longer → kept.
- "Event on 2025-03-28" vs "Event on 2025-03-29" → numbers differ → kept.
- "Owns a road bike" vs "Owns a road bike." at 0.99 → duplicate.

## Scope

- **Read path:** `SearchEnrichmentService.enrich_context` drops duplicates from the ranked list with it (Router
  prefetch, `search_memory`, the claude.ai connector); the consolidator's own search turns it off
  (`skip_semantic_dedup=True`).
- **Write path:** `FactWriteService` checks it (threshold 0.96) only when `skip_deduplication` is False. Live
  consolidation writes pass `skip_deduplication=True`: the consolidator decides duplicates itself after searching.

## Consequences

- Fewer false positives, at the cost of an occasional real duplicate.
- Sorted numbers ignore order, so "from 80 to 75" equals "from 75 to 80": a change of direction is not detected.
  Accepted, because a change of value is the case that matters and versioning (SCD2) carries history.

## Rejected alternatives

- **One higher threshold, no number check** — still merges facts that differ only in a value or a date.
- **Keep 0.85** — the loss of detail described above.
