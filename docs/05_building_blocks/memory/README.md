<!-- living-doc
audited: 2026-11-23
covers:
  src/domain/fact_*.py
  src/domain/consolidation*.py
  src/domain/entities.py
  src/domain/stored_fact.py
  src/domain/memory_fact.py
  src/domain/memory_write_lease.py
  src/domain/memory_search_format.py
  src/domain/memory_search_texts.py
  src/domain/new_fact_attributes.py
  src/domain/due_fact.py
  src/domain/search.py
  src/domain/settings.py
  src/domain/local_time.py
  src/domain/owner_timezone.py
  src/domain/pass_actor.py
  src/domain/person_cards.py
  src/domain/confirmed_people.py
  src/domain/profile_*.py
  src/domain/place_names.py
  src/domain/interactive_*.py
  src/domain/choice_outcome.py
  src/ports/fact_*.py
  src/ports/consolidation_queue.py
  src/ports/memory_fact_draft_repository.py
  src/ports/memory_write_lease.py
  src/ports/repository.py
  src/ports/biographical_cache_port.py
  src/ports/search_enrichment_port.py
  src/ports/session_store.py
  src/ports/confirmed_people_repository.py
  src/ports/person_card_repository.py
  src/ports/profile_answer_repository.py
  src/ports/interactive_message_port.py
  src/ports/choice_handler_port.py
  src/adapters/fact_management_adapter.py
  src/adapters/firestore_consolidation_queue.py
  src/adapters/firestore_fact_*.py
  src/adapters/firestore_memory_*.py
  src/adapters/firestore_repo.py
  src/adapters/firestore_session_store.py
  src/adapters/firestore_confirmed_people_repo.py
  src/adapters/firestore_person_card_repo.py
  src/adapters/firestore_profile_answer_repo.py
  src/adapters/plain_text_interactive_message_adapter.py
  src/services/biographical_context_service.py
  src/services/cabinet_fact_service.py
  src/services/consolidation_service.py
  src/services/fact_*.py
  src/services/memory_fact_service.py
  src/services/overflow_routing_service.py
  src/services/search_enrichment_service.py
  src/services/interactive_choice_service.py
  src/services/profile_question_answers.py
  src/services/owner_timezone_resolver.py
  src/agents/consolidation_agent.py
  src/agents/memory_search_agent.py
  src/agents/fact_profiler_agent.py
  src/agents/fact_query_slots_agent.py
  src/composition/fact_*.py
  src/composition/consolidation_model_setup.py
  src/composition/memory_search_setup.py
  src/composition/memory_setup.py
  src/composition/session_routing_setup.py
  src/infrastructure/memory_fact_tools.py
  src/infrastructure/fact_review_scope.py
  src/infrastructure/interactive_message_router.py
  scripts/consolidation/
  scripts/data_audit/
  scripts/people/
  scripts/migration/fact_*.py
  scripts/export_account_facts.py
  scripts/cleanup_consolidation_queue_dev.py
  scripts/validation/inspect_consolidation.py
  scripts/admin/fix_broken_vector_facts.py
  src/domain/lease.py
-->
# Memory

The owner's map of long-term memory: how a conversation becomes facts, how facts reach every prompt, how they are
searched, how a fact ends, what fails. Current state only; the code is the ground truth and this file names where.
Every session that touches memory facts, consolidation, the baseline, memory search, `$memory save`, standing directives
or the fact lifecycle ends by updating this file.

What it is not: the drive index, the agent workspace and the Gmail index are separate stores (only Stage 3, §3 step 8,
reads indexed mail into facts, see [gmail_email_indexing](../gmail_email_indexing/README.md)). Companion channels (a
channel bound with `$agent tutor`) do not feed facts: their overflow goes to the companion's own extraction queue
(`src/services/overflow_routing_service.py::OverflowRoutingService`). Lelik's phone calls reach memory only as the
call-summary pair written into the primary channel's history, which is consolidated like any other message.

**Links.** RFCs (design records; where they differ from this file, the code and this file win):
`docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md`, `docs/10_rfcs/CLUSTER_REVIEW_RFC.md`,
`docs/10_rfcs/STANDING_DIRECTIVES_RFC.md`, `docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md`,
`docs/10_rfcs/FACT_PROFILE_RFC.md`, `docs/10_rfcs/FACT_LIFECYCLE_RFC.md`, `docs/10_rfcs/FACT_SEARCH_LIST_MODE_RFC.md`.
Decision records (`docs/04_solution_strategy/decisions/`): `fact_s1_defects.md`, `fact_versions_one_writer.md`,
`fact_expiry_and_janitor.md`, `fact_review_and_owner_questions.md`, `standing_directives.md`,
`directive_applicability_gate.md`, `memory_facts_owner_confirmed.md`, `consolidation_one_batch_per_task.md`,
`consolidation_failed_batch_bounded.md`, `sonnet55_adaptive_thinking_stall.md`, `consolidation_provider_model_eval.md`,
`router_prefetch_once_per_message.md`, `smart_deduplication_keep_duplicate_over_lost_detail.md`,
`memory_telemetry.md`, `model_refresh_2026_10.md`.

## 0. How to answer questions about memory

