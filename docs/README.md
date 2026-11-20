<!-- living-doc
covers: none
-->

# Alek-Core Documentation

Documentation for Alek-Core, a personal exocortex built on hexagonal (ports and adapters) architecture, organised
with the [arc42](https://arc42.org) template. Repository-level rules for agents are in the root `CLAUDE.md`.

## Living docs

A living doc describes how things are now. The reasons for this contract:
[`living_docs_contract.md`](./04_solution_strategy/decisions/living_docs_contract.md).

**Living docs are** this file, `05_building_blocks/**`, `06_runtime/*`, `07_deployment/*`, the kept `08_concepts/*`,
`11_quality/*`, `12_risks/*` and `how_to/*`. Decision records are point-in-time, RFCs follow their own status rule,
and `CLAUDE.md` files hold rules; none of them is a living doc.

**A living doc holds only what code cannot answer in a couple of reads:** why, external facts verified live,
invariants and flows across files, layers or processes, traps, owner rulings. Every claim names the code or test it
rests on.

**It never holds** lists of methods or parameters, constant values (name the constant instead), file inventories, or
history (decision records hold history). Procedure docs (`07_deployment/*`, `how_to/*`) keep their procedure and
lose only those. A change of implementation that keeps the flows and invariants does not touch a living doc.

**Header.** Every living doc starts with a comment naming the code it owns, one repository path, directory
(trailing `/`) or glob per line:

```
<!-- living-doc
audited: 2026-01-05
covers:
  src/services/example/
  src/agents/example_agent.py
-->
```

- `audited:` is the date of the last scheduled audit. The audit sets it; a doc without it is audited first.
- An index doc that owns no code writes `covers: none`.
- `covers:` lists only files the doc owns. A tracked file is covered by at most one living doc. A hub file shared by
  many subsystems belongs to the one doc that describes the hub itself and is named elsewhere only as a
  `path::Symbol` entry point.

**How living docs stay current.**
1. `tests/unit/test_living_doc_references.py` fails when a path or `path::Symbol` a living doc names does not exist,
   a `covers:` line matches no tracked file, a file has two owners, or `audited:` is not an ISO date. It also checks
   the paths named in every `CLAUDE.md`.
2. A CI check on every pull request (`.github/workflows/living_docs.yml`): a PR that changes a file some living doc
   covers either changes that doc or carries this line in its description:
   `Docs not affected: <doc path> — <reason>`
3. A scheduled audit every two weeks takes the four living docs with the most changed lines in their covered files
   since their `audited:` date, plus the one with the oldest `audited:`. A writer rewrites each, an independent
   verifier checks every claim against the code, and the audit opens a pull request for the owner to merge.

## Index

- [04 Solution strategy](./04_solution_strategy/README.md) — decision records in `decisions/`.
- [05 Building blocks](./05_building_blocks/README.md) — how each subsystem works now.
- [06 Runtime](./06_runtime/README.md) — dynamic flows; [API reference](./06_runtime/API_REFERENCE.md).
- [07 Deployment](./07_deployment/README.md) — topology; [schedulers](./07_deployment/SCHEDULERS.md),
  [alerting](./07_deployment/ALERTING.md), [logging](./07_deployment/LOGGING.md),
  [keep-alive setup](./07_deployment/KEEP_ALIVE_SETUP.md).
- [08 Concepts](./08_concepts/README.md) — [database schema](./08_concepts/DATABASE_SCHEMA.md),
  [multi-vector RRF search](./08_concepts/multi_vector_rrf_search.md),
  [groovy prompt pattern](./08_concepts/groovy_prompt_pattern.md).
- [10 RFCs](./10_rfcs/README.md) — proposals and their status.
- [11 Quality](./11_quality/README.md) — quality gates and where the test rules live.
- [12 Risks](./12_risks/README.md) — open risks and technical debt; [implementation roadmap](./12_risks/IMPLEMENTATION_ROADMAP.md).
- How-to: [adapter wire testing](./how_to/ADAPTER_WIRE_TESTING.md),
  [new agent playbook](./how_to/NEW_AGENT_PLAYBOOK.md).
