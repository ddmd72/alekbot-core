# Fact Write Service (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the application service responsible for persisting new knowledge with automatic multi-vector generation and semantic deduplication.

### When to Read

- **For AI Agents:** Before modifying the fact creation pipeline or embedding generation logic.
- **For Developers:** When troubleshooting missing facts, duplicate knowledge, or slow consolidation performance.

### When to Update

This document MUST be updated when:

- [ ] The multi-vector generation strategy (number or type of vectors) changes.
- [ ] The semantic deduplication threshold is adjusted.
- [ ] New fact types or metadata mapping logic is introduced.
- [ ] The service's dependencies (Ports) are modified.

### Cross-References

- **Sliding Window Consolidation:** [../sliding_window_consolidation/README.md](../sliding_window_consolidation/README.md)
- **Embedding System:** [../embedding_system/README.md](../embedding_system/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)

---

## 1. Overview

The **Fact Write Service** is an Application Layer service that bridges the gap between domain reasoning and infrastructure persistence. It takes raw fact data synthesized by agents and transforms it into fully-indexed `FactEntity` objects ready for semantic search.

**Core Principle:** Extract infrastructure-heavy tasks (like embedding generation) from domain agents to maintain a clean Hexagonal Architecture.

---

## 2. The Write Pipeline

### 2.1 Multi-Vector Generation

For every new fact, the service generates **3 specialized embeddings** in parallel to support the multi-vector search strategy:

1. **Text Vector:** Semantic embedding of the main fact content (`RETRIEVAL_DOCUMENT` task).
2. **Tags Vector:** Embedding of the domain keywords/tags (`SEMANTIC_SIMILARITY` task).
3. **Metadata Vector:** Embedding of the structured metadata JSON (`SEMANTIC_SIMILARITY` task).

**Optimization:** Uses `asyncio.gather` to generate all three vectors simultaneously, providing a 3x speedup per fact.

### 2.2 Entity Construction

The service maps raw LLM output to the formal `FactEntity` domain model:

- **Type Mapping:** Maps string labels (e.g., "state", "principle") to `FactType` enums.
- **Tagging:** Automatically adds technical tags like `#consolidated` and `#anchor`.
- **Lineage:** Generates a unique `lineage_id` for SCD Type 2 versioning.

### 2.3 Smart Semantic Deduplication (2026-02-08)

Before saving, the service ensures the new fact isn't already known using a **5-level algorithm**:

- **Mechanism:** Uses `SmartDeduplicationService` (unified READ/WRITE logic)
- **Algorithm:**
  1. `similarity < 0.96` → NOT duplicate (quick exit)
  2. Numbers differ (sorted) → NOT duplicate (75 kg ≠ 84 kg)
  3. `similarity >= 0.98` → DUPLICATE (very similar)
  4. New fact longer by 15%+ → NOT duplicate (more detail)
  5. Otherwise → DUPLICATE (moderate + similar length)
- **Philosophy:** "Better to add a duplicate than to lose important information"
- **Action:** If duplicate detected, write is skipped with detailed reason logged

**Number-Aware:** Small numeric differences = different facts (critical for time-series data)

---

## 3. Architectural Role

### 3.1 Hexagonal Boundaries

- **Input:** Receives `facts_data` (plain dicts) from the `ConsolidationAgent` (Domain).
- **Dependencies:** Uses `EmbeddingService` (Port) and `FactRepository` (Port).
- **Output:** Persists `FactEntity` objects to the database via the repository adapter.

### 3.2 Comparison with Search Enrichment

While `SearchEnrichmentService` handles the **Read** path (RRF ranking), `FactWriteService` handles the **Write** path (Multi-vector generation). Both services ensure consistency in how vectors are handled across the system.

---

## 4. Code References

- `src/services/fact_write_service.py`: Main service implementation.
- `src/domain/entities.py`: `FactEntity` and `FactType` definitions.
- `src/ports/embedding_service.py`: Interface for vector generation.
- `src/ports/repository.py`: Interface for fact persistence and deduplication.

---

## 5. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Streaming Writes:** Support for real-time fact extraction during conversation (no batching).
- **Conflict Detection:** Identify when a new fact contradicts an existing one and trigger a "reconciliation" agent.
- **Cross-Vector Validation:** Ensure that text, tags, and metadata vectors are semantically aligned.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.9
