# RFC: Fact lifecycle — expiry decided by content, a daily janitor, a periodic review pass, owner questions, real versions

**Status:** Accepted for implementation (owner, 2026-10-21): rev 2, independent review applied, owner confirmations in §8. S1 is live; **S2 is built** (2026-10-23, on the S2–S5 branch, not deployed; see "S2 as built" in §9); S3–S5 are not built.
Rev 1 had owner decisions Q1–Q4; two adversarial reviews (code paths; rollout and steady state) and a binding
arbitration produced this revision. Slice S1 (§9) is merged and deployed; S2 is built and deploys with S3–S5; the rest is not built.
**Supersedes, once accepted:** `DELIBERATE_FACT_MANAGEMENT_RFC.md` §2.2 axis 2 "Lifecycle Transitions", §4.4 (default
TTL by class), §4.5 (state transition rules), §5.3 "Update Modes", §7.1 `FactLifecycleService`. The rest of that RFC
(taxonomy, tools, cognitive process) stays in force.
**Related:** `MEMORY_FACT_CONFIRMATION_RFC.md` (owner-in-the-loop precedent, echo trap),
`STANDING_DIRECTIVES_RFC.md` (Stage 2b: a review pass over a record set),
`docs/04_solution_strategy/decisions/fact_s1_defects.md` (what S1 fixed, what it left for S2),
`docs/12_risks/IMPLEMENTATION_ROADMAP.md` TD-4 (in-place `update_fact`).

Decision numbers changed in rev 2. Map from rev 1: D1 → D1; D2 → D2; (new) D3 validation; D3 janitor → D4; D4
visibility → D5; D5 review → D6; D6 questions → D7; D7 versions → D8; (new) D9 one writer; (new) D10 observability;
D8 cleanups → D11.

## 1. Problem

Measured on 2026-10-21 **before S1**: a full read of `{prefix}domain_facts_v2` (1193 documents, 564 `current`) and 242
consolidation turns in `prompt_content` (since 2026-09-23). The trigger: an `ephemeral` fact with `ttl_days=14` and
`reported_date=2026-06-01` ("User is refactoring the Alek document generation pipeline (Jun 2026)…") was still
`current` 4.5 months later.

### 1.1 Fixed by S1 (merged and deployed 2026-10-21)

| Defect before S1 | What S1 did | Live data after the S1 backfill |
|---|---|---|
| `update_fact` wrote the old three vectors back, so an edited fact was found by the meaning of its first text | re-embeds text, tags and metadata on any change to them, with the creation recipe `domain/fact_embedding.py::fact_embedding_texts` (`fact_management_adapter.py:312-322`) | 354 documents with `version > 1` re-embedded, in any state |
| `is_current`/`valid_to` out of sync with `state` (78 `superseded` with `is_current=True`); Cabinet invalidate flipped only `state` | `domain/fact_lifecycle.py::lifecycle_fields` maps state to flags on every retirement path: adapter update and merge, Cabinet `invalidate_fact` (`firestore_repo.py:307-319`), the directive cap backstop | 182 documents fixed: 78 superseded now `is_current=False`; 53 archived and 51 invalidated got `valid_to` |
| `add_fact_if_unique` deduplicated against `is_current==True` | filters `state == "current"` (`firestore_repo.py:680`) | — |
| the domain channel returned the 10 **oldest** facts of a domain | `search_facts_by_domain` orders `created_at DESC` (`firestore_repo.py:962`) | — |
| `update_fact` did not accept `metadata` although `CONSOLIDATION_TOOLS` promised it | accepted, merged key by key (`fact_management_adapter.py:289-296`, schema `consolidation_agent.py:1346`) | — |
| `context` missing from the vector-search projection | projected (`firestore_repo.py:388`) | — |
| no tenant check on `update_fact`/`merge_facts` by id | foreign fact = `not_found`; merge skips foreign sources (`fact_management_adapter.py:272-281,385-390`) | — |
| a state-only update bumped `version` | `version` bumps only on a content change (`fact_management_adapter.py:312-336`) | — |
| active-fact projections named a nonexistent `updated_at` | they read `last_updated` (`firestore_repo.py:239,490`) | — |
| 3 `reported_date` values stored as strings | — | converted to timestamps |

Post-backfill audit: 0 violations of the flag and vector invariants. Backup: `scripts/memory/fact_s1_backup_2026_10_21.json`
(gitignored). The 88 legacy 2026-02 documents with no `state` were deleted by the owner (§8).

### 1.2 Still open: expiry is recorded and never enforced

- **`expires_at` is never computed.** It is empty on every document. No code writes or reads it for facts (Appendix A).
- **The lifecycle job was never built.** Nothing moves a fact from `current` to `stale` or `archived`. The 55
  stale/archived facts got there only because the consolidator chose `update_fact(state=ARCHIVED)` on its own.
  **62 current facts were past their own TTL** (29 ephemeral, 33 dynamic).
- **`ttl_days` is whatever the model writes, and nothing reads it.** The tool schema says "auto-calculated from
  temporal_class" (`consolidation_agent.py:1318`); nothing calculates it. Ephemeral: 14 (44 facts), 30–120 (25), none
  (22). Dynamic: none on 96. Stable: 180–730 on 20, although the old RFC says stable facts never expire.
- **A class is not an expiry.** Class-based TTL would archive "the owner and his partner took a trip, Feb 13–16"
  (`dynamic`, 90 d) and "the owner works on his employer's apps" (`dynamic`, 90 d): one is history and never stops
  being true, the other is true until the owner changes jobs. Only the content says when a fact stops being true.
- **The old RFC contradicts itself.** §2.2/§4.4 tie expiry to the class; §4.5 stales every fact after 30 days without
  an update, including PERMANENT ones.

### 1.3 Still open: versioning does not exist (closed by S2, see §9 "S2 as built")

- `update_fact` overwrites the document in place (`fact_management_adapter.py:258-354`, TD-4). `version` reached 23 on
  one fact, and none of the 22 earlier texts survives. Old texts cannot be recovered; history starts at S2.
- `lineage_id` is unique per document: no lineage has two documents. `replaces_fact_id` is empty everywhere.
- `merge_facts` supersedes its sources **before** it creates the merged fact (`:376-395` then `:425-430`): a failed
  create leaves the sources hidden with no successor, and the merged fact has no link to them.
- The model compensates by keeping history inside the text ("auto-recharges $30.79 (Sep 5), $30.33 (Sep 25)…"),
  which grows on every update.
- The only serialization of writes is an in-process lock per input id (`:219-231`); two instances or two passes on
  one account can interleave.

### 1.4 Still open: field and prompt defects

- The prompt offers context priority `HISTORICAL` (`CONSOLIDATION_TAXONOMY.groovy:137`, `CONSOLIDATION_POLICIES.groovy:49`,
  `CONSOLIDATION_TOOLS.groovy:44`); the enum has `ARCHIVAL` (`domain/entities.py:61`). A `create_fact` with `HISTORICAL`
  fails with `ValueError`. 0 of 87 live priority values in 30 days were `historical`.
- `reported_date` has no owner: the model writes content dates into it (the trigger fact's `2026-06-01` for "(Jun
  2026)"), every token example hard-codes `"2026-02-16T10:00:00"` (`CONSOLIDATION_TOOLS.groovy:72,106,140`,
  `CONSOLIDATION_EXAMPLES.groovy:63,134,281`), the tool token says "Always update to now" (`CONSOLIDATION_TOOLS.groovy:89`),
  and since S1 code sets it to now on every content change (`fact_management_adapter.py:338-341`).
- S1 residuals (`fact_s1_defects.md`): `updates.tags` replaces tags wholesale and an explicit null writes `None`, so
  the `anchor` tag that anchors are looked up by (`consolidation_agent.py:1529,1541`) can be dropped;
  `_TrackingFactManagement.update_fact` records the input id and records content even for a failed update
  (`consolidation_agent.py:78-86`).

Appendix A maps every significant field to the code that reads it. The rule for this RFC and its plan: no field
changes meaning, gains a writer or loses one without its row in that map being re-checked against the code.

## 2. Goals and non-goals

**Goals**

1. Every fact carries an expiry decision taken by the model from its content, and code enforces it.
2. A fact past its expiry is visible to no reader until someone decides what it is now.
3. That decision is taken by the model in a dedicated periodic pass (every 3 days by default), independent of conversation volume; when the model
   cannot decide, it asks the owner in chat and acts on the answer later.
4. Every content change keeps the previous version, and search always uses vectors of the current text.
5. A failure of any part is loud and bounded: no write path can hide a fact without a successor, and a broken review
   pass cannot keep hiding facts.

**Non-goals**

- Semantics of state facts (weight, balances, "programs 12 h/day as of Feb"): what "as of" means to Smart and whether
  Smart should see the date. Separate research (owner, 2026-10-21). This RFC only gives such facts a review path.
- Browsing versions, and a "what did I used to…" search. The data model must allow it later. (A read-only view of
  stale and recently archived facts is proposed for S4, pending the owner, §8 P3.)
- Changing how facts are extracted (Stage 1) beyond the lifecycle fields and the memory-review exception (D7).

## 3. Kinds of facts, from the live data

| Kind | Examples (live, paraphrased) | When it stops being current | Expiry answer |
|---|---|---|---|
| Past event | "had an appointment on Apr 16", "a trip Feb 13–16", "a trip in 2013" | never; it is history | none |
| Planned event | "checkup Sep 25, bring ID", "a visit Sep 30–Oct 3", "a festival Sep 7" | after its date; the plan is dead, an outcome may exist | date |
| Validity or deadline | "a service deletes videos ~Apr 27", "a residence document renewal due Oct 1", "passport expires 2028" | after the date the "valid / pending" claim is false | date |
| Current activity (ephemeral) | "refactoring the doc pipeline (Jun 2026)", "exploring a grounding API" | silently; no end date is said | horizon |
| Changing state | weight, balances, subscriptions, "programs 12 h/day" | becomes doubtful, not false on a date | horizon |
| Stable / permanent | job, address, birthdate, marriage | only by an explicit change | none |
| Noise that passed the filter | "a transfer ready for pickup", "testing token rotation" | days | horizon (short) |
| Standing directive, principle | `agent_directive` domain, `principle` type, `mindset` tag | only by Stage 2b curation | none, pinned by code (D1) |

