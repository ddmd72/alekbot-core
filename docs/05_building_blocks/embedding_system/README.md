# Embedding System (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the system for generating vector embeddings used in semantic search, deduplication, and knowledge retrieval.

### When to Read

- **For AI Agents:** Before changing embedding models, task types, or vector dimensionality.
- **For Developers:** When troubleshooting vector search failures, index mismatches, or embedding latency.

### When to Update

This document MUST be updated when:

- [ ] The primary embedding model (e.g., `models/gemini-embedding-001`) changes.
- [ ] The output dimensionality (default 768) is modified.
- [ ] New task types or configuration parameters are introduced.
- [ ] The `EmbeddingService` port interface is updated.

### Cross-References

- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Fact Write Service:** [../fact_write_service/README.md](../fact_write_service/README.md)
- **Constraints:** [../../02_constraints/README.md](../../02_constraints/README.md)

---

## 1. Overview

The **Embedding System** transforms raw text into high-dimensional numerical vectors (embeddings) that capture semantic meaning. These vectors enable Alek-Core to perform "fuzzy" searches, identify duplicates, and retrieve relevant context without relying on exact keyword matches.

**Core Principle:** Consistent vector representation across all knowledge storage and retrieval paths.

---

## 2. Architecture

### 2.1 EmbeddingService (Port)

A domain-level interface that defines the contract for vector generation.

- **Method:** `get_embedding(text, task_type)`
- **Independence:** The core logic doesn't know which LLM provider is generating the vectors.

### 2.2 GeminiEmbeddingAdapter (Adapter)

The production implementation using Google's Gemini API (`generativelanguage.googleapis.com`,
AI Studio API key).

- **Model:** `models/gemini-embedding-001`.
- **Dimensionality:** Fixed at **768** to match Firestore's KNN vector indexes.
- **Concurrency:** `asyncio.to_thread` bridges the sync SDK into the async runtime.
- **Batch API:** `get_embeddings_batch()` sends multiple texts in a single
  `batchEmbedContents` call, reducing per-search embedding latency from ~15s
  (3 sequential calls) to ~1–2s.

### 2.3 Throttling and 429 retry

The adapter caps **in-flight requests** with a process-local
`asyncio.Semaphore` and retries **`RESOURCE_EXHAUSTED` (429)** with exponential
backoff. Both the read path (`SearchEnrichmentService.enrich_context`) and the
write path (`FactWriteService` storing 3 vectors per fact) go through this
adapter, so consolidation runs that fan out parallel `search_existing_facts`
+ `create_fact` tool calls can momentarily burst above Google's per-second
limiter even when the per-minute quota has plenty of headroom.

Defaults — sized for AI Studio Tier 2 (`gemini-embedding-001` = 5000 RPM
sustained, ~83 RPS):

| Knob                         | Default | Override env var              |
| ---------------------------- | ------- | ----------------------------- |
| Concurrency cap (semaphore)  | **20**  | `GEMINI_EMBED_CONCURRENCY`    |
| Retries on 429               | 3       | (not configurable)            |
| Initial backoff              | 2s      | (doubles each attempt: 2/4/8) |

Math behind the default: with avg latency ~0.7s, `N=20` → ~28 RPS = ~1700 RPM
≈ 34 % of the Tier 2 ceiling. Leaves ~3300 RPM of headroom for cross-instance
Cloud Run bursts and parallel write-path embedding calls. Bump
`GEMINI_EMBED_CONCURRENCY` if you upgrade to Tier 3 or migrate to Vertex AI.

The retry path matches `genai_errors.ClientError` with `code=429` or the
literal string `RESOURCE_EXHAUSTED`. Other failure modes propagate
immediately.

---

## 3. Task Types

The system uses specialized task types to optimize embedding quality for different use cases:

| Task Type             | Use Case       | Description                                                 |
| --------------------- | -------------- | ----------------------------------------------------------- |
| `RETRIEVAL_DOCUMENT`  | Fact Writing   | Optimized for storing long-term knowledge in the database.  |
| `RETRIEVAL_QUERY`     | Search Queries | Optimized for short user queries or search phrases.         |
| `SEMANTIC_SIMILARITY` | Deduplication  | Optimized for comparing two pieces of text for equivalence. |

---

## 4. Integration Points

### 4.1 Knowledge Ingestion (Write Path)

The `FactWriteService` generates three vectors per fact (text, tags, metadata) using the `RETRIEVAL_DOCUMENT` and `SEMANTIC_SIMILARITY` tasks.

### 4.2 Context Retrieval (Read Path)

The `SearchEnrichmentService` generates query embeddings using the `RETRIEVAL_QUERY` task to find matching documents in Firestore.

### 4.3 Smart Deduplication

The `SmartDeduplicationService` uses embeddings to calculate cosine similarity between facts, ensuring knowledge remains unique and concise.

---

## 5. Code References

- `src/ports/embedding_service.py`: Port interface definition.
- `src/adapters/gemini_embedding_adapter.py`: Gemini-specific implementation.
- `src/domain/vector_math.py`: Cosine similarity and vector utilities.

---

## 6. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Local Embeddings:** Support for small local models (e.g., Sentence-Transformers) for faster deduplication and lower costs.
- **Dimensionality Reduction:** Explore PCA or other techniques to reduce index size while maintaining recall.
- **Multi-Modal Embeddings:** Support for image and audio embeddings to enable cross-modal search.

---

**Last Updated:** 2026-04-25  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.11

### Changelog

- **2026-04-25:** Added §2.3 Throttling and 429 retry. Production hit AI Studio
  per-second burst limiter on parallel consolidation flows even though Tier 2
  per-minute quota was barely used; adapter now caps concurrency at 20 and
  retries 429 with exponential backoff.
