# Decision: number-aware deduplication — keep a duplicate rather than lose detail (formerly ADR-007)

**Status:** Accepted (2026-02-08) — in force on the read path.
**Mechanics today:** `docs/05_building_blocks/memory/README.md` §4.3 (step "Semantic dedup");
code `src/domain/deduplication_service.py` (`SmartDeduplication`).

## Context

Fact deduplication at cosine similarity 0.85 dropped about two thirds of the facts, and most of the "duplicates" were
complementary: the same statement with a place, a date or an interpretation added, or with a different value.
LLM-written facts are phrased very consistently, so high similarity alone says little.

## Decision

Better to keep a duplicate than to lose information. A pair is a duplicate only when similarity is at least 0.96, the
numbers in both texts are equal (compared as sorted lists), and either similarity reaches the strict threshold (0.98) or
the existing text is at least 85 % as long as the new one (a clearly longer new text is more detailed and is kept).

## Scope

- **Read path:** the ranked search result drops duplicates with it (Router prefetch, `search_memory`, the connector);
  the consolidator's own search turns it off, because the consolidator decides duplicates itself.
- **Write path:** no live write checks it; `add_fact_if_unique` keeps the check and has no production caller.

## Rejected alternatives

- **One higher threshold, no number check:** still merges facts that differ only in a value or a date.
- **Keep 0.85:** the loss of detail above.

## Consequences

- Fewer false positives, at the cost of an occasional real duplicate.
- Sorted numbers ignore order, so "from 66 to 64" equals "from 64 to 66": a change of direction is not detected.
  Accepted, because a change of value is the case that matters and SCD2 versioning carries the history.
