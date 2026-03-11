# RFC: Native Tools Integration & Router-Centric Enrichment (v2)

**Status:** Partial (SearchEnrichmentService implemented; Quick delegation refactored; biographical cache → HEXAGONAL_PROMPT_CACHING_RFC)
**Date:** 2026-01-27
**Updated:** 2026-02-28
**Owner:** AI Engineering
**Milestone:** M4 (Multi-Agent Integration)

**Related ADR:** ADR-005 (Router-Centric Enrichment)
**Related Building Block:** Search Enrichment
**Implemented Sections:** 3.2 (Router-Centric Enrichment), 4 (SearchEnrichmentService), 5 (Biographical Cache — see HEXAGONAL_PROMPT_CACHING_RFC.md)

---

## 1. Problem Statement

We need a scalable, hexagonal architecture that:

1. Uses **native provider tools** (e.g., Google Search grounding) without conflicts.
2. Avoids manual reasoning chains where unnecessary.
3. Preserves **personalization** (memory search, lenses, tone) before response generation.
4. Allows **future expansion** of custom tools without breaking provider tool chains.
5. Supports **provider interchangeability** (Gemini ↔ Claude ↔ OpenAI) through clean boundaries.

Current pain points:
- Native tool calls conflict with custom tool declarations in a single provider call.
- SmartResponseAgent must remain an orchestrator for custom tools and multi-step reasoning.
- We need consistent enrichment and deduplication across Quick and Smart paths.

---

## 2. Current State

### 2.1 Routing Pipeline

```
User → RouterAgent → QuickResponseAgent | SmartResponseAgent
```

### 2.2 Personalization Pipeline (Current)
- RouterAgent extracts tone, complexity, keywords (LLM triage).
- Quick/Smart agents run their own semantic lens search (duplicated work).
- Biographical context is injected via `PromptBuilder` (Firestore cache, ~10ms).

### 2.3 Tool Usage (Current)
- SmartResponseAgent uses **manual function-calling loop** for custom tools.
- WebSearchAgent uses **native Google Search grounding** (isolated).

---

## 3. Approved Architecture (Router-Centric Enrichment)

### 3.1 Principle: Separation by Responsibility & Speed

```
RouterAgent (Gemini Flash)
  ├─ LLM Triage (tone, keywords, phrases, complexity)
  ├─ Delegates enrichment to SearchEnrichmentService
  └─ Route → Quick or Smart

SearchEnrichmentService (Application Service)
  ├─ Triple Memory Search (keywords + phrase_1 + phrase_2)
  ├─ Weighted Merge + Dedup
  ├─ Double Dedup vs Biographical Cache
  └─ Returns EnrichedContext

QuickResponseAgent (Gemini Flash)
  ├─ Receives enriched_context from Router
  └─ Uses native provider tools only

SmartResponseAgent (Claude Sonnet)
  ├─ Receives enriched_context from Router
  ├─ Custom tool orchestration
  └─ Optional prompt caching for biographical context
```

### 3.2 Key Decisions
- ✅ **Search is Router-owned via SearchEnrichmentService** (single source of enrichment).
- ✅ **Triple search** with 2 LLM phrases + keyword search.
- ✅ **Weighted merge + dedup** across all search results.
- ✅ **Double dedup** against biographical cache to avoid token waste.
- ✅ **Biographical cache optional prompt caching (Smart path only).**
- ✅ **Provider-agnostic cache protocol** in `LLMPort` port.
- ✅ **Fallback**: if Smart/Claude unavailable → route to Quick.
- ⏳ **Burst session detection** deferred until cache is implemented.
- ⏳ **Anchors/Core Facts tagging** deferred to a future milestone.

---

## 4. RouterAgent Enhancements (Triage + Enrichment)

### 4.1 LLM Triage Output

Router LLM must return:

```
tone
keywords
search_phrase_1
search_phrase_2
complexity
escalation_flags
```

### 4.2 SearchEnrichmentService (Parallel Search)

```
keyword_results   = search_by_keywords(keywords)
phrase_results_1  = search_by_phrase(search_phrase_1)
phrase_results_2  = search_by_phrase(search_phrase_2)
```

All three queries are executed **async in parallel** to keep Router latency low.

### 4.3 Weighted Merge Strategy (Approved)

```
merged = []
merged.extend(keyword_results[:10])
merged.extend(phrase_results_1[:15])
merged.extend(phrase_results_2[:10])
unique = deduplicate_preserve_order(merged)[:30]
```

### 4.4 Double Deduplication (Against Biographical Context)

```
biographical_ids = set(biographical_facts.fact_id)
enriched_context = [f for f in unique if f.fact_id not in biographical_ids]
```

Result: 25–30 unique enriched facts without duplicating core identity facts.

### 4.5 SearchEnrichmentService Responsibilities

SearchEnrichmentService lives in `src/services/` and is injected into RouterAgent.

