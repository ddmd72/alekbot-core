# Search Enrichment (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the multi-vector semantic search system that retrieves and ranks relevant knowledge for agent context.

### When to Read

- **For AI Agents:** Before modifying search algorithms, RRF ranking, or deduplication logic.
- **For Developers:** When troubleshooting low search relevance, high latency, or duplicate facts in context.

### When to Update

This document MUST be updated when:

- [ ] The multi-query strategy (number of phrases, fields, or channels) changes.
- [ ] The domain-based channel (`relevant_domains`) logic changes.
- [ ] The RRF (Reciprocal Rank Fusion) algorithm or constant `k` is modified.
- [ ] The semantic deduplication logic is updated.
- [ ] New vector fields (e.g., `tags_vector`, `metadata_vector`) are added to Firestore.
- [ ] Search limits resolution logic changes.
- [ ] Deduplication thresholds or modes are modified.

### Cross-References

- **Hybrid Router:** [../hybrid_router/README.md](../hybrid_router/README.md)
- **Multi-Vector RRF Search Guide:** [../../08_concepts/multi_vector_rrf_search.md](../../08_concepts/multi_vector_rrf_search.md)
- **Biographical Context Cache:** [../biographical_context_cache/README.md](../biographical_context_cache/README.md)
- **Deliberate Fact Management RFC:** [../../10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md](../../10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md)

---

## 1. Overview

The **Search Enrichment** system is responsible for retrieving the most relevant facts from the user's long-term memory. It uses a sophisticated multi-vector strategy and advanced ranking algorithms to ensure that agents have the precise context needed to answer complex queries.

**Core Principle:** High recall via multiple specialized queries, high precision via RRF ranking and smart deduplication.

---

## 2. Multi-Vector Strategy

To capture different aspects of relevance, the system executes **up to 7 parallel queries** per request: 1 domain channel (optional) + 6 vector channels.

### 2.1 Query Matrix

The system supports up to 7 channels. Two callers use different subsets:

- **RouterAgent:** 6 channels (`search_phrase_2=""` — alternative phrase channel skipped). Rationale: router is a quick first-pass enrichment, not deep retrieval.
- **MemorySearchAgent:** All 7 channels (full retrieval, LLM formulates `alternative_query` via its own cognitive process prompt).

The system uses 3 input phrases and maps them to specialized vector fields in Firestore. An optional domain channel fires when `relevant_domains` is provided.

| Channel | Input | Vector Field | Rationale |
| ------- | ----- | ------------ | --------- |
| **Domain** (optional) | `relevant_domains` | N/A (direct Firestore IN query) | Precise: returns ALL current facts in specified domains. |
| **Keywords** | `keywords` | `tags_vector` | Best for category and entity matching. |
| **Keywords** | `keywords` | `metadata_vector` | Structured data matching at 75% limit. |
| **Phrase 1** | `search_phrase_1` | `vector` (text) | Natural language matching. |
| **Phrase 1** | `search_phrase_1` | `tags_vector` | Domain tag matching at 75% limit. |
| **Phrase 2** | `search_phrase_2` | `vector` (text) | Natural language matching at 75% limit. |
| **Phrase 2** | `search_phrase_2` | `metadata_vector` | Structured data matching. |

### 2.2 Adaptive Routing

- **Domain channel:** Direct Firestore IN query on `domain` field — not vector-based. Returns all current facts in the listed domains at relevance score 1.0.
- **Keywords:** Prioritize `tags_vector` (domain concepts) with 100% limit, `metadata_vector` at 75%.
- **Phrase 1:** Prioritize main `vector` (semantic meaning) at 100%, `tags_vector` at 75%.
- **Phrase 2:** Balanced — `vector` at 75%, `metadata_vector` at 100%.

**Rationale:** Different query types need different representations. Keywords work best with compressed domain tags, while natural language phrases need full text context. Domain-based lookup is precise and fast for known domain categories.

---

## 3. Ranking & Fusion (RRF)

Results from all active queries are merged using **Reciprocal Rank Fusion (RRF)**, an industry-standard algorithm for combining multiple ranked lists.

### 3.1 Algorithm

The RRF score for each fact is calculated as:

```
Score = Σ 1 / (k + rank_i)
```

Where:

- **k:** Constant (default 60) to mitigate the impact of low-ranked results
- **rank_i:** The position of the fact in the i-th query result list

