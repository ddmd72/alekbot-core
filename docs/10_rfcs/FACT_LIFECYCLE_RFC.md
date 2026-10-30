# RFC: Fact lifecycle — expiry decided by content, a daily janitor, a periodic review pass, owner questions, real versions

**Status:** Accepted for implementation (owner, 2026-10-21): rev 2, independent review applied, owner confirmations in §8. S1 is live; **S2, S3 and S4 were deployed 2026-10-27** (the real versions and lease, expiry decided from content, the review, owner questions and digest are on; the janitor was log-only until S5; see "S2 as built", "S3 as built", "S4 as built" in §9); **S5's classify was applied 2026-10-27** (see "S5 as built" in §9) and the owner's re-review of closed facts applied; **the janitor writes since the deploy of 2026-10-27** (`FACT_LIFECYCLE_SWEEP_DRY_RUN=0`; rollback = remove the variable, a deploy). **P2 revised by the owner (2026-10-27):** a fact whose date has passed is no longer archived without a question; what happened, and what was only intended, becomes history, and archive is kept for noise and what the owner archives; never-true stays invalidated. The review change this needs is **deployed** (see "P2 revised" in §9); the janitor was switched on after it shipped.
Rev 1 had owner decisions Q1–Q4; two adversarial reviews (code paths; rollout and steady state) and a binding
arbitration produced this revision. Slice S1 (§9) is merged and deployed; S2–S4 are deployed (2026-10-27) with the janitor log-only; S5's classify was applied and the janitor switched on on 2026-10-27.
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
- **A class is not an expiry.** Class-based TTL would archive "the user and Clara took a weekend trip, Mar 6–8"
  (`dynamic`, 90 d) and "the user works as a project manager" (`dynamic`, 90 d): one is history and never stops
  being true, the other is true until the user changes jobs. Only the content says when a fact stops being true.
- **The old RFC contradicts itself.** §2.2/§4.4 tie expiry to the class; §4.5 stales every fact after 30 days without
  an update, including PERMANENT ones.

### 1.3 Still open: versioning does not exist (closed by S2, see §9 "S2 as built")

- `update_fact` overwrites the document in place (`fact_management_adapter.py:258-354`, TD-4). `version` reached 23 on
  one fact, and none of the 22 earlier texts survives. Old texts cannot be recovered; history starts at S2.
- `lineage_id` is unique per document: no lineage has two documents. `replaces_fact_id` is empty everywhere.
- `merge_facts` supersedes its sources **before** it creates the merged fact (`:376-395` then `:425-430`): a failed
  create leaves the sources hidden with no successor, and the merged fact has no link to them.
- The model compensates by keeping history inside the text ("phone plan tops up $20 (Mar 1), $25 (Apr 1)…"),
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

- Semantics of state facts (a reading streak, balances, "practises guitar 1 h/day as of Mar"): what "as of" means to Smart and whether
  Smart should see the date. Separate research (owner, 2026-10-21). This RFC only gives such facts a review path.
- Browsing versions, and a "what did I used to…" search. The data model must allow it later. (A read-only view of
  stale and recently archived facts is proposed for S4, pending the owner, §8 P3.)
- Changing how facts are extracted (Stage 1) beyond the lifecycle fields and the memory-review exception (D7).

## 3. Kinds of facts, from the live data

| Kind | Examples (invented, shaped like the live data) | When it stops being current | Expiry answer |
|---|---|---|---|
| Past event | "had a team offsite on May 12", "a weekend trip Mar 6–8", "a trip in 2015" | never; it is history | none |
| Planned event | "car service Nov 4, bring the logbook", "a conference Nov 18–20", "a concert Dec 5" | after its date; the plan is dead, an outcome may exist | date |
| Validity or deadline | "a cloud trial ends ~May 20", "a library card renewal due Nov 30", "a gym contract ends 2028" | after the date the "valid / pending" claim is false | date |
| Current activity (ephemeral) | "refactoring the doc pipeline (Jun 2026)", "exploring a grounding API" | silently; no end date is said | horizon |
| Changing state | a reading streak, balances, subscriptions, "practises guitar 1 h/day" | becomes doubtful, not false on a date | horizon |
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
- On a content change the model decides again: a rewrite can turn a plan into history ("the car service took place…" → `none`).
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
| Merge (any pass) | never earlier than the newest source fact: the later of the model's span-clamped date (Stage 1 and email) and the latest `reported_date` of the sources; a cleanup pass takes the latest source date |
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

*As built (S4), the cadence is daily, the review revives only on evidence or a reply, and what it cannot decide it asks about
through a queue; "S4 as built" in §9 lists every difference from the text below.*

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
| rewrite | can be restated as what is now true ("the Nov 4 car service took place; result in fact X") | new version (D8), `current` | required | preserved |
| archive | it was true and is over (a dead plan, an ended activity) *(revised 2026-10-27, see §9 "P2 revised": a plan whose date passed is rewritten as history; archive is for noise, an end the owner states, or the Cabinet)* | `state=archived`, `valid_to=now`, in place | refused | preserved |
| invalidate | it was never true | `state=invalidated`, `valid_to=now`, in place | refused | preserved |
| supersede | a current fact already says the newer thing | `state=superseded`, `superseded_by=<that fact>` | refused | preserved |
| ask | the answer would change what Smart says, and the pass cannot decide | a question (D7); the fact stays stale | — | — |

A revive or rewrite names its basis (`evidence_fact_id` or `question_id`); code reads the evidence date from it. A
revive without a basis keeps the old `reported_date` and is therefore refused by the born-due rule unless its new
horizon is long enough. Dead plans with no outcome are archived without asking (pending P2). *(Revised 2026-10-27, see §9
"P2 revised": a plan whose date passed becomes history, on evidence, on its own text, on the owner's answer, or as "unknown"
on silence; it is never archived without a question.)*

On an ordinary day 0–5 facts go stale. After the one-time classify the backlog drains at 20 per review run (every 3
days by default; the owner may shorten the interval for the account while the backlog drains).

### D7. Questions to the owner, answered asynchronously

*As built (S4), one fact is asked per message, the question text is written by code, and an answer is only a reply to the
question message, a button or a Cabinet click, applied by code; "S4 as built" in §9 lists every difference from the text below.*

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
  what the owner said as a normal fact, restated in full from the question and the reply: "User still takes evening
  pottery classes (confirmed Oct 2026)". Stage 1 cannot see the stale fact (Q1), so this is a new current fact, and the review pass
  finds it as a neighbour created after `asked_at`. A framed **negative** answer is recorded the same way, as a
  restated fact ("User stopped taking pottery classes (Oct 2026)"); today the live rule handles "I stopped…" as an update of
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
due); `reported_date` is unchanged and the event's actor is `timeout`. *(Revised 2026-10-27, see §9 "P2 revised": archive
is no longer the default for planned events and deadlines; their fallback is `history`, the question's "unknown" text written
as history, and `archive` is only for noise.)*

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
- **Text history stays in the text** for time series (Smart sees only current facts, and "how did my savings change"
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

**Order set by the owner (2026-10-23, S3 planning).** Steps 2, 3, 3b and 5 above run in this order: the classify (dry run → the
owner reads it → apply), then the review dry run (3b), then the janitor stays **log-only on the classified facts for as long as
needed** (the owner reads `would_stale` per account and the breaker each morning; "one week" in step 2 is a minimum, not a
rule), and the janitor is switched on **only on the owner's word** (`FACT_LIFECYCLE_SWEEP_DRY_RUN=0`, a deploy). Step 5's "the
first run stales the backlog at once" is bounded since S3-D10: at most `50 − backlog` per account per run, oldest expiry
first, the rest deferred. The owner's rule for classify and the review: **when unsure, keep the fact** (classify defaults to
`none`; the review revives the fact or asks the owner). Classify must accept a filtered set, to re-classify the documents an
old revision rewrote during a rollback window.

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
- **Revised 2026-10-27** (owner; see §9 "P2 revised"): a plan whose date has passed is no longer archived without a
  question, and archive is no longer the default for planned events and deadlines. What happened, and what was only
  intended, becomes history (a rewrite at `archival` priority); the owner is asked when nothing in memory decides the
  outcome, and silence writes "whether it took place is unknown". Archive is kept for noise and what the owner archives;
  never-true stays invalidated.
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
3. **S3 — expiry, prompts, janitor log-only** (**built 2026-10-23**, D1, D2, D4, D5, D11, D10 counters): new fields in the model and
   projections; `reported_date` ownership; born-due rule; directive and principle pin; STALE refused; janitor with
   breaker and cache refresh; tokens and code-built messages (§6) except the review token; `HISTORICAL` → `ARCHIVAL`;
   legacy v2 path validated; email date rule.
4. **S4 — review pass and owner questions** (D6, D7, D10): **built 2026-10-26** (on the S2–S5 branch, deploys with S5).
   `consolidation_review` profile and token; selection, prefetch, outcomes, timeouts in code; the question queue and its
   delivery; echo-trap exception; P1, P2, P3; the owner's answers by digit, button, reply and Cabinet; alerts. Record:
   `decisions/fact_review_and_owner_questions.md`; the code is described in the owner's map, §5.5 and §5.6.
5. **S5 — one-time classify (§7.3), review dry run (§7.3b), janitor on.**
6. **P2 revised (owner, 2026-10-27)** — a fact whose date has passed becomes history: built, not deployed; it ships, with its
   `CONSOLIDATION_LIFECYCLE` upload, before the janitor is switched on. See "P2 revised" below.

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
- `ConsolidationRun` has two fields beyond the plan's three (`processed`, `has_more`, `deferred`): `unattended` (a deferral or continuation had no task queue to go to, in local runs, so `$consolidate` tells the owner to send the command again) and `failed` (a batch ended in an agent failure, an internal error or a setup error; `$consolidate` then says the batch failed and stays queued).
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
- **Owner decision (F1 S-1), decided 2026-10-23, built in S3** (below): a Cabinet delete from a page loaded before the fact was rewritten no longer invalidates a current version whose text the owner never saw; until then the behaviour was D8 as written.


**S3 as built (2026-10-23).** On the S2–S5 branch, not deployed (it ships with S4 and S5 in one switch window). Where this
text and the code differ, the code wins and is described here. The owner's map is `docs/05_building_blocks/memory/README.md`
(§2.7, §3.11, §5.5); the decision record is `decisions/fact_expiry_and_janitor.md`; the deploy steps are in
the rollout notes (ops).

*Two owner decisions of 2026-10-23, built first.*
- **Cabinet delete of a stale row (F1 S-1).** `invalidate_fact` follows the id forward as in D8, but when the walk moved
  and the version it ends on has a different text from the requested document (a closed version keeps its own text, so
  that is what the page showed), it raises `FactResolutionError` ("rewritten after the page loaded; reload"); the route
  answers 409 and the page reloads. Nothing is written. A newer version with the same text is invalidated at once. The
  check runs on both attempts, so a version written between the resolve and the write is caught too.
- **A consolidation batch whose user has no profile fails.** The account comes from the profile only: a missing profile
  (possible while the user's agents are still cached) fails the oldest pending batch ("user has no profile"), like a blank
  account; no lease is taken under the user id and nothing is written. `user_repo` is a required argument of
  `ConsolidationService`. Before, the run leased and wrote under `account_id = user_id`.

*Owner decisions taken before and during the build.*
- **S3-D1** born-due also applies to a change that only sends `expiry` (in place). **S3-D2** the `reported_date` rules where
  D2 is silent (below). **S3-D3** `STALE` is refused from the model on create and merge too, not only on update.
  **S3-D4** the legacy v2 path writes facts with no expiry decision; its model `ttl_days` is ignored and `STALE` refused
  there as everywhere. **S3-D5** a pinned fact (directive, principle, `mindset`) that arrives without `expiry` gets `none`
  filled in. **S3-D6**, widened by the owner: the consolidator sees Smart's own timestamp format (year, weekday, the owner's
  zone), produced by **one shared domain formatter** that Smart's `_inject_timestamps` and the consolidator both call, plus a
  `Today` line on every pass. **S3-D7** (option a) the janitor stores its counters in log-only mode too; the benefit is a
  plumbing check, not a preview, because before S5's classify no current fact has `expires_at`. **S3-D8** the five prompt
  tokens are uploaded before the switch. **S3-D9** `compute_expiry` takes no `now`; the janitor is log-only unless the knob is
  explicitly off.