A **date** is a calendar date stated or implied by the content. A **horizon** is "if the owner has not said this again
within N days, it needs review". **None** means no expiry; change happens only through an explicit update.

## 4. Decisions

### D1. The model decides expiry from content

Tools take an `expiry` object:

```
expiry: {"kind": "date", "date": "2026-09-25"}     // planned event, deadline, validity end
expiry: {"kind": "horizon", "days": 14}            // ephemeral activity, state, noise
expiry: {"kind": "none"}                           // past event, stable, permanent
```

**When it is required, accepted, refused.**

| Call | `expiry` |
|---|---|
| `create_fact`, `merge_facts` | required |
| `update_fact` whose result is a `current` fact with a content change (text, tags, metadata, temporal class) | required |
| `update_fact` that revives (`stale` → `current`) | required (a revive without a new expiry is born expired, D2) |
| `update_fact` with only `expiry` (and/or `reported_date`) | accepted; a lifecycle write, in place (D8) |
| `update_fact` to `stale`, `archived`, `invalidated`, `superseded` | refused (tool error) |

- A missing or malformed `expiry` where it is required is a tool error the model must correct, not a default. Today
  22% of facts have no TTL; silence must be impossible.
- On a content change the model decides again: a rewrite can turn a plan into history ("checkup occurred…" → `none`).
- **Pinned in code:** a fact with domain `agent_directive`, type `principle` or tag `mindset` takes `expiry: none` only;
  anything else is a tool error. Their lifecycle stays with Stage 2b (`STANDING_DIRECTIVES_RFC.md`), which reads only
  `current` directives (`consolidation_agent.py:798-843`); a directive that went stale would stop binding Smart and
  leave Stage 2b's view. The one-time classify (§7) skips them.
- **The model may not set `STALE`.** `update_fact(state=STALE)` is refused. Only the janitor stales, and it stamps
  `stale_since`. (Today `Taxonomy_Enforcement` lists STALE as a valid state, `CONSOLIDATION_POLICIES.groovy:48`, and
  `update_fact` accepts any `FactState`; a model-set STALE would have no `stale_since` and never be reviewed.)
- Search results given to the consolidator carry the current `expiry_kind`, `ttl_days` and `expires_at` (D2
  projections), so it decides with the old decision in view.
- `temporal_class` stays, unchanged. Appendix A shows that no code decides anything by it: it is written, shown to the
  model in search results, and read by nothing else. The prompt keeps class → typical answer guidance, as guidance.
  Any future reader of it goes through Appendix A.

### D2. Code owns the lifecycle fields; each field keeps one meaning

| Field | Meaning | Written by |
|---|---|---|
| `expires_at` | the moment the fact goes due; the janitor queries it | code: `date` → end of that day in the owner's timezone; `horizon` → `reported_date + days`; `none` → null |
| `ttl_days` | the horizon in days; null for `date` and `none` | code, from `expiry`. A model-sent `ttl_days` is accepted and ignored (deploy tolerance, §7.1) |
| `expiry_kind` (new) | `date` / `horizon` / `none`; needed to recompute and to show the old decision | code, from `expiry` |
| `reported_date` | when the owner last stated this; the base of a horizon | code (rules below) |
| `last_updated` | when the document last changed, for any reason | code |
| `stale_since` (new) | when the janitor marked it due; orders the review queue | janitor only |
| `superseded_by` (new) | the id of the document that replaced this one; null unless `state=superseded` | code, on supersede and merge |
| `lifecycle_events` (new) | capped array, last 20, of `{at, from, to, actor, reason}` | code, in the same write as each transition |
| `created_at` | **lineage birth**; copied to every version | code |
| `valid_from` / `valid_to` | when this version became / stopped being the current truth (`valid_from` = version birth) | code, on every transition (`lifecycle_fields`) |

**`reported_date` is code-owned.** The model can suggest it only where it knows when the owner spoke:

| Path | `reported_date` |
|---|---|
| Stage 1 create / update | the model's value clamped into the batch's message time span; default: the batch's last message time. The tool handler receives the span from `_execute_deliberate_process_v3` (messages carry timestamps) |
| Stage 1 re-mention of an existing horizon fact (exact duplicate) | `update_fact({reported_date})`, in place, clamped like above; moves the horizon (§6 rule) |
| Email consolidation | the email date: the model's value clamped into the span of the batch's email dates; default: the newest |
| Stage 2a, Stage 2b, review rewrites, classify | any model value ignored; preserved |
| Review revive on evidence | the later of its own value and the evidence fact's `reported_date` |
| Review revive on the owner's reply | the reply's message time (recorded with the question, D7) |

This replaces the S1 rule "content change → `reported_date = now`" (`fact_management_adapter.py:340-341`). The S1 test
pinning it changes by reviewer decision (§10.6).

**No expiry may be born due.** On create, content update, revive and rewrite, an `expiry` that resolves to
`expires_at <= now + 1 day` is a tool error. The error text tells the model what to do instead: a plan whose date is
past or today is history (`none`) or needs a short horizon of at least 2 days. Classify (§7.3) is exempt: it records
the decision for existing facts, and the ones already due are what the migration exists to find. Without this rule a
revive that keeps an old `reported_date` computes an `expires_at` in the past, and the fact is staled and revived
every day on an Opus pass.

**Timezone.** "End of day" uses `UserBotConfig.timezone` (`domain/user.py:279`) of the account's owner (the user with
role `owner` in `BillingAccount.iam_policy`, `domain/billing.py:114`). The consolidation tool handler passes it to
the pure domain function `domain/fact_lifecycle.py::compute_expiry(expiry, reported_date, tz, now)`.

**Model and projections.** Every new field is a `FactEntity` field with a default (`domain/entities.py:97-175`;
`FactEntity` drops unknown stored fields on read, and `FirestoreRepository.update_fact` writes a full `set()` of
`model_dump()`, `firestore_repo.py:434-453`, so a field outside the model is erased by the next update). The fields
the consolidator and the review read are added to the `search_facts` select (`firestore_repo.py:384-391`),
`EnrichedFact` (`domain/search.py:11-28`), both enrichment mappers (`search_enrichment_service.py:392-407,446-463`)
and the consolidator result dict (`fact_management_adapter.py:105-119`). Lifecycle transitions (janitor, revive,
archive, invalidate, Cabinet) use a field-masked `update()`, never a `set()` of a projected read.

### D3. Every write path validates everything before its first write

All arguments, `expiry` included, are parsed and validated before any embedding call or write, on create, update,
merge and classify. S1 already does this for enums and dates on update (`fact_management_adapter.py:298-310`); D3
extends it to every path.

- **Merge** pre-generates the merged id, embeds first, then writes in **one transaction**: create the merged fact and
  close every source (`state=superseded`, `superseded_by=merged.id`, `lifecycle_fields`). A failure anywhere writes
  nothing. Today the sources are superseded first and a failed create orphans them (§1.3).
- **Version write** (D8): same shape, one transaction creating the new version and closing the old one.
- Both need a new repository/port method. `FactWriteService.add_facts_batch` cannot be used: it always mints a new
  `lineage_id` (`fact_write_service.py:169`) and generates the id itself.
- The legacy v2 path (`_synthesize_session_facts_v2` → `add_facts_batch`, used when `prompt_version != v3`,
  `consolidation_agent.py:288-388`) goes through the same validation. Deleting it is a separate decision.

### D4. A daily janitor, zero LLM

Cloud Scheduler, daily at a daytime hour in Europe/Madrid (`--time-zone`, like the billing summary job; the question
message follows it) → `/worker` `task_type=fact_lifecycle_sweep`:

- **Source:** one collection-wide query `state == "current" AND expires_at <= now`, index `(state, expires_at)`,
  grouped by account. (`sweep_consolidation` enumerates the batch queue, `worker_handler.py:340-360`, and is no model
  here.)
- **Per account, breaker first:** the janitor stales nothing for an account that has stale facts and whose last
  successful review pass is older than 72 h, or whose stale backlog is over 50. It posts an ops alert instead. Every
  day of a broken review pass would otherwise hide more facts with no one to bring them back (the consolidation
  model has known stall and billing failure modes). During the first drain after janitor-on the backlog cap
  throttles new stales; that alert is expected then.
- **Stale:** `state = stale`, `stale_since = now`, a `lifecycle_events` entry, in place (D8), as a field-masked
  update with a precondition (`state == current` and unchanged `update_time`); a conflict skips that fact until the
  next run. The janitor never archives, never deletes, never reads text.
- **Cache:** for each account where it staled anything, `refresh_biographical_context_cache(account)` (D5).
- **Enqueue** one `fact_review` task for the owner when any of: stale facts without a decision and without an open
  question; a question with `pair_consolidated_at` set and still open (D7, pending P1); a delivered question past its
  timeout. `deadline_seconds=1800` (the consolidation precedent, `gcp_task_queue.py:464`); `fact_review` is deduped by
  task name like `agent_execution` (`main.py`, `decisions/worker_task_dedup.md`).
- One summary log line per account (`[FactLifecycle] account=… staled=N review_enqueued=…`) and the daily ops line
  (D10).
- Log-only mode: `FACT_LIFECYCLE_SWEEP_DRY_RUN`, an optional knob read with `os.getenv` **in `composition/`** and
  injected (REQ-ARCH-17 forbids env access in services, handlers and agents; precedent `CONSOLIDATION_STATS_ALERTS`,
  `user_agent_factory.py:584`).
