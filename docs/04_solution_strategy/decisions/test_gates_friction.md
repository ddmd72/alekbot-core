# Decision: test gates cost the feature path nothing; the weekly cloud runs carry the rest

**Date:** 2026-10-27
**Status:** Accepted (owner review of the test programme)
**Context:** [TEST_COVERAGE_SYSTEMIC_RFC.md](../../10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md) §5 "Rules revised (owner, 2026-10-27)"; [tests/CLAUDE.md](../../../tests/CLAUDE.md).

**Decision:** Every package of the plan is built now. Friction on a feature change drops, and the programme runs for years without the owner's attention:
- Mutation survivors and Firestore parity run weekly in the cloud (`Mutation survivors`, `Firestore parity`); artifacts for the owner and check-run annotations for the routine (a cloud session cannot download artifacts), never Slack, never red for a survivor or a failed scenario. A scheduled routine fixes survivors by test-only PR and keeps a KPI.
- Docs-only PRs skip `make ci` (`scripts/ci/docs_only.py`); the URL map is re-pinned by `UPDATE_PINS=1`, not by hand.
- Mechanical test adaptations go in a `test(mechanical): ...` commit (assertions unchanged); a count-1 wiring-ledger line is the implementer's; a layer floor may be lowered in the PR that needs it, with a reason and the reviewer's OK.
- P23 (`mutmut`) is dropped; P12 loses the lease document and the render gate; P17 needs a regeneration command per recorded fixture.

**Rejected alternatives:**
- *Defer packages to "when the store is next touched":* the owner will not interrupt feature work later to pay the debt.
- *Survivor reports to Slack:* nobody triages daily.
- *A mutation run on every merge to `main`:* composition modules change in almost every feature PR, 1-5 h each.
- *A local pre-push hook for `slow` tests:* CI is the gate.

**Triggers to revisit:**
- Survivors accumulate between weekly runs: add an on-merge mutation run for the touched modules.
- An `*_equivalent_mutants.txt` file grows faster than the tests: an operator is noisy.
- A floor is lowered more than twice a quarter: that layer's tests are the problem, not the floor.

## Update 2026-10-27: no per-package mutation done-check

**Decision (owner):** Mutant hunting during normal work only adds cost and makes no sense. The weekly cloud run (`Mutation survivors`, `.github/workflows/mutation.yml`, read by the Monday routine) is the only mutation pass; between runs the code lives with its survivors. The per-package local done-check and the P9 wiring done-check are dropped as requirements.

**What stays:** a package registers its new modules in `tests/support/mutation_targets.py` `TARGETS`, so the weekly run covers them; the driver (`python -m tests.support.semantic_mutants`) and `--tests-for` remain tools anyone may run by hand; the reviewer still owns the lines of `*_equivalent_mutants.txt`.

**Why:** the local run costs hours of machine time per package (the wiring set alone about 3 h) and delays the hand-over. Every done-check so far found test gaps and no code bug; P15 part 2, the last package that had one, gave 31 survivors: 24 test gaps, 7 equivalent, 0 code bugs.

**Rejected alternative:** keep the done-check for packages that change `src/`. The owner chose none.

**Trigger to revisit:** the weekly run finds a code bug that a done-check would have caught before merge.
