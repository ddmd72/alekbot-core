# Biographical Context Cache (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the high-speed caching system for user-specific biographical facts and guiding principles.

### When to Read

- **For AI Agents:** Before modifying cache refresh logic, keyword resolution, or context formatting.
- **For Developers:** When troubleshooting stale context, missing principles, or slow prompt assembly.

### When to Update

This document MUST be updated when:

- [ ] The cache storage mechanism (Firestore collection) changes.
- [ ] The 3-level configuration resolution for keywords or limits is modified.
- [ ] The logic for separating facts and principles changes.
- [ ] The cache refresh trigger (e.g., after consolidation) is updated.
- [ ] New technical tags for filtering are introduced.

### Cross-References

- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Sliding Window Consolidation:** [../sliding_window_consolidation/README.md](../sliding_window_consolidation/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

The **Biographical Context Cache** provides agents with instant access to a user's most important personal information and behavioral anchors. It eliminates the need for expensive vector searches during every prompt assembly by maintaining a pre-ranked, deduplicated set of facts in a high-speed storage layer.

**Core Principle:** Fast reads for prompt assembly, background updates for knowledge evolution.

---

## 2. Cache Structure

The cache is stored in the `user_context` collection and is split into two distinct categories:

### 2.1 Biographical Facts (`facts`)

- **Content:** Episodic memories, personal details, and historical events.
- **Limit:** Default 50 facts (configurable).
- **Source:** Extracted from conversation history during consolidation.

### 2.2 Guiding Principles (`principles`)

- **Content:** Behavioral anchors, core beliefs, and interaction preferences (mindset).
- **Limit:** Default 15 principles (configurable).
- **Source:** Facts with tag `"mindset"` (any domain) - classified by consolidation v3.
- **Rule:** Principle = any fact containing `"mindset"` tag, regardless of domain.

---

## 3. Dynamic Configuration

The cache behavior is governed by a **3-level resolution strategy** (USER > ACCOUNT > SYSTEM), managed by the `ConfigurationService`.

### 3.1 Search Keywords

To refresh the cache, the system resolves three sets of keywords:

1. **Query 1:** Focuses on tags and metadata (e.g., "identity", "bio").
2. **Query 2:** Focuses on text and tags (e.g., "health", "preferences").
3. **Query 3:** Focuses on text and metadata (e.g., "assets", "history").

### 3.2 Context Limits

- `biographical_cache_limit`: Controls the size of the `facts` section.
- `principles_cache_limit`: Controls the size of the `principles` section.

---

## 4. Refresh Mechanism

The cache is refreshed automatically after every successful consolidation batch.

### 4.1 Refresh Pipeline

1. **Trigger:** `ConsolidationAgent` completes a batch and calls `refresh_biographical_context_cache()`.
2. **Resolution:** `BiographicalContextService` resolves the current keywords and limits for the account.
3. **Retrieval:** Delegates to `SearchEnrichmentService` to perform a multi-vector RRF search using the resolved keywords.
   - **Deduplication:** Uses smart semantic deduplication (2026-02-08 update)
   - **Threshold:** Default 0.98 (balanced filtering)
   - **Number-Aware:** Different numbers = different facts (time-series support)
4. **Processing:**
   - Fetches full `FactEntity` objects for metadata.
   - Filters out technical tags (e.g., `#consolidated`, `#test`).
   - Separates facts by type (`PRINCIPLE` vs others).
5. **Persistence:** Saves the processed lists back to the Firestore cache.

---

## 5. Integration with Prompt v3

During prompt assembly, the `PromptAssemblyService` retrieves the cached context:

- **Formatting:** `BiographicalFactsFormatter` converts facts into domain-grouped Markdown sections.
  - **Session 2026-02-17:** Domain-based structure (biographical, health, preference, etc.)
  - **Hashtags removed:** Clean output (except `[MINDSET]` prefix for behavioral anchors)
  - **Dual sorting:** biographical (oldest→newest), others (newest→oldest)
  - **Semantic facts:** Preserved as separate "Query-Specific Context" section
- **Validation:** The context is treated as `UNTRUSTED` and passed through the `SecurityPort` before injection.

---

## 6. Code References

- `src/services/biographical_context_service.py`: Main refresh orchestration.
- `src/services/prompt_v3/biographical_formatter.py`: Formatting for prompts.
- `src/services/configuration_service.py`: 3-level limit and keyword resolution.
- `src/adapters/firestore_repo.py`: Cache persistence methods.

---

## 7. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Proactive Refresh:** Refresh the cache when a user updates their profile or settings.
- **Semantic TTL:** Automatically expire facts that are contradicted by newer information.
- **Tiered Caching:** Use an in-memory cache (Redis) for even faster access in high-traffic accounts.

---

**Last Updated:** 2026-02-17  
**Status:** ✅ Complete  
**Phase:** Updated for Mindset-based principles & state=CURRENT filtering
