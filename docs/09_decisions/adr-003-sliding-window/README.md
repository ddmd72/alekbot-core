# ADR-003: Sliding Window Session Lifecycle

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
