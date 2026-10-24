# Multi-Vector RRF Search: Philosophy & Ideology

## 📖 Overview

This concept guide explains the **philosophy and ideology** behind Multi-Vector Semantic Search with RRF Ranking. While the [Building Block](../05_building_blocks/search_enrichment/README.md) describes "how it works", this document explains "**why it works**" and "**when to use it**".

**Key Insight:** Different representations of the same query → better retrieval than single representation.

---

## 🎯 The Core Philosophy: Query-Independent Ranking

### The Problem with Similarity Scores

**Traditional approach:**

```
Query → Single vector → Vector search → Sort by similarity score
```

**Problem:** Similarity scores are **query-dependent**

```python
Query 1: "car" → Fact A similarity = 0.79
Query 2: "vehicle" → Fact A similarity = 0.66

# Question: Is Query 1 Fact A better than Query 2 Fact A?
# Answer: We don't know! Scores are not comparable.
```

**Why scores aren't comparable:**

- Different queries → different embedding distributions
- similarity=0.8 from one query ≠ similarity=0.8 from another
- Can't merge results from multiple queries reliably

### RRF Solution: Ranks Are Always Comparable

**RRF Insight:** Instead of scores, use **ranks**

```python
Query 1: "car" → Fact A (rank=1), Fact B (rank=2)
Query 2: "vehicle" → Fact B (rank=1), Fact A (rank=3)

# Rank=1 always means "best match in this query"
# Ranks are comparable across ALL queries
```

**RRF Formula:**

```
RRF_score(Fact) = Σ 1/(k + rank_i)

Fact B: 1/(60+2) + 1/(60+1) = 0.0325  ← Winner!
Fact A: 1/(60+1) + 1/(60+3) = 0.0323
```

**Result:** Fact B wins because it has better **average rank** (consensus), not because of absolute similarity scores.

---

## 🧠 Ideology: Semantic Lens Evolution

### The Journey: From Single Vector to Multi-Vector

#### Stage 1: Single Text Vector (v1.0)

```
User Query → RouterAgent → Semantic Lens: ["car", "toyota"]
                        ↓
                   Single embedding
                        ↓
                  Vector search (text only)
                        ↓
                  Results (similarity ~0.66)
```

**Problem:** One embedding loses information

- Keywords like "car" best match via tags, not text
- Dates/VINs best match via metadata, not text
- Natural language needs text embedding

#### Stage 2: Multi-Vector with Adaptive Routing (v2.0)

```
Semantic Lens: ["car", "toyota"] → 3 different embeddings:
    1. tags_vector (domain knowledge)
    2. metadata_vector (structured data)
    3. vector (natural language)
```

**Insight:** Different query types need different representations!

### The "Three Lenses" Metaphor

Imagine looking at a fact through 3 different lenses:

1. **Tags Lens** (compressed knowledge)
   - Sees: Domain categories, entity types
   - Best for: "Show me car", "What toyota"
   - Example: Tags [automotive, toyota, corolla] compress knowledge

2. **Metadata Lens** (structured view)
   - Sees: Dates, numbers, IDs, schemas
   - Best for: "VIN JHMFA...", "2015 model"
   - Example: Metadata captures structured facets

3. **Text Lens** (natural language)
   - Sees: Full narrative, context, relationships
   - Best for: "my car is Skoda Fabia 2015"
   - Example: Text preserves complete story

**Multi-Vector = Using all 3 lenses simultaneously**

---

## 📊 Adaptive Routing Manifesto

### Principle: Match Query Type to Vector Field

**The Manifesto:**

> **Not all queries are created equal.**  
> Keywords seek categories.  
> Natural language seeks narratives.  
> Structured queries seek schemas.
>
> **Route queries to their natural representation.**

### Routing Strategy

#### 1. Keywords → Tags Priority

**Rationale:** Domain nouns = compressed knowledge

```
Keywords: ["car", "toyota", "corolla"]

Why tags_vector?
- Tags are compressed representations of domain knowledge
- "car" → [automotive, vehicle, transport]
- One keyword can match many semantic tags
- Precision improves dramatically (0.66 → 0.79)
```

