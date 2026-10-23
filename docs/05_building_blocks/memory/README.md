# Memory (Building Block)

The owner's map of long-term memory: how a conversation becomes facts, how facts reach every prompt, how they are
searched, what fails and what it costs. Current state only. The code paths (`path:line`) are the ground truth; when this
text and the code disagree, the code wins and this file is wrong. Every session that touches memory facts,
consolidation, the biographical baseline, memory search, `$memory save`, standing directives or the fact lifecycle ends
by updating this file.

Design records: `docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md` (taxonomy, tools, cognitive process),
`docs/10_rfcs/CLUSTER_REVIEW_RFC.md` (Stage 2a), `docs/10_rfcs/STANDING_DIRECTIVES_RFC.md` (Stage 2b),
`docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md` (`$memory save`), `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (S1 and S2 built,
S3–S5 not built), decisions `docs/04_solution_strategy/decisions/{fact_s1_defects,fact_versions_one_writer,standing_directives,directive_applicability_gate,memory_facts_owner_confirmed,consolidation_one_batch_per_task,consolidation_failed_batch_bounded,sonnet55_adaptive_thinking_stall,consolidation_provider_model_eval}.md`.

## 0. How to answer questions about memory

| Question | Read |
|---|---|
| What is in memory; what a fact holds; what `domain`, `temporal_class`, `state`, `context_priority` mean | §2 |
| What `is_current`, `valid_to`, `lineage_id`, `version`, `superseded_by` mean today; what an update or a merge does to them | §2.3, §2.4 |
| Why an update gave the fact a new id; whether the old wording is kept and how to look at it; why an old id still works | §2.4, §9.3 |
| When does a conversation become facts; why nothing happened after I told it something | §3 steps 1–3, §7 |
| How to deploy or roll back the fact versions (S2) and how to read the audit afterwards | `docs/07_deployment/README.md` "Facts S2", §9.6 |
| What the memory-write lease is; why a consolidation task waited or ran twice a few minutes apart | §3 step 3, §6, §7 |
| What the consolidator sees (whose words, which text of the bot's replies) and how it decides create / update / merge / discard | §3 steps 4–5 |
| Why the bot remembered something wrong, or something it said itself (echo trap) | §3 step 5, §9.3 |
| What Stage 2a (cluster review) and Stage 2b (directive review) do and when they run | §3 steps 6–7 |
| What a standing directive is; why one disappeared or was rewritten; the cap of 15 | §3 step 7, §4.2, §5.3 |
| What the bot sees about me in every message (the baseline); how it is chosen and ordered; its limits | §4.1 |
| How memory search ranks (channels, RRF, dedup); what `search_memory` returns | §4.3, §4.5 |
| What the Router prefetches before Smart answers | §4.4 |
| What Lelik and the claude.ai connector (MCP) see | §4.6, §4.7 |
| What `$memory save` does; why the bot never writes memory itself | §5.1 |
| What the Cabinet fact page reads and changes | §4.8, §5.2 |
| What `$consolidate` does | §5.4 |
| What happens on a tool error, a bad enum value, a provider outage, the turn cap, a crash; what retries | §6 |
| A fact told today, a correction, a directive: when each is in memory and in the baseline | §7 |
| Which model consolidates; what a batch costs; where cost is recorded | §8 |
| How to see what consolidation did in a given batch; which log lines, BigQuery, Logfire | §9.1, §9.4 |
| Why the bot did not remember X | §9.2 |
| Why an update, a merge or a Cabinet delete did not apply ("changed concurrently", "merged into") | §6, §5.2, §9.2 |
| Why a reminder's or a system note's text became a fact | §9.3 |
| Which batch wrote a given fact | §9.4 (step 0) |
| Owner scripts, dry-run benches, the data audit, the prompt snapshot and its vocabulary tests | §9.5–§9.7 |
| Expiry, janitor, review pass, owner questions | §10 (not built) |
| Where a piece of code lives | §11 |

---

## 1. Overview

### 1.1 The cycle

1. The owner talks in a chat channel. Every message and every bot reply is appended to that channel's session.
2. Smart answers from the session's recent messages, the **baseline** (facts in every prompt, §4.1), the Router's
   **prefetch** (facts searched for this message, §4.4) and, when it asks, **`search_memory`** (§4.5).
3. When a session grows past a threshold, its oldest messages are cut off as a **batch** and queued (§3 steps 2–3).
4. A Cloud Task runs the **ConsolidationAgent**: one model, a tool loop, three passes (Stage 1 conversation → facts,
   Stage 2a cluster review, Stage 2b directive review) and a pass over indexed emails (§3 steps 4–8).
5. At the end the baseline cache is rebuilt, so the next message sees the new facts (§3 step 9).

There is no other writer of facts in normal operation: the bot does not save facts itself (§5.1). The owner can
invalidate a fact from the Cabinet (§5.2); that rebuilds the baseline at once.

### 1.2 The stores

| Store | Collection | What it holds | Written by |
|---|---|---|---|
| Facts | `{prefix}domain_facts_v2` (`src/config/environment.py:114-120`) | one document per fact **version** (a content change adds a document, a lifecycle change edits one, §2.4), three 768-dim vectors each | consolidation tools (§3), Cabinet invalidate (§5.2) |
| Sessions | `{prefix}sessions` (`environment.py:455-463`), doc id `{user_id}:{channel_id}` | the channel's recent messages (the sliding window) | `ConversationHandler`, notifications, `$memory save` |
| Consolidation queue | `{prefix}consolidation_queue` (`environment.py:465-472`) | batches cut from sessions, until consolidated | overflow routing (§3 step 3), `$consolidate` |
| Baseline cache | `{prefix}user_context`, doc id = account id (`environment.py:524-531`) | `biographical_facts`, `principles`, `directives` lists | `refresh_biographical_context_cache` (§3 step 9) |
| Memory-write lease | `{prefix}memory_write_leases`, doc id = account id (`environment.py:507-513`) | `lease_owner`, `lease_until`, `acquired_at`; no TTL policy | `ConsolidationService`, one lease per batch (§3 step 3) |
| Memory drafts | `{prefix}memory_fact_drafts`, doc id `{user_id}:{code}` (`environment.py:350-354`) | facts Smart proposed, 7-day TTL | `propose_memory_facts` (§5.1) |

### 1.3 The actors

| Actor | Role | Code |
|---|---|---|
| `FirestoreSessionStore` | appends messages, cuts overflow batches | `src/adapters/firestore_session_store.py` |
| `OverflowRoutingService` | sends a batch to Alek's queue or a companion's | `src/services/overflow_routing_service.py` |
| `ConsolidationService` | takes the account's memory-write lease, takes one pending batch, runs the agent, deletes or retries the batch; defers itself when the lease is taken | `src/services/consolidation_service.py` |
| `FirestoreMemoryWriteLease` | the lease: one memory-writing run per account | `src/adapters/firestore_memory_write_lease.py` |
| `ConsolidationAgent` | the model loop, all stages | `src/agents/consolidation_agent.py` |
| `FactManagementAdapter` | the tools: search, create, update, merge; follows stale ids forward, writes under preconditions | `src/adapters/fact_management_adapter.py` |
| `FactWriteService` | builds a `FactEntity` with its three vectors and stores it | `src/services/fact_write_service.py` |
| `FirestoreFactRepository` | every fact query; the version, merge and in-place transactions; forward resolution; the baseline cache document | `src/adapters/firestore_repo.py` |
| `BiographicalContextService` | chooses the baseline | `src/services/biographical_context_service.py` |
| `SearchEnrichmentService` | multi-vector search + RRF | `src/services/search_enrichment_service.py` |
| Router, Smart, `FactsMemoryAgent`, Lelik, MCP | readers (§4) | §11 |

### 1.4 What is not this memory

Companion channels (a channel bound with `$agent tutor`) do not feed facts: their overflow goes to the companion's own
extraction queue (`overflow_routing_service.py:56-63`, `COMPANION_AGENTS_RFC.md`). The drive index, the agent workspace
and the Gmail index are separate stores; only Stage 3 (§3 step 8) reads indexed emails into facts. Lelik's phone calls
reach memory only through the call-summary pair written into the primary channel's history, which is then consolidated
like any other message.

---

## 2. A fact

### 2.1 The fields that matter

`FactEntity` (`src/domain/entities.py:101-179`):

| Field | Meaning today |
|---|---|
| `id` | document id (UUID) |
| `account_id`, `created_by_user_id` | tenant (every query filters `account_id`); author |
| `text` | the fact, one self-contained sentence; the only thing most readers show |
| `vector`, `tags_vector`, `metadata_vector` | the three embeddings (§2.6) |
| `tags` | keywords; `mindset` makes the fact a principle (§4.1); `consolidated` is added to every fact, `anchor` to principles (`fact_write_service.py:153-157`); an update keeps both (§2.4) |
| `type` | `state`, `event`, `principle`, `system`, `alert` (`entities.py:7-12`); the consolidator does not set it, so it defaults to `event` (`fact_management_adapter.py:207`) |
| `metadata` | structured values (numbers, dates) |
| `domain`, `temporal_class`, `state`, `context_priority` | the four axes (§2.2) |
| `context` | a short label for why it was recorded; shown by `search_memory` |
| `reported_date` | when the fact was told; shown by `search_memory` as `reported: YYYY-MM-DD` |
| `created_at` | ordering of the baseline and the domain channel; a version copies it from the document it replaces, so it is the fact's first mention |
| `version`, `lineage_id`, `replaces_fact_id`, `superseded_by`, `is_current`, `valid_from`, `valid_to` | §2.3–§2.4 |
| `ttl_days`, `expires_at`, `last_updated` | written, read by no code (expiry is §10) |
| `visibility` | defaults to `account_shared`; never filtered on read: every member of an account sees every fact |

The adapter also stores `context_priority_rank` (critical 1 … archival 5) next to the entity for ordering
(`firestore_repo.py:38-44`, written by `_to_document`, `:176-184`, for every new document).

### 2.2 The four axes

| Axis | Values (`entities.py`) | What code does with it |
|---|---|---|
| `domain` | `biographical`, `possession`, `health`, `medical_records`, `location`, `work`, `network`, `preference`, `agent_directive`, `skill`, `project`, `finance`, `education`, `legal`, `entertainment`, `communication` (`:18-35`) | baseline order (biographical first), standing directives (`agent_directive`), Router domain channel, Lelik's filter |
| `temporal_class` | `permanent`, `stable`, `dynamic`, `ephemeral` (`:38-43`) | none; shown to the consolidator. The prompt's TTLs (90 / 14 days) are not enforced by any code |
| `state` | `current`, `stale`, `archived`, `superseded`, `invalidated` (`:46-52`) | **the only visibility switch**: every reader filters `state == "current"` (`firestore_repo.py:232,286,400,471,661,899`). No code ever sets `stale` |
| `context_priority` | `critical`, `high`, `medium`, `low`, `archival` (`:55-61`) | baseline order; `archival` (rank 5) is never in the baseline or the rulebook (`firestore_repo.py:479`) |

The model writes these in upper case; `normalize_fact_taxonomy` lowercases them before the enums parse
(`entities.py:83-94`). A value outside the enum fails the tool call (§6). The prompt (as mirrored in `prompts_snapshot/` on main) tells the model to use
`HISTORICAL` as a priority, which is not an enum member: such a create fails. In the 30 days before 2026-10-21 none
of 87 written priorities was `historical` (`decisions/fact_s1_defects.md`); the vocabulary fix is in §9.7.

### 2.3 States and the SCD2 flags

`domain/fact_lifecycle.py:11-26` maps a state to its flags; every retirement path uses it (adapter update and merge,
Cabinet invalidate, the directive backstop):

| State | `is_current` | `valid_to` | `superseded_by` | Typical cause |
|---|---|---|---|---|
| `current`, `stale` | true | none | none | created; `stale` is never set |
| `archived` | true | now | none | "was true, no longer" (the prompt's Negation rule) |
| `invalidated` | true | now | none | "was never true", Stage 2b removal, Cabinet, backstop |
| `superseded` | **false** | now | the successor's id (empty on documents closed before S2) | a document replaced by a newer version of its lineage, a merge source, or a compound fact the model split (it must name the fact that replaces it; the call is refused without it) |

`is_current` means "latest document of its lineage", not "visible". Visibility is `state` alone. No `src/` code reads `is_current`; the data audit checks it (§9.6).

### 2.4 Updates, merges, versions, lineage

A fact is a **lineage**: the documents that share one `lineage_id`, told apart by `version`. At most one of them is
the live one (a lineage whose last document was merged away has none). Two kinds of write exist (`fact_update_plan.py:30-73`):

- **A content change is a new document.** If an `update_fact` changes the text, the tags, the metadata or the
  `temporal_class`, the store writes a new document: new `id`, same `lineage_id`, `version` + 1,
  `replaces_fact_id` = the old id, `created_at` copied from the old one, `valid_from` now, and `reported_date` = the
  value the call sent, else now (until FACT_LIFECYCLE S3). The old document is closed in the **same transaction**:
  `state=superseded`, `is_current=false`, `valid_to` now, `superseded_by` = the new id
  (`firestore_repo.py:562-587`, `fact_lifecycle.py:73-75`). The result's `fact_id` is the **new** id.
  "Tags changed" compares the lists as multisets, so resending the stored tags in another order is not a change.
- **A lifecycle change stays in place.** A call that only changes `state` or `reported_date` (or changes nothing)
  writes just those fields and `last_updated` onto the live document; `version` and the id stay
  (`fact_update_plan.py:88-98`, `firestore_repo.py:589-603`; only the keys of `IN_PLACE_FIELDS`,
  `fact_lifecycle.py:34`, can be written this way). Re-applying the stored state leaves `valid_to` alone. A state change
  together with a content change goes into the new version.
- **`domain` and `context_priority` cannot be changed by an update**: the tool has no such fields. A different domain
  means a new fact (Stage 2b demotes a directive by creating a `preference` fact and invalidating the directive).
- **Retiring a fact for another one needs the other one.** `state=SUPERSEDED` needs `superseded_by`, the id of the
  replacing fact (resolved forward like any id, and it must be a different fact). Without it, or together with a
  content key, the call is refused (`fact_update.py:24-53`).
- **Tags.** The system tags `consolidated` and `anchor` survive a tags update (appended at the end, in stored order).
  `tags: null`, `tags: []` and an empty `content` are refused: an empty field never destroys data
  (`fact_update.py:56-71`).
- **Everything is checked before anything is embedded or written**: enums, dates, tags, content, on create, update and
  merge (`fact_update.py`, `new_fact_attributes.py:35-78`). One bad fact fails a whole `add_facts_batch` call (the
  legacy v2 path) before any embedding. A fact cannot be created or merged into `state=superseded`.
- **Vectors follow content.** All three are re-embedded when the three embedding texts change (§2.6), before
  anything is written (`_complete_version`, `fact_management_adapter.py:327-345`); an embedding failure fails the call
  with nothing stored. A `temporal_class`-only version copies the old vectors. After a conflict the retry reuses an
  embedding made for the same texts.
- **A stale id still works.** The model reuses ids from an earlier search, and the consolidator often writes one fact
  twice in a pass. `update_fact` and `merge_facts` follow `superseded_by` from the id they got to the live document
  (`resolve_current`, `firestore_repo.py:541-560`, one hop at a time through `forward_step`, `fact_lifecycle.py:41-70`).
  They stop with an error in these cases: the chain leaves the lineage (the fact was **merged** into another lineage:
  "merged into <id>; search again"), a superseded document has no successor (every document closed before S2),
  the end is `archived`, `invalidated` or `stale`, or the chain is longer than 50 hops. The adapter returns each as
  `status: "failed"` with that message (§6). Only a `current` document can be updated by the consolidator
  (`CONSOLIDATION_ACCEPTS`); the Cabinet also accepts archived, invalidated and stale ones (`CABINET_ACCEPTS`, §5.2).
  When an update was followed forward, the result says so: it carries `resolved_from` (the id the model sent) and its
  message reads "…fact <old id> was replaced earlier, so the update was applied to its current version <id>"
  (`fact_management_adapter.py:303-309`).
- **Writes carry a precondition, not a lock.** Every write reads the document and writes only if its `update_time` is
  unchanged. On a conflict the adapter resolves again and tries once more; a second conflict is a failed result
  ("changed concurrently; search again"). **A content or tags update is also refused when the second read lands on a
  different document than the first**: `content` and `tags` replace the whole value, so written over a version the
  model never saw they would silently drop that version's change (`fact_management_adapter.py:268-277`). Metadata,
  `temporal_class`, `state` and `reported_date` updates do not refuse; they re-resolve and apply to the newer version.
  Two calls of one turn on the same fact therefore settle in the store, across processes. A content change and an
  archive of one fact in one turn: the archive has no embedding to wait for, so it normally commits first and the
  content call then fails with "fact X is archived". Two content (or two tags) changes gathered in one turn, both
  reading the same version: the first applies, the later one is refused ("changed concurrently; search again") and the
  model must search again. A **later call that still sends the old id** is not refused: it is forwarded to the current
  version and applied over it, with `resolved_from`.
- **A merge is one transaction.** `merge_facts` resolves every source forward, builds the merged fact (new id, new
  lineage, three vectors) and then, in one transaction, creates it and closes every source with
  `superseded_by` = the merged id (`fact_management_adapter.py:386-427`, `firestore_repo.py:562-587`). A failure writes
  nothing, so the sources stay live. A missing or foreign source is skipped; no usable source is
  "Failed to supersede any facts"; a source that ends in another lineage or in a non-current state fails the whole
  merge; a source that moved forward between the two attempts fails it too ("source changed; search again").
- **History.** The old wording stays in the closed document. There is **no Cabinet view or script for old versions**.
  To read them, open `{prefix}domain_facts_v2` in the Firestore console, take the fact's `lineage_id` and filter
  `lineage_id == <it>`; the documents sorted by `version` are its history, each closed one with its old text. A merged
  fact starts a new lineage: its sources are the documents with `superseded_by == <the merged fact's id>`.
  The repository method `get_lineage(account_id, lineage_id)` returns the same versions by `version`, without vectors
  (`firestore_repo.py:515-528`); nothing in the product calls it yet.
  `get_latest_fact_by_lineage` returns the highest version that is not superseded (`:530-539`). History starts at the
  deploy: the 442 documents superseded before it (of 1112 on 2026-10-23) have no `superseded_by`.
