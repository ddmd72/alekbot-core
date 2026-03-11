# ADR-001: Actor Model & Multi-Agent Core

## 📖 HowTo: Using This Document

### Purpose
Record the decision to adopt an actor-based multi-agent core.

### When to Read
- **For AI Agents:** Before refactoring orchestration or agent topology.
- **For Developers:** When implementing new agents or coordination patterns.

### When to Update
This document MUST be updated when:
- [ ] The agent topology changes.
- [ ] A new coordination mechanism replaces AgentCoordinator.

### Cross-References
- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Building Blocks:** [../../05_building_blocks/README.md](../../05_building_blocks/README.md)

---

## Status
PROPOSED (placeholder)

## Context
Legacy BrainService became a bottleneck; a modular agent network was required.

## Decision
Adopt AgentCoordinator + per-user agents (UserAgentFactory).

## Consequences
- ✅ Scalable orchestration
- ✅ Clear agent responsibilities
- ❌ Requires coordination overhead
