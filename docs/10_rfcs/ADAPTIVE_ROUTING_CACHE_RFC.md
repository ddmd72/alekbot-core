# RFC: Adaptive Routing & Cache Strategy (Gemini + Claude Hybrid)

**Status:** Partial (Routing + Enrichment implemented; Cache §8 superseded by HEXAGONAL_PROMPT_CACHING_RFC)
**Date:** 2026-01-27
**Updated:** 2026-02-28
**Owner:** AI Engineering
**Scope:** RouterAgent, QuickResponseAgent, SmartResponseAgent, WebSearchAgent
**Goal:** Scalable hybrid architecture with native tools, adaptive caching, and continuity-aware routing.

**Related ADR:** ADR-005 (Router-Centric Enrichment)
**Related Building Block:** Search Enrichment
**Implemented Sections:** 3.2 (Triple Search), 7 (Dedup Strategy), 3.1–3.5 (Router), 4 (Quick), 5 (Smart)
**Superseded Section:** §8 (Adaptive Cache Strategy) → see HEXAGONAL_PROMPT_CACHING_RFC.md

---

## 1. Problem Statement

We need a durable, scalable architecture that:

1. Uses **Gemini native tools** for fast responses (Quick path).
2. Uses **Claude Sonnet** for deeper reasoning and custom tools (Smart path).
3. Preserves personalization using **memory search + lenses + tone**.
4. Avoids tool conflicts (native tools vs custom tools).
5. Maintains continuity: if user is in a “smart session”, Router should keep routing to Smart.
6. Minimizes cost for episodic users with **adaptive caching**.

---

## 2. Proposed Architecture (Final)

```
RouterAgent (Gemini Flash)
  ├─ LLM triage: tone, keywords, search phrase, complexity
  ├─ Memory search: keyword + phrase_1 + phrase_2 (weighted merge + dedup)
  ├─ Anthropic cache: cache keyword_context (TTL 5m)
  └─ Route decision: quick vs smart (continuity-aware)

QuickResponseAgent (Gemini Flash)
  ├─ Native Gemini tools enabled
  ├─ Inject keyword_context + phrase_context
  └─ No custom tool declarations

SmartResponseAgent (Claude Sonnet 4.5)
  ├─ Uses router cache (keyword_context)
  ├─ Injects phrase_context inline
  ├─ Custom tools orchestration (manual loop)
  └─ Complex + sensitive requests

WebSearchAgent (Gemini Flash)
  ├─ Native Google Search tool
  └─ Returns raw results (Smart formats)
```

---

## 3. RouterAgent Responsibilities (Gemini Flash)

### 3.1 LLM Output Fields

Router LLM produces:

```
tone
keywords
search_phrase
complexity
escalation_flags
topic_similarity (if previous agent = smart)
```

### 3.2 Memory Search (Triple Pass + Dedup)

The Router executes **three parallel semantic searches** and merges them into one block:

1) **keyword** (narrow, precise)
2) **phrase_1** (broad semantic context)
3) **phrase_2** (secondary semantic angle)

These are merged with weighting and then deduplicated.

**Weighted merge (current implementation):**
- keyword: 10 facts
- phrase_1: 15 facts
- phrase_2: 10 facts
- total_limit: 30

**Dedup rules:**
- Remove duplicate fact IDs across the merged list
- Also remove any facts already present in biographical_context (to avoid repetition)

### 3.3 Cache Creation (Anthropic, TTL 5m)

Router creates an Anthropic prompt cache with **keyword_context only**.

Rationale:
- Keyword context is compact, stable for short sessions
- Phrase context stays inline (mutable, larger)
- TTL 5m fits burst sessions

### 3.4 Routing Decision (Continuity-Aware)

Add continuity logic to prevent “Smart → Quick” topic drops.

Rules:

1. **If previous agent == smart** and **topic_similarity ≥ 0.7** → Smart
2. **If previous agent == smart** and **complexity ≥ 3** → Smart
3. **If escalation_flags present** → Smart
4. **Else if complexity ≥ 5** → Smart
5. **Else** → Quick