Responsibilities:
1. Execute 3 parallel Firestore searches (keywords + phrase_1 + phrase_2)
2. Perform weighted merge with stable ordering
3. Deduplicate facts across searches
4. Deduplicate against biographical cache
5. Return `EnrichedContext` (Pydantic model)

This isolates heavy enrichment logic from RouterAgent and keeps routing lean.

---

## 5. Biographical Context Cache (Smart Path Only)

### 5.1 Purpose
Cache biographical context only when:
- Router routes to Smart agent
- Active conversation is detected (burst session)

**Goal:** Avoid re-sending the full biographical context on every Smart request.

### 5.2 Provider-Agnostic Cache Protocol

We must keep caching **outside domain logic** and **inside ports/adapters**.

#### New Port Models (LLMPort)

```python
class PromptCacheConfig(BaseModel):
    enabled: bool = False
    ttl_seconds: Optional[int] = None
    cache_scope: str = "user"
    cache_key: Optional[str] = None

class CacheMetadata(BaseModel):
    provider: str
    cache_id: Optional[str] = None
    cache_hit: bool = False
    tokens_saved: int = 0
    created_at: float
    expires_at: Optional[float] = None
```

#### Port Extension
```python
class LLMPort(ABC):
    async def generate_content(..., cache_config: Optional[PromptCacheConfig] = None) -> LLMResponse:
        pass

    def supports_caching(self) -> bool:
        pass
```

### 5.3 Cache Metadata Storage

**Approved:** store cache metadata in `session_store` (not domain).

This keeps cache lifecycle aligned with session continuity and avoids bleeding provider-specific data into domain.

---

## 6. QuickResponseAgent Enhancement (Native Tools)

QuickResponseAgent should handle most queries by using **native provider tools** only.

**Constraints:**
- No custom tool declarations
- Native tools only (Gemini grounding)
- Receives `enriched_context` from Router

---

## 7. SmartResponseAgent Role (Custom Tool Orchestration)

SmartResponseAgent remains the **custom tools orchestrator** and uses Claude Sonnet for deeper reasoning.

**Responsibilities:**
1. Accept enriched context from Router
2. Build system prompt with biographical context
3. Use manual function-calling loop for custom agents
4. Optional prompt caching when supported by provider

**Fallback:** If Smart provider is unavailable → route to QuickResponseAgent.

---

## 8. Provider Interchangeability

We must maintain hexagonal boundaries:

- ✅ Domain contains no infrastructure logic
- ✅ Ports define provider-agnostic contracts
- ✅ Adapters implement provider-specific logic
- ✅ Application layer uses ports via DI

Example provider swap:

```python
# main.py
llm_smart = ClaudeAdapter(api_key=CLAUDE_KEY)
smart_agent = SmartResponseAgent(llm_port=llm_smart)

# swap to Gemini
llm_smart = GeminiAdapter(api_key=GEMINI_KEY)
```

---

## 9. Performance & Cost Impact

| Path | Model | Tooling | Expected Latency | Use Case |
|------|-------|---------|------------------|----------|
| Quick | Gemini Flash | Native tools | ~2–4s | 70–80% of queries |
| Smart | Claude Sonnet | Custom tools | ~8–15s | Complex / sensitive |

Cost optimization:
- Quick path absorbs most traffic.
- Smart path uses caching to reduce repeated biographical prompt cost.

---

## 10. Detailed Implementation Plan (Approved)

### Phase 1 — **Port Extension (Week 1, Days 1–2)**
**Goal:** Add provider-agnostic cache protocol to `LLMPort`.

1. Add `PromptCacheConfig` model to `src/ports/llm_port.py`.
2. Add `CacheMetadata` model to `src/ports/llm_port.py`.
3. Extend `LLMResponse` with `cache_metadata`.
4. Add `cache_config` parameter to `generate_content()`.
5. Add `supports_caching()` abstract method.
6. Update unit tests for port models.

**Deliverable:** Updated port contract.

---

### Phase 2 — **SearchEnrichmentService + Router Integration (Week 1, Days 3–5)**
**Goal:** Implement keyword + 2 phrase searches with weighted merge + dedup as a reusable service.

1. Update triage prompt to output 2 search phrases.
2. Add `EnrichedContext` domain model (Pydantic).
3. Create `src/services/search_enrichment_service.py`.
4. Implement async parallel Firestore queries inside service.
5. Weighted merge strategy (keyword 10 + phrase_1 15 + phrase_2 10).
6. Double deduplication vs biographical cache.
7. Inject SearchEnrichmentService into RouterAgent.
8. Update RouterAgent to pass `enriched_context` into message context.
9. Update UserAgentFactory DI to instantiate SearchEnrichmentService.
10. Unit tests for merge/dedup logic (mock Firestore).
11. Integration tests (dev Firestore).

---

### Phase 3 — **Claude Adapter (Week 2, Days 1–3)**
**Goal:** Implement Claude adapter with caching.