- **One more version per retry.** A consolidation batch that fails and is retried runs Stage 1 again over facts it
  already wrote; an update it repeats on the live version can add one more version document (§6).
- `consolidate_cluster` (`consolidation_agent.py:432`) has no live caller: nothing in `src/` sends it, only the dry-run
  scripts reach it. It runs without the lease and without the run tracker, so it has no failure refresh (§3 step 9). If
  it ever gets a caller it needs both.

### 2.5 Tenancy

Every read filters `account_id`. `resolve_current` returns nothing for a document of another account, decided before it
follows any pointer, so `update_fact` answers `not_found` (the same words as for a missing id) and `merge_facts` skips
the source; a pointer that leads into another account is "cannot be resolved" and never names the foreign id
(`firestore_repo.py:541-560`, `fact_lifecycle.py:54-55`). A `superseded_by` successor that is missing or foreign is
"successor fact not found". Cabinet invalidate checks the stored account of the id it is given and returns 403
(`firestore_repo.py:316-322`). `visibility` is not enforced (owner, 2026-10-21).

### 2.6 The three vectors

`domain/fact_embedding.py:9-17` defines the texts, shared by creation and re-embedding: `text`; `tags` joined (system
tags `consolidated`/`anchor` left out; the text when no tag is left); `metadata` as JSON (the text when empty). All
three are embedded with `RETRIEVAL_DOCUMENT` in one batch call (`fact_write_service.py:181-187`), model
`gemini-embedding-2` at 768 dimensions (`src/adapters/gemini_embedding_adapter.py`). Searches embed queries with
`RETRIEVAL_QUERY` (`search_enrichment_service.py:123`).

---

## 3. A fact's life, step by step

### Step 1 — the message is saved

`append_messages_batch` adds the turn to the session `{user_id}:{channel_id}` in one transaction
(`firestore_session_store.py:168-273`). A user message keeps its full text. A bot reply stores two texts: `full_text`
and `text`, the summary Smart writes for history (up to 60 words, `smart_response_agent.py:125`). Notifications,
reminders, the daily email review, call summaries and `$memory save` pairs are messages in the same sessions and count
toward the window.

### Step 2 — the sliding window overflows

When the session holds more than `threshold` messages after an append, the oldest `batch_size` are cut off, repeatedly
until it is back under the threshold (`firestore_session_store.py:223-231`):

- **Threshold 70, batch 50** in the live environment (`src/domain/settings.py:18-24`; the `is_production` branch sets
  50/30 and is not live, `src/config/settings.py:160-170`). Wired at `src/composition/service_container.py:329-337`.
  The values are global: `UserBotConfig.consolidation_threshold` has no effect anywhere, and
  `consolidation_batch_size` only sizes the `$consolidate` cut (§5.4).
- A channel bound to a companion uses that binding's own window and routes elsewhere (§1.4).
- **No time trigger.** A channel that never passes 70 messages is never consolidated on its own. A session idle for
  90 days is deleted on its next load, unconsolidated messages included (`firestore_session_store.py:58,93-98`).
  `$consolidate` is the manual lever.

### Step 3 — the batch is queued

The cut batch goes to `OverflowRoutingService.route_overflow` as a background task (`firestore_session_store.py:258-265`):

1. `serialize_messages_for_consolidation` (`src/domain/consolidation_serialization.py:20-46`): **user parts** →
   `consolidation_text or text` (`consolidation_text` is legacy, no writer left); **bot parts → `text`, the summary**,
   never `full_text` (verbose, carries web-search context). Each message keeps its role and `created_at`.
2. A `ConsolidationBatch` (`src/domain/consolidation.py:14-28`) is stored in the queue with status `pending`
   (`overflow_routing_service.py:93-103`).
3. A Cloud Task `task_type=consolidation` is enqueued for the user, with a 1800 s dispatch deadline
   (`src/adapters/gcp_task_queue.py:408-438`).

`/worker` → `WorkerHandler._handle_consolidation` (`src/handlers/worker_handler.py:444-460`) →
`ConsolidationService.process_user_batches(max_batches=1)` (`consolidation_service.py:110-121`, the loop `_run`, `:141-172`).
Each batch runs under the account's **memory-write lease** (`_consolidate_one`, `:220-237`):

- **takes the lease.** One lease per account, in `{prefix}memory_write_leases`, a fresh random owner per batch,
  held for the 15-minute run timeout plus a 60 s margin = 16 min (`domain/memory_write_lease.py:5-16`, adapter
  `firestore_memory_write_lease.py:23-46`). Any unexpired lease means "busy", even one this same service took a moment
  ago. So two users of one account, two tasks of one user and `$consolidate` all queue behind each other. Why:
  every fact write is a precondition write (§2.4), but two runs searching and deciding at once still produce duplicates
  or contradicting decisions; one writer per account removes that (`decisions/fact_versions_one_writer.md`);
- **busy: it defers itself.** If the user has any batch that is `pending`, `retry_pending` or `processing`
  (`has_open_batches`, `firestore_consolidation_queue.py:100-109`), the run enqueues a `consolidation` task for the user
  with a 120 s delay and returns having done nothing; the worker does **not** enqueue another one itself
  (`_defer`, `consolidation_service.py:341-353`). The deferral has a dedup key per 120 s time bucket,
  `consolidation-defer:<user>:<bucket>`; the Cloud Tasks **name** is `consolidation-<sha256 of that key, first 32
  characters>` (`gcp_task_queue.py:369-373`), so searching the queue for "consolidation-defer" finds nothing. Tasks that
  lose at the same time share one deferral, and a loser that loses again lands in the next bucket. With no open batch a
  busy run just stops;