### 3.2 Benefits

- **No Score Normalization:** Works across different vector distances without complex scaling
- **Consensus Reward:** Facts appearing in multiple lists (even at lower ranks) are boosted
- **Robustness:** Prevents a single "lucky" match from dominating the results
- **Query-Independent:** Ranks are always comparable, unlike similarity scores

### 3.3 ID Deduplication

RRF automatically removes ID duplicates (same fact appearing in multiple queries). Only unique `fact_id` values proceed to semantic deduplication.

---

## 4. Smart Deduplication (2026-02-08, 2026-02-16)

After ranking, the system applies a **5-level semantic deduplication** algorithm to ensure the context window is not wasted on redundant information.

**Philosophy:** "Better to add a duplicate than to lose important information"

### 4.1 Algorithm Levels

```python
def is_duplicate(new_text, existing_text, similarity):
    # 1️⃣ Quick exit for dissimilar
    if similarity < 0.96:
        return False  # NOT duplicate

    # 2️⃣ Number comparison (highest priority)
    if numbers_differ(new_text, existing_text):
        return False  # NOT duplicate (75 kg ≠ 84 kg)

    # 3️⃣ Very high similarity
    if similarity >= 0.98:  # Configurable threshold!
        return True  # DUPLICATE

    # 4️⃣ Length-based heuristic
    if existing_length < 0.85 * new_length:
        return False  # NOT duplicate (new has more detail)

    # 5️⃣ Default
    return True  # DUPLICATE (moderate similarity + similar length)
```

### 4.2 Configurable Thresholds (2026-02-16)

The `strict_threshold` (level 3) is now configurable for different use cases:

```python
# Normal mode (READ/WRITE)
dedup_service = SmartDeduplicationService(
    moderate_threshold=0.96,  # Fixed
    strict_threshold=0.98     # Balanced filtering
)

# Consolidation mode (MERGE decisions)
dedup_service = SmartDeduplicationService(
    moderate_threshold=0.96,
    strict_threshold=1.0      # Only exact duplicates
)
```

### 4.3 Skip Semantic Dedup Mode

For special cases (e.g., ConsolidationAgent searching for MERGE candidates), semantic deduplication can be skipped entirely:

```python
enriched = await search_enrichment.enrich_context(
    keywords=["car", "honda"],
    search_phrase_1="User car details",
    skip_semantic_dedup=True  # Keep ALL facts with different IDs
)
```

**When to use:**

- ✅ ConsolidationAgent: Needs to see ALL similar facts for MERGE decisions
- ❌ Normal search: Would return many duplicates, confusing users

### 4.4 Number-Aware Comparison

**Critical Feature:** Small numeric differences = different facts!

```python
"Weight 75 kg" vs "Weight 84 kg"
similarity = 0.97  # Very high!

Old logic: DUPLICATE ❌
Smart logic: NOT duplicate (numbers differ: [83.0] != [84.0]) ✅
```

**Handles:**

- Time series: `[83.0]` (Feb 5) vs `[84.0]` (Feb 16)
- Measurements: `[185.0]` cm vs `[186.0]` cm
- Dates: `[2025.0, 3.0, 28.0]` vs `[2025.0, 3.0, 29.0]`
- Ranges: `[95.0, 98.0]` kg vs `[98.0, 100.0]` kg

Numbers are extracted and sorted, allowing order-independent comparison.

### 4.5 Performance Optimization

`EnrichedFact` objects include their embeddings, so the deduplication service calculates similarity **in-memory without additional Firestore reads**.

```python
# No Firestore reads - vectors already in memory
for enriched_fact in facts:
    similarity = cosine_similarity(enriched_fact.vector, other.vector)  # ⚡ In-memory
```

---

## 5. Operation Modes

### 5.1 Normal Mode (READ)

```python
enriched = await search_enrichment.enrich_context(
    keywords=["car", "details"],
    search_phrase_1="Tell me about my car",
    search_phrase_2="Vehicle information",
    limits=SearchLimits(total_limit=30),
    dedup_threshold=0.98,        # Balanced filtering
    skip_semantic_dedup=False    # Remove duplicates
)
```

**Purpose:** User-facing search — clean, deduplicated results.

**Result:** ~25-30 unique, relevant facts (after deduplication).

### 5.2 Domain-Targeted Mode

