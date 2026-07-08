# RFC: Standing Directives — user-issued behavioral rules as a first-class fact domain

**Status:** Approved (design), implementation in progress
**Date:** 2026-07-08
**Owner decision:** new `AGENT_DIRECTIVE` domain (not a tag), consolidator maintains the rulebook

## 1. Problem

Users issue behavioral feedback to the agent ("never offer partial answers", "trace conditional
logic before risk judgments"). Today these land in `FactDomain.PREFERENCE` as narrative
third-person facts and render inside `knowledge_base.biographical_context` — a block whose
preamble says "treat older entries as potentially stale". Three defects:

1. **Wrong framing** — binding rules presented as stale-able data; the only bridge is the
   advisory `Align_With_Anchors` style rule.
2. **No taxonomy home** — `PREFERENCE` is defined as "habits, likes, dislikes, anchors";
   agent directives are squatters, so any future taste-preference would leak into a
   directives rendering filtered by that domain.
3. **Anti-directive merge policy** — `Core_Identity_Caution` (BIOGRAPHICAL + PREFERENCE)
   prescribes "CREATE separate observation, not UPDATE" on contradiction. For directives this
   accumulates near-duplicates instead of mutating the rule — structural drift/white-noise.

## 2. Decision

1. **New 16th domain `AGENT_DIRECTIVE`** in `FactDomain` (mandatory + validated, unlike
   free-form tags). Semantics: standing behavioral orders the user issued for the agent.
2. **Consolidator owns the rulebook — curated as a living organism on EVERY pass.**
   Directives are captured in Stage 1 as ordinary `AGENT_DIRECTIVE` facts, then curated by a
   new **unconditional Stage 2b directive-review pass** (`_review_directives`) that runs on
   every consolidation regardless of whether any directive changed. Stage 2b fetches the
   **entire** directive rulebook (not a semantic neighbour subset) and feeds it through the
   existing `_run_consolidation_loop` with the `Directive_Maintenance` rule. This makes the
   rulebook self-improving: each pass may reword, merge, re-prioritise, or evict — and a user
   complaint captured in Stage 1 is folded/refactored in the same pass.

   `Directive_Maintenance` rule in `CONSOLIDATION_TAXONOMY`:
   - imperative second-person syntax; NO third-person narrative, NO dates/meta ("the rulebook
     is timeless"); self-contained ≤40 words;
   - overlap → UPDATE/MERGE into one refined rule, never CREATE-alongside; generalize ≥2
     incidents into one rule;
   - a directive is one atomic rule — exempt from "decompose multi-concept" pressure;
   - never merge a directive with a non-directive fact;
   - **hard cap 15 (two-layer)**: (a) prompt-enforced — the pass is shown the full rulebook and
     must merge/evict down to 15; MERGE only genuinely adjacent rules, otherwise INVALIDATE the
     least essential (do NOT force unrelated behaviors into an 'umbrella' directive to preserve
     everything); (b) code backstop — `_enforce_directive_cap` re-queries after the loop and
     deterministically invalidates the lowest-priority tail if the LLM ever left >15. Merge (the
     LLM's job) preserves content; the backstop only guarantees the ceiling for the pathological
     all-orthogonal case;
   - **optimisation every pass**: semantic precision (one actionable rule each), token efficiency
     (terse imperative; strip preambles/dates/meta/decorative mottos), coherence (no overlap, no
     contradiction), **English-only** (exception: a quoted literal the agent must output or match —
     keep verbatim);
   - **convergence, not churn**: rewrite any directive short of the target form (narrative, dated,
     non-English, overlapping) — that IS improvement; a directive already imperative/atomic/terse/
     English/date-free is optimal → NO-OP. Anti-churn guards the optimum, never blocks the first pass.
   `AGENT_DIRECTIVE` is excluded from `Core_Identity_Caution`. Examples added to the rule.
   Domain list in `CONSOLIDATION_POLICIES` extended. The tracker and fact-cluster builder are
   NOT modified (Stage 2b is a self-contained pass with its own feeder — the full rulebook).

   **Curate vs obey separation.** In Stage 2b the directives appear as ordinary fact-records to
   curate (JSON cluster message), never as the orchestrator "apply, don't weigh" block. The
   `standing_directives` block is gated OUT of the consolidator's own system prompt
   (`build_for_agent(include_directives=False)`), so the consolidator never sees them as orders
   to itself.
3. **Guaranteed cache inclusion + read backstop.** `BiographicalContextService.refresh_context`
   fetches `AGENT_DIRECTIVE` facts in a **separate** priority-ordered query
   (`DEFAULT_DIRECTIVES_CACHE_LIMIT = 15` — the injection ceiling, matched by the storage cap; the
   consolidator curates to ≤15 in Stage 2b + a code backstop enforces ≤15 in storage) and stores
   them as a third cache list `directives`
   alongside `biographical_facts`/`principles`; `get_biographical_context_cached` flattens all
   three into the returned list. Rationale: the BIOGRAPHICAL-first + priority-fill selection can
   silently evict directives at `facts_limit` — unacceptable for binding rules; the separate
   query + top-10-by-priority read means the orchestrator prompt can never grow unbounded,
   independent of rulebook state.
4. **Rendering — split at PromptBuilder (mirrors the `query_specific_context` channel).**
   `PromptBuilder.build_for_agent` already partitions the flat cache list into named channels
   (`static_bio` vs `semantic_lens` → `query_specific_context`); directives become a **third
   channel**: `static_bio` excludes `agent_directive`, `directive_facts` collects it, passed as a
   separate `directives=` param to `assemble()`. The split lives in exactly ONE place. Assembly
   receives pre-separated lists and only renders: `BiographicalFactsFormatter.format_directives()`
   → `standing_directives { … }` block at the END of the static section (after the blueprint,
   before `PROMPT_CACHE_BOUNDARY`) with binding framing ("standing orders … apply, don't weigh").
   `format()` renders whatever it is given (no domain filtering). Gated by
   `include_directives` (False for the consolidator). Cache-friendly: content changes only on
   consolidation, which already refreshes the bio cache and invalidates the prompt-builder cache.
5. **One-time migration** of the existing directive entries currently in PREFERENCE:
   re-domain + reformulate to directive syntax. Texts drafted, user approves each;
   `mindset`-tagged value statements stay PREFERENCE (they are values, not orders).
   Script under `scripts/memory/` (gitignored outputs).

## 3. Rejected alternatives

- **Rendering-only split by `domain == preference`** — taste-preferences would leak into a
  binding block; producer (consolidator) would keep writing narrative syntax and
  CREATE-alongside duplicates. Promotion to binding changes the data contract; the producer
  must honor it from day one.
- **Tag on preference facts** — tags are free-form and unvalidated; a forgotten tag silently
  demotes a directive to a bio fact.
- **USER-priority prompt token per directive** (LanguagePreferenceService pattern) — new
  write path + confirmation protocol + Cabinet surface; duplicates curation the consolidator
  already does (SCD2, merge, dedup).
- **Reviving autonomous self-notes** — rejected permanently; agent-initiated self-assessment
  drifts (historical garbage source).
- **Dedicated directive audit sweep (scheduled task)** — considered for cap enforcement; rejected
  as needless infra. Curation belongs in the existing consolidation loop (Stage 2b), reusing the
  same fact-curation mechanism, running unconditionally each pass.
- **Curate directives only when one changed (tracker-domain detection + cluster union)** —
  rejected in favour of the unconditional Stage 2b: simpler (no tracker change, no cluster
  branch) and yields continuous refinement of the whole rulebook every pass. Anti-churn is a
  prompt clause (NO-OP unless clear improvement/overlap/overflow).
- **Splitting directives out of the flat list inside the formatter/assembly** (first draft) —
  re-partitioned the same list in two downstream layers; the split belongs once at PromptBuilder,
  where the `query_specific_context` channel is already split the same way.

## 4. Non-goals

- Router `relevant_domains` list NOT extended — directives are always-injected, never
  retrieval-gated.
- Orchestrator `save_to_memory` protocol NOT changed in v1 — capture works
  ("dedup downstream" contract); revisit only if directive capture proves unstable.
- No Cabinet UI in v1.

## 5. Implementation plan

| Phase | Change | Files |
|-------|--------|-------|
| 0 ✅ | Dry run: current PREFERENCE records + draft contract through a no-write cluster-review loop (real Sonnet-5 LLM, intercepted writes). PASSED — imperative rewrites + priority-based merges, NO-OP on unrelated. Examples in the rule proved mandatory (first run emitted narrative "User demands…"). | `scripts/memory/dry_run_directive_consolidation.py` |
| 1 | Domain + contract | `src/domain/entities.py` (FactDomain), `prompts_snapshot/tokens/system/CONSOLIDATION_{TAXONOMY,POLICIES}.groovy` → `firestore_utils/snapshot_upload.py` |
| 2 | Cache guarantee + rendering | `src/services/biographical_context_service.py`, `src/adapters/firestore_repo.py` (cache doc third list), `src/services/prompt_v3/biographical_formatter.py`, `src/services/prompt_v3/prompt_assembly_service.py`, `src/services/prompt_builder.py` (directive channel split + `include_directives` gate), `src/domain/settings.py` (directives cache limit = 10) |
| 3 | Stage 2b directive review | `src/agents/consolidation_agent.py` (`_review_directives`, unconditional hook in `_process_deliberate_consolidation`) |
| 4 | Migration of existing entries | script in `scripts/memory/` (re-domain + approved reformulations) |

Phase 0 gated Phase 1: the dry run confirmed the contract handles the live records
correctly before any code shipped.

## 6. Test plan

Full coverage of the delta; existing tests untouched:
- formatter: `format()` excludes domain / `format_directives()` renders imperative list,
  newest-first, empty → `""`;
- assembly: block present and positioned before `PROMPT_CACHE_BOUNDARY`, absent when no
  directives, both `kb_preamble` modes, bio block free of directives;
- bio context service: directives fetched separately, never evicted by `facts_limit`,
  third cache list round-trips through `get_biographical_context_cached`;
- enum: new domain valid in `FactEntity` / fact write path.

E2E: migrate on dev → live message → assembled prompt pulled from BigQuery
`prompt_content` shows the block; then send behavioral feedback → after consolidation
verify UPDATE/MERGE (not CREATE-alongside) in operations log.

## 7. Rollback

Prompt tokens are snapshot-versioned (git) — revert file + re-upload. Code changes are
additive (new domain, new block); disabling = re-domain directives back to PREFERENCE.
