# Building-block docs: what they hold and how they stay current

**Status:** Accepted (owner decision). The contract itself lives in `docs/05_building_blocks/README.md` § Contract.

## Context

The rule "docs reflect the real behaviour" turned most of `docs/` into a retelling of the code: file trees, method
lists, parameter tables, constant values. That text is what changes with every refactor, so keeping it current cost
about one changed doc line per four changed code lines, and the text that lagged behind was the most harmful kind for
an AI reader: a coding agent trusts a doc and follows a stale paragraph into a wrong hypothesis. The code itself answers
"what does this class do" faster and more accurately than any description of it.

Building blocks still have a reader the code does not serve: a developer, a reviewer or the owner who needs to know
how a subsystem works now — what it stores, which flows run through it, what must never break — before opening a file.
A pull request and its RFC say what one change did; a decision record says why; nothing else says how the whole
subsystem works today.

## Decision

1. **A doc holds only what code cannot answer in a couple of reads:** why (decision records, RFCs), external facts
   verified live, invariants and flows that cross files, layers or processes, traps, owner rulings. Every claim names
   the code or the test it rests on.
2. **A building block gets a README** when it owns stored data, sits on an external boundary, or runs a flow across
   three or more layers or processes. Anything smaller is described by its code and docstrings.
3. **A block README is a guide at the level that changes rarely:** purpose, owned data (what fields mean, who writes,
   who reads), main flows with named entry points, invariants and traps, links, where to start reading. It never
   lists methods, parameters, constant values or files. A change of implementation that keeps the flows and the
   invariants does not touch it.
4. **Three mechanisms keep it current:**
   - a unit test checks that every path and every `path::Symbol` a block README names exists, and that each `covers:`
     pattern matches a tracked file — renames and deletions fail the build;
   - the `covers:` header names the code the block describes; a pull request that changes covered code says in its
     description, per block, "README updated" or "not affected" with a reason;
   - a scheduled audit compares two blocks every two weeks against their code and opens a pull request with the fixes.

   The test only catches references to things that no longer exist. Text that still names existing code but describes
   behaviour that changed is caught only by the second and third mechanisms.

## Rejected alternatives

- **Drop building blocks and rely on code plus decision records.** Leaves no "how it works now" layer for a human
  reader; a reviewer would have to rebuild the picture from code for every review.
- **Keep the old rule and only clean up once.** The retelling grows back, because "reflect the real behaviour" invites
  it; the cost was in the rule, not in one backlog.
- **Generate block docs from code.** Produces the retelling this decision removes; what a block doc must hold
  (invariants, reasons, cross-process flows) is not in the code.
- **Rely on the per-PR statement alone.** A statement written while the change is fresh misses the slow drift of
  surrounding text; the audit exists for that.
