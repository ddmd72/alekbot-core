# Decision: Memory-first dispatch partition — deletion deferred, gated on per-test approval

**Status:** Investigation complete; deletion deferred
**Date:** 2026-05-29
**Context:** Inspection finding F3.8 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

The audit-named artifact (memory-first tool-call partition in
`DelegationEngine._execute_tool_calls`) is **confirmed real** by static analysis:
no production code consumes the `memory_context` field it populates. The
audit's recommendation to delete-after-experiment is supported by code evidence.
Deletion is deferred to a separate session because the current behavior is
encoded as the system's spec by an existing unit test, which `tests-sacrosanct`
forbids touching without per-test approval.

## What was investigated

`DelegationEngine._execute_tool_calls` (`src/infrastructure/delegation_engine.py:274-336`)
partitions tool calls into a sequential `search_memory` phase followed by a
parallel "others" phase. The sequential phase populates `memory_context: List[str]`
which is then forwarded to each downstream dispatch via the `delegation_context`
dict (`_dispatch_single:381`).

### Static-analysis findings

- **Zero consumers in `src/agents/`** — `grep -rn memory_context src/agents/`
  returns no matches.
- **Zero consumers in `src/`** — only the writer (`delegation_engine.py`) appears.
- **`AgentCoordinator.handle_delegation`** does not read or transform
  `memory_context`; it merely passes the delegation_context through to the
  dispatched agent.

The audit's framing therefore holds: the partition reorders execution but the
LLM's tool-call parameters are already committed (atomic emission per turn).
Memory results matter only to the *next* turn's LLM call, which uses the
tool-response message — independent of per-turn ordering.

## Why deletion is deferred

`tests/unit/agents/core/test_smart_response_agent.py::test_memory_first_then_parallel`
(line 287) asserts `call_order[0] == "search_memory"`. The test is the system's
spec for the current ordering. Per `CLAUDE.md` test-sacrosanct rule, this test
cannot be modified, deleted, or rewritten without explicit per-test approval
from the user. Removing the partition without first obtaining that approval
violates the rule.

The audit's acceptance criterion is also explicitly experiment-driven:
*"Run an experiment removing the memory partition: parallelize all tools
uniformly, measure whether anything regresses."* Static analysis is necessary
but not sufficient — a production-traffic measurement window is the gating
artifact.

## Rejected alternatives

- **Delete the partition now, leave the test failing.** Violates test-sacrosanct.
- **Delete the partition + rewrite the test to assert all-parallel.** Touches an
  existing test; requires explicit per-test approval not in scope of this session.
- **Delete only `memory_context` field (keep the partition).** Trivial cleanup
  that doesn't address the architectural concern — the latency cost is in the
  sequential phase, not the unused field.

## Trigger to revisit

- User explicitly approves modifying `test_memory_first_then_parallel`.
- A production-traffic measurement window opens (e.g. via a feature-flag rollout
  with metrics on memory-search turn latency).
- A downstream consumer of `memory_context` is added — at which point the
  partition becomes load-bearing and this entire decision becomes obsolete.

## Pinning evidence preserved

Investigation findings frozen in this record so the next session does not
re-walk the static-analysis pass. Resume by validating the test still encodes
the spec, then proceed to per-test approval + deletion + replacement test
asserting all-parallel ordering.