| Question | Read |
|---|---|
| What is in memory; what a fact holds; what `directive`, `temporal_class`, `state`, `context_priority` mean | §2 |
| What a fact's retrieval profile fields are, who writes them, where the domain registry lives | §2.1, §3 step 9b |
| What `is_current`, `valid_to`, `lineage_id`, `version`, `superseded_by` mean; what an update or a merge does to them | §2.3, §2.4 |
| Why an update gave the fact a new id; whether the old wording is kept and how to look at it; why an old id still works | §2.4, §9.3 |
| When does a conversation become facts; why nothing happened after I told it something | §3 steps 1–3, §7 |
| When a fact ends: what `expiry` is, who computes `expires_at`, why a write was refused for its expiry, what "born due" means | §2.7, §6 |
| What the daily janitor does, why a fact went `stale`, what log-only means, the breaker, the pool, the run guard, the alerts, the 🧹 morning line | §5.5, §9.1 |
| What the review pass does with a stale fact; why a fact came back, was set aside, became history or was asked about | §5.6, §6 |
| What happens to a plan whose date passed, a document or membership that ended, an invoice, a fine | §5.6, §2.7, §7 |
| How a question reaches me, how I answer it (a digit, a button, words, the Cabinet), what happens if I do not | §5.6 |
| Why no question arrived, or arrived twice, or its buttons did nothing | §5.6 (one question at a time, the send), §9.2b |
| What the weekly digest says and when it is sent | §5.6 |
| What the Cabinet shows for stale, set-aside and waiting facts, and what it cannot do | §4.8, §5.6 |
| What the memory-write lease is; why a consolidation task waited or ran twice a few minutes apart | §3 step 3, §6, §7 |
| Why a batch has another size than the current window, or how a window change treats messages already in a batch | §3 steps 2–3 |
| What the consolidator sees (whose words, which text of the bot's replies) and how it decides create / update / merge / discard | §3 steps 4–5 |
| Why the bot remembered something wrong, or something it said itself (echo trap) | §3 step 5, §9.3 |
| What Stage 2a (cluster review) and Stage 2b (directive review) do and when they run | §3 steps 6–7 |
| What a standing directive is; why one disappeared or was rewritten; the cap | §3 step 7, §4.2, §5.3 |
| What the bot sees about me in every message (the baseline); how it is chosen and ordered; its limits | §4.1 |
| How memory search ranks (the scored engine every reader uses, and the older RRF engine); what `search_memory` returns | §4.3, §4.5 |
| What the Router prefetches before Smart answers | §4.4 |
| What Lelik and the claude.ai connector (MCP) see | §4.6, §4.7 |
| What `$memory save` does; why the bot never writes memory itself | §5.1 |
| What the Cabinet fact page reads and changes | §4.8, §5.2 |
| What `$consolidate` does | §5.4 |
| What happens on a tool error, a bad enum value, a provider outage, the turn cap, a crash; what retries | §6 |
| A fact told today, a correction, a directive: when each is in memory and in the baseline | §7 |
| Which model consolidates; what drives its cost; where cost is recorded | §8 |
| How to see what consolidation did in a given batch; which log lines, BigQuery, Logfire | §9.1, §9.4 |
| Why the bot did not remember X | §9.2 |
| Why an update, a merge or a Cabinet delete did not apply ("changed concurrently", "merged into") | §6, §5.2, §9.2 |
| Why a reminder's or a system note's text became a fact | §9.3 |
| Which batch wrote a given fact | §9.4 (step 0) |
| Owner scripts, dry-run benches, the data audit, the prompt snapshot and its vocabulary tests | §9.5–§9.7 |
| What is not built | §10 |
| Where to start reading the code | §11 |

---

## 1. Overview

### 1.1 The cycle

1. The owner talks in a chat channel. Every message and every bot reply is appended to that channel's session.
2. Smart answers from the session's recent messages, the **baseline** (facts in every prompt, §4.1), the Router's
   **prefetch** (facts searched for this message, §4.4) and, when it asks, **`search_memory`** (§4.5).
3. When a session grows past a threshold, its oldest messages are queued as a **batch** and cut from the session
   (§3 steps 2–3).
4. A Cloud Task runs the **ConsolidationAgent** (`src/agents/consolidation_agent.py::ConsolidationAgent`): one model, a
   tool loop, three passes (Stage 1 conversation → facts, Stage 2a cluster review, Stage 2b directive review) and a pass
   over indexed mail (§3 steps 4–8).
5. At the end the baseline cache is rebuilt, so the next message sees the new facts (§3 step 9).

There is no other writer of facts in normal operation. The bot never saves facts itself (§5.1). The owner can invalidate
a fact from the Cabinet (§5.2). A daily zero-LLM job, the janitor, only moves a fact whose expiry has passed from
`current` to `stale` (§5.5). What becomes of a stale fact is decided by the **review pass** (§5.6): it brings the fact
back, sets it aside, rewrites it as history, or asks the owner one question at a time in the primary chat. The owner's
answers are applied by code, not by a model.

### 1.2 Owned data

Facts and the lifecycle collections are in the `us-production` database with the environment prefix. Collection names
are `EnvironmentConfig` properties (`src/config/environment.py::EnvironmentConfig`), named below.

| Store | Property | What it holds | Written by |
|---|---|---|---|
| Facts | `domain_facts_collection` | one document per fact **version** (a content change adds a document, a lifecycle change edits one, §2.4), three 768-dim vectors each | consolidation tools (§3), Cabinet invalidate (§5.2), the janitor (state to `stale` only, §5.5), the review pass and the owner's answers (§5.6) |
| Sessions | `sessions_collection`, doc id `{user_id}:{channel_id}` | the channel's recent messages (the sliding window) | `ConversationHandler`, notifications, `$memory save` |
| Consolidation queue | `consolidation_queue_collection` | batches cut from sessions, until consolidated | overflow routing (§3 step 3), `$consolidate` |
| Baseline cache | `user_context_collection`, doc id = account id | `biographical_facts`, `principles`, `directives` lists (plus `refreshed_at`, which keys the fact-search snapshot) | `BiographicalContextService.refresh_cache` (§3 step 9) |
| Memory-write lease | `memory_write_leases_collection`, doc id = account id | `lease_owner`, `lease_until`, `acquired_at` | `ConsolidationService`, `FactReviewService` (§3 step 3) |
| Memory drafts | `memory_fact_drafts_collection`, doc id `{user_id}:{code}` | facts Smart proposed, TTL policy on `expires_at` | `propose_memory_facts` (§5.1) |
| Janitor day | `fact_lifecycle_daily_collection`, doc id `{account_id}:{UTC day}` | the day's janitor snapshot (last run of the day wins) plus counters other code increments (§5.5) | the janitor, the consolidator, the review (§5.5) |
| Janitor line mark | `fact_lifecycle_lines_collection`, doc id = the UTC day | claims the day's single 🧹 ops line | the janitor |
| Janitor run guard | `fact_lifecycle_run_collection`, document `sweep` | `run_owner`, `run_until`: at most one janitor run at a time | the janitor |
| Review questions | `fact_review_questions_collection`, doc id = the question id | one question per fact (status, quoted text, queue place, posted message, the owner's reply and answer); a closed one carries `purge_after` | the review service, `FactAnswerService` (§5.6) |
| Review messages | `fact_review_messages_collection` | the claim that lets one weekly digest go out per ISO week, and quote translations; both carry `purge_after` | the review service |
| Review state | `fact_review_state_collection`, doc id = account id | when the review last ran or failed, offers per fact, the current question and today's send count, digest marks, alert marks, message language and labels | the janitor (enqueue and alert marks), the review service, `FactAnswerService` |
| Profile data | `confirmed_people_collection`, `profile_answers_collection`, `person_cards_collection` | the owner's confirmed people list, his answers to profile questions, the derived people cards (§3 step 9b) | `src/adapters/firestore_confirmed_people_repo.py`, `FactAnswerService` (profile answers), the profile step (cards) |

**No TTL policy** may exist on the facts, janitor-day, line-mark, run-guard, review-state collections or any field named
`expires_at` of facts: `expires_at` there is a decision to stale, not a deletion time. A test walks the tree and fails on
one (`docs/04_solution_strategy/decisions/fact_expiry_and_janitor.md`). The two review collections that expire carry a
different field, `purge_after` (§5.6).

---

## 2. A fact

### 2.1 The fields that matter

`FactEntity` (`src/domain/entities.py::FactEntity`):

| Field | Meaning |
|---|---|
| `id`, `account_id`, `created_by_user_id` | document id; tenant (every query filters `account_id`); author |
| `text` | the fact, one self-contained sentence; the only thing most readers show |
| `vector`, `tags_vector`, `metadata_vector` | the three embeddings (§2.6) |
| `tags` | keywords; `mindset` makes the fact a principle (§4.1); `consolidated` is added to every fact and `anchor` to principles (`src/domain/entities.py::FACT_SYSTEM_TAGS`); an update keeps both (§2.4) |
| `type` | `FactType`; the consolidator does not set it, a created fact defaults to `event` |
| `metadata` | structured values (numbers, dates) |
| `directive`, `temporal_class`, `state`, `context_priority` | the consolidator's directive decision and three axes (§2.2) |
| `context` | a short label for why it was recorded; shown by `search_memory` |
| `reported_date` | when the owner last stated the fact; code owns it per pass (§2.7) |
| `created_at` | ordering of the baseline and the Cabinet lists; a version copies it from the document it replaces, so it is the fact's first mention |
| `version`, `lineage_id`, `replaces_fact_id`, `superseded_by`, `is_current`, `valid_from`, `valid_to` | §2.3–§2.4 |
| `expiry_kind`, `ttl_days`, `expires_at` | the expiry decision, written by code from the model's `expiry` argument (§2.7). Read by the janitor (§5.5) |
| `stale_since`, `lifecycle_events` | `stale_since`: when the janitor staled the fact; it orders the review queue and is cleared whenever the fact comes back to `current`. `lifecycle_events`: the newest `LIFECYCLE_EVENTS_CAP` state transitions `{at, from, to, actor, reason}` with code-built reasons, written in the same write as the transition, never shown to a model |
| the retrieval profile: `label_words`, `time_points`, `place_keys`, `people`, `primary_domain`, `domain_keys`, `user_identity`, `kinds`, `sensitivity`, `profile_*`, `profiled_at` | `src/domain/fact_profile.py::PROFILE_FIELDS` is the set. Optional; only the profiler step writes them, through `FirestoreFactRepository.write_fact_profile`, a compare-and-set on `profile_fingerprint`. Readers: the fact search (§4.3), the baseline's identity block and section labels, Lelik's voice filter (§4.1, §4.6), the Cabinet's domain filter and badge (§4.8) |
| `last_updated` | written on every in-place write; read by no code (`tests/unit/test_fact_field_registry.py::WRITE_ONLY_BY_DESIGN`) |
| `visibility` | defaults to `account_shared`; never filtered on read: every member of an account sees every fact |

The profile is made by `src/agents/fact_profiler_agent.py::FactProfilerAgent`, one structured model call per fact; the
The domains live in one registry, `src/domain/fact_domains.py`. The consolidator does not classify subjects; it decides
only whether a fact is a directive (§2.2).

`FirestoreFactRepository` also stores `context_priority_rank` (critical 1 … archival 5) beside the entity for ordering
(`src/adapters/firestore_repo.py::FirestoreFactRepository._to_document`).

### 2.2 The directive decision and the three axes

| Axis | Values | What code does with it |
|---|---|---|
| `directive` | `true` / `false` (default `false`) | a standing directive: in the rulebook, kept out of the baseline fill, expiry pinned to `none`. Decided in one place, `src/domain/fact_lifecycle.py::is_directive`; every read that selects directives filters `directive == true` (§4.2) |
| `temporal_class` | `permanent`, `stable`, `dynamic`, `ephemeral` | none; a prompt rule makes it the default for the expiry decision when no row of the kinds table applies (§2.7) |
| `state` | `current`, `stale`, `archived`, `superseded`, `invalidated` | **the only visibility switch**: every reader filters `state == "current"`. Only the janitor sets `stale` (§5.5); the model is refused it everywhere (§2.7). A fact whose question waits for the owner is `current` (§5.6) |
| `context_priority` | `critical`, `high`, `medium`, `low`, `archival` | baseline order; `archival` (rank 5) is never in the baseline or the rulebook (`FirestoreFactRepository.get_active_facts_ordered`) |

The model writes the three axes in upper case; `normalize_fact_taxonomy` lowercases them before the enums parse and reads
the retired priority `historical` as `archival` (`src/domain/entities.py::normalize_fact_taxonomy`). A value outside the
enum fails the tool call (§6).

### 2.3 States and the SCD2 flags

`src/domain/fact_lifecycle.py::lifecycle_fields` maps a state to its flags; every retirement path uses it.

| State | `is_current` | `valid_to` | `superseded_by` | Typical cause |
|---|---|---|---|---|
| `current`, `stale` | true | none | none | created; `stale` is set by the janitor when the expiry has passed. A stale fact the review cannot decide is made `current` again while its question waits (§5.6) |
| `archived` | true | now | none | "was true, no longer" (Negation rule) |
| `invalidated` | true | now | none | "was never true"; Stage 2b removal, Cabinet, backstop |
| `superseded` | **false** | now | the successor's id | a document replaced by a newer version of its lineage, a merge source, or a compound fact the model split (it must name its replacement) |

`is_current` means "latest document of its lineage", not "visible"; visibility is `state` alone. No `src/` code reads
`is_current`; the data audit checks it (§9.6).

### 2.4 Updates, merges, versions, lineage

A fact is a **lineage**: the documents that share one `lineage_id`, told apart by `version`. At most one is the live one.
Two kinds of write (`src/domain/fact_update_plan.py`):

- **A content change is a new document.** An `update_fact` that changes the text, the tags, the metadata or the
  `temporal_class` writes a new document: new `id`, same `lineage_id`, `version` + 1, `replaces_fact_id` = the old id,
  `created_at` copied, the expiry decision recomputed, the old trail plus the version's own entry. The old document is
  closed in the **same transaction** (`state=superseded`, `is_current=false`, `valid_to`, `superseded_by` = the new id;
  `FirestoreFactRepository.write_superseding`). The result's `fact_id` is the **new** id. Tags are compared as
  multisets, so resending them in another order is no change.
- **A lifecycle change stays in place.** A call that changes only `state`, `reported_date` or `expiry` writes just those
  fields (`src/domain/fact_lifecycle.py::IN_PLACE_FIELDS`) onto the live document through
  `FirestoreFactRepository.update_fact_fields`; `version` and the id stay. A state change together with a content change
  goes into the new version.
- **`directive` and `context_priority` cannot be changed by an update**: the tool has no such fields. A demotion creates
  a fact with `directive: false` and invalidates the directive.
- **Retiring a fact for another one needs the other one.** `state=SUPERSEDED` needs `superseded_by` (resolved forward
  like any id, and a different fact); otherwise the call is refused (`src/domain/fact_update.py`).
- **Tags.** The system tags survive a tags update. `tags: null`, `tags: []` and an empty `content` are refused: an empty
  field never destroys data.
- **Everything is checked before anything is embedded or written** (`src/domain/fact_update.py`,
  `src/domain/new_fact_attributes.py`, the expiry rules of §2.7): enums, dates, tags, content, `expiry`, on create,
  update and merge. A fact cannot be created or merged into `superseded` or `stale`.
- **Vectors follow content.** All three are re-embedded when the three embedding texts change (§2.6), before anything is
  written (`FactManagementAdapter._complete_version`); an embedding failure fails the call with nothing stored.
- **A stale id still works.** The model reuses ids from an earlier search. `update_fact` and `merge_facts` follow
  `superseded_by` from the id they got to the live document (`FirestoreFactRepository.resolve_current`, walked by
  `src/domain/fact_lifecycle.py::forward_step`, at most `MAX_FORWARD_HOPS`). They stop with an error when the chain leaves
  the lineage (merged: "merged into <id>; search again"), a superseded document has no successor (documents closed before
  the version chain existed), the end is `archived`, `invalidated` or `stale`, or the chain is too long. The adapter
  returns each as `status: "failed"` (§6). Only a `current` document can be updated by the consolidator
  (`CONSOLIDATION_ACCEPTS`); the review also accepts `stale` (`REVIEW_ACCEPTS`); the Cabinet accepts every state but
  `superseded` (`CABINET_ACCEPTS`, §5.2). A followed update carries `resolved_from`.
- **Writes carry a precondition, not a lock.** Every write reads the document and writes only if its `update_time` is
  unchanged. On a conflict the adapter resolves again and tries once more; a second conflict is a failed result
  ("changed concurrently; search again") (`FactManagementAdapter._update_with_one_retry`). **A content or tags update is
  also refused when the second read lands on a different document than the first**: they replace the whole value, so
  written over a version the model never saw they would silently drop that version's change. Metadata, `temporal_class`,
  `state` and `reported_date` updates re-resolve and apply to the newer version. A later call that still sends the old id
  is forwarded and applied over the live version.
- **A merge is one transaction.** `merge_facts` resolves every source forward, builds the merged fact (new id, new
  lineage, three vectors) and creates it while closing every source with `superseded_by` = the merged id
  (`FactManagementAdapter.merge_facts`). A failure writes nothing, so the sources stay live. A missing or foreign source
  is skipped; no usable source is "Failed to supersede any facts"; a source that ends in another lineage, a non-current
  state, or moved forward between the two attempts fails the whole merge.
- **History.** The old wording stays in the closed document. There is **no Cabinet view or script for old versions**: in
  the Firestore console filter the facts collection by `lineage_id` and sort by `version`; a merged fact starts a new
  lineage whose sources are the documents with `superseded_by == <its id>`. `FirestoreFactRepository.get_lineage` returns
  the same, without vectors; the only caller is the review's prefetch of "earlier versions" (`FactReviewService._task`).
- **A retry can add a version.** A consolidation batch that fails and is retried runs Stage 1 again over facts it already
  wrote; an update it repeats on the live version can add one more version document (§6).
- `ConsolidationAgent._handle_consolidate_cluster` (task `consolidate_cluster`) has no live caller: only dry-run scripts
  reach it. It runs without the lease and the run tracker, so it has no failure refresh (§3 step 9).

### 2.5 Tenancy

Every read filters `account_id`. `resolve_current` returns nothing for a document of another account, decided before it
follows any pointer: `update_fact` answers `not_found` (the same words as for a missing id), `merge_facts` skips the
source, and a pointer that leads into another account is "cannot be resolved" and never names the foreign id. Cabinet
invalidate checks the stored account and returns 403 (§5.2). `visibility` is not enforced (owner ruling).

### 2.6 The three vectors

`src/domain/fact_embedding.py` defines the texts, shared by creation and re-embedding: `text`; `tags` joined (system
tags left out; the text when none is left); `metadata` as JSON (the text when empty). All three are embedded with
`RETRIEVAL_DOCUMENT` in one batch call by `src/services/fact_write_service.py::FactWriteService.build_facts`, which builds
the `FactEntity`; the fact management adapter stores it. Model: `src/adapters/gemini_embedding_adapter.py`, 768
dimensions. Searches embed queries with `RETRIEVAL_QUERY`.

### 2.7 Expiry, `reported_date` and the trail

**The model decides, code computes.** `create_fact`, `merge_facts` and an `update_fact` that makes a new version need an
`expiry` inside `fact_attributes` / `updates`; an `update_fact` that sends only `expiry` (and optionally `reported_date`)
edits the document in place and recomputes. Three answers (`src/domain/fact_expiry.py::parse_expiry`):
`{kind: date, date: YYYY-MM-DD}` the last day the fact holds; `{kind: horizon, days: N}` review it if the owner has not said
it again within N days (`MAX_HORIZON_DAYS` bound); `{kind: none}` history, stable or permanent. `null` is refused with an
explanation. The model's `ttl_days` is dropped at every parse boundary. Which kind a fact gets is decided by the
`CONSOLIDATION_TAXONOMY` token (planned event / validity → `date`; current activity, changing value, noise → `horizon`;
past event, stable, directive, principle → `none`).

**What code writes** (`src/domain/fact_lifecycle.py::compute_expiry`): `date` → `expires_at` = the start of the next
local day in the **owner's timezone**, as UTC (so it is due at the end of that day, DST-safe); `horizon` → `reported_date`
+ N days and `ttl_days` = N; `none` → no `expires_at`. The owner's zone is resolved once per consolidation run
(`ConsolidationService._run` through `src/services/owner_timezone_resolver.py`, deciding with
`src/domain/owner_timezone.py`; an unresolvable one is UTC with one WARNING) and travels in
`message.context["owner_timezone"]`.

**Born due is refused** (`src/domain/fact_lifecycle.py::born_due`, `resolves_too_soon`): a decision that resolves at or
before now + 1 day would be stale on arrival. Which refusal and wording depends on the pass
(`src/domain/pass_actor.py::PassActor`, the one table of whose dates count, whether a due horizon is floored, which
stored states a write may start from and how a refusal reads):
- a past or today's `date` is refused on every pass; on Stage 1 and mail with "discard the candidate, or rewrite it as
  history with `{kind: none}`", on a cleanup pass with "do not drop the fact because its date passed" (the subject is a
  stored fact, there is no DISCARD);
- a horizon that ends at or before now + 1 day is refused on Stage 1 and mail with the minimum N;
- **cleanup passes** (Stage 2a, Stage 2b, the directive backstop, `consolidate_cluster`; they cannot move `reported_date`)
  are not refused for a due horizon: `expires_at` becomes now plus `_CLEANUP_FLOOR`, kind and days are kept, and the
  result carries a note, so the fact reaches the review soon (owner ruling).
Every expiry refusal ends "Nothing was written. Resend this call with a valid expiry before retiring any fact it
replaces." (`src/domain/fact_expiry.py::NOTHING_WRITTEN`) and the tool result is `status: failed, refused: "expiry"`,
always before any embedding or write.

**Pin and closing states.** Directive, principle and `mindset` facts take `none` (`expiry_pinned`, `check_pin`): a missing
`expiry` is filled in as `none`, a `date` or `horizon` is refused. An `expiry` sent with a closing state is ignored with a
note. `state=stale` is refused from the model on update, create and merge: only the system marks a fact stale.

**`reported_date` means "when the owner last stated it", and code owns it** (`src/domain/fact_write_context.py`). Each pass
has a write context (`FactWriteContext`): the actor, the owner's zone, the **span** (first and last message time of the
batch; for mail the oldest and newest `email_date`) and whether the model's date counts. On Stage 1 and mail the model's
`reported_date` is clamped into the span (never after now) and used; a missing one is the span end. A merge is never dated
before its newest source. A create or merge on a cleanup pass ignores a model date and takes the latest date of its
sources (`derived_from` for a create). An update never moves the date back, and a cleanup pass keeps the stored one. A
naive ISO value is read as UTC (the clamp bounds a wrong guess). **Extension rule** (Stage 1 and mail tokens only): when
the owner says an existing fact again and the conversation confirms it held all along, the model sends
`update_fact({reported_date: ...})`; a horizon then counts from the new date and never ends earlier than before.

**Review and owner writes.** The review pass and the owner's answers write through the same update path with their own
pass actors (`review`, `owner`, and `timeout` for a silence that writes history). They differ from the consolidation
passes:
- **A horizon counts from now** (`horizon_from_now`): a revive, a rewrite or a fallback gives N days from the moment of
  the write, under the strict born-due rule with no floor (N at least `MIN_REVIEW_HORIZON_DAYS`, a `date` must be ahead).
  A pinned fact always gets `none`.
- **`reported_date`**: a revive on evidence takes the later of the stored date and the evidence fact's date; on a reply or
  an owner answer, the later of the stored date and the answer time; a rewrite, a queue write and a fallback keep it. The
  review never sends a date (`reported_date_for_update`).
- **A confirmation keeps the decision it can** (`src/domain/fact_review_rules.py::confirmed_expiry`): a pinned or `none`
  fact stays permanent, a `date` still ahead keeps its date, anything else gets a horizon of the fallback window.
- **A fact waiting for the owner is `current` with a past `expires_at`.** Queueing clears `stale_since` and leaves the
  expiry as stored; every reader sees an ordinary current fact; the janitor skips it while its question is open (§5.5).
- **Silence doubles the window** (`src/domain/fact_review_rules.py::fallback_window_days`): a fact that falls back to `revive` gets twice
  the window its last revive gave, between `MIN_FALLBACK_DAYS` and `MAX_FALLBACK_DAYS`.
- **A fact whose date passed becomes history, never an archive.** The review or an owner's answer sends `as_history`:
  `updates {state: CURRENT, content: <the history text>, expiry: {kind: none}}`. It is a **version** in the same lineage
  at **`archival` priority** (out of the baseline, found by every search), with `metadata` emptied and `context` cleared
  before the embedding texts are built. Only the review, the owner and a timeout may write one
  (`src/domain/fact_update_lifecycle.py::finish_fact_update`). The text is English, past tense, at most 40 words, keeps
  the date at the fact's precision and drops identifiers. Archive is kept for noise, an end the owner states, and the
  Cabinet.

**What the model sees.** The consolidator's search results carry `expiry` in the shape it sends, `due` (the local end date
of a horizon) and `reported_date` as an ISO time with the owner's offset; never `ttl_days`, `expires_at` or
`expiry_kind` (`src/domain/fact_expiry.py::expiry_view`). A fact with no `expiry_kind` shows `expiry: null`, which Stage 2a
tells the model means "classified separately". Every conversation line and every pass's user message uses one timestamp
formatter, in the owner's zone, with year and weekday (`src/domain/local_time.py`).

**The trail.** Every state change writes a `lifecycle_events` entry in the same write, with code-built reasons only
(`src/domain/fact_lifecycle.py::lifecycle_event`, `with_event`; the review's reasons in
`src/domain/fact_review_reasons.py`). A version starts with its predecessor's trail plus its own entry; a merge or version
close appends to each closed document. Actors are the pass labels plus `cabinet`, `janitor`, `migration` and `review_fallback` (a kept-twice queue write, `KEPT_TWICE_ACTOR`). The trail is
how §9.2 step 7 tells who removed a fact.

---

## 3. A fact's life, step by step