- Wiring: the job goes in `scripts/infrastructure/cloudbuild_scheduler_jobs.sh` (where the consolidation sweep job
  lives, `:160-180`), `docs/07_deployment/SCHEDULERS.md`, the `WorkerHandler` dispatch (`worker_handler.py:166-211`)
  and the dispatch list in the root `CLAUDE.md`. The main service's repository has the biographical context service
  injected; `refresh_biographical_context_cache` skips silently without it (`firestore_repo.py:805-810`).

**No Firestore TTL policy on the facts collection, ever.** Nine other collections in this database have an ACTIVE TTL
policy on a field named `expires_at`, which deletes the document (checked 2026-10-21; the facts collection has none).
A policy added to the facts collection by analogy would physically delete every due fact. The janitor is the only
actor on `expires_at`; a test pins the field name and the decision record says why. The same warning applies to the
new question collection: its retention field is `purge_after`, not `expires_at`.

### D5. Who sees what

| Reader | current | stale | archived / invalidated / superseded |
|---|---|---|---|
| Biographical baseline: the cache document (`user_context_col/{account}`), built by `refresh_biographical_context_cache` from `get_active_facts_ordered` (`firestore_repo.py:781-842`), read by Smart, Router, Quick, Lelik, PromptBuilder and the consolidator (`smart_response_agent.py:430`, `router_agent.py:321`, `quick_response_agent.py:182`, `lelik_persona_service.py:48`, `prompt_builder.py:160`, `consolidation_agent.py:234`) | yes | **no**, after the refresh | no |
| Smart / Router enrichment, `search_memory`, remote MCP (`search_facts`, `search_facts_by_domain`) | yes | **no** | no |
| Lelik voice domains (`get_active_facts_ordered`, `companion_context_assembler_service.py:91`) | yes | **no** | no |
| Cabinet facts list (`get_active_facts`, `get_paginated_facts`) | yes | no (P3: a read-only view) | no (P3) |
| Consolidator Stage 1/2a search (`search_existing_facts`) | yes | **no** (Q1) | no |
| Stage 2b (`get_active_facts_ordered(domain=agent_directive)`) | yes | never stale (D1 pin) | no |
| Review pass (D6) | context | yes | context (versions) |

Every query filters `state == "current"` (`firestore_repo.py:223,277,383,475,962`), so STALE needs no new filter. The
leak is the cache document: it is rebuilt only at the end of a consolidation pass (`consolidation_agent.py:337,406,
495,617`), so a staled fact would stay in every prompt, with its id, until the next consolidation, which can be days
on a quiet account. With its id in the consolidator's own prompt, Stage 1 could also `update_fact` it and bypass the
review. Therefore **the janitor, the review task and Cabinet invalidate refresh the account's cache document after
any state change they make.**

No in-process copy needs clearing: every reader above fetches the cache document per request. The in-process
biographical component `PromptBuilder._get_biographical_component` (`prompt_builder.py:225-264`), which keeps a copy
"until explicit invalidation", has no caller. If it is ever wired, it gets a 15-minute TTL, because the janitor and
the review run on another instance than the chat.

"Stale reaches no one" (owner, 2026-10-21) applies to agents, the consolidator's own extraction included (Q1). A
re-mention of a stale topic therefore makes Stage 1 create a new current fact; the review pass finds it as a
neighbour of the stale one and supersedes the stale fact with it. One extra step, never a loss.

### D6. A periodic review pass decides what a stale fact is now

`ConsolidationAgent` gets a fourth pass, `review_stale_facts`, run by the `fact_review` task.

**Cadence (owner, 2026-10-21).** The review pass, and with it the question message, runs **every 3 days by default**,
set at system level and overridable per account and per user, through the existing USER → ACCOUNT → SYSTEM resolution
(`ConfigurationService`, as the biographical cache limits are resolved): a system default
`DEFAULT_FACT_REVIEW_INTERVAL_DAYS = 3`, an optional account default, an optional `UserBotConfig` field. The janitor
stays daily (zero LLM, cheap); it enqueues a review for an account only when that account's interval has elapsed since
its last successful review, or when a recorded reply or a question timeout is waiting. A stale fact can therefore stay
hidden for up to one interval before its review: that is the intended cost of the lower cadence. The breaker (D4)
uses the interval: it trips when the last successful review is older than `interval + 72 h`. The steady-state cost
falls to about a third of §7's daily estimate.

**Wiring.**

- **Own prompt profile and blueprint, `consolidation_review`.** It shares `CONSOLIDATION_TAXONOMY` (one definition of
  states, priorities, kinds) and adds a new token `CONSOLIDATION_LIFECYCLE` with the review rules. It is **not** in the
  shared `consolidation` profile (`prompts_snapshot/profiles/consolidation.yaml`), which every Stage 1/2a/2b pass
  reads; there the rules would cost tokens on every pass and describe a tool those passes do not have.
- **Pass-scoped tools.** `ask_owner` is declared only on this pass. Today one tool list serves every pass
  (`_get_tool_declarations()`, `consolidation_agent.py:904,1259`); it gains a pass parameter.
- **Task:** `review_stale_facts` added to `can_handle` (`consolidation_agent.py:185-192`) and the `execute` dispatch.
- **Context:** the `fact_review` handler calls `ensure_agents_for_user` and sets `RequestContext`, as
  `ConsolidationService.process_user_batches` does (`consolidation_service.py:71-77`); `execute` requires an
  effective account id (`consolidation_agent.py:222-227`) and `search_facts` raises without one.
- **Owner:** facts are per account; questions, sessions, language and timezone are per user. The owner is the user
  with role `owner` in the account's `BillingAccount.iam_policy` (`domain/billing.py:114`). Solo today; the rule
  is stated because the code is multi-tenant.
- **Selection:** a new repository query `state == "stale"` ordered by `stale_since` ascending, index
  `(account_id, state, stale_since)`. At most `REVIEW_BATCH` facts (default 20). **Excluded:** facts with an open
  question that has neither a recorded reply nor a timeout (they would refill the batch every day and invite
  duplicate questions).
- **Prefetched in code, not by tool turns:** per fact, its expiry decision, `reported_date`, `stale_since`, previous
  versions (`get_lineage`, D8), the nearest current neighbours with their `created_at` and `reported_date`, and any
  question about it with the owner's recorded reply (D7). The pass has its own turn budget (config
  `review_max_turns`, default 15), separate from the consolidation passes (`agent_config.py:168`).
- **Lease:** the pass holds the account's memory-write lease (D9).
- **Timeouts** of delivered questions are applied in code inside the task before the model pass, with no LLM call.
- At the end, the task refreshes the cache document (D5) and writes its outcome counters (D10). The 📊 line of
  `CONSOLIDATION_STATS_ALERTS` carries the pass label `review`, so it can be filtered.

**Outcomes, per fact:**

| Outcome | When | Effect | `expiry` | `reported_date` |
|---|---|---|---|---|
| revive | still true: the owner confirmed, or a neighbour confirms it | `state=current`, in place | required; no born-due (D2) | moves to the evidence (D2 table) |
| rewrite | can be restated as what is now true ("the Sep 25 checkup took place; result in fact X") | new version (D8), `current` | required | preserved |
| archive | it was true and is over (a dead plan, an ended activity) | `state=archived`, `valid_to=now`, in place | refused | preserved |
| invalidate | it was never true | `state=invalidated`, `valid_to=now`, in place | refused | preserved |
| supersede | a current fact already says the newer thing | `state=superseded`, `superseded_by=<that fact>` | refused | preserved |
| ask | the answer would change what Smart says, and the pass cannot decide | a question (D7); the fact stays stale | — | — |

A revive or rewrite names its basis (`evidence_fact_id` or `question_id`); code reads the evidence date from it. A
revive without a basis keeps the old `reported_date` and is therefore refused by the born-due rule unless its new
horizon is long enough. Dead plans with no outcome are archived without asking (pending P2).

On an ordinary day 0–5 facts go stale. After the one-time classify the backlog drains at 20 per review run (every 3
days by default; the owner may shorten the interval for the account while the backlog drains).

### D7. Questions to the owner, answered asynchronously

When the review pass cannot decide, it calls `ask_owner(fact_ids, question, fallback)`.

**Asking: not through `notify()`'s history path.** `notify()` stores the model turn as Smart's ≤60-word summary
(`history_summary = response_summary`, `user_notification_service.py:438,452`), the consolidation serializer feeds
model parts by `text` (the summary) in SUMMARY mode (`consolidation_serialization.py:38`, used at
`overflow_routing_service.py:94`), its history write swallows errors (`:455-456`), and it sends before it writes. So
Stage 1 would see a playful retelling, not the facts, and a failed write could not stop a resend. The review task
therefore writes and posts the message itself:

