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

The production implementation using Google's Gemini API.

- **Model:** `models/gemini-embedding-001`.
- **Dimensionality:** Fixed at **768** to match Firestore's KNN vector indexes.
- **Concurrency:** Uses `asyncio.to_thread` for safe integration with the async runtime.
- **Batch API:** `get_embeddings_batch()` sends multiple texts in a single `batchEmbedContents` call, reducing per-search embedding latency from ~15s (3 sequential calls) to ~1-2s.

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

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.11