### Step 1 — the message is saved

`FirestoreSessionStore.append_messages_batch` adds the turn to the session `{user_id}:{channel_id}` in one transaction.
A user message keeps its full text. A bot reply stores two texts: `full_text` and `text`, the summary Smart writes for
history. Notifications, reminders, the daily mail review, call summaries and `$memory save` pairs are messages in the
same sessions and count toward the window.

### Step 2 — the sliding window overflows

When the session holds more than `threshold` messages after an append, the oldest `batch_size` form a batch, repeatedly
until the rest is back under the threshold. The values are `src/domain/settings.py::ConsolidationSettings` (threshold and batch size),
lowered by `load_settings` only for the `is_production` environment, which is not the live one; they are global:
`consolidation_batch_size` of the user only sizes the `$consolidate` cut (§5.4). A channel bound to a companion uses that
binding's own window (`threshold_resolver`) and routes elsewhere. The append is stored whole; a batch leaves the session
only after it was queued (step 3). The values are read at every append, but a batch keeps the boundaries it was formed
with (step 3), so a changed threshold or batch size applies to the messages after the batches already formed.

- **No time trigger.** A channel that never passes the threshold is never consolidated on its own; a session idle for 90
  days is deleted on its next load, unconsolidated messages included (`FirestoreSessionStore.load_session`).
  `$consolidate` is the manual lever.

### Step 3 — the batch is queued

One tracked background task after the append's commit (`FirestoreSessionStore._hand_off`) hands each batch, oldest first,
to `OverflowRoutingService.route_overflow` and then cuts it with `FirestoreSessionStore.remove_head` (a transaction that
removes exactly those messages if they still start the history). If the hand-off raises, that batch and the later ones stay
in the session and the next append over the window hands them off again. The batch id is `handoff_key` of the messages
(`src/domain/consolidation.py::handoff_key`) and the queue stores a batch only if its id is absent, so a repeat finds the
first record and leaves it as it is. **A repeat is the same batch even when the window changed in between** (a Cabinet
edit of a companion's `window_threshold`/`batch_size`, new `ConsolidationSettings` on a deploy): the append's transaction
also stores `handoff_plan` on the session, the size and `handoff_key` of each batch it formed, and the next append forms
those batches first, as recorded, before it applies the current values to the rest
(`src/domain/consolidation_window.py::overflow_window`). `remove_head` drops the cut batches from the plan in its own
transaction; a cut that does not end on a recorded boundary (`$consolidate` cuts its own size) drops the whole plan. A
planned batch no longer found at its place (`save_session` replaced the history) ends the plan there, and a session
stored before the plan existed forms its batches from the current values. **A hand-off that fails every time** is bounded by
`src/adapters/firestore_session_store.py::OVERFLOW_CEILING_WINDOWS`: past it each append cuts its oldest batches (a
planned one by its own size) without a hand-off and logs a 🚨 ERROR; those messages are lost to memory (the only lossy
path). The ceiling follows the current window, so lowering the threshold can bring a session with failing hand-offs past
it at once.

`route_overflow` (`src/services/overflow_routing_service.py::OverflowRoutingService._route_to_alek`):
1. `serialize_messages_for_consolidation` (`src/domain/consolidation_serialization.py`): **user parts** → the user's text;
   **bot parts → `text`, the summary**, never `full_text` (verbose, carries web-search context). A memory-review question
   reaches the batch as an ordinary pair of messages (§5.6) that Stage 1 reads under the narrowed echo-trap exception
   (step 5).
2. A `ConsolidationBatch` is stored with status `pending` under id `batch_<handoff_key>`.
3. A Cloud Task `consolidation` is enqueued for the user (`src/adapters/gcp_task_queue.py`). A failure of step 2 or 3
   is logged and re-raised, so the session store does not cut the batch.

`/worker` → `WorkerHandler._handle_consolidation` → `ConsolidationService.process_user_batches(max_batches=1)`
(`src/services/consolidation_service.py::ConsolidationService`). Each batch runs under the account's **memory-write
lease** (`ConsolidationService._consolidate_one`):

- **One lease per account** (`memory_write_leases_collection`, a fresh random owner per batch), held for the run timeout
  plus `LEASE_MARGIN` (`src/domain/memory_write_lease.py`; the run timeout is `CONSOLIDATION_CFG.timeout_ms`, wired in
  `src/composition/memory_setup.py::build_memory`; adapter `src/adapters/firestore_memory_write_lease.py`). Any unexpired
  lease means "busy". So two users of one account, two tasks of one user, `$consolidate` and the review all queue behind
  each other. Why: every fact write is a precondition write (§2.4), but two runs searching and deciding at once still
  produce duplicates or contradicting decisions; one writer per account removes that
  (`docs/04_solution_strategy/decisions/fact_versions_one_writer.md`).
- **Busy: it defers itself.** If the user has any open batch (`pending`, `retry_pending`, `processing`), the run enqueues
  a `consolidation` task with a `LEASE_BUSY_RETRY_DELAY_S` delay and returns having done nothing
  (`ConsolidationService._defer`). The dedup key is per delay-wide bucket (`defer_bucket`) and Cloud Tasks hashes it into
  the task name, so searching the queue for "consolidation-defer" finds nothing. Losers at the same time share one
  deferral, and a loser that loses again lands in the next bucket. With no open batch a busy run just stops.
- **Resets zombies, and counts them.** Under the lease, a `processing` batch older than the lease margin is a dead run's:
  it goes back to `retry_pending` with `attempts` + 1, and at `MAX_BATCH_ATTEMPTS` goes `failed` (error "run lost while
  processing") (`FirestoreConsolidationQueue.reset_recoverable_batches`). A run that kills its worker never records its own
  failure, so without this count a batch that crashes the instance every time would loop for ever. The hourly sweep takes no
  lease and uses the longer `_ZOMBIE_THRESHOLD_SECONDS` threshold, with the same count.
- Takes the **oldest** pending batch (`pending` or `retry_pending`, ordered by `created_at`; the composite index in
  `config/firestore.indexes.json` must be `READY` before the code is deployed). With less than the margin of the run budget
  left it starts nothing: the batch stays pending, the lease is released, a deferral is enqueued.
- Marks it `processing`, runs the agent with `consolidate_full`, the time left on the lease as its timeout and a single
  attempt (no whole-run transient retry), and on success **deletes the batch** (`ConsolidationService._process_one_batch`).
- **Releases the lease** in a `finally` (only the owner that took it can). A failed release is logged and ignored, since
  the lease expires anyway; a failed acquire, open-batch check or deferral enqueue raises, so the worker answers 5xx and
  Cloud Tasks retries.
- **An internal error inside the batch** (a store or queue error, not an agent failure) is logged and swallowed. The chain
  of next tasks continues only when the broken batch is no longer `pending` **and** another batch waits
  (`_has_other_pending`); a broken batch still `pending` would be the next run's oldest again, an endless chain of
  immediate tasks, so it waits for the hourly sweep.
- **One batch per task**: if more remain, the worker enqueues the next task at once
  (`docs/04_solution_strategy/decisions/consolidation_one_batch_per_task.md`: Cloud Run throttles CPU after a response, so
  each batch gets its own request). A deferral returns "no more", so a deferred task does not start a second chain.
- **The account comes from the user's profile only, never from the user id** (owner ruling: facts are personal data, and
  a fallback would write them under the wrong key). A user with no profile or an empty account id cannot be leased: the
  oldest pending batch is marked `failed`, logged `CONSOLIDATION_FAILED`, nothing is leased, routed or retried
  (`ConsolidationService._account_to_lease`).
- **The owner's timezone** is resolved once per run (§2.7).

There is **no per-write fence**: nothing checks the lease before each fact write. The run is bounded by its timeout
instead (the agent stops at `lease_until` minus the margin).

An hourly sweep (`sweep_consolidation`, Cloud Scheduler, `docs/07_deployment/SCHEDULERS.md`;
`WorkerHandler._handle_sweep_consolidation`) takes every user with any batch left in the queue. For each it first
**revives that user's `failed` batches** (`ConsolidationService.revive_failed_batches`: back to `retry_pending`, attempts 0,
`revivals` + 1) and then enqueues the normal task. A batch is revived while `revivals` is under `MAX_FAILED_REVIVALS`, so
one batch costs at most (1 + `MAX_FAILED_REVIVALS`) rounds of `MAX_BATCH_ATTEMPTS` runs in its life
(`docs/04_solution_strategy/decisions/consolidation_failed_batch_bounded.md`). Past the cap it stays `failed` with its
messages in the queue document and one `CONSOLIDATION_GAVE_UP` line is logged (§9.1).

### Step 4 — the consolidation run

`ConsolidationAgent.execute` needs `account_id` from the request context, loads the baseline cache (a failed read is
logged and the run goes on without it) and dispatches `consolidate_full` → Stage 1, 2a, 2b
(`ConsolidationAgent._execute_deliberate_process_v3`), then Stage 3 (`ConsolidationAgent._handle_consolidate_full`). The
agent also accepts `consolidate` (Stage 1–2b only), `consolidate_cluster`, `consolidate_email` and `review_stale_facts`
(§5.6); the queue and `$consolidate` always send `consolidate_full`. An agent built without a fact management port refuses
with one ERROR line and writes nothing.

- **Model:** agent type `consolidation`, default provider Claude, **no cross-provider fallback**
  (`src/services/agent_context_builder.py`, owner ruling: a memory write never moves to another provider on an outage; the
  Claude adapter alone retries once on the next model down on a 5xx or 529, `src/adapters/claude_adapter.py`;
  the transcript is locked to one provider after the first tool call anyway, `src/agents/CLAUDE.md`), tier PERFORMANCE
  (`src/domain/user.py::_DEFAULT_AGENT_TIERS`), but on Claude the model is pinned to
  `ConsolidationAgentConfig.claude_model` (`src/composition/consolidation_model_setup.py::consolidation_context`); a
  user's `model_overrides`, `agent_providers` and `agent_tiers` can still override. Thinking, output tokens, turns per pass and the agent
  timeout are `src/infrastructure/agent_config.py::ConsolidationAgentConfig` and `CONSOLIDATION_CFG`; the service passes
  the run only what is left of its lease, so a run ends before its lease does.
- **Prompt:** one system prompt per pass, built by `PromptBuilder.build_for_agent(agent_type="consolidation",
  include_directives=False)`. Profile `prompts_snapshot/profiles/consolidation.yaml` assembles the six
  `CONSOLIDATION_*` tokens (blueprint `consolidation_agent_v1`); then a `knowledge_base {}` block with the baseline facts
  and, in Stage 1 only, the conversation. Standing directives are never in this prompt as rules: the consolidator curates
  them as records (Stage 2b).
- **Loop:** `ConsolidationAgent._run_consolidation_loop`: each turn sends the whole history with the tool declarations
  (`_get_tool_declarations`); tool calls of one turn run in parallel; a turn with no tool call is the final report.

### Step 5 — Stage 1: conversation → facts

The conversation sits in the system prompt as lines `User (<stamp>): …` / `Assistant (<stamp>): …` in the owner's zone
(§2.7); the user message is "Begin deliberate consolidation." plus `Today: <date> (<zone>, the owner's timezone).`, which
every pass's user message carries. The process (`CONSOLIDATION_COGNITIVE_PROCESS` token): extract candidates → decide
`directive` and the three axes → **search once per candidate** → analyse → decide → execute → verify → report.

The tools (`ConsolidationAgent._execute_fact_management_tools`, adapter
`src/adapters/fact_management_adapter.py::FactManagementAdapter`):
- `search_existing_facts(keywords, primary_query, alternative_query, limit)`: the multi-vector engine of §4.3 with
  per-channel limits and **no semantic dedup**; returns id, text, `primary_domain`, `directive`, class, state, priority,
  tags, metadata, reported date, `expiry`, `due`, version. `similarity` is **null** for every vector hit.
- `create_fact(content, fact_attributes)`: requires `temporal_class`, `context_priority` and `expiry`; `directive` is
  required by the schema and read as `false` when missing; written **without deduplication**.
- `update_fact(fact_id, updates)` and `merge_facts(fact_ids, merged_content, fact_attributes)`: §2.4, §2.7.
- `count_words(text)`: word count against the 40-word fact limit.

**Deduplication is the model's decision.** Code does not reject a near-duplicate on this path; the prompt makes the model
search first and DISCARD (no tool call) what is already known. `FirestoreFactRepository.add_fact_if_unique` has no
production caller.

**Rules that decide what is stored** (prompt tokens, mirrored in `prompts_snapshot/tokens/system/`):
- `Domain_Scope` (POLICIES): never process ASSISTANT statements as facts unless the USER confirms with new information;
  never store facts from questions. **The one exception**: a USER message that begins `[Reply to memory review q=<id>]`
  is the owner answering the memory-review question framed by the earlier `[System: memory review q=<id> …]` pair. The
  quoted fact belongs to the review, which applies the answer, so Stage 1 does not update, extend, invalidate or
  re-create it; it records only what the answer adds (a correction that names the true value). A bare digit, "yes", "no",
  "stopped" or "don't remember" add nothing (`tests/unit/prompts/test_consolidation_echo_trap_exception.py`).
- `Trivial_Exclusions` (TAXONOMY): no logistics, outbursts, chitchat, meta talk, UI commands, unanswered questions, file
  operations, and the **RAG echo trap**: the assistant recalls a fact and the user only confirms it: discard.
- Conflict rules: a value measured or counted over time updates with history in the text; identity and values need
  explicit correction; negations → `invalidated` ("never true") or `archived` ("no longer true").
- An existing fact over 40 words is split before an update: create the atoms, then mark the old one `superseded`
  **with `superseded_by`**.
- `Directive_Maintenance` (TAXONOMY): `directive: true` only when the rule is about **how the agent behaves** AND is
  **in force across most requests**; a conditional agent rule is an ordinary fact (§4.2).
- Expiry and dating (TAXONOMY, TOOLS, EXAMPLES, COGNITIVE_PROCESS): the kinds table and its date rules, the extension
  rule (§2.7). The owner's rule behind them: when unsure, keep the fact.

**The run tracker.** `_execute_deliberate_process_v3` makes one `_TrackingFactUpdates` per run and passes it to every
pass as the tools' port, so two passes of one agent instance never record each other's writes. It records only what the
store reports as written: `changed` holds `(id, text)` for a created fact, a merged fact and a **content** update; `wrote`
is set by any successful write. A refused or failed result records nothing. `changed` seeds Stage 2a; it is not every id
that changed (a tags-only version has a new id and is not in it).

### Step 6 — Stage 2a: cluster review (only if Stage 1 wrote)

Skipped when `changed` is empty, so a Stage 1 that only archives or invalidates skips it. For each written fact it
searches the store, unions the hits by id and keeps a capped cluster of **current** documents
(`ConsolidationAgent._build_review_cluster`). A fresh loop gets the "FACT CLUSTER REVIEW" message
(`_build_cluster_message`): merge repeats, decompose compounds, fix inconsistencies, no fact over 40 words, keep numbers
and dates. It sees no conversation. It is a cleanup pass: a due horizon is floored, the stored `reported_date` is kept, and
each part of a split fact gets `derived_from` (§2.7).

### Step 7 — Stage 2b: the directive rulebook (every run)