- **S3-D10 (2026-10-23, after the T9 review):** the breaker reads only the backlog before the run, so one run could stale any
  number of facts. The janitor now stales at most `50 − backlog` per account per run, oldest `expires_at` first; the rest is
  counted `deferred` (snapshot field, ops line, log line, totals). At a backlog of 50 nothing is staled. In S3
  `last_review_at` is none, so any backlog trips the breaker and the cap bites only from an empty backlog.
- **Q4 (b):** a horizon that resolves already due at write time on a **cleanup pass** (Stage 2a, Stage 2b, the directive
  backstop, and `consolidate_cluster`, which has no live caller but is a cleanup pass by the same `model_dates=False` rule) is
  not refused: `expires_at` = now + 2 days, kind and days kept, with a note, so the fact reaches review soon. A past `date` is
  refused on every pass. Stage 1 and email keep the refusal. **The extension rule** in the Stage 1 and email tokens: when the
  conversation confirms an existing fact held all along, extend it with `update_fact({reported_date})`; when the link is
  unclear, create a new fact and leave the old one; cleanup passes never extend; a RAG echo stays DISCARD.
- **Q5:** a fact a cleanup pass creates (a decomposition part, a demotion) inherits the `reported_date` of the facts named in
  an optional `derived_from` (the latest), else the batch end. **Q6 (a):** a deploy-step script stamps `stale_since` on stale
  facts without one. **Q7:** the S5 order (below). **Q8:** a mandatory model dry run of the new tokens and schema before the
  deploy, owner-run on the Mac, up to $30. **Q9:** an `expiry` sent with a closing state is ignored with a note; the janitor
  skips due facts with no `expiry_kind`.
- **Owner rule carried to S4 and S5:** when unsure, keep the fact. S5's classify defaults to `none` when unsure; S4's review
  revives a fact or asks the owner when unsure.

*Deltas from the RFC text.*
- **D1, what the consolidator sees.** Search results carry `expiry` in the shape the model sends (`{kind: date, date}` with
  the last day, `{kind: horizon, days}` plus a read-only `due`, `{kind: none}`, or `null` without a decision), `reported_date`
  as an ISO time with the owner's offset, and never `ttl_days`, `expires_at` or `expiry_kind`. `expiry_view` shows a `date` as
  the last day (the moment before the next local midnight, so a copy sent back keeps the same `expires_at` even on a 23-hour DST
  day; the plan said "minus one day"). A copy sent back keeps the same `expires_at` only while that date is still ahead: a
  date that is today or past is refused as born due (on a cleanup pass with the rewrite-or-future-date text, S4 as built). `EnrichedFact` and the search projection keep the raw fields; `get_active_facts` projects
  them too (the cluster auto-fetch reads them there).
- **D1, closing states.** `expiry` with `archived`, `invalidated` or `superseded` is **ignored** with a note, not refused, on an
  update and on a create or merge whose state is closing (such a create needs no `expiry`). A model `STALE` is refused on
  update, create and merge. `SUPERSEDED` is also left out of the model-facing create and merge state lists.
- **D1, input.** A top-level `expiry` (next to `fact_attributes` / `updates`) is moved into place; `days` accepts an integer, a
  whole float or a string of digits, 1 to 36 500; a `date` year must be 1900–9998. A refusal is tagged `refused: "expiry"` (a
  born-due one also `born_due: true`) and always ends "Nothing was written. Resend this call with a valid expiry before
  retiring any fact it replaces."; a refusal comes before any embedding or write.
- **D2, `reported_date` rules** (S3-D2): on Stage 1 and email passes the model's value is clamped into the batch span (never
  after now) and, for a missing one, the span end is used; cleanup passes ignore a model value (the result says so) and keep or
  inherit it; an update never moves the date back; a re-mention on a horizon fact never moves `expires_at` earlier. The v1
  rule "a content change resets `reported_date` to now" is gone. A naive time (an email date, an ISO string) is read as UTC; the
  consolidator's own rendered stamp is accepted as a `reported_date` where the owner's zone is known.
- **D2, email.** Each candidate's `date` is the owner-zone moment with its offset, copied **as given** into `reported_date`;
  the message adds that information whose date or horizon has passed is history or noise.
- **D4, the janitor.** It skips (and counts) a due fact with no `expiry_kind`; in log-only mode `would_stale` counts every due
  fact with a decision whatever the breaker says; the per-run cap above; a failed run logs, posts one 🚨, answers 500 and is
  **not retried** (a retry could overlap a running sweep); every ops post is capped at 10 s; legacy documents without an
  `account_id` are skipped by its reads; it takes no lease. Two overlapping runs can stale up to twice the cap, so do not trigger
  the job by hand while the 09:30 run is going; a real guard is S4's call.
- **D5.** The janitor refreshes the baseline of every account it staled, and every reader already filters `state == "current"`
  (one test per D5 read path).
- **D10, counters.** The daily document holds a snapshot of the run (last run of the day wins, so a hand run never
  double-counts) plus the tool handler's `expiry_tool_errors` increment; the posted-line mark lives in its own collection. The
  ops line is posted **after** the run, once a day, about that run (the RFC said "for the previous day"), plus yesterday's
  expiry tool errors. Tool-error turns, expiry errors and abandoned refusals are in the 📊 and 🚨 lines; an abandoned refusal
  posts a 🚨 even with `CONSOLIDATION_STATS_ALERTS` off; born-due refusals are counted apart and never abandoned; the pass
  close-out is capped at 10 s. S4 adds reviewed, outcomes, questions, timeouts and every alert threshold except the breaker's.
- **D11.** `historical` is read as `archival` in `normalize_fact_taxonomy`; the tokens say `ARCHIVAL`.
- **§6, prompts.** The five tokens carry the S3 contract: the kinds table and date rules, `expiry` in every create, merge and
  content-update example, `superseded_by` wherever a token retires a fact with `SUPERSEDED`, `STALE` as system-only, the
  extension rule. The code-built messages carry the expiry, `reported`, `derived_from` and `fact_attributes` lines (the Stage 2a
  text still says "with the original superseded" without the field; the tokens and the tool schema, where `superseded_by` is
  required with `SUPERSEDED`, carry that rule). The review token, the echo-trap exception and Smart's memory-review line stay
  S4's. The Stage 2a entries show `expiry`, `due` and `reported`; the Stage 2b lines name `fact_attributes`;
  `_enforce_directive_cap` no longer ignores a refused update.
- **Owner's timezone.** Resolved once per consolidation run from the account owner's profile (UTC with a WARNING when it
  cannot be); it decides the end of a `date` and every stamp.
- **Known limits.** The legacy v2 path parses `reported_date` without the zone (its stamp cannot be read back; a naive time is
  UTC), and writes no expiry. A model `reported_date` with no offset is read as UTC and can be off by the owner's offset; the
  clamp bounds it. Abandoned-refusal detection is approximate and errs towards an alert. Late Stage 1 batches (10 of 27 in 31
  days were 1–90 days late) can see a short horizon refused; the dry run's bench 1 reads a fresh window only, so this risk is
  not measured before the deploy.

*The model dry run (Q8).* The kit (`scripts/consolidation/s3_dry_run_kit.py`) runs the five benches with `--s3-kit` on the real
agent and adapter over a no-write store, the five edited tokens read from `prompts_snapshot/`, a no-write email repository, a
recording counters store and a cost meter with a $30 cumulative cap. It writes no fact, cache document, email mark, lifecycle
counter or account usage counter; `prompt_content` rows (account id empty, only when `DEBUG_PROMPTS=true` with a prompt dataset set), model and embedding
calls leave the Mac, and no Logfire span does. It needs `CONSOLIDATION__PROMPT_VERSION` v3, one bench at a time. Gates: no
`stage2a` or `cluster` pass over 13 turns, zero abandoned refusals, and the owner reads the decision table. Commands and costs:
the rollout notes (ops), step M.

*Notes for S4 and S5.*
- **S4:** the breaker's rule becomes the review interval + 72 h (D6) when `last_review_at` is wired (S3 passes none and pins
  only its own 72 h and 50 edges); the review enqueue is S4's (S3 logs `review_enqueued=0`); revives keep the strict born-due
  refusal (no floor); the janitor's overlap guard is S4's call; "revived 3× in 30 days" reads `lifecycle_events`.
- **S5 order (owner, Q7; completed 2026-10-27, the janitor writes since that deploy; rollback = remove the variable):** classify (dry run → the owner reads it → apply) → review dry run (§7 step 3b) → the janitor stays
  log-only **on the classified facts for as long as needed** (the owner reads `would_stale` per account and the breaker each
  morning) → janitor on **only on the owner's word** (`FACT_LIFECYCLE_SWEEP_DRY_RUN=0`, a deploy). S5's classify defaults to
  `none` when unsure ("when unsure, keep the fact") and accepts a filtered set, so that documents an old revision rewrote in a
  rollback window (they lose `expiry_kind`, `stale_since` and `lifecycle_events` and keep a stale `expires_at`, which the
  janitor skips) can be re-classified after the roll-forward.
- **Deploy-step script (Q6 a):** `scripts/migration/fact_s3_stamp_legacy_stale.py` stamps `stale_since = now` and one event
  (`migration`, "legacy stale, stamped at S3") on stale facts without one (2 live on 2026-10-23), nothing else, under the scan's
  `update_time`; its dry run prints counts only and writes ids and text to `scripts/memory/`; `last_updated` is left alone, the
  only age signal those facts have.

Cites in this RFC that had moved or were wrong:

| RFC says | Live (2026-10-23) |
|---|---|
| lock `fact_management_adapter.py:219-231` | the lock is deleted; preconditions in `firestore_repo.py:562-603` |
| merge supersede `:376-395`, create `:425-430` | one transaction, `fact_management_adapter.py:386-427` |
| `get_lineage` `firestore_repo.py:519-532`; latest `:534-564` | `:515-528`; `:530-539` |
| Cabinet invalidate `firestore_repo.py:307-319` | `:316-344` |
| tracker swap `consolidation_agent.py:748-759` | gone: the tracker is a parameter, `:789` |
| anchors looked up at `consolidation_agent.py:1529,1541` | one reader, `_get_existing_anchors_list`, `consolidation_agent.py:1591`, on the v2 path only |
| D5: the fact id is "in the consolidator's own prompt" | false: `BiographicalFactsFormatter` renders no id; cache ids are read only by enrichment's baseline dedup (`search_enrichment_service.py:214-231`) and by the cache build |
| `update_fact` tool schema `:1331-1353` | `:1393-1419` (it now has `superseded_by`) |
| D8 growth "about 35 updates a month" | 72 content updates a month measured |
| `FactEntity` `entities.py:97-175` | `entities.py:111-179` (S3 fields included) |
| `normalize_fact_taxonomy` `entities.py:83` | `entities.py:91-104` (`historical` → `archival`) |
| `search_facts` select `firestore_repo.py:384-391` | `search_facts` `firestore_repo.py:365` (select carries `expiry_kind`, `ttl_days`, `expires_at`) |
| search mappers `search_enrichment_service.py:392-407,446-463` | the two `EnrichedFact(` constructions, `:398` and `:455` |
| consolidator dict `fact_management_adapter.py:105-119` | `fact_management_adapter.py:134` (adds `expiry`, `due`; drops raw expiry fields) |
| S1 rule `fact_management_adapter.py:338-341` ("a content change resets `reported_date` to now") | gone; the rules are `domain/fact_write_context.py` and `fact_update_lifecycle.py` |
| `FirestoreRepository.update_fact` full `set()` `:434-453` | deleted in S2 (D-4); in-place writes `firestore_repo.py:627` (`update_fact_fields`) |
| `WorkerHandler` dispatch `:166-211` | `_dispatch` `worker_handler.py:278` (adds `fact_lifecycle_sweep`) |
| `sweep_consolidation` `worker_handler.py:340-360` | `_handle_sweep_consolidation` `:470` |
| `_build_cluster_message` `:1111-1138`; `_build_directive_review_message` `:1021-1109`; `_enforce_directive_cap` `:845-865`; Stage 2b reads directives `:798-843`; code tool schema `:1259-1393` | `consolidation_agent.py:1406`; `:1314`; `:1024`; `_review_directives` `:972`; `_get_tool_declarations` `:1600` |
| `refresh_biographical_context_cache` skips without service `:805-810` | `firestore_repo.py:753` (logs and swallows its errors) |
| `TAXONOMY:78-93` (TTL column); `TOOLS:44`, `TAXONOMY:137`, `POLICIES:49` (`HISTORICAL`) | the TTL column is now the `expiry` block (kinds table, date rules) of `CONSOLIDATION_TAXONOMY`; `ARCHIVAL` since P8, uploaded with S3 |