### 3.5 Escalation Flags

Router escalates to Smart if:
- User asks for more detail (“more detail”, “elaborate”)
- Tone = panic / medical / grief / suicidal
- Multi-step reasoning required

---

## 4. QuickResponseAgent (Gemini Flash)

### 4.1 Input Context & Injection

- Receives Router **enriched_context** (merged facts list)
- Does NOT perform extra memory search
- Builds **one unified knowledge_base block**:
  - `biographical_context`
  - `enriched_context` (router merged block)
  - Both are injected inside the **same Groovy class** (not appended outside)

### 4.2 Prompt Constraints

- **No custom tool declarations**
- Avoid mentioning capabilities it doesn’t have

### 4.3 Native Tools

- Google Search grounding
- Code execution (optional)

### 4.4 Target Distribution

Quick handles 50–80% of total requests.

---

## 5. SmartResponseAgent (Claude Sonnet 4.5)

### 5.1 Input Context & Injection

- Uses **Anthropic cache** (keyword_context only)
- Receives Router **enriched_context** (merged facts list)
- Builds **one unified knowledge_base block**:
  - `biographical_context`
  - `enriched_context` (router merged block)
  - Both are injected inside the **same Groovy class** (not appended outside)

### 5.2 Tools

- Custom tool orchestration (manual loop)
- Delegates web search to WebSearchAgent
- Optional future tools: image, code, reasoning, etc.

### 5.3 Reasoning Focus

Smart handles complex/sensitive tasks, using deeper reasoning and richer context.

---

## 6. WebSearchAgent (Gemini Flash)

### 6.1 Simplified Behavior

- Uses native Google Search tool
- Returns raw results
- Smart agent formats results

---

## 7. Context Deduplication Strategy

**Goal:** Avoid duplicated facts that dilute attention and waste tokens.

Algorithm (current implementation):

```
keyword_context = top 10 facts by keyword search
phrase_1_context = top 15 facts by semantic search
phrase_2_context = top 10 facts by semantic search

merged = keyword_context + phrase_1_context + phrase_2_context
deduped = remove duplicate fact IDs
final = remove any facts already present in biographical_context
```

Expected token sizes:
- Keyword context: ~0.5K–1K tokens
- Phrase contexts (combined): ~1–2K tokens after dedup
- Total enriched block: ~2–3K tokens

---

## 8. Cache Strategy (Superseded)

> **This section is superseded by [HEXAGONAL_PROMPT_CACHING_RFC.md](./HEXAGONAL_PROMPT_CACHING_RFC.md).**
>
> Summary of actual implementation:
> - Caching is applied transparently via `CachingLLMProxy` wrapping `LLMService`.
> - `PromptCacheStrategy` maps agent_type → cache config based on provider capabilities.
> - Claude (Anthropic) caches the static system prompt prefix; the dynamic suffix (datetime + Q-S context)
>   is split off by `PROMPT_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"` in `PromptAssemblyService`.
> - Gemini does not support API-level prompt caching; Router and WebSearch are not cached.
> - No burst-session detection gate. Cache applied on every eligible Claude request (provider decides TTL = 5 min ephemeral).

---

## 9. Data Model Updates

### 9.1 RoutingMetadata (additions)

```
previous_agent: "quick" | "smart" | null
topic_similarity: float
messages_since_smart: int
```

### 9.2 EnrichedContext (Current Code Shape)

```
facts: List[EnrichedFact]  # merged + deduped list
total_sources: int
dedup_count: int
biographical_dedup_count: int
```

```
EnrichedFact:
  fact_id: str
  content: str
  source: "keyword" | "phrase_1" | "phrase_2"
  relevance_score: Optional[float]
```

**Note:** The Router passes `enriched_context` in `message.context` to downstream agents. Agents extract `facts[*].content` and inject the combined block into the Groovy `knowledge_base`.