Unconditional, even when Stage 1 did nothing; a no-op only when the rulebook is empty
(`ConsolidationAgent._review_directives`). It reads up to `DIRECTIVE_REVIEW_FETCH_LIMIT` current non-archival directives
and sends the whole rulebook with the "STANDING DIRECTIVES REVIEW" message (`_build_directive_review_message`): one
checklist line per record, then per record DEMOTE (create a fact with `directive: false` stating the condition, and
invalidate the directive), INVALIDATE, MERGE, or rewrite to the target form (imperative, English, terse, one rule), hard
cap `DIRECTIVE_HARD_CAP`. Then the **code backstop** `ConsolidationAgent._enforce_directive_cap`: if more than the
cap remain current, the tail in (priority, newest first) order is invalidated through the same `update_fact` (state only,
so in place, trail reason `directive cap`); a result that is not `updated` is logged at ERROR and posted as one 🚨.
Run-to-run results differ on identical input (`docs/04_solution_strategy/decisions/directive_applicability_gate.md`).

### Step 8 — Stage 3: indexed mail

Runs only after Stage 1–2 succeeded, and is **best-effort**: it runs under what is left of the run's budget minus
`_STAGE3_MARGIN_S` and is skipped under `_STAGE3_MIN_S`. A Stage 3 error or timeout is logged as one ERROR line and the
batch still succeeds: Stage 1 does not re-run, and the mails stay unconsolidated for the next run. It is skipped when the
mail repository or its card reads (`EmailMemoryBatches`) are not wired; nothing is read or marked then.

Stage 3 reads mail with its **card** (`ConsolidationAgent._consolidate_email_cards`): the prompt is the
`consolidation_email` profile (the six tokens plus `CONSOLIDATION_EMAIL_CANDIDATES`; a build failure fails Stage 3 before
anything is read or marked), the owner's confirmed people list is read once, and each pass
(`ConsolidationAgentConfig.email_triage_passes`) reads up to `email_card_batch_size` unconsolidated mails: every one whose
card is settled (`src/domain/email_memory_candidate.py::SETTLED_FOR_MEMORY`), plus every one indexed longer ago than
`MEMORY_CARD_WAIT` whatever its card (no online retry of `pending` exists, so strict waiting would starve those mails),
oldest `indexed_at` first. The message is one code header line (`EMAIL_CANDIDATES_HEADER`), then one numbered JSON object
per mail:
- a carded mail whose card reads back is sent as its code-filtered card (`card_candidate`: sender, subject, the sent
  moment in the owner's zone as `date`, the card's type, life areas, title, summary, verified time points, the names and
  values the mail prints, attachments with `read` true or false, the printed people);
- a mail whose card is `empty` is not sent;
- every other mail (`abandoned`, `source_gone`, unsettled past the wait, a `carded` card that does not read back) is sent as
  its headers (`header_candidate`). No snippet.

Each candidate's `date` is the sent moment with its offset; the token tells the model to copy it into `reported_date`, and
the pass's span is the oldest and newest sent date (§2.7). Title and summary are withheld together when they name a listed
person whom the card does not link, and on every card when the stored list does not parse. Every mail of the batch,
skipped ones included, is marked consolidated after its pass, whatever the model did; a pass whose mail is all skipped
makes no model call. A card written later never clears the mark. The dry run of the pass is
`scripts/email/test_consolidation_dryrun.py` (§9.5).

### Step 9 — the baseline is rebuilt; the batch is deleted

After Stage 1–2b the agent's cache refresher rebuilds the baseline document; Stage 3 rebuilds it again when it processed
mails. The refresher is `BiographicalContextService.refresh_cache` (port
`src/ports/biographical_cache_port.py::BiographicalCacheRefresher`, handed to the agent through `AgentServices`): it selects
the baseline (§4.1) and saves it with `FirestoreFactRepository.save_biographical_context_cache`. `refresh_cache` logs and
swallows its own errors, so a failed rebuild leaves the previous cache. `ConsolidationService` then deletes the batch.
The Cabinet invalidate (§5.2), the janitor (§5.5, per account it changed), the review, the owner's answer and the profile
step take the same refresher.

**A run that fails after it wrote refreshes too.** Stages 1–2b run inside a `try/finally`: if the run did not finish and
its tracker saw a write, the `finally` rebuilds the cache; Stage 3 does the same. A run that failed without writing does
not refresh. That is a cache rebuild, not a fact write, so it does not break the one-writer rule. A worker that dies
mid-batch refreshes nothing (§6).

### Step 9b — the profile step

After the baseline is rebuilt and still inside the memory-write lease, `FactProfileService.run`
(`src/services/fact_profile_service.py::FactProfileService`, `docs/10_rfcs/FACT_PROFILE_RFC.md` §4.2, §4.4) brings the
account's fact profiles up to date. It runs in both leased runs: `ConsolidationService._consolidate_leased` after a batch
ended `CONSOLIDATED` or `FAILED`, and `FactReviewService._daily` after the review. The lease's `until` is its deadline. A
step that raises is logged and changes nothing of the run's result.

- **Order:** one projected scan of the current facts, classified by `classify_profile`; free work first (a stored answer is
  re-derived without a call; a standing directive gets its fixed profile; `abandoned` facts are skipped); then model calls
  for the missing, stale-input and former-directive facts, newest first, at most `FACT_PROFILER_MAX_PER_RUN` per run
  (`src/composition/fact_profile_setup.py`), each started only while the lease leaves time; one embedding batch of the
  label words; one compare-and-set write per fact. Facts of the SYSTEM account are out.
- **Confirmed people:** the owner-confirmed people list is read once per run before classifying; every call carries it as a
  block, and a fact's fingerprint holds only the entries it names, so a list edit re-profiles only the facts naming that
  person. An unreadable list ends the step with `error=True`.
- **Owner's answers to profile questions** are applied by code in the derivation (place before the atlas, person, date); an
  answer re-derives exactly the facts whose stored answer names it, without a model call.
- **Profile questions:** an open point is asked through the review's own channel as a `FactReviewQuestion` of kind `place`,
  `person` or `date` (§5.6), after lifecycle questions, at most one per owner-local day, until `PROFILE_OPEN_CAP` are
  open. The daily review run creates them after its profile step (`FactProfileService.run(..., collect_points=True)`) and
  closes one whose point is gone. A digit or a button picks an option; a reply in words is read by code only
  (`src/domain/profile_reply_words.py`), never by a model. The store's list reads return lifecycle questions unless a
  reader names `scope="profile"` or `"all"`.
- **Failures:** an `invalid` or `refused` reply writes only a failure count; the third failure on one fingerprint abandons
  the fact until its content changes. An `unavailable` reply is an outage: no failure is written, the rest waits.
- **After any written profile** the step refreshes the baseline once.
- **People cards:** at the end of every profile step the account's people cards are rebuilt by code with no model call
  (`src/domain/person_cards.py::build_person_cards`, stored by `PersonCardRepository.replace_cards`): mentions with the
  same transliteration skeleton are one spelling; a group becomes a card at `PERSON_CARD_MIN_FACTS` distinct facts. They
  only feed the weekly digest's last line; never a fact, never in a prompt, never read by the consolidator.

### Step 10 — the next conversation

Smart reads the cache document on every request, but `PromptBuilder` keeps a component copy that the consolidator
invalidates (`invalidate_biographical_cache`), so the new baseline applies from the next message
after the run. A new fact is searchable from the moment it is written. Whether it is in the baseline depends on §4.1.

### 3.11 Loop alerts and stats lines

`src/domain/consolidation_pass_stats.py::ConsolidationPassStats` counts each pass's tool calls; the agent turns each call
into a typed `PassCall` and renders the lines (no arguments, no fact text):
- 🚨 permanent: a pass that uses all its turns without a final report logs an ERROR and posts to the ops sink; the pass
  returns no operations, the writes it made stay.
- 📊 temporary: while `CONSOLIDATION_STATS_ALERTS` is on (`os.getenv`, wired in `src/composition/user_agent_factory.py`),
  every pass that ends with a report posts one line: pass label (`stage1`, `stage2a`, `stage2b`, `email`, `cluster`,
  `review`), model, effort, turns, tool counts, writes, tool-error turns, seconds.
- **Abandoned refusals, 🚨 permanent, also with the stats line off.** A refused create or merge is abandoned when no later
  successful create or merge in the same pass answers it (same text, or on a cleanup pass a born-due refusal answered by
  the rewrite it asks for: a later merge of the same set, or a create with the same `directive` decision (and, when made from facts, the same
  source set) that keeps more than half of the content words); a refused update, when no later successful non-closing update targets the same fact. It errs towards an
  alert. On Stage 1 and mail a born-due refusal is never abandoned (the text lets the model discard).
- **The review pass** (§5.6) gives each fact one result (`PassActor.one_result_per_fact`): any successful update or ask of
  the fact anywhere in the pass answers it; a basis or ask refusal leaves the fact kept.