1. **Claim the day.** Create-if-absent `{prefix}fact_review_messages/{user_id}:{local_date}` (owner's timezone). The
   pass that loses the claim sends nothing. At most one message a day, at most 5 questions (owner, Q4); with the
   default cadence that is at most one message per review run, every 3 days.
2. **Records `pending`.** One record per question in `{prefix}fact_review_questions`: `question_id`, `account_id`,
   `user_id`, `fact_ids`, `text`, `fallback`, `status`, `created_at`, `session_id`, `asked_at`,
   `pair_consolidated_at`, `reply_text`, `closed_at`, `closed_by`, `purge_after`. `ask_owner` on a fact that already
   has an open question is a tool error.
3. **Append the pair** to the primary session (`UserNotificationService.resolve_channel`, the same channel used to
   post) with `append_messages_batch(..., raise_on_error=True)` (`ports/session_store.py:30-41`; precedent
   `MemoryFactService.confirm`, `memory_fact_service.py:66`):
   - user part: a code literal frame carrying the question ids and no fact text, e.g.
     `[System: memory review q=<id1>,<id2> — Alek asked the owner about facts it could not confirm]`. A user part
     reaches the consolidator as the owner's own words, so it must not carry Alek's restated facts;
   - model part: `text` = `full_text` = the verbatim numbered questions, written by the review pass in the owner's UI
     language (USER → ACCOUNT → SYSTEM resolution), each quoting the fact's stored text verbatim. Nothing is summarized,
     so Smart and the consolidator see exactly what was asked.
   If the append fails, nothing is posted, the records are marked `not_asked`, the claim is released, and the facts
   are eligible on the next run.
4. **Post** the same text with `notify_raw` (`user_notification_service.py:142-189`, returns whether it was sent).
5. **Mark `asked`**, `asked_at` = delivery time.
   If the post fails after the append: append a second pair, `[System: the questions above were not delivered]` +
   a code literal model line, mark the records `not_delivered`; they are eligible again the next day.

Smart gets one protocol line: a memory-review pair in history is a question Alek asked; answer the owner's reply
naturally; Smart does not write memory.

**Answering.** The owner replies in normal chat, in his own words, whenever he likes. The questions are numbered, so
"1 yes, 2 no, 3 never was" is a valid reply.

**Resolving: ordinary consolidation (owner, Q2), plus a recorded reply (pending P1).**

- The pair and the reply leave the window with the next overflow batch (median 6.4 h, up to 30 h). Stage 1 extracts
  what the owner said as a normal fact, restated in full from the question and the reply: "User still takes protein
  X (confirmed Oct 2026)". Stage 1 cannot see the stale fact (Q1), so this is a new current fact, and the review pass
  finds it as a neighbour created after `asked_at`. A framed **negative** answer is recorded the same way, as a
  restated fact ("User stopped taking protein X (Oct 2026)"); today the live rule handles "I stopped…" as an update of
  the existing fact (`CONSOLIDATION_TAXONOMY.groovy:301-335`), which Stage 1 cannot find under Q1.
- **Recorded reply (P1, pending).** When `ConsolidationService` processes a batch that contains a memory-review frame,
  code sets `pair_consolidated_at` on those questions and stores the owner's messages that follow the frame in that
  batch verbatim in `reply_text` (bounded: the first 5 user messages, 2,000 characters; zero LLM). The review pass
  then closes the question from that evidence too, so a "no" or "never true" closes even when Stage 1 wrote nothing.
  `pair_consolidated_at` is also the janitor's "answered" signal (D4).
- **Echo-trap exception.** The rule "never process ASSISTANT statements as facts unless the USER confirms with NEW
  info" drops a bare "yes". It lives in three live tokens: `CONSOLIDATION_POLICIES.groovy:21` (`Domain_Scope`),
  `CONSOLIDATION_TAXONOMY.groovy:189` (`Trivial_Exclusions`) and the worked example
  `CONSOLIDATION_EXAMPLES.groovy:157-210` ("ASSISTANT: … USER: yes" → DISCARD), which has the exact shape of a
  memory-review yes. All three get the exception, scoped to pairs carrying the memory-review frame, and EXAMPLES gets a
  counter-example. A reply to a memory-review question is the owner confirming or correcting a fact on request: new
  information (true as of the reply). The question alone, unanswered, creates nothing (`MEMORY_FACT_CONFIRMATION_RFC.md`
  §2). Before the token change, a Stage 1 dry run on synthetic yes / no / never-true replies (§7.4).

**Timeout.** A question unanswered 14 days after **delivery** (`asked_at`; owner, Q3; `QUESTION_TIMEOUT_DAYS`)
resolves in code to its fallback. An undelivered question never times out, so a fact cannot be archived on a question
the owner never saw. Fallback policy, pending P2: for facts true until changed (current activity, changing state,
stable-as-dynamic), `revive with a longer horizon, ask again at most once`, never archive; `archive` is the default
only for planned events, deadlines and noise. Code refuses `fallback=archive` on a fact whose `expiry_kind` is
`horizon` unless the pass marks it as noise. A fallback revive is the one horizon that does not count from
`reported_date`: its base is the time of the fallback (no one spoke, and counting from the old date would be born
due); `reported_date` is unchanged and the event's actor is `timeout`.

**Residuals.**

- Question and reply in different batches: Stage 1 sees a bare reply without its question and writes nothing; with
  P1 the recorded reply still closes it, without P1 it times out to its fallback.
- A reply in another channel lands in another session (`session_id = user:channel`) and is not recorded by P1.
- Smart sees only the last 30 messages, so the pair can leave its view before the owner replies (the same gap as
  `MEMORY_FACT_CONFIRMATION_RFC.md` §2).
- The reply reaches Stage 1 on whatever boundary the batch window gives (70/50 on the live account).
- Measured after rollout: questions closed by reply vs by timeout, and the partial-answer rate of numbered replies
  (D10).

### D8. Real versions: content changes are versions, lifecycle changes are not

- **A content change is a new version.** `update_fact` that changes text, tags, metadata or temporal class writes a
  new document: new `id`, same `lineage_id`, `version + 1`, `replaces_fact_id = old.id`, `created_at` copied from the
  old document (lineage birth), `valid_from = now`, **three vectors recomputed** (S1 recipe). The old document becomes
  `state=superseded`, `is_current=False`, `valid_to=now`, `superseded_by=new.id`. One transaction (D3, D9).
  (Domain and priority are not changeable through `update_fact`: the schema has no such fields,
  `consolidation_agent.py:1331-1353`. Stage 2b demotes a directive by create + invalidate.)
- **A lifecycle write is in place:** current ↔ stale, archive, invalidate, an `expiry`-only change, a
  `reported_date`-only re-mention, classify. Same document; `version` does not change; `expires_at` is recomputed.
- **Tags:** an explicit null is refused; the system tags `consolidated` and `anchor` survive a tags update (S1
  residual).
- **Forward resolution, one domain helper** in `domain/fact_lifecycle.py`, used by `update_fact`, `merge_facts` and
  Cabinet invalidate. Given a loaded document it says "this one", "follow `superseded_by`" or "error". It follows
  `superseded_by` only while `lineage_id` stays the same. A chain that ends in a merge (another lineage), a superseded
  document without a successor, or a non-current end returns a tool error naming the current id ("merged into X —
  search again"). The accepted end state is `current` for consolidation passes and `stale` for the review pass. The
  consolidator often writes one fact twice in a pass (seen 2026-10-19: two updates 4 s apart); the second call lands
  on the current version. Without the lineage rule an update meant for merge source A would overwrite merged fact M.
- **Supersede needs a successor.** `update_fact` gains `superseded_by` (one id; a decomposition names its main
  successor). A model-set `SUPERSEDED` without it is refused. The live prompts tell the model to retire with
  `update_fact(state=SUPERSEDED)` and create the replacement separately (`CONSOLIDATION_COGNITIVE_PROCESS.groovy:97`,
  `CONSOLIDATION_TAXONOMY.groovy:246,358`); they change accordingly (§6).
- **Merge** closes each source with `superseded_by = merged.id` in the same transaction that creates it (D3). The
  merged fact starts a new lineage; provenance is the query `superseded_by == X`.
- **Cabinet invalidate** resolves forward before it writes: a page loaded before a version write would otherwise set
  `lifecycle_fields(INVALIDATED)` (`is_current=True`) on a superseded document (`firestore_repo.py:307-319`), leaving
  two `is_current=True` documents in a lineage while the current one stays visible.
- **`is_current`** keeps its S1 meaning: "latest document of its lineage". **`get_latest_fact_by_lineage`** filters
  on `state != superseded` instead (`firestore_repo.py:534-564`); its only caller is dead (Appendix A).
- **`get_lineage`** orders by `version`, filters by `account_id`, and projects without vectors. Today it orders by
  `created_at`, has no tenant filter and loads all three vectors (`firestore_repo.py:519-532`). With `created_at`
  copied, `created_at` orders nothing inside a lineage; across facts it keeps ordering the baseline, the domain
  channel and the Cabinet by when the owner first said it, so a consolidator rewrite does not jump a fact to the
  front of its domain.
- **Text history stays in the text** for time series (Smart sees only current facts, and "how did my weight change"
  needs the series). Versions are for recovery and a later archive view, not for Smart.
- **Tracker.** `_TrackingFactManagement` records the result id (`result["fact_id"]`, the new version), and only on
  success.
- Growth: one document per content update. Measured 2026-09-23 → 10-22: 72 content updates a month on the live account (rev 2 said about 35), at about 20 KB a document (three 768-dimension vectors), so about **1.4 MB a month** and about 100 new superseded documents a month. Every index on the collection starts with `(account_id, state)`, so closed documents cost nothing at query time. Negligible.

### D9. One writer per account

- **Preconditions.** A version write and a merge are Firestore transactions: read the old document(s), require the
  accepted state (D8) and an unchanged `update_time`, then write. On a conflict, re-resolve forward once; a second
  conflict is a tool error. This replaces the in-process lock (`fact_management_adapter.py:219-231`, whose comment
  already says "S2 replaces this with a Firestore precondition write"). Without it two writers that both read the
  document while it is current fork the lineage: two current documents, one pointed to by nobody.
- **Memory-write lease per account** in Firestore (the drive indexer's pattern: `lease_until`, transactional take and
  release, writes stop before `lease_until − margin` — as built, writes are bounded by the run timeout, not fenced per write; see "S2 as built" and `decisions/fact_versions_one_writer.md`), taken by consolidation (`ConsolidationService`) and by the
  review task. The loser re-enqueues itself with a delay (`enqueue_worker_task` has `delay_seconds`,
  `gcp_task_queue.py:377`; `enqueue_consolidation_task`, `:446`, gains one — not needed as built: the deferral uses `enqueue_worker_task` with a bucketed key, see D-14 in "S2 as built"). Today consolidation tasks run on a queue
  with up to 5 concurrent dispatches (`gcp_task_queue.py:129`) and the overflow and hourly sweep paths can overlap.
  The janitor and Cabinet do not take the lease; they write single documents under a precondition.
- **The tracker is a parameter.** `_execute_deliberate_process_v3` swaps `self._fact_management` on the per-user
  singleton agent and restores it in `finally` (`consolidation_agent.py:748-759`); two passes on one instance record
  each other's writes or restore the wrong object. The tool executor gets the port as an argument instead.

### D10. Observability

- **Daily ops line** (`AlertSinkPort`, permanent, posted by the janitor run for the previous day): staled, reviewed,
  outcomes by kind, questions sent / closed by reply / timed out, backlog size, oldest `stale_since` age, expiry tool
  errors. Counters live in a per-account daily document written by the janitor, the review task and the tool
  handler.
- **Alerts** (initial thresholds, tuned after a month): backlog over 50 or oldest stale over 7 days; the same fact
  revived 3 or more times in 30 days (from `lifecycle_events`); expiry tool errors over 10 a day; the janitor ran but
  no review pass completed in 48 h; the breaker tripped (D4).
- **Turn pressure:** `ConsolidationPassStats` counts tool-error turns per pass, next to the existing 🚨 max-turns
  alert.
- **Per-fact trail:** `lifecycle_events` (D2) answers "why did Smart forget X?" after the logs have rotated. A new
  version starts with its predecessor's events plus its own.
- **Owner-facing digest and Cabinet view:** pending P3 (§8).

### D11. Schema cleanups that ride along

- Context priority: `HISTORICAL` is mapped to `ARCHIVAL` in `normalize_fact_taxonomy` (`domain/entities.py:83`), and
  the prompts say `ARCHIVAL`. Lifecycle lives in `state`; priority no longer carries it. (Moved from S1 to S3 by the
  owner.)
- `reported_date` is always parsed to a timestamp at the write boundary, on every path.
- (`update_fact` accepts `metadata`: done in S1.)

## 5. Flows

**Create / merge.** Model → `create_fact(content, fact_attributes{…, expiry})` → validate everything (D3), compute
`expires_at`/`ttl_days`/`expiry_kind` (`domain/fact_lifecycle.py`), clamp `reported_date` (D2) → embed → write (merge:
one transaction with the source closures).

**Update.** Model → `update_fact(fact_id, updates{…})` → validate → resolve forward (D8) → content change? new version
in a transaction (re-embed) : in-place lifecycle write → recompute `expires_at` → tracker records the result id.

**Daily.** Scheduler → `fact_lifecycle_sweep` → ops line for yesterday → per account: breaker → stale the due facts →
refresh the cache → enqueue `fact_review` where needed → task: take the lease → apply timeouts in code → select
(excluding open questions) → prefetch → `review_stale_facts` → outcomes + at most one question message → refresh the
cache → release.

**Question.** Claim the day → records `pending` → append the framed pair → `notify_raw` → `asked`.

**Reply.** Owner replies in chat → Smart answers the turn → the window overflows → Stage 1 writes the owner's answer
as a new current fact, and (P1) code records the reply on the question → the janitor sees `pair_consolidated_at` →
the next review closes the question (supersede / archive / invalidate / revive).

**Cabinet invalidate.** Resolve forward → in place: `state=invalidated`, `valid_to=now`, `last_updated` → refresh the
cache.

## 6. Prompt changes

Firestore tokens (mirrored in `prompts_snapshot/`), the code tool schema (`consolidation_agent.py:1259-1393`), which
the model actually sees, and the code-built pass messages.

- `CONSOLIDATION_TAXONOMY`: replace the TTL column (`:78-93`) with the three expiry answers and the kinds table (§3);
  keep `temporal_class` as a description; `HISTORICAL` → `ARCHIVAL` (`:137`); STALE described as set by the system
  only (`:101`); `reported_date` = when the owner last stated it; the echo-trap exception in `Trivial_Exclusions`
  (`:189`); "I stopped…" under a memory-review frame → a restated new fact (`:301-335`); retire-as-SUPERSEDED rules
  name the successor (`:246,358`); a new rule: an exact duplicate of a horizon fact → `update_fact({reported_date})`
  (today `discard_conditions` drops it, `:373-378`, so the horizon would count from the first mention).
- `CONSOLIDATION_TOOLS` + code schema: `expiry` per D1; `superseded_by` on `update_fact`; `ttl_days` dropped from the
  model's inputs (`:45,63`; code accepts and ignores it); `reported_date` described as "when the owner said it" (`:49,
  89`); example literals `"2026-02-16T10:00:00"` replaced (`:72,106,140`); `HISTORICAL` → `ARCHIVAL` (`:44`).
- `CONSOLIDATION_EXAMPLES`: `TTL: 90 days` / `ttl_days: 90` (`:26,98,125`) → `expiry`; date literals (`:63,134,281`);
  a counter-example to `example_3_discard_echo` (`:157-210`) for a framed memory-review yes.
- `CONSOLIDATION_COGNITIVE_PROCESS` step 2: "Decide expiry from content" replaces "Determine TTL" (`:30`); the
  decomposition step names the successor (`:97`); the duplicate rule (`:73`) gains the re-mention exception.
- `CONSOLIDATION_POLICIES`: the echo-trap exception in `Domain_Scope` (`:21`); `Taxonomy_Enforcement` (`:47-54`):
  `ARCHIVAL`, STALE not model-settable.
- Code-built messages: `_build_cluster_message` (Stage 2a, `consolidation_agent.py:1111-1138`) shows each fact's
  current expiry and says a rewrite needs `expiry`; `_build_directive_review_message` (Stage 2b, `:1021-1109`) gives
  `expiry: none` on merges and `expiry` on the demotion `create_fact`; state-only invalidations stay as they are.
  `_enforce_directive_cap` (`:845-865`) checks each result: `status != "updated"` → ERROR log + ops alert (today the
  result is ignored, so a refusal would silently stop the cap).
- New `CONSOLIDATION_LIFECYCLE` token, in the `consolidation_review` profile only: D6 outcomes, the basis rule, when
  to ask (only when the answer changes what Smart would say), question style (numbered, owner's language, fact quoted
  verbatim), fallbacks.
- Smart: one protocol line about memory-review question pairs in history.
- **Do not edit** `COGNITIVE_PROCESS_CONSOLIDATION`: an inactive duplicate, referenced only by the legacy profile
  `universal_agent_v1_SYSTEM_consolidation`, not by the active `consolidation` profile.

## 7. Migration and rollout (single live environment; dry run first; owner OK before each write)

S1 steps are done: flags (182 documents) and vectors (354 documents), §1.1.

1. **Deploy order.**
   1. Create the indexes `(state, expires_at)` and `(account_id, state, stale_since)`; wait until `READY` (the drive
      rollout's step).
   2. Code deploy. It tolerates the old tokens (`ttl_days` accepted and ignored; `expiry` required, so the old tokens
      cost tool-error turns until step 3).
   3. Token upload (`snapshot_upload.py`), the same day.
   4. Scheduler job created, janitor in log-only mode.
   For the first week, watch the 🚨 max-turns count; if Stage 2a hits its 15 turns more often (it already needs ~12
   on large clusters, `agent_config.py:168`), raise its cap.
2. **Janitor log-only for one week** (`FACT_LIFECYCLE_SWEEP_DRY_RUN`): logs what it would stale.
3. **One-time classify.** The review pass in a "classify" mode gives every current fact an `expiry`. It is a
   lifecycle write: in place, no version, no re-embed; `reported_date`, `created_at` and `version` untouched; it writes
   only `expiry_kind`, `ttl_days`, `expires_at` (and a `lifecycle_events` entry). Excluded: account `SYSTEM` (prompt
   components read by `PromptBuilder.preload_components`, `prompt_builder.py:62`), directives and principles (pinned
   `none` in code). The dry run writes the proposed decisions to `scripts/memory/`; the owner reads them; apply then
   replays the recorded decisions with an `update_time` precondition from the scan (the S1 backfill pattern), so a fact
   the live consolidator changed meanwhile is skipped and re-classified, and the model runs once.
   - 3b. **Review dry run before janitor-on.** Run the review pass once over the would-be-stale set (every fact whose
     new `expires_at` is past) in dry-run mode: outcomes, questions and fallbacks go to `scripts/memory/`, nothing is
     written or sent. The owner reads it. This is the step that removes facts from view; the classify dry run alone
     does not show it.
4. **Stage 1 dry run of answers**, before the token change for the echo-trap exception: synthetic batches with a
   framed memory-review pair followed by "yes", "no, I stopped", "that was never true" and a numbered mixed reply,
   through the real Stage 1 prompt, no writes. Cost below $1; over $3 would need the owner's OK.
5. **Janitor on.** The first run stales the backlog at once; the breaker's backlog cap then throttles new stales while
   the review drains 20 a day (D4).
6. Old in-place versions cannot be recovered; version history starts at the S2 deploy.

**Audit after migration** (owner-run, read-only script): no current fact past `expires_at`; no superseded document
with `is_current=True` or without `superseded_by` (except pre-S2 ones, counted); every current non-`SYSTEM` fact has
`expiry_kind`; directives and principles have `none`; the `reported_date` distribution is unchanged from before
classify. The framework is `scripts/data_audit/` (`TEST_COVERAGE_SYSTEMIC_RFC.md` P21); S5 adds the expiry invariants to its fact checker.

**Cost** (Opus 5.5: $4 / $20 per Mtok, cache read 0.05×, write 1.25×, `billing.py:206`; consolidation system prompt
≈12k tokens plus ≈3k of baseline facts = 15k static):

| Run | Arithmetic | Estimate |
|---|---|---|
| Classify, ~564 facts in batches of 25 ≈ 23 batches | per batch ≥2 turns: input 2 × 15k static + ≈12k dynamic, output ≈7k (calls, thinking, report). Uncached $0.17 + $0.14 = $0.31; static cached $0.05 + $0.14 = $0.19 | **$4.5–7** |
| Review dry run, ≈100–150 would-be-stale facts in passes of 20 ≈ 5–8 passes | per pass ≈3 turns: input ≈100k (45k static, 55k facts + neighbours + versions + turns), output ≈8k. Uncached $0.40 + $0.16 = $0.56; cached ≈$0.39 | **$2–4.5** |
| Stage 1 answer dry run | ~6 synthetic batches × 2 turns × ≈18k input | **< $1** |
| One-time total | | **≈ $7–12; needs the owner's OK** |
| Steady state | one review pass every 3 days by default (D6), $0.3–0.8 per pass depending on the stale count | **≈ $3–8 / month** (≈ $10–25 if the interval is set to 1 day) |

## 8. Owner decisions

**Taken (2026-10-21)**

- **Q1. Stale facts are invisible to the consolidator's extraction search too.** A re-mention creates a new fact; the
  review pass supersedes the stale one with it (D5).
- **Q2. Owner replies are resolved by ordinary consolidation**, provided the question is saved in chat history and the
  orchestrator sees it (D7). Kept; P1 adds to it, it does not replace it.
- **Q3. 14 days** without an answer, then the fallback chosen when asking. Rev 2 counts the 14 days from delivery.
  The fallback policy is P2.
- **Q4. At most one question message a day, at most 5 questions.** Rev 2 enforces it with a durable claim (D7).
- **S1 defects shipped separately**, in their own urgent session: done (§1.1).
- **The 88 unclassified 2026-02 facts** were re-sent by the owner through chat consolidation (mostly duplicates),
  verified never returned to any model, and deleted (backup kept in `scripts/memory/`, gitignored).
- `HISTORICAL` → `ARCHIVAL` moved from S1 to S3 (0 of 87 live values were `historical`).

**Confirmed by the owner (2026-10-21): P1, P2, P3 below, and the review cadence (D6: every 3 days by default, system
level, overridable per account and per user).** The context is kept for the record.

**P1. Recording the owner's reply next to the question.**

- *Problem.* Under Q1 Stage 1 cannot see a stale fact. "Yes" works: Stage 1 writes a new fact from the question and
  the reply, and the review finds it. "No", "I stopped" and "that was never true" usually produce nothing Stage 1
  can write, because the live rules handle them as an update of the existing fact, which Stage 1 cannot find. Those
  are exactly the answers that should archive or invalidate a fact. Separately, nothing without an LLM can tell the
  janitor that a question was answered.
- *What changes for the owner.* Nothing he does. When his chat is consolidated, code copies his messages that follow
  the question (at most 5 messages, 2,000 characters) onto the question record. The next review reads them and
  closes the question: a "no" archives or invalidates within about a day of consolidation instead of waiting 14 days
  for the fallback. His words are stored once more, in the facts database next to the question, and purged with it
  (`purge_after`, 90 days after closing).
- *Alternative.* Q2 only. Negative answers close only by timeout, so they take 14 days and end in whatever the
  fallback is (P2); and the janitor cannot know a question was answered, so the review either runs every day while
  any question is open (cost) or notices answers only at timeout.
- *Recommendation:* confirm. It costs no LLM call, and without it the questions that matter most do not work.
- **Confirmed.**

**P2. What silence does.**

- *Problem.* Q3 archives an unanswered question's fact after 14 days, by default. Archived facts are invisible to
  every agent, the Cabinet shows only current facts, and the consolidator never sees them (Q1). Many questions will
  be about facts that are obviously still true to the owner ("you still work on those apps?"). An owner who stops
  answering for two weeks loses them silently, and he will not re-mention what he thinks the bot already knows.
  "Reversible" today means a manual database edit.
- *What changes for the owner.* For a fact that is true until changed (a current activity, a changing state, a stable
  fact filed as dynamic), silence keeps it: it comes back with a longer horizon (twice the old one, at most a year)
  and is asked about at most once more; after a second silence it stays, with no further question. Archive stays the
  default for planned events, deadlines and noise, where silence after the date means the plan is over. He is asked
  only when his answer would change what Smart says; a plan whose date has passed with no outcome is archived without
  a question. Fewer questions, and silence never removes a standing fact.
- *Alternative.* Q3 as decided: the model picks the fallback, archive by default. Fewer surviving facts of doubtful
  currency, more silent losses.
- *Recommendation:* confirm.
- **Confirmed.**

**P3. Letting the owner see what was archived.**

- *Problem.* "Stale reaches no one" was decided for agents. Applied to the owner too, he has no way to notice a wrong
  archive, by the review or by a timeout; the earlier owner remark was that a way to view the archive may come later.
- *What changes for the owner.* (a) Once a week, one message listing what the review and timeouts archived that week,
  each fact quoted, with one line on how to bring one back (say it again in chat, or the Cabinet). It is posted with
  `notify_raw` and not stored in history, so it does not feed consolidation. (b) In the Cabinet facts list, a
  read-only state filter "stale / recently archived" (a filter on the existing list), moved from "later" into S4.
- *Alternative.* No digest and no view in S4; the archive is visible only in logs and the database until a later
  archive feature.
- *Recommendation:* confirm both. If only one, the Cabinet view: it is quiet and he looks when he wants.
- **Confirmed (both).**

**Still open, not part of this RFC:** semantics of state facts (non-goal, separate research).

## 9. Delivery slices

1. **S1 — defects.** Done: merged and deployed 2026-10-21, backfill applied, audit clean (§1.1,
   `decisions/fact_s1_defects.md`).
2. **S2 — versions and one writer** (D3, D8, D9): **built 2026-10-23** (on the S2–S5 branch, deploys with S3–S5). Validation before any write; merge and version transactions with a
   pre-generated id; forward resolution helper; `superseded_by`; `created_at` as lineage birth; `get_lineage` by
   version, tenant-filtered, projected; `get_latest_fact_by_lineage` on `state`; Cabinet invalidate resolves forward;
   memory-write lease; tracker as a parameter, result id, success only; tags null refused and system tags kept. These
   fold in every S1 residual of `fact_s1_defects.md`. Record: `decisions/fact_versions_one_writer.md`; the code is
   described in the owner's map, `docs/05_building_blocks/memory/README.md`.
3. **S3 — expiry, prompts, janitor log-only** (D1, D2, D4, D5, D11, D10 counters): new fields in the model and
   projections; `reported_date` ownership; born-due rule; directive and principle pin; STALE refused; janitor with
   breaker and cache refresh; tokens and code-built messages (§6) except the review token; `HISTORICAL` → `ARCHIVAL`;
   legacy v2 path validated; email date rule.
4. **S4 — review pass and owner questions** (D6, D7, D10): `consolidation_review` profile and token; selection, prefetch,
   outcomes, timeouts in code; question delivery and claim; echo-trap exception; P1, P2, P3 if confirmed; alerts.
5. **S5 — one-time classify (§7.3), review dry run (§7.3b), janitor on.**

**S2 as built (2026-10-23).** Where this text and the code differ, the code wins and is described here. The owner
confirmed deltas D-1 to D-15 on 2026-10-23.

Deltas from the RFC text:
- **D-1** In-place lifecycle writes are transactions with the `update_time` precondition and are field-masked, already in S2 (consolidator state-only and `reported_date`-only updates, Cabinet invalidate). The RFC names only version write and merge as transactions and puts field masking in S3.
- **D-2** `create_fact`, `merge_facts` and `add_facts_batch` refuse `state=superseded`: a document must not be born closed with no successor.
- **D-3** `update_fact` refuses `tags: []` and an empty `content`, as well as `tags: null`.
- **D-4** `FactRepository.update_fact` (a whole-document `set()`) is deleted: it had no caller left and bypassed every precondition.
- **D-5** A merge source that resolves into another lineage or to a non-current fact fails the whole merge; so does a source that moved forward between the two attempts. Missing and foreign sources are still skipped silently.
- **D-6** The Cabinet accepts every end state except `superseded` (the consolidator accepts `current` only).
- **D-7** `get_latest_fact_by_lineage` returns the highest version that is not `superseded` (filtered in code) and drops the `owner_id` fallback.
- **D-8** The `$consolidate` shim (`handlers/consolidation_handler.py`) and the dead `_execute_consolidation_background` are deleted; `$consolidate` uses the one composed `ConsolidationService`, through `consolidate_now`.
- **D-9** The service bounds a run explicitly: the message `timeout_ms` is computed from the lease and `suppress_transient_retry` is set.
- **D-10** The lease is taken per batch, not per task. `$consolidate` processes one batch in its request and continues in the background.
- **D-11** Stale RFC claims are corrected (table below).
- **D-12** A lease loser defers only when the user has an open batch: `pending`, `retry_pending` or `processing`.
- **D-13** The validation of the legacy v2 path (parse before embed, `superseded` refused) moves from S3 to S2, because it lives in `add_facts_batch`, which S2 changes for every caller.
- **D-14** The deferral uses the existing `enqueue_worker_task` (delay plus a bucketed dedup key). `enqueue_consolidation_task` is unchanged.
- **D-15** Stricter argument checks than S1: a `reported_date` that is neither a string nor a datetime is refused (S1 ignored it), and the create path refuses empty content, non-list tags and malformed `ttl_days` or `context` before any embedding, so a legacy v2 batch with one malformed fact now fails whole. A naive date is read as UTC.

Where the built code differs from the S2 plan:
- The lease is held 16 minutes (900 s run timeout + 60 s margin). Under the lease a `processing` batch is a zombie after **60 s** (the margin), not 960 s; the sweep, which has no lease, keeps 1800 s. On both paths a zombie reset **counts the lost run** (`attempts` + 1, `failed` at 3 per round, then the sweep's bounded revivals: 12 runs in a batch's life), so a batch that kills its worker cannot loop for ever.
- Tags are compared as a multiset: resending the stored tags in another order, or with a repeated tag, is not a content change.
- `forward_step` takes the account (`origin_account_id`) and the hop count; the cap of 50 hops is raised inside it, and an account mismatch is checked first and names no foreign id.
- `ConsolidationRun` has more fields than the plan's three: `unattended`: a deferral or continuation had no task queue to go to (local runs), and `$consolidate` then tells the owner to send the command again.
- The Cabinet UI shows a fixed line on a 409 ("This fact changed in the meantime; the list was reloaded.") and reloads the list. The technical message goes into the JSON body and a WARNING log, not to the screen.
- A failing "more batches?" read after a batch answers "yes" (a spare task finds nothing and stops; a lost one would wait for the sweep). The service refuses a run timeout at or below the margin.
- `write_superseding` refuses an empty `replaced` list and a document without its three vectors. The repository maps `Aborted`, the client's `ValueError` after five aborted commits, and any error that replaced one of them to `FactWriteConflict`.
- `release` of the lease logs a WARNING when the document belongs to another owner; a missing document stays silent.
- The data audit has more classes than the plan listed: `superseded_by_cycle`, `superseded_by_on_open_doc`, `lineage_open_below_highest_version` (both shapes of a lineage with a not-closed document below its head), `last_updated_not_timestamp`; and the pre-S2 count has its own class.
- The plan's "three gathered updates of one fact: a later one may fail" is not reproducible on the test fake, and the plan's R2-M4 outcome "two content changes both apply" is **superseded**: an update with `content` or `tags` (full replacements) is refused ("changed concurrently; search again") when its second read lands on a different version than the first, so the later of two gathered content changes fails and the model must search again. Metadata, `temporal_class`, `state` and `reported_date` updates still re-resolve and apply to the newer version.
- A forwarded update result carries `resolved_from` (the id the caller sent) and a message saying the update was applied to the current version.
- `consolidate_cluster` (no live caller) was not given the tracker or the lease.
- Writes are bounded by the run timeout (lease end − margin), not fenced per write; a commit in flight at cancellation may land shortly after, which the margin covers (`decisions/fact_versions_one_writer.md`).
- After an internal error in a batch the chain of next tasks continues only when the broken batch is no longer `pending` and another batch waits; otherwise the hourly sweep retries it. `ConsolidationRun` also has `failed`, and `$consolidate` words a failed batch as "A batch failed and stays queued; it will be retried later" instead of "nothing done".
- The Cabinet invalidate route no longer wraps the cache refresh in its own `except`: the port contract says the refresh never raises (the repository logs and swallows its failures).
- **Open owner decision (F1 S-1).** A Cabinet delete from a page loaded before the fact was rewritten invalidates the **current version**, whose text the owner never saw (RFC D8 as written; the UI says "Fact deleted"). A proposal to answer 409 and reload when the text changed is waiting for the owner; until it is decided, the behaviour stands as written in D8.

Cites in this RFC that had moved or were wrong:

| RFC says | Live (2026-10-23) |
|---|---|
| lock `fact_management_adapter.py:219-231` | the lock is deleted; preconditions in `firestore_repo.py:562-603` |
| merge supersede `:376-395`, create `:425-430` | one transaction, `fact_management_adapter.py:386-427` |
| `get_lineage` `firestore_repo.py:519-532`; latest `:534-564` | `:515-528`; `:530-539` |
| Cabinet invalidate `firestore_repo.py:307-319` | `:316-337` |
| tracker swap `consolidation_agent.py:748-759` | gone: the tracker is a parameter, `:789` |
| anchors looked up at `consolidation_agent.py:1529,1541` | one reader, `_get_existing_anchors_list`, `consolidation_agent.py:1591`, on the v2 path only |
| D5: the fact id is "in the consolidator's own prompt" | false: `BiographicalFactsFormatter` renders no id; cache ids are read only by enrichment's baseline dedup (`search_enrichment_service.py:214-231`) and by the cache build |
| `update_fact` tool schema `:1331-1353` | `:1393-1419` (it now has `superseded_by`) |
| D8 growth "about 35 updates a month" | 72 content updates a month measured |

**Documentation (owner, 2026-10-21).** Before S2, an owner's map of memory, `docs/05_building_blocks/memory/README.md`,
on the pattern of the drive building block: a §0 question index, a plain-English walk-through of a fact's life from
conversation to prompt, outcome tables, timelines, costs, debugging and a code map; the four older fragments
(sliding-window consolidation, fact write service, biographical cache, search enrichment) become pointers to it. Each
of S2–S5 ends by updating it, and a newcomer review (answer the owner's questions from the document alone, then
verify against the code) closes S5.

S2–S5 land together on one branch (owner: one migration). The plan lists up front the S1 tests whose behaviour the
spec changes (§10.6), so the implementer does not touch them.

## 10. Verification

New behaviour gets new tests. Tests run on the evaluating fact-store fake where the store is involved (Appendix B.1),
never on a mocked port. The fake's transactions are pass-through today; it gains a way to fail a precondition once,
so conflict paths are testable.

1. **Domain.** `compute_expiry` per kind, end of day in the owner's timezone, horizon from `reported_date`; malformed
   `expiry` refused; born-due refused on create/update/revive/rewrite and accepted on classify; fallback revive counts
   from the fallback time; directive, principle and `mindset` facts refuse anything but `none`; STALE from the model
   refused; forward resolution: same lineage followed, cross-lineage (merge) end, superseded without successor and
   non-current end each give the error with the current id, the accepted end state per caller.
2. **Write paths (store).** Merge with a malformed `expiry` writes nothing and leaves the sources current; a failed
   merged-fact embedding writes nothing; merge sets `superseded_by` on every source in one transaction; a content
   update writes a new version (new id, same lineage, copied `created_at`, `valid_from` new, recomputed vectors) and
   closes the old one in one transaction; an expiry-only, a `reported_date`-only and a state change stay in place with
   `version` unchanged; a second update on a superseded id lands on the current version; a precondition conflict
   re-resolves once; Cabinet invalidate on a superseded id invalidates the current version and leaves one
   `is_current=True` per lineage; `get_lineage` is tenant-filtered, vector-free and ordered by version; a lifecycle
   update preserves every new field (`superseded_by`, `stale_since`, `expiry_kind`, `lifecycle_events`) through the
   next adapter update; tags null refused and `anchor`/`consolidated` kept; the tracker records the result id only
   on success; `lifecycle_events` capped at 20.
3. **`reported_date`.** Stage 1 value clamped into the batch span, default the last message time; Stage 2a/2b,
   review rewrite and classify ignore a model value; revive on evidence moves to the evidence date; revive on a
   reply moves to the reply time; email path uses the email dates.
4. **Janitor.** Stales only due current facts; idempotent; precondition conflict skips; log-only mode writes nothing;
   breaker trips on 72 h without a successful review and on backlog over 50, and alerts; enqueue only under the D4
   conditions; refreshes the cache document of every account it changed; the job's task type is dispatched.
   **Read path:** a staled fact is absent from `get_biographical_context_cached` after the sweep, and from every
   reader in D5's "no" cells (one test per read path).
5. **Review pass and questions.** Each outcome's writes, expiry and `reported_date` rule; `ask_owner` declared only on
   the review pass and refused on a fact with an open question; facts with open questions excluded from selection;
   at most one message a day under a redelivered task (claim) and 5 questions; delivery order: append failure posts
   nothing; post failure after the append writes the not-delivered pair and leaves the questions eligible; the
   stored pair: the user part has the frame and no fact text, the model part's `text` equals the posted questions
   with every fact quoted, and the serialized consolidation batch (`serialize_messages_for_consolidation`) contains
   every quoted fact and no fact text in the user role; timeout counted from `asked_at`, never for an undelivered
   question, applied without an LLM call; (P1) a consolidated batch with a frame sets `pair_consolidated_at` and the
   bounded `reply_text`; (P2) `archive` fallback refused on a non-noise horizon fact; the echo-trap exception applies
   only to framed pairs (an assistant claim confirmed by the owner outside them is still dropped); revive → janitor
   → not staled again the next day; the cache is refreshed after the pass; the lease excludes a concurrent
   consolidation, and the loser re-enqueues; `_enforce_directive_cap` alerts on a refused update.
6. **S1 tests the spec changes** (a reviewer edits them under the reviewer rule; the plan completes the list by running
   the suite against the S2/S3 change; S2 built: `test_fact_management_adapter.py` was retired and its behaviour lives in
   `tests/unit/adapters/test_fact_versions_roundtrip.py` and `test_fact_update_embedding_and_dates.py`, for example
   `test_a_content_change_resets_reported_date_to_now_on_the_new_version`): the former `tests/unit/adapters/test_fact_management_adapter.py::test_content_change_bumps_version_and_resets_reported_date`
   (D2 `reported_date`); `tests/unit/adapters/test_fact_store_roundtrip.py::test_update_content_reembeds_and_bumps_version`
   (a content change is a new document, D8); `::test_superseded_update_closes_the_version` (SUPERSEDED without
   `superseded_by` is refused, D8); the merge tests in both files (`superseded_by`, transaction order, D3).
7. **Prompt ↔ code contract** (Appendix B.2) over the changed tokens, and the "no TTL policy on `expires_at`" pin.
8. **Audit after migration** (§7).

## Appendix A. Where each fact field is read (verified against main, 2026-10-21, after S1)

**S2 update (2026-10-23):** the rows `id`, `account_id`, `version`, `created_at`, `is_current`, `lineage_id`, `replaces_fact_id`, `superseded_by` and `valid_from` were re-checked against the S2 code; the other rows keep their 2026-10-21 text and line numbers.

Production code under `src/` only; `scripts/` and tests are not processes. "Write only" means no code path reads the
field to decide or render anything. Re-check the row before changing a field (§1 rule).

| Field | Read by | Effect today |
|---|---|---|
| `id` | the biographical cache dicts (`biographical_context_service.py:176-183`); enrichment drops facts whose id is in the baseline (`search_enrichment_service.py:214-231`); consolidator search results (`fact_management_adapter.py:102`); forward resolution follows `superseded_by` ids (`firestore_repo.py:541-560`) | bio dedup; the id the consolidator writes back to. A version write changes it, so a stale id is followed forward (`update_fact`, `merge_facts`, Cabinet invalidate), and the cache is refreshed after a run that wrote, failed or not (D5) |
| `text` | everything that renders a fact, including the cache document | what Smart, Lelik, MCP, Cabinet and the consolidator see |
| `vector`, `tags_vector`, `metadata_vector` | 6 vector channels next to the domain channel (`search_enrichment_service.py:137-164`) and its semantic dedup; `add_fact_if_unique` nearest neighbour (`firestore_repo.py:677-687`) | retrieval. Since S1 recomputed on every text/tags/metadata change (`fact_management_adapter.py:318-322`) |
| `account_id` | every query; tenant check on update/merge by id (S1), on `resolve_current` (a foreign start document is "not found") and on Cabinet invalidate (`firestore_repo.py:316-322`); `get_lineage` filters it since S2 | tenancy |
| `state` | every read path filters `== "current"` (`firestore_repo.py:223,277,383,475,962`); dedup (`:680`); consolidator search results | the only visibility switch |
| `domain` | router `relevant_domains` → `search_facts_by_domain`; baseline build (biographical first, then fill; `agent_directive` → `standing_directives`); Stage 2b (`consolidation_agent.py:811-815`); Lelik `voice_fact_domains` (`companion_context_assembler_service.py:91`); MCP grouping; Cabinet filter and display; `PromptBuilder` channel split; cache document | selection and grouping |
| `context_priority` (+ `context_priority_rank`, stored only) | baseline filter `rank < 5` and order (`firestore_repo.py:483-485`); cache document; shown to the consolidator | what enters every prompt |
| `tags` | `tags_vector` input at write; baseline split (`mindset` → principles, `biographical_context_service.py:190`); anchor lookups (`consolidation_agent.py:1529,1541`); `semantic_lens` added at read by the router merge; cache document; shown to the consolidator | retrieval, principles, anchors |
| `metadata` | `metadata_vector` input at write; `search_memory` render (`memory_search_agent.py:365-366`); shown to the consolidator | retrieval, detail on explicit search |
| `context` | `search_memory` render (`memory_search_agent.py:360-361`); projected by vector search since S1 | detail on explicit search |
| `reported_date` | `search_memory` render "reported: YYYY-MM-DD" (`memory_search_agent.py:362-364`); consolidator search results (`fact_management_adapter.py:117`) | the only date Smart can see, and only through `search_memory` |
| `type` (`FactType`) | `search_memory` drops `PRINCIPLE` (`memory_search_agent.py:321`); write path adds the `anchor` tag (`fact_write_service.py:146-148`); Cabinet display; shown to the consolidator | principles excluded from explicit search |
| `temporal_class` | consolidator search results | **no code decision**; model input only |
| `version` | consolidator search results (`fact_management_adapter.py:114`); `plan_fact_update` (`fact_update_plan.py:57`); `get_lineage` order; the data audit (unique per lineage, `replaces_fact_id` target at version − 1) | counts the versions of a lineage since S2: 1 at birth, + 1 on each version document, unchanged by an in-place write |
| `created_at` | baseline order (`created_at DESC` after rank, `firestore_repo.py:481`); domain channel newest-first (`:900`); Cabinet sort (`user_cabinet_app.py:782`) and paginated order (`firestore_repo.py:287`); cache document | ordering. Since S2 it is lineage birth: a version copies it, so it orders nothing inside a lineage (`get_lineage` orders by `version`). A copied value makes ties at a page boundary of the Cabinet browser possible |
| `is_current` | no `src/` reader since `get_latest_fact_by_lineage` filters on `state`; written by `lifecycle_fields` on every transition (`domain/fact_lifecycle.py:20-26`), by the version close and the merge close (`closure_fields`); the data audit checks it | "latest of its lineage"; false only on a superseded document |
| `lineage_id` | forward resolution (a hop that leaves the lineage is a merge end, `fact_lifecycle.py:56`); `get_lineage` and `get_latest_fact_by_lineage` (no production caller; the former caller in `prompt_builder.py` is gone); `PromptBuilder.preload_components` cache key for `SYSTEM` facts (`prompt_builder.py:62-67`); the data audit | the lineage rule of D8 |
| `last_updated` | projected by `get_active_facts` and `get_active_facts_ordered` (`firestore_repo.py:239,490`; since S1, they named `updated_at` before); read by no code | write only; projected so a read does not default it to now |
| `visibility` | defaulted on read; never filtered | not enforced on reads, by design: every fact of an account is visible to all its members (owner, 2026-10-21); out of scope |
| `created_by_user_id` | ownership migration on read (`firestore_repo.py:121-157`) | attribution only |
| `valid_from`, `valid_to` | none (`valid_to` written by `lifecycle_fields` since S1; `valid_from` set at birth and by the version write) | **write only** |
| `ttl_days`, `expires_at` | none; `expires_at` is never written for facts | **write only** |
| `replaces_fact_id` | written by the version write (`fact_update_plan.py:58`); read by the data audit only (target in the same lineage, at version − 1, pointing back with `superseded_by`) | audit only |
| `superseded_by` (S2) | written by the version close, the merge close and a model `SUPERSEDED` in place (`closure_fields`, `fact_lifecycle.py:73-75`); read by forward resolution (`resolve_current`) and the data audit | the successor of a closed document; a merge successor is in another lineage |

**Projections** (what each read carries, for D2):

| Read | Carries | Lacks (relevant here) |
|---|---|---|
| `search_facts` select (`firestore_repo.py:384-391`) → `EnrichedFact` → consolidator dict (`fact_management_adapter.py:105-119`) | text, type, state, domain, tags, priority, valid_*, created_at, version, metadata, temporal_class, reported_date, context, vector | `expiry_kind`, `ttl_days`, `expires_at`, `stale_since`, `superseded_by`; the dict also drops `context` and `created_at` |
| `get_active_facts` / `get_active_facts_ordered` (`:232-242`, `:486-494`) | the above minus vector and context, plus last_updated, is_current, language | the new fields |
| cache document dicts (`biographical_context_service.py:176-183`) | id, text, domain, tags, context_priority, created_at | everything else |
| `get_fact_by_id`, `get_lineage` | the whole document, vectors included | — |

Consequences for this RFC: `state` stays the single visibility switch, so STALE needs no change to any query, only
the cache refresh (D5) and tests that pin it; `expires_at`, `ttl_days`, `valid_*` and `replaces_fact_id` can take their
new meanings without breaking a reader; `temporal_class` keeps its only role; `created_at` keeps ordering facts by
first mention once it is the lineage birth; the new fields must be added to the projections the consolidator and the
review read.

## Appendix B. Test requirements from the blind-spot analysis (2026-10-21)

The fact-lifecycle tests stopped at mocked ports: `consolidation_agent.py` and `fact_write_service.py` had 100% line
coverage while `fact_management_adapter.py` had 15% and `firestore_repo.py` 19–25%; replacing 12 lifecycle methods with
functions that raise left the same 486 tests green. Every defect in §1 lived behind a port. This RFC's tests therefore
add, before or with the code:

1. **A fact-store suite on an evaluating fake** (the drive index already has one that evaluates filters, order,
   missing fields and projections): write → read round trips for text ↔ vectors, `state` ↔ `is_current`/`valid_to`/
   `superseded_by`, `stale_since`, stored types; ordering of every query; a missing field never matching an equality
   filter; projections carrying every field a reader renders; tenant isolation on by-id loads. S1 started it
   (`tests/support/fact_store.py`, `tests/support/firestore_eval.py`, `tests/unit/adapters/test_fact_store_roundtrip.py`);
   S2–S4 extend it, including a precondition that can fail once (§10).
2. **Prompt ↔ code contract tests** over `prompts_snapshot/`: every enum value a `CONSOLIDATION_*` token names exists;
   every tool field a token promises exists in the code schema and is applied by the handler.
3. **An offline consolidation → store test** with a scripted LLM through the real adapter and the evaluating fake.
4. **A field registry test** pinning, per fact field, its writers and readers (Appendix A as code), so a write-only or
   never-written field fails the build.
5. **A text hash** (`text_sha256`) written with the vectors, so text ↔ vector consistency is checkable offline and by the
   owner-run audit.

Full report: owner-local, `scripts/memory/test_blind_spots_report_2026_10_21.md` (gitignored).

## Appendix C. Execution order across this RFC and TEST_COVERAGE_SYSTEMIC_RFC (owner-approved, 2026-10-21)

The slices here run as the main stream after the test foundation, because S2 (transactions, versions) needs the full
evaluating Firestore fake and its parity check against real Firestore:

1. Foundation (test RFC P1, P2, P3; the owner's test-infrastructure step).
2. Test RFC P4 (fake) → P5 (parity) → P8 (Consolidation and Tasks prompt vocabularies, before any consolidation token
   change) → P21 (data-audit framework, used by §7's audit).
3. The owner's map of memory (§9, documentation) → S2 → S3 → S4 → S5 (after the owner has read the dry-run outputs);
   each slice updates the map.

A security-and-breadth stream from the test RFC runs in parallel. The live status of every item is kept in the
project's working notes, not in this file.
