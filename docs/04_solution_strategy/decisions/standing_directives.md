# Decision: Standing Directives — user behavioral rules as a first-class fact domain

**Date:** 2026-07-08
**Status:** Shipped (live). Full design + migration: `docs/10_rfcs/STANDING_DIRECTIVES_RFC.md`.

## Decision

User-issued behavioral rules ("never give partial answers", "trace conditional logic before
judging") are now a dedicated fact domain **`FactDomain.AGENT_DIRECTIVE`**, rendered as a binding
**`standing_directives {}` block** injected into the orchestrator's system prompt on every request —
NOT stored as biographical `preference` facts and NOT as firing self-reminders.

Three distinct persistence kinds were being collapsed into two containers; this separates them:

| Kind | Home | Property |
|------|------|----------|
| Facts about the user | `preference`/biographical → `knowledge_base` | retrieval/priority-gated context |
| **Behavioral rules for the agent** | **`agent_directive` → `standing_directives`** | **always-injected, binding** |
| Deferred firing instruction | `orchestrator_notes` (self-reminders) | fires as a future conversation |

## Mechanism (the load-bearing parts)

- **Rendering is a channel split at PromptBuilder**, mirroring the existing `query_specific_context`
  channel: `build_for_agent` partitions the flat cache list into `static_bio` / `directives` and
  passes each as its own param to `assemble()`. Assembly only renders — no re-partitioning downstream.
  Gated by `include_directives` (False for the consolidator, so it never sees them as orders to itself).
- **Curation reuses the consolidation loop.** New **unconditional Stage 2b** (`_review_directives`)
  runs every consolidation, is fed the FULL rulebook (not a semantic subset), and optimises it as a
  system-instruction section: imperative English, terse, one rule each, no overlap/contradiction/
  white noise. Directives appear as records-to-curate, never as obey-orders.
- **Convergence, not churn.** The review rewrites anything short of the target form (narrative,
  dated, non-English, overlapping) — that IS improvement; an already-optimal directive is NO-OP.
  Verified live: after optimisation a following pass returned 0 operations.
- **Hard cap 15, two layers.** (a) prompt-enforced: merge genuinely-adjacent rules, else INVALIDATE
  the least essential — do NOT force unrelated behaviors into an "umbrella" directive; (b) code
  backstop `_enforce_directive_cap`: after the loop, deterministically invalidate the lowest-priority
  tail if the LLM left >15. Injection is independently hard-bounded by
  `DEFAULT_DIRECTIVES_CACHE_LIMIT = 15`, so the prompt can never exceed 15 regardless of storage.
- **English-only** except quoted literals the agent must output verbatim or match (e.g. a required
  disclosure phrase, a forbidden phrase) — kept in original language.

## Alternatives rejected

- **USER-priority prompt token per directive** (LanguagePreferenceService pattern) — new write path +
  confirmation protocol + Cabinet surface; duplicates curation the consolidator already does (SCD2,
  merge, dedup).
- **Rendering-only split by `domain == preference`** — promotion to binding changes the data contract;
  the producer (consolidator) must author imperative rules, not narrative facts, from day one.
- **Free-form tag on preference facts** — unvalidated; a forgotten tag silently demotes a directive.
- **Dedicated audit sweep / curate-only-when-a-directive-changed** — needless infra / extra branch;
  the unconditional Stage 2b reuses the fact-curation loop and yields continuous refinement.
- **Prompt-only cap ("HARD CAP" as instruction)** — LLM hoards; the deterministic code backstop is
  the actual guarantee, the prompt merely permits dropping the weakest.
- **Autonomous agent self-notes** — rejected permanently; agent-graded self-corrections drift (the
  historical garbage source that got the old self-notes mechanism disabled).

## Revise if

- The rulebook regularly hits the backstop (LLM systematically hoards) → tighten the discard prompt
  or lower cap.
- Over-merging bundles unrelated behaviors despite the "no umbrella" rule → raise cap or add a
  per-directive coherence check.
- A future real prod needs per-user directive editing → add a Cabinet surface over the domain.
