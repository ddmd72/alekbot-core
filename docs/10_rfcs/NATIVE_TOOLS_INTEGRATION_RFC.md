# RFC: Native Tools Integration & Router-Centric Enrichment (v2)

**Status:** Active (Partial Implemented)
**Date:** 2026-01-27
**Owner:** AI Engineering (Cline)
**Milestone:** M4 (Multi-Agent Integration)

**Related ADR:** ADR-005 (Router-Centric Enrichment)
**Related Building Block:** Search Enrichment
**Implemented Sections:** 3.2 (Router-Centric Enrichment), 4 (SearchEnrichmentService)

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
RouterAgent (Gemini 3 Flash)
  ├─ LLM Triage (tone, keywords, phrases, complexity)
  ├─ Delegates enrichment to SearchEnrichmentService
  └─ Route → Quick or Smart

SearchEnrichmentService (Application Service)
  ├─ Triple Memory Search (keywords + phrase_1 + phrase_2)
  ├─ Weighted Merge + Dedup
  ├─ Double Dedup vs Biographical Cache
  └─ Returns EnrichedContext

QuickResponseAgent (Gemini 3 Flash)
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
- ✅ **Provider-agnostic cache protocol** in `LLMService` port.
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

#### New Port Models (LLMService)

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
class LLMService(ABC):
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
smart_agent = SmartResponseAgent(llm_service=llm_smart)

# swap to Gemini
llm_smart = GeminiAdapter(api_key=GEMINI_KEY)
```

---

## 9. Performance & Cost Impact

| Path | Model | Tooling | Expected Latency | Use Case |
|------|-------|---------|------------------|----------|
| Quick | Gemini 3 Flash | Native tools | ~2–4s | 70–80% of queries |
| Smart | Claude Sonnet | Custom tools | ~8–15s | Complex / sensitive |

Cost optimization:
- Quick path absorbs most traffic.
- Smart path uses caching to reduce repeated biographical prompt cost.

---

## 10. Detailed Implementation Plan (Approved)

### Phase 1 — **Port Extension (Week 1, Days 1–2)**
**Goal:** Add provider-agnostic cache protocol to `LLMService`.

1. Add `PromptCacheConfig` model to `src/ports/llm_service.py`.
2. Add `CacheMetadata` model to `src/ports/llm_service.py`.
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
✅ Smart uses Claude Sonnet; Quick uses Gemini 3 Flash native tools.
✅ Fallback: Smart unavailable → Quick.

---

## 13. Discovery & Baseline (2026-01-27)

### 13.1 Infrastructure: Prompt Management
- **Location**: System prompts (kernels) are stored in Firestore as "Facts" under the `SYSTEM` owner.
- **Components**:
    - `kernel`: Full prompt for complex agents (Smart).
    - `kernel_light`: Lightweight prompt for fast agents (Quick).
- **Tooling**:
    - `scripts/memory/ops/download_component.py`: Used to pull prompts from Firestore to local `.groovy` files.
    - `scripts/memory/ops/upload_kernel.py`: Used to upload modified prompts back to Firestore with version tracking.
- **Finding**: Discovery in this session revealed that `kernel_light` in dev was outdated and duplicated much of the `kernel` logic, including manual tool protocols that conflict with native provider tools.

### 13.2 Real Prompt Analysis & Baseline
- **Current State**: Both `QuickResponseAgent` and `SmartResponseAgent` currently use manual reasoning chains for tool calls.
- **Quick Agent Baseline**: `kernel_light.groovy` (v1.3) contains a `Tool_Usage_Protocol` rule that manually instructs the model to call `search_memory` or `ask_web_search_agent` via `AgentCoordinator`.
- **Finding**: Actual prompts in Firestore dev environment confirmed that both agents are configured as custom tool executors, creating high latency for simple queries.

### 13.3 Refined Plan for Quick Agent (M4.1)
- **Source Sync**: `kernel_light` v2.0 is derived from the latest `kernel` (P.9.2) to maintain personality but is stripped of all `protocols` (manual tools).
- **Cognitive Process Enhancement**:
    - A new step `ESCALATION_CHECK` is added to the `cognitive_process` block.
    - This check happens early (Step 4) to ensure fast exit if a query is too complex.
- **Escalation Protocol (English for consistency)**:
    - Defines `trigger_conditions` (deep data analysis, multi-step planning, specialized agents).
    - Instruction: `IF any trigger_condition is met: STOP reasoning -> Generate brief witty response -> Recommend Smart Agent`.
- **Native Transition**: `QuickResponseAgent` will switch to Gemini native tools (grounding) via `automatic_function_calling=True` in the `GeminiAdapter`.

### 13.4 Refined Plan for Smart Agent (M4.2)
- **Custom Orchestration**: Smart Agent remains the manual tool loop orchestrator using specialist agents.
- **Model Migration**: Migrating from Gemini Pro to Claude Sonnet for complex reasoning turns.
- **Prompt Caching**: Implementation of `PromptCacheConfig` to optimize costs and latency for biographical context.

---

## 14. Detailed Implementation Workflow (2026-01-27)

### Step 1: Port Extensions (Hexagonal Foundation)
1.  **File**: `src/ports/llm_service.py`
2.  **Add**: `AutomaticFunctionCallingConfig` (enabled, mode).
3.  **Update**: `generate_content` signature to accept this config.

### Step 2: Adapter Updates (Native Grounding)
1.  **File**: `src/adapters/gemini_adapter.py`
2.  **Action**: Support `AutomaticFunctionCallingConfig`. If enabled, set `automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=False)` in Google AI SDK.
3.  **Action**: Ensure grounding tool is properly initialized when requested.

### Step 3: Prompt Migration (Cloud Ops)
1.  **Action**: Use `memory/kernel_light_v2_final_proposal.groovy` (approved).
2.  **Command**: `python scripts/memory/ops/upload_kernel.py --file memory/kernel_light_v2_final_proposal.groovy --component kernel_light --environment dev`.
3.  **Verification**: Pull it back and verify the `version: "L.2.0 (Native Tools Integration)"`.

### Step 4: QuickResponseAgent Refactoring
1.  **File**: `src/agents/core/quick_response_agent.py`
2.  **Remove**: Custom tool loop logic (`_get_tool_declarations`, `_handle_tool_calls`, etc.).
3.  **Enable**: Native tools in `execute()` call.
4.  **Update**: Message history handling to be compatible with native grounding responses.

### Step 5: Claude Adapter Implementation
1.  **File**: `src/adapters/claude_adapter.py`
2.  **Feature**: Prompt caching support via breakpoints.
3.  **Integration**: Map usage metadata and cache hits.

---

**Next Step:** Execute Phase 1 (Port Extension).

---

## Changelog

### 2026-01-27
- Added SearchEnrichmentService as Router-owned enrichment pipeline.
- Updated Phase 2 plan to include EnrichedContext models and service DI wiring.