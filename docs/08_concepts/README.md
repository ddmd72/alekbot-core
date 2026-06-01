# 08 Concepts

## 📖 HowTo: Using This Document

### Purpose

Provides deep dives into cross-cutting architectural patterns, philosophies, and practical guides for Alek-Core.

### When to Read

- **For AI Agents:** Before implementing features that rely on core system philosophies (e.g., RAG, multi-vector search).
- **For Developers:** To understand the "why" behind specific implementation choices and follow best practices.

### When to Update

This document MUST be updated when:

- [ ] A new concept guide is created.
- [ ] A core philosophy (e.g., memory model) evolves.
- [ ] Best practices for agents or prompts are updated.

### Cross-References

- **Building Blocks:** [../05_building_blocks/README.md](../05_building_blocks/README.md)
- **Target Architecture:** [../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)

---

## 📂 Contents

- **[Hexagonal Architecture Patterns](./hexagonal_architecture_patterns.md)** — where a class goes, how layers stay

- **[Multi-Vector RRF Search](./multi_vector_rrf_search.md)** — why memory search fuses ranks from several vectors,
and how duplicates are judged.

to write a token.

system prompt, and where the cache boundary sits.
- **[Firestore Collections](./DATABASE_SCHEMA.md)** — every collection, its document id, and where its shape is
defined.


in `src/*/CLAUDE.md`.










---


