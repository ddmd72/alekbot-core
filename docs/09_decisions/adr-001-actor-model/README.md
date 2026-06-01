# ADR-001: Actor Model & Multi-Agent Core

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