---

## 10. Prompt Assembly (Groovy Knowledge Base Injection)

### 10.1 Single Unified Knowledge Base

Quick and Smart agents must construct **one unified knowledge base block**:

```
knowledge_base {
  biographical_context: '''
  [biographical facts]

  // ENRICHED CONTEXT (Router merged block)
  - [fact 1]
  - [fact 2]
  '''
}
```

### 10.2 Hard Rule: No External Appends

The enriched context **MUST NOT** be appended outside the Groovy class.
It must be injected into the class `knowledge_base` section at build time.

### 10.3 Kernel Placeholder Requirement

`kernel_light` and `kernel_full` must contain a placeholder knowledge_base block to support runtime injection:

```
knowledge_base {
  biographical_context: '''
  // Runtime injection placeholder
  '''
}
```

### 10.4 Caching Implications

- Cache only static content: kernel + biographical context
- Inject enriched_context dynamically per request
- Do **not** cache final prompt if it includes router-enriched facts

### 10.5 Test Emulation Requirements

Unit tests must emulate enriched context injection by:

- Loading a dev user id from `.env` (DEV_USER_ID)
- Building a system prompt with a synthetic enriched context block
- Verifying:
  - The merged `knowledge_base` contains both biographical and enriched facts
  - The enriched block appears **inside** the Groovy class
  - No external `// SEMANTIC CONTEXT` append exists

### 10.6 User-Level Prompt Overrides (Custom Kernels)

The platform supports per-user prompt overrides via `UserBotConfig`:

- `custom_kernel_id`
- `custom_kernel_light_id`
- `custom_examples_id`

These are loaded by `UserPromptBuilder` **before** default SYSTEM components.

**Hard requirement:** any custom kernel or kernel_light MUST include the runtime injection placeholder:

```
knowledge_base {
  biographical_context: '''
  // Runtime injection placeholder
  '''
}
```

Without this placeholder, enriched context injection will silently fail. Custom kernels must be audited and aligned to the default structure.

### 10.7 Smart Provider Selection (Resolved)

> **Status: Resolved.** Provider selection is now driven by `AgentContextBuilder` and `AgentProviderStrategy`
> which map `agent_type → PerformanceTier → provider`. `UserBotConfig.agent_tiers` allows per-user overrides.
> The `startswith("gemini")` heuristic bug described here was eliminated when `AgentExecutionContext` was introduced.

---

## 11. Migration Plan

### Phase 1: Router Enhancements
1. Add output fields: search_phrase, escalation_flags, topic_similarity
2. Implement two-pass memory search + dedup
3. Add session continuity logic

### Phase 2: Quick Agent Native Tools
1. Enable native tools in Quick
2. Ensure prompt excludes custom tool hints
3. Inject both contexts

### Phase 3: Smart Agent Cache Integration
1. Use Anthropic cache for keyword_context
2. Inject phrase_context inline
3. Keep custom tool loop

### Phase 4: WebSearch Simplification
1. Keep native Google search only
2. Return raw results

---

## 12. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Smart → Quick drop | Context loss | topic_similarity + continuity rules |
| Duplicate context | Token waste | dedup strategy |
| Cache churn | Cost spikes | adaptive TTL + burst gating |
| Tool conflicts | Failures | isolate native tools to Quick/WebSearch |

---

## 13. Open Questions

1. What similarity threshold is correct for continuity? (default 0.7)
2. Should Router run LLM for topic similarity or use embedding-based cosine similarity?
3. Should Smart agent ever run without cache when cache exists? (fallback rules)

---

## 14. Decision Summary

✅ Gemini Flash Router for triage & enrichment
✅ Gemini Flash Quick for most requests (native tools)
✅ Claude Sonnet Smart for deep reasoning & custom tools
✅ Anthropic 5m cache for keyword_context
✅ Continuity-aware routing rules

---

**Next Step:** Review, adjust parameters, and greenlight for implementation.