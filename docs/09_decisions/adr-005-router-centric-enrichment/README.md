# ADR-005: Router-Centric Enrichment Pattern

## Status

**Accepted & Implemented** | **Date:** 2026-01-27 | **Production:** 2026-01-30

---

## Context

### Problem Statement

Before this decision, each agent (Quick, Smart) performed its own semantic memory search:
- **Duplication:** Same search executed multiple times per user query
- **Inconsistency:** Quick and Smart used different search strategies
- **Waste:** Biographical facts duplicated in enriched context (token waste)
- **Latency:** Extra Firestore queries added 500-1000ms per agent

### The Challenge

How to provide **personalized context** to Quick/Smart agents without:
1. Duplicating semantic searches
2. Mixing biographical facts with enriched facts
3. Requiring agents to know about search implementation
4. Breaking hexagonal boundaries

---

## Decision

**We centralized memory enrichment in RouterAgent using SearchEnrichmentService**

### Core Pattern

```
RouterAgent
  ├─> Receives user query
  ├─> LLM Triage (extracts keywords + 2 phrases)
  ├─> Delegates to SearchEnrichmentService
  │     ├─> Parallel: keyword search (10 results)
  │     ├─> Parallel: phrase_1 search (15 results)
  │     ├─> Parallel: phrase_2 search (10 results)
  │     ├─> Weighted merge → 35 facts
  │     ├─> Dedup by fact_id → ~30 unique facts
  │     └─> Dedup vs biographical cache → ~25-28 enriched facts
  ├─> Attaches EnrichedContext to AgentMessage
  └─> Routes to Quick/Smart with pre-enriched context
```

### Rationale

1. **Single Source of Truth:** Router is the only component performing enrichment
2. **Efficiency:** One search per query, shared by all downstream agents
3. **Consistency:** Quick and Smart receive identical context base
4. **Deduplication:** Biographical facts excluded from enriched context
5. **Hexagonal:** SearchEnrichmentService is application service, not domain logic

---

## Implementation

### SearchEnrichmentService

**Location:** `src/services/search_enrichment_service.py`

**Responsibilities:**
1. Execute 3 parallel Firestore vector searches (keywords + 2 phrases)
2. Perform weighted merge with limits (10+15+10 = 35 initial)
3. Deduplicate by fact_id (→ ~30 unique)
4. Deduplicate against biographical cache (→ ~25-28 final)
5. Return `EnrichedContext` with `List[EnrichedFact]`

**Domain Models:**
- `EnrichedFact` (fact_id, content, source, relevance_score)
- `EnrichedContext` (facts, total_sources, dedup_count, biographical_dedup_count)

### Integration Flow

```python
# RouterAgent.execute()
enriched_context = await self.search_enrichment.enrich_context(
    keywords=keywords,
    phrase_1=search_phrase_1,
    phrase_2=search_phrase_2,
    user_id=user_id
)

# Attach to message
message.context["enriched_context"] = enriched_context

# Quick/SmartResponseAgent receive pre-enriched message
# They extract enriched_context.facts and inject into knowledge_base
```

### Weighted Merge Strategy

Current implementation:
- keyword: limit 10 (precise)
- phrase_1: limit 15 (primary semantic)
- phrase_2: limit 10 (secondary semantic)
- global cap: 30 after dedup

**Why these weights?**
- Keywords provide precision
- Phrase_1 provides breadth
- Phrase_2 provides alternative angle
- Total ~30 facts ≈ 2-3K tokens (manageable)

### Double Deduplication

**First pass:** Remove duplicate fact_ids across 3 search results
**Second pass:** Remove facts already in biographical cache

**Impact:** Reduces token waste by 20-30% on average

---

## Consequences

### Positive

- ✅ **60% latency reduction:** One enrichment per query vs N agent searches
- ✅ **Consistency:** All agents see identical enriched facts
- ✅ **Token efficiency:** Biographical dedup saves 500-1000 tokens per request
- ✅ **Scalability:** Adding new agents doesn't multiply searches
- ✅ **Testability:** SearchEnrichmentService testable in isolation

### Negative

- ⚠️ **Coupling:** Router must know about enrichment (application layer dependency)
- ⚠️ **Flexibility:** Agents can't customize search strategy per agent type
- ⚠️ **Cache complexity:** Router cache invalidation more critical

### Neutral

- 🔄 **Search quality:** Triple search provides better coverage but higher cost than single
- 🔄 **Phrase extraction:** LLM triage quality determines enrichment quality

---

## Related Decisions

### ADR-004: Agent Handoff Pattern
SearchEnrichmentService complements Agent Handoff by providing **proactive context** before delegation, reducing the need for reactive tool calls.

### Future: Adaptive Caching (In Progress)
RFCs propose caching enriched context for burst sessions. Not yet implemented.

### Future: Continuity-Aware Routing (In Progress)
RFCs propose topic_similarity tracking to prevent context loss on Smart→Quick transitions. Not yet implemented.

---

## Compliance

### Hexagonal Architecture
- ✅ SearchEnrichmentService is **application service**, not domain
- ✅ Uses `FactRepository` port (not Firestore directly)
- ✅ Uses `EmbeddingService` port (not Gemini directly)
- ✅ Returns domain model (`EnrichedContext`)

### Actor Model
- ✅ RouterAgent remains stateless
- ✅ Enrichment is synchronous operation within routing decision
- ✅ No side effects (pure function)

---

## Lessons Learned

### What Worked Well

1. **Parallel Firestore queries:** `asyncio.gather` reduced search time from ~1500ms to ~500ms
2. **Biographical dedup:** Eliminated 20-30% token waste immediately
3. **Phrase extraction via LLM:** Higher quality than keyword-only search

### What Didn't Work

- **Initial implementation:** Agent-owned enrichment caused duplication and inconsistency
- **Single search approach:** Not enough context coverage for complex queries

### Future Improvements

- [ ] **Adaptive weights:** Adjust merge weights based on query complexity
- [ ] **Lens integration:** Use semantic lens to filter fact types
- [ ] **Caching:** Cache enriched context for burst sessions (5-min TTL)

---

## References

- **RFC (Partial):** `docs/10_rfcs/ADAPTIVE_ROUTING_CACHE_RFC.md` (active)
- **RFC (Partial):** `docs/10_rfcs/NATIVE_TOOLS_INTEGRATION_RFC.md` (active)
- **Implementation:** `src/services/search_enrichment_service.py`
- **Domain Models:** `src/domain/search.py`
- **Building Block:** [../../05_building_blocks/search_enrichment/README.md](../../05_building_blocks/search_enrichment/README.md)

---

## Status History

| Date | Status | Reason |
|------|--------|--------|
| 2026-01-27 | Proposed (RFCs) | Part of Native Tools Integration plan |
| 2026-01-27 | Implemented | SearchEnrichmentService deployed |
| 2026-01-30 | Production | Router-centric enrichment active |
| 2026-01-30 | Documented (ADR) | Extracted from RFCs during migration |
