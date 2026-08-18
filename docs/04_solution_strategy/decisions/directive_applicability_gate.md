# Standing directives need an applicability gate, not just an optimiser

**Date:** 2026-08-17, shipped 2026-08-18
**Status:** Accepted and live — both prompts changed, ran twice in production
**Scope:** `src/agents/consolidation_agent.py` (Stage 2b user message),
`prompts_snapshot/tokens/system/CONSOLIDATION_TAXONOMY.groovy` (Stage 1 classification rule),
`scripts/consolidation/test_directive_review_dryrun.py`,
`scripts/consolidation/test_stage1_classification_dryrun.py`

> **Outcome first.** The rulebook went 14 → 6 directives: 2 reminder-protocol duplicates invalidated,
> 6 situational rules demoted to `PREFERENCE` with their trigger condition stated, 6 universal rules
> kept. Nothing was lost — a demotion creates the preference fact before invalidating the directive.
>
> **The measurement that matters most is the variance, not the result.** Two production runs on an
> identical rulebook, identical prompts and identical model produced **0 and 6 demotions**. The bench
> had already shown the same spread (4, 4, 1 demotions on one prompt). The composition of the rulebook
> on any given pass is therefore governed as much by run-to-run dispersion as by the rule text. Read
> § Variance before concluding that a future pass "broke" anything.

## Context

A user question — "measure the distance from the blue dot to the cross in the sea", with an iPhone
screenshot of Google Maps attached — burned Smart's entire 300s `INTERACTIVE` budget and fell back to
Quick (trace `01a00eb17f…`, 2026-08-17 07:48–07:53 UTC).

The initial reading was a vision failure: the model had missed the map's scale bar. The telemetry
says the opposite. Grok's own output on the turn where it first saw the image:

> «На скріні є масштаб 50 м — звірю його з орієнтирами на карті й порахую пряму дистанцію до хреста.»
> *(There is a 50 m scale on the screenshot — I'll cross-check it against landmarks on the map and
> compute the straight-line distance to the cross.)*

It saw the scale bar and read it correctly. Verified alongside: the image reached the model intact
(1179×2556 PNG, RGBA, ~1.85 MB, no resizing in our pipeline — `MAX_FILE_BYTES` rejects above 5 MB and
does nothing else; delivered as an `input_image` data URI; xAI billed ~2.5k tokens, so it was
processed at real resolution, not as a thumbnail).

What it did next was cross-check the scale against real-world geometry — breakwater coordinates and
lengths — through Maps `search_places` and `overpass-api.de` web fetches. No tool could supply those,
and the budget died. The binding standing directive:

> Verify structural logic before drawing conclusions; **never judge from surface-level text
> parsing**, never force conclusions to fit facts, and flag conflicting search results explicitly
> instead of presenting one source as definitive.

Reading "50 m" off a screenshot *is* surface-level text parsing. The directive was written about code
and logs; it carries no scope, and the rulebook is injected verbatim on every request under
"Apply, don't weigh". Reinforced by a second directive: *"Require deep, evidence-based argumentation
with actual data; challenge abstract claims…"*.

This is the design working as specified. The damage is not token cost — the block is 3106 chars
(~780 tokens of a ~22k prompt) and sits in the cached static prefix. The damage is **misapplication**
of a situational rule made binding on every request.

## Findings

Applying "does this rule change behavior on EVERY request?" to the live rulebook (14 records):

- **7 pass** — honesty about bugs, answer completeness, proactivity, joke-vs-serious, chat
  formatting, information-sourcing policy, argumentation standard.
- **5 fail, needing a trigger** — real-time data staleness, bookings-as-proof, PDF/DOCX formatting,
  background-task reporting, car-as-transport.
- **2 are not behavioral rules at all** — "Run Valencia event radar Tuesday and Friday at 18:30" and
  "Run the slow-burn enrichment sweep every 3 days at 19:00" **exactly duplicate existing
  self-reminders** (`FREQ=WEEKLY;BYDAY=TU,FR` and `FREQ=DAILY;INTERVAL=3`, matching times). As
  directives they change nothing on a normal request and consume 2 of 15 cap slots.

