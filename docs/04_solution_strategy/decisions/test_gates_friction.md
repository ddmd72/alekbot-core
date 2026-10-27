# Decision: test gates cost the feature path nothing; the weekly cloud runs carry the rest

**Date:** 2026-10-27
**Status:** Accepted (owner review of the test programme)
**Context:** [TEST_COVERAGE_SYSTEMIC_RFC.md](../../10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md) §5 "Rules revised (owner, 2026-10-27)"; [tests/CLAUDE.md](../../../tests/CLAUDE.md).

**Decision:** Every package of the plan is built now. Friction on a feature change drops, and the programme runs for years without the owner's attention:
- Mutation survivors and Firestore parity run weekly in the cloud (`Mutation survivors`, `Firestore parity`); artifacts, never Slack, never red for a survivor or a failed scenario. A scheduled routine fixes survivors by test-only PR and keeps a KPI.
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