**Example:**

```python
Fact: "I have a Skoda Fabia 2015"
Tags: [automotive, toyota, corolla, personal_asset]

Query: "car"
tags_vector match: 0.79 ✅ (high precision)
text_vector match: 0.66 ❌ (lower precision)
```

#### 2. Natural Language → Text Priority

**Rationale:** Narratives need full context

```
Phrase: "my car details and maintenance history"

Why vector (text)?
- Full sentence carries intent + context
- Word relationships matter ("maintenance history")
- Tags lose narrative structure
```

**Example:**

```python
Fact: "Changed oil in Toyota 01.01.2020, mileage 10000 km"

Query: "oil change history"
text_vector match: 0.75 ✅ (captures narrative)
tags_vector match: 0.68 ❌ (loses context)
```

#### 3. Contextual Queries → Balanced

**Rationale:** Context may reference both narrative and structure

```
Phrase: "vehicle information and documents"

Why vector + metadata_vector?
- "information" is broad (needs text)
- "documents" may be structured (metadata)
- Balance provides comprehensive recall
```

---

## 🏆 Why RRF > Other Approaches?

### Head-to-Head Comparison

#### Approach 1: Max Similarity Score

```python
# Take highest similarity across all queries
final_score = max(score1, score2, score3)
```

**Problems:**

- ❌ Ignores "consensus" (fact in multiple queries)
- ❌ One lucky high score dominates
- ❌ No credit for consistent appearance

**Example:**

```
Fact A: Query1=0.9, Query2=0.0, Query3=0.0 → score=0.9
Fact B: Query1=0.7, Query2=0.7, Query3=0.7 → score=0.7

Max Score chooses A, but B appears in ALL queries!
```

#### Approach 2: Weighted Average

```python
# Average similarities with weights
final_score = (w1*score1 + w2*score2 + w3*score3) / (w1+w2+w3)
```

**Problems:**

- ❌ Scores not comparable (query-dependent)
- ❌ Weights need tuning per query type
- ❌ Can "dilute" strong matches

**Example:**

```
Fact A: Query1=0.9 (text), Query2=0.5 (tags) → avg=0.7
But text similarity=0.9 and tags similarity=0.5 not comparable!
```

#### Approach 3: Sum of Weighted Scores

```python
# Sum all weighted similarities
final_score = w1*score1 + w2*score2 + w3*score3
```

**Problems:**

- ❌ Unbounded (can be >1.0)
- ❌ Still query-dependent
- ❌ Requires normalization

#### Approach 4: RRF (Our Choice) ✅

```python
# Use ranks instead of scores
RRF_score = Σ 1/(k + rank_i)
```

**Advantages:**

- ✅ **Query-independent:** Ranks always comparable
- ✅ **Rewards consensus:** More queries = higher score
- ✅ **No tuning needed:** k=60 universal
- ✅ **Bounded:** Scores naturally normalized
- ✅ **Top-rank bias:** Rank 1 >> Rank 10
- ✅ **Industry proven:** Elasticsearch, Google

**Example:**

```
Fact A: Ranks [1, 5, 10] → RRF = 0.0164 + 0.0154 + 0.0143 = 0.0461
Fact B: Ranks [2, 3, 4] → RRF = 0.0161 + 0.0159 + 0.0156 = 0.0476

B wins: Better average rank (consensus), despite lower best rank
```

---

## 💡 The Genius: Consensus Ranking

### What is "Consensus"?

**Consensus = Fact appears in multiple queries with good ranks**

**Key Insight:** A fact appearing in 3 queries (even with moderate ranks) is often more relevant than a fact with one high rank.

### Consensus Examples

#### Example 1: The Consistent Performer