So roughly half the rulebook fails the test. A perverse consequence of the hard cap: while
situational rules occupy slots, the cap forces the curator to drop *universal* rules to keep them.

Also recorded, since it shaped the design:

- `FactManagementAdapter.update_fact` handles content/tags/state/temporal_class and **never
  `domain`**. A directive cannot be re-domained in place; demotion must be
  `create_fact(domain=PREFERENCE)` + invalidate. That is the safer shape anyway — the original record
  survives, so TD-4 (in-place text overwrite with no SCD2 history) does not apply to demotions.
- `invalidate_fact` and `_enforce_directive_cap` both write only `state`, leaving `is_current=True`.
  Retrieval filters on `state`, so behavior is correct, but the two flags disagree on 2 records today.
  Any future code that trusts `is_current` for directives will see a different set. Not fixed here.
- Directives render only into the **orchestrator** prompt. The PDF/DOCX rule is therefore binding on
  the agent that does not generate documents and absent from DocPlanner/PdfGenerator, which do.
  Demotion to a preference fact does not fix that; a channel to the specialist would. Not fixed here.

## Decision

1. **Invalidated** the directive that caused the incident —
   `1a7bf31f-dfbc-4934-ac8a-95d7e52eb43b`, `state: current → invalidated` (revert: set back to
   `current`). The reinforcing "evidence-based argumentation" directive was left in place.

2. **Built a bench before changing the curator** —
   `scripts/consolidation/test_directive_review_dryrun.py`. Runs the real
   `ConsolidationAgent._review_directives` over the real rulebook with fact writes intercepted, and
   replays the SAME input N times: since nothing is persisted, any disagreement between runs is churn,
   not progress.

3. **Drafted a candidate Stage 2b instruction, deliberately kept in the bench script, not promoted
   to `consolidation_agent.py`.** It adds what the production prompt lacks: a definition of what
   qualifies as a directive, the application mechanism to judge against (verbatim, unconditional,
   "Apply, don't weigh", with this incident as the worked example), an instruction to treat existing
   wording as *unreliable authorship* — earlier passes of this same review authored it without
   knowing that mechanism — and a routing table for records that fail the gate: situational → demote
   to `PREFERENCE`, schedule → invalidate (a reminder already covers it), unactionable → invalidate,
   overreaching-but-universal → narrow rather than delete.

## Measurements

| | baseline (production) | candidate |
|---|---|---|
| ops on the live rulebook | **0 — everything declared optimal** | 19 (run 1) / 19 (run 2) |
| tokens per pass | 1 935 | 10 313 – 12 595 |
| elapsed per pass | 25 s | 113 – 131 s |

The baseline does not churn — it converges instantly by leaving everything alone, including the two
duplicate schedules. Its defect is the absence of a rejection criterion, not instability.

The candidate reproduced the intended classification across both runs: both invalidated the two
duplicate schedules, and both demoted the same four situational rules into `PREFERENCE` with the
trigger named explicitly (e.g. *"When asked about real-time data (weather, sea conditions, sensor
readings), never…"*).

That first candidate was **not** promotable: 3 of 14 records disagreed between runs, one in the
dangerous direction (run 1 demoted *"Acknowledge failures and bugs openly"*). What followed is the
iteration that shipped.

## What actually shipped (2026-08-18)

Two changes, in two different places, deliberately not duplicating each other.

