# ADR-002: Firestore as Adapter Behind Ports

## 📖 HowTo: Using This Document

### Purpose
Record the decision to treat Firestore as an adapter, not core architecture.

### When to Read
- **For AI Agents:** Before describing storage in architecture docs.
- **For Developers:** When adding or replacing persistence layers.

### When to Update
This document MUST be updated when:
- [ ] Storage backend changes.
- [ ] New persistence ports are introduced.

### Cross-References
- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Building Blocks:** [../../05_building_blocks/README.md](../../05_building_blocks/README.md)

---

## Status
PROPOSED (placeholder)

## Context
The system must remain cloud-agnostic; storage should be replaceable.

## Decision
Model Firestore as the current adapter for FactRepository and SessionStore ports.

## Consequences
- ✅ Infrastructure invariance
- ✅ Easier migration to other datastores
- ❌ Additional abstraction to maintain