- **resets zombies, and counts them.** Under the lease, a `processing` batch older than **60 s** is a dead run's. It goes
  back to `retry_pending` with `attempts` + 1 (`reset_recoverable_batches(zombie_after_s=60)`,
  `firestore_consolidation_queue.py:111-202`), and at 3 it goes `failed` instead (`CONSOLIDATION_FAILED`, error "run lost
  while processing"). A run that kills its worker never records its own failure, so without this count a batch that
  crashes the instance every time would loop for ever, one run every 16–18 minutes. Holding the lease means every
  earlier run has ended or its lease has expired, and every run marks its batch at least 60 s before its lease ends. The
  hourly sweep has no lease and keeps the old 1800 s threshold, and counts the same way. Neither zombie reset touches a
  `failed` batch; the sweep's separate revival step does (below);
- takes the **oldest** pending batch (`pending` or `retry_pending`, `order_by created_at`, `:37-44`; the composite
  index in `config/firestore.indexes.json` must be `READY`). With less than 60 s of the run budget left it starts
  nothing: the batch stays pending, the lease is released and a deferral is enqueued (`:261-262`);
- marks it `processing`, runs the agent with `consolidate_full`, the time left on the lease as its timeout and a single
  attempt (no whole-run transient retry, `base_agent.py:560-572`), and on success **deletes the batch** (`_process_one_batch`, `:251-310`);
- **releases the lease** in a `finally` (only the owner that took it can release it). A failed release is logged and
  ignored, since the lease expires anyway; a failed acquire, open-batch check or deferral enqueue raises, so the worker
  answers 5xx and Cloud Tasks retries;
- **an internal error inside the batch** (a store or queue error, not an agent failure) is logged (ERROR, `Unhandled
  error in batch processing`) and swallowed. The chain of next tasks continues only when the broken batch is no longer
  `pending` (it was marked `processing`, so the zombie reset bounds it) **and** another batch waits (`_has_other_pending`,
  `:190-203`). A broken batch that is still `pending` would be the next run's oldest again, an endless chain of immediate
  tasks, so it waits for the hourly sweep instead; an error before any batch was read continues nothing either;
- one batch per task: if more remain, the worker enqueues the next task at once (`worker_handler.py:457-459`; why:
  `decisions/consolidation_one_batch_per_task.md` — Cloud Run throttles CPU after a response, so each batch gets its
  own request). A deferral returns "no more", so a deferred task does not start a second chain;
- the account comes from the user's profile only, never from the user id (owner decision 2026-10-23: facts are personal
  data, and a fallback would write them under the wrong key once an account holds several users). A user with **no
  profile** or with an empty account id cannot be leased: the oldest pending batch is marked `failed` ("user has no
  profile" / "user has no account"), logged as `CONSOLIDATION_FAILED`, no lease is taken, nothing is routed, and there
  is no 5xx or retry loop (`_account_to_lease`, `:176-195`). The missing-profile case is real: the user's agents stay
  cached for about an hour, so `ensure_agents_for_user` can succeed after the profile is gone. The user repository is a
  required argument of `ConsolidationService`, so no wiring can leave it out.

There is **no per-write fence**: nothing checks the lease before each fact write. The run is bounded by its timeout
instead (the agent stops at `lease_until` − 60 s); a commit already on the wire at the moment of cancellation may land
shortly after, which the margin covers. A holder whose lease expired and was taken over logs `release found another
owner` (WARNING) when it releases; a release that finds no lease document at all stays silent, although it can mean
"our lease expired, was taken, and was released" (`firestore_memory_write_lease.py:48-64`).

An hourly sweep (`sweep_consolidation`, Cloud Scheduler at minute 17, `docs/07_deployment/SCHEDULERS.md`) takes every
user with any batch left in the queue (`worker_handler.py:462-484`). For each it first **revives that user's `failed`
batches** (`ConsolidationService.revive_failed_batches` → `reset_recoverable_batches(revive_failed=True)`: back to
`retry_pending`, attempts 0, `revivals` +1) and then enqueues the normal task. `revivals` is a lifetime counter on the
batch document, never reset: a batch is revived while `revivals < 3` (`MAX_FAILED_REVIVALS`, `domain/consolidation.py`),
so one batch costs at most 4 rounds × 3 runs = 12 runs in its life (`MAX_BATCH_ATTEMPTS` = 3 per round; a run is an
agent failure or a lost run, both count; `decisions/consolidation_failed_batch_bounded.md`). Past the cap it stays
`failed` with its messages in the queue document, and the service logs one `CONSOLIDATION_GAVE_UP` (§6, §9.1); a batch
that fails by lost runs logs the same two lines from the queue adapter.

**Runs of one account are serialised by the lease.** Until Facts S2, consolidation tasks ran side by side with no
per-user lock: an overflow during a live run enqueued another task that took the next batch and ran beside it, and two
tasks that started together could take the same batch. Now the second task defers (above). The worst case after a crash
is a wait of up to about 18 minutes, the 16-minute lease plus one 2-minute deferral, if a task arrives to defer (§7);
how many tasks the queue dispatches at once is still set on the queue itself, not in the repo.

### Step 4 — the consolidation run

`ConsolidationAgent.execute` (`consolidation_agent.py:211-287`) needs `account_id` from the request context, loads the
baseline cache (`get_biographical_context_cached`, the whole cache: facts, principles, directives; a failed read is
logged and the run goes on without it, `consolidation_agent.py:246-254`) and dispatches
`consolidate_full` → Stage 1, 2a, 2b (`_execute_deliberate_process_v3`, `:747-844`), then Stage 3 (`_handle_consolidate_full`, `:638-712`). The
agent also accepts `consolidate` (Stage 1–2b only), `consolidate_cluster` and `consolidate_email` (`can_handle`, `:205-209`); the
queue and `$consolidate` always send `consolidate_full`.

- **Model:** agent type `consolidation`, default provider Claude, no cross-provider fallback (`"fallback": None` in `src/services/agent_context_builder.py`; owner ruling 2026-10-23, P9 D8: a memory write never moves to another model on an outage; the declared Gemini fallback was dead before that, the agent never stored its execution context)
  at tier PERFORMANCE (`src/domain/user.py:82`), which on Claude is `claude-opus-5-5` unless the live env var
  `CLAUDE_PERFORMANCE_MODEL` pins another model (`src/adapters/claude_adapter.py:166-167,249-252`; `make claude-model`
  shows the live value). A user's `agent_providers` / `agent_tiers` can override. Thinking `medium`, 64 000 output
  tokens, 15 turns per pass, 15-minute agent timeout (`src/infrastructure/agent_config.py:166-190`); the service passes the run only what is left of its lease (lease end − 60 s − now), so a run ends before its lease does.
- **Prompt:** one system prompt per pass, built by `PromptBuilder.build_for_agent(agent_type="consolidation",
  include_directives=False)`. The profile `prompts_snapshot/profiles/consolidation.yaml` assembles six tokens in order:
  `CONSOLIDATION_TAXONOMY`, `_COGNITIVE_PROCESS`, `_TOOLS`, `_EXAMPLES`, `_POLICIES`, `_OUTPUT_SPEC` (blueprint
  `consolidation_agent_v1`). Then a `knowledge_base {}` block with the baseline facts and, in Stage 1 only, the
  conversation (`src/services/prompt_v3/prompt_assembly_service.py:449-459`). Standing directives are never in this
  prompt as rules: the consolidator curates them as records (Stage 2b).
- **Loop:** `_run_consolidation_loop` (`consolidation_agent.py:920-1021`): each turn sends the whole history with the five tool
  declarations; tool calls of one turn run in parallel (`:1318`); a turn with no tool call is the final report. The loop takes
  the fact-management port as an argument; each run passes its own tracker (below), and the agent's own port is never swapped.

### Step 5 — Stage 1: conversation → facts

The conversation sits in the system prompt as lines `User (Oct 22, 10:00 UTC): …` / `Assistant (…): …`
(`src/services/prompt_v3/context_formatter.py:52-63`); the user message is just "Begin deliberate consolidation."
(`consolidation_agent.py:793`). The token's 8 steps: extract candidates → classify on the four axes → **search
once per candidate** → analyse → decide → execute → verify → report (`CONSOLIDATION_COGNITIVE_PROCESS.groovy`).

| Tool | What the code does (`consolidation_agent.py:1200-1318`, adapter `fact_management_adapter.py`) |
|---|---|
| `search_existing_facts(keywords, primary_query, alternative_query, limit=20)` | `enrich_context` with 6 vector channels (phrase 2 = alternative, else primary), per-channel limits 7/10/10, total `limit`, **no semantic dedup** (`fact_management_adapter.py:85-96`); returns id, text, domain, temporal class, state, priority, tags, metadata, reported date, version. `similarity` is **null** for every vector hit (§4.3) |
| `create_fact(content, fact_attributes)` | requires `domain`, `temporal_class`, `context_priority` (`fact_management_adapter.py:159-167`); state defaults to `current`, priority to `medium`; written **without deduplication** (`skip_deduplication=True`, `fact_management_adapter.py:174-179`) |
| `update_fact(fact_id, updates)` | a content change writes a new version and returns its id; a state or date change edits the document in place; a stale id is followed forward; `superseded_by` is required with state `superseded` (§2.4, `fact_management_adapter.py:218-302`) |
| `merge_facts(fact_ids, merged_content, fact_attributes)` | one transaction: the merged fact (new lineage) is created and every source closed with `superseded_by` (§2.4, `fact_management_adapter.py:347-439`) |
| `count_words(text)` | word count against the 40-word fact limit (`consolidation_agent.py:1286-1297`) |

**Deduplication is the model's decision.** Code does not reject a near-duplicate on this path; the prompt makes the
model search first and DISCARD (no tool call) what is already known. The number-aware 0.96 dedup of
`add_fact_if_unique` (`firestore_repo.py:622-713`) runs only on the legacy v2 path (`prompt_version="v2"`), which is
not live.

**Rules that decide what is stored** (prompt tokens, mirrored in `prompts_snapshot/tokens/system/`):
- `Domain_Scope` (POLICIES): never process ASSISTANT statements as facts unless the USER confirms with new
  information; never store facts from questions.
- `Trivial_Exclusions` (TAXONOMY): no logistics, outbursts, chitchat, meta talk, UI commands, unanswered questions,
  file operations, and the **RAG echo trap** — the assistant recalls a fact and the user only confirms it: discard.
  Test: "relevant in 30+ days?"
- Conflict rules: time series (health, finance, skill) update with history in the text; core identity
  (biographical, preference) needs explicit correction; negations → `update_fact` to `invalidated` ("never true") or
  `archived` ("no longer true").
- `Size_Triggers_Review`: an existing fact over 40 words is split before an update (create the atoms, mark the old one
  `superseded`). The prompt tokens (mirrored in `prompts_snapshot/`) do not yet say that the old fact needs its
  `superseded_by`, so until S3 rewrites them such a `SUPERSEDED` call is refused (§6); measured at about 2 tool errors a month.
- `Directive_Maintenance` (TAXONOMY): `agent_directive` only when the rule is about **how the agent behaves** AND is
  **in force across most requests**; a conditional agent rule becomes `preference` (§4.2).

**The run tracker.** `_execute_deliberate_process_v3` makes one `_TrackingFactManagement` per run (`consolidation_agent.py:47-110`,
`:789`) and passes it to Stage 1, Stage 2a, Stage 2b and the directive backstop as the tools' port. Two passes of one
agent instance can therefore no longer record each other's writes. It records only what the store reports as written:
`changed` holds `(id, text)` for a created fact, a merged fact and a **content** update (the id of the version
written, from the result); `wrote` is set by any successful write, a state-only update included. A `not_found`,
`failed` or refused result records nothing. `changed` seeds Stage 2a; it is not "every id that changed": a tags-only or
metadata-only version has a new id and is not in it.

### Step 6 — Stage 2a: cluster review (only if Stage 1 wrote)

Skipped when Stage 1 wrote nothing (`consolidation_agent.py:809`). "Wrote" means created, merged, or updated with
new `content` (`changed` of the run tracker, snapshotted right after Stage 1, `:800`; the tracker ignores an update
without `content`, `:87-98`), so a Stage 1 that only archives or invalidates facts skips Stage 2a. The cluster holds the
**current** documents the search returns, so a fact Stage 1 versioned appears with its new id and text. For each written fact it searches the store (words longer than 3
characters among the first 10 as keywords, the text as phrase, limit 20), unions the hits by id, sorts by length and keeps 30
(`_build_review_cluster`, `:1032-1078`). A fresh loop gets the "FACT CLUSTER REVIEW" message: merge repeats, decompose
compounds, fix inconsistencies, no fact over 40 words, keep numbers and dates (`_build_cluster_message`, `:1171-1198`). It sees no conversation.

### Step 7 — Stage 2b: the directive rulebook (every run)

Unconditional, even when Stage 1 did nothing (`consolidation_agent.py:836`); a no-op only when the rulebook is
empty. It reads up to 40 current non-archival `agent_directive` facts (`_review_directives`, `:846-893`) and sends the whole rulebook with the "STANDING DIRECTIVES REVIEW" message
(`_build_directive_review_message`, `:1081-1168`): one checklist line per record (`scope_ok`, `reminder_dup`, `unusable`), then per record DEMOTE
(create a `preference` fact stating the condition + invalidate the directive), INVALIDATE, MERGE, or rewrite to the
target form (imperative, English, terse, one rule), hard cap 15. Then the **code backstop** `_enforce_directive_cap`
(`:895-918`, through the run's tracker): if more than 15 current directives remain, the ones after the 15th in (priority, newest first) order are
invalidated. Run-to-run results differ on identical input (`decisions/directive_applicability_gate.md` § Variance).

### Step 8 — Stage 3: indexed emails

Runs only after Stage 1–2 succeeded (`consolidation_agent.py:638-712`). **Best-effort:** it runs under what is left of
the run's own budget (the message timeout, which the service derives from the lease, at most 15 minutes) minus a 30 s margin (`_stage3_budget_s`, `consolidation_agent.py:714-723`, `_STAGE3_MARGIN_S`); with under 30 s left it is
skipped with an INFO line. A Stage 3 error or its timeout is logged as one ERROR line (`Stage 3 (email) failed` /
`timed out`) and the batch still succeeds: Stage 1 does not re-run, and the emails stay unconsolidated for the next run.
Cancellation is not caught. Up to 100 unconsolidated `IndexedEmail` facts (1 pass,
`agent_config.py:186-187`) go to a fresh loop as a numbered candidate list under a "[system_alert] … scanned the
user's email inbox" message; afterwards all of them are marked consolidated, whatever the model did (`consolidation_agent.py:516-626`).
Skipped when the repository is not wired or nothing is pending.

### Step 9 — the baseline is rebuilt; the batch is deleted

After Stage 1–2b, `refresh_biographical_context_cache` rebuilds the `{prefix}user_context` document
(`consolidation_agent.py:346-352`, `firestore_repo.py:715-779`); Stage 3 rebuilds it again when it processed emails.
The refresh logs and swallows its own errors. `ConsolidationService` then deletes the batch (§3 step 3). The Cabinet
invalidate (§5.2) calls the same refresh.

**A run that fails after it wrote refreshes too.** Stages 1–2b run inside a `try/finally`: if the run did not finish and
its tracker saw a write (`wrote`), the `finally` rebuilds the cache (`consolidation_agent.py:789-844`); Stage 3 does the
same when a pass wrote and it did not reach its own refresh (`:559-617`). Before, a failed run left the cache with the
replaced version's id and text next to the new version, until the next consolidation. A run that failed without writing
does not refresh. The same `finally` also runs when the run is cancelled by its timeout, which is close to the lease
margin: that is a cache rebuild (one read, one cache-document write), not a fact write, so it does not break the
one-writer rule. The retried batch can add one more version of a fact (§2.4).

### Step 10 — the next conversation

Smart reads the cache document on every request (no in-process copy, `smart_response_agent.py:425-435`), so the new
baseline applies from the next message after the run. A new fact is searchable by `search_memory` from the moment it
is written. Whether it is in the baseline depends on §4.1.

### 3.11 Loop alerts and stats lines

`ConsolidationPassStats` (`src/domain/consolidation_pass_stats.py`) counts each pass's tool calls (no arguments, no
fact text, 8 characters of the user id):
- 🚨 permanent: a pass that uses all 15 turns without a final report logs an ERROR and posts to the ops sink (`:108-115`,
  `consolidation_agent.py:1017-1021`). The pass returns no operations; the writes it made stay.
- 📊 temporary: while `CONSOLIDATION_STATS_ALERTS` is on (`os.getenv`, default on), every pass that ends with a report
  posts one line: pass label (`stage1`, `stage2a`, `stage2b`, `email`, `cluster`), model, effort, turns, tool counts,
  repeated turns, writes, report ops, seconds, per-turn sequence (`:98-106`).

Why: `decisions/sonnet55_adaptive_thinking_stall.md`.

---

## 4. Reading memory

### 4.1 The baseline: what the bot sees in every message

**Who gets it.** Every agent whose prompt is built with the biographical cache: Smart and Quick (merged with the
Router's prefetch), Notes, Tasks, EmailSearch, PdfGenerator, the consolidator. Lelik gets a filtered copy (§4.6). Every
other agent builds without it (`include_biographical=False`): the Router, MemorySearch, web, maps, compute,
DocPlanner/DocGenerator, HtmlPageGenerator, image and video generation, EmailClassification, the companion agents and
the drive agents; DeepResearch passes no account id, so it gets none either (`deep_research_agent.py:91-93`).

**How it is chosen** (`BiographicalContextService.refresh_context`, `biographical_context_service.py:73-220`):
1. Limits: `biographical_cache_limit` (default **65**) and `principles_cache_limit` (default **20**)
   (`src/domain/settings.py:49-50`). The consolidator passes its own limits, resolved per user when its agents are
   built (user profile → account defaults → system default, `src/composition/user_agent_factory.py:364-371`), and an
   explicit limit wins over the service's own account-level resolution (`biographical_context_service.py:109-138`,
   `firestore_repo.py:749-755`). The consolidator passes those limits, so the user-level settings apply to its refresh. The Cabinet invalidate (§5.2)
   also refreshes, with **no** explicit limits: the service then resolves account defaults → system default, so a
   user-level override is not applied to that rebuild. An account with no `account_defaults` resolves to the system
   limits (the fallback is `UserBotConfig()`; it used to be `SearchConfig()`, which made the resolution raise and the
   refresh silently never write).
2. Up to 65 current, non-archival `biographical` facts, ordered by priority rank then newest first
   (`get_active_facts_ordered`, `firestore_repo.py:452-507`).
3. If fewer than 65, fill from the 65 top facts of all domains in the same order, minus those already taken and minus
   directives (`biographical_context_service.py:162-171`). So other domains enter only when biographical facts leave room.
4. Facts tagged `mindset` become **principles** (up to 20; extras are dropped, not moved to facts); the rest are facts
   (`biographical_context_service.py:200-206`). Principles are carved out of the 65, not added to them
   (`biographical_context_service.py:162-206`).
5. Directives are fetched separately: up to 15 current non-archival `agent_directive` facts (`biographical_context_service.py:156-160`).
6. Each cached item carries only id, text, domain, tags, priority, created_at (`biographical_context_service.py:190-198`).

**At most**, an orchestrator prompt carries 65 facts and principles (of which ≤ 20 principles) + ≤ 15 directives + the
Router's prefetch of ≤ 30 facts minus those already in the baseline.

**How it is rendered.** `PromptBuilder.build_for_agent` splits the list (`src/services/prompt_builder.py:183-202`):
directives → `standing_directives {}` (§4.2); items tagged `semantic_lens` (the Router's prefetch) → `query_specific_context`
after the cache boundary; the rest → `knowledge_base { biographical_context }`, grouped by domain, biographical first
(oldest → newest), other domains alphabetically (newest → oldest), each line with its recorded date, a `preference`
principle prefixed `[MINDSET]` (`src/services/prompt_v3/biographical_formatter.py:56-180`),
under the note "Dates = when recorded … conversation history takes precedence if contradictory"
(`prompt_assembly_service.py:452-454`). Facts and directives pass the security validator as untrusted text.

**When it changes:** at the end of a consolidation run (§3 step 9) and right after a Cabinet invalidate (§5.2).

**When the read fails.** A missing or empty cache document reads as no facts. A backend failure is logged and
re-raised (`firestore_repo.py:851-853`): Smart, Quick, the Router and `PromptBuilder` catch it and answer without the
baseline (`smart_response_agent.py:435`, `quick_response_agent.py:187`, `router_agent.py:319-328`,
`prompt_builder.py:158-162`); Lelik fails the call closed (§4.6); the consolidator runs without it (§3 step 4).

### 4.2 Standing directives

`agent_directive` facts are the owner's standing orders to the agent, rendered verbatim as the last static block,
`standing_directives {}`, newest first (`biographical_formatter.py:87-98`, `prompt_assembly_service.py:477-483`).
Every agent that gets the baseline gets them, unless it passes `include_directives=False` (the consolidator, Lelik and
his summarizer, Tutor and its extractor, WebSearchLight, the drive model agents). Bounds: the rulebook is curated to 15
by Stage 2b and its backstop; the cache holds at most 15 (`DEFAULT_DIRECTIVES_CACHE_LIMIT`, `settings.py:55`).

**Why a directive disappeared**, in order of likelihood: Stage 2b demoted it to `preference` (a new fact exists, the
directive is `invalidated`); Stage 2b invalidated it (reminder duplicate, unusable, superseded); Stage 2b merged it (a
new fact with a new id; the sources are `superseded`); the backstop cut it (over 15, lowest priority, then oldest);
its priority is `archival`; Stage 1 archived or invalidated it after the owner said to stop (a negation, §3 step 5);
the owner invalidated it in the Cabinet. The 📊 `stage2b` line of that run shows the writes; BigQuery shows the
checklist (§9.4).

A directive invalidated in the Cabinet leaves `standing_directives` from the next message: the invalidate rebuilds the
cache at once (§5.2).

### 4.3 The search engine: `enrich_context`

`SearchEnrichmentService.enrich_context` (`search_enrichment_service.py:42-249`):

1. Embeds the non-empty inputs in one batch with `RETRIEVAL_QUERY` (`:113-129`).
2. Runs up to 7 queries in parallel (`:131-179`), each restricted to `account_id` and `state == "current"`:

| Channel | Input | Field | Limit (defaults) |
|---|---|---|---|
| `domain_direct` | `relevant_domains` (1–3) | Firestore `IN` on `domain`, newest first | keyword limit (10) |
| `keyword_tags` | keywords joined | `tags_vector` | 10 |
| `keyword_metadata` | keywords joined | `metadata_vector` | 7 |
| `phrase1_text` | phrase 1 | `vector` | 10 |
| `phrase1_tags` | phrase 1 | `tags_vector` | 7 |
| `phrase2_text` | phrase 2 | `vector` | 7 |
| `phrase2_metadata` | phrase 2 | `metadata_vector` | 10 |

   Defaults from `SearchConfig` (`settings.py:37-40`): 10 / 10 / 10, total 30 (`semantic_search_limit` overrides the
   total per user or account). A failed channel counts as empty.
3. **RRF**: score = Σ 1/(60 + rank) over the channels a fact appears in; sorted by score (`src/domain/rrf.py:21-44`).
   A fact found by several channels beats a fact ranked first by one.
4. **Semantic dedup** (unless skipped): walking the ranked list, a fact is dropped as a duplicate of one already kept
   when cosine ≥ 0.96 and both hold the same numbers (sorted), and either cosine ≥ the strict threshold (0.98 by
   default) or the kept text is at least 85 % of its length (`src/domain/deduplication_service.py:117-144`). Different
   numbers are never duplicates; a clearly longer, more detailed text is kept.
5. Cut to the total limit; drop facts whose id is in the `biographical_facts` passed in (`search_enrichment_service.py:214-236`).

Scores: Firestore returns no distance in this projection, so `relevance_score` is `None` for every vector hit (only
the domain channel sets 1.0, `search_enrichment_service.py:397,452`). Ranking uses ranks, not scores. A process-wide semaphore caps parallel
`find_nearest` calls at 30 (`firestore_repo.py:50`).

### 4.4 The Router's prefetch (every message)

The Router's triage returns `search_intent`, `semantic_lens` (keywords), `search_phrase` and `relevant_domains`. Unless
`search_intent` is `none`, it calls `enrich_context(keywords, phrase 1, phrase 2 = "", domains, biographical_facts =
the baseline)` (`src/agents/core/router_agent.py:310-338`): at most 4 vector channels + the domain channel, deduplicated,
up to 30 facts, minus those already in the baseline. Smart and Quick receive them as `query_specific_context`
(`prompt_builder.py:71-110`). This is the one search per message every orchestrator turn shares; Smart's own
`search_memory` calls are extra.

### 4.5 `search_memory`

Smart delegates `search_memory` (SYNC) to `FactsMemoryAgent` (`src/agents/memory_search_agent.py`):
1. One Gemini ECO call (`gemini-3.5-flash-lite`, provider fixed to Gemini, `agent_context_builder.py:78-83`) turns
   the request into `keywords`, `primary_query`, `alternative_query`, optional `domains` (JSON, token
   `MEMORYSEARCH_*`) (`memory_search_agent.py:108-153`). If that call fails, the agent falls back to one
   `vector` search with the raw query (10 results, principles dropped, `memory_search_agent.py:295-352`).
2. `enrich_context` with all channels and default limits, dedup on, **no baseline exclusion** (`memory_search_agent.py:241-247`).
3. Each fact rendered as its text plus `context:`, `reported:` and `metadata:` lines, joined by `---` (`memory_search_agent.py:354-367`).

### 4.6 Lelik

`LelikPersonaService.assemble` (`src/services/lelik_persona_service.py:46-64`) takes the baseline cache and keeps only
the domains in `UserBotConfig.voice_fact_domains` (default `biographical`, `preference`); no standing directives
(`lelik_agent.py:105`). Anything else he reaches through `search_memory` (in his allowlist) or by asking Alek. A failed
cache read raises, and the call is not opened on an empty context (`lelik_persona_service.py:46-48`).

### 4.7 The claude.ai connector (MCP)

`get_user_context(query, alternate_phrasing, keywords)` (`src/composition/mcp_setup.py:255-332`) calls
`enrich_context` directly (no model, no Router) with phrase 2 = alternate phrasing or the query, dedup at 0.98, total
30 (`main.py`, the MCP `SearchEnrichmentService`), as the token's account. The answer groups facts by domain, one line each (`mcp_setup.py:128-144`).

### 4.8 The Cabinet fact page

Read routes (`src/web/user_cabinet_app.py:767-848`): all current facts; a paginated browser, newest first, optional
domain filter; a single-vector search over `vector` (50 results). They show text, domain, type and created date.
The page holds fact ids, and a version write changes the id. A stale id still works for a delete (§5.2).
**Pagination tie.** The browser pages by a cursor on `created_at`, ties broken by document id
(`firestore_repo.py:266-314`). A version copies `created_at` from the document it replaces, so a fact that was versioned
between two page loads can show twice, or be missed once, at a page boundary; reload the list.

---

## 5. Writing memory outside consolidation

### 5.1 `propose_memory_facts` and `$memory save`

The bot never writes facts. The consolidator reads every user message as the owner's own words, so a bot-written save
let procedures and file notes become facts and directives (`decisions/memory_facts_owner_confirmed.md`).

1. On interactive turns only, Smart has the local tool `propose_memory_facts` (`smart_response_agent.py:508-517`).
   Up to 20 facts, each one line of at most 300 characters plus a source (`src/domain/memory_fact.py:9-51`). Each
   text passes the security validator; a flagged fact rejects the proposal (`src/services/memory_fact_service.py:79-92`).
2. The draft is stored under a 4-hex-character code for 7 days with the session it came from (`:30-41`,
   `firestore_memory_fact_draft_repository.py:19`). The owner gets the facts as `memory-facts.txt` and a separate
   `$memory save <code>` message (`conversation_handler.py:392-415`).
3. Pasting the command (`_handle_memory_command`, `conversation_handler.py:1838+`) runs `MemoryFactService.confirm`
   (`memory_fact_service.py:43-77`): it appends a **user/model pair** to the origin session — the user part
   "[System: the owner reviewed and confirmed these facts … read from: <sources>.]" plus one line per fact
   (`memory_fact.py:66-80`) — and only then deletes the draft. A failed append keeps the draft for a retry.
4. From there the pair is an ordinary pair of messages: it becomes facts at the channel's next overflow (§7). Until
   then `search_memory` cannot find them; Smart sees the pair only while it is among the last 30 messages.

### 5.2 Cabinet invalidate

`POST /api/user/facts/<id>/invalidate` (`user_cabinet_app.py:850-871`) → `invalidate_fact` (`firestore_repo.py:316-343`):
the id must exist (else 404) and its stored account must be the caller's (else 403). Then the repository
**follows the id forward** to the fact that is live now (`CABINET_ACCEPTS`: every state except `superseded`, so the owner
can still delete a fact the consolidator archived meanwhile) and sets `state=invalidated`, `valid_to` and `last_updated`
on that document, in place, under the `update_time` precondition. A page loaded before the consolidator versioned the
fact therefore deletes the **current version**, not a closed document (before S2 it would have re-opened the old
document and left two `is_current` documents in one lineage), **but only when that version still has the text the
page showed**: a closed version keeps its own text, so the repository compares it with the text of the id the page
sent. On a conflict it resolves once more, and the text check runs again.
Search stops returning the fact at once. **The route then rebuilds the baseline** with
`refresh_biographical_context_cache(owner_id=account_id)` (§3 step 9), so the fact leaves the next prompt too. That
method never raises: it logs `❌ [FirestoreRepo] Failed to refresh biographical cache for <account>: …` (ERROR) and
returns, the invalidate still answers 200, and the baseline keeps the fact until the next consolidation run. (The route
once had its own `Fact <id> invalidated but the biographical cache refresh failed` guard; it could never fire and is
gone, so grep the repository's line.) The route's own log line (`🗑️ Fact <id> invalidated by user …`) names the id the
page sent, not the version that was invalidated.

**When the fact was rewritten meanwhile (owner decision 2026-10-23).** If the consolidator rewrote the fact after the
page loaded (a content change, so the id moved on) and the new version's text differs from the row the owner saw, the
delete is **refused** with a 409 (`FactResolutionError` "fact X was rewritten after the page loaded; reload",
`firestore_repo.py:327-333`) and nothing is written; the page reloads and the owner sees the new text before deciding
again. This covers the row that was the subject of the owner's own correction (the Cabinet's "Edit" button, modal
"Correct Fact", which drafts a correction request for the chat) that has just been applied: the correction is not
deleted unseen. A newer version with the **same** text (tags, metadata or temporal class changed) is deleted at once.
Until this decision (S2 as built) the delete landed on the new version.

Failures: a fact that was **merged** into another lineage, a closed document with no successor (any document closed
before S2), a chain longer than 50 hops, a rewritten text (above), or a second conflict is **409** (`FactResolutionError` / `FactWriteConflict`,
caught before the `ValueError` → 404 branch, `user_cabinet_app.py:863-866`). The JSON carries the technical message
(for example "fact X was merged into Y; search again") and the route logs it as a WARNING
(`Fact <id> not invalidated: …`); the repository logs each retry as `[FactRepo] invalidate of <id> conflicted (attempt
n)` (`firestore_repo.py:333`). The Cabinet page does not show that message: on a 409 it shows "This fact changed in
the meantime; the list was reloaded." and reloads the list (`cabinet.html:3526-3531`). A failed invalidate (404 / 403 /
409) does not touch the cache. Cabinet invalidate does not take the memory-write lease: it writes one document under a
precondition.

**What to do after that message.** Usually a consolidation run merged this fact into another one meanwhile (a list
loaded after the S2 switch never holds a closed pre-S2 document, so that is not the cause), or it was written twice
while you clicked. Look for the merged fact in the reloaded list and decide there; deleting it deletes the merged text as
a whole.

### 5.3 The directive cap backstop

§3 step 7. It writes through the same `update_fact`, on the run's tracker (state only, so it edits the document in
place: same id, no version, no re-embed).

### 5.4 `$consolidate`

`ConversationHandler` (`conversation_handler.py:1526-1618`) cuts the oldest `batch_size` messages of the current
session (the user's `consolidation_batch_size` override, else 50), queues them, saves the trimmed session, posts a
"started" note, then calls `ConsolidationService.consolidate_now` (`consolidation_service.py:123-139`, handed over by
the adapter factories as `overflow_callback`). It processes **at most one batch** inside the request, because the chat
request is a Cloud Task with an 1800 s deadline and two batches at the 16-minute lease bound would exceed it. That is
the **oldest** pending batch of the user, which after a backlog is not necessarily this command's. If more remain,
`consolidate_now` enqueues the next consolidation task, as the worker does. It uses its own copy of the serializer
(same rules as §3 step 3).

The reply is honest about what happened (`_consolidate_outcome`, `conversation_handler.py:1469-1491`). It never says "N
messages were processed". It says the command's messages were queued and then one of: "N batch(es) consolidated now";
"no batch was consolidated in this request" (nothing was pending to run); "A batch failed and stays queued; it will be
retried later" (an agent failure, an internal error or a setup error such as a missing account, `ConsolidationRun.failed`; check the `[Librarian]
Batch … failed` and `Unhandled error in batch processing` lines); "queued behind another memory run in progress, it
continues in the background" (the lease was taken, §3 step 3); plus "The rest continues in the background" when more
batches wait. The line reaches the chat through `notify()`: the formatter agent rephrases it in your language and it is
saved in the session history (§9.3); the phrases above are the facts it carries. When your messages will be in memory: your
batch runs after the run now holding the lease and after every older pending batch of yours, one per task, each a few
minutes (§7). With no
task queue (a local run) it says instead that nothing runs in the background and the owner should send `$consolidate`
again. If `consolidate_now` raises (lease acquire, deferral or continuation enqueue), the owner gets "Memory consolidation
stopped with an error. The command's messages are queued; a later run picks up whatever is left" and the error is logged
(`[Librarian] $consolidate failed`); the batch is already in the queue and the hourly sweep finds it. Without a wired
service the command only enqueues.

---

## 6. Outcomes: tool calls and failures

| Where | What happens | Facts | Batch | Retried? |
|---|---|---|---|---|
| tool call ok | result JSON back to the model | written at once | — | — |
| `search_existing_facts` fails (embedding, Firestore) | the adapter catches everything and returns `[]` (`fact_management_adapter.py:136-138`), which the model reads as "no match" | a duplicate of an existing fact may be created | — | no |
| a tool raises past the adapter | `{"error": …}` returned to the model (`consolidation_agent.py:1307-1312`); rarely reached, since create, update and merge catch their own errors and return `status: failed` | nothing of that call | — | the model may retry in a later turn |
| `create_fact` missing a required attribute | `status: failed`, message names the fields (`fact_management_adapter.py:159-167`) | none | — | the model may retry |
| bad value (an enum such as `domain`, `state`, `temporal_class`, `context_priority` e.g. `HISTORICAL`; a malformed date, `tags: null` or `[]`, empty `content`; `state=superseded` on a create or merge; `superseded_by` missing, or given without `superseded`) | checked **before** any embedding or write; the `ValueError` is caught → `status: failed` with the message (`fact_management_adapter.py:192-198,244-250,372-374`; `new_fact_attributes.py:35-78`, `fact_update.py:24-53`). One bad fact fails a whole legacy `add_facts_batch` call | none for that call | — | the model may retry |
| `update_fact` on an unknown or foreign id | `status: not_found` (`fact_management_adapter.py:262-265`), the same words for both | none | — | — |
| `update_fact` or `merge_facts` with an id the store has moved on from (versioned) | the id is followed forward and the call lands on the live document; the result's `fact_id` is the id of the document written, and an update also carries `resolved_from` (the id sent) and a message saying it was applied to the current version (§2.4) | written on the live version | — | — |
| `update_fact` or `merge_facts` with an id that cannot be followed to a `current` fact | `status: failed`: "was merged into <id>; search again", "was superseded and its successor is unknown" (every document closed before S2), "is archived/invalidated/stale", "more than 50 forward hops", or "cannot be resolved" (the chain left the account; no foreign id named) (`fact_lifecycle.py:41-70`) | none | — | the model searches again |
| `update_fact` or `merge_facts` and the document changed under the write | resolved and written once more; a second conflict is `status: failed`, "changed concurrently; search again" (log `changed concurrently twice`). A content or tags update also fails the same way when the second read lands on a **different** document (log `moved forward between attempts; update refused`): those fields replace the whole value. A merge whose source moved forward between the two attempts fails "source changed; search again" | none | — | only if the model searches again and redoes it **in the same pass**: the run still succeeds, the batch is deleted and the tracker records nothing, so a change the model drops is gone for good |
| `update_fact` with `state=superseded` and a missing or foreign successor | `status: failed`, "successor fact not found" (nothing named), or the successor's own resolution error | none | — | the model may retry |
| `merge_facts` with no source supersedable | `status: failed`, "Failed to supersede any facts" (`fact_management_adapter.py:406-408`), with no embedding call | none | — | — |
| `merge_facts` fails (bad value, embedding error, conflict, a source that ends elsewhere) | `status: failed`. The merged fact and the closing of the sources are one transaction (§2.4), so nothing is half done | none; **every source stays live** | — | the model may retry |
| final report not JSON | parsed loosely, else `[]` (`consolidation_agent.py:1459-1513`) | writes stand | succeeds | no (the report is cosmetic) |
| a pass hits 15 turns | 🚨, pass returns `[]` (`consolidation_agent.py:1017-1021`) | writes so far stand; candidates not yet written are lost | **succeeds and is deleted** | no |
| Stage 1/2a/2b raises (provider down after retries, prompt build fails, timeout of 15 min) | `AgentResponse.failure` | writes so far stand | attempts +1 → `retry_pending`; the worker re-enqueues at once; at 3 → `failed` + `CONSOLIDATION_FAILED` log (`consolidation_service.py:298-333`); a lost run (below) counts in the same 3 | yes, the whole batch; Stage 1 runs again over facts it already wrote (the model should find and discard them); an update it repeats on the live version can add one more version document. The cache is rebuilt after a failed run that wrote (§3 step 9) |
| Stage 3 raises, or runs past its share of the budget | caught in `_handle_consolidate_full`: one ERROR line (`Stage 3 (email) failed` / `timed out`); with under 30 s of budget left it is skipped with an INFO line (`consolidation_agent.py:658-698`) | Stage 1–2b writes stand | **succeeds and is deleted** | no: the emails stay unconsolidated and the next run picks them up |
| batch `failed` (3 attempts or lost runs) | the per-task path never revives it (it only resets zombies), so a newer batch is taken next (`get_pending_batches` selects `pending` / `retry_pending` only). The hourly sweep revives it: `retry_pending`, attempts 0, `revivals` +1, at most 3 revivals (`firestore_consolidation_queue.py:111-202`). If the round after the 3rd revival fails too, the service (or the queue, for lost runs) logs one `❌ [Librarian] CONSOLIDATION_GAVE_UP batch_id=… user_id=<8> revivals=3 messages=n` and the batch stays `failed` in the queue with its messages; nothing revives it again (the owner decides) | each run pays for the stages that ran before the failure; at most 12 runs in a batch's life (4 rounds × 3) | bounded: 12 runs, then given up | yes, up to the cap |
| worker crashes mid-batch | batch stays `processing`, the lease is not released, and **no cache refresh happens** (the `finally` of §3 step 9 does not run when the instance dies, so the baseline keeps the replaced version's id and text until the retried run ends) | partial writes stand | the next tasks of that account defer every 120 s (§3 step 3) until the lease expires (at most 16 min after it was taken); the winner then resets the batch (older than 60 s) to `retry_pending` with attempts + 1 and runs it. Without a lease (the sweep) the threshold is 1800 s. A batch that kills its worker three times is `failed` (then the sweep's bounded revivals; 12 runs in all) instead of looping | yes, but only if a task arrives to defer: Cloud Tasks retries the killed request (the queue's retry settings), a new overflow comes, or the hourly sweep |
| internal error inside a batch (a store or queue error, not an agent failure) | logged `Unhandled error in batch processing` and swallowed; `$consolidate` reports "A batch failed and stays queued" | writes so far stand | stays as it was (`pending`, or `processing` if the error came after the mark: then a zombie later) | the next task continues the chain only if the broken batch is no longer `pending` and another batch waits; otherwise the hourly sweep. **Known residual:** an error after the agent has finished (for example a failed `delete_batch`) leaves the batch `processing`, so it re-runs as a zombie (attempts + 1) although its facts are already written |
| baseline refresh fails | logged, swallowed (`firestore_repo.py:775-779`) | stored | succeeds | old baseline until the next run |
| enqueue of the batch fails at overflow | logged `Error in overflow routing` (`overflow_routing_service.py:64-69`) | — | **lost**: the messages were already cut from the session | no |
| batch stored but the Cloud Task enqueue fails | logged | — | waits in the queue | the hourly sweep |
| two runs of one account at once | prevented by the memory-write lease (§3 step 3): the second finds it taken and defers itself (a named task, 120 s later) | none from the loser | the loser does nothing; the winner owns the batch | yes, by the deferral |
| lease taken and the user has no open batch | the loser stops; nothing to do | — | — | — |
| less than 60 s of the run budget left when the batch would start | the batch stays `pending`, the lease is released, a deferral is enqueued (`consolidation_service.py:261-262`) | none | untouched | yes |
| lease acquire, open-batch check or deferral enqueue fails | the error propagates, the worker answers 5xx | none | untouched | yes, Cloud Tasks retries |
| lease release fails | WARNING `releasing the memory-write lease failed`; the lease expires on its own | stored | succeeds | — |
| user has no profile, or no account id | the oldest pending batch is marked `failed` ("user has no profile" / "user has no account"), `CONSOLIDATION_FAILED` is logged, no lease, nothing routed or written (`consolidation_service.py:176-195`, `:341-345`) | none | `failed` | no 5xx loop; the sweep's bounded revivals fail it again at no model cost |
| provider failover | none for consolidation: a Claude outage fails the pass and the batch is retried later (owner ruling 2026-10-23; the transcript is locked to one provider after the first tool call anyway, `src/agents/CLAUDE.md` → Transcript integrity) | — | — | same-provider retries |

---

## 7. Timelines

**A fact told today.**
1. *Seconds:* it is in the session. Smart sees it while it is among the last 30 messages **the session still holds**
   (`agent_config.py:104`), and Lelik if it is the primary channel. The session holds 21–70 messages: an overflow cuts
   the oldest 50 of 71 (`firestore_session_store.py:223-231`), so a recent message can leave Smart's history minutes
   before its batch is consolidated.
2. *Gap:* once it is out of Smart's history (30 newer messages, or cut by an overflow) and not yet in memory, nothing
   finds it.
3. *Consolidated:* at the channel's next overflow after which it is among the oldest 50 messages. With threshold 70 /
   batch 50, a message waits for roughly 20–70 more messages in the same channel (10–35 exchanges; notifications
   count). A quiet channel may wait days; one that stays under 70 never consolidates on its own (`$consolidate`).
4. *Minutes later:* the Cloud Task runs; Stage 1 writes the fact. From then `search_memory` and the Router prefetch can
   find it.
5. *End of the run* (all stages, typically a few minutes): the baseline is rebuilt. It enters the baseline only if it
   is selected (§4.1): a `biographical` fact with room under 65, or another domain when biographical facts leave room,
   ordered by priority then age. Otherwise it is reached by search only.

**A correction** ("I moved to Lisbon").
- Before consolidation: the baseline still says the old city; the bot sees the correction only in recent history, and
  the baseline note says history wins on conflict.
- At consolidation: the consolidator searches, finds the old fact and either updates it (a new version document: the new
  text, version +1, vectors recomputed; the old document is closed and keeps the old wording) or archives it and creates a
  new one. A stable identity fact needs an explicit
  correction (`Core_Identity_Caution`).
- After the run: search returns the new text at once; the baseline shows it from the next message.

**A Cabinet invalidate.** (On a page loaded before the fact was versioned, it deletes the current version when its text is unchanged, and answers 409 with a reload when it changed, §5.2.) Search stops returning the fact immediately, and the route rebuilds the baseline cache in the
same request, so the next message no longer has it (if the rebuild fails, the baseline keeps it until the next
consolidation run).

**A consolidation task that finds the account busy.**
1. *Minute 0:* an overflow enqueues a `consolidation` task while another run holds the account's lease (taken at most
   16 minutes earlier, normally a few minutes).
2. *Seconds later:* the task finds the lease taken, sees an open batch, enqueues a deferral for 120 s later and ends.
   Tasks that lose in the same two minutes share that one deferral.
3. *Every 2 minutes after that:* the deferral tries again; the first one that finds the lease free takes it and runs the
   oldest batch. The worker then enqueues the next task at once while batches remain.
4. *After a crash:* the holder's lease stays until it expires (at most 16 minutes after it was taken). The killed task is
   retried by Cloud Tasks within seconds to minutes, and that retry defers; if no retry comes, the hourly sweep or the
   channel's next overflow starts the chain (up to about an hour). Deferrals then keep trying every 2 minutes; the first
   after the expiry resets the stuck `processing` batch (older than 60 s) with its attempts + 1 and runs it. So a crashed
   run delays its account's memory by up to about 18 minutes once a task arrives. Nothing is lost and no two runs overlap,
   but the batch is run again from its start (Stage 1 repeats, §6). The baseline is not rebuilt after a crash, only by
   the retried run. A batch that crashes the worker three times is marked `failed`, and the hourly sweep's bounded
   revivals take over (12 runs in all, §3 step 3).

**A directive** ("always answer in bullet points").
- Consolidated with its batch (above). Stage 1 classifies it: `agent_directive` only if it is about agent behaviour
  and holds for most requests; otherwise a conditional `preference` reached by search.
- Stage 2b of the same run already sees it (it reads the rulebook after Stage 1) and may rewrite, merge or demote it.
- It is in `standing_directives` from the next message after the run.

---

## 8. Costs

**Which model.** Claude PERFORMANCE: `claude-opus-5-5` by code default, or the model pinned by the live
`CLAUDE_PERFORMANCE_MODEL` (`make claude-model`; `make claude-forward` pins `claude-sonnet-5-5`, `make claude-rollback`
`claude-sonnet-5`). The records conflict: `src/adapters/CLAUDE.md` says `cloudbuild-dev.yaml` carries no pin since
2026-10-15, so a new revision returns to Opus 5.5; `decisions/sonnet55_adaptive_thinking_stall.md` (2026-10-20) says
consolidation stays on Sonnet 5.5. Live `prompt_content`, read 2026-10-22 for the last 7 days: consolidation ran on
Opus 5.5 (71 calls, 2026-10-15 to 10-21) and on Sonnet 5.5 (22 calls, 2026-10-20 to 10-22). Check `make claude-model`
before reasoning about cost or behaviour.

**What one batch costs.** Measured only on the A/B harness (`scripts/consolidation/ab_cross_provider.py`, one real
window, 2 runs each): Sonnet 5.5 $0.17–0.26, Sonnet 5 $0.27–0.39 (`decisions/model_refresh_2026_10.md`). An earlier
eval (2026-07-15, older models and prices) put Sonnet ≈ $0.5 and Opus ≈ $1.5 per run
(`decisions/consolidation_provider_model_eval.md`). Opus 5.5 on consolidation: not measured.

**What drives it.**
- Turns × prompt: every turn resends the system prompt (six tokens, the baseline, in Stage 1 the 50-message
  conversation) and the growing tool history; Claude caches the static part.
- Passes: Stage 1 and Stage 2b (when any directive exists) run on every batch, Stage 2a when Stage 1 wrote, Stage 3 when emails are pending. A
  batch with nothing new still pays for two loops.
- Thinking at `medium` on every turn; retries re-run whole batches (§6).
- Frequency: one batch per ~50 messages per channel.

**Where it is recorded.** Every model call goes through `BaseAgent._call_llm`, which bills the account per model that
ran (`TokenLedger`, root `CLAUDE.md` → Token accounting) and writes a `prompt_content` row per turn (§9.1). The 📊 line
carries no cost.

**Embeddings.** One batch call of 3 texts per create, merge or content update; one of up to 3 texts per search
(consolidator searches, Router prefetch, `search_memory`, MCP). Embedding calls are not billed to the account and not
measured. `search_memory` adds one Gemini flash-lite call per search.

---

## 9. Debugging and operations

### 9.1 Where to look

| Need | Source |
|---|---|
| Did overflow happen, was a batch queued, consumed, retried, failed | Cloud Logging (`make fetch-logs`, grep the lines below) |
| What the consolidator saw and answered, turn by turn | BigQuery `alek_observability_dev.prompt_content`, `agent_type = "consolidation"` |
| The run as a tree, failed calls included | Logfire (a failed call has no BigQuery row) |
| What a pass did, at a glance | the 📊 / 🚨 Slack lines (§3.11) |

Log lines, in pipeline order:
- `❄️ Overflow batch #n for <session>… (extracted=50, remaining=…)`, `🔔 Triggering overflow callback` (session store)
- `📦 [Overflow] Created batch <batch_id>`, `📬 [Overflow] Consolidation task enqueued`; loss: `❌ Error in overflow routing`
- `♻️ Reset N stale PROCESSING batches (attempts +1)` (a lost run was counted; the third one logs `CONSOLIDATION_FAILED … error='run lost while processing'`, and `CONSOLIDATION_GAVE_UP` past the revivals) / `♻️ Revived N FAILED batches` (sweep only) / `⏳ Skipped N live PROCESSING batches`
- `[MemoryWriteLease] busy for account <8>` (INFO, a task found the lease taken), `⏳ [Librarian] lease busy: deferred consolidation for user <8>` or `… less than the lease margin left: deferred …` (INFO, one named deferral task enqueued), `[Librarian] lease busy, no queue to defer to` (WARNING, local runs only), `[Librarian] releasing the memory-write lease failed` (WARNING), `[MemoryWriteLease] release found another owner` (WARNING: a run outlived its lease and was taken over)
- `📦 [Librarian] Processing batch <batch_id> (<n> messages)`
- `👨‍🏫 [ConsolidationAgent] Turn i/15`, `Stage 1 done: N ops, M facts written`, `Stage 2a: reviewing cluster of N facts`,
  `Stage 2b: reviewing N directives`, `⚖️ … Directive cap backstop`, `⚠️ … Max turns reached without final report`
- `🔧 [FactManagement] create_fact() TOOL CALL:` (content length and metadata keys only; fact text, queries and metadata values are not logged), `✅ … Updated fact <id8> to version N`,
  `🔀 … Merging N facts`, `❌ Tool <name> failed`; the version rules (§2.4): `⚠️ [FactManagement] Cannot resolve fact <id8>: …` (merged, ended, no successor), `Fact <id8> conflicted on write (attempt n)` and `… changed concurrently twice; update refused`, `Fact <id8> moved forward between attempts; update refused` (a content or tags update that would have dropped a newer version), `Merge refused: …`, `Merge conflicted on write (attempt n)`, `Merge sources changed concurrently twice; merge refused`, `A merge source moved forward between attempts`
- `✅ [BiographicalContext] Cache refreshed for <acct>: N facts, M principles, K directives`
- `✅ [Librarian] Batch <batch_id> consolidated: stage1_ops=…, email_batches=… → DELETED`; or
  `⚠️ [Librarian] Batch <batch_id> failed (attempt n/3)`; or `❌ [Librarian] CONSOLIDATION_FAILED batch_id=… attempts=3 …`; or, once per batch, when the last allowed round failed
  `❌ [Librarian] CONSOLIDATION_GAVE_UP batch_id=… user_id=<8> revivals=3 …` (the batch stays `failed`; grep this to find batches that need the owner)
  Stage 3: `Stage 3 (email) failed` / `timed out` (ERROR, batch still succeeds), `Stage 3 skipped` (INFO)
- `[Librarian] $consolidate failed for user <8>` (ERROR) and `[Librarian] no queue for the continuation` (WARNING, local runs): the `$consolidate` path (§5.4)
- Cabinet: `Fact <id> not invalidated: FactResolutionError|FactWriteConflict: …` (WARNING, the 409 of §5.2), `[FactRepo] invalidate of <id> conflicted (attempt n)`, and, if the baseline rebuild after a delete fails, `❌ [FirestoreRepo] Failed to refresh biographical cache for <account>`
- `[Worker] sweep_consolidation complete: swept=N` (each swept user's FAILED batches were revived first)
- search: `🔍 [SearchEnrichment] Final: N facts (semantic_dedup: …, bio_dedup: …)`, `🔍 [find_nearest] START/DONE`

### 9.2 Why did the bot not remember X?

1. Was it said in a channel that consolidates? Companion-bound channels do not (§1.4).
2. Has that channel overflowed since? Count messages since (§7). If not: it is waiting (`$consolidate` forces it).
3. Was the batch consumed? Grep the batch id: `DELETED`, `failed`, or still in `{prefix}consolidation_queue`.
4. What did Stage 1 decide? BigQuery `prompt_content` for that run: the candidate list, the searches, the DISCARD
   reasons in the thinking and the final report. Usual reasons: echo trap, Trivial_Exclusions, judged a duplicate.
5. Is the fact stored but not `current` (archived, invalidated, superseded)? Readers show only `current`. A fact that was
   updated is a **chain**: the closed document keeps the old wording and its `superseded_by` leads to the live one (§2.4),
   so look for the newest `version` of its `lineage_id`.
6. Stored and current but not used: not in the baseline (§4.1 selection), and this message's prefetch did not rank it
   (§4.3). Try `search_memory` with other words; check its domain and priority.

### 9.3 Why did it remember something wrong?

- From the bot's own words: a bot reply reaches the consolidator only as its summary (§3 step 1); `Domain_Scope`
  forbids storing assistant statements unless the user adds new information. When this happens it is a model error;
  the run's BigQuery rows show which message it came from.
- A stale value: no expiry exists (§10); `temporal_class` TTLs are not enforced. An update wrote a new version; the
  earlier wording stays in the closed document of the lineage. There is no Cabinet view or script for it: in the
  Firestore console filter `{prefix}domain_facts_v2` by the fact's `lineage_id` and sort by `version`, or follow
  `replaces_fact_id` back (§2.4). History starts at the S2 switch: older updates overwrote the text and it is gone.
- From an email: Stage 3 writes facts from indexed emails (§3 step 8).
- From a system note (a known gap, current behaviour): notes the system writes into history are stored as
  `role="user"` messages — every `notify()` alert with the default `save_history=True`
  (`user_notification_service.py:287`, stored as user at `:448`), the call-event line (`:240`),
  `[System: async document ready …]` (`:547`) and the `$memory save` note. The consolidator reads them as
  `User (…): …` (`context_formatter.py:52-63`), and `Domain_Scope` bars only ASSISTANT statements, so a reminder's
  alert text sits in the owner's slot.
- Fix: invalidate it in the Cabinet (the baseline is rebuilt at once), or tell the bot the correction.

### 9.4 What did consolidation do in a given batch?

0. Starting from a fact: a fact carries no batch id. Take its `created_at` (`last_updated` for an update), find the
   nearest earlier `📦 [Librarian] Processing batch` line, then search that run's `tool_calls` in `prompt_content` for
   the fact text. Logs and `prompt_content` (30-day TTL) are the only trail.
1. Find the run: the `📦 [Librarian] Processing batch <batch_id>` line and its time.
2. BigQuery, one row per model turn, in order:
   `SELECT turn, request_text, response_text, tool_calls FROM … prompt_content WHERE agent_type = "consolidation" AND
   timestamp BETWEEN … ORDER BY timestamp` (use `--format=json`). The pass restarts its turn count, so Stage 1, 2a,
   2b and email appear as consecutive runs of turns 1, 2, …; the user message of turn 1 names the pass
   ("Begin deliberate consolidation.", "FACT CLUSTER REVIEW", "STANDING DIRECTIVES REVIEW", "[system_alert] …email").
3. `tool_calls` carry the arguments (fact texts, ids, attributes); the log lines of §9.1 carry the results.

### 9.5 Owner scripts

| Script | What it does |
|---|---|
| `scripts/consolidation/ab_cross_provider.py` | real agent, two providers, same input (dedup audit or a real chat window), per-turn cost |
| `scripts/consolidation/test_directive_review_dryrun.py` | Stage 2b on the real rulebook; `--runs N` shows the run-to-run spread |
| `scripts/consolidation/test_stage1_classification_dryrun.py` | Stage 1 on a real window, prints each fact's domain |
| `scripts/consolidation/test_confirmed_facts_dryrun.py` | do `$memory save` pairs survive Stage 1 |
| `scripts/consolidation/test_file_ops_exclusion_dryrun.py` | do drive operations leak into facts |
| `scripts/consolidation/test_{cluster_audit,decomposition,anchor_cluster}_dryrun.py`, `ab_sonnet5.py` | older Stage 2 and model benches |
| `scripts/export_account_facts.py` | dumps the account's facts to `reports/` (gitignored) |
| `scripts/validation/inspect_consolidation.py` | prints the queue and recent facts |

The `scripts/consolidation/` benches run the real agent against live reads: fact writes are intercepted and the cache
refresh is stubbed, but the model calls are real (paid) and may be recorded in the account's billing counters (the
confirmed-facts script says so in its header). Each needs the owner's OK, credentials and `.env`. Exports go to
gitignored folders only.

### 9.6 The data audit

`python -m scripts.data_audit.run facts [--account <id>] [--limit N] [--s2-cutoff <ISO time>]` streams the facts
collection read-only and checks every stored document against the invariants: stored `id` equals the document id,
`account_id` and `state` present and valid, `is_current` / `valid_to` matching `state` (§2.3), `created_at` present,
`reported_date` and `last_updated` timestamps, all three vectors stored as vectors, the document parses as a
`FactEntity`, and no lineage with more than one `is_current` document. It prints counts only and writes the offending
ids per class to `scripts/memory/data_audit_facts_<time>.json` (gitignored; no text or vectors). Run it after any
migration or backfill.

**Version-chain classes (Facts S2, `scripts/data_audit/facts.py`).** `superseded_by_dangling` (the target is missing or
in another account), `superseded_by_cycle`, `superseded_by_not_pointing_back` (a same-lineage successor that does not
name the document in `replaces_fact_id`; a merge successor is another lineage and exempt), `superseded_by_on_open_doc`
(a pointer on a document that is not superseded), `replaces_target_missing`, `replaces_lineage_mismatch`,
`replaces_version_mismatch` (the target is not at version − 1), `replaces_not_pointing_back`, `version_not_unique`,
`lineage_open_below_highest_version` (a lineage with a not-closed document below its highest version), and
`superseded_without_successor`.

`--s2-cutoff` splits the last one. A superseded document without `superseded_by` that was last written **before** the
cutoff is counted under `superseded_without_successor_pre_s2` and is not a violation: those are the documents closed before
S2 (442 on 2026-10-23). One written at or after it is a violation. A closed document with no `last_updated` is always
counted as pre-S2, whatever the cutoff. **The healthy shape:** the counted number stays at 442 for ever, because new code
never closes a document without a successor and never rewrites a closed one. A higher count means the cutoff was set too
late (old-revision closes counted as old) or old code ran after it; a non-zero `superseded_without_successor` is the
alarm. The cutoff is the time the **last old-revision
consolidation request ended** (after the queue was paused and drained), not the time of the deploy command, or a
document the old revision closed in the mixed window is reported as a post-S2 violation (`docs/07_deployment/README.md`).
With no cutoff, and version pointers present, the report says the class was not judged. `--limit` and `--account`
make the cross-document classes unreliable (a target outside the read looks dangling).

**Reading the audit after a rollback window** (pause, drain, roll back, the old revision runs, pause, drain, roll
forward). The old revision writes as it was designed, so three kinds of document dated inside the window are expected,
not damage done by S2:
- every old-revision merge and every old `SUPERSEDED`-only update leaves `superseded_without_successor` (the old code
  closes in place without `superseded_by`);
- every old-revision content edit of an S2 version document bumps its `version` in place and keeps `replaces_fact_id`, so
  `replaces_version_mismatch` fires. That is the one real loss: a text was overwritten;
- an old-revision Cabinet delete from a page loaded before a version write updates the closed document
  (`state=invalidated`, `is_current=true`, `superseded_by` kept): `lineage_multiple_current`, `superseded_by_on_open_doc`
  and `lineage_open_below_highest_version`. The owner's delete did not apply and the current version is still shown;
  reload the Cabinet and delete the live fact from the list again. (A delete started from the stale row would hit the
  closed document, which is now `invalidated` and an accepted end state, and change nothing.) That delete does **not**
  clear `lineage_multiple_current`: both documents of the lineage keep `is_current=true`. Restore the closed document's S2
  closure by hand in the Firestore console: set `state=superseded` **and** `is_current=false` together. Its
  `superseded_by` and `valid_to` are still there, so `lineage_multiple_current`, `superseded_by_on_open_doc` and
  `lineage_open_below_highest_version` all clear. Setting `is_current=false` alone on the `invalidated` document would
  trade them for `is_current_mismatch`, because the audit requires `is_current=true` for `invalidated`
  (`scripts/data_audit/facts.py:38-43`).

Then re-run the audit with the **end of the roll-forward drain** as `--s2-cutoff`. **The cutoff moves only
`superseded_without_successor`** (the old revision's closes then count as pre-S2). `replaces_version_mismatch`,
`lineage_multiple_current`, `superseded_by_on_open_doc`, `lineage_open_below_highest_version` and
`replaces_not_pointing_back` take no cutoff and stay listed. Match their ids against documents whose `last_updated` lies
inside the rollback window: those are the expected ones, any other is real.

A skipped drain (old and new revision consolidating together) shows as `lineage_multiple_current` plus
`replaces_not_pointing_back`: an old-revision blind write re-opened an older version (it is `current` with no
`superseded_by`). Do not delete either by reflex: the re-opened document carries the old run's own update, so it may hold
the newest wording. Compare both texts and their `last_updated` in the console first. If the older document holds the
newer wording, carry that wording over first (a correction in chat, consolidated onto the head). Then **close the
re-opened older document by hand**: `state=superseded`, `is_current=false`, `valid_to` = now, `superseded_by` = the newer
version's id. That restores the chain and clears `lineage_multiple_current`, `lineage_open_below_highest_version` and
`replaces_not_pointing_back`. A Cabinet delete does not: it sets `invalidated` with `is_current=true`, so the lineage
stays red.

### 9.7 The prompt snapshot and its vocabulary tests

The consolidation prompt lives in Firestore; `prompts_snapshot/` is its git mirror (profile `consolidation.yaml`,
blueprint `consolidation_agent_v1.yaml`, the six `CONSOLIDATION_*` tokens; `COGNITIVE_PROCESS_CONSOLIDATION.groovy`
is an older token the profile does not render). Edit there and upload with `firestore_utils/snapshot_upload.py`
(`prompts_snapshot/README.md`). The P8 PR (branch `claude/gallant-mendel-ocl9xp`, not on main at the time of writing)
adds `tests/unit/prompts/test_consolidation_vocabulary.py`, which checks every enum value and tool field the tokens
name against `entities.py` and `_get_tool_declarations`, and corrects the tokens' `HISTORICAL` to `ARCHIVAL`; until it
merges and the tokens are uploaded, the live prompt says `HISTORICAL` (§2.2).

---

## 10. Not built yet

`docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (rev 2, accepted 2026-10-21) — S1 (defect fixes) is live. **S2 is built** on the
branch that carries S2–S5 (real versions with `superseded_by`, validation before any write, preconditioned writes, the
memory-write lease, the run tracker as a parameter; §2.4, §3 step 3, §5.2; `decisions/fact_versions_one_writer.md`)
and goes live with S3–S5, which deploy together. Still to come:
- **S3** expiry decided from the content (date / horizon / none), a daily zero-LLM janitor (log-only first),
  `HISTORICAL` → `ARCHIVAL`, prompt changes. The S3 prompt work must also name a successor wherever a prompt still tells
  the model to retire a split fact with `SUPERSEDED` alone (S2 refuses that; §6).
- **S4** a review pass over overdue facts and questions to the owner in chat (it will take the same lease).
- **S5** one-time classification of all current facts, review dry run, janitor on.

Until the S3–S5 deploy: nothing expires and `stale` is never set. S2 does not ship alone: it refuses `SUPERSEDED` without
a successor while the live consolidation prompts still tell the model to retire a split fact that way.

---

## 11. Code map

| File | Role |
|---|---|
| `src/domain/entities.py` | `FactEntity` (with `superseded_by`), the four enums, `normalize_fact_taxonomy`, `FACT_SYSTEM_TAGS` |
| `src/domain/fact_lifecycle.py` | state → `is_current` / `valid_to`; forward resolution (`forward_step`), the accepted end states per caller, `IN_PLACE_FIELDS`, `closure_fields` |
| `src/domain/fact_update.py`, `src/domain/fact_update_plan.py` | `update_fact` arguments parsed and validated; the plan: version document or in-place fields |
| `src/domain/new_fact_attributes.py`, `src/domain/fact_argument_parsing.py` | a new fact's arguments parsed before any embedding; enum and date parsing |
| `src/domain/stored_fact.py`, `src/domain/consolidation_run.py` | a document with its change token; the outcome of one consolidation request |
| `src/domain/memory_write_lease.py` | lease margin, deferral delay, `lease_until`, the run timeout left, the deferral bucket |
| `src/domain/exceptions.py` | `FactResolutionError`, `FactWriteConflict`, `InPlaceFieldRefused` (none is a `ValueError`) |
| `src/domain/fact_embedding.py` | the three embedding texts |
| `src/domain/consolidation.py` | `ConsolidationBatch` (with `revivals`), `BatchStatus`, `MAX_FAILED_REVIVALS` |
| `src/domain/consolidation_serialization.py` | messages → batch (summary vs full text) |
| `src/domain/consolidation_pass_stats.py` | 📊 / 🚨 texts |
| `src/domain/settings.py` | `ConsolidationSettings` (70/50), `SearchConfig` (limits, baseline 65/20, directives 15) |
| `src/domain/search.py`, `src/domain/rrf.py`, `src/domain/deduplication_service.py`, `src/domain/vector_math.py` | search DTOs, RRF, number-aware dedup, cosine |
| `src/domain/memory_fact.py` | `$memory save` draft, preview and history pair |
| `src/adapters/firestore_session_store.py` | sessions, sliding window, 90-day TTL |
| `src/services/overflow_routing_service.py` | batch → Alek's queue or a companion's |
| `src/adapters/firestore_consolidation_queue.py` | queue, oldest first, zombie reset (threshold per caller), bounded failed revival (`revive_failed`), `has_open_batches`, sweep query |
| `src/adapters/gcp_task_queue.py` | `enqueue_consolidation_task` |
| `src/handlers/worker_handler.py` | `consolidation`, `sweep_consolidation` (revives FAILED, then enqueues) |
| `src/services/consolidation_service.py` | one batch under the lease: run, delete or retry; deferral; `consolidate_now` (`$consolidate`); `revive_failed_batches` (sweep); the give-up log |
| `src/ports/memory_write_lease.py`, `src/adapters/firestore_memory_write_lease.py` | the per-account lease (acquire, release by owner token) |
| `src/agents/consolidation_agent.py` | all stages, tools, loop, backstop, cluster and directive messages |
| `src/infrastructure/agent_config.py` | `ConsolidationAgentConfig`, `MemorySearchAgentConfig`, Router and Smart windows |
| `src/composition/application_graph.py` (`:641-650`, `build_application_graph`), `src/composition/{slack,telegram}_adapter_factory.py` | the one `ConsolidationService` is built here, with the lease, the task queue and the 900 s run timeout; the chat adapters get its `consolidate_now` |
| `src/composition/user_agent_factory.py` | builds the consolidator, the per-user `SearchEnrichmentService`, MemorySearch |
| `src/services/agent_context_builder.py`, `src/domain/user.py` | provider and tier per agent |
| `src/ports/fact_management_port.py`, `src/adapters/fact_management_adapter.py` | consolidator tools |
| `src/ports/fact_write_port.py`, `src/services/fact_write_service.py` | fact creation with vectors (`build_facts` embeds and builds, `add_facts_batch` also stores) |
| `src/ports/repository.py`, `src/adapters/firestore_repo.py` | every fact query, the cache document, Cabinet invalidate; `resolve_current`, `write_superseding`, `update_fact_fields`, `get_lineage` |
| `src/services/biographical_context_service.py`, `src/services/configuration_service.py` | baseline selection and limits |
| `src/services/prompt_builder.py`, `src/services/prompt_v3/{prompt_assembly_service,biographical_formatter,context_formatter}.py` | channel split, rendering of baseline, directives, conversation |
| `src/ports/search_enrichment_port.py`, `src/services/search_enrichment_service.py` | multi-channel search |
| `src/agents/core/router_agent.py` | prefetch |
| `src/agents/core/smart_response_agent.py` | baseline + prefetch merge, `propose_memory_facts` |
| `src/agents/memory_search_agent.py` | `search_memory` |
| `src/services/lelik_persona_service.py` | Lelik's facts |
| `src/composition/mcp_setup.py` | `get_user_context` |
| `src/infrastructure/memory_fact_tools.py`, `src/services/memory_fact_service.py`, `src/adapters/firestore_memory_fact_draft_repository.py` | `$memory save` |
| `src/handlers/conversation_handler.py` | `$consolidate` (and its outcome wording), `$memory save`, memory preview delivery |
| `src/web/user_cabinet_app.py` | Cabinet fact routes (invalidate follows the id forward, answers 409, rebuilds the baseline) |
| `prompts_snapshot/{profiles/consolidation.yaml,blueprints/consolidation_agent_v1.yaml,tokens/system/CONSOLIDATION_*.groovy}` | the consolidation prompt (mirror) |
| `scripts/consolidation/`, `scripts/data_audit/` | benches; the audit with the version-chain classes (§9.6) |

Stale code comments, out of scope for this document (`src/`): `ConsolidationAgent._review_directives` says "hard cap of
10" and `biographical_context_service.py:155` says "soft cap ~12" (the code uses 15); the `BiographicalContextService`
docstring says CRITICAL facts are always included (the code does not).
