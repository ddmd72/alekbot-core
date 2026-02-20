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

The system uses 3 input phrases (extracted by the Router) and maps them to specialized vector fields in Firestore. An optional domain channel fires first when `relevant_domains` is provided.

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
        return False  # NOT duplicate (83 kg ≠ 84 kg)

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
"Weight 83 kg" vs "Weight 84 kg"
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
2. "Honda has Allshift gearbox" (fact_id=def)
3. "Car in Puzol, Spain" (fact_id=ghi)

Agent decision: MERGE all 3 → "Honda Civic 2015 with Allshift gearbox in Puzol, Spain"
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
    skip_semantic_dedup: bool = False
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
    source: str  # "keyword_tags", "phrase1_text", etc.
    relevance_score: Optional[float]  # Can be None
    vector: Optional[List[float]]  # Included for dedup!
```

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

**Typical Performance:**

- Up to 7 parallel queries: ~150-250ms (Firestore)
- RRF ranking: ~5-10ms (in-memory)
- Semantic dedup: ~20-50ms (in-memory, no Firestore reads)
- **Total:** ~200-300ms

### 10.2 Cost

**Firestore Reads:**

- Up to 7 queries × avg 10-15 results = 70-105 reads per search
- Deduplication: 0 additional reads (vectors in memory)

**Embedding API Calls:**

- Up to 3 embeddings per search (keywords, phrase1, phrase2); domain channel requires none
- Embeddings generated in parallel

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

---

**Last Updated:** 2026-02-18
**Status:** ✅ Production Ready
