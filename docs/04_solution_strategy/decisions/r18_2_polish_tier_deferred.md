# Decision: R18.2 polish-tier wire tests — deferred with rationale

**Status:** Deferred — R18.2 closed
**Date:** 2026-05-29
**Context:** Inspection finding R18.2 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

R18.2 (`CapturingStub` + `ContractRule` propagation to non-LLM adapters) is closed
as **DONE-with-deferred-polish**. Numeric acceptance is met — the rule repository
has grown from 3 to 9 named rules (target: "~9+"). The three remaining polish-tier
targets (`deep_research_webhooks`, ASGI dispatcher in `main.py`, MCP
`get_user_context` handler) are deferred and not blocking R18.2 closure.

## Why this is a legitimate closure, not a third-lane

The audit acceptance criterion (R18.2 card line 3854) is numeric: "rule repository
grows from 3 to ~9+ named rules". Repository sits at 9. The four boundary shapes
proven (LLM SDK kwargs, HTTP client requests, chained-query Firestore SDK,
subprocess) span the architectural variety in the codebase. The three remaining
files are HTTP-server-side variations of an already-proven HTTP boundary shape
(client-side covered by `gmail_provider_adapter` wire tests).

## Why we keep "polish" off the work queue

- **Solo-portfolio context** — `feedback_solo_portfolio_doc_priority.md` rejects
  exhaustive coverage-by-name as portfolio priority-A. Functionality showcase wins
  over per-file test density once mechanical contracts are proven.
- **MCP `get_user_context`** is tracked separately as F16.3 (Remote MCP Server is
  `WIP-experimental` per CC.4); revisit when MCP exits experimental status.
- **`deep_research_webhooks`** already has 5 unit tests (`test_deep_research_webhooks.py`),
  lowest marginal value of any R18.2 target.
- **ASGI dispatcher** is a thin route splitter (main.py); its correctness is asserted
  by the production behavior of both Quart blueprints and FastMCP routes — a wire
  test would mock the very thing it dispatches to.

## Rejected alternatives

- **Ship all three.** Defensible but consumes a session of work for marginal coverage
  on shapes already proven. Cost > benefit at solo-use scale.
- **Ship MCP only, defer other two.** Cleaner per "load-bearing first", but MCP
  is WIP-experimental and tracked under F16.3; introducing wire tests now would
  pin behavior that is explicitly subject to change.
- **Leave card at 🟡 IN PROGRESS with numerics met.** Forbidden — `feedback_clean_or_explain.md`
  disallows the "third lane" of partial-fix-no-decision. Forces binary closure.

## Trigger to revisit

- Any of the three surfaces ships a production bug that would have been caught
  by a wire test (signal: per-adapter post-mortem cites the missing test).
- MCP exits experimental status → F16.3 picks up the `get_user_context` test.
- Pre-release-branch pass (Bucket I) where portfolio polish gets a dedicated
  scope budget.
- A new non-LLM adapter is added — pattern propagates to it via this rule
  repository, regardless of the deferred three.

## Mechanical state preserved

The pattern is fully durable for future propagation:
- `tests/contracts/adapter_contracts.py` — 9-rule repository, AI MODIFICATION
  POLICY guards against silent rule changes.
- `docs/how_to/ADAPTER_WIRE_TESTING.md` — protocol for adding new adapters.
- `tests/integration/adapters/conftest.py` — `CapturingStub` infrastructure for
  HTTP, subprocess, chained-query SDK boundary shapes.
- `docs/04_solution_strategy/decisions/adapter_contract_pattern_propagation.md`
  — propagation rationale and pattern catalogue.