**S4 as built (2026-10-26).** On the S2–S5 branch, not deployed (it ships with S5 in one switch window). Where this text and
the code differ, the code wins and is described here. The owner's map is `docs/05_building_blocks/memory/README.md` (§2.7,
§5.5, §5.6); the decision record is `decisions/fact_review_and_owner_questions.md`; the deploy steps are in the rollout notes
(ops).

*Owner decisions of the build.*
- **Daily review, a question queue, silence is safe (Q6).** The review runs daily (`DEFAULT_FACT_REVIEW_INTERVAL_DAYS = 1`,
  per user `fact_review_interval_days`, no deploy needed to change it). A fact that needs the owner's answer joins a queue
  ordered by how long it has been stale, and is **current while it waits** (it keeps its past `expires_at`; the janitor skips it
  until the question is answered or times out). Code never replaces a question with a silent revive.
- **The question mechanism.** One fact per message: "Is this still true?", the fact quoted as the owner is shown it (as stored, or its translation into the message language, "Quote translations" in §9), and four options: 1 yes,
  2 no longer, 3 never, 4 don't remember. One question is open at a time; the next goes out after an answer, else the next
  local day; at most 5 a day; no quiet hours in code. An answer is **only a reply to the question message** (a Slack thread
  reply, a Telegram reply), a button click (Slack, now) or the Cabinet; any other message is ordinary conversation.
  *(Revised 2026-10-27, see §9 "P2 revised": the question line and options follow the question's kind, `still_true`,
  `happened`, `renewed` or `paid`; the four above are `still_true`'s.)*
- **Answers (Q5, Q8).** Yes brings the fact back; "no longer" archives it and, for a lasting fact, creates a separate history
  fact "No longer true (the owner said so in <month year>): …" in the same transaction; for a dated plan it only archives
  (Q8); never invalidates; "don't remember" applies the question's fallback at once. A correction is a fact about the true value.
  *(Revised 2026-10-27, see §9 "P2 revised": a dated plan is now asked "Did this happen?" (or "renewed?", "paid?") and its
  answer is written as history; Q8's archive-only "no longer" stays for a stored S4 question about a dated plan and for noise.)*
- **Language (Q3).** The response-language setting decides: fixed gives that language, mirror gives the language of the owner's
  recent messages, reported by the review pass; the question line, option labels, acknowledgements and digest come from the UI
  strings (uk, en, fr, es) or, for another language, from the model under code checks, else the UI language.
- **Q4.** The review never archives a fact as noise on its own; `noise` only decides what a question's silence does.
- **Q1a, Q1b (S3 follow-ups).** A merge is never dated before its newest source. On a cleanup pass a past-date refusal has no
  discard: "rewrite it as what happened with `{kind: none}`, or give a future date if the plan still holds", and an unanswered
  one is an abandoned refusal (🚨).
- **Q2.** No filter for Lelik in S4. The question pair stays visible to him, and an answer given on a phone call counts as
  silence (the 14-day fallback). A follow-up.
- **Q7.** The Cabinet question list is built in S4: each waiting or asked fact in full with three buttons and bulk marking; a
  click runs the same code as a chat digit. *(Revised 2026-10-27, see §9 "P2 revised": the buttons are the deciding options of
  each question's kind, the bulk bar takes one kind at a time, and the invoice digest questions are their own group.)*
- **Interactive messages (owner).** A question is a platform-neutral message (prompt, quote, options, a binding); Slack draws
  buttons, Telegram plain text with the digits; a click comes back as a choice routed by the binding's kind. Built so that Smart
  can later send blocks through the same port.
- **O1, the fact-changed line.** A digit for a fact that changed since the question was asked (or whose write conflicted) is
  not applied and is not sent on to Smart: the owner gets a neutral line ("That fact has changed in the meantime, so I didn't
  apply your answer; I'll come back to it.") and the question stays open.
- **O3, a refused click keeps its buttons.** The same line is shown on the Slack message with the options kept
  (`ChoiceOutcome.keep_options`), so the owner can click again.
- **O4, state words in the digest.** Each digest line says why the fact is listed ("no longer true" for an archive, "never was
  true" for an invalidation). This brought the label set from 13 to 15 texts (12 at the start of the build, +1 for O1, +2 for O4).
  *(P2 revised, 2026-10-27, brings it to 28.)*
- **Literal text.** Text the bot did not write (a stored fact in the digest, a model-translated acknowledgement, the receipts
  of reminders, drive and deep-research copies) is sent literally: Slack's markup characters are escaped and `mrkdwn` is off.
  Both Slack routes verify the signature on the raw bytes before parsing and cap the body at 1 MiB.

*Deltas from the RFC text.*
- **D6, cadence and selection.** Daily, not every 3 days. The janitor enqueues the review when stale facts exist after its
  run and the interval of owner-local days has passed since the last success, or when a reply in words waits, a question timed
  out, a send never finished, the digest is due, or a queued question may go out. The stale facts of an account (at most about
  50) are read and sorted in code; **no index `(account_id, state, stale_since)` is needed**. The only index S4 adds is
  `(account_id, state, valid_to DESC)` on the facts, for the "set aside lately" list.
- **D6, outcomes.** A revive needs evidence or the owner's reply (code refuses it otherwise); an ask is always allowed and
  never needs a reason; the model has three tools (`update_fact`, `ask_owner`, `count_words`) and no search, create or merge.
  A fact brought back is reviewed again N days from now, not from when the owner last said it (a horizon counts from now). Dead
  plans are archived without a question when their date has passed (the basis `date_passed`), and the first review after the
  deploy is expected to archive the two legacy dated plans that way. The "undecided twice → asked" rule (S4-D20) is by code.
  *(Revised 2026-10-27, see §9 "P2 revised": `date_passed` now backs only a history rewrite, and an archive needs the
  owner's reply; the two plans the first review archived this way are re-opened by the P2 reopen script.)*
- **D6, breaker and cap.** The breaker is the review interval + 72 h with a wired `last_review_at`. The cap `50 − pool` and the
  breaker's "over 50" count stale facts plus the facts of open questions (the pool), so waiting facts cannot grow the lifecycle
  past 50.
- **D7, asking.** `ask_owner(fact_id, fallback, fallback_date?, noise?)` takes one fact and no text; code writes the question
  and sends it, so there is no model-written sentence, no numbering, no packing of several questions. The question record has
  one `fact_id` (not `fact_ids`) and the quoted text. The daily claim `fact_review_messages/{user_id}:{local_date}` is replaced
  by the question queue and the state document's send count; that collection now holds only the weekly digest's claim
  (`{user_id}:digest:{iso_week}`). *(Revised 2026-10-27, see §9 "P2 revised": `ask_owner(fact_id, fallback, question?,
  texts?, folds?, noise?)`; the pass writes the answer texts of a `happened`, `renewed` or `paid` question, still no question
  sentence; `fallback_date` is retired.)*
- **D7, answering (P1).** The recorded reply is taken **at intake** from a reply to the question message, not copied by
  `ConsolidationService` (which is unchanged): a digit is applied by code at once; words are recorded on the question and read by
  the review once on its next run. There is no `pair_consolidated_at`. The reply and its acknowledgement are appended to
  history as one pair, `[Reply to memory review q=<id>] <text>` and the acknowledgement; Smart sees them and is not asked to
  act. The echo-trap exception is narrowed to this frame: the quoted fact belongs to the review and Stage 1 records only what the
  answer adds (a correction's true value).
- **D7, residuals corrected.** "A reply in another channel is not recorded" and "question and reply in different batches" no
  longer apply: only a reply to the question message counts, wherever it was posted, and it is applied at intake, not by a
  batch. The residual that stays: Smart sees the last 30 messages only, so the pair can leave its view before consolidation.
  A question that stays stale no longer waits "stale until answered": its fact is current while it waits (above).
- **D7, timeout (P2).** 14 days from the send, for `asked` and `unanswered` questions only; a queued question never times out. A
  fallback `revive` gives twice the window the last revive gave (14 to 365 days), **every silence again**: the RFC's "never asked
  again after two silences" is dropped, because a fact is asked again only when it falls due again, and never replaced by a silent
  revive when it next needs the owner. A fallback `archive` is for a dated plan, a past `fallback_date` or noise. *(Revised
  2026-10-27, see §9 "P2 revised": `archive` only with noise; a dated plan falls back to `history`; `fallback_date` is
  retired for new asks.)*
- **Quote translations (2026-10-27).** A message in a language other than English quotes the fact translated by the review
  pass. The pass is told the language (fixed mode: the task's `language`; mirror mode: the one it reports) and returns
  `translations` in its final report; the code keeps one only if it is one line with no `[`, link or control character, at most
  500 characters and with the source's numbers unchanged. It is stored right after the pass in `fact_review_messages` under
  `{account_id}:quote:{language}:{digest of the English text}` (keyed by the text, so any fact id with that text finds it;
  `purge_after` 180 days after the write) and shown on the question, its Slack redraws and the digest while it holds; else the
  stored English text is shown (also when a read fails). Presentation only: the fact and the question's `quoted_text` stay as
  stored. The history pair carries the shown text, so four tokens say a quote may be translated: the final-report shape in
  `CONSOLIDATION_LIFECYCLE`, the echo exception in `CONSOLIDATION_POLICIES`, the example `case_4_translated_quote` in
  `CONSOLIDATION_EXAMPLES` and the marker line in `PROTOCOL_MEMORY_REVIEW_MARKERS`. The Cabinet keeps showing English.
- **P3.** The weekly digest lists removals made by the review, a timeout or the owner, in the message language, ending "To bring
  one back, tell me in chat that it is still true." The Cabinet shows the stale facts and everything set aside in the last 30 days
  with the actor; it **cannot restore** a closed fact (the RFC's "or the Cabinet" is not built; a follow-up).
- **Cabinet question list (Q7).** Each waiting or asked fact is shown with its current text, followed forward as an answer
  would follow it. A fact rewritten since its question (or gone) shows the **question's quote**, the text the owner was
  asked about, not the new version, so nothing unseen is shown or answered; a click on such a row answers "changed" and
  reloads. A click is a direct owner action under the same preconditions as a chat digit and releases the next question when
  it answers the current one.
- **D10.** Counters, alerts and the 🧹 line are listed in the owner's map (§5.5). Thresholds: the oldest stale fact older than
  the interval + 2 days (when it starts, then weekly); more than 35 queued questions (once); no successful review for 48 h; the
  breaker at the interval + 72 h; the pool over 50; closed review records stored 3 days past their expiry; a fact revived a
  third time in 30 days.
- **Janitor guard.** The janitor takes a run guard before anything is read (a small collection, 5 minutes), so two runs never
  overlap; S3's "the job is not retried" rule stays.
- **Reply in words releases the slot (2026-10-30).** A reply in words to the current question gives up the slot in the same transaction that records it (`record_reply` returns `ReplyRecord`), so the next question goes out at once; before, it held the slot and the day's questions stopped after it. The status stays `asked`; the review still reads the words on its next run.
- **Other collections.** `fact_review_questions` and `fact_review_messages` carry `purge_after` (a Firestore TTL policy on that
  field deletes closed records 90 days after closing, digest claims 90 days after the claim and quote translations 180 days after they were written; nothing named `expires_at`);
  `fact_review_state` and `fact_lifecycle_run` have no TTL policy (the no-TTL test lists them).
- **Config.** `UserBotConfig.fact_review_interval_days` and the same field on the account defaults.

*Appendix A, rows touched by S4.* `stale_since` is cleared whenever a fact comes back to current (revive, rewrite, queue, a
fallback revive, an owner's yes) and kept on a fact the review or the owner sets aside; the review also reads it for the queue
order and the Cabinet for the stale view. `state` gains the review's and the owner's writers (`stale → current`, `→ archived`,
`→ invalidated`, `→ superseded`, and `current → archived|invalidated|current` for a waiting fact); every reader still filters
`current`, so a waiting fact is an ordinary current fact. `reported_date`: a revive on evidence takes the later of the stored
date and the evidence date, on a reply or an answer the later of the stored date and the answer time; a rewrite, a queue
and a fallback keep it. `expiry_kind`, `ttl_days`, `expires_at`: a revive counts the horizon from now, with `ttl_days` stored
so that `expires_at = reported_date + ttl_days`; a queued fact keeps its past `expires_at`. `content`: the review's rewrite is a
version, and the history fact of an "ended" answer is a new fact in its own lineage. `lifecycle_events`: actors `review`, `owner`,
`timeout` and `review_fallback`; read by the revive alert, the digest (last actor) and the Cabinet (last actor). `valid_to` is read
by the "set aside lately" list and the digest. `vector` is read by the review's neighbour query.

*Notes for S5.*
- Classify accepts a filtered set; S4 never assumes an `expiry_kind`. Revives keep the strict born-due rule.
- The pool bound makes the drain safe: classify feeds the review at most 50 facts at a time (about three days of review passes
  of 20), then questions go one at a time, up to 5 a day; the Cabinet answers any number at once. A question nobody answers
  closes 14 days after it was sent.
- The review dry run of §7 step 3b is bench 7 of the model dry run (`test_review_dryrun.py`); the answers bench (bench 6) is
  the Stage 1 check of the echo-trap exception.
- S5's expiry audit gets three invariants: a current fact has no `stale_since`; every horizon has `expires_at = reported_date +
  ttl_days`; while the janitor writes and after that day's run, a current fact past its `expires_at` belongs to an open question.
- Follow-ups outside S4: a call note that carries an answer to a question; a Cabinet "bring back" for a closed fact; Telegram
  inline keyboards on the same port; Smart sending interactive blocks through it.

**S5 as built (2026-10-27).** Classify, the audit's expiry classes and the bench 7 fix are built; classify was applied on 2026-10-27, and the
janitor has written since that day's deploy.

- *The script.* `scripts/migration/fact_s5_classify.py`, owner-run, with its pure rules in `fact_s5_classify_rules.py`. Two modes.
  **Dry run** (the default) scans the current facts (reads only), checks the live prompt (below), asks the live consolidation
  agent, and writes the owner's report and a replay plan under `scripts/memory/`; it writes nothing to the database. **Apply**
  (`--apply <plan>`) reads a plan, makes no model call and costs nothing. Scope: a current fact with an account that is not
  `SYSTEM` and no `expiry_kind`; a fact decided since the 2026-10-27 switch is `already_decided`; an account whose owner cannot be
  resolved is skipped. A filtered mode (`--ids-file`) applies the same rules to the listed ids, for the documents an old revision
  rewrote in a rollback window and for the facts apply skipped because they changed after the scan.
- *Why one structured call and not the tool loop.* The consolidation tool path applies rules classify must not: born-due
  refuses a past `date` and floors a due horizon, and the `reported_date` rules clamp or ignore the field. The facts classify
  exists to find are already due, so the tool path would refuse them or invent an `expires_at`. Classify makes one single-turn
  call per batch of 25 through the live agent's own call path (its provider, model pin and failover), with a JSON response
  schema; the facts are numbered inside the batch and the model answers by number. The answer goes through the tool's own
  parser and `compute_expiry`, so no rule has a second copy. The agent has no cross-provider failover, so a response served by
  a fallback is a stop. `--expect-model` is required: the run refuses unless every account's agent runs that model.
- *The instruction.* A tracked text file next to the script, outside `prompts_snapshot/` (a file inside would be drift for the
  snapshot check and deleted by the next pull; nothing is uploaded). The system prompt is the taxonomy's `temporal_classes` and
  `expiry` blocks verbatim, then the instruction. Before the estimate the script reads the live `CONSOLIDATION_TAXONOMY` token
  and refuses to run unless it equals the snapshot file, so "the text the live consolidator follows" is checked at every run. A
  missing instruction file or a block not found exactly once stops the run before any call. The plan records the digests.
- *What it writes.* Per fact, one field-masked in-place write of `expiry_kind`, `ttl_days`, `expires_at` (computed from the stored
  `reported_date` in the owner's zone; this also clears the legacy `ttl_days` of date and none facts) and one `lifecycle_events`
  entry `current → current`, actor `migration`, reason `expiry classified at S5 (<source>)`, which no S4 reader takes for a revive
  or a window. `reported_date`, `state`, text and vectors stay untouched, and so does `last_updated` (§7.3 says "only"; the
  field has no reader in `src/`). Directives, principles and `mindset` facts get `none` by code, with no model call. No lease:
  a precondition per document, as the janitor and the Cabinet write.
- *A date already past when stated.* A `date` answer before the local day of the fact's `reported_date` becomes `none` in
  code, source `past_at_report`: the fact recorded something already over when it was said (the taxonomy's "past event →
  none"), not a plan. The instruction says so too; the first dry run had 4 such answers among 45 dates.
- *Unsure → none.* A decision flagged `unsure` becomes `none` in code, whatever kind was sent. An answer that is missing, repeated or
  refused by the parser is asked again once for those facts only; still unusable it becomes `none` with a code
  (`missing`, `invalid_expiry`, `no_reported_date`). Every such decision shows in the report's flag column and in the summary.
- *Stop rules.* A call that raises is repeated once, and a second raise stops the run; so does a fallback-served response and a
  budget refusal. Facts not yet decided are **not** written as `none`: they are listed as not decided (ready for `--ids-file`), the
  failing batch is listed apart, and the plan holds only what was decided. A `none` written for an outage would be a real
  decision that the scope rule then protects.
- *Cost.* Without `--budget-usd` the run only estimates. The cap is lifetime over classify's own ledger (all its runs), with no
  default. The estimate on the live volume (about 500 facts to the model, 21 batches) is about $2–4 on Opus 5.5 and $1–2 on
  Sonnet 5.5, against the $4.5–7 the tool loop was estimated at.
- *Apply is an exact replay.* The whole plan is validated before the first write; each entry's fields are checked through the
  domain (`stored_expiry`, `expiry_refusal`). The scan records each document's change token and apply writes under it, the recorded
  fields and not a recomputation: what the owner read is what is written. A fact gone, no longer current or changed since is
  `skipped_changed` and listed for `--ids-file`; a re-run of the same plan reports `already_applied` and writes nothing; an
  unreadable document is `failed` (its type only) and the loop goes on.
- *Dry run, then the review dry run.* The review dry run of §7 step 3b is bench 7 of the model dry run. Its G5 detector no longer
  counts the history fact of an "ended" answer as a revive without a basis (`unbased_revives` excludes the domain's own
  `history_reason("")` prefix, next to the queued prefix it already excluded). Bench 7 stages a sample, not the whole set of
  facts that classify would make due; the janitor's pool cap bounds the first live wave to at most 50 facts, oldest expiry first.
- *The audit.* `python -m scripts.data_audit.run facts` gains `check_expiry` beside `check_fact`, with five classes:
  `current_without_expiry_kind`, `pinned_expiry_not_none`, `current_with_stale_since`,
  `horizon_not_counted_from_reported_date` and `current_past_expiry_not_waiting`. Two flags set when they are judged.
  `--expiry-required` (after classify is applied) makes a current fact without `expiry_kind` a violation; without it the fact is
  counted. `--janitor-ran-at <time>` (while the janitor writes, after that day's run) judges a current fact past its `expires_at`
  that no open question holds. A run whose pool cap deferred facts, or whose breaker held an account back, lists those facts in
  that class too, so the report asks to compare the count with that run's `deferred` and breaker. The horizon rule is relaxed to
  `expires_at >= reported_date + ttl_days`: the cleanup floor and a re-mention make it later by design; earlier is the defect.
- *Deltas from the text above* (each decided by the arbiter): (1) §7.3's "classify mode of the review pass" is a standalone script
  with one structured call. (2) The scope is the current facts with no `expiry_kind`. (3) Directives, principles and `mindset`
  get `none` written by code. (4) Documents with no `account_id` and accounts with no resolvable owner are skipped as well.
  (5) `last_updated` is not written. (6) The trail entry has the shape above. (7) Unsure → `none`, but no answer at all → not
  decided and the run stops. (8) The instruction suspends the two "Today" date rules and sends no Today: classify decides as of
  `reported_date` (the born-due exemption of owner decision 3), so a plan whose date has passed keeps `date` and falls to the S4
  review. (9) The live taxonomy must equal the snapshot. (10) A fact changed since the scan is listed and re-classified by the
  owner with `--ids-file`, not automatically. (11) The cost figure above replaces the RFC's. (12) The audit's "no current fact past
  `expires_at`" takes the S4 form, judged only with `--janitor-ran-at`. (13) The horizon invariant is `>=`. (14) "`reported_date`
  distribution unchanged" is not an audit class (no baseline); the apply test pins that apply writes no `reported_date`. (15) The
  missing-expiry class is a violation only with `--expiry-required`. (16) Bench 7 stages a sample (see above). (17) Switching
  the janitor on is a separate change, made only on the owner's word. (18) Classify has no throttle of its own; the janitor's pool
  cap feeds the review at most 50 facts at a time.

**P2 revised (2026-10-27).** Deployed 2026-10-27; the janitor was switched on after it shipped. Where this text and the code
differ, the code wins and is described in *As built* at the end of this section. The owner's map is `docs/05_building_blocks/memory/README.md`
(§2.7, §5.5, §5.6, §7, §9.5); the decision record is `decisions/fact_review_and_owner_questions.md` ("Revised 2026-10-27").

**P2 revised (owner, 2026-10-27): a fact whose date has passed becomes history.** Built on `feat/facts-p2-history`. It
replaces P2's "a plan whose date has passed with no outcome is archived without a question" (§8 P2), D6's "Dead plans with no
outcome are archived without asking", S4 as built's `date_passed` archive and Q8's "a dated plan is archived only", and the
review token's lines that say the same. The janitor was switched on only after this shipped.

*The owner's rule.* Anything that really happened is a fact; an intention is a fact too ("planned it, did not go"); a bill or
a payment is a fact of past life with a very low priority. So a stale fact whose date has passed is rewritten as **history**,
in one of three phrasings:
- **happened**: "In Nov 2026 the user had the car serviced (Nov 5).";
- **planned, did not happen**: "The user planned to attend a conference Nov 18–20, 2026, and did not go.";
- **planned, unknown**: "The user planned to attend a concert on Dec 5, 2026; whether it took place is unknown."

What is in memory picks the phrasing; otherwise the owner is asked; **silence gives the "unknown" phrasing**, never an
archive and never a second question. A validity end (a document, a permit, a membership, a trial) is asked "renewed?",
because the answer changes the current fact. Archive is kept for noise and for what the owner himself archives;
never-true stays `invalidated`; a duplicate is still superseded.

**P2-1. History is a rewrite: a new version in the same lineage** (D8), not a new fact beside an archive.
- The review writes `update_fact(fact_id, {state: CURRENT, content: <history>, expiry: {kind: none}}, basis, as_history:
  true)` (`as_history` also accepted inside `updates`, like `basis`). The write path is the existing review rewrite
  (`fact_update_lifecycle._finish_review` → `_review_version`): a new document, same `lineage_id`, `version + 1`, three
  vectors recomputed for the new text, `created_at` and `reported_date` kept; the dated original becomes `superseded` with
  `superseded_by` = the history version, in one transaction. Its text (booking codes and all) survives as a closed version,
  which no agent reads.
- Why not the "ended" shape (archive + a new fact in its own lineage, S4-D27): it archives the original, which the owner now
  reserves for noise; the weekly digest lists every review or owner archive as "no longer true", so a trip that happened
  would be reported as put aside; and the lineage would split a plan from what became of it. D6's own table already defines
  the rewrite for this case ("the Nov 4 car service took place …" → new version). The S4 "ended" answer on a standing fact
  keeps its shape (arbiter, Q3).
- **`as_history` is a domain field**, `ParsedFactUpdate.as_history: bool`, parsed by `parse_fact_update`. Three checks, each
  where its inputs are: `typed_update_refusal` requires state `CURRENT`, a `content` and `expiry {kind: none}`; the content
  change itself (which needs the stored fact) is checked in `basis_refusal` and again in `_review_version`; the writer is
  checked in `finish_fact_update` (only the review, owner and timeout writers, `reviews_stored_memory`; any other pass gets a
  refusal). `plan_fact_update` (`fact_update_plan.py`) clears `metadata` to `{}` and `context` to None **before** it builds
  `fact_embedding_texts`, so the metadata vector is computed from the new text alone (they described the plan while it held,
  as `history_fact` already drops them). `_review_version` then sets on the version `context_priority = archival` (owner,
  decided: out of the biographical baseline cache, found by every search as usual, as the S4 history fact is).
  `update_fact` cannot change priority otherwise (D8 note), and only to `archival` here; bills and every other history are
  therefore equal in priority (there is no level below `archival`). A `history` text sent with a `CURRENT` rewrite is refused
  with "history goes beside an archive; to rewrite this fact as history send as_history: true".
- Text: English (stored facts are English), past tense, at most 40 words, the date kept at the precision the fact had (a
  day, a range, a month), what and where kept, identifiers dropped (booking and invoice numbers, PINs, codes, times, seat and
  baggage detail); an invoice or payment keeps its amount and due day. `temporal_class` is not touched (no reader, Appendix A).
- **Trail reasons**, one per path, all built in `fact_review_reasons.py`:

| Path | Reason |
|---|---|
| review, basis evidence | `rewritten as history on <id8>` |
| review, basis reply | `rewritten as history on reply q=<question_id>` |
| review, basis date_passed | `rewritten as history: date passed <YYYY-MM-DD>` |
| owner digit / button / Cabinet, a history outcome | `rewritten as history q=<question_id>: <happened\|not_happened\|not_needed> (<digit\|button\|cabinet>)` |
| owner, renewed | `renewed q=<question_id> (<source>)` |
| owner, not renewed | `rewritten q=<question_id>: not_renewed (<source>)` |
| owner, paid / not paid (chat or Cabinet) | `rewritten as history q=<question_id>: <paid\|not_paid> (<source>)` |
| timeout (silence) | `rewritten as history: no answer to q=<question_id>` |
| owner, "don't remember" | `rewritten as history: did not remember q=<question_id>` |

  `basis_reason` gains the history form and accepts `date_passed` for it (before P2 it raised unless the action was an
  archive). `is_rewrite_reason` (read by `gives_window_reason` and by `_review_version`'s guard) accepts the prefixes
  `rewritten on `, `rewritten as history`, `rewritten q=` and `renewed q=`; so `renewed` gives a window that a later silence
  doubles. `is_revive_reason` is false for every new reason (none starts with `revived on `, `confirmed q=`, `did not
  remember q=` or `no answer to q=`), so none counts towards the revive alert. The S4 reason `archived: date passed <date>` is
  kept in the builder (no P2 writer uses it).

**P2-2. Evidence decides the phrasing without a question.** The basis kinds stay three; what each may back changes:

| Basis | Backs (before) | Backs (now) |
|---|---|---|
| `evidence` (a current fact of another lineage; a fact this pass made current, except a `date_passed` history) | revive, rewrite, archive, invalidate | revive, rewrite, an `as_history` rewrite, invalidate; **no archive** |
| `reply` (the owner's words on the question, with a quote) | revive, rewrite, archive, invalidate | the same, plus an `as_history` rewrite; **the only basis for a model archive** |
| `date_passed` (the plan's date is before today; a `date` fact uses its own) | archive only | an `as_history` rewrite only |

- **Evidence** is a neighbour that reports the outcome ("the garage receipt for the car service on Nov 5" → happened; "sold the
  concert tickets" → did not happen). A standing fact that a neighbour shows ended is superseded by that neighbour (outcome
  (1), unchanged), never archived on it.
- **The fact's own text** is the `date_passed` case: what the stored text already reports as done (an invoice received, a
  payment made, a subscription cancelled with access until a day). `date_passed` never says that a planned event took place
  or did not, nor that an invoice was paid: that is evidence, the owner's answer, or silence. Code cannot check the phrasing:
  a prompt rule, checked by the bench (P2-7). A `date_passed` history is no evidence for another fact of the pass
  (`made_current_documents` leaves out decisions whose basis is `date_passed`, and `basis_refusal` refuses them by an explicit
  `not_evidence` set, as built). Accepted limit: a stale duplicate of a fact this pass rewrote as history on `date_passed`
  cannot be superseded by it in the same pass; it returns the next run, when the history is an ordinary current fact.
- **Invoices (owner, decided 2026-10-27).** An invoice is not a payment. A payment is confirmed only by evidence (a payment
  fact, or an indexed email or receipt that became a fact) or by the owner. So:
  - a **routine invoice** with no confirmation is rewritten on `date_passed` as "An invoice for <X> (<amount>), due <D>, was
    received; payment not confirmed." with `as_history` and `payment: {paid: <text>, not_paid: <text>}` (two more texts, same
    checks as ask texts). No chat question. After the pass, code creates a **digest question** on the written version
    (P2-3, kind `paid`, delivery `digest`), so the owner can tick it in the Cabinet;
  - a **fine or a debt** is asked individually in chat: `ask_owner(question: "paid", texts {paid, not_paid, unknown})`,
    fallback `history` (the `unknown` text says "payment not confirmed");
  - a neighbour that confirms the payment decides "paid" on evidence, with no question.
- **Parts of one plan** (the flights, the hotel and the transfer of one trip) are one question: `ask_owner` gains `folds:
  [fact_id, …]`, other batch facts with no result and no open question; the scope holds them as the ask's (no other result
  in the pass). In `_after_pass`, only after `_queue` returns True for the asked fact, code supersedes each fold by it
  (`ParsedFactUpdate(state=SUPERSEDED, superseded_by=<asked id>)`, review context, reason `superseded by <id>`); the asked fact
  is current by then, so the successor rule holds with no change to `_usable_fact_refusal`. A failed queue folds nothing
  (the folds stay stale and come back next run); a refused fold is a WARNING and stays stale. The texts of the asked fact
  describe the whole plan. Folds are left out of the scope's `kept()` (they had a result: the ask) and their `offers` entries
  are removed with the asked fact's. `folds` is **not stored** on the question: each fold's own trail entry (`superseded by
  <id>`, actor `review`) is the record. So that the parts share a batch, `select_batch` orders stale facts by (offers,
  `stale_since`, `expires_at`, id): parts staled in one janitor run share `stale_since`, and parts of one plan share their
  last day.

**P2-3. The question.** `ask_owner` gains `question`, `texts` and `folds`:

| `question` | For | Options (digits) | `texts` the pass writes now | Each answer does |
|---|---|---|---|---|
| `still_true` (default) | everything S4 asks | 1 Yes, 2 No longer, 3 Never, 4 Don't remember | none | S4, unchanged |
| `happened` | a plan or event whose date passed (a `date` fact, or a horizon plan) | 1 It happened, 2 It did not, 3 Don't remember | `happened`, `not_happened`, `unknown` | 1, 2: that text as history; 3: the fallback now |
| `renewed` | a validity end that changes the current fact | 1 Renewed, 2 Not renewed, 3 No longer needed, 4 Don't remember | `renewed`, `not_renewed`, `not_needed`, `unknown` | 1: rewrite to `renewed`, current, `confirmed_expiry` (the doubled window), priority kept; 2: rewrite to `not_renewed`, `none`, priority kept (it is the current state); 3: `not_needed` as history; 4: the fallback now |
| `paid`, delivery `chat` | a fine or a debt whose due day passed | 1 Paid, 2 Not paid, 3 Don't remember | `paid`, `not_paid`, `unknown` | 1, 2: that text as history; 3: the fallback now |
| `paid`, delivery `digest` | a routine invoice already rewritten "payment not confirmed" | Cabinet only: Paid, Not paid | `paid`, `not_paid` (from `payment`) | that text as history, over the "not confirmed" version |

- **The question record** gains `question` (`still_true | happened | renewed | paid`, default `still_true`), `texts`
  (outcome → text), `origin` (`review | kept_twice`), `delivery` (`chat | digest`, default `chat`) and `fallback_attempts`
  (int, default 0). The validator keys off `origin`: a `review` question has exactly the texts of its kind (a digest `paid`
  question: `paid`, `not_paid`); a `kept_twice` question has none and its texts are built by code (below). A `digest`
  question is never sent in chat: it is left out of the send queue (`next_in_queue`, `may_send`), the pool
  (`open_fact_ids`, `pool`), the queue alert and the janitor's skip (its fact is current with `none`, never due), and it has
  no timeout. It closes on a Cabinet answer, or `resolved_elsewhere` when its fact changes or after 90 days unanswered (the
  history already says "payment not confirmed").
- **Fallback** (`QuestionFallback`) gains `history`: the `unknown` text written as history. It is the only fallback of a
  `happened` or `renewed` question, except `archive` with `noise: true`. A fact of expiry kind `date` may be asked only as
  `happened`, `renewed` or `paid` (as built), or as `still_true` with `archive` + `noise: true`. `fallback_date` is retired for
  new asks (it existed to archive a horizon plan whose date passed); stored questions keep theirs.
- **The texts are written at ask time**, all in one turn, with no `count_words` call: code checks them (non-empty, one line,
  no `[`, at most 40 words by the one domain `word_count`, which the `count_words` tool also uses) and refuses the ask naming
  the text that failed. Every answer is then applied by code with no model call, as S4's digits are, and silence needs no
  model either. The answer, timeout and don't-remember paths take a `review` question's stored text and build a
  `kept_twice` question's text, both by `origin`.
- **Silence** (14 days after the send, the S4 rule): the `unknown` text is written as history through the typed write path
  (a version) under a new `PassActor.TIMEOUT` row (the owner's row: accepts stale and current, review write rules).
  `FactReviewService` gets the `FactUpdatePort` in composition; `_fallback` routes `history` there and keeps
  `fallback_fields` for revive and archive. Outcomes, never a re-raise (one bad timed-out question must not abort the daily
  run): a write conflict leaves the question for the next run (as before); an exception or a typed refusal is a WARNING, the
  counter `fallbacks_not_applied`, and `fallback_attempts + 1` on the question, which stays open for the next run; it is
  closed `resolved_elsewhere` on a typed refusal at an attempt after the first, or at the third attempt (as built). A
  transient error therefore never ends in a second question or a lost history. The fact now has `none`: it never falls due
  again, so there is no second question. "Don't remember" writes the same text under the owner, through `answer_updates` and
  the same typed path.
- **The fallback helpers are exhaustive** over `revive | archive | history`: `fallback_revives` and `fallback_sets_aside`
  match each value (before P2 `fallback_revives` was `!= "archive"`, which would read `history` as a revive), the new
  `fallback_writes_history` matches `history`, and `fallback_fields` raises on `history`.
- **A reply in words** goes to the review as before; the reply mapping gains "happened / did not happen / not needed → an
  `as_history` rewrite on basis reply; renewed or not renewed → a rewrite on basis reply". On a `renewed` question a renewed
  rewrite gets `confirmed_expiry` in code, as the digit does (the model's expiry is replaced, with a note).
- **Answers fit their question.** A domain `answer_fits(kind, question)` says whether an answer is one of the question's
  options; `FactAnswerService.apply` returns `not_applied` before any write when it is not (a click from another message
  version, a stale Cabinet page). `DECIDING_ANSWERS` is a map from question kind to its deciding answers, and
  `deciding_answer(raw, question)` reads an answer against that question's own options; the Cabinet route parses each item
  against its question (read first), the bulk bar offers only the options common to the selected questions' kind (one kind
  at a time), and the 400 text is "each answer needs a question_id and one of that question's options". `answer_options` is
  per question. "Don't remember" stays out of the Cabinet, as in S4. The Cabinet question list shows the `digest` questions
  as their own group, "Invoices with no payment confirmation", each with a checkbox and the bulk bar "Paid / Not paid".
- **The quote** of a `happened` or `renewed` question is the plan's stored text, shown translated through the deployed
  translation path (the asked fact is a batch fact, so the pass's `translations` covers it). The history texts are stored
  facts in English and are shown nowhere in the review flow, so they need no translation there.
- **The weekly digest** (owner, decided 2026-10-27) gains two blocks; `digest_message` takes three optional sections and is
  sent when any is non-empty (before P2 it raised on no removals), and `digest_due` also fires for the new blocks
  (`undigested_since` is set by a silence-written history and by a new digest question):
  - **"Recorded as history without your answer:"** one line per fact a timeout wrote as history since the last digest (read
    from the questions closed `timeout` with fallback `history`: the stored `unknown` text, or the built one for
    `kept_twice`). An owner answer and "don't remember" are not listed. A history the review wrote on evidence or
    `date_passed` is not listed either (it is not a decision taken for the owner on silence).
  - **"Invoices with no payment confirmation:"** one line per open `digest` question (at most 50, oldest first), then
    "Mark the paid ones in the Cabinet:" followed by the Cabinet questions link, appended by code from the configured public
    base URL (labels may not hold a URL, `check_labels`).
  - Removals stay as in S4. Every line is shown through `shown_quote`: its translation into the message language when one is
    stored and holds, the English text only when there is none.
- **Translations of the texts the pass writes** (arbiter, 2026-10-27: English lines in a translated digest would regress the
  deployed "fact in the message language"). In a message language other than English, the pass returns in its final report's
  `translations`, besides each listed fact's quote, every text it writes: the content of an `as_history` rewrite and of any
  other rewrite, the `texts` of each ask (`happened`, `not_happened`, `unknown`, `renewed`, `not_renewed`, `not_needed`,
  `paid`, `not_paid`) and the `payment` texts of a routine invoice. Keys are `<fact_id>` for the quote (as before) and
  `<fact_id>:<text key>` for a written text (`history` for a rewrite's content). The scope collects each accepted write's and
  ask's English texts under those keys; `report_translations` checks them against that map exactly as it checks quotes
  (`translation_problem`: one line, no `[` or link, the source's numbers kept, at most 500 characters) and refuses a key
  that names no text the pass wrote. A kept translation is stored as before, `{account_id}:quote:{language}:{quote_digest(<the
  English text>)}`, so any later reader of that exact text finds it: the digest's history and invoice lines (the written
  version's text equals the stored English text), a later question that quotes a history fact, and the timeout's `unknown`
  text written 14 or more days later (inside the 180-day retention). The quote-translation retention, `shown_quote` and the
  read failure rule ("the English text") are unchanged. **The only English residual** is a code-built kept-twice text: no
  pass writes it, so nothing translates it.
- The history-pair frame stays "[System: memory review q=<id> — Alek asked the owner about a fact he could not confirm]": he
  could not confirm the outcome either, and the frame is a code literal that four live tokens quote, so changing it would cost
  a token round for no gain.
- **Kept twice** (S4-D20): a `date` fact the model left undecided twice gets a code `happened` question with no stored texts.
  Its texts are built by code at write time from the answer's or the timeout's moment, like `ended_text`: "Took place as
  planned (the owner confirmed in <Mon YYYY>): <text>", "Did not take place (the owner said so in <Mon YYYY>): <text>",
  "Planned; whether it took place is unknown (no answer by <Mon YYYY>): <text>". They keep the stored text whole and are
  exempt from the 40-word check. Residual: a validity end kept twice is asked as `happened`, so its wording fits badly.
- **UI strings, new (13 keys × uk, en, fr, es = 52 strings).** The label set grows from 15 to 28 (`LABEL_KEYS`,
  `CANONICAL_LABELS`, the root `CLAUDE.md` count); `check_labels` asks for distinct option labels per kind;
  `fact_question_message` takes the options of the question's kind. "Don't remember", the hint and every acknowledgement are
  reused. fr and es use the familiar register of their live strings ("Réponds", "Responde").

| Key | en | uk | fr | es |
|---|---|---|---|---|
| `memory_review_question_happened` | Did this happen? | Це відбулося? | Est-ce que ça a eu lieu ? | ¿Esto ocurrió? |
| `memory_review_question_renewed` | Was this renewed? | Це поновили? | L'as-tu renouvelé ? | ¿Lo renovaste? |
| `memory_option_happened` | It happened | Відбулося | Ça a eu lieu | Sí, ocurrió |
| `memory_option_not_happened` | It did not | Не відбулося | Pas eu lieu | No ocurrió |
| `memory_option_renewed` | Renewed | Поновлено | Renouvelé | Renovado |
| `memory_option_not_renewed` | Not renewed | Не поновлено | Pas renouvelé | No renovado |
| `memory_option_not_needed` | No longer needed | Вже не потрібно | Plus nécessaire | Ya no hace falta |
| `memory_review_question_paid` | Was this paid? | Це оплачено? | Est-ce payé ? | ¿Está pagado? |
| `memory_option_paid` | Paid | Оплачено | Payé | Pagado |
| `memory_option_not_paid` | Not paid | Не оплачено | Pas payé | No pagado |
| `memory_digest_history_header` | Recorded as history without your answer: | Записано як історію без твоєї відповіді: | Enregistré comme historique sans ta réponse : | Registrado como historia sin tu respuesta: |
| `memory_digest_invoices_header` | Invoices with no payment confirmation: | Рахунки без підтвердження оплати: | Factures sans confirmation de paiement : | Facturas sin confirmación de pago: |
| `memory_digest_invoices_cabinet` | Mark the paid ones in the Cabinet: | Познач оплачені в кабінеті: | Coche celles qui sont payées dans le Cabinet : | Marca las pagadas en el Cabinet: |

**P2-4. What is still archived, and how code keeps a real past fact out of the archive.**
- Archived: noise, only through a question about it (its `archive` fallback with `noise: true` on silence or "don't
  remember", or the owner's "No longer", which archives it with no history fact, as built); S4 Q4 stands: the review never
  archives noise on its own; a fact the owner's own words say is over (basis `reply`; S4's "ended" with its history fact for a
  standing fact); anything the owner archives through the Cabinet.
- Invalidated: never true, on evidence, a reply or the owner's "never" (unchanged).
- Superseded: a duplicate, a standing fact a neighbour shows ended, and the folded parts of a plan.
- Code refuses: any model `archive` whose basis is not `reply` (so `evidence` and `date_passed` archives go, for every expiry
  kind; horizon plans share the guard); an ask `fallback: archive` without `noise: true`; a `revive` fallback or a plain
  `still_true` ask about a `date` fact; `date_passed` on anything but an `as_history` rewrite; `as_history` outside its shape;
  a `history` text beside a reply archive of a fact whose question falls back to archive (as built). Every refusal names its
  fix ("rewrite it as history, or ask the owner with question happened").
- Residual (#11): a **horizon** plan (a pickup, a callback) carries no date code can see. If the model asks it as
  `still_true` with a `revive` fallback, silence revives a plan that is over, for twice its window, and the janitor asks again
  when it falls due. The prompt tells the model to ask such facts as `happened`; code cannot enforce it (next to Q7).
- The model is told in `CONSOLIDATION_LIFECYCLE` (P2-7) and in the code-built `_REVIEW_HEADER` and schemas, which repeat the
  outcome order.

**P2-5. Volume and money.** The size of the first wave after classify is bounded by the janitor's pool cap of 50 and by 5
questions a day, not by how many facts are due. Most of the questions are the S4 `still_true` kind (S4 revives only on
evidence); the facts of expiry kind `date` are the ones P2 adds, and some of those are decided by code without a question
(a bill or a cancellation whose own text already reports it).
- *Questions, without optimism.* The old rule asked nothing about a due `date` fact. The new one, if no neighbour decides
  anything and nothing folds, asks one question per such fact, less the own-text histories (bills, cancellations), and
  including a few noise asks the old rule archived on their date. Folds and evidence are unmeasured; folding needs the parts
  in one batch (now likely, P2-2), and evidence needs a neighbour written after the plan, which the dry run did not count.
  All of these questions are added by P2 over the old rule. The invoice decision changes no chat count: routine invoices were
  already own-text histories; they now land in the digest's invoice block (the Cabinet ticks them, no chat question), and a
  fine or a debt is one chat `paid` question. The horizon facts are S4's business and unchanged by P2. Silence lines start in
  the digest about 14 days after the first plan questions are sent.
- *Drain.* With the two morning archives re-opened (P2-6) the pool starts small, so the first janitor run stales facts up to
  the pool cap, the oldest `expires_at` first (then by id). The review selects up to 20 a day by (offers, `stale_since`,
  `expires_at`, id); a few daily passes queue the questions, and the >35 queue alert fires once (expected). Open questions
  count in the pool, so new stales follow only as questions close. One question is out at a time, the next after an answer,
  at most 5 a day; unanswered, one a day. The owner answering in chat clears the wave in a few weeks. Ignoring them: one a
  day, each closing 14 days after its send, a few months, while the due facts not yet staled stay current (visible). The
  Cabinet clears any number in one sitting. The bound is the pool cap and 5 a day; evidence-first only shortens the queue. A
  re-review of closed facts (P2-6) adds what it reopens to the pool.
- *Cost.* About 5 full passes for the wave plus 10–20 small ones (words replies, timeout candidates): about $3 on the live
  agent's model (`claude-sonnet-5-5`; owner's map §5.6: $0.22–0.30 a 20-fact pass, $0.08–0.10 a small one), about $6 on
  Opus 5.5. P2's extra output (3–4 texts per plan question, in the ask turn) is about $0.01–0.02 a question, $0.2–0.4 for the
  wave. Answers, silence and kept-twice texts cost no model call. Bench 7 re-run with the new tokens about $0.5–1.5. Steady
  state unchanged.

**P2-6. Interactions and release order.**
- *Classify (S5).* Unchanged. Delta 8 already keeps a passed plan at `date` so it falls to the review; with P2 it becomes
  history instead of an archive. A `past_at_report` `none` keeps its stored text: a plan text stated after its own date is
  never reviewed (residual, Q6).
- *Release order* (owner): P2 built and reviewed → **P2 code deploy** → the same day, **P2's `CONSOLIDATION_LIFECYCLE`
  upload** (never the token before the code: the old parser refuses fallback `history`, `question`, `texts` and `folds`;
  the old token on new code costs refused archives and abandoned-refusal 🚨 until the upload) → the reopen script → classify
  apply → bench 7 re-run on the classified set with the P2 token (bars: the G5 detector counts no history rewrite as an
  unbased revive; no review pass over 12 of its 15 turns; the owner reads every `date_passed` history) → janitor on, on the
  owner's word. While the janitor is log-only the review sees only facts already stale, so the deploy-to-upload window
  touches next to nothing. The translation change is deployed and its four tokens are live, so P2 edits the live
  `CONSOLIDATION_LIFECYCLE` text (the final report's `message_language, labels, translations` order is unchanged).
- *The two facts archived on the morning of 2026-10-27* under the old rule (trail `archived: date passed <date>`, actor
  `review`): **re-opened** (owner, decided), by the reopen script after the token upload; the next daily review decides them
  under the new rule.
- *The reopen script, as built* (`scripts/migration/fact_p2_reopen_archives.py`, owner-run, the
  `fact_s3_stamp_legacy_stale.py` pattern). It is general, not limited to the two, and takes only the owner's reviewed list:
  `--ids-file` (ids under `scripts/memory/`). A listed fact in state `archived` or `invalidated` is reopened; one that is
  missing, in any other state (`not_closed`) or of the `SYSTEM` account is counted and left alone. Dry run by default (counts
  on stdout; ids, state, text and the last trail entry to the gitignored `scripts/memory/`). `--apply` writes, per fact, under
  the scanned `update_time` (a changed document is `skipped_changed`): `lifecycle_fields(STALE)`, `last_updated = now`,
  `stale_since` (the stored one restored, a closed fact keeps it; else the moment of its `valid_to`, then of its last trail
  entry, then now) and one trail entry `{from: <its real state>, to: stale, actor: migration, reason: "reopened under P2
  revised"}`. With `--expiry-plan` (a classify plan under `scripts/memory/`) it also writes the plan's `expiry_kind`,
  `ttl_days` and `expires_at` in the same preconditioned write; a fact absent from the plan is reopened without and listed
  (`without_expiry`). Text and vectors are never touched. A second `--apply` reopens nothing.
- *The owner-decisions script, as built* (`scripts/migration/fact_p2_owner_decisions.py`, owner-run): for a re-review in which
  the owner decided each fact himself. Its input (`--decisions`, a file under `scripts/memory/`) is a JSON list of `{fact_id,
  account_id, action, text?, successor_id?}`, `action` one of `history` (the text as a history version: `as_history`, `none`,
  `archival`), `current` (the text as a current fact, `none`, its stored priority), `supersede` (closed in favour of
  `successor_id`, which must be current or written earlier in the same run), `invalidate`, or `revive` (current again, text
  unchanged, `none`). The whole file is validated before anything is written (an unknown action, a missing or bad text by
  `written_text_problem`, a duplicate fact, an unusable successor refuse the file). Per fact, in file order: it is reopened to
  `stale` by the reopen script's own function under the scanned `update_time`, then the action is one typed write through
  `FactUpdatePort` as the owner, with one reason, `owner decided on review` (`owner_decided_reason`; a rewrite for every
  reader, never a revive), and `expected_id` = the listed fact, so a chain that moved on to a newer version is refused (MOVED)
  and the fact is skipped and listed, never followed. Dry run by default (counts on stdout; id, action, old and new text and
  state to `scripts/memory/`); `--apply` writes. A changed fact is skipped and listed; a failure never stops the run; a fact
  whose reopen landed and whose action did not stays `stale` and is `resumed` by the next run without a second reopen; a fact
  whose last trail entry is already the owner's decision is `already_decided`, so a second `--apply` writes nothing. A fact of
  another account than the file names, of the `SYSTEM` account or in another state is counted and left alone.
- *A re-review of closed facts* (owner, decided 2026-10-27; planned, not run): every archived fact and every invalidated fact
  that is not a directive is looked at again under the new rule. The mechanism: (1) a dry run first, which writes nothing: the
  classify script with `--include-closed --ids-file` decides an expiry for the listed closed facts and writes a plan, and
  bench 7 with `--stage-ids` (the same ids, staged stale in the kit's overlay in any state), `--expiry-plan` and
  `--max-passes` shows what the review would write for each and writes a human `_listed.md` (original state and text,
  outcome, the text or question it would write); (2) the owner reads it; (3) either the reopen script with `--ids-file` and
  `--expiry-plan` reopens what he keeps and the daily review decides those facts as stale, or, for the facts he decided
  himself, the owner-decisions script writes his decision for each.
- *Labels.* The 13 new labels go through `CANONICAL_LABELS`, `check_labels` and the LANGUAGE section, so `labels_digest()`
  changes and every cached model label set is ignored until the next pass writes one (UI language meanwhile,
  `labels_fallback` counted): one-time, expected.
- *Rollback* (`scripts/migration/fact_p2_rollback_questions.py`, owner-run, dry run by default, `--apply`; each write under its
  document's `update_time`; ids only, no text, to `scripts/memory/`). `FactReviewQuestion` forbids unknown fields and the old
  `AnswerKind` knows none of the new answers, so the old revision cannot read a P2 question, and it refuses even a
  default-valued P2-era record because the store writes the defaults. So the script removes the five new keys (`question`,
  `texts`, `origin`, `delivery`, `fallback_attempts`) from **every** question document, and rewrites every P2 record (a
  `question` other than `still_true`, non-empty `texts`, an `origin` other than `review`, a `delivery` other than `chat`,
  `fallback_attempts` above 0, fallback `history`, or an answer outside the S4 four):
  - **digest** ones are deleted (their facts are already history, "payment not confirmed");
  - **closed** ones get `closed_by: resolved_elsewhere`, no `answer` and no `answer_source` (`purge_after` kept; the digest
    and the Cabinet read closed records), and the fallback fields of an open one (a closed `history` fallback is no S4 value);
  - **open and sent** ones (`pending`, `asked`, `unanswered`: their message went out with P2 options) are **closed**
    `resolved_elsewhere` (`closed_at` and a 90-day `purge_after` stamped at apply), never converted: the old code would read
    the owner's digit with S4 meanings ("3" → never) and invalidate a real event;
  - **open and `queued`** ones get fallback `revive`, `noise` false and no `fallback_date`, so the old code treats them as S4
    questions; their facts stay current while they wait, as in S4.
  It is run once more after the traffic switch, since the P2 code may write records between the first run and the switch.
  History versions already written stay (plain current facts with `none`, `archival`).

**P2-7. What changes.**
- *Tokens* (`prompts_snapshot/`, then `snapshot_upload.py`): `CONSOLIDATION_LIFECYCLE` only — `in_this_pass` (a passed plan
  is rewritten as history, never archived), `outcome_order` step (3) (history before archive; archive only on the owner's
  reply), `calls` (`as_history`; `ask` with `question`, `texts`, `folds`), `basis` (`date_passed` backs a history rewrite
  only; evidence never archives), `reply_mapping` (the new answers), `asking` (kinds, texts in one turn without
  `count_words`, fallback `history`, archive only with noise, folds, a horizon plan asked as `happened`), `checklist`
  (`dated_plan_passed` → `history=<evidence|own_text|ask|none>`), the text rules of P2-1, the invoice rules (an invoice is no
  payment; routine → `payment` texts; fine or debt → `paid` question). `CONSOLIDATION_TAXONOMY` is not touched (Stage 1 and
  email DISCARD of passed plans is a separate session, Q5).
- *Code:* `domain/fact_update.py` (`as_history`, `typed_update_refusal`), `fact_update_plan.py` (metadata and context cleared
  before the embedding texts), `fact_update_lifecycle.py` (`_review_version`: priority, the content-change check; the writer
  check; history refusal text), `fact_review_basis.py` (`parse_payment`), `fact_review_rules.py`
  (`basis_refusal`, `payment_refusal`, `date_passed_histories`, `_BASIS_SHAPES`, `_date_passed_refusal`, `_history_refusal`, `ask_refusal`, `is_dated_plan`,
  `archived_only`, `kept_twice_fallback`, `kept_twice_question`, `digest_question`, `fallback_revives`,
  `fallback_sets_aside`, `fallback_writes_history`, `fallback_fields`, `select_batch`, `made_current_documents`,
  `open_fact_ids`, `pool`, `next_in_queue`, `queue_alert` leaving out digest questions), `fact_review_question.py`
  (`question`, `texts`, `origin`, `delivery`, `fallback_attempts`, fallback `history`, the validator by `origin`),
  `fact_review_options.py` (the options per kind), `fact_review_ask.py` (`question`, `texts`, `folds`),
  `fact_review_invoice.py` (`InvoiceRequest`), `fact_review_digest.py` (three optional blocks), `fact_review_answer_kind.py`
  (+7 kinds), `fact_review_answers.py` (`answer_updates`, `answer_fits`, `answer_closes_fact`, `DECIDING_ANSWERS`,
  `deciding_answer`, `history_fallback`, kept-twice texts), `fact_review_reasons.py` (the reasons above, `basis_reason`,
  `is_rewrite_reason`), `pass_actor.py` (`TIMEOUT`), `fact_words.py` (one domain `word_count`, `written_text_problem`),
  `fact_review_language.py` (labels per kind), `ui_messages.py` and `locales/{uk,en,fr,es}.py`,
  `infrastructure/fact_review_scope.py` (`as_history`, `payment`, `folds`, folds out of `kept()`, the written texts by
  translation key), `domain/fact_review_quote_translation.py` (`report_translations` over quotes and written texts),
  `domain/fact_review_result.py` (the wider `translations`, `invoices`), `agents/consolidation_agent.py` (`_BASIS_SCHEMA`,
  `_REVIEW_UPDATE_SCHEMA`, `_ASK_OWNER_SCHEMA`, `_REVIEW_HEADER`, `_TRANSLATIONS_RULE`: an entry for every text the pass
  writes, keyed `<fact_id>:<text key>`; `count_words` on the domain counter), `services/fact_review_service.py` (`_fallback`
  through the typed path, its outcomes and attempt count, `_ask_builder` stores the texts, `_after_pass` folds and digest
  questions, `_store_translations` over the written texts, `_digest` blocks through `shown_quote` and the Cabinet link,
  digest questions closed after 90 days), `composition/application_graph.py` (the port into `FactReviewService`, the Cabinet
  link), `services/fact_answer_service.py` (`apply` → `not_applied` on a misfit, `answer_options` per question),
  `web/user_cabinet_app.py` and the Cabinet page (per-question options, bulk bar per kind, the invoices group),
  `ports/fact_review_store.py` and `adapters/firestore_fact_review_store.py` (the new fields, `timeout_histories`,
  `mark_fallback_failed`), D10 counters `to_history`, `folded`, `fallbacks_not_applied` in the 🧹 review segment, the bench 7
  detector and turn bar, `review_bench_cases.py` (the `plan_date_passed` case), the reopen script, the owner-decisions script, the rollback script; `expected_id` on
  `FactUpdatePort.apply_fact_update`.
- *New tests:* each basis × `as_history` (allowed, refused, per writer); every model archive not on a reply refused;
  `date_passed` only with `as_history`; a history rewrite is a version with `archival`, `none`, empty metadata, no context,
  `reported_date` and `created_at` kept and the original superseded in one transaction; the history version's metadata
  vector is computed from the new text (not the plan's metadata); each trail reason and its
  `is_rewrite_reason` / `is_revive_reason` / `gives_window_reason` reading; ask with each kind (texts required, exact keys,
  ≤ 40 words by `word_count`, fallback `history`); folds superseded only after a queued ask, none after a failed queue; the
  batch order; every answer of every kind through chat, Slack and the Cabinet, and `not_applied` for an answer that is not
  an option of its question; silence writes the
  `unknown` text under `timeout`; a raising timeout write leaves the question open with `fallback_attempts + 1` and the run
  goes on, a typed refusal after the first attempt or the third attempt closes it `resolved_elsewhere`, and no second question
  follows; "don't remember" writes it under `owner`; kept-twice texts dated at write time and chosen by `origin`; a
  `date_passed` history is no evidence; folds out of `kept()` and their offers removed; the fallback helpers over all three
  values; a routine invoice → history + a digest question, none in chat, none in the pool or the queue; a fine → a chat
  `paid` question; the digest's two new blocks (and a digest with only them is sent); the pass's translations of its written
  texts kept, refused (a key naming no written text, a changed number) and stored under the text's digest, and the digest's
  history and invoice lines shown translated, English only without a stored translation, a kept-twice text English; the
  Cabinet invoices group and its bulk answers; the labels for the new kinds in four languages and through `check_labels`;
  noise answered "No longer" archived only; the reopen and rollback scripts (closed, open, digest and default-only records);
  bench 7's `--stage-ids`; prompt ↔ domain pins for the new shapes.
- *Existing tests whose behaviour this intentionally changes* were each decided by the card's reviewer under the reviewer
  rule (the plan's list, completed by running the suite). Kept green by design: `test_ended_on_a_dated_plan_archives_only`
  and `tests/unit/web/test_cabinet_review_answers.py::test_no_longer_on_a_dated_plan_archives_it_and_adds_nothing` (a stored
  S4 `still_true` question about a `date` fact keeps S4's answer; Q4).
- *Docs:* the RFC (§8 P2 gets a "revised 2026-10-27" line pointing here; D6, D7 and S4 as built notes), the owner's map §5.5,
  §5.6, §2.7, the §7 timeline and §9.5, `decisions/fact_review_and_owner_questions.md`, `src/agents/CLAUDE.md` (review tool
  rules), and the root `CLAUDE.md` "Memory review" paragraph (the silence rule, the label count, the scope's refusals).

*Arbiter decisions on the open questions (2026-10-27).* Q2 `date_passed` honesty: the prompt rule, the bench 7 reading of
every `date_passed` history and the evidence exclusion; no domain allowlist. Q3 two shapes of history: S4's "ended" (archive +
a separate history fact) stays; P2 rewrites. Q4 a stored S4 question about a `date` fact keeps S4's archive-only "No
longer". Q5 Stage 1 and email DISCARD of passed plans: a separate session. Q6 `past_at_report` plan texts never reviewed: a
residual. Q7 kept-twice validity ends asked as `happened` with code wording, and the horizon-plan silence revive: residuals.
Q8 routine invoice vs fine or debt is the model's call; a fine filed as routine gets only the digest line and the Cabinet
tick: a residual, the bench reads each invoice decision. Q9 digest lines in English: rejected (every text the pass writes is
translated; a code-built kept-twice text is the only English residual).

*As built (where the code differs from the text above, or adds to it; each decided by the arbiter).*
- **`not_evidence`.** `basis_refusal` takes an explicit set of the ids this pass rewrote as history on `date_passed`
  (`date_passed_histories`) and refuses them as evidence and as a successor ("… names a fact this pass rewrote as history on
  date_passed; that is no evidence"); `made_current_documents` alone could not enforce it.
- **`paid` on a `date` fact** is allowed: a fine with a due day is a `date` fact, and the text's "only `happened` or
  `renewed`" would refuse its ask.
- **The timeout attempt rule.** "The same typed refusal repeats" is read as "a typed refusal at an attempt after the first":
  the record stores no earlier refusal. A typed refusal with `fallback_attempts >= 1`, or any failure at the third attempt,
  closes the question `resolved_elsewhere`; otherwise `mark_fallback_failed` records the attempt and the question stays open.
- **`timeout_histories`** is the store read behind the digest's history block (equalities on the account, `closed_by
  timeout` and fallback `history`, served by single-field indexes, no composite index).
- **Invoice digest questions** carry `fallback: history`, never applied (they are never sent, so they never time out); their
  `queue_key` is their creation time, not the fact's `stale_since`, which a fresh history version lacks.
- **The Cabinet link** in the invoice block is `CLOUD_RUN_SERVICE_URL` + `/cabinet?tab=memory` (the Memory tab), passed to
  `FactReviewService` as `cabinet_url`.
- **Two Cabinet page strings** in four languages (`questions.invoices_title`, `questions.one_kind`): the page's own
  dictionary cannot reach the UI strings.
- **The renewal expiry in the scope.** When, on a `renewed` question's reply, the model writes a rewrite that is not
  `as_history` with an expiry other than `none`, the scope replaces that expiry with `confirmed_expiry`; code cannot otherwise
  tell "renewed" from "not renewed" in a rewrite.
- **`fallback_date` leaves the `ask_owner` schema**; the parser still reads it, so that its refusal can name it.
- **Noise answered "No longer" is archived only** (`archived_only`: a dated plan, or a question that falls back to archive):
  no history fact beside it, and a `history` text beside a reply archive of such a fact is refused. A standing fact keeps its
  S4 history fact.
- **The reopen script is general** (P2-6): `--ids-file` only (the owner's reviewed list), facts in state `archived` or
  `invalidated` (the trail's `from` is the real state; any other state is `not_closed`), optional `--expiry-plan`.
- **The owner-decisions script** (P2-6): the owner's own decision per closed fact (`history`, `current`, `supersede`,
  `invalidate`, `revive`), reopened and then written as the owner with `owner decided on review` and `expected_id`.
- **The rollback script strips the five keys from every question document**, P2 or not, closes the sent P2 questions
  `resolved_elsewhere` and converts only the `queued` ones; it is run again after the traffic switch (P2-6).
- **Typed writes take `expected_id`**, the exact document the caller decided on: a first resolution to another document is
  refused MOVED and nothing is written. The review's writes, the owner's answers, the timeout's history and the
  owner-decisions script pass it, so an answer racing a timeout can no longer overwrite the history the timeout wrote (or
  the other way round).
- **A timeout whose history version landed is still closed `timeout`**: when the question's fact moved on and the version it
  resolves to ends with this question's own timeout reason (a crash between the write and the close), the question is closed
  `timeout`, not `resolved_elsewhere`, so the digest keeps its "recorded as history" line. The give-up rule
  (`history_fallback_gives_up`) is in the domain.
- **An ask text equal to the fact's stored text is refused** ("the same as the fact's stored text; write what the answer makes
  true").
- **Classify `--include-closed`** (with `--ids-file`) also classifies the listed archived and invalidated facts, for the
  re-review's expiry plan.
- **Bench 7** reads P2: `G5_MAX_TURNS = 12` of the review's 15; the `plan_date_passed` case expects a `happened` question
  (or a history on evidence), never an archive and never a `date_passed` history; `--stage-ids PATH` stages exactly the listed
  facts, in any state, as stale copies in the kit's overlay (no seeded cases), `--expiry-plan` gives them a classify plan's
  expiry, `--max-passes N` (default 6) runs review passes until every listed fact is decided, and the report gains a human
  `<report>_listed.md`.

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

**S3 update (2026-10-23):** the rows `state`, `temporal_class`, `reported_date`, `ttl_days`, `expires_at`, `last_updated` were
re-checked against the S3 code and rewritten, and the rows `expiry_kind`, `stale_since` and `lifecycle_events` are new. After S3
the writers and readers below are the production ones; the janitor is a reader and writer only of the fields named for it, and
nothing renders `stale_since` or `lifecycle_events` to a model. Line numbers of the S3 rows are given by function (the code wins).

Production code under `src/` only; `scripts/` and tests are not processes. "Write only" means no code path reads the
field to decide or render anything. Re-check the row before changing a field (§1 rule).

| Field | Read by | Effect today |
|---|---|---|
| `id` | the biographical cache dicts (`biographical_context_service.py:176-183`); enrichment drops facts whose id is in the baseline (`search_enrichment_service.py:214-231`); consolidator search results (`fact_management_adapter.py:102`); forward resolution follows `superseded_by` ids (`firestore_repo.py:541-560`) | bio dedup; the id the consolidator writes back to. A version write changes it, so a stale id is followed forward (`update_fact`, `merge_facts`, Cabinet invalidate), and the cache is refreshed after a run that wrote, failed or not (D5) |
| `text` | everything that renders a fact, including the cache document | what Smart, Lelik, MCP, Cabinet and the consolidator see |
| `vector`, `tags_vector`, `metadata_vector` | 6 vector channels next to the domain channel (`search_enrichment_service.py:137-164`) and its semantic dedup; `add_fact_if_unique` nearest neighbour (`firestore_repo.py:677-687`) | retrieval. Since S1 recomputed on every text/tags/metadata change (`fact_management_adapter.py:318-322`) |
| `account_id` | every query; tenant check on update/merge by id (S1), on `resolve_current` (a foreign start document is "not found") and on Cabinet invalidate (`firestore_repo.py:316-322`); `get_lineage` filters it since S2 | tenancy |
| `state` | every read path filters `== "current"` (`firestore_repo.py:223,277,383,475,962`); dedup (`:680`); consolidator search results; the janitor's due query (`state == "current"`) and its backlog query (`state == "stale"`) | the only visibility switch. **Since S3** `stale` is written by the janitor only (`stale_due_fact`: the state change with its flags, `stale_since`, `last_updated`, a trail entry); the model is refused it on update, create and merge, and the cache document is rebuilt after the janitor staled a fact |
| `domain` | router `relevant_domains` → `search_facts_by_domain`; baseline build (biographical first, then fill; `agent_directive` → `standing_directives`); Stage 2b (`consolidation_agent.py:811-815`); Lelik `voice_fact_domains` (`companion_context_assembler_service.py:91`); MCP grouping; Cabinet filter and display; `PromptBuilder` channel split; cache document | selection and grouping |
| `context_priority` (+ `context_priority_rank`, stored only) | baseline filter `rank < 5` and order (`firestore_repo.py:483-485`); cache document; shown to the consolidator | what enters every prompt |
| `tags` | `tags_vector` input at write; baseline split (`mindset` → principles, `biographical_context_service.py:190`); anchor lookups (`consolidation_agent.py:1529,1541`); `semantic_lens` added at read by the router merge; cache document; shown to the consolidator | retrieval, principles, anchors |
| `metadata` | `metadata_vector` input at write; `search_memory` render (`memory_search_agent.py:365-366`); shown to the consolidator | retrieval, detail on explicit search |
| `context` | `search_memory` render (`memory_search_agent.py:360-361`); projected by vector search since S1 | detail on explicit search |
| `reported_date` | `search_memory` render "reported: YYYY-MM-DD" (`memory_search_agent.py:362-364`); consolidator search results (ISO with the owner's offset, `fact_management_adapter.py:134`) and the Stage 2a `reported`; **the base of a horizon** (`compute_expiry`, the re-mention recompute, `domain/fact_lifecycle.py`) | written by the code rules of D2 per pass (`domain/fact_write_context.py`, `fact_update_lifecycle.py`): clamped into the batch span on Stage 1 and email passes, kept or inherited (`derived_from`) on cleanup passes, never moved back by an update. Means "when the owner last stated it"; the S1 rule "a content change sets it to now" is gone. Smart still sees it only through `search_memory` |
| `type` (`FactType`) | `search_memory` drops `PRINCIPLE` (`memory_search_agent.py:321`); write path adds the `anchor` tag (`fact_write_service.py:146-148`); Cabinet display; shown to the consolidator | principles excluded from explicit search |
| `temporal_class` | consolidator search results | **no code decision**; model input only. Since S3 the prompt treats it as a default for the expiry decision when no row of the kinds table applies; no code reads it |
| `version` | consolidator search results (`fact_management_adapter.py:114`); `plan_fact_update` (`fact_update_plan.py:57`); `get_lineage` order; the data audit (unique per lineage, `replaces_fact_id` target at version − 1) | counts the versions of a lineage since S2: 1 at birth, + 1 on each version document, unchanged by an in-place write |
| `created_at` | baseline order (`created_at DESC` after rank, `firestore_repo.py:481`); domain channel newest-first (`:900`); Cabinet sort (`user_cabinet_app.py:782`) and paginated order (`firestore_repo.py:287`); cache document | ordering. Since S2 it is lineage birth: a version copies it, so it orders nothing inside a lineage (`get_lineage` orders by `version`). A copied value makes ties at a page boundary of the Cabinet browser possible |
| `is_current` | no `src/` reader since `get_latest_fact_by_lineage` filters on `state`; written by `lifecycle_fields` on every transition (`domain/fact_lifecycle.py:20-26`), by the version close and the merge close (`closure_fields`); the data audit checks it | "latest of its lineage"; false only on a superseded document |
| `lineage_id` | forward resolution (a hop that leaves the lineage is a merge end, `fact_lifecycle.py:56`); `get_lineage` and `get_latest_fact_by_lineage` (no production caller; the former caller in `prompt_builder.py` is gone); `PromptBuilder.preload_components` cache key for `SYSTEM` facts (`prompt_builder.py:62-67`); the data audit | the lineage rule of D8 |
| `last_updated` | projected by `get_active_facts` and `get_active_facts_ordered` (`firestore_repo.py:260,513`; since S1, they named `updated_at` before); read by no code | written on every in-place write, now also by the janitor; projected so a read does not default it to now. The stamp script leaves it alone (the only age signal of the legacy stale facts) |
| `visibility` | defaulted on read; never filtered | not enforced on reads, by design: every fact of an account is visible to all its members (owner, 2026-10-21); out of scope |
| `created_by_user_id` | ownership migration on read (`firestore_repo.py:121-157`) | attribution only |
| `valid_from`, `valid_to` | none (`valid_to` written by `lifecycle_fields` since S1; `valid_from` set at birth and by the version write) | **write only** |
| `ttl_days` | `expiry_view` (only when `expiry_kind == horizon`); the re-mention recompute of a horizon. Never rendered to a model as such | written by code only (`compute_expiry`): the horizon's days, else null. The model's value is dropped at every parse boundary. The 113 values the model wrote before S3 mean nothing and are ignored while `expiry_kind` is null |
| `expires_at` | the janitor's due query (`state == "current" AND expires_at <= now`, index `(state, expires_at)`); `expiry_view` (the `date` answer, `due` for a horizon); the data audit (type only) | written by code only (`compute_expiry`, `born_due`'s floor, the re-mention recompute): create, merge, version, `expiry`-only update. Null for `none`. **Never a TTL policy field**: the janitor stales, it never deletes |
| `expiry_kind` (S3) | the janitor (a due fact without it is skipped); `expiry_view`; the re-mention recompute; the data audit (enum validity) | `date` / `horizon` / `none`, written by code from the model's `expiry`; null on facts written by the old revision, the legacy v2 path, and every fact before S5's classify |
| `stale_since` (S3) | the janitor's backlog (count and oldest age); S4's selection order | written by the janitor and by the deploy-step stamp script only |
| `lifecycle_events` (S3) | the next transition (read, append, write under the same precondition); copied into a version; S4's "revived 3× in 30 days" alert | every state transition, in the same write (§2.7 of the owner's map): model state changes, version and merge closes, Cabinet invalidate, the directive backstop (reason `directive cap`), the janitor (`expired <ISO>`), the stamp script; the newest 20 `{at, from, to, actor, reason}`, code-built reasons only |
| `replaces_fact_id` | written by the version write (`fact_update_plan.py:58`); read by the data audit only (target in the same lineage, at version − 1, pointing back with `superseded_by`) | audit only |
| `superseded_by` (S2) | written by the version close, the merge close and a model `SUPERSEDED` in place (`closure_fields`, `fact_lifecycle.py:73-75`); read by forward resolution (`resolve_current`) and the data audit | the successor of a closed document; a merge successor is in another lineage |

**Projections** (what each read carries, for D2):

| Read | Carries | Lacks (relevant here) |
|---|---|---|
| `search_facts` select (`firestore_repo.py:365`) → `EnrichedFact` → consolidator dict (`fact_management_adapter.py:134`) | text, type, state, domain, tags, priority, valid_*, created_at, version, metadata, temporal_class, reported_date, context, vector, **`expiry_kind`, `ttl_days`, `expires_at`** (S3; `expires_at` as an ISO string) | `stale_since`, `lifecycle_events`, `superseded_by`; the dict renders `expiry` / `due` instead of the raw fields and drops `context` and `created_at` |
| `get_active_facts` / `get_active_facts_ordered` (`firestore_repo.py:229`, `:479`) | the above minus vector and context, plus last_updated, is_current, language; `get_active_facts` (and `get_longest_facts` through it) also `expiry_kind`, `ttl_days`, `expires_at` | the new fields on `get_active_facts_ordered`; `stale_since`, `lifecycle_events` |
| `find_due_facts` / `stale_backlog` (the janitor, S3) | `account_id`, `expires_at`, `expiry_kind`, `lifecycle_events` and the snapshot `update_time` (no text, no vectors); `account_id`, `stale_since` | everything else |
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