**Stage 1 — `Directive_Maintenance.classification` (Firestore token, mirrored in
`prompts_snapshot/`).** The old rule routed by SUBJECT only ("the user instructs HOW THE AGENT must
behave → AGENT_DIRECTIVE"), which correctly admits *situational* agent rules and is exactly why they
kept being created. It now applies two tests — SUBJECT **and** SCOPE ("in force across most requests,
not only when a specific condition holds") — with a third outcome: passes SUBJECT, fails SCOPE →
`PREFERENCE`, rewritten to state its condition. A `reminders` key was added telling the consolidator
that recurring tasks already carry their execution protocol in the reminder system and must not be
restated as facts. Fixing the classifier at creation is what stops the create→demote treadmill; Stage
2b alone would have fought Stage 1 forever.

**Stage 2b — `_build_directive_review_message` (code).** Baseline emitted **0 operations** on the live
rulebook, and the reason was structural: its only removal branch sat *inside* the HARD CAP section, so
below the cap it never activated, while "Guard the optimum; never oscillate an already-clean rule" read
as a licence to do nothing at all. The rewrite keeps every baseline mandate (optimisation objectives,
convergence guard, cap) and adds:

- a standing **REMOVE** duty with three outcomes — DEMOTE / INVALIDATE / MERGE — applied on every pass,
  not only at the cap;
- the convergence guard explicitly scoped to **wording**, so it can no longer excuse keeping a record
  that does not belong;
- the SCOPE criterion **by reference** to `Directive_Maintenance` rather than restated — the rule lives
  in the shared system prompt, which Stage 2b also receives, so there is one definition, not two;
- a **PROCEDURE checklist**: one line per record (`scope_ok` / `reminder_dup` / `unusable`) before any
  operation, with "a record you did not list is a record you did not review". This fixed a real failure
  mode — an earlier run examined only a third of the rulebook and stopped.

## Variance

This is the finding to carry forward. On identical input, identical prompts and identical model
(`claude-sonnet-5`, thinking=medium):

| | demotions | invalidations |
|---|---|---|
| bench, reference variant, 3 runs | 4 / 4 / 1 | 2 / 2 / 2 |
| bench, checklist variant, 3 runs | 4 / 4 / 4 | 2 / 2 / 2 |
| **production run 1** | **0** | 2 |
| **production run 2** | **6** | 0 (already done) |

Production run 1 marked `scope_ok=true` for all 14 records and argued *"conditional-on-topic is fine
per precedent … these are standing agent policies"* — the rulebook's own contents used as evidence that
its contents belong. Run 2, same everything, failed six records on SCOPE.

Two hypotheses were raised and **both refuted by evidence**, which is why they are recorded here rather
than left as folklore:

- *"The single-line formatting of the Firestore rule differs from the multi-line text the bench
  measured."* Refuted: run 2 used the identical token and demoted six.
- *"Stage 2b behaves differently because Stage 1 ran before it in the same execution."* Refuted:
  `_run_consolidation_loop` starts `history` from scratch per stage — Stage 2b cannot see Stage 1.

What remains is dispersion. For a pass that runs on every consolidation, that means the rulebook's
composition is decided as much by which way a given run falls as by the rule text. The owner accepted
this and is observing it over time rather than tuning further.

## Consequences

- Semantic retrieval of demoted rules is not guaranteed; a miss means the rule does not fire. This is
  the right trade for situational rules: a missed formatting preference is cheap, while a misapplied
  verification rule cost a full interactive budget and a wrong answer.
- Consolidation *can* address the relevance problem, contrary to the first analysis in this
  investigation, which held that only the injection side could. The curator is the earlier lever: it
  decides what is allowed to be a directive at all.
- Cap pressure eased: 6 directives against a cap of 15, so `_enforce_directive_cap` is dormant.
- **Two demotions are judgement calls, not clear wins.** *"Acknowledge failures and bugs openly"* is
  tone rather than topic, and the bench over-fired on it 1 run in 3 before shipping. *"Always send the
  scheduled background-task report"* now depends on semantic retrieval firing on the background-notify
  path — if `UserNotificationService.notify` does not enrich from memory there, the rule silently stops
  applying and reports start being skipped, which is the exact behaviour it exists to forbid. That path
  was **not** verified. Check it before trusting the demotion.
- Reverting any demotion is `state: invalidated → current` on the original record; the ids are in the
  bench outputs under `scripts/memory/consolidation/` (gitignored).