```python
enriched = await search_enrichment.enrich_context(
    keywords=["health", "weight"],
    search_phrase_1="User health and weight tracking",
    search_phrase_2="Physical measurements",
    relevant_domains=["health", "possession"],  # Direct domain fetch
    limits=SearchLimits(total_limit=30)
)
```

**Purpose:** When the Router identifies high-confidence domain categories. Adds a direct Firestore query for all current facts in those domains on top of vector search.

**Result:** More precise recall for known-domain queries.

### 5.3 Consolidation Mode (MERGE)

```python
enriched = await search_enrichment.enrich_context(
    keywords=["car", "honda"],
    search_phrase_1="User car honda details",
    search_phrase_2="Honda vehicle information",
    limits=SearchLimits(total_limit=20),
    dedup_threshold=1.0,         # Only exact duplicates
    skip_semantic_dedup=True     # Keep ALL different IDs
)
```

**Purpose:** ConsolidationAgent searching for MERGE candidates

**Result:** ~15-20 facts, including semantic duplicates with different IDs

**Why needed:** Agent needs to see ALL similar facts to make informed MERGE decisions:

```
Found facts:
1. "Honda Civic 2015" (fact_id=abc)
2. "Honda has automatic gearbox" (fact_id=def)
3. "Car in Example City" (fact_id=ghi)

Agent decision: MERGE all 3 → "Honda Civic 2015 with automatic gearbox in Example City"
```

Without consolidation mode, search would return only fact #1, losing #2 and #3.

### 5.4 Write Mode (Deduplication)

```python
# Used by FactWriteService before storing new facts
dedup_service = SmartDeduplicationService(
    moderate_threshold=0.96,
    strict_threshold=0.98  # Same as READ mode
)

for new_fact in candidate_facts:
    for existing_fact in database:
        is_dup, reason = dedup_service.is_duplicate(
            new_fact.text,
            existing_fact.text,
            similarity
        )
        if is_dup:
            reject(new_fact, reason)
```

**Purpose:** Prevent duplicate storage

**Consistency:** Uses SAME logic as READ mode (unified!)

---

## 6. Configuration & Limits

### 6.1 SearchLimits Value Object

```python
@dataclass
class SearchLimits:
    keyword_limit: int = 10       # Per keywords query
    phrase_one_limit: int = 15    # Per phrase 1 query
    phrase_two_limit: int = 15    # Per phrase 2 query
    total_limit: int = 30         # Final result limit
```

### 6.2 Override Mechanism

```python
# Use custom limits
custom_limits = SearchLimits(
    keyword_limit=20,
    phrase_one_limit=25,
    phrase_two_limit=25,
    total_limit=50
)

enriched = await search_enrichment.enrich_context(
    keywords=keywords,
    search_phrase_1=phrase1,
    search_phrase_2=phrase2,
    limits=custom_limits  # Override defaults
)
```

### 6.3 Default Fallback

If `limits=None`, service uses constructor defaults:

```python
def __init__(self, ..., keyword_limit=10, phrase_one_limit=15, ...):
    self._keyword_limit = keyword_limit
    # ...
```

---

## 7. API Reference

### 7.1 Main Method

```python
async def enrich_context(
    self,
    keywords: List[str],
    search_phrase_1: str,
    search_phrase_2: str,
    relevant_domains: Optional[List[str]] = None,
    biographical_facts: Optional[List[Union[FactEntity, Dict]]] = None,
    limits: Optional[SearchLimits] = None,
    dedup_threshold: float = 0.98,
    skip_semantic_dedup: bool = False,
    sequential: bool = False,
) -> EnrichedContext
```

**Parameters:**

- `keywords`: Domain keywords from Router semantic lens
- `search_phrase_1`: Primary natural language phrase
- `search_phrase_2`: Secondary/alternative phrase
- `relevant_domains`: Optional list of 1-3 domain values for direct Firestore query. Returns ALL current facts in those domains. Uses existing `(account_id, domain, created_at)` index.
- `biographical_facts`: Facts already present in the biographical baseline (ID-based). Matched facts are removed from results to avoid duplication in context.
- `limits`: Optional limit overrides via `SearchLimits`
- `dedup_threshold`: Similarity threshold for semantic dedup (0.96-1.0)
  - `0.98`: Default (balanced filtering)
  - `1.0`: Only exact duplicates (consolidation mode)
