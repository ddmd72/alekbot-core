# Biographical Context Cache (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the high-speed caching system for user-specific biographical facts and guiding principles.

### When to Read

- **For AI Agents:** Before modifying cache refresh logic, priority classification, or context formatting.
- **For Developers:** When troubleshooting stale context, missing principles, or slow prompt assembly.

### When to Update

This document MUST be updated when:

- [ ] The cache storage mechanism (Firestore collection) changes.
- [ ] The 3-level configuration resolution for limits is modified.
- [ ] The logic for separating facts and principles changes.
- [ ] The cache refresh trigger (e.g., after consolidation) is updated.
- [ ] The priority-rank ordering or domain-first strategy changes.

### Cross-References

- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Sliding Window Consolidation:** [../sliding_window_consolidation/README.md](../sliding_window_consolidation/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

The **Biographical Context Cache** provides agents with instant access to a user's most important personal information and behavioral anchors. It eliminates the need for expensive vector searches during every prompt assembly by maintaining a pre-ranked, deduplicated set of facts in a high-speed storage layer.

**Core Principle:** Fast reads for prompt assembly, background updates after consolidation.

**Architecture note:** The cache is built via **direct priority-ordered repository queries** — no semantic search at cache-build time. Consolidation already classified and tagged all facts; the cache layer simply selects and orders them. `SearchEnrichmentService` is used only at query time (MemorySearchAgent), not here.

---

## 2. Cache Structure

The cache is stored in the `user_context` collection and is split into two distinct categories:

### 2.1 Biographical Facts (`facts`)

- **Content:** Episodic memories, personal details, and historical events.
- **Limit:** Default 50 facts (configurable via `biographical_cache_limit`).
- **Source:** Extracted from conversation history during consolidation.
- **Ordering:** Priority-first (`CRITICAL` → `HIGH` → `MEDIUM` → `LOW`), then by `created_at DESC` within each tier.

### 2.2 Guiding Principles (`principles`)

- **Content:** Behavioral anchors, core beliefs, and interaction preferences (mindset).
- **Limit:** Default 15 principles (configurable via `principles_cache_limit`).
- **Source:** Facts with tag `"mindset"` (any domain) — classified by consolidation.
- **Rule:** Principle = any fact containing `"mindset"` tag, regardless of domain.

---

## 3. Priority-Based Selection

Facts are ranked by `context_priority_rank` — an adapter-internal integer field written to Firestore by `FirestoreFactRepository` on every `add_fact` / `update_fact`. **It is not part of `FactEntity`** (domain model is unaware).

| `ContextPriority` | `context_priority_rank` |
|---|---|
| `CRITICAL` | 1 |
| `HIGH` | 2 |
| `MEDIUM` | 3 |
| `LOW` | 4 |
| `ARCHIVAL` | 5 |

### 3.1 Domain-First Selection

Biographical domain facts are preferred over all others at equal priority:

1. **Query 1:** `domain=BIOGRAPHICAL, ORDER BY rank ASC, created_at DESC, LIMIT K`
2. **Query 2 (fill):** If Q1 returns fewer than K facts — fetch all domains (same ordering), exclude IDs from Q1, append until limit reached.

This ensures biographical identity facts (name, age, relationships) always appear in context before professional or health facts of the same priority tier.

### 3.2 Context Limits

Resolved dynamically via `ConfigurationService` (USER > ACCOUNT > SYSTEM):

- `biographical_cache_limit`: Controls the size of the `facts` section (default: 50).
- `principles_cache_limit`: Controls the size of the `principles` section (default: 15).

---

## 4. Refresh Mechanism

The cache is refreshed automatically after every successful consolidation batch.

### 4.1 Refresh Pipeline

1. **Trigger:** `ConsolidationAgent` completes a batch and calls `BiographicalContextService.refresh_context()`.
2. **Resolution:** Resolves `biographical_cache_limit` and `principles_cache_limit` for the account.
3. **Retrieval (domain-first):**
   - Q1: `get_active_facts_ordered(account_id, domain=BIOGRAPHICAL, limit=K)` — biographical facts ordered by `context_priority_rank`.
   - Q2 (fill): if `len(Q1) < K`, fetch all domains ordered by rank, filter out Q1 IDs, append up to `K - len(Q1)`.
4. **Separation:** Facts with `"mindset"` tag → `principles` list (up to `principles_limit`). Others → `facts` list.
5. **Persistence:** Saves processed lists back to the `user_context` Firestore collection.

### 4.2 No Semantic Search at Cache Time

The old implementation used `SearchEnrichmentService` + keyword queries for cache building. This was replaced (2026-02-24):

- Consolidation already classifies and assigns `context_priority` to every fact.
- Priority-ordered repository queries are deterministic, 2–5× faster, and require no embedding calls.
- `SearchEnrichmentService` is now used only at query time (by `MemorySearchAgent`), not here.

---

## 5. Integration with Prompt Assembly

During prompt assembly, `PromptAssemblyService._inject_runtime_context()` retrieves the cached context:

- **Formatting:** `BiographicalFactsFormatter` converts facts into domain-grouped Markdown sections.
  - Domain-based structure (biographical, health, preference, etc.)
  - Hashtags removed from output (except `[MINDSET]` prefix for behavioral anchors)
  - Dual sorting: biographical (oldest→newest), others (newest→oldest)
- **Cache Boundary split:** Facts with `"semantic_lens"` tag go to the dynamic (post-boundary) section; all others are in the static (pre-boundary, cached) section.
- **Validation:** Context is treated as `UNTRUSTED` and passed through `SecurityPort` before injection.

---

## 6. Code References

- `src/services/biographical_context_service.py`: Refresh orchestration, domain-first selection.
- `src/adapters/firestore_repo.py`: `get_active_facts_ordered()` — Firestore query with `context_priority_rank` ordering. `_PRIORITY_RANK` mapping dict.
- `src/services/prompt_v3/biographical_formatter.py`: Formatting for prompts.
- `src/services/configuration_service.py`: 3-level limit resolution.

---

## 7. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Proactive Refresh:** Refresh the cache when a user updates their profile or settings.
- **Semantic TTL:** Automatically expire facts that are contradicted by newer information.
- **Tiered Caching:** Use an in-memory cache (Redis) for even faster access in high-traffic accounts.

---

**Last Updated:** 2026-03-02
**Status:** ✅ Production Ready
**Phase:** Priority-based selection + domain-first bio cache (2026-02-24)