```
Query 1 (keyword_tags): Fact B rank=2 (similarity 0.75)
Query 2 (phrase1_text): Fact B rank=3 (similarity 0.72)
Query 3 (phrase1_tags): Fact B rank=2 (similarity 0.74)

RRF(Fact B) = 1/62 + 1/63 + 1/62 = 0.0483

Interpretation: Fact B consistently relevant across ALL query types
              → High confidence in relevance
```

#### Example 2: The Lucky Winner

```
Query 1 (keyword_tags): Fact A rank=1 (similarity 0.85)
Query 2 (phrase1_text): Fact A rank=15 (similarity 0.60)
Query 3 (phrase1_tags): Not found

RRF(Fact A) = 1/61 + 1/75 = 0.0277

Interpretation: Fact A excellent for keywords, but poor for phrases
              → Lower confidence, may be false positive
```

**RRF Verdict:** Fact B (0.0483) > Fact A (0.0277)

**Why this is correct:**

- B works across multiple query representations
- A only works for specific keywords
- User intent probably closer to B (consistent match)

### The "Voting" Analogy

Think of each query as a "voter":

- **Max Score:** Only the most enthusiastic voter counts
- **Average:** All voters count equally (even weak votes)
- **RRF:** Strong voters (low ranks) count more, but all votes contribute

```
Fact A: Voter1=💚💚💚💚💚 (rank 1), Voter2=💔 (not found)
Fact B: Voter1=💚💚💚💚 (rank 2), Voter2=💚💚💚💚 (rank 2), Voter3=💚💚💚💚 (rank 2)

RRF chooses B: More voters, consistent support
```

---

## 🎭 Connection to Semantic Lens

### Semantic Lens: The Foundation

**Semantic Lens** (from RouterAgent cognitive process):

```groovy
SEMANTIC_LENS {
    Extract: domain_nouns, specific_entities, time_markers, action_verbs
    Purpose: Compress user intent into searchable keywords
}
```

**Example:**

```
User: "Show me information about my car"
        ↓
Semantic Lens: ["car", "information", "my"]
        ↓
Keywords: ["car", "information", "my"]
```

### Multi-Vector Evolution

**Old thinking:**

```
Semantic Lens extracts keywords → Single embedding → Search
```

**New thinking:**

```
Semantic Lens extracts keywords → 3 different embeddings:
    1. tags_vector: Domain knowledge representation
    2. metadata_vector: Structured data representation
    3. vector (text): Natural language representation
```

### The Deeper Connection

**Semantic Lens IS multi-dimensional:**

- Keywords = **domain signals** (not just text strings)
- "car" represents a **concept** (automotive domain)
- Concepts have multiple **facets**: category, properties, relationships

**Multi-Vector = Matching different facets:**

| Facet         | Semantic Lens Output          | Vector Field    | Match Type       |
| ------------- | ----------------------------- | --------------- | ---------------- |
| **Category**  | Domain noun: "car"            | tags_vector     | Domain match     |
| **Structure** | Time/number: "2015"           | metadata_vector | Structured match |
| **Narrative** | Full phrase: "my car details" | vector (text)   | Semantic match   |

**Insight:** Multi-vector search is the **natural extension** of semantic lens philosophy.

---

## 🔬 When to Use Multi-Vector RRF

### ✅ Perfect For

1. **Category Queries**
   - "Show me car facts"
   - "What do I know about toyota"
   - Tags vector excels here

2. **Mixed-Type Queries**
   - "car details and VIN"
   - Natural language + structured data
   - Multi-vector captures both

3. **User with Diverse Memory**
   - Facts span categories, dates, narratives
   - Single vector misses structured data
   - Multi-vector ensures comprehensive recall

4. **Quality-First Applications**
   - Precision > cost
   - Personal memory (not search engine scale)
   - Worth 2x cost for 30% better results

### ❌ Not Ideal For

1. **Pure Keyword Search**
   - If queries are only keywords
   - Single tags_vector may suffice
   - Multi-vector overkill

2. **Cost-Constrained Applications**
   - Need to minimize reads
   - Every extra vector channel is another vector query
   - Consider single-vector with higher limit