- `skip_semantic_dedup`: Skip semantic deduplication entirely
  - `False`: Normal search (remove semantic duplicates)
  - `True`: Consolidation mode (keep ALL facts with different IDs)
- `sequential`: Execute Firestore vector queries one-by-one instead of `asyncio.gather`
  - `False` (default): Parallel — optimal for all paths; 6 parallel streams complete in 700ms–1.2s with full CPU
  - `True`: Sequential — reduces peak concurrency; use only if Firestore quota pressure is observed

**Returns:**

```python
@dataclass
class EnrichedContext:
    facts: List[EnrichedFact]
    total_sources: int               # Number of active query channels
    dedup_count: int                 # Semantic duplicates removed
    biographical_dedup_count: int    # Facts removed (already in biographical baseline)
```

### 7.2 EnrichedFact DTO

```python
@dataclass
class EnrichedFact:
    fact_id: str
    content: str
    source: str                      # "keyword_tags", "phrase1_text", etc.
    relevance_score: Optional[float] # Can be None
    vector: Optional[List[float]]    # Included for dedup!
    # Taxonomy fields — populated by SearchEnrichmentService directly from Firestore.
    # Eliminates the need for a secondary get_facts_by_ids batch fetch in callers.
    fact_type: Optional[str]         # e.g. "FACT", "ANCHOR", "EVENT"
    domain: Optional[str]            # e.g. "health", "possession", "professional"
    temporal_class: Optional[str]    # e.g. "PERMANENT", "PERIODIC", "POINT_IN_TIME"
    state: Optional[str]             # e.g. "current", "outdated"
    context_priority: Optional[str]  # e.g. "HIGH", "MEDIUM", "LOW"
    tags: Optional[List[str]]
    metadata: Optional[Dict]
    reported_date: Optional[str]     # ISO-8601 string, e.g. "2026-02-24"
    version: Optional[int]
```

**Design note:** Taxonomy fields are populated in both the domain-channel path and the vector-search path, so all callers receive a fully populated DTO regardless of which search channel matched. Fields are `None` when the underlying Firestore document lacks the field.

---

## 8. Code References

- **Main Service:** `src/services/search_enrichment_service.py`
- **Deduplication:** `src/services/deduplication_service.py`
- **Domain Models:** `src/domain/search.py` (`EnrichedFact`, `EnrichedContext`, `SearchLimits`)
- **Vector Math:** `src/domain/vector_math.py` (`cosine_similarity`)
- **Fact Management Adapter:** `src/adapters/firestore_fact_management_adapter.py` (consolidation mode usage)

---

## 9. Testing

### 9.1 Unit Tests

```bash
pytest tests/unit/services/test_search_enrichment_service.py
pytest tests/unit/services/test_deduplication_service.py
```

### 9.2 Integration Tests

```bash
pytest tests/integration/test_search_enrichment_integration.py
```

### 9.3 Key Test Cases

- ✅ RRF ranking with multiple queries
- ✅ Smart deduplication with number differences
- ✅ Consolidation mode (skip_semantic_dedup=True)
- ✅ Custom limits override
- ✅ Configurable thresholds (0.98 vs 1.0)
- ✅ Vector inclusion in EnrichedFact
- ✅ Domain channel (relevant_domains) fires as additional channel

---

## 10. Performance Characteristics

### 10.1 Latency

**Isolated execution (single caller, no concurrent find_nearest from other paths):**

| Step | Time |
|------|------|
| Batch embedding (up to 3 vectors) | ~100-200ms |
| Up to 6 parallel `find_nearest` (Firestore KNN) | ~300-500ms |
| RRF ranking | ~5-10ms (in-memory) |
| Semantic dedup | ~20-50ms (in-memory) |
| **Total** | **~450-750ms** |

**Previously observed degradation (resolved 2026-02-24):**

When consolidation ran via `asyncio.create_task()`, Cloud Run throttled CPU to ~5% immediately after
returning 200. With ~5% CPU, grpc.aio callbacks for Firestore `find_nearest` were starved — causing
74–180s latency. This had nothing to do with the number of concurrent streams or Firestore KNN backend
limits. The fix was architectural: consolidation now runs inside its own Cloud Tasks HTTP request, which
keeps Cloud Run at full CPU. Under full CPU, latency with 6 parallel streams is 700ms–1.2s.