1. Install Anthropic SDK.
2. Create `src/adapters/claude_adapter.py`.
3. Implement cache breakpoints via `PromptCacheConfig`.
4. Map cache metadata from provider response.
5. Unit tests (mock API).

---

### Phase 4 — **Quick Agent Native Tools (Week 2, Days 4–5)**
**Goal:** Enable Gemini native tools for QuickResponseAgent.

1. Update `GeminiAdapter` to enable native tools.
2. Ensure Quick prompt excludes custom tool hints.
3. Validate latency and correctness.

---

### Phase 5 — **Smart Agent Claude Integration (Week 3, Days 1–2)**
**Goal:** Inject Claude into SmartResponseAgent and use cache if supported.

1. Update `UserAgentFactory` to inject Claude adapter.
2. Pass `cache_config` from Router → Smart.
3. Log cache hits / token savings.
4. Fallback to Quick if Claude unavailable.

---

### Phase 6 — **E2E Testing & Rollout (Week 3, Days 3–5)**
**Goal:** Validate full pipeline + observe metrics.

1. E2E test: Router → Quick (native tools).
2. E2E test: Router → Smart (Claude cached).
3. Load test: burst sessions & cache hit rates.
4. Shadow rollout (10% → 50% → 100%).

---

## 11. Deferred Milestones

### Milestone X — Anchors / Core Facts Tagging
We currently lack an explicit mechanism to tag facts as core identity. This will be handled later.

Potential strategies:
- Manual tagging by ConsolidationAgent (`importance_level=anchor`)
- Usage-based auto-tagging (`hit_count` threshold)
- Hybrid ranking (recency + frequency + importance)

### Milestone Y — Multi-Provider Billing
Provider selection and billing overrides per user are deferred to a later milestone.

---

## 12. Decision Summary

✅ Router performs triple semantic search and dedup.
✅ Weighted merge strategy approved.
✅ Biographical cache is prompt-cached only for Smart + active conversation.
✅ Cache protocol is provider-agnostic and stored in session_store.
✅ Smart uses Claude Sonnet; Quick uses Gemini Flash native tools.
✅ Fallback: Smart unavailable → Quick.

---

## 13. Discovery & Baseline (2026-01-27) — Historical Context

> **Note:** §13–14 describe the state and plan as of 2026-01-27. The system has since evolved significantly.
> Key changes: prompt system migrated from monolithic `kernel`/`kernel_light` facts in Firestore to
> **PromptBuilder v3/v4** (token-based, blueprint-driven, per-agent profiles). Old upload/download scripts
> (`scripts/memory/ops/`) do not exist. Quick agent delegation is now function-calling based (not grounding);
> `WebSearchLightAgent` (ECO tier) handles light web search for Quick separately (session 7, commit c38191e).

### 13.1 Prompt System (as of 2026-01-27)
- System prompts stored in Firestore as `kernel` / `kernel_light` Facts under `SYSTEM` owner.
- **Replaced by:** PromptBuilder v3 (token system) and v4 (blueprint + ProfileToken) — see PROMPT_BUILDER_V4_RFC.md.

### 13.2 Quick Agent Tool Delegation (Resolved)
- Original plan: native Gemini grounding (`automatic_function_calling=True`).
- **Actual implementation:** Quick uses explicit function-calling (`search_memory`, `search_web_light`) with `MAX_DELEGATION_TURNS=2`. `WebSearchLightAgent` (ECO, Flash Lite + Google grounding) handles web search in isolation because Gemini cannot combine grounding + function-calling in one request.

### 13.3 Smart Agent (Resolved)
- Smart uses Claude (Opus/Sonnet via PERFORMANCE tier). Custom tool loop: `search_memory`, `search_web`, `search_email` (planned).
- Prompt caching implemented via `HEXAGONAL_PROMPT_CACHING_RFC` (CachingLLMProxy + CACHE_BOUNDARY).

---

## 14. Detailed Implementation Workflow (2026-01-27) — Superseded

> Steps 1–5 below reflect the original plan. All steps are either implemented differently or superseded.
> See current architecture in `docs/04_solution_strategy/current_implementation/STRUCTURE.md`.

- Step 1 (Port Extensions): `AgentExecutionContext` + `ProviderCapabilities` implemented (supersedes `AutomaticFunctionCallingConfig`).
- Step 2 (Native Grounding): Implemented for WebSearchAgent and WebSearchLightAgent only. Quick uses function calling.
- Step 3 (Prompt Migration): Superseded by PromptBuilder v3/v4 — token upload via `firestore_utils/upload.py`.
- Step 4 (Quick Refactoring): Done (session 7). Quick has `MAX_DELEGATION_TURNS=2`, function-calling, no grounding.
- Step 5 (Claude Adapter): Implemented (`src/adapters/claude_adapter.py`), with prompt caching.

---

## Changelog

### 2026-01-27
- Added SearchEnrichmentService as Router-owned enrichment pipeline.
- Updated Phase 2 plan to include EnrichedContext models and service DI wiring.