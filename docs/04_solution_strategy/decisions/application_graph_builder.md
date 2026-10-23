# Decision: one application-graph builder, shared by `main()` and the tests

**Status:** Adopted (2026-10-23, P9).

## Decision

`src/composition/application_graph.py` holds `ApplicationGraph` (a typed dataclass) and `build_application_graph(config, db_client)`:
every non-web object the main service runs. `main()` calls it and passes typed `app.<field>`s to the blueprints; the wiring tests
build the same function over the H1 fake in the sealed deploy environment. `main.py` went from 1442 to about 635 lines and keeps
telemetry, settings, the Firestore client and the web layer.

## Declared deviation

The builder is one function of about 750 lines, not section helpers: splitting it meant passing about 40 shared locals through
parameters, a non-mechanical rewrite of code the move had to keep verbatim. **Split when** the next change needs one part of the
graph alone, or "tidy main.py" runs.

## Rejected

- **An AST ledger of `main.py`:** it reads shape, not the values wired.
- **A `main()` harness alone:** no typed graph for tests, class recorders, and the private late writes stay.
- **An additive builder that `main()` does not call:** a second copy of the composition is the #148 class.
- **`blueprint_kwargs`:** stringly typed; `main()` passes `app.<field>`.
- **A full "tidy main.py" now:** too large; the voice closures, `/worker`, `/health` and the MCP dispatcher stay.

## Triggers to revise

`/worker` moves into a blueprint (then `test_main_worker_route_gate.py` retires); a second process needs the graph
(`indexer_main`, `job_main`, `relay_main` still wire themselves).