3. **Homogeneous Data**
   - If all facts are pure text (no tags/metadata)
   - Multi-vector redundant
   - Stick with single vector

4. **Real-Time Constraints**
   - If latency critical (<100ms)
   - Several parallel vector queries may be too slow
   - Consider caching or single-vector

---

## 🎯 Design Principles Summary

### 1. **Query-Independence Principle**

> **Rankings must be comparable across different query types.**

Use ranks, not absolute scores. RRF ensures this.

### 2. **Representation Diversity Principle**

> **Different query types need different vector representations.**

Keywords → tags, phrases → text, structure → metadata.

### 3. **Consensus Reward Principle**

> **Facts appearing in multiple queries are more relevant.**

RRF naturally rewards multi-query consensus.

### 4. **Configuration Flexibility Principle**

> **Users know their quality/cost tradeoff better than we do.**

Tiered configuration: USER > ACCOUNT > SYSTEM.

### 5. **Backward Compatibility Principle**

> **Evolution, not revolution.**

Existing code works unchanged, automatically gets better results.

---

## 🧭 In Alek-Core

- **Facts carry three vectors:** `vector` (the text), `tags_vector` and `metadata_vector`. Indexed emails add a fourth,
  `attachments_vector`.
- **Where it runs:** `SearchEnrichmentService` (`src/services/search_enrichment_service.py`) runs the channels in
  parallel and fuses them with RRF, k = 60.
  - The Router prefetches once per message, with up to 4 vector channels plus the domain channel. The orchestrator
    turn shares that result.
  - `search_memory` searches again with up to 7 channels.
- **Limits** resolve USER > ACCOUNT > SYSTEM (`UserBotConfig.semantic_search_limit` and the account defaults).

---

## 🔄 Deduplication: Number-Aware, Detail-Preserving

Similar is not the same: two facts can embed almost identically and still say different things. The rule is
**"better to keep a near-duplicate than to lose information."**

### The Algorithm

`SmartDeduplication` (`src/domain/deduplication_service.py`, pure logic, stdlib only) decides in this order:

```python
1. similarity < 0.96                    → NOT duplicate (quick exit)
2. the numbers differ (sorted)          → NOT duplicate
3. similarity >= strict (default 0.98)  → DUPLICATE
4. the new fact is 15%+ longer          → NOT duplicate (more detail)
5. otherwise                            → DUPLICATE
```

**Numbers outrank similarity.** `"Rent 900 EUR"` and `"Rent 950 EUR"` embed at about 0.97. A plain threshold calls
them duplicates; the number check keeps both. The same holds for dates (`2025-03-28` vs `2025-03-29`), measurements
and ranges.

**Length keeps detail.** `"Rent 900 EUR, paid by transfer"` is longer than `"Rent 900 EUR"` by more than 15%, so at a
moderate similarity the new fact is kept rather than dropped.

### Where Each Mode Applies

| Path | Dedup | Why |
| --- | --- | --- |
| **Search results** (`enrich_context`) | `dedup_threshold` 0.98 | the reader gets clean results |
| **Consolidation search** (`skip_semantic_dedup=True`) | off | the agent must see every candidate to decide a MERGE |
| **Consolidation writes** (`skip_deduplication=True`) | off | the model decides duplicates after searching |
| **Non-live v2 write path** (`add_fact_if_unique`) | 0.96 / 0.98 | rejects a duplicate before it is stored |

**Why consolidation turns it off:** the consolidator searches before it writes, and it needs the scattered,
overlapping facts side by side to merge them into one. A filtered result would hide exactly the facts it is meant to
merge.

---

## 📖 Further Reading

- **Reciprocal Rank Fusion:** Cormack, Clarke and Büttcher, SIGIR 2009.
- **Elasticsearch RRF:** [Official Docs](https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html)
- **Building Block:** [../05_building_blocks/search_enrichment/README.md](../05_building_blocks/search_enrichment/README.md)
- **Memory map:** [../05_building_blocks/memory/README.md](../05_building_blocks/memory/README.md) — how facts are
  written, versioned and searched.
