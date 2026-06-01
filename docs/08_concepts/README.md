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

### Core Philosophies

- **[Multi-Vector RRF Search](./multi_vector_rrf_search.md)** — The ideology behind query-independent ranking and consensus retrieval.
- **[Fractal Architecture](./fractal_architecture.md)** — Recursive hexagonal patterns.

### Practical Guides

- **[Prompt Assembly Guide](./prompt_assembly_guide.md)** — Token-based assembly (v4).
- **[Provider Resolution Guide](./provider_resolution_guide.md)** — How to configure models, tiers, and fallbacks.
- **[User Management Guide](./user_management_complete_guide.md)** — Onboarding, platform linking, and IAM.
- **[Security Validation Guide](./security_validation_guide.md)** — Implementing defense-in-depth against injections.

### Agent Best Practices

- **[Agent Best Practices](./agent_best_practices.md)** — Production readiness, error handling, and coordination.
- **[Agent Business Logic](./agent_business_logic.md)** — Workflow optimization and cost management.
- **[Groovy Prompt Pattern](./groovy_prompt_pattern.md)** — Code-based prompt engineering with Groovy DSL.

### Data & Schema

- **[Database Schema](./DATABASE_SCHEMA.md)** — Firestore collection structures and relationships.
- **[OAuth Multi-Tenant Guide](./oauth_multi_tenant_guide.md)** — Technical details of the multi-tenant implementation.

---

_Alek-Core Concepts_