**No gRPC errors are raised.** The Firestore SDK silently waits for grpc.aio callback delivery — which
only happens when the event loop has CPU to process it. Confirmed via diagnostic logging of `grpc`,
`grpc.aio`, and `google` namespaces — zero retries or exceptions observed.

### 10.2 Concurrency Control (`_FIND_NEAREST_SEMAPHORE`)

A module-level semaphore in `src/adapters/firestore_repo.py` caps the number of simultaneous `find_nearest` calls across **all callers in the process**:

```python
# src/adapters/firestore_repo.py
_FIND_NEAREST_SEMAPHORE = asyncio.Semaphore(30)
```

**Why this exists:** Quota guard against Firestore RESOURCE_EXHAUSTED errors under very high concurrency
(e.g., many simultaneous Cloud Tasks workers hitting the same process). Not a latency fix — with full CPU
allocation (see §10.1), parallel find_nearest calls complete in 700ms–1.2s regardless.

**Current value: 30.**

**Interaction between callers:**

The semaphore is shared by all code paths in the same process:
- `SearchEnrichmentService.enrich_context()` — used by RouterAgent and MemorySearchAgent (up to 6 streams per call)
- `FactManagementAdapter.search_existing_facts()` — used by ConsolidationAgent (up to 6 streams per call)
- Any `AgentWorkerHandler` task that triggers a search agent

### 10.3 Cost

**Firestore Reads:**

- Up to 7 queries × avg 10-15 results = 70-105 reads per search
- Deduplication: 0 additional reads (vectors in memory)

**Embedding API Calls:**

- Up to 3 embeddings per search (keywords, phrase1, phrase2); domain channel requires none
- Batch API call: `batchEmbedContents` sends all non-empty texts in a single HTTP request

---

## 11. Status

**Status:** ✅ Production Ready

---

## 12. Troubleshooting

### 12.1 Low Relevance

**Symptom:** Search returns irrelevant facts

**Causes:**

- Keywords too broad → Refine semantic lens extraction
- Missing vector field → Check Firestore indexes
- RRF k too high → Lower k to emphasize top ranks

**Fix:** Review Router semantic lens quality, adjust k constant

### 12.2 Too Many Duplicates

**Symptom:** Similar facts in results

**Causes:**

- `skip_semantic_dedup=True` (consolidation mode)
- `dedup_threshold` too high (e.g., 1.0)

**Fix:**

- Use `skip_semantic_dedup=False` for normal search
- Lower `dedup_threshold` to 0.98 or 0.96

### 12.3 Missing Important Facts

**Symptom:** Fact exists but not returned

**Causes:**

- Deduplication too aggressive
- Numbers extracted incorrectly
- Limit too low

**Fix:**

- Check dedup logs for "numbers_differ" reason
- Increase `total_limit` in SearchLimits
- Review numeric extraction regex

### 12.4 find_nearest Takes 74–180s

**Symptom:** `[find_nearest] DONE ... elapsed=74000ms` (or more) in logs. No errors, no retries.
Typically occurs only during consolidation, not during router/user-facing requests.

**Cause:** Cloud Run CPU throttling. Cloud Run reduces CPU to ~5% when no HTTP request is actively
being processed. If consolidation is launched via `asyncio.create_task()`, the worker returns 200
immediately, ending the HTTP request. grpc.aio callbacks (Firestore AsyncClient) are only delivered
when the asyncio event loop has CPU — at 5% CPU, this can take 74–180s.

**Confirmation:** Send a user message while consolidation is stuck → `find_nearest` unblocks instantly
(new HTTP request restores full CPU).

**Fix (already applied):** Consolidation runs inside its own Cloud Tasks HTTP request (`task_type="consolidation"`
in `main.py:/worker`). The request stays alive throughout consolidation → full CPU. See
`src/handlers/consolidation_handler.py` and `main.py:overflow_callback`.

**If this regresses:** Check that `overflow_callback` in `main.py` is calling
`agent_task_queue.enqueue_consolidation_task()` and NOT `asyncio.create_task(process_user_batches_on_overflow(...))`.

**Note on `[_cygrpc] Loaded running loop` messages:** This is the gRPC C extension registering itself
with the asyncio event loop. It appears frequently under CPU pressure. It is not an error and does
not indicate a connection problem — it is a symptom of CPU throttling.

---

**Last Updated:** 2026-02-24
**Status:** ✅ Production Ready
