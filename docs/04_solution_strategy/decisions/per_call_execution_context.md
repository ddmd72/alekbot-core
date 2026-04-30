# Decision: Per-Call Execution Context

**Status:** Adopted
**Date:** 2026-04-30
**Context:** [docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md] § 4

## Decision

Agents do NOT store per-call execution state on `self.*`.
Provider, model, thinking effort, intent remap, and any other
per-invocation parameter flow through `AgentMessage.context`
(or as explicit method arguments) and are resolved per call.

## Why

Storing per-call state on the agent instance forces serialization
(via `asyncio.Lock` or similar) to prevent concurrent invocations
from racing on `self.llm` / `self.model_name` / etc. Serialization
becomes a hidden bottleneck whose cost is invisible until concurrent
notifications arrive (e.g. daily email review + reminder fire on the
same user).

Per-call context is the gexagonal-architecture-correct shape: an
agent is a stateless computation; per-call data arrives via the
message contract.

## Concrete consequences

- `SmartResponseAgent._execute_lock` removed.
- `ExecutionOverride` (formerly `TaskExecutionOverride`, frozen
  dataclass co-located with `TaskExecutionResolver` in
  `src/services/task_execution_resolver.py`) carried via
  `AgentMessage.context["execution_override"]` or resolved locally
  in `execute()` from `task_complexity`. Never written back to
  `self`.
- Same rule applies to any future agent: if a parameter varies per
  call, do not store it on the instance.

## Where it applies

All `BaseAgent` subclasses. Today this changes only `SmartResponseAgent`.
Quick, Router, specialist agents already comply.

## Trade-off

Resolution happens on every call rather than once at construction.
Cost: ~microseconds per call. Benefit: no serialization, no
locks, no race window.