- **Close-out.** The abandoned 🚨 and the add of the pass's refused calls to the account's day (`expiry_tool_errors`; the
  review's refusals go to `review_refusals`) run at the end of every pass, also one that raises, in a `finally` capped at
  `ConsolidationAgent.CLOSE_OUT_TIMEOUT_S`: best effort, it never raises and never changes the pass.

Why: `docs/04_solution_strategy/decisions/sonnet55_adaptive_thinking_stall.md`.

---

## 4. Reading memory

### 4.1 The baseline: what the bot sees in every message

**Who gets it.** `PromptBuilder.build_for_agent` loads the cache unless the caller passes `include_biographical=False`
(`src/services/prompt_builder.py`); grep that argument for the list of agents that skip it (the Router, the query-slots and
profiler agents, web, maps, compute, document, image and video agents, mail classification and card agents, the tutor, the
drive agents, Lelik's summarizer). Smart and Quick read the cache document directly and merge the Router's prefetch. Lelik
gets a filtered copy (§4.6).

**How it is chosen** (`src/services/biographical_context_service.py::BiographicalContextService.refresh_context`):
1. Limits: `DEFAULT_BIOGRAPHICAL_CACHE_LIMIT` and `DEFAULT_PRINCIPLES_CACHE_LIMIT`
   (`src/domain/settings.py::SearchConfig`), resolved user → account defaults → system by `ConfigurationService`; an
   explicit limit wins. The consolidator passes its own, resolved per user when its agents are built
   (`src/composition/user_agent_factory.py`); the Cabinet invalidate passes none, so a user-level override is not applied
   to that rebuild.
2. The **identity block**: current, non-archival facts whose profile says `user_identity == true`, ordered by priority rank
   then newest first (`get_active_facts_ordered(identity_only=True)`; needs the composite index in
   `config/firestore.indexes.json`). A fact with no profile is not in the block.
3. If fewer than the limit, fill from the top facts of all domains in the same order, minus those already taken and minus
   directives.
4. Facts tagged `mindset` become **principles** (up to the principles limit; extras are dropped, not moved to facts), carved
   out of the same limit.
5. Directives are fetched separately: up to `DEFAULT_DIRECTIVES_CACHE_LIMIT` current non-archival facts with
   `directive == true`.
6. Each cached item carries id, text, `directive`, `primary_domain`, `sensitivity`, tags, priority, `created_at`; the two
   profile fields are None while the fact is unprofiled.

**How it is rendered.** `PromptBuilder.build_for_agent` splits the list: directives → `standing_directives {}` (§4.2);
items tagged `semantic_lens` (the Router's prefetch) → `query_specific_context` after the cache boundary; the rest →
`knowledge_base { biographical_context }`, one section per `primary_domain` headed by its registry label
(`src/domain/fact_domains.py`), the Biography section first (oldest to newest), the other registry domains after it (newest first; the identity flag only
selects the identity block, it is not a section), an item with no `primary_domain` in the Lifestyle section, a
fact tagged `mindset` prefixed `[MINDSET]`, each line with its recorded date, under a note that conversation history wins
on conflict (`src/services/prompt_v3/biographical_formatter.py`,
`src/services/prompt_v3/prompt_assembly_service.py`). Facts and directives pass the security validator as untrusted text.

**When it changes:** at the end of a consolidation run (§3 step 9), right after a Cabinet invalidate (§5.2), after a
janitor run that staled a fact of the account (a stale fact leaves the baseline, the prefetch and `search_memory`
together, §5.5), and after a review run or an owner's answer that wrote a fact (§5.6). No reader of the baseline shows an
expiry field.

**When the read fails.** A missing or empty cache document reads as no facts. A backend failure is raised: Smart, Quick,
the Router and `PromptBuilder` catch it and answer without the baseline; Lelik fails the call closed (§4.6); the
consolidator runs without it (§3 step 4).

### 4.2 Standing directives

Directives (facts with `directive: true`) are the owner's standing orders to the agent, rendered verbatim as the last
static block, `standing_directives {}`, newest first. Every agent that gets the baseline gets them unless it passes
`include_directives=False` (the consolidator, Lelik, and others). The rulebook is curated to 15 by Stage 2b and its
backstop; the cache holds at most 15.

**Why a directive disappeared**, in order of likelihood: Stage 2b demoted it (a new fact with `directive: false` exists, the
directive is `invalidated`); Stage 2b invalidated it (reminder duplicate, unusable, superseded); Stage 2b merged it (a new
fact; the sources are `superseded`); the backstop cut it (over 15, lowest priority, then oldest); its priority is
`archival`; Stage 1 archived or invalidated it after the owner said to stop; the owner invalidated it in the Cabinet. The
📊 `stage2b` line of that run shows the writes; BigQuery shows the checklist (§9.4).

**What makes a fact a directive** is decided in one place, `is_directive` (§2.2): the consolidator's `directive` flag and
nothing else. The prompt builder's split, the baseline fill, the profile step and the review rules call it; the reads that
select directives (the baseline, Stage 2b and its backstop, the companion) filter on the same flag. A fact written before the flag existed is a directive only once the owner-run
mark script has set it (`scripts/migration/fact_directive_flag_mark.py`). The expiry pin (`expiry_pinned`) is wider: a
directive, a principle or a `mindset` fact; principles are pinned but are not directives.

### 4.3 The search engines

Readers use the scored fact search (`src/services/fact_search_service.py::FactSearchService.search_phrases`,
`docs/10_rfcs/FACT_PROFILE_RFC.md` §5.3): per-phrase candidate lists, a linear score, fusion over up to four phrases, a
cut. How it ranks: it embeds the queries' subject, kinds and message in one batch and reads the account's cached snapshot
(`src/domain/fact_search_snapshot.py`, valid while the baseline document keeps its `refreshed_at`); candidate lists come
from the kNN and from set membership over profile keys (labels, domain, time, kinds, places, people, home); one linear
score with fitted weights (`src/domain/fact_search_rank.py`) orders them; a near-duplicate pass and a cut follow; several
phrases are fused by `fuse_phrases` with priority weights and a bonus for a fact several phrases found
(`src/domain/fact_search_lists.py`). The Router, `search_memory`, Lelik and the connector all reach it (§4.4, §4.5, §4.7), and so does the companion's
biographical slice (`FactSearchPort.search` with the channel's `session_domains`). The rest of this section describes
`enrich_context`, the older multi-vector engine, which only the consolidator's `search_existing_facts` still calls.

`SearchEnrichmentService.enrich_context` (`src/services/search_enrichment_service.py::SearchEnrichmentService`):
1. Embeds the non-empty inputs in one batch with `RETRIEVAL_QUERY`.
2. Runs up to 6 vector queries in parallel, each restricted to `account_id` and `state == "current"`: keywords against
   `tags_vector` and `metadata_vector`, phrase 1 against `vector` and `tags_vector`, phrase 2 against `vector` and
   `metadata_vector`. Limits come from `SearchLimits`; the second channel of each input takes 0.75 of the first, except
   phrase 2 where the text channel is the smaller. The consolidator passes keyword 7, phrase 10 / 10 and its own total.
   A failed channel counts as empty.
3. **RRF**: score = Σ 1/(60 + rank) over the channels a fact appears in (`src/domain/rrf.py`). A fact found by several
   channels beats a fact ranked first by one.
4. **Semantic dedup** (step skipped by the consolidator, `skip_semantic_dedup=True`, so it can merge): a fact is dropped as
   a duplicate of one already kept when cosine and numbers agree (`src/domain/deduplication_service.py`). Different
   numbers are never duplicates. No production caller reaches this branch
   (`docs/04_solution_strategy/decisions/smart_deduplication_keep_duplicate_over_lost_detail.md`).
5. Cut to the total limit; drop facts whose id is in the `biographical_facts` passed in (none from the consolidator).

Firestore returns no distance in this projection, so `relevance_score` is `None` for every hit; ranking uses ranks. A
process-wide semaphore caps parallel `find_nearest` calls (`firestore_repo.py`). The result also carries `expiry_kind`,
`ttl_days` and `expires_at`; only the consolidator's search dict renders them (§2.7). The concept:
[multi_vector_rrf_search.md](../../08_concepts/multi_vector_rrf_search.md).

### 4.4 The Router's prefetch (every message)

The prefetch starts in parallel with triage: the slots agent (`src/agents/fact_query_slots_agent.py`) reads the turn's
messages into a `FactQuery` whose `message` is the user's latest message, and the scored fact search runs on it
(`src/agents/core/router_agent.py::RouterAgent._start_prefetch`). Triage returns `search_intent`; when it is `none` the
prefetch is cancelled. The result is up to `prefetch_limit` facts minus those already in the baseline. Smart and Quick
receive them as `query_specific_context`, where the facts still carry the tag `semantic_lens` that splits them from the
baseline (`src/services/prompt_builder.py`). This is the one search per message every orchestrator turn shares
(`docs/04_solution_strategy/decisions/router_prefetch_once_per_message.md`); Smart's own `search_memory` calls are extra.

### 4.5 `search_memory`

Smart delegates `search_memory` to `FactsMemoryAgent` (`src/agents/memory_search_agent.py::FactsMemoryAgent`). The request
is `context.queries`: one to four English phrases in priority order, written by the calling model. The agent has no model
call of its own:
1. A call without phrases is accepted and answered with a tool note naming `context.queries`.
2. Each phrase goes through the slots agent (a failure costs that phrase its slots, not the search), then
   `FactSearchService.search_phrases` scores and fuses them. `FactQuery.message` is the user's latest message when the turn
   has one, else the phrase. No baseline exclusion.
3. The result is formatted by `src/domain/memory_search_format.py` (one formatter for the connector and this agent): an
   explanation line and one header line per phrase saying how many facts it found, then per fact a head line (a `[qN]` tag
   naming the phrases that found it, its domain label, when it applies, when it was recorded), its text, and `context:` and
   `metadata:` lines; no match gives `No records matched.`

### 4.6 Lelik

`LelikPersonaService.assemble` (`src/services/lelik_persona_service.py`) takes the baseline cache and keeps only the facts
whose `primary_domain` is in the account's voice list **and** whose `sensitivity` is `ordinary`; a fact missing either
field never reaches the call. The list is `UserBotConfig.voice_fact_domains` through `voice_domain_keys()` (default: the
registry's `voice_open` domains). No standing directives. Anything else he reaches through `search_memory` or by asking
Alek. A failed cache read raises, and the call is not opened on an empty context.

### 4.7 The claude.ai connector (MCP)

`get_user_context(queries)` (`src/composition/mcp_setup.py`) hands the phrases to the token account's `FactsMemoryAgent`
(§4.5), the same entry point as every other caller; no model and no Router in the connector itself. The answer is the same
`format_search_result` text as `search_memory` returns (§4.5).

### 4.8 The Cabinet fact page

Read routes (`src/web/user_cabinet_app.py`): all current facts; a paginated browser, newest first, with an optional domain
filter and a state filter (`current` by default or `stale`); a single-vector search over `vector`. **Domains come from the
profile** (`FACT_PROFILE_RFC.md` §6): the filter is one registry key and lists the facts whose `domain_keys` hold it (a fact
not yet profiled is in no domain; any other value is a 400); each fact's badge is the registry label of its
`primary_domain`; the page takes the area labels from `GET /api/user/fact-domains`. The area select shows each area's
number of facts from `GET /api/user/facts/domain-counts` (`FactRepository.count_facts_by_domain`: count aggregations over
the browser's own query, so each number is what the filter lists; a fact in two areas counts in both, an unprofiled
one only in the total). The page has four views:
- **Current** (the browser).
- **Stale**: the stale facts, each with its `stale_since`.
- **Closed**: `GET /api/user/facts/recently-closed`, everything archived or invalidated in the last 30 days,
  newest closure first, each with its state and the actor of its last trail entry. Read-only: the Cabinet cannot bring a
  closed fact back; the owner tells the bot in chat that it is still true.
- **Questions**: the open memory-review questions with answer buttons (§5.6), including the group "Invoices with no
  payment confirmation" (Paid / Not paid, in bulk).

The page holds fact ids, and a version write changes the id; a stale id still works for a delete (§5.2).
**Pagination tie.** The browser pages by a cursor on `created_at`, ties broken by document id
(`FirestoreFactRepository.get_paginated_facts`). A version copies `created_at`, so a fact versioned between two page loads
can show twice or be missed once at a page boundary; reload the list.

---

## 5. Writing memory outside consolidation

### 5.1 `propose_memory_facts` and `$memory save`

The bot never writes facts. The consolidator reads every user message as the owner's own words, so a bot-written save let
procedures and file notes become facts and directives (`docs/04_solution_strategy/decisions/memory_facts_owner_confirmed.md`).

1. On interactive turns only, Smart has the local tool `propose_memory_facts` (`src/agents/core/smart_response_agent.py`).
   Up to `MAX_FACTS_PER_DRAFT` facts, each one line of at most `MAX_FACT_CHARS` plus a source
   (`src/domain/memory_fact.py`). Each text passes the security validator; a flagged fact rejects the proposal
   (`src/services/memory_fact_service.py::MemoryFactService.propose`).
2. The draft is stored under a 4-hex-character code for `DRAFT_TTL` with the session it came from
   (`src/adapters/firestore_memory_fact_draft_repository.py`). The owner gets the facts as `memory-facts.txt` and a separate
   `$memory save <code>` message.
3. Pasting the command (`ConversationHandler._handle_memory_command`) runs `MemoryFactService.confirm`: it appends a
   **user/model pair** to the origin session (the user part "[System: the owner reviewed and confirmed these facts …]" plus
   one line per fact) and only then deletes the draft. A failed append keeps the draft for a retry.
4. From there the pair is an ordinary pair of messages: it becomes facts at the channel's next overflow (§7). Until then
   `search_memory` cannot find them; Smart sees the pair only while it is among its history window.

### 5.2 Cabinet invalidate

`POST /api/user/facts/<id>/invalidate` (`src/web/user_cabinet_app.py`) → `CabinetFactService.invalidate`
(`src/services/cabinet_fact_service.py`; the rule is `src/domain/fact_lifecycle.py::cabinet_invalidation`, the walk is
`src/domain/fact_resolution.py::ForwardWalk`): the id must exist (else 404) and its stored account must be the caller's
(else 403). Then the service **follows the id forward** to the fact that is live now (`CABINET_ACCEPTS`) and sets
`state=invalidated`, `valid_to`, `last_updated` and a trail entry (actor `cabinet`) on that document, in place, under the
`update_time` precondition. A page loaded before the consolidator versioned the fact therefore deletes the **current
version**, **but only when that version still has the text the page showed**: if the consolidator rewrote the fact after the
page loaded and the new text differs, the delete is **refused** with a 409 ("rewritten after it was shown"; the page
reloads) and nothing is written, so the owner's correction is not deleted unseen (owner ruling). A newer version with the
same text (tags, metadata or class changed) is deleted at once.

Search stops returning the fact at once, and **the service then rebuilds the baseline** with `refresh_cache`, so the fact
leaves the next prompt too. That method never raises: on a failure it logs `Failed to refresh biographical cache`, the
invalidate still answers 200, and the baseline keeps the fact until the next consolidation run.

Failures: a fact **merged** into another lineage, a closed document with no successor, a chain that is too long, a
rewritten text or a second conflict is **409** (`FactResolutionError` / `FactWriteConflict`), logged as a WARNING
(`Fact <id> not invalidated: …`); the page shows "This fact changed in the meantime; the list was reloaded." and reloads. A
failed invalidate does not touch the cache. Cabinet invalidate does not take the memory-write lease: it writes one
document under a precondition. After that message, look in the reloaded list for the merged fact; deleting it deletes the
merged text as a whole.

### 5.3 The directive cap backstop

§3 step 7. It writes through the same `update_fact`, on the run's tracker (state only: in place, same id, no version, no
re-embed), actor `backstop`, trail reason `directive cap`. A refused or failed update is logged at ERROR and posted as one
🚨 per backstop run.

### 5.4 `$consolidate`

`ConversationHandler.handle_command` (command `consolidate`) takes the oldest `batch_size` messages of the current session
(the user's `consolidation_batch_size`, else the global one), posts a "started" note, enqueues them (id
`batch_<handoff_key>`, as the overflow does), cuts exactly those messages with `remove_head`, then calls
`ConsolidationService.consolidate_now` (handed over by the adapter factories as the overflow callback). It processes **at
most one batch** inside the request, because the chat request is a Cloud Task with an 1800 s deadline and two batches at
the lease bound would exceed it. That is the **oldest** pending batch of the user, which after a backlog is not
necessarily this command's. If more remain, `consolidate_now` enqueues the next consolidation task. It uses its own copy of
the serializer (same rules as §3 step 3).

The reply is honest about what happened (`ConversationHandler._consolidate_outcome`): it never says "N messages were
processed"; it says the command's messages were queued and then one of: batches consolidated now, no batch consolidated
in this request, a batch failed and stays queued, or queued behind another memory run (the lease was taken, §3 step 3),
plus "the rest continues in the background" when more wait. With no task queue (a local run) it says nothing runs in the
background and the owner should send `$consolidate` again. If `consolidate_now` raises, the owner gets a "stopped with an
error" message and `[Librarian] $consolidate failed` is logged; the batch is already queued and the hourly sweep finds it.
The line reaches the chat through `notify()` and is saved in the session history (§9.3).

### 5.5 The janitor (`fact_lifecycle_sweep`)

A zero-LLM daily job that enforces the expiry decisions of §2.7 and asks for a review when one is due. Cloud Scheduler job
`alek-bot-dev-fact-lifecycle-sweep` (`docs/07_deployment/SCHEDULERS.md`), `POST /worker {"task_type":
"fact_lifecycle_sweep"}`, not retried by design (a retry after the deadline would run beside a run still going).
Code: `src/services/fact_lifecycle_sweep_service.py::FactLifecycleSweepService.sweep`, the plan in
`src/domain/fact_lifecycle_sweep.py`, the alerts in `src/domain/fact_lifecycle_alerts.py`, the review part of the line in
`src/domain/fact_lifecycle_review_line.py`.

**One run.**
0. *Run guard.* The run takes the guard (`fact_lifecycle_run_collection`, held for `RUN_GUARD_SECONDS`, taken in a
   transaction) and frees it in a `finally`. A run that finds it held does nothing and answers 200 `{"status": "busy"}`; a
   guard never freed expires by itself.
1. *Due:* `FirestoreFactRepository.find_due_facts` returns every document with `state == "current"` and `expires_at <= now`,
   collection-wide, projected to account, `expires_at`, `expiry_kind` and the trail (no text, no vectors), with its
   snapshot `update_time`. It needs the composite index `(state, expires_at)`. A due document **with no `expiry_kind`** is
   skipped and counted `no_decision`, never staled; a document without `account_id` is skipped and logged.
2. *Backlog and reads:* `stale_backlog` counts the `stale` documents per account and the oldest `stale_since`; the review
   store gives every open question and every account's review state.
3. *Accounts visited:* every account with a due fact, a stale backlog, a review state, an open question, or a review
   figure or `reply_intake_failed` in yesterday's counters, so that its alerts and review are judged
   (`accounts_to_visit`).
4. *Per account: skip, pool, breaker.* The facts of the account's open questions are taken out of the due facts: not
   staled, not counted as due, shown as `due_waiting`. The **pool** is the stale facts plus the facts of open questions.
   `breaker_reason` trips when the account has stale facts and no successful review within the review interval +
   `BREAKER_REVIEW_AGE`, or when the pool is over `BREAKER_BACKLOG`. A tripped breaker stales nothing for that account and
   posts one 🚨 per account per run, only when a due fact with a decision was actually held back.
5. *Per-run cap (owner ruling).* An account that is not tripped gets at most `stale_headroom(pool)` stales per run, **oldest
   expiry first**; the rest is counted `deferred`. So stale facts plus waiting questions never pass the pool cap.
6. *Stale:* `stale_fields` builds the write from the trail the snapshot projected and
   `FirestoreFactRepository.stale_due_fact` applies it in one transaction under the snapshot's `update_time`: `state=stale`,
   `is_current` true, `valid_to` none, `stale_since`, `last_updated`, and the trail entry `{current → stale, actor janitor,
   reason "expired <ISO>"}`. A changed or deleted document is a conflict, skipped until the next run; any other error stops
   the run. It never archives, deletes or reads text, and takes no lease.
7. *Review enqueue* (log-only mode too). `review_due` (`src/domain/fact_review_rules.py`) names the first reason that
   holds: `stale` (stale facts after this run and the interval elapsed), `reply` (a reply in words nobody has read),
   `timeout` (a question past its timeout), `pending` (a send that never finished), `digest` (due), `queue` (a queued
   question that may go out). Then one `fact_review` task `{user_id: the account's first owner, account_id, mode:
   "daily"}` is enqueued under a name made of the account and the owner's local date, and `enqueued_at` is set. The interval
   is `fact_review_interval_days` (the user's, else the account default, else `DEFAULT_FACT_REVIEW_INTERVAL_DAYS`), so a
   user's value needs no deploy. Cloud Tasks keeps a task name only while the task is queued or running and for about an
   hour after, so "one review a local day" comes from the interval rule, not from the name.
8. *Alerts:* one 🚨 per account per run listing every condition that holds (pool over the cap, oldest stale fact older than
   interval + `OLDEST_STALE_GRACE`, more than `QUEUE_ALERT` queued questions, no successful review for
   `UNSUCCESSFUL_REVIEW_ALERT`, yesterday's `reply_intake_failed` above zero, yesterday's refused expiry calls above
   `EXPIRY_TOOL_ERRORS_ALERT`, the breaker line). The marks the repeat rules need are written before the post, so a lost
   post is not repeated. Once per run, one more 🚨 when closed review records are stored more than
   `PURGE_OVERDUE_AFTER` past their `purge_after` (the TTL policy is late or missing).
9. *Baseline:* each account that staled anything gets a cache refresh, so the fact leaves every prompt (§4.1).
10. *Snapshot and line:* one snapshot per account (`FirestoreFactLifecycleCounters.record_sweep`), one `[FactLifecycle]`
    log line per account, and the 🧹 line posted once.

**Log-only is the code default; the deploy sets the variable.** `FACT_LIFECYCLE_SWEEP_DRY_RUN` (`os.getenv` in
`src/composition/memory_setup.py::build_memory`, not in `load_settings()`) makes the janitor log-only unless it is an
explicit off value (`src/domain/fact_lifecycle_sweep.py::sweep_dry_run`: unset, empty or anything else stays log-only).
`cloudbuild-dev.yaml` sets it to `0`, so the live janitor writes; the rollback lever is removing the variable (a deploy;
`--set-env-vars` replaces every variable). In log-only mode nothing is written to facts or the cache: `would_stale` counts
every due fact with a decision, `deferred` is what the cap would hold, the breaker reads `would trip`, no breaker 🚨 is
posted, and the snapshot and the line are written anyway. **The review is not log-only**: it works on the facts that are
already stale, whatever the janitor's mode.

**What it stores and posts.** `fact_lifecycle_daily_collection/<account>:<UTC day>` gets one snapshot (`due`, `newly_due`,
`no_decision`, `staled`, `would_stale`, `deferred`, `stale_conflicts`, `breaker`, `backlog`, `oldest_stale_days`,
`review_enqueued`; the last run of the day wins) and carries counters that other code adds to with increments and a
snapshot never replaces: the consolidator's `expiry_tool_errors` (§3.11), and the review's and the question flow's figures
(`src/domain/fact_lifecycle_day.py`, `src/domain/fact_review_question.py::ANSWER_COUNTERS`). Once a day (the day's mark in
`fact_lifecycle_lines_collection` is claimed before the post, so a failed post loses the line and never doubles it) a
single ops message goes to the ops sink when some figure is non-zero or a breaker is not ok: `🧹 Fact lifecycle — run …`,
then one line per account with the janitor figures and, only where a figure is above zero, the review segments
(yesterday's counters; the queue, unanswered and `due_waiting` figures are this run's). The buckets of the review segment
are counters of their own, not a breakdown of `reviewed`. The queue figures and the "queued" alert count chat questions
only: a digest question is never sent and is never in the queue or the pool.

**Failure and overlap.** Every ops post is capped at 10 s. A failure other than a write conflict is logged with its type
and the stage reached, posts one "🚨 Fact lifecycle run failed" naming no fact, re-raises, and the job answers 500; the next
day's run catches up. The 🧹 line is claimed per **UTC day**: a hand run earlier on the same UTC day takes the day's line,
and the scheduled run then posts none (its numbers are in the snapshot).

### 5.6 The review pass, the question queue and the owner's answers

Decision record: `docs/04_solution_strategy/decisions/fact_review_and_owner_questions.md`; spec
`docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D6, D7. The owner's rule through all of it: **when unsure, keep the fact, and ask.**
The second: **anything that really happened is a fact, and so is an intention**, so a fact whose date passed becomes
history, never an archive.

**What a stale fact becomes.** The janitor hides a fact whose expiry passed; the review decides what it is now. One
`fact_review` run per account per day (when something needs it, §5.5 step 7)
(`src/services/fact_review_service.py::FactReviewService.run`, `_daily`) does, in order, under the account's memory-write
lease (the one consolidation holds, so the two never write at once; busy → the run defers itself with a named task):
1. *Owner check.* The user must be the account's first owner and the profile must belong to the account; else a WARNING
   and nothing done. Questions, sessions, language and timezone are the owner's; facts are the account's.
2. *Housekeeping* (`_housekeeping`). A question whose send never finished is completed (it has a message reference or a
   recorded post) or taken back (a "not delivered" pair is appended and the question goes to the front of the queue). An
   asked question sent on an earlier local day becomes `unanswered`: still open, still timing out, no longer current.
3. *Timeouts, in code* (`_timeouts`). A question sent `QUESTION_TIMEOUT` ago is closed by silence. If its fact is still the
   same document: when the owner answered in words the review has not read, or a fact created since looks like an answer
   (one of the nearest current facts), the fact goes to the model once; otherwise the question's **fallback** applies at
   once. A fact closed or rewritten meanwhile closes the question `resolved_elsewhere`.
4. *Kept twice* (`_kept_twice`). A stale fact the model left undecided `MAX_OFFERS` times gets a code question and is
   queued; code never revives a fact on its own. For a `date` fact that question is `happened` with fallback `history`; any
   other fact keeps `still_true` with `revive`.
5. *Selection* (`select_batch`). Up to `REVIEW_BATCH` facts: first those with a reply in words not yet read and the
   timed-out candidates, then the stale facts, offered least first and the longest stale first. A fact with an open question
   is left out.
6. *The model pass* (`ConsolidationAgent._handle_review_stale_facts`, task `review_stale_facts`, label `review`; nothing
   selected means no model call). Profile `consolidation_review` (tokens `CONSOLIDATION_TAXONOMY` and
   `CONSOLIDATION_LIFECYCLE`), no conversation (the baseline is in the prompt like on every pass). It sees, per fact, built by code: the text, domain, expiry,
   `reported`, `stale_since`, earlier versions, its nearest current neighbours and, when there is one, its question with
   the owner's reply. Every listed fact must end with exactly one outcome, decided by the first rule that applies:
   **supersede** (a current fact says the same), **revive** (a reply or other evidence says it still holds), **history**
   (its date passed: rewritten as what happened, what was planned and did not happen, or planned with an unknown outcome),
   **archive** (the owner's reply says it is over), **invalidate** (it was never true), else **ask**. A fact the pass cannot
   decide and has already asked about is *left*.
7. *The tools* are three (`ConsolidationAgent._get_tool_declarations("review")`): `update_fact` (with `basis`, `history`,
   `as_history`, `payment`), `ask_owner` and `count_words`; no search, create or merge.
   `src/infrastructure/fact_review_scope.py::FactReviewScope` (one `asyncio.Lock`, so calls apply in the order written)
   routes them and refuses what `src/domain/fact_review_rules.py` refuses, each refusal naming the argument and its fix and
   tagged `basis`, `scope` or `ask`; a refused call writes nothing:
   - **scope**: a fact outside the batch, or one that already has a result in this pass;
   - **basis** (`basis_refusal`): an invalidate or a rewrite needs evidence (another current fact, never a version of the
     same fact) or the owner's reply with a quote copied from it; **an archive needs the owner's reply**; **a revive needs
     evidence or a reply** ("ask the owner instead"); a **history rewrite** takes evidence, a reply or `date_passed` (the
     fact's own text reporting the outcome, never "it took place" or "it was paid"), and `date_passed` backs nothing else;
   - **history** (the "ended" shape): an archive on a reply that says a standing thing ended may carry one past-tense
     sentence, which code creates as a separate fact in the same transaction; refused on a `date` fact and on noise;
   - **payment**: with a history rewrite of a routine invoice that was received and is not confirmed paid, the two texts
     `paid` and `not_paid` that code keeps for the owner's later tick. An invoice is never a payment; a fine or a debt is
     asked;
   - **ask** (`ask_refusal`): one fact, no question text (code writes the question), a question kind, the `texts` for each
     answer (one line, no `[`, at most 40 words, not the fact's stored text) and a `fallback`: `revive` (`still_true` only),
     `history` (the question's "unknown" text written as history; the fallback of `happened` and `renewed`, the only one of
     `paid`) or `archive` (only with `noise: true`). `folds` names other batch facts that are parts of the same plan: one
     question for all. Noise never archives anything on its own; it only sets what silence does.
8. *After the pass* (`_after_pass`). Outcomes remove the fact's offer and close its question `review`. Each ask makes the
   fact `current` again under its precondition (`stale_since` cleared, the expiry as stored) and creates a `queued`
   question that takes its place in line from the fact's `stale_since`; the folds are superseded by the asked fact only after
   the ask is queued. Each routine invoice's history version gets a `paid` **digest question** (never sent in chat,
   answered in the Cabinet, closed `resolved_elsewhere` when its fact changes or after `DIGEST_QUESTION_DAYS`). A
   conflicting fact write leaves the fact stale and creates no question.
9. *Send* the next question if one may go; *refresh* the baseline once if any fact was written; *digest* if due; add the
   counters; post the revive alert (a fact revived `REVIVE_ALERT` times within `REVIVE_WINDOW`); mark the run. **A failed
   review pass** still sends the next question and the digest; its earlier writes stand; the run answers 200 `failed`, with
   no Cloud Tasks retry, and the janitor enqueues again the next day.

**The fact is current while it waits.** A queued fact is an ordinary current fact for every reader and keeps its past
`expires_at`; the janitor skips it while its question is open. A question never hides a fact, and an unanswered one never
silently revives it. The cost is that the pool (stale + waiting facts) is bounded (§5.5).

**One question at a time** (`may_send`, `next_in_queue`, `FactReviewService._send_next`). The queue holds `queued`
questions ordered by how long their facts have been stale. There is one current question per user
(`current_question_id` in the state). The next goes out only when the current one is closed or, if the owner does not
answer, on the next local day (the old one becomes `unanswered` and keeps its timeout). At most `MAX_QUESTIONS_PER_DAY` go
per owner-local day and none on a day a delivery failed. There are no quiet hours in code. Questions go **only to the
owner's primary channel**; with no primary channel, or one bound to a companion, nothing is sent and the queue waits (the
Cabinet can answer it). A send: claim the first queued question in one transaction on the state; append the pair to the
session (user part `[System: memory review q=<id> — …]`, model part the question, `text == full_text`, raising when not
stored); post; store the message reference; mark `asked`. A pair that cannot be appended puts the question back at the
front; a post that fails appends a "not delivered" pair and puts it back, and that day sends no more. The message is
code-built: the question line of its kind, the fact quoted as the owner is shown it, the options as digits, then a hint that
a reply in words works too. The kinds and their options are `src/domain/fact_review_options.py::OPTIONS`:

| Kind | For | Options (digit order) |
|---|---|---|
| `still_true` | everything not covered below | still true, no longer, never, don't remember |
| `happened` | a plan or event whose date passed | it happened, it did not, don't remember |
| `renewed` | a validity end that changes the current fact (a document, a permit, a membership, a trial) | renewed, not renewed, no longer needed, don't remember |
| `paid` (chat) | a fine or a debt whose due day passed | paid, not paid, don't remember |
| `paid` (digest) | a routine invoice already written "payment not confirmed" | never sent in chat; the Cabinet's Paid / Not paid |

**Language and quotes.** The owner's response-language setting decides the message language (a fixed language, or in mirror
mode the language of his recent messages, which the review pass reports as `message_language`)
(`src/services/fact_question_texts.py::FactQuestionTexts`, `src/domain/fact_review_language.py`). Question texts come from
the UI strings for the languages that have them; for any other language the review model returns them and they are kept
only if the code's checks pass, else the UI language is used. Stored facts are English: the review pass also returns
`translations` of the facts it lists and of every text it wrote, and the code keeps one only if it is one line, has no `[`,
link or control character, is short, and keeps the source's numbers exactly
(`src/domain/fact_review_quote_translation.py`); a kept one is stored with `purge_after` in the review-messages collection
under the account, the language and a digest of the English text. It is presentation only: the fact, its id and the
question's `quoted_text` stay as stored. The pair appended to the history carries the shown text, so the consolidator reads
a translated quote (its echo-trap exception says so). The Cabinet keeps showing the English text.

**How the owner answers.** Only a *reply to the question message* counts (a Slack thread reply, a Telegram reply), or a
click, or the Cabinet. Any other message is ordinary conversation and the question stays open. Four ways; a digit, a button and a Cabinet click share
one path (`FactAnswerService.apply`, source `digit`, `button` or `cabinet`), while words are only recorded
(`record_reply`) and applied by the next review pass:
- **A digit as a reply.** Taken at intake in `ConversationHandler` (`answer_reply`), before any model call, in an unbound
  channel, for a text-only message. Applied at once by code; the acknowledgement is posted as a real reply to the question
  message, and the pair `[Reply to memory review q=<id>] <text>` plus the acknowledgement is appended to the session.
- **A button (Slack).** One button per option. `POST /slack/interactivity` (`src/adapters/slack/interactivity_route.py`)
  verifies the Slack signature on the raw bytes before it parses anything, answers 200 at once and queues an
  `interactive_choice` task; the worker applies it through the same path (`InteractiveChoiceService`). The click must come
  from the question's own user, on the question's own message.
- **A reply in words.** Recorded on the question (capped by `MAX_REPLY_CHARS`) and acknowledged. When it answers the
  *current* question, the same transaction gives up the slot and the next question is released at once; the question stays
  `asked`. The review is shown the words once on its next run, which applies them and closes the question. It never
  shortens the timeout; if the review leaves it undecided, the timeout sees the reply.
- **The Cabinet.** The "Questions" list shows each waiting or asked fact with one button per deciding option of its kind
  ("don't remember" is never offered) and a bulk "mark selected as …" for one kind at a time; the invoice digest questions
  are their own group. An answer that is not one of its question's options is `not_applied` before any write. A fact rewritten since its question
  shows the quoted text the owner was asked about, and only the session user's own questions are listed or answerable
  (`FactAnswerService.review_queue`).

Two kinds of message that are not taken as an answer fall through to ordinary conversation, so nothing the owner wrote is
dropped: a reply the system could not take (an error in the path, logged, counted as `reply_intake_failed` and alerted the
next morning, or a reply the store would not record) and a digit whose answer the write rules refused. A digit for a fact
that **changed since the question was asked**, or whose write conflicted, is not applied and not sent on: the owner is told
the fact changed and the question stays open.

**What an answer does** (`src/domain/fact_review_answers.py`; all against the fact as the owner saw it: a fact rewritten
since, or a current text other than the quoted one, is `changed` and nothing is written):
- *Still true*: the fact is `current` with a confirmed expiry (§2.7); `reported_date` becomes the later of the stored date
  and the answer time.
- *No longer*: a **standing** fact is archived and a separate lasting fact "No longer true (the owner said so in <Mon
  YYYY>): <the old text>" is created beside it in one transaction; **noise** is only archived. *Never*: the fact is
  invalidated.
- *It happened*, *It did not*, *No longer needed*, *Paid*, *Not paid*: the question's text for that answer is written as
  history. *Renewed*: the fact is rewritten to the `renewed` text, current, with a confirmed expiry. *Not renewed*: rewritten
  to the `not_renewed` text, `expiry none`. A kept-twice question has no stored texts: code builds them at the answer's
  moment, keeping the stored text whole.
- *Don't remember*: the question's fallback now (for `history`, its "unknown" text as history, under the owner).
- A correction in words is handled by the review (invalidate, or supersede by the fact that states the true value) and by
  Stage 1, which records the true value as its own fact.
Every close goes through one method (`FactAnswerService.close_question`) that clears the current slot, releases the next
question and re-renders a Slack-posted message. Answers take no memory-write lease: the preconditions keep a concurrent
consolidation or review write from being lost and the loser reports `conflict`.

**What silence does** (`FactReviewService._fallbacks`). A question's `fallback` decides, `QUESTION_TIMEOUT` after it was
sent. `revive` (the default for `still_true`): the fact is brought back with twice the window its last revive gave (§2.7).
`archive` (noise): the fact is archived. `history` (`happened`, `renewed`, `paid`): the question's "unknown" text is written
as history through the typed write path under the `timeout` actor; the fact now has `none`, so it is never asked again, and
the weekly digest lists it. A write conflict leaves the question for the next run; for the `history` fallback a refused or raising write is a WARNING
and `fallbacks_not_applied`, the question stays open with one more attempt, and it closes `resolved_elsewhere` on a typed
refusal at an attempt after the first or at the `MAX_FALLBACK_ATTEMPTS`-th failure (`history_fallback_gives_up`); one bad
question never stops the run. If the history landed and the close did not (a crash between), the next run finds the
question's own timeout reason on the version its fact resolves to and closes it `timeout`, so the digest still lists it
(`FactReviewService._silence_already_written`). Every typed write (the review's, an
answer's, the timeout's) names the exact document it decided on (`expected_id`): if the fact moved on meanwhile the write
is refused, so an answer racing a timeout never overwrites the other's history.

**The weekly digest** (`FactReviewService._digest`). When the review or an answer sets a fact aside (archive or invalidate),
the state records `undigested_since`. At the end of a review run (after a failed pass too), if something is undigested and
the last digest is at least `DIGEST_INTERVAL` old, the run claims the owner's ISO week and sends one message by
`notify_raw`, as literal text (stored fact text is shown, never obeyed, and nothing is written to history): one line per
fact as the owner is shown it with its state in words, and the footer "To bring one back, tell me in chat that it is still
true." It lists only removals made by the review, a timeout or the owner (the last trail entry's actor), at most 50. Two
more blocks appear only when they have lines, and a digest with only them is sent too: facts a silence wrote as history,
and open invoice digest questions with the link to the Cabinet. The last line is the people-cards line (§3 step 9b). If
there is nothing to list, no message is sent but the week is still claimed; a digest that could not be delivered releases
its claim and tries again at the next run.

**Retention.** `purge_after` is the close time (or the claim time of a digest) plus `QUESTION_RETENTION`, and a quote
translation's write time plus its own retention (`src/domain/fact_review_quote_translation.py`); it exists only in storage. A Firestore TTL policy on it deletes them; without one they stay and the janitor's run-level 🚨 reports them. An
open question never has one; the state document has none.

---

## 6. Outcomes: tool calls and failures

| Where | What happens | Retried? |
|---|---|---|
| `search_existing_facts` fails (embedding, Firestore) | the adapter catches everything and returns `[]`, which the model reads as "no match"; a duplicate may be created | no |
| a tool raises past the adapter | `{"error": …}` returned to the model; rare, since create, update and merge catch their own errors | the model may retry |
| `create_fact` missing a required attribute | `status: failed`, the message names the fields | the model may retry |
| a bad value (an enum, a `directive` that is not a boolean, a malformed date, `tags: null` or `[]`, empty `content`, `superseded` on a create or merge, `stale` on a create, merge or update, `superseded_by` missing or without `superseded`) | checked **before** any embedding or write, `ValueError` caught → `status: failed` with the message | the model may retry |
| a missing, `null` or malformed `expiry` on create, merge or a version-making update | `status: failed`, `refused: "expiry"`, the text names where `expiry` goes and the three shapes (§2.7). A malformed merge leaves its sources current | the model resends; a refusal never answered is an abandoned refusal (🚨, §3.11) |
| an `expiry` born due | as above plus `born_due: true`; the way out depends on the pass (§2.7) | the model resends, rewrites, or (conversation passes only) discards |
| a due horizon on a cleanup pass | **not** refused: written with `expires_at` = now plus the cleanup floor and a `note` | — |
| `expiry` sent with a closing state, or a `reported_date` or `derived_from` the pass ignores | not an error: the result's `note` says so | — |
| `update_fact` on an unknown or foreign id | `status: not_found`, the same words for both | — |
| `update_fact` or `merge_facts` with an id the store has moved on from | followed forward, the call lands on the live document; the result carries the written id (and `resolved_from` for an update) | — |
| an id that cannot be followed to a `current` fact | `status: failed` ("was merged into <id>; search again", "was superseded and its successor is unknown", "is archived/invalidated/stale", too many hops, "cannot be resolved") | the model searches again |
| the document changed under the write | resolved and written once more; a second conflict is "changed concurrently; search again". A content or tags update also fails when the second read lands on a different document; a merge source that moved forward fails "source changed; search again" | only if the model searches again and redoes it **in the same pass**: otherwise the change is gone for good |
| `merge_facts` with no source supersedable, or any failure | `status: failed`; the merged fact and the closing of the sources are one transaction, so **every source stays live** | the model may retry |
| final report not JSON | parsed loosely, else `[]`; writes stand, the batch succeeds | no (the report is cosmetic) |
| a pass hits its turn cap | 🚨, the pass returns `[]`; writes so far stand, candidates not yet written are lost; **the batch succeeds and is deleted** | no |
| Stage 1/2a/2b raises (provider down after retries, prompt build fails, timeout) | `AgentResponse.failure`; writes so far stand; attempts + 1 → `retry_pending`, the worker re-enqueues at once; at `MAX_BATCH_ATTEMPTS` → `failed` + `CONSOLIDATION_FAILED`. The whole batch is retried; Stage 1 runs again over facts it already wrote, and an update it repeats can add one more version. The cache is rebuilt after a failed run that wrote | yes |
| Stage 3 raises or runs past its share | caught: one ERROR line; Stage 1–2b writes stand; **the batch succeeds and is deleted**; the mails stay unconsolidated | next run |
| batch `failed` | the per-task path never revives it; the hourly sweep does, bounded (§3 step 3); past the cap one `CONSOLIDATION_GAVE_UP` and it stays `failed` with its messages, for the owner | bounded |
| worker crashes mid-batch | the batch stays `processing`, the lease is not released, and **no cache refresh happens**, so the baseline keeps the replaced version's id and text until the retried run ends. The next tasks of that account defer until the lease expires; the winner then resets the batch to `retry_pending` with attempts + 1. A batch that kills its worker repeatedly goes `failed` instead of looping | yes, once a task arrives to defer: Cloud Tasks retries the killed request, a new overflow comes, or the hourly sweep |
| an internal error inside a batch | logged and swallowed; `$consolidate` reports "A batch failed and stays queued". **Known residual:** an error after the agent has finished (a failed `delete_batch`) leaves the batch `processing`, so it re-runs as a zombie although its facts are written | the next task or the sweep |
| baseline refresh fails | logged, swallowed; the old baseline stays until the next run | — |
| enqueue of the batch fails at overflow, or the Cloud Task enqueue fails | logged; the messages stay in the session | yes: the next append hands the same batch off again (same id, create-if-absent) |
| an overflow hand-off fails every time | past `OVERFLOW_CEILING_WINDOWS` the oldest batches are cut without a hand-off: **lost to memory** (§3 step 3) | no |
| the cut after a successful hand-off fails, or the next append runs while the hand-off is in flight | the next append hands the same batch off again, with its recorded boundaries even if the window changed meanwhile (`handoff_plan`, §3 step 3). **Accepted residual:** if a run finished and deleted the batch before the repeat, the repeat stores it again and those messages are consolidated a second time | yes |
| two runs of one account at once | prevented by the lease: the second defers itself (§3 step 3) | yes, by the deferral |
| lease acquire, open-batch check or deferral enqueue fails | propagates, the worker answers 5xx | yes, Cloud Tasks |
| lease release fails | WARNING; the lease expires on its own | — |
| user has no profile, or no account id | the oldest pending batch is `failed`, nothing leased or written | no 5xx loop; the sweep's bounded revivals fail it again at no model cost |
| provider failover | none for consolidation: an outage fails the pass and the batch is retried later (owner ruling) | same-provider retries |
| the janitor run fails (a query, the counters, a stale write) | one "🚨 Fact lifecycle run failed", the job answers 500; what was staled before is already out of the baseline | **no**: the next day's run (§5.5) |
| a janitor stale write finds the fact changed; the breaker is tripped; a second run starts | counted `stale_conflicts` and skipped; nothing staled for that account and one 🚨; the run guard answers `busy` | the next run |
| a review call is refused | the tool result says what to fix, tagged `basis`, `scope` or `ask`; nothing is written; **a revive without evidence or a reply is never written** | the model fixes the call or leaves the fact |
| the review pass fails | 200 `failed`, `last_failed_at` set, the next send and the digest still run, writes already made stand | no Cloud Tasks retry; the janitor enqueues again |
| a review run finds the lease taken | it enqueues itself again, later, under a name made of the account and the lease bucket | yes |
| an owner's answer finds the fact changed, or its write conflicts | `changed` or `conflict`: nothing written, the owner is told, the question stays open | the owner may answer again |
| an owner's answer is refused by the write rules (`not_applied`) | WARNING and `answers_not_applied` counted, nothing written, the owner is not told: a digit reply goes on as ordinary conversation, a button click returns nothing and keeps its buttons; the question stays open | no |
| a question cannot be sent | pair not stored: back to the front of the queue, nothing posted; post failed: a "not delivered" pair, back to the front, no more that day | the next day's run |
| a Slack click arrives with a bad signature, or the queue is down | 401 with nothing parsed; 503 or 500 when the task is not queued | the owner clicks again |
| the digest is not delivered | the claim is released, the next run tries again | yes |

---

## 7. Timelines

**A fact told today.**
1. *Seconds:* it is in the session. Smart sees it while it is within the history window it still holds. An overflow cuts the
   oldest batch, so a recent message can leave Smart's history before its batch is consolidated; once out of it and not yet
   in memory, nothing finds it.
2. *Consolidated:* at the channel's next overflow after which it is among the oldest `batch_size` messages. With the live threshold
   and batch size a message waits for roughly 20–70 more messages in the same channel (notifications count). A quiet channel
   may wait days; one that stays under the threshold never consolidates on its own (`$consolidate`).
3. *Minutes later:* the Cloud Task runs; Stage 1 writes the fact. From then `search_memory` and the Router prefetch find it.
4. *End of the run:* the baseline is rebuilt. The fact enters it only if selected (§4.1); otherwise it is reached by search.

**A correction.** Before consolidation the baseline still says the old value; the bot sees the correction only in recent
history, and the baseline note says history wins on conflict. At consolidation the consolidator finds the old fact and either
updates it (a new version, the old document closed with the old wording) or archives it and creates a new one; a stable
identity fact needs an explicit correction. After the run search returns the new text at once; the baseline shows it from
the next message.

**A Cabinet invalidate.** Search stops returning the fact immediately and the route rebuilds the baseline in the same
request (if the rebuild fails, the baseline keeps it until the next run; §5.2).

**A consolidation task that finds the account busy.** It sees the lease taken and an open batch, enqueues a shared deferral
and ends; the deferral retries every `LEASE_BUSY_RETRY_DELAY_S` and the first that finds the lease free runs the oldest
batch. After a crash the holder's lease stays until it expires; the first deferral after that resets the stuck `processing`
batch and runs it, so a crashed run delays its account's memory by about the lease length plus one deferral once a task
arrives. Nothing is lost and no two runs overlap, but the batch is run again from its start (§6).

**A fact with an end.** Consolidated like any fact; the model decides `expiry` from the content and code stores
`expires_at` (§2.7). Until its end it is an ordinary current fact. The first janitor run after `expires_at` stales it (with
writes on): the account's baseline is rebuilt and the fact leaves the baseline, the prefetch and `search_memory` from the
next message (§5.5). A stale fact keeps its text; the review decides what it is now (§5.6): another fact that says the same
supersedes it; a plan whose date passed is rewritten as history on evidence or its own text, otherwise asked about (the
parts of one plan folded into one question); what the owner does not answer for `QUESTION_TIMEOUT` falls back as its
question says, and a weekly digest lists what was set aside.

**A directive.** Consolidated with its batch. Stage 1 decides `directive: true` only if the rule is about agent behaviour
and holds for most requests; otherwise it is an ordinary fact reached by search. Stage 2b of the same run already sees it and
may rewrite, merge or demote it. It is in `standing_directives` from the next message after the run.

---

## 8. Costs

**Which model.** On provider Claude consolidation runs on `ConsolidationAgentConfig.claude_model` (`claude-sonnet-5-5`),
pinned in code by `src/composition/consolidation_model_setup.py::consolidation_context`; its tier stays PERFORMANCE, but
neither a deploy nor the env var `CLAUDE_PERFORMANCE_MODEL` moves it (they still move every other PERFORMANCE agent). A
user's `model_overrides["consolidation"]` wins and is the live lever without a deploy; a user who moved consolidation to
another provider gets that provider's tier model. The 📊 line names the model that ran. The decision
`docs/04_solution_strategy/decisions/sonnet55_adaptive_thinking_stall.md` records the choice. Measured per-batch figures are in
`docs/04_solution_strategy/decisions/model_refresh_2026_10.md` and `consolidation_provider_model_eval.md`.

**What drives it.** Turns × prompt (every turn resends the system prompt and the growing tool history; Claude caches the
static part); passes (Stage 1 and, when any directive exists, Stage 2b on every batch, Stage 2a when Stage 1 wrote, Stage 3
when mail is pending, so a batch with nothing new still pays for two loops); retries re-run whole batches (§6); one batch per
`batch_size` messages per channel. The janitor calls no model. The review pass is one more model loop with a large fixed
prompt, run only when a fact is stale or a reply in words waits; a digit, a button, a Cabinet click, a send, a digest and a
timeout fallback with nothing to look at cost no model call. The profile step is one structured call per fact, capped per
run by `FACT_PROFILER_MAX_PER_RUN`.

**Where it is recorded.** Every model call goes through `BaseAgent._call_llm`, which bills the account per model that ran
(`docs/04_solution_strategy/decisions/billing_execution_scoped_ledger.md`) and writes a `prompt_content` row per turn
(§9.1). The 📊 line carries no cost. Embedding calls are billed too: each successful one adds its tokens to the ledger in scope
(`src/adapters/gemini_embedding_adapter.py`).

---

## 9. Debugging and operations

### 9.1 Where to look

| Need | Source |
|---|---|
| Did overflow happen, was a batch queued, consumed, retried, failed | Cloud Logging (`make fetch-logs`, grep the lines below) |
| What the consolidator saw and answered, turn by turn | BigQuery `prompt_content`, `agent_type = "consolidation"` (30-day TTL) |
| The run as a tree, failed calls included | Logfire (a failed call has no BigQuery row) |
| What a pass did, at a glance | the 📊 / 🚨 Slack lines (§3.11) |
| What the janitor staled or would stale, the breaker, the backlog | the 🧹 morning line in the ops channel; the janitor-day collection (one snapshot per account per day); the `[FactLifecycle]` lines |
| What the review did, what is queued or unanswered, why a question was not sent | the review segments of the 🧹 line; the review state document (current question, today's count, `delivery_failed_on`, `unsuccessful_since`) and the question documents (status, `closed_by`, `answer`); the `[FactReview]` and `[FactAnswer]` lines |
| Memory telemetry over time | `docs/05_building_blocks/memory_telemetry/README.md`, `docs/04_solution_strategy/decisions/memory_telemetry.md` |

Log lines to grep (ids are shortened, no fact text is logged): overflow `Error in overflow routing … the batch stays in the
session`, `Overflow hand-off failed`, `🚨 [SessionStore] … the history passed`; queue `Reset N stale PROCESSING batches`,
`Revived N FAILED batches`; lease `[MemoryWriteLease] busy`, `[Librarian] lease busy: deferred consolidation`,
`release found another owner` (a run outlived its lease); run `[Librarian] Processing batch <id>`, `Stage 1 done`,
`Stage 2a: reviewing cluster`, `Stage 2b: reviewing N directives`, `Directive cap backstop`, `Max turns reached without final
report`, `Stage 3 (email) failed` / `timed out`, `Email cards pass n`; tools `[FactManagement] create_fact() TOOL CALL`,
`Cannot resolve fact`, `changed concurrently twice; update refused`, `moved forward between attempts`, `Merge refused`;
baseline `[BiographicalContext] Cache refreshed`; batch result `consolidated … → DELETED`, `failed (attempt n/…)`,
`CONSOLIDATION_FAILED`, and once per batch when the last allowed round failed `CONSOLIDATION_GAVE_UP` (grep it to find
batches that need the owner); `$consolidate`: `[Librarian] $consolidate failed`; Cabinet `Fact <id> not invalidated`,
`Failed to refresh biographical cache`; janitor startup `fact lifecycle sweep: log-only` / `: writes`,
`[FactLifecycle] account=…`, `breaker tripped`; review `[FactReview] …`, `[FactAnswer] question <id> answered`,
`reply to <platform> message <id> not taken`, `[SlackInteractivity] queued a fact_question choice`; profile
`[FactProfile] fact=… outcome=…`, `[FactProfile] run account=…`; expiry `[OwnerZone] owner timezone unresolved`;
search `[SearchEnrichment] Final: N facts`.

### 9.2 Why did the bot not remember X?

1. Was it said in a channel that consolidates? Companion-bound channels do not (top of this file).
2. Has that channel overflowed since? Count messages since (§7). If not: it is waiting (`$consolidate` forces it).
3. Was the batch consumed? Grep the batch id: `DELETED`, `failed`, or still in the consolidation queue.
4. What did Stage 1 decide? BigQuery `prompt_content` for that run: the candidate list, the searches, the DISCARD reasons
   in the thinking and the final report. Usual reasons: echo trap, `Trivial_Exclusions`, judged a duplicate.
5. Is the fact stored but not `current` (archived, invalidated, superseded, or `stale`)? Readers show only `current`. A fact
   that was updated is a **chain**: the closed document keeps the old wording and its `superseded_by` leads to the live one
   (§2.4), so look for the newest `version` of its `lineage_id`.
6. Stored and current but not used: not in the baseline (§4.1 selection), and this message's prefetch did not rank it
   (§4.3). Try `search_memory` with other words; check its domain and priority.
7. A fact that left memory without the owner saying so: look at its last trail entry (`actor`, `reason`). `janitor` = its
   expiry passed (it is `stale`); `review` or `timeout` = the review set it aside, and it is in the next weekly digest and in
   the Cabinet's "set aside lately" for 30 days; `owner` = his own answer. A set-aside fact is brought back by telling the
   bot in chat that it is still true (the Cabinet cannot).

### 9.2b Why did no question arrive, or why did one not move?

1. Is there a stale fact? The Cabinet's "waiting for review" view, or the 🧹 line's `backlog`. A review runs only when a
   stale fact, a reply in words, a timed-out question, an unfinished send, a due digest or a queued question needs it
   (§5.5 step 7).
2. Did the review run? The review state document: `enqueued_at`, `started_at`, `last_review_at`, `last_failed_at`.
3. Did the review decide the facts itself? Most facts are decided from their neighbours and ask nothing. Look at the
   counters (`revived`, `archived_by_review`, `queued`).
4. Did a question arrive twice? A send that crashed between the post and the `asked` mark is completed by the next run's
   housekeeping from the recorded post or message reference, never sent again (§5.6 housekeeping).
5. Is a question waiting? `queued` questions wait for the current one to be answered, or for the next local day; at most
   `MAX_QUESTIONS_PER_DAY` go per local day; a failed delivery stops that day; with no primary channel, or one bound to a
   companion, nothing is sent (`no question sent … the queue waits`). The Cabinet's question list shows the same queue.
6. Did a button do nothing? Interactivity must be enabled in the Slack app with the main service's `/slack/interactivity` as
   its request URL; a click that failed leaves no `[SlackInteractivity] queued …` line (a bad signature is a 401). A thread
   reply works without it.
7. Did a digit get "That fact has changed in the meantime"? The fact was rewritten after the question was asked; the review
   or the timeout handles that question.
8. An invoice never arrives as a chat question: a routine invoice is a digest question (weekly digest, Cabinet group). A fact
   the review rewrote as history from evidence or its own text asks nothing either.

### 9.3 Why did it remember something wrong?

- From the bot's own words: a bot reply reaches the consolidator only as its summary (§3 step 1); `Domain_Scope` forbids
  storing assistant statements unless the user adds new information. When this happens it is a model error; the run's
  BigQuery rows show which message it came from.
- A stale value: a newer fact's wrong `date` or `horizon` shows in the consolidator's `prompt_content` row (§9.4). An update
  wrote a new version; the earlier wording stays in the closed document of the lineage (§2.4).
- From mail: Stage 3 writes facts from indexed mail (§3 step 8).
- From a system note (a known gap, current behaviour): notes the system writes into history are stored as `role="user"`
  messages: every `notify()` alert with the default `save_history=True`, the call-event line and the async-document note
  (`src/services/user_notification_service.py`), and the `$memory save` pair (`src/services/memory_fact_service.py`). The consolidator reads them as `User (…): …`
  (`src/services/prompt_v3/context_formatter.py`), and `Domain_Scope` bars only ASSISTANT statements, so a reminder's alert
  text sits in the owner's slot.
- Fix: invalidate it in the Cabinet (the baseline is rebuilt at once), or tell the bot the correction.

### 9.4 What did consolidation do in a given batch?

0. Starting from a fact: a fact carries no batch id. Take its `created_at` (`last_updated` for an update), find the nearest
   earlier `Processing batch` line, then search that run's `tool_calls` in `prompt_content` for the fact text. Logs and
   `prompt_content` are the only trail.
1. Find the run: the `[Librarian] Processing batch <batch_id>` line and its time.
2. BigQuery, one row per model turn, in order: `SELECT turn, request_text, response_text, tool_calls FROM … prompt_content
   WHERE agent_type = "consolidation" AND timestamp BETWEEN … ORDER BY timestamp` (use `--format=json`). The pass restarts
   its turn count, so Stage 1, 2a, 2b and mail appear as consecutive runs of turns 1, 2, …; the user message of turn 1 names
   the pass ("Begin deliberate consolidation.", "FACT CLUSTER REVIEW", "STANDING DIRECTIVES REVIEW", the mail candidates
   header).
3. `tool_calls` carry the arguments (fact texts, ids, attributes); the log lines carry the results.

### 9.5 Owner scripts

All need the owner's OK, credentials and `.env`; exports go to gitignored folders only.

| Script | What it does |
|---|---|
| `scripts/consolidation/` benches (`ab_cross_provider.py`, `test_directive_review_dryrun.py`, `test_stage1_classification_dryrun.py`, `test_confirmed_facts_dryrun.py`, `test_file_ops_exclusion_dryrun.py`, `test_cluster_audit_dryrun.py`, `test_decomposition_dryrun.py`, `test_anchor_cluster_dryrun.py`) | run the real agent against live reads: fact writes are intercepted and the cache refresh is stubbed, but the model calls are real (paid) and may be recorded in the account's billing counters |
| `scripts/email/test_consolidation_dryrun.py` | the dry run of Stage 3 (mail cards) |
| `--s3-kit` of the stage 1 classification, cluster audit, decomposition and directive review benches, the mail bench and the review benches (`scripts/consolidation/s3_dry_run_kit.py`, `scripts/consolidation/s3_bench_support.py`) | puts a no-write fact store under the **real** agent and the real `FactManagementAdapter` (parse, expiry, pin, born-due, `derived_from` and the notes run as in production), a no-write mail repository and a recording counters store: no fact, cache document, mail mark or lifecycle counter is written; spend is capped by the kit's own ledger under `scripts/memory/` |
| `scripts/consolidation/test_review_answers_dryrun.py`, `scripts/consolidation/test_review_dryrun.py` | the review benches: Stage 1 over the history shapes of a question and a reply (the quoted fact must be left alone), and the real `FactReviewService.run` on the kit over live stale facts and seeded cases; no stale fact, question, message or counter is written live |
| `scripts/migration/fact_p2_reopen_archives.py`, `scripts/migration/fact_p2_owner_decisions.py` | re-open archived or invalidated facts of an owner-chosen list as `stale` so the daily review decides them again, or write the owner's own decision on each; dry run by default, `--apply` writes under the scanned `update_time` |
| `scripts/migration/fact_p2_rollback_questions.py` | brings review questions back to the older shape before a rollback; dry run by default |
| `scripts/migration/fact_directive_flag_mark.py` | sets the `directive` flag from the old domain (§4.2) |
| `scripts/export_account_facts.py`, `scripts/validation/inspect_consolidation.py`, `scripts/cleanup_consolidation_queue_dev.py`, `scripts/admin/fix_broken_vector_facts.py` | dump the account's facts to a gitignored folder; print the queue and recent facts; clean the dev queue; repair facts with broken vectors |
| `scripts/people/assign_people_entry_ids.py` | gives the confirmed people entries their stable ids |

**A re-review of closed facts** is always a dry run first: run the review bench `scripts/consolidation/test_review_dryrun.py
--s3-kit --stage-ids <ids file>` to read what the review would write for each fact (its `--expiry-plan` input is an expiry plan
JSON; no tool in the repo produces one now, so omit it), and only then re-open the kept ones with the reopen script or write
the owner's decisions.

### 9.6 The data audit

`python -m scripts.data_audit.run facts [--account <id>] [--limit N] [--s2-cutoff <ISO time>] [--expiry-required]
[--janitor-ran-at <ISO time>]` (`scripts/data_audit/run.py`, `scripts/data_audit/facts.py`) streams the facts collection
read-only and checks every stored document against the invariants: stored `id` equals the document id, `account_id` and
`state` present and valid, `is_current` / `valid_to` matching `state` (§2.3), `created_at` present, all three vectors
stored as vectors, the document parses as a `FactEntity`, and no lineage with more than one `is_current` document. It
prints counts only and writes the offending ids per class to `scripts/memory/` (gitignored). Run it after any migration or
backfill.
- **Expiry classes** (`check_expiry`): `current_without_expiry_kind` (a violation only with `--expiry-required`),
  `pinned_expiry_not_none`, `current_with_stale_since`, `horizon_not_counted_from_reported_date` (the rule is `>=`: cleanup
  passes floor a due horizon and a re-mention never moves `expires_at` earlier, so a later date is legal),
  `current_past_expiry_not_waiting` (judged only with `--janitor-ran-at`; facts a pool cap deferred or a breaker held back
  appear here too, so compare with that run's `deferred` before reading it as a defect).
- **Version-chain classes:** the `superseded_by_*`, `replaces_*`, `version_not_unique`, `lineage_open_below_highest_version`
  and `superseded_without_successor` checks of `scripts/data_audit/facts.py`. `--s2-cutoff` splits the last: a superseded
  document without `superseded_by` last written before the cutoff predates the version chain and is not a violation; with
  version pointers present and no cutoff the class is reported as not judged. `--limit` and `--account` make the
  cross-document classes unreliable (a target outside the read looks dangling).
- **A skipped drain** between an old and a new revision shows as `lineage_multiple_current` plus
  `replaces_not_pointing_back`: an old-revision blind write re-opened an older version. Do not delete either by reflex: the
  re-opened document may hold the newest wording. Compare both in the console, carry the newer wording over, then close the
  re-opened older document by hand (`state=superseded`, `is_current=false`, `valid_to`, `superseded_by`). A Cabinet delete does
  not restore the chain (it sets `invalidated` with `is_current=true`).

### 9.7 The prompt snapshot and its vocabulary tests

The consolidation prompt lives in Firestore; `prompts_snapshot/` is its git mirror: profile `consolidation.yaml`,
blueprint `consolidation_agent_v1.yaml` and the six `CONSOLIDATION_*` tokens; the mail pass has `consolidation_email.yaml`
over `CONSOLIDATION_EMAIL_CANDIDATES`; the review pass has `consolidation_review.yaml` over `CONSOLIDATION_TAXONOMY` and
`CONSOLIDATION_LIFECYCLE`; Smart's profile carries `PROTOCOL_MEMORY_REVIEW_MARKERS`. `COGNITIVE_PROCESS_CONSOLIDATION.groovy` is an older token no
profile renders: an edit to it ships nothing. Tokens ship by token upload, not by a
code deploy (`CLAUDE.md` § Traps): edit there and upload with `firestore_utils/snapshot_upload.py`
(`prompts_snapshot/README.md`). The static template is cached per instance, so the upload goes in before code that depends
on it. The tokens are pinned to the code by tests: `tests/unit/prompts/test_consolidation_vocabulary.py` checks every enum
value and tool field the tokens name against `src/domain/entities.py` and the tool declarations;
`tests/unit/prompts/test_consolidation_expiry_prompt.py` pins the expiry contract (kinds named only as `date` / `horizon` /
`none`, no token offers `ttl_days`, every instruction to retire a fact with `SUPERSEDED` names `superseded_by`, every
create / merge / content-update example carries an `expiry`, the extension rule is worded);
`tests/unit/prompts/test_consolidation_review_vocabulary.py` and `test_consolidation_echo_trap_exception.py` pin the review
vocabulary and the echo-trap exception. The review pass cannot run without its profile.

---

## 10. Not built

- A Cabinet "bring back" for a closed fact (only the chat can).
- Telegram inline keyboards on the interactive-message port (Telegram gets plain text with digits); Smart sending
  interactive blocks through the port.
- An answer given on a phone call counts as silence (no Lelik filter on the question pair).
- A Cabinet or script view of old versions of a fact (§2.4).

---

## 11. Where to start reading

| Subject | Entry points |
|---|---|
| The fact and its rules | `src/domain/entities.py::FactEntity`, `src/domain/fact_lifecycle.py` (states, forward resolution, expiry computation, `born_due`), `src/domain/fact_update.py`, `src/domain/fact_update_plan.py`, `src/domain/fact_expiry.py`, `src/domain/fact_write_context.py`, `src/domain/pass_actor.py::PassActor` |
| Fact writes | `src/adapters/fact_management_adapter.py::FactManagementAdapter`, `src/services/fact_write_service.py::FactWriteService`, `src/adapters/firestore_repo.py::FirestoreFactRepository` (queries, version, merge and in-place transactions, `resolve_current`, the cache document, the janitor's reads), `src/ports/repository.py` |
| Sessions, overflow, queue, lease | `src/adapters/firestore_session_store.py::FirestoreSessionStore`, `src/services/overflow_routing_service.py::OverflowRoutingService`, `src/adapters/firestore_consolidation_queue.py`, `src/services/consolidation_service.py::ConsolidationService`, `src/adapters/firestore_memory_write_lease.py`, `src/domain/memory_write_lease.py`, `src/domain/consolidation.py`, `src/domain/consolidation_window.py` |
| The consolidator | `src/agents/consolidation_agent.py::ConsolidationAgent`, `src/domain/consolidation_pass_stats.py`, `src/infrastructure/agent_config.py::ConsolidationAgentConfig`, `prompts_snapshot/tokens/system/CONSOLIDATION_*.groovy` |
| The baseline and directives | `src/services/biographical_context_service.py::BiographicalContextService`, `src/services/prompt_builder.py`, `src/services/prompt_v3/biographical_formatter.py`, `src/domain/settings.py::SearchConfig` |
| Search | `src/services/fact_search_service.py::FactSearchService`, `src/services/search_enrichment_service.py::SearchEnrichmentService`, `src/agents/memory_search_agent.py::FactsMemoryAgent`, `src/agents/core/router_agent.py::RouterAgent`, `src/domain/rrf.py`, `src/composition/memory_search_setup.py` |
| The profile step | `src/services/fact_profile_service.py::FactProfileService`, `src/agents/fact_profiler_agent.py::FactProfilerAgent`, `src/domain/fact_domains.py`, `src/domain/person_cards.py`, `src/composition/fact_profile_setup.py` |
| Janitor and review | `src/services/fact_lifecycle_sweep_service.py::FactLifecycleSweepService`, `src/domain/fact_lifecycle_sweep.py`, `src/services/fact_review_service.py::FactReviewService`, `src/services/fact_answer_service.py::FactAnswerService`, `src/domain/fact_review_rules.py`, `src/infrastructure/fact_review_scope.py::FactReviewScope`, `src/adapters/firestore_fact_review_store.py` |
| Interactive messages | `src/domain/interactive_message.py`, `src/infrastructure/interactive_message_router.py`, `src/adapters/slack/interactive_message_adapter.py`, `src/adapters/plain_text_interactive_message_adapter.py`, `src/services/interactive_choice_service.py` |
| `$memory save`, `$consolidate`, Cabinet | `src/services/memory_fact_service.py::MemoryFactService`, `src/handlers/conversation_handler.py::ConversationHandler`, `src/services/cabinet_fact_service.py::CabinetFactService`, `src/web/user_cabinet_app.py` |
| Wiring | `src/composition/memory_setup.py::build_memory`, `src/composition/application_graph.py::build_application_graph`, `src/composition/user_agent_factory.py` |
