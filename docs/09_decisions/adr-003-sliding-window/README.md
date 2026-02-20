# ADR-003: Sliding Window Session Lifecycle

## 📖 HowTo: Using This Document

### Purpose
Record the decision to use sliding window + consolidation for session lifecycle.

### When to Read
- **For AI Agents:** Before modifying session persistence or consolidation logic.
- **For Developers:** When tuning session thresholds or batch sizes.

### When to Update
This document MUST be updated when:
- [ ] Sliding window thresholds change.
- [ ] Consolidation pipeline changes.

### Cross-References
- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Building Blocks:** [../../05_building_blocks/README.md](../../05_building_blocks/README.md)

---

## Status
PROPOSED (placeholder)

## Context
Per-message observation was costly; session-level consolidation was required.

## Decision
Use sliding window hot storage + consolidation queue for cold storage synthesis.

## Consequences
- ✅ Reduced cost and latency
- ✅ Simplified lifecycle management
- ❌ Requires batch orchestration
