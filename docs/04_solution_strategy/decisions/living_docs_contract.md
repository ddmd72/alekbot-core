# Living docs: what they hold, who owns which code, how they stay current

**Status:** Accepted (owner decision, 2026-11-20). The general contract is in `docs/README.md` § Living docs; the
block-specific part is in `docs/05_building_blocks/README.md` § Contract. This record holds the reasons.

## Context

The rule "docs reflect the real behaviour" turned most of `docs/` into a retelling of the code: file trees, method
lists, parameter tables, constant values. That text changes with every refactor, so keeping it current cost about one
changed doc line per four changed code lines, and the text that lagged behind was the most harmful kind for an AI
reader: a coding agent trusts a doc and follows a stale paragraph into a wrong hypothesis. The code itself answers
"what does this class do" faster and more accurately than any description of it.

Some docs still serve a reader the code does not: a developer, a reviewer or the owner who needs to know how a
subsystem works now, what it stores, which flows run through it, what must never break, before opening a file. A pull
request and its RFC say what one change did; a decision record says why; nothing else says how things work today.

The first version of this contract covered building blocks only. The same drift hit runtime, deployment, concept,
quality and risk docs, so the contract now covers every doc that describes how things are now.

## Decision

1. **A doc holds only what code cannot answer in a couple of reads:** why, external facts verified live, invariants and
   flows that cross files, layers or processes, traps, owner rulings. Every claim names the code or the test it rests
   on. A retelling of code goes.
2. **A living doc is a doc that describes how things are now:** `docs/README.md`, `docs/05_building_blocks/**`,
   `docs/06_runtime/*`, `docs/07_deployment/*`, the kept `docs/08_concepts/*`, `docs/11_quality/*`,
   `docs/12_risks/*`, `docs/how_to/*`. Not living docs: decision records (point in time), RFCs (their own status
   rule), `CLAUDE.md` files (rules; they get only the reference check). Procedure docs (`07_deployment/*`,
   `how_to/*`) keep their procedure and lose only file inventories, constant values and history.
3. **Every living doc starts with a header** naming the code it owns, one repository path, directory (trailing `/`) or
   glob per line, with the date of the last scheduled audit. An index doc that owns no code writes `covers: none`.
   `audited:` is set by the audit; a doc without it is audited first.
4. **Ownership.** `covers:` lists only files the doc owns. A tracked file is covered by at most one living doc, and a
   test enforces it. A hub file shared by many subsystems belongs to the one doc that describes the hub itself (the
   worker handler to the schedulers doc, the user cabinet app to the cabinet block, the OAuth web app to the OAuth
   web block, the credential store to the multi-tenant OAuth block; the application graph to no doc). Other docs
   name a hub file only as a `path::Symbol` entry point.
5. **A building block gets a README** when it owns stored data, sits on an external boundary, or runs a flow across
   three or more layers or processes. Anything smaller is described by its code and docstrings. A block README is a
   guide at the level that changes rarely and never lists methods, parameters, constant values or files.
6. **Three mechanisms keep living docs current:**
   - a unit test (`tests/unit/test_living_doc_references.py`) checks that every path and `path::Symbol` a living doc
     names exists, that each `covers:` line matches tracked files, that no file has two owners, that `audited:` is an
     ISO date, and that every path a `CLAUDE.md` names exists;
   - a CI check on every pull request (`.github/workflows/living_docs.yml`, `scripts/ci/living_docs_pr_check.py`): a
     PR that changes a file some living doc covers either changes that doc or carries in its description the line
     `Docs not affected: <doc path> — <reason>`;
   - a scheduled audit every two weeks (`scripts/ci/living_docs.py` picks the targets): the four living docs with the
     most changed lines in their covered files since their `audited:` date, plus the one with the oldest `audited:`.
     A writer rewrites each, an independent verifier checks every claim against the code, and the audit opens a pull
     request that the owner merges.

   The test only catches references to things that no longer exist. Text that still names existing code but describes
   behaviour that changed is caught only by the second and third mechanisms.
7. **Obsolete or rejected RFCs are kept as history, never deleted.**

## Rejected alternatives

- **Drop living docs and rely on code plus decision records.** Leaves no "how it works now" layer for a human reader;
  a reviewer would have to rebuild the picture from code for every review.
- **Docs that retell code.** The retelling grows back, because "reflect the real behaviour" invites it; the cost was in
  the rule, not in one backlog.
- **`covers:` listing every file a doc mentions.** Hub files then belong to many docs, and every change to a hub pings
  all of them. Ownership by one doc keeps the check precise and the signal meaningful.
- **Generate docs from code.** Produces the retelling this decision removes; what a living doc must hold (invariants,
  reasons, cross-process flows) is not in the code.
- **Rely on the per-PR statement alone.** A statement written while the change is fresh misses the slow drift of
  surrounding text; the audit exists for that.
- **An audit by oldest date only.** It misses the docs under the most churn, which drift fastest.

## Triggers to revisit

- Audit pull requests that mostly change nothing (the targets are chosen badly) or that cannot be reviewed in one sitting.
- `Docs not affected:` lines used as a reflex, with reasons that do not hold.
- A hub file that no single doc can honestly own.
- A doc kind outside the list above that starts to drift the same way.
