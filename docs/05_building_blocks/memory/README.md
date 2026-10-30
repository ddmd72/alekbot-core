# Memory (Building Block)

The owner's map of long-term memory: how a conversation becomes facts, how facts reach every prompt, how they are
searched, what fails and what it costs. Current state only. The code paths (`path:line`) are the ground truth; when this
text and the code disagree, the code wins and this file is wrong. Every session that touches memory facts,
consolidation, the biographical baseline, memory search, `$memory save`, standing directives or the fact lifecycle ends
by updating this file.

Design records: `docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md` (taxonomy, tools, cognitive process),
`docs/10_rfcs/CLUSTER_REVIEW_RFC.md` (Stage 2a), `docs/10_rfcs/STANDING_DIRECTIVES_RFC.md` (Stage 2b),
`docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md` (`$memory save`), `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (S1 live; S2, S3 and S4 deployed 2026-10-27, janitor writes since the deploy of 2026-10-27;
S5's classify applied 2026-10-27; P2 revised, "a fact whose date passed becomes history", deployed), decisions `docs/04_solution_strategy/decisions/{fact_s1_defects,fact_versions_one_writer,fact_expiry_and_janitor,fact_review_and_owner_questions,standing_directives,directive_applicability_gate,memory_facts_owner_confirmed,consolidation_one_batch_per_task,consolidation_failed_batch_bounded,sonnet55_adaptive_thinking_stall,consolidation_provider_model_eval}.md`.

## 0. How to answer questions about memory

| Question | Read |
|---|---|
| What is in memory; what a fact holds; what `domain`, `temporal_class`, `state`, `context_priority` mean | §2 |
| What `is_current`, `valid_to`, `lineage_id`, `version`, `superseded_by` mean today; what an update or a merge does to them | §2.3, §2.4 |
| Why an update gave the fact a new id; whether the old wording is kept and how to look at it; why an old id still works | §2.4, §9.3 |
| When does a conversation become facts; why nothing happened after I told it something | §3 steps 1–3, §7 |
| How to deploy or roll back the fact versions (S2), the expiry work (S3) and the review (S4), and how to read the audit afterwards | the rollout notes (ops), §9.6 |
| When a fact ends: what `expiry` is, who computes `expires_at`, why a write was refused for its expiry, what "born due" means | §2.7, §6 |
| What the daily janitor does, why a fact went `stale`, what log-only means, the breaker, the pool, the run guard, the alerts, the 🧹 morning line | §5.5, §9.1 |
| What the review pass does with a stale fact; why a fact came back, was set aside, became history or was asked about | §5.6, §6 |
| What happens to a plan whose date passed, a document or membership that ended, an invoice, a fine (P2 revised: history, the question kinds, the invoice block) | §5.6, §2.7, §7 |
| How a question reaches me, how I answer it (a digit, a button, a reply in words, the Cabinet), what happens if I do not | §5.6 |
| Why no question arrived, or arrived twice, or its buttons did nothing | §5.6, §9.2b |
| What the weekly digest says and when it is sent | §5.6 |
| What the Cabinet shows for stale, set-aside and waiting facts, and what it cannot do | §4.8, §5.6 |
| How to run the model dry run of the S3 and S4 prompts before the deploy (step M) | the rollout notes (ops) step M, §9.5 |
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
| How to re-open archived facts, re-review closed facts with a dry run first, or roll P2's questions back | §9.5 |
| One-time classify of the existing facts, janitor on | §10 (not built) |
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
invalidate a fact from the Cabinet (§5.2); that rebuilds the baseline at once. One daily zero-LLM job, the janitor, only
moves a fact whose expiry has passed from `current` to `stale` (§5.5); it has written since the deploy of 2026-10-27 (rollback: remove `FACT_LIFECYCLE_SWEEP_DRY_RUN` from the deploy).
What becomes of a stale fact is decided by the **review pass** (a fifth model pass, §5.6): it brings the fact back, sets
it aside, or asks the owner one question at a time in the primary chat. The owner's answers (a digit, a button, a
reply, a Cabinet click) are applied by code, not by a model (§5.6).

### 1.2 The stores

| Store | Collection | What it holds | Written by |
|---|---|---|---|
| Facts | `{prefix}domain_facts_v2` (`src/config/environment.py:114-120`) | one document per fact **version** (a content change adds a document, a lifecycle change edits one, §2.4), three 768-dim vectors each | consolidation tools (§3), Cabinet invalidate (§5.2), the janitor (§5.5, a state change to `stale` only), the review pass and the owner's answers (§5.6) |
| Sessions | `{prefix}sessions` (`environment.py:455-463`), doc id `{user_id}:{channel_id}` | the channel's recent messages (the sliding window) | `ConversationHandler`, notifications, `$memory save` |
| Consolidation queue | `{prefix}consolidation_queue` (`environment.py:465-472`) | batches cut from sessions, until consolidated | overflow routing (§3 step 3), `$consolidate` |
| Baseline cache | `{prefix}user_context`, doc id = account id (`environment.py:524-531`) | `biographical_facts`, `principles`, `directives` lists | `refresh_biographical_context_cache` (§3 step 9) |
| Memory-write lease | `{prefix}memory_write_leases`, doc id = account id (`environment.py:507-513`) | `lease_owner`, `lease_until`, `acquired_at`; no TTL policy | `ConsolidationService`, one lease per batch (§3 step 3) |
| Memory drafts | `{prefix}memory_fact_drafts`, doc id `{user_id}:{code}` (`environment.py:350-354`) | facts Smart proposed, 7-day TTL | `propose_memory_facts` (§5.1) |
| Janitor counters | `{prefix}fact_lifecycle_daily` (`environment.py:515-521`), doc id `{account_id}:{UTC day}` | one snapshot of the day's janitor run per account (last run of the day wins) and the day's `expiry_tool_errors` count; **no TTL policy**, no field named `expires_at` | the janitor, the consolidator's tool handler (§5.5) |
| Janitor line mark | `{prefix}fact_lifecycle_lines` (`environment.py:523-529`), doc id = the UTC day | claims the day's single 🧹 ops line; never read as an account row; **no TTL policy** | the janitor (§5.5) |
| Janitor run guard | `{prefix}fact_lifecycle_run`, one document `sweep` | `run_owner`, `run_until`: at most one janitor run at a time (an expired guard stays until the next run takes it over); **no TTL policy** | the janitor (§5.5) |
| Review questions | `{prefix}fact_review_questions`, doc id = the question id | one question per fact: its status, the quoted text, the queue place, the message it was posted as, the owner's reply and answer (§5.6); a closed one carries `purge_after` for a TTL policy | the review service, `FactAnswerService` (§5.6) |
| Review messages | `{prefix}fact_review_messages`, doc id `{user_id}:digest:{iso_week}` or `{account_id}:quote:{language}:{source_digest}` | the claim that lets one weekly digest go out per ISO week, and the quote translations (a stored fact's text as shown in a message language, §5.6); both carry `purge_after` | the review service (§5.6) |
| Review state | `{prefix}fact_review_state`, doc id = account id | when the review last ran or failed, the offers per fact, the current question and today's send count, the digest marks, the alert marks, the message language and its labels; **no TTL policy** | the janitor (its enqueue and alert marks), the review service, `FactAnswerService` (§5.6) |

### 1.3 The actors

| Actor | Role | Code |
|---|---|---|
| `FirestoreSessionStore` | appends messages, cuts overflow batches | `src/adapters/firestore_session_store.py` |
| `OverflowRoutingService` | sends a batch to Alek's queue or a companion's | `src/services/overflow_routing_service.py` |
| `ConsolidationService` | takes the account's memory-write lease, takes one pending batch, runs the agent, deletes or retries the batch; defers itself when the lease is taken | `src/services/consolidation_service.py` |
| `FirestoreMemoryWriteLease` | the lease: one memory-writing run per account | `src/adapters/firestore_memory_write_lease.py` |
| `ConsolidationAgent` | the model loop, all stages | `src/agents/consolidation_agent.py` |
| `FactManagementAdapter` | the model's tools (`FactManagementPort`): search, create, update, merge; and code's typed update (the agent's own `fact_updates` dependency, the same adapter in composition; `FactUpdatePort.apply_fact_update`: a decided `ParsedFactUpdate`, checked by `typed_update_refusal`, with the review's and the owner's `evidence_at`, `reason` and `history`; it returns a typed `FactUpdateOutcome`, and the model's `update_fact` dict is that outcome rendered by `fact_tool_result.update_result`), one apply under both updates; follows stale ids forward, writes under preconditions; applies the expiry, `reported_date` and trail rules of the pass's write context (§2.7) | `src/adapters/fact_management_adapter.py` |
| `FactLifecycleSweepService` | the janitor: stales due facts, applies the breaker and the per-run cap, refreshes the baseline, stores the day's snapshot, posts the 🧹 line (§5.5) | `src/services/fact_lifecycle_sweep_service.py` |
| `FirestoreFactLifecycleCounters` | the janitor's snapshots, the review's and the answers' counter increments, the posted-line mark and the run guard | `src/adapters/firestore_fact_lifecycle_counters.py` |
| `FactReviewService` | one review run for an account: the owner check, the memory-write lease, housekeeping, timeouts, selection, the model pass, the queue, the send of the next question, the cache, the digest, the counters (§5.6) | `src/services/fact_review_service.py` |
| `FactReviewScope` | the review pass's two tools, `update_fact` and `ask_owner`, with the basis and ask rules | `src/infrastructure/fact_review_scope.py` |
| `FactAnswerService` | the one path of an owner answer and the one way a question closes; the reply at intake; the click on a button (§5.6) | `src/services/fact_answer_service.py` |
| `FirestoreFactReviewStore` | the question records, the account's review state, the digest claim | `src/adapters/firestore_fact_review_store.py` |
| `FactQuestionTexts` | the one resolver of the question's language, its labels and its message | `src/services/fact_question_texts.py` |
| Interactive messages | a platform-neutral message with options (`InteractiveMessage`), a router that picks the platform's adapter (Slack buttons, plain text with digits on Telegram), and `InteractiveChoiceService` for a click | `src/domain/interactive_message.py`, `src/infrastructure/interactive_message_router.py`, `src/adapters/slack/interactive_message_adapter.py`, `src/adapters/plain_text_interactive_message_adapter.py`, `src/services/interactive_choice_service.py` |
| `CabinetFactService` | the Cabinet's invalidate (§5.2) | `src/services/cabinet_fact_service.py` |
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

`FactEntity` (`src/domain/entities.py:111-179`):

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
| `reported_date` | when the owner last stated the fact; code owns it per pass (§2.7); shown by `search_memory` as `reported: YYYY-MM-DD`, and to the consolidator as an ISO time with the owner's offset |
| `created_at` | ordering of the baseline and the domain channel; a version copies it from the document it replaces, so it is the fact's first mention |
| `version`, `lineage_id`, `replaces_fact_id`, `superseded_by`, `is_current`, `valid_from`, `valid_to` | §2.3–§2.4 |
| `expiry_kind`, `ttl_days`, `expires_at` | the expiry decision, written by code from the model's `expiry` argument (§2.7): kind `date`, `horizon` or `none`; `ttl_days` only for a horizon; `expires_at` null for `none`. Read by the janitor (§5.5) and, as `expiry` / `due`, by the consolidator. The `ttl_days` values the model wrote before S3 (113 on 2026-10-23) mean nothing and are ignored while `expiry_kind` is null |
| `stale_since`, `lifecycle_events` | `stale_since`: when the janitor staled the fact; it orders the review queue and is cleared whenever the fact comes back to `current` (a revive, a rewrite, a queued question, an answer, a fallback), and kept on a fact the review or the owner sets aside. `lifecycle_events`: the newest 20 state transitions `{at, from, to, actor, reason}` with code-built reasons (§2.7); written in the same write as the transition, never shown to a model |
| `last_updated` | written on every in-place write; read by no code |
| `visibility` | defaults to `account_shared`; never filtered on read: every member of an account sees every fact |

The adapter also stores `context_priority_rank` (critical 1 … archival 5) next to the entity for ordering
(`firestore_repo.py:38-44`, written by `_to_document`, `:176-184`, for every new document).

### 2.2 The four axes

| Axis | Values (`entities.py`) | What code does with it |
|---|---|---|
| `domain` | `biographical`, `possession`, `health`, `medical_records`, `location`, `work`, `network`, `preference`, `agent_directive`, `skill`, `project`, `finance`, `education`, `legal`, `entertainment`, `communication` (`:18-35`) | baseline order (biographical first), standing directives (`agent_directive`), Router domain channel, Lelik's filter |
| `temporal_class` | `permanent`, `stable`, `dynamic`, `ephemeral` (`:38-43`) | none; shown to the consolidator. Since S3 it is only a default for the expiry decision when no row of the kinds table applies (§2.7); no code reads it |
| `state` | `current`, `stale`, `archived`, `superseded`, `invalidated` (`:46-52`) | **the only visibility switch**: every reader filters `state == "current"` (`firestore_repo.py:232,286,400,471,661,899`). Only the janitor sets `stale` (§5.5); the model is refused it everywhere (§2.7). A fact whose question waits for the owner is `current` (§5.6) |
| `context_priority` | `critical`, `high`, `medium`, `low`, `archival` (`:55-61`) | baseline order; `archival` (rank 5) is never in the baseline or the rulebook (`firestore_repo.py:479`) |

The model writes these in upper case; `normalize_fact_taxonomy` lowercases them before the enums parse
(`entities.py:91-104`) and reads the retired priority `historical` as `archival` (RFC D11; the S3 tokens say `ARCHIVAL`).
A value outside the enum fails the tool call (§6). In the 30 days before 2026-10-21 none of 87 written priorities was
`historical` (`decisions/fact_s1_defects.md`); the vocabulary tests are in §9.7.

### 2.3 States and the SCD2 flags

`domain/fact_lifecycle.py:15-33` maps a state to its flags; every retirement path uses it (adapter update and merge,
Cabinet invalidate, the directive backstop):

| State | `is_current` | `valid_to` | `superseded_by` | Typical cause |
|---|---|---|---|---|
| `current`, `stale` | true | none | none | created; `stale` is set by the janitor when the fact's expiry has passed (§5.5). A stale fact the review cannot decide is made `current` again while its question waits (§5.6) |
| `archived` | true | now | none | "was true, no longer" (the prompt's Negation rule) |
| `invalidated` | true | now | none | "was never true", Stage 2b removal, Cabinet, backstop |
| `superseded` | **false** | now | the successor's id (empty on documents closed before S2) | a document replaced by a newer version of its lineage, a merge source, or a compound fact the model split (it must name the fact that replaces it; the call is refused without it) |

`is_current` means "latest document of its lineage", not "visible". Visibility is `state` alone. No `src/` code reads `is_current`; the data audit checks it (§9.6).

### 2.4 Updates, merges, versions, lineage

A fact is a **lineage**: the documents that share one `lineage_id`, told apart by `version`. At most one of them is
the live one (a lineage whose last document was merged away has none). Two kinds of write exist (`fact_update_plan.py:30-73`):

- **A content change is a new document.** If an `update_fact` changes the text, the tags, the metadata or the
  `temporal_class`, the store writes a new document: new `id`, same `lineage_id`, `version` + 1,
  `replaces_fact_id` = the old id, `created_at` copied from the old one, `valid_from` now, `reported_date` by the rule of
  the pass (§2.7: the stored date is kept, never moved back), the expiry decision recomputed from the call's `expiry`, and
  the old document's `lifecycle_events` plus the version's own entry. The old document is closed in the **same transaction**:
  `state=superseded`, `is_current=false`, `valid_to` now, `superseded_by` = the new id
  (`firestore_repo.py:562-587`, `fact_lifecycle.py:84-86`). The result's `fact_id` is the **new** id.
  "Tags changed" compares the lists as multisets, so resending the stored tags in another order is not a change.
- **A lifecycle change stays in place.** A call that only changes `state`, `reported_date` or `expiry` (or changes nothing)
  writes just those fields, the derived decision fields, a `lifecycle_events` entry for a state change, and `last_updated`
  onto the live document; `version` and the id stay
  (`fact_update_plan.py:88-98`, `firestore_repo.py:589-603`; only the keys of `IN_PLACE_FIELDS`,
  `fact_lifecycle.py:41-47`, can be written this way). Re-applying the stored state leaves `valid_to` alone. A state change
  together with a content change goes into the new version.
- **`domain` and `context_priority` cannot be changed by an update**: the tool has no such fields. A different domain
  means a new fact (Stage 2b demotes a directive by creating a `preference` fact and invalidating the directive).
- **Retiring a fact for another one needs the other one.** `state=SUPERSEDED` needs `superseded_by`, the id of the
  replacing fact (resolved forward like any id, and it must be a different fact). Without it, or together with a
  content key, the call is refused (`fact_update.py:32-71`).
- **Tags.** The system tags `consolidated` and `anchor` survive a tags update (appended at the end, in stored order).
  `tags: null`, `tags: []` and an empty `content` are refused: an empty field never destroys data
  (`fact_update.py:81-98`).
- **Everything is checked before anything is embedded or written**: enums, dates, tags, content, `expiry`, on create,
  update and merge (`fact_update.py`, `new_fact_attributes.py:45-103`, the expiry rules of §2.7). One bad fact fails a whole `add_facts_batch` call (the
  legacy v2 path) before any embedding. A fact cannot be created or merged into `state=superseded` or `state=stale` (stale is the janitor's, §2.7).
- **Vectors follow content.** All three are re-embedded when the three embedding texts change (§2.6), before
  anything is written (`_complete_version`, `fact_management_adapter.py:327-345`); an embedding failure fails the call
  with nothing stored. A `temporal_class`-only version copies the old vectors. After a conflict the retry reuses an
  embedding made for the same texts.
- **A stale id still works.** The model reuses ids from an earlier search, and the consolidator often writes one fact
  twice in a pass. `update_fact` and `merge_facts` follow `superseded_by` from the id they got to the live document
  (`resolve_current`, `firestore_repo.py:541-560`, one hop at a time through `forward_step`, `fact_lifecycle.py:52-81`).
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
(`firestore_repo.py:541-560`, `fact_lifecycle.py:52-62`). A `superseded_by` successor that is missing or foreign is
"successor fact not found". Cabinet invalidate checks the stored account of the id it is given and returns 403
(`firestore_repo.py:316-322`). `visibility` is not enforced (owner, 2026-10-21).

### 2.6 The three vectors

`domain/fact_embedding.py:9-17` defines the texts, shared by creation and re-embedding: `text`; `tags` joined (system
tags `consolidated`/`anchor` left out; the text when no tag is left); `metadata` as JSON (the text when empty). All
three are embedded with `RETRIEVAL_DOCUMENT` in one batch call (`fact_write_service.py:181-187`), model
`gemini-embedding-2` at 768 dimensions (`src/adapters/gemini_embedding_adapter.py`). Searches embed queries with
`RETRIEVAL_QUERY` (`search_enrichment_service.py:123`).

### 2.7 Expiry, `reported_date` and the trail (Facts S3)

**The model decides, code computes.** `create_fact`, `merge_facts` and an `update_fact` that makes a new version (it sends
text, tags, metadata or temporal class) need an `expiry` inside `fact_attributes` / `updates`; an `update_fact` that sends only
`expiry` (± `reported_date`) edits the document in place and recomputes. Three answers (`domain/fact_expiry.py`):
`{kind: date, date: YYYY-MM-DD}` the last day the fact holds; `{kind: horizon, days: N}` review it if the owner has not said it
again within N days; `{kind: none}` history, stable or permanent. The string `"none"` stands for the last; `days` takes a whole
number from 1 to 36 500 (an integer, a whole float or a string of digits); a `date` year must be 1900–9998; `null` is
refused with an explanation. A top-level `expiry` next to `fact_attributes` / `updates` is moved into place (a nested one wins).
The model's `ttl_days` is dropped at every parse boundary, on the legacy v2 path too. The kinds table that decides lives in
`CONSOLIDATION_TAXONOMY` (planned event / validity → `date`; current activity, changing value, noise → `horizon`; past event,
stable, directive, principle → `none`); the `temporal_class` line is only a default when no row applies.

**What code writes** (`compute_expiry`, `domain/fact_lifecycle.py:89-104`): `date` → `expires_at` = the start of the next
local day in the **owner's timezone**, as UTC (so it is due at the end of that day; DST-safe); `horizon` → `reported_date` +
N days and `ttl_days` = N; `none` → no `expires_at`. The owner's zone is resolved once per consolidation run from the
account's owner profile (`ConsolidationService._owner_timezone` deciding with `domain/owner_timezone.py`, which the dry-run kit calls too; an unresolvable one is UTC with one WARNING) and travels in
`message.context["owner_timezone"]`; a dry-run script without it gets UTC.

**Born due is refused** (`born_due`, `fact_lifecycle.py:107-137`): a decision that resolves at or before now + 1 day would be
stale on arrival. A past `date` is refused on every pass: on Stage 1 and email with "discard the candidate, or rewrite it as
history with `{kind: none}`", on a cleanup pass with "do not drop the fact because its date passed: rewrite it as history with
`{kind: none}` or give a future date" (S3-D12, no DISCARD: the subject is a stored fact); today's date is refused too
("a plan for today is not over yet: give a short horizon"); on Stage 1 and email passes a horizon whose
end falls at or before now + 1 day is refused with the minimum N (counted with a one-day margin). **Cleanup passes**
(Stage 2a, Stage 2b, the directive backstop, `consolidate_cluster`; they cannot move `reported_date`) are not refused for a
due horizon: `expires_at` becomes now + 2 days, kind and days are kept, and the result carries a note, so the fact reaches
review soon (owner, Q4 b). Every expiry refusal ends "Nothing was written. Resend this call with a valid expiry before
retiring any fact it replaces." and the tool result is `status: failed, refused: "expiry"` (a born-due refusal also has
`born_due: true`), always before any embedding or write.

**Pin and closing states.** Directive, principle and `mindset` facts take `none`: a missing `expiry` is filled in as `none`, a
`date` or `horizon` is refused. An `expiry` sent together with a closing state (`archived`, `invalidated`, `superseded`), on an
update, or on a create or merge whose state is closing, is ignored with a note (a closing create needs none). `state=stale`
is refused from the model on update, create and merge: only the system marks a fact stale.

**`reported_date` means "when the owner last stated it", and code owns it** (`domain/fact_write_context.py`). Each pass has a
write context: the actor (`stage1`, `stage2a`, `stage2b`, `backstop`, `email`, `cluster`), the owner's zone, the **span**
(first and last message time of the batch, any role; for email the oldest and newest `email_date`, naive dates read as UTC;
none for `cluster`) and whether the model's date counts. On Stage 1 and email passes the model's `reported_date` is clamped
into the span (never after now) and used; a missing one is the span end. A merge on any pass is never dated before its
newest source (the later of the model's clamped date and the sources' latest). A create or merge on a cleanup pass ignores a
model date (the result says so) and takes the latest date of its sources: for a create, the facts named in the optional
`derived_from` (a decomposition part or a demotion; read by id, same account only), else the span end, else now. An update
never moves the date back (`max(stored, new)`), and a cleanup pass keeps the stored one. A model value may be an ISO time with
an offset or the exact stamp the consolidator shows its conversation (`2026-10-22 Thu 12:00 CEST`, read in the owner's zone);
a naive ISO value is read as UTC (the clamp bounds a wrong guess). **Extension rule** (Stage 1 and email tokens only): when the
owner says an existing fact again and the conversation confirms it held all along, the model sends
`update_fact({reported_date: <the message's time>})`; a horizon then counts from the new date and never ends earlier than
before (`max(stored, new + days)`); when the link is unclear it creates a new fact and leaves the old one. A RAG echo stays
DISCARD, and cleanup passes never extend.

**Review and owner writes (S4).** The review pass and the owner's answers write through the same update path with their own
pass actors, `review`, `owner` and (P2 revised) `timeout` for a silence that writes history (`domain/pass_actor.py`, the one
table that says whose dates count, whether a due horizon is floored, which stored states a write may start from and how a
refusal reads; `timeout` uses the owner's row). They differ from the consolidation passes in six ways (§5.6 has the flow):
- **A horizon counts from now.** A revive or a rewrite by the review, an owner's "yes" and a silence's fallback give a
  horizon of N days from the moment of the write (`horizon_from_now`): `expires_at` is that moment plus N days and `ttl_days`
  is stored so that `reported_date + ttl_days` still equals it (up to a day later, whole days). The strict born-due rule
  applies: no floor, so N must be at least 2 and a `date` must be ahead. The review prompt gives `none` only on evidence or a reply,
  or with a history rewrite on any basis (P2 revised), and a pinned fact always gets `none`.
- **`reported_date`.** A revive on evidence takes the later of the stored date and the evidence fact's date; on a reply or an
  owner answer, the later of the stored date and the answer time. A rewrite, a queue write and a fallback keep it. The review
  never sends a date (it is dropped with a note).
- **A confirmation keeps the decision it can** (`confirmed_expiry`): a pinned or `none` fact stays permanent, a `date` that
  is still ahead keeps its date, anything else gets a horizon of the fallback window.
- **A fact waiting for the owner is `current` with a past `expires_at`.** Queueing clears `stale_since` and leaves the expiry
  as stored. Every reader sees an ordinary current fact; the janitor skips it while its question is open (§5.5).
- **Silence doubles the window.** When nobody answers, a fact that falls back to `revive` gets twice the window its last
  revive gave, between 14 and 365 days (28, 56, … days). It is never replaced by a silent revive of an unrelated length and
  never "asked once and then forgotten". A fallback to `archive` is used only for noise (P2 revised; before it, also for a dated
  plan or a past `fallback_date`, which stored questions keep); a fallback to `history` writes the question's "unknown" text.
- **A fact whose date passed becomes history (P2 revised, built, not deployed).** The review or an owner's answer sends
  `as_history`: `updates {state: CURRENT, content: <the history text>, expiry: {kind: none}}`. It is a **version** in the same
  lineage (D8: `version + 1`, three vectors from the new text, `created_at` and `reported_date` kept, the dated original
  `superseded` by it in one transaction), at **`archival` priority** (out of the baseline cache, found by every search), with
  `metadata` emptied and `context` cleared before the embedding texts are built, so nothing of the plan's metadata describes the
  history. Only the review, the owner and a timeout may write one (`finish_fact_update` refuses any other writer). The text is
  English, past tense, at most 40 words, keeps the date at the fact's precision, drops identifiers (booking numbers, codes,
  times) and keeps an invoice's amount and due day: "In Nov 2026 the user had the car serviced (Nov 5).", "The user planned to
  attend a conference Nov 18–20, 2026, and did not go.", "The user planned to attend a concert on Dec 5, 2026; whether it took
  place is unknown."
The trail actors added are `review` (reasons such as `revived on <id8>`, `revived on reply q=<id>`, `archived: date passed
<date>`, `queued for the owner q=<id>`), `owner` (`confirmed q=<id> (digit|button|cabinet)`, `ended …`, `never …`,
`did not remember q=<id>`), `timeout` (`no answer to q=<id>: revive|archive`) and `review_fallback` (`kept twice: queued
q=<id>`). P2 revised adds the history reasons: `rewritten as history on <id8>` / `on reply q=<id>` / `: date passed <date>`
(review), `rewritten as history q=<id>: <happened|not_happened|not_needed|paid|not_paid> (<source>)`, `renewed q=<id>
(<source>)`, `rewritten q=<id>: not_renewed (<source>)` and `rewritten as history: did not remember q=<id>` (owner),
`rewritten as history: no answer to q=<id>` (timeout). They read as rewrites (so `renewed` gives a window a later silence
doubles), never as revives. The S4 reason `archived: date passed <date>` has no P2 writer; the facts it closed are re-opened by
id with the reopen script (§9.5).

**What the model sees.** The consolidator's search results carry `expiry` in the shape it sends, `due` (the local end date of
a horizon) and `reported_date` as an ISO time with the owner's offset; never `ttl_days`, `expires_at` or `expiry_kind`. A
`date` view sent back unchanged keeps the same `expires_at` unless that date is today or past: then it is refused as born
due; a fact with no `expiry_kind` shows `expiry: null`, which Stage 2a tells the model means "classified separately"
(it decides `expiry` only for what it writes in the pass). **One timestamp
format:** the consolidator's conversation lines (`User (2026-10-22 Thu 12:00 CEST): …`) and every pass's user message
(`Today: 2026-10-23 Fri (Europe/Paris, the owner's timezone).`) use the formatter Smart's own message stamps use
(`domain/local_time.py`), in the owner's zone, with year and weekday.

**The trail.** Every state change writes a `lifecycle_events` entry in the same write: `{at, from, to, actor, reason}`, the
newest 20 kept, with code-built reasons only (`model`, `replaced by <id>`, `version <n> of <id>`, `merged into <id>`,
`superseded by <id>`, `deleted in the Cabinet`, `directive cap`, `expired <ISO>` by the janitor). A version starts with its
predecessor's trail plus its own entry; a merge or version close appends to each closed document. The actor labels are the
pass labels plus `cabinet`, `janitor` and `migration`. Nothing renders the trail to a model.

**Known limits.** The legacy v2 path writes no expiry decision and parses `reported_date` without the zone (the stamp it
shows cannot be read back; a naive time is UTC). A resent `reported_date` is read in UTC when it has no offset, so it can be
off by the owner's offset; the clamp bounds it.

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
   A memory-review question reaches the batch as an ordinary pair of messages (§5.6): the user part is the code frame
   `[System: memory review q=<id> — …]`, the model part is the question with the quoted fact (written with
   `text == full_text`, so the batch sees the question as sent). The owner's reply is the pair
   `[Reply to memory review q=<id>] <his text>` and the acknowledgement. Stage 1 reads them under the narrowed echo-trap
   exception (step 5).
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
  is no 5xx or retry loop (`_account_to_lease`, `:182-200`). The missing-profile case is real: the user's agents stay
  cached for about an hour, so `ensure_agents_for_user` can succeed after the profile is gone. The user repository is a
  required argument of `ConsolidationService`, so no wiring can leave it out;
- resolves **the owner's timezone** once per run, after the account (`OwnerTimezoneResolver`, a required argument, deciding with `domain/owner_timezone.py`): the account's owner
  (`BillingAccount.owner_user_ids()`; the first when there are several, with a WARNING) → that profile's `timezone`,
  checked as an IANA zone. Any missing step, an invalid zone or a failing repository gives `UTC` and one WARNING
  (`[OwnerZone] owner timezone unresolved for account <8>: …; using UTC`), never a failed batch. It rides in the agent
  message's context as `owner_timezone` and sets how every date, stamp and expiry of the run is read (§2.7). The
  resolver is built once in the composition root; the dry-run kit resolves through the same class.

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

The conversation sits in the system prompt as lines `User (2026-10-22 Thu 12:00 CEST): …` / `Assistant (…): …`: every
time is rendered in the **owner's zone** with year and weekday, by the formatter Smart's own message stamps use (§2.7), and an
unreadable time renders empty. The user message is "Begin deliberate consolidation." plus the line
`Today: <date> (<zone>, the owner's timezone).`, which every pass's user message carries (`consolidation_agent.py`,
`_run_consolidation_loop`). The token's 8 steps: extract candidates → classify on the four axes → **search
once per candidate** → analyse → decide → execute → verify → report (`CONSOLIDATION_COGNITIVE_PROCESS.groovy`).

| Tool | What the code does (`consolidation_agent.py:1200-1318`, adapter `fact_management_adapter.py`) |
|---|---|
| `search_existing_facts(keywords, primary_query, alternative_query, limit=20)` | `enrich_context` with 6 vector channels (phrase 2 = alternative, else primary), per-channel limits 7/10/10, total `limit`, **no semantic dedup** (`fact_management_adapter.py:85-96`); returns id, text, domain, temporal class, state, priority, tags, metadata, reported date (ISO, owner's offset), `expiry`, `due`, version. `similarity` is **null** for every vector hit (§4.3) |
| `create_fact(content, fact_attributes)` | requires `domain`, `temporal_class`, `context_priority` and `expiry` (pinned facts and a closing state excepted, §2.7) (`fact_management_adapter.py:159-167`); state defaults to `current`, priority to `medium`; optional `derived_from` on cleanup passes; written **without deduplication** (`skip_deduplication=True`, `fact_management_adapter.py:174-179`) |
| `update_fact(fact_id, updates)` | a content change writes a new version (and needs `expiry`) and returns its id; a state, date or `expiry`-only change edits the document in place; a stale id is followed forward; `superseded_by` is required with state `superseded`; `stale` is refused (§2.4, §2.7, `fact_management_adapter.py:218-302`) |
| `merge_facts(fact_ids, merged_content, fact_attributes)` | one transaction: the merged fact (new lineage, `expiry` required) is created and every source closed with `superseded_by` (§2.4, `fact_management_adapter.py:347-439`) |
| `count_words(text)` | word count against the 40-word fact limit (`consolidation_agent.py:1286-1297`) |

**Deduplication is the model's decision.** Code does not reject a near-duplicate on this path; the prompt makes the
model search first and DISCARD (no tool call) what is already known. The number-aware 0.96 dedup of
`add_fact_if_unique` (`firestore_repo.py:622-713`) runs only on the legacy v2 path (`prompt_version="v2"`), which is
not live.

**Rules that decide what is stored** (prompt tokens, mirrored in `prompts_snapshot/tokens/system/`):
- `Domain_Scope` (POLICIES): never process ASSISTANT statements as facts unless the USER confirms with new
  information; never store facts from questions. **The one exception** (S4): a USER message that begins
  `[Reply to memory review q=<id>]` is the owner answering Alek's memory-review question, framed by the earlier pair
  `[System: memory review q=<id> …]`. The quoted fact belongs to the review, which applies the answer, so Stage 1 does not
  update, extend, invalidate or re-create it from that reply. It records only what the answer adds: a correction that names
  the true value becomes a fact about that value; any other new information follows the ordinary rules; a digit, "yes", "no",
  "stopped" or "don't remember" add nothing. The question itself (the quote and the options) stays an echo and is discarded.
  Other lines point to this one definition (the fallback line of POLICIES, the RAG-echo line and the negation line of
  TAXONOMY), and worked examples in EXAMPLES show it (a correction, a bare digit, and a plain message with no marker). A bare digit sent as an ordinary message, with no
  marker, is read by the ordinary rules; it states no fact.
- `Trivial_Exclusions` (TAXONOMY): no logistics, outbursts, chitchat, meta talk, UI commands, unanswered questions,
  file operations, and the **RAG echo trap** — the assistant recalls a fact and the user only confirms it: discard.
  Test: "relevant in 30+ days?"
- Conflict rules: time series (health, finance, skill) update with history in the text; core identity
  (biographical, preference) needs explicit correction; negations → `update_fact` to `invalidated` ("never true") or
  `archived` ("no longer true").
- `Size_Triggers_Review`: an existing fact over 40 words is split before an update (create the atoms, then mark the old one
  `superseded` **with `superseded_by`**; the S3 tokens name the successor in every place that retires a fact this way, and a
  bare `SUPERSEDED` call is still refused, §6).
- `Directive_Maintenance` (TAXONOMY): `agent_directive` only when the rule is about **how the agent behaves** AND is
  **in force across most requests**; a conditional agent rule becomes `preference` (§4.2).
- **Expiry and dating (S3, TAXONOMY `expiry`, TOOLS, EXAMPLES, COGNITIVE_PROCESS):** the kinds table and its date rules
  (compare every date with `Today`, not with the message time; a range → its last day, a month → its last day, a year → `none`
  unless a deadline or validity end in that year is stated, then Dec 31; a horizon counts from `reported_date`, so
  `reported_date + N` must end at least two days after Today, else a larger N, history or DISCARD); the extension rule for an
  existing fact said again (§2.7); `reported_date` as the message's time in ISO with its offset; every create, merge and
  content-update example carries an `expiry`. The owner's rule behind them: when unsure, keep the fact.

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
compounds, fix inconsistencies, no fact over 40 words, keep numbers and dates (`_build_cluster_message`). It sees no conversation.
Each cluster entry shows `fact_id`, `content`, `similarity`, `expiry` (the stored decision in the shape the model sends, or
`null`), `due` (a horizon's end date) and `reported` (the local date of `reported_date`). The message says that a horizon
counts from `reported`, that this pass cannot change `reported`, that `expiry` is given only on a create, merge or rewrite made
in this pass (facts showing `expiry: null` are classified separately, so the model does not classify them ad hoc), and that
each part of a split fact gets `derived_from: <the split fact's id>`. This is a cleanup pass: a due horizon is floored to now
+ 2 days (§2.7), and the stored `reported_date` is kept.

### Step 7 — Stage 2b: the directive rulebook (every run)

Unconditional, even when Stage 1 did nothing (`consolidation_agent.py:836`); a no-op only when the rulebook is
empty. It reads up to 40 current non-archival `agent_directive` facts (`_review_directives`, `:846-893`) and sends the whole rulebook with the "STANDING DIRECTIVES REVIEW" message
(`_build_directive_review_message`, `:1081-1168`): one checklist line per record (`scope_ok`, `reminder_dup`, `unusable`), then per record DEMOTE
(create a `preference` fact stating the condition + invalidate the directive), INVALIDATE, MERGE, or rewrite to the
target form (imperative, English, terse, one rule), hard cap 15. The message names `fact_attributes={…}` in its DEMOTE and
MERGE lines: a merge carries `expiry: {kind: "none"}` (directives are pinned), a demotion's `create_fact` carries an `expiry`
decided from the rewritten preference and `derived_from` naming the directive (so it keeps the directive's date). Then the
**code backstop** `_enforce_directive_cap` (through the run's tracker): if more than 15 current directives remain, the ones
after the 15th in (priority, newest first) order are invalidated (reason `directive cap`); a result that is not `updated` is
logged at ERROR, reported as no op, and posted as one 🚨 (before S3 it was ignored). Run-to-run results differ on identical input (`decisions/directive_applicability_gate.md` § Variance).

### Step 8 — Stage 3: indexed emails

Runs only after Stage 1–2 succeeded (`consolidation_agent.py:638-712`). **Best-effort:** it runs under what is left of
the run's own budget (the message timeout, which the service derives from the lease, at most 15 minutes) minus a 30 s margin (`_stage3_budget_s`, `consolidation_agent.py:714-723`, `_STAGE3_MARGIN_S`); with under 30 s left it is
skipped with an INFO line. A Stage 3 error or its timeout is logged as one ERROR line (`Stage 3 (email) failed` /
`timed out`) and the batch still succeeds: Stage 1 does not re-run, and the emails stay unconsolidated for the next run.
Cancellation is not caught. Up to 100 unconsolidated `IndexedEmail` facts (1 pass,
`agent_config.py:186-187`) go to a fresh loop as a numbered candidate list under a "[system_alert] … scanned the
user's email inbox" message; afterwards all of them are marked consolidated, whatever the model did (`consolidation_agent.py`,
`_handle_consolidate_email`). Each candidate's `date` is the email's date as the owner-zone moment with its offset
(`2026-10-23T01:30:00+02:00`; the stored date is naive UTC), and the message tells the model to copy it, as given, into
`reported_date` and that information whose date or horizon has already passed is history or noise. The pass's span is the
oldest and newest candidate date, so a copied date is kept and a missing or stray one is clamped (§2.7).
Skipped when the repository is not wired or nothing is pending.

### Step 9 — the baseline is rebuilt; the batch is deleted

After Stage 1–2b, `refresh_biographical_context_cache` rebuilds the `{prefix}user_context` document
(`consolidation_agent.py:346-352`, `firestore_repo.py:715-779`); Stage 3 rebuilds it again when it processed emails.
The refresh logs and swallows its own errors. `ConsolidationService` then deletes the batch (§3 step 3). The Cabinet
invalidate (§5.2) and the janitor (§5.5, for each account it changed) call the same refresh.

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

`ConsolidationPassStats` (`src/domain/consolidation_pass_stats.py`) counts each pass's tool calls. The agent turns each
call into a typed `PassCall` through the write path's parsers (so `" id"` and `"id"` are one source), and renders the
lines (`consolidation_agent.py`: no arguments, no fact text, 8 characters of the user id):
- 🚨 permanent: a pass that uses all 15 turns without a final report logs an ERROR and posts to the ops sink
  (`consolidation_agent.py`, `_run_consolidation_loop`). The pass returns no operations; the writes it made stay.
- 📊 temporary: while `CONSOLIDATION_STATS_ALERTS` is on (`os.getenv`, default on), every pass that ends with a report
  posts one line: pass label (`stage1`, `stage2a`, `stage2b`, `email`, `cluster`), model, effort, turns, tool counts,
  repeated turns, writes, **tool-error turns**, report ops, seconds, per-turn sequence.
- **Tool-error turns (S3).** Both lines carry `tool-error turns N (expiry N[, born due N], abandoned M)`. A turn is a
  tool-error turn when any of its calls failed (`status` failed or not found, an `error` key, or a refusal over `expiry`);
  `expiry` counts the refusals over a missing or malformed `expiry` and the born-due ones; `born due` is shown only when
  non-zero. On Stage 1 and email they are never abandoned (the refusal text lets the model discard the candidate); on
  `stage2a`, `stage2b` and `cluster` they can be (next bullet, S3-D12).
- **Abandoned refusals, 🚨 permanent, also with the stats line off.** A refused create or merge (missing or malformed
  `expiry`, or, on a cleanup pass, born due) is abandoned when no later successful create or merge in the same pass has the
  same text; a refused update, when no later successful, non-closing update targets the same fact (retiring the fact without
  its new text is the loss, not an answer). A **born-due** refusal on a cleanup pass is also answered by the rewrite it asks
  for, which has other text: a later successful merge of the same set of `fact_ids`; for a create, a later successful
  create of the same domain in which **more than half of the refused fact's content words** appear (digit-only
  tokens, tokens under 3 characters, a short stop list, the subjects "user"/"owner" and the planning words "plans",
  "intends", "scheduled", "going", "starts"… do not count, because a past-tense rewrite drops them; the words the rewrite
  adds cost nothing). A create made from facts (`derived_from`) is answered only by one from the same set that also
  passes the text rule, so a sibling split part never answers a dropped one. Residual, on both sides: a near-duplicate of
  the same subject (the same kind of event with another date or place, e.g. a Lisbon trip vs a Porto trip) is taken as the
  answer, so a dropped fact goes unseen; a rewrite that keeps half or fewer of the content words alerts. The comparisons
  use hashes held in memory, never logged. `abandoned > 0` posts "🚨 Consolidation left expiry
  refusals unanswered" with the pass label, model, tool names and text lengths. It errs towards an alert: a retry that
  rewrites the text counts as abandoned.
- **The review pass (Facts S4, §5.6).** `_run_consolidation_loop(pass_kind="review", max_turns=review_max_turns (15),
  review_scope=FactReviewScope)` offers three tools (`update_fact` with `basis` and `history` and no `reported_date`,
  `ask_owner`, `count_words`); `update_fact` and `ask_owner` go to the scope, any other tool is unknown, and the pass kind
  must match the actor. A review refusal (`refused`: `basis`, `ask`; outcome `review_refusal`) and a scope refusal
  (`refused`: `scope`; outcome `scope_refusal`) are shown together as `review refusals N` in the tool-error part. Only an
  expiry, born-due or scope refusal can be abandoned in the review: a basis or ask refusal leaves the fact kept (the
  review reports it as kept). The review gives each fact one result (`PassActor.one_result_per_fact`), so any successful
  update or ask of the fact anywhere in the pass, before or after the refusal and closing included, answers it; a
  re-sent archive or a write after an ask is no loss. An abandoned scope refusal makes the 🚨 read "left refusals
  unanswered".
- **Close-out.** The abandoned 🚨 and the add of the pass's refused calls to the account's day (`expiry_tool_errors`; the
  review's expiry and review refusals go to `review_refusals` instead, `PassActor.refusals_counter`, F14; §5.5)
  run at the end of every pass, also one that raises, in a `finally`. The close-out is best effort and capped at 10 s
  (`CLOSE_OUT_TIMEOUT_S`): it never raises and never changes the pass, a hung sink delays a timeout by at most 10 s, and a
  cancellation propagates.

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

**When it changes:** at the end of a consolidation run (§3 step 9), right after a Cabinet invalidate (§5.2), after a
janitor run that staled a fact of the account (§5.5): a stale fact is not `current`, so it leaves the baseline, the
prefetch and `search_memory` together; and after a review run or an owner's answer that wrote a fact (§5.6): a fact brought
back, queued for a question (it is `current` again) or set aside changes what the next message sees. No reader of the baseline
shows an expiry field.

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
`find_nearest` calls at 30 (`firestore_repo.py:50`). Since S3 the result (`EnrichedFact`) also carries `expiry_kind`,
`ttl_days` and `expires_at` (the last as an ISO string, so the Router's context stays JSON-serialisable for Cloud Task
payloads); only the consolidator's search dict renders them (§2.7), and Smart, `search_memory`, Lelik, the connector and the
Cabinet show none.

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

Read routes (`src/web/user_cabinet_app.py`): all current facts; a paginated browser, newest first, optional
domain filter and a state filter, `current` (the default) or `stale`; a single-vector search over `vector` (50 results).
They show text, domain, type and created date. Since S4 the page has three views and a question list (§5.6):
- **Current** (the browser as before).
- **Waiting for review**: the stale facts (`state=stale`), each with its `stale_since`, read-only.
- **Set aside lately**: `GET /api/user/facts/recently-closed`, everything archived or invalidated in the last 30 days (not
  only what the digest listed), newest closure first, each with its state and the actor of its last trail entry. Read-only: the Cabinet cannot bring a closed fact back. To bring one
  back, the owner tells the bot in chat that it is still true.
- **Questions**: the open memory-review questions with answer buttons, the options of each question's kind (§5.6); since P2
  revised (not deployed) also the group "Invoices with no payment confirmation" (Paid / Not paid, in bulk).
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

`POST /api/user/facts/<id>/invalidate` (`user_cabinet_app.py`) → `CabinetFactService.invalidate`
(`services/cabinet_fact_service.py`; the rule itself is `cabinet_invalidation` in `domain/fact_lifecycle.py`, the walk is
`domain/fact_resolution.py::ForwardWalk`): the id must exist (else 404) and its stored account must be the caller's
(else 403; the write itself refuses with 403 when the stored `account_id` is not the caller's, so a legacy `owner_id`
document is refused too). Then the service **follows the id forward** through the repository's `resolve_current` to the fact that is live now (`CABINET_ACCEPTS`: every state except `superseded`, so the owner
can still delete a fact the consolidator archived meanwhile) and sets `state=invalidated`, `valid_to`, `last_updated` and a `lifecycle_events` entry (actor `cabinet`, reason `deleted in
the Cabinet`) on that document, in place, under the `update_time` precondition. A page loaded before the consolidator versioned the
fact therefore deletes the **current version**, not a closed document (before S2 it would have re-opened the old
document and left two `is_current` documents in one lineage), **but only when that version still has the text the
page showed**: a closed version keeps its own text, so the service compares it with the text of the id the page
sent. On a conflict it resolves once more, and the text check runs again.
Search stops returning the fact at once. **The service then rebuilds the baseline** with
`refresh_biographical_context_cache(owner_id=account_id)` (§3 step 9), so the fact leaves the next prompt too. That
method never raises: it logs `❌ [FirestoreRepo] Failed to refresh biographical cache for <account>: …` (ERROR) and
returns, the invalidate still answers 200, and the baseline keeps the fact until the next consolidation run. (The route
once had its own `Fact <id> invalidated but the biographical cache refresh failed` guard; it could never fire and is
gone, so grep the repository's line.) The route's own log line (`🗑️ Fact <id> invalidated by user …`) names the id the
page sent, not the version that was invalidated.

**When the fact was rewritten meanwhile (owner decision 2026-10-23).** If the consolidator rewrote the fact after the
page loaded (a content change, so the id moved on) and the new version's text differs from the row the owner saw, the
delete is **refused** with a 409 (`FactResolutionError` "fact X was rewritten after it was shown"; the route adds
"; reload" to every 409 body, the page shows its own text) and nothing is written; the page reloads and the owner sees the new text before deciding
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
place: same id, no version, no re-embed), with the actor `backstop` and the trail reason `directive cap`. A refused or failed
update is no longer swallowed: it is logged at ERROR with the fact id prefix, reported as no op, and posted as one 🚨 per backstop run
naming how many updates were not applied.

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

### 5.5 The janitor (`fact_lifecycle_sweep`)

A zero-LLM daily job that enforces the expiry decisions of §2.7 and asks for a review when one is due. Cloud Scheduler job
`alek-bot-dev-fact-lifecycle-sweep`, `30 9 * * *` Europe/Madrid (after the 09:00 billing summary),
`POST /worker {"task_type": "fact_lifecycle_sweep"}`, 180 s attempt deadline, created by the deploy
(`docs/07_deployment/SCHEDULERS.md`). Code: `FactLifecycleSweepService` (`src/services/fact_lifecycle_sweep_service.py`), the
plan in `src/domain/fact_lifecycle_sweep.py`, the alerts in `fact_lifecycle_alerts.py`, the review part of the line in
`fact_lifecycle_review_line.py`.

**One run.**
0. *Run guard.* Before it reads anything the run takes the guard (`{prefix}fact_lifecycle_run`, document `sweep`, held for
   5 minutes, taken in a transaction) and frees it in a `finally`. A run that finds it held does nothing and answers 200
   `{"status": "busy"}`; a guard that was never freed expires by itself. So two janitor runs never overlap (a hand run during
   the scheduled one is now harmless).
1. *Due:* `find_due_facts(now)` returns every document with `state == "current"` and `expires_at <= now`, collection-wide,
   projected to account, `expires_at`, `expiry_kind` and the trail (no text, no vectors) with its snapshot `update_time`. It
   needs the composite index `(state, expires_at)`. A due document **with no `expiry_kind`** (the old revision after a rollback,
   a hand edit) is skipped and counted `no_decision`, never staled; a document without an `account_id` is skipped and logged.
2. *Backlog and reads:* `stale_backlog()` counts the `stale` documents per account and the oldest `stale_since` (a stale fact
   with no `stale_since` counts but has no age). The review store gives every open question and every account's review state.
3. *Accounts visited:* every account with a due fact, a stale backlog, a review state, an open question, or a review figure
   or `reply_intake_failed` in yesterday's counters, so that its alerts and its review are judged.
4. *Per account: skip, pool, breaker.* The facts of the account's open questions (`queued`, `pending`, `asked`,
   `unanswered`) are taken out of the due facts **in both modes**: they are not staled and are not counted as due, deferred or
   would-stale; the line shows them as `due_waiting`. Such a fact is `current` with a past `expires_at` until its answer or its
   fallback gives it a new horizon or closes it (§5.6). The **pool** is the stale facts plus the facts of open questions.
   `breaker_reason` trips when the account has stale facts and no successful review within the review interval + 72 h (4 days at
   the daily default; no review yet counts as too old), or when the pool is over 50. A tripped breaker stales nothing for that
   account and posts one 🚨 per account per run, **only when a due fact with a decision was actually held back**; the snapshot and
   the morning line say `tripped` either way.
5. *Per-run cap (owner, S3-D10).* An account that is not tripped gets at most `50 − pool` stales per run, **oldest expiry
   first** (then by id); the rest is counted `deferred` and waits for the next run (a conflict does not use the headroom). So
   stale facts plus waiting questions never pass 50: at a pool of 50 nothing is staled and everything due is deferred until
   questions are answered or timed out.
6. *Stale:* the service builds the write (`stale_fields`, from the trail the snapshot projected) and `stale_due_fact`
   applies it in one transaction under the snapshot's `update_time`: `state=stale`, `is_current` true, `valid_to` none,
   `stale_since` and `last_updated` = now, and the trail entry `{current → stale, actor janitor, reason "expired <ISO>"}`. A
   changed or deleted document is a conflict (`FactWriteConflict`), skipped and counted until the next run; any other error stops
   the run (below). It never archives, deletes or reads text, and takes no lease.
7. *Review enqueue* (log-only mode too, so the review works from the deploy). `review_due` names the first reason that holds:
   `stale` (stale facts after this run, and as many owner-local days since the last successful review as the account's
   interval; none yet counts as elapsed), `reply` (a reply in words nobody has read), `timeout` (a question past its 14 days),
   `pending` (a send that never finished), `digest` (due), `queue` (a queued question that may go out). Then one `fact_review`
   task `{user_id: the account's first owner, account_id, mode: "daily"}` is enqueued with an 1800 s deadline and a name made of
   the account and the owner's local date, and `enqueued_at` is set (which starts `unsuccessful_since`). No owner or no task
   queue is a WARNING and nothing enqueued. The interval is `fact_review_interval_days` (the user's, else the account default,
   else `DEFAULT_FACT_REVIEW_INTERVAL_DAYS` = 1), so a user's value needs no deploy. Cloud Tasks keeps a task name only while the
   task is queued or running and for about an hour after, so the name merges runs that start close together; "one review a
   local day" comes from the interval rule, not from the name.
8. *Alerts:* one 🚨 per account per run (log-only too), listing every condition that holds: the pool over 50 (each run); the
   oldest stale fact older than the interval + 2 days (when it starts, then weekly, and cleared when it stops holding); more
   than 35 queued questions (once, re-armed when the queue is back to 35 or fewer); no successful review for more than 48 h
   since the first one asked for (each run); yesterday's `reply_intake_failed` above zero; yesterday's refused expiry calls above
   10; the breaker line. The marks the repeat rules need are written before the post, so a lost post is not repeated. Once
   per run, one more 🚨 when closed review records are still stored more than 3 days past their `purge_after` (the TTL policy
   is late or missing). The review service itself posts one when a fact is revived a third time within 30 days.
9. *Baseline:* each account that staled anything gets `refresh_biographical_context_cache`, so the fact leaves every prompt
   (§4.1).
10. *Snapshot and line:* one snapshot per account is stored (below), one log line per account
   (`[FactLifecycle] account=<8> due=N newly_due=N no_decision=N staled=N would_stale=N deferred=N conflicts=N backlog=N
   breaker=<ok|reason> review_enqueued=<0|1> dry_run=<bool>`), and the 🧹 line is posted once (below). The job returns
   `{accounts, due, staled, would_stale, deferred, conflicts, no_decision, breaker_tripped, review_enqueued, purge_overdue,
   dry_run}`, or `{"status": "busy"}`.

**Log-only is the code default; the deploy sets the variable, so the janitor writes since 2026-10-27.** `FACT_LIFECYCLE_SWEEP_DRY_RUN` (`os.getenv` in `build_application_graph`, not in
`load_settings()`) is log-only unless it is an explicit off value (`0`, `false`, `no`, `off`); empty or unset or anything else
stays log-only. The startup line says `fact lifecycle sweep: log-only` or `: writes`. In log-only mode nothing is written to
facts or the cache: `would_stale` counts every due fact with a decision (open questions' facts excepted) whatever the breaker
says, `deferred` is what the cap would hold, the breaker reads `would trip`, and no breaker 🚨 is posted. The snapshot and the
line are written in log-only mode too, because the owner reads them before switching writes on. **The review is not log-only**:
it works on the facts that are already stale, whatever the janitor's mode (§5.6). Nothing in the code switches writes on: the owner did it on 2026-10-27 with `FACT_LIFECYCLE_SWEEP_DRY_RUN=0` in
`cloudbuild-dev.yaml` (a deploy). The rollback lever is removing the variable (a deploy; `--set-env-vars` replaces every variable). S5's classify was applied the same day and the owner's re-review of closed facts applied.

**What it stores and posts.** `{prefix}fact_lifecycle_daily/<account>:<UTC day>` gets one snapshot with the whole field set:
`run_at`, `due`, `newly_due` (due facts whose `expires_at` is after the previous run's `run_at`), `no_decision`, `staled`,
`would_stale`, `deferred`, `stale_conflicts`, `breaker` (`ok`, `tripped`, `would trip`), `backlog` and `oldest_stale_days`
(both measured **before** the run) and `review_enqueued`. The last run of the day wins, so a hand run never double-counts.
The same document carries counters that other code adds to with increments and a snapshot never replaces: the consolidator's
`expiry_tool_errors` (§3.11), the review's `reviewed`, `revived`, `rewritten`, `archived_by_review`, `invalidated`,
`superseded`, `kept`, `queued`, `to_history`, `folded`, `review_failed`, `review_refusals`, `labels_fallback`, and the question
flow's `fallbacks_not_applied` (P2 revised: a silence's history write that was refused or raised), `questions_sent`,
`not_delivered`, `answered_by_digit`, `answered_by_button`, `answered_in_cabinet`, `answers_not_applied`, `closed_by_reply`,
`timed_out`, `replies_in_words`, `already_answered`, `reply_intake_failed` (§5.6). After the run, **once a day** (the day's mark
in `{prefix}fact_lifecycle_lines` is claimed before the post, so a failed post loses the line and never doubles it) a single
ops message goes to the ops sink when some figure is non-zero or a breaker is not ok: `🧹 Fact lifecycle — run
<YYYY-MM-DD HH:MM> UTC[ · log-only]`, then, when closed review records are overdue, `purge overdue N`, then one line per account:
`<8>: due N (new N, no decision N) | staled N / would stale N / deferred N | conflicts N | breaker ok/tripped/would trip | backlog N
(oldest Nd) | expiry tool errors yesterday N`, followed by the review segments, each only when one of its figures is above zero:
`| review: reviewed N; counted on their own: revived N, rewritten N, archived N, invalidated N, superseded N, kept N, queued N,
to history N, folded N`,
`| questions sent N, answered by digit N, by button N, in the Cabinet N, by reply N, timed out N, queue N, unanswered N,
due_waiting N`, `| review failed N, refusals N, fallbacks not applied N`, `| replies not recorded N` and a closing `| enqueued 0|1`. The counters in the
segments are **yesterday's** (the day the review actually ran), the `queue`, `unanswered` and `due_waiting` figures are this
run's. The buckets of the review segment are counters of their own, not a breakdown of `reviewed` (a `kept` fact can also be
`timed out`, and `queued` also counts a kept-twice question; `to history` counts the review's history rewrites, which are
in `rewritten` too, and a silence's history, not an owner's answer). The `queue` and `unanswered` figures and the "more than 35 queued" alert count chat
questions only: a digest question (P2 revised) is never sent and is never in the queue or the pool.

**Failure and overlap.** Every ops post is capped at 10 s (a hung sink holds neither the run nor its error). A failure other
than a write conflict (a query, a counters read or write, a stale write) is logged with its type and the stage reached, posts
one "🚨 Fact lifecycle run failed (<Type>) at <stage>" naming no fact, re-raises, and the job answers 500. **The job is not
retried, by design**: a retry after the 180 s deadline would run beside a run still going; the run guard now makes that
harmless (the second run answers `busy`), but a retry would also find the guard held, so the next day's run catches up. The
guard frees itself after 5 minutes. The 🧹 line is claimed per **UTC day**: a hand run before 09:30 Europe/Madrid on the same
UTC day takes the day's line, and the scheduled run then posts none (its numbers are in the snapshot).

**Never a TTL policy.** A Firestore TTL policy deletes documents when a field passes. `expires_at` on a fact is a decision to
stale it, so no policy may exist for the facts collection, the daily collection, the line collection or the run-guard
collection (a test walks the tree and fails on one; `decisions/fact_expiry_and_janitor.md`). The two review collections
that expire carry a different field, `purge_after` (§5.6).

### 5.6 The review pass, the question queue and the owner's answers (Facts S4)

Decision record: `decisions/fact_review_and_owner_questions.md`. Spec: `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D6, D7 and "S4 as
built", and "P2 revised" in §9 for what this section marks **P2** (owner, 2026-10-27; built, not deployed, and the janitor is
not switched on before it ships). The owner's rule through all of it: **when unsure, keep the fact, and ask.** P2 adds a second:
**anything that really happened is a fact, and so is an intention**, so a fact whose date passed becomes history, never an
archive; archive is kept for noise, an end the owner states, and the Cabinet.

**What a stale fact becomes.** The janitor hides a fact whose expiry passed. The review decides what it is now. One
`fact_review` run per account per day (when something needs it, §5.5 step 7) does, in order, under the account's
memory-write lease (the one consolidation holds, so the two never write at once; busy → the run defers itself by 120 s with a
named task):
1. *Owner check.* The user must be the account's first owner, and the profile must belong to the account; else a WARNING and
   `not_owner`. Questions, sessions, language and timezone are the owner's; facts are the account's.
2. *Housekeeping.* A question whose send never finished is completed (it has a message reference: it was posted) or taken
   back (it has none: a "not delivered" pair is appended and the question goes back to the front of the queue). An asked
   question sent on an earlier local day becomes `unanswered`: still open, still timing out, no longer the current one.
3. *Timeouts, in code.* A question sent (`asked`, `unanswered`) 14 days ago is closed by silence. If its fact is still the
   same document: when the owner answered in words that the review has not read, or a fact created since the question looks
   like an answer (one of the five current facts nearest to it), the fact goes to the model once; otherwise its question's
   **fallback** applies at once (below). A fact closed or rewritten meanwhile closes the question `resolved_elsewhere`.
4. *Kept twice.* A stale fact the model left undecided twice (`offers` in the state) gets a code question and is queued. Code
   never revives a fact on its own. **P2:** for a `date` fact that question is `happened` with fallback `history` and texts
   built by code at the answer's or the silence's moment (below); any other fact keeps the S4 `still_true` with `revive`.
5. *Selection.* Up to 20 facts: first those with a reply in words not yet read and the timed-out candidates, then the stale
   facts, those offered least first and the longest stale first. A fact with an open question is the question's business and is
   left out.
6. *The model pass* (`review_stale_facts`, label `review`): nothing selected means no model call. The agent is the consolidation
   agent with its own profile `consolidation_review` (blueprint `consolidation_review_v1`; tokens `CONSOLIDATION_TAXONOMY` and
   `CONSOLIDATION_LIFECYCLE`), no baseline conversation, at most 15 turns. It sees, per fact, built by code: the text, domain,
   expiry, `reported`, `stale_since`, its earlier versions, its five nearest current neighbours (its own lineage left out) and, when
   there is one, its question with the owner's reply in words. In mirror mode it also sees the owner's last ten messages (up to
   1,500 characters, system notes left out) so that it can report their language. Every listed fact must end with exactly one
   outcome, decided by the first rule that applies: **supersede** (a current fact says the same), **revive** (a reply or other
   evidence says it still holds; rewritten if the text changed), **history** (**P2**: its date passed; rewritten as what happened,
   what was planned and did not happen, or what was planned with an unknown outcome, on evidence, on its own text or on a reply),
   **archive** (the owner's reply says it is over; before P2 also evidence or the passed date of a dated plan), **invalidate**
   (it was never true), else **ask** the owner. A fact the pass cannot decide and has
   already asked about is *left*.
7. *The tools* are three (`_get_tool_declarations("review")`): `update_fact(fact_id, updates, basis?, history?, as_history?,
   payment?)`, `ask_owner(fact_id, fallback, question?, texts?, folds?, noise?)` and `count_words` (**P2**; before it `ask_owner`
   took `fact_id, fallback, fallback_date?, noise?`, and `fallback_date` is now refused). There is no search, create or merge tool. `FactReviewScope` (one
   `asyncio.Lock`, so calls apply in the order the model wrote them) routes them and refuses what the rules refuse, each
   refusal naming the argument and its fix and tagged `basis`, `scope` or `ask`:
   - **scope**: a fact outside the batch, or one that already has a result in this pass, is refused;
   - **basis**: an invalidate or a rewrite needs evidence (another current fact, never a version of the same fact) or the
     owner's reply (with a quote copied from it); **an archive needs the owner's reply** (**P2**; before it, evidence or
     `date_passed` too); a **history rewrite** (`as_history: true` with `updates {state: CURRENT, content, expiry: {kind:
     none}}`) takes evidence, a reply or `date_passed` (a `date` fact uses its own date; any other kind needs a date before today
     in the owner's zone), and `date_passed` backs nothing else: it is the fact's **own text** reporting the outcome (an
     invoice received, a payment made, a subscription cancelled), never "it took place" or "it was paid"; **a revive needs
     evidence or a reply, and is refused otherwise** ("ask the owner instead"); a supersede names a current fact, or a fact this
     pass made current; a fact this pass rewrote as history on `date_passed` is no evidence and no successor;
   - **history** (the S4 "ended" shape): an archive on a reply that says a standing thing ended may carry one past-tense
     sentence, which code creates as a separate fact (`{kind: none}`, its own lineage, same domain and tags) in the same
     transaction. Refused on a `date` fact (rewrite it with `as_history`) and on noise (archived only), and with a `CURRENT`
     rewrite ("send as_history: true");
   - **payment** (**P2**): with an `as_history` rewrite of a routine invoice that was received and is not confirmed paid
     ("An invoice for the phone bill (€24), due Nov 10, was received; payment not confirmed."), the two texts `paid` and
     `not_paid` code keeps for the owner's later tick. An invoice is never a payment: payment is confirmed only by evidence or
     the owner. A fine or a debt is asked instead (`question: paid`);
   - **ask**: one fact, no question text (code writes the question), a `question` kind and the `texts` code writes for each
     answer (**P2**, below), and a `fallback`: `revive` (the default, `still_true` only), `history` (the question's "unknown"
     text written as history; the fallback of `happened` and `renewed`, and the only one of `paid`) or `archive` (only with
     `noise: true`, on any kind but `paid`). A
     `date` fact is asked `happened`, `renewed` or `paid`, or `still_true` with `archive` and noise. The texts are written in
     the same turn with no `count_words` call: code checks each (one line, no `[`, at most 40 words, not the same as the
     fact's stored text) and refuses the ask naming the one that failed. `folds` names other batch facts that are parts of the same plan (the flights, the hotel and the
     transfer of one trip): one question for all of them, its texts describing the whole plan. Noise never archives anything
     on its own; it only sets what silence does.
   The model never archives a fact as noise by itself, and a refused call writes nothing.
8. *After the pass.* Outcomes remove the fact's `offers` entry and close its question `review`. Each ask makes the fact
   `current` again under its precondition (state `current`, `stale_since` cleared, the expiry as stored) and creates a `queued`
   question that quotes the fact and takes its place in line from the fact's `stale_since`. **P2:** only after an ask is
   queued are its folds superseded by the asked fact (a failed queue folds nothing; the folds stay stale for the next run), and
   each routine invoice's history version gets a `paid` **digest question** (delivery `digest`, never sent in chat, answered in
   the Cabinet, closed `resolved_elsewhere` when its fact changes or after 90 days). The stale facts are put before the model
   by (offers, `stale_since`, `expires_at`, id), so the parts of one plan, staled in one janitor run with the same last day,
   share a batch. A timed-out candidate left
   undecided gets its fallback. A conflicting fact write leaves the fact stale and creates no question.
9. *Send* the next question if one may go (below); *refresh* the baseline cache once if any fact was written; *digest* if due
   (below); add the counters, post the revive alert (a fact revived a third time in 30 days), and mark the run (`last_review_at`
   and a cleared `unsuccessful_since` on success; `last_failed_at` on a failed pass). **A failed review pass** (the agent failed
   or raised) still sends the next question and the digest; its earlier writes stand; the run answers 200 `failed`, with no Cloud
   Tasks retry, and the janitor enqueues again the next day.

**The fact is current while it waits.** A queued fact is an ordinary current fact for every reader and keeps its past
`expires_at`; the janitor skips it while its question is open. This is the owner's rule: a question never hides a fact, and an
unanswered one never silently revives it. The cost is that the pool (stale + waiting facts) is bounded at 50 (§5.5).

**One question at a time.** The queue holds `queued` questions ordered by how long their facts have been stale. There is one
current question per user (`current_question_id` in the state). The next goes out only when the current one is closed (a
digit, a button, a reply the review has handled, a Cabinet click) or, if the owner does not answer, on the next local day (the
old one becomes `unanswered` and keeps its timeout). At most 5 are sent per owner-local day and none on a day when a delivery
failed. There are no quiet hours in code: the chat's own notification settings decide. Questions go **only to the owner's primary
channel**; with no primary channel, or one bound to a companion, nothing is sent and the queue waits (the Cabinet can answer it).
A queued question never times out. An invoice digest question (**P2**) is never in this queue, the pool or the janitor's skip:
its fact is a current history with `none`. A send is: claim the first queued question in one transaction on the state (no current
question, today's count under 5); append the pair to the session (user part `[System: memory review q=<id> — Alek asked the owner
about a fact he could not confirm]`, model part the question, `text == full_text`, raising when not stored); post; store the
message reference; mark `asked`. A pair that cannot be appended puts the question back at the front; a post that fails appends a
second pair (`[System: memory review q=<id> not delivered — …]` and `(not delivered)`) and puts it back, and that day sends no more.
The message is code-built: the question line of its kind, the fact quoted as the owner is shown it (translated into the
message language when the review stored a translation, else as stored), the options of its kind as digits, then a hint that a
reply in words works too. **P2:** the kind decides the line and the options (before P2 every question was the first row):

| Kind | For | Line | Options |
|---|---|---|---|
| `still_true` | everything S4 asks | Is this still true? | 1 Yes, 2 No longer, 3 Never, 4 Don't remember |
| `happened` | a plan or event whose date passed (a `date` fact, or a horizon plan such as a pickup) | Did this happen? | 1 It happened, 2 It did not, 3 Don't remember |
| `renewed` | a validity end that changes the current fact (a document, a permit, a membership, a trial) | Was this renewed? | 1 Renewed, 2 Not renewed, 3 No longer needed, 4 Don't remember |
| `paid` (chat) | a fine or a debt whose due day passed | Was this paid? | 1 Paid, 2 Not paid, 3 Don't remember |
| `paid` (digest) | a routine invoice already written "payment not confirmed" | never sent in chat | the Cabinet's Paid / Not paid |

**The language of the message.** The owner's response-language setting decides: a fixed language gives that language
(uk, en, fr, es); mirror mode gives the language of his recent messages, which the review pass reports as `message_language`.
The 28 texts of a question (**P2**; 15 in S4: the question line, the four option labels, the reply hint, three
acknowledgements, a "fact changed" note, a "closed" note, the digest's header and footer and its two state words; P2 adds the
three other question lines, the seven other option labels and the digest's history header, invoice header and Cabinet line)
come from the UI strings for uk, en, fr and es. For any other language the review model returns them in its report and they are
kept only if the code's checks pass (the exact 28 keys, one line each, within the length limits, no digit, `[` or URL, no
control or invisible characters, option labels of at most four words that differ within each kind); a set that fails is not
used, the UI language is, and `labels_fallback` is counted. The P2 key set changes the labels' digest, so a cached model set
is ignored once until the next pass writes one. A passed set is cached in the state per language, with a digest of the English texts it translates, so a set written
for other texts is ignored. The digits are the options of the question's kind, the same in every language. Each platform's adapter escapes whatever its markup would read as control syntax.

**The fact in the message language (quote translations).** Stored facts are English. A message in another language quotes
the fact translated by the review pass: the pass is told the language (fixed mode: the task's `language`; mirror mode: the
language it reports from the owner's messages) and returns `translations`, one per listed fact, in its final report (no
call, no effect on any outcome). **P2:** it also returns every text it wrote in the pass, under `<fact_id>:<text key>`
(`history` for a rewrite's content, each ask text, each `payment` text), so the digest's history and invoice lines and a later
question about a history fact are shown translated too; a key that names no text the pass wrote is refused. A code-built
kept-twice text is the one text never translated. The code keeps a translation only if it is one line, has no `[`, link or control character,
is at most 500 characters and keeps the source's numbers exactly (the multiset of digit runs); a fact whose own text has a
link or `[` is never translated. A kept one is stored right after the pass (before the closes) under
`{account_id}:quote:{language}:{digest of the English text}`, so the same text under any fact id finds it; it expires 180 days
after it was written. Presentation only: the fact, its id and the question's `quoted_text` stay as stored. The question, its
Slack redraws (after a click, a refused click, a close) and the weekly digest all show the translation when one is stored for
the lines' language (`shown_language`) and still passes the checks, else the stored English text; a failed read shows English
too. The pair appended to the history carries the shown text, so the consolidator reads a translated quote: its echo-trap
exception says the quote may be the stored fact translated (same fact), and Smart's memory-review marker line says the
quote is in the message's language. The Cabinet keeps showing the English text. English messages read no translation.

**How the owner answers.** Only a *reply to the question message* counts (a Slack thread reply on the question's message, a
Telegram reply to its message), or a click, or the Cabinet. Any other message is ordinary conversation and the question stays
open. Four ways, one path (`FactAnswerService.apply`, with its source `digit`, `button` or `cabinet`):
- **A digit as a reply** (the options of the question's kind; for `still_true` 1 yes, 2 no longer, 3 never, 4 don't remember). Taken at intake in `ConversationHandler`, before any
  model call, in an unbound channel, for a text-only message. Applied at once by code. The acknowledgement is posted as a real
  reply to the question message (in its thread on Slack, a Telegram reply on Telegram), as literal text, and the pair
  `[Reply to memory review q=<id>] <text>` plus the acknowledgement is appended to the session.
- **A button (Slack).** One button per option of the question's kind. A click is queued by `POST /slack/interactivity` as an
  `interactive_choice` task (the route verifies the Slack signature on the raw bytes before it parses anything, then answers 200 at
  once and does no more work in the request), and the worker applies it through the same path. The message is then re-rendered with
  the chosen answer and no buttons. A click on a question that is already answered shows its answer. The click must come from the
  question's own user, on the question's own message.
- **A reply in words.** Recorded on the question (up to 2,000 characters), acknowledged ("Noted — I'll take it into account.").
  When it answers the *current* question, the same transaction gives up the slot and the next question is released at once (a
  `next` task; no model involved); the question stays `asked`. The review is shown the words once on its next run, which applies
  them and closes the question; that close finds the slot already free and releases nothing more. It does not go to Smart. It never shortens the 14 days; if the review leaves it undecided, the question waits for its timeout, and the timeout
  step sees the reply.
- **The Cabinet.** The "Questions" list shows each waiting or asked fact in full with one button per deciding option of its
  question (for `still_true`: yes, no longer, never; "don't remember" is never offered there), checkboxes and a bulk "mark
  selected as …" (1 to 50 at once) that offers only the options of one kind at a time. **P2:** the invoice digest questions are
  their own group, "Invoices with no payment confirmation", with Paid / Not paid in bulk. A click is a direct owner action under
  the same preconditions; an answer that is not one of its question's options (a click from another message version, a stale
  page) is `not_applied` before any write.
A reply to a question that is already closed gets "Already answered." An answer to the *current* question, a reply in words included, releases the next one.
Two kinds of message that are not taken as an answer fall through to ordinary conversation, so nothing the owner wrote is
dropped: a reply the system could not take (an error in the path, which is logged, counted as `reply_intake_failed` and alerted the
next morning; or a reply the store would not record) and a digit whose answer the write rules refused. A digit for a fact that
**changed since the question was asked**, or whose write conflicted, is not applied and not sent on: the owner gets the
"That fact has changed in the meantime, so I didn't apply your answer; I'll come back to it." note, and the question stays open
(a button keeps its buttons and shows the note, so he can click again).

**What an answer does** (`domain/fact_review_answers.py`; all against the fact as the owner saw it: a fact rewritten since, or a
current text other than the quoted one, is `changed` and nothing is written):
- *Yes* (still true): the fact is `current` with a confirmed expiry (§2.7); `reported_date` becomes the later of the stored
  date and the answer time.
- *No longer* (ended): a **standing** fact is archived and a separate lasting fact "No longer true (the owner said so in <Mon
  YYYY>): <the old text>" is created beside it in one transaction (the month name is a fixed English tuple, never locale-
  dependent); a **dated plan** (an expiry kind `date`, on a stored S4 question) or **noise** (a question whose fallback is
  archive) is only archived, with no history fact. Since P2 a new question about a dated plan is `happened`, not `still_true`.
- *Never*: the fact is invalidated; no new fact.
- **P2** *It happened*, *It did not*, *No longer needed*, *Paid*, *Not paid*: the question's text for that answer is written as
  history (a version at `archival`, `expiry none`, §2.7). *Renewed*: the fact is rewritten to the `renewed` text, current, with
  a confirmed expiry, its priority kept. *Not renewed*: rewritten to the `not_renewed` text, `expiry none`, priority kept (it is
  the current state). A kept-twice question has no stored texts: code builds them at the answer's moment, keeping the stored
  text whole ("Took place as planned (the owner confirmed in Nov 2026): …", "Did not take place (the owner said so in
  …): …", "Planned; whether it took place is unknown (no answer by …): …").
- *Don't remember*: the question's fallback now (for `history`, its "unknown" text as history, under the owner).
- A correction in words is handled by the review (invalidate, or supersede by the fact that states the true value) and by
  Stage 1, which records the true value as its own fact.
A close from any source goes through one method that clears the current slot, releases the next question and re-renders a
Slack-posted message (the answer, or "This question is closed."); every close, including a review's, a timeout's and a
`resolved_elsewhere`, uses it. Answers take no memory-write lease: the preconditions keep a concurrent consolidation or review
write from being lost and the loser reports `conflict`.

**What silence does.** A question's `fallback` decides, 14 days after it was sent. `revive` (the default for `still_true`): the
fact is brought back with twice the window its last revive gave, 14 to 365 days (§2.7), with no new question until it falls due
again. `archive` (noise; on a stored S4 question also a dated plan or a past `fallback_date`): the fact is archived. **P2**
`history` (`happened`, `renewed`, `paid`): the question's "unknown" text ("The user planned to attend a concert on Dec 5, 2026;
whether it took place is unknown.") is written as history through the typed write path under the `timeout` actor; the fact now
has `none`, so it never falls due and is never asked again, and the weekly digest lists it. A write conflict leaves the question
for the next run; a refused or failing write is a WARNING and `fallbacks_not_applied`, the question stays open with one more
attempt, and it closes `resolved_elsewhere` on a typed refusal after the first attempt or at the third attempt; one bad question
never stops the run. If the history landed and the close did not (a crash between), the next run sees the question's own
timeout reason on the version its fact now resolves to and closes it `timeout`, so the digest still lists it. Every typed write
(the review's, an answer's, the timeout's) names the exact document it decided on (`expected_id`): if the fact moved on
meanwhile the write is refused and nothing is written, so an answer racing a timeout never overwrites the other's history. So an ignored question never silently revives a fact for a short, arbitrary time, never archives a lasting
one, and never loses what was planned.

**The weekly digest.** When the review or an answer sets a fact aside (archive or invalidate), the state records
`undigested_since`. At the end of a review run (after a failed pass too), if something is undigested and the last digest is at
least 7 days old (or there is none), the run claims the owner's ISO week and sends one message by `notify_raw`, as literal
text (stored fact text is shown, never obeyed, and nothing is written to history): a header, one line per fact as the owner is shown it (its translation when one is stored, else as stored)
with its state in words ("no longer true" for an archive, "never was true" for an invalidation), and the footer "To bring one
back, tell me in chat that it is still true." The digest lists only removals made by the review, a timeout or the owner (the
last trail entry's actor), at most 50, read from the facts closed since the last digest (the first digest looks 30 days back).
A fact the owner ended is listed as archived; the history fact written beside it is not. If there is nothing to list, no message
is sent but the week is still claimed and the marks move. A digest that could not be delivered releases its claim and tries again
at the next run. The Cabinet cannot bring a closed fact back; only the chat can.
**P2** adds two blocks, each only when it has lines, and a digest with only them is sent too: **"Recorded as history without your
answer:"**, one line per fact a silence wrote as history since the last digest (its "unknown" text; an owner's answer and a
history the review wrote on evidence or its own text are not listed), and **"Invoices with no payment confirmation:"**, one line
per open invoice digest question (at most 50, oldest first), then "Mark the paid ones in the Cabinet:" and the link to the
Cabinet's Memory tab, appended by code. Every line is shown as the owner is shown any quote: translated when a translation is
stored, else the English text.

**What the owner sees in the Cabinet** (§4.8): the stale facts, everything set aside in the last 30 days with the actor of
each close, and the question list. The question list shows each fact's current text; a fact that was rewritten since its
question (and would answer "changed" if clicked) shows the quoted text the owner was asked about, so nothing unseen is shown or
answered. Every click is checked for the session user, so one user's questions are never listed or answered by another.
**P2:** each question carries its own kind and options; the invoice digest questions are a group of their own, where "Paid" or
"Not paid" writes that text as history over the "payment not confirmed" version.

**Counters, alerts, retention.** §5.5 lists the counters, the alerts and the 🧹 segments. `purge_after` is `closed_at + 90 days`
on a closed question and `claimed_at + 90 days` on a digest claim; it exists only in storage. A Firestore TTL policy on
`purge_after` for `fact_review_questions` and `fact_review_messages` deletes them; without one they stay and the janitor's
run-level 🚨 reports them 3 days past. An open question never has one. The state document has none.

**The cost.** A digit, a button, a Cabinet click, a timeout fallback with nothing to see, a send and a digest cost no model
call (P2's answers and silence included: the texts are written when the question is asked, about $0.01–0.02 more per plan
question). A review pass is one model loop whose fixed prompt is about 25,000 tokens: about $0.08–0.10 (Sonnet 5.5) or $0.15–0.20
(Opus 5.5) for a pass of 2 facts, and about $0.22–0.30 or $0.45–0.60 for a pass of 20 (§8).

---

## 6. Outcomes: tool calls and failures

| Where | What happens | Facts | Batch | Retried? |
|---|---|---|---|---|
| tool call ok | result JSON back to the model | written at once | — | — |
| `search_existing_facts` fails (embedding, Firestore) | the adapter catches everything and returns `[]` (`fact_management_adapter.py:136-138`), which the model reads as "no match" | a duplicate of an existing fact may be created | — | no |
| a tool raises past the adapter | `{"error": …}` returned to the model (`consolidation_agent.py:1307-1312`); rarely reached, since create, update and merge catch their own errors and return `status: failed` | nothing of that call | — | the model may retry in a later turn |
| `create_fact` missing a required attribute | `status: failed`, message names the fields (`fact_management_adapter.py:159-167`) | none | — | the model may retry |
| bad value (an enum such as `domain`, `state`, `temporal_class`, `context_priority` (`HISTORICAL` is read as `ARCHIVAL`); a malformed date, `tags: null` or `[]`, empty `content`; `state=superseded` on a create or merge, `state=stale` on a create, merge or update; `superseded_by` missing, or given without `superseded`) | checked **before** any embedding or write; the `ValueError` is caught → `status: failed` with the message (`fact_management_adapter.py:192-198,244-250,372-374`; `new_fact_attributes.py:45-103`, `fact_update.py:32-71`). One bad fact fails a whole legacy `add_facts_batch` call | none for that call | — | the model may retry |
| `create_fact`, `merge_facts` or a version-making `update_fact` with a missing, `null` or malformed `expiry` | `status: failed`, `refused: "expiry"`, the text names where `expiry` goes (`fact_attributes` / `updates`) and the three shapes, and ends "Nothing was written. Resend this call with a valid expiry before retiring any fact it replaces." Checked before any embedding or write (§2.7). A malformed merge leaves its sources current | none | — | the model resends; a refusal never answered is an abandoned refusal (🚨, §3.11) |
| an `expiry` born due (past or today's `date`; on Stage 1 or email a horizon ending within a day) | as above plus `born_due: true`; on Stage 1 and email the text offers discard, rewrite as history (`none`), or a short or longer horizon, and it is counted as an expiry error, never as abandoned; on a cleanup pass a past date reads "do not drop the fact… rewrite as history or give a future date" (no discard) and an unanswered one is abandoned (🚨, §3.11) | none | — | the model resends, rewrites or (conversation passes only) discards |
| a due horizon on a cleanup pass (Stage 2a, Stage 2b, backstop, cluster) | **not** refused: written with `expires_at` = now + 2 days and a `note` ("counted from … this horizon had already passed; the fact goes to review on …") | written | — | — |
| `expiry` sent with a closing state (update, or a create or merge that is archived or invalidated) | ignored, one line in the result `note`, logged at INFO; the fact is written or closed | written | — | — |
| a `reported_date` or `derived_from` the pass ignores (cleanup pass, or `derived_from` on Stage 1) | not an error: the result's `note` says "reported_date is kept by the system in this pass (…)" or that `derived_from` was ignored | written | — | — |
| a tool turn with any failed call | the turn counts as a tool-error turn in the 📊 / 🚨 lines (§3.11) | per call | — | — |
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
| the janitor run fails (a query, the counters, a stale write) | logged with its type and stage, one "🚨 Fact lifecycle run failed" (no fact named), the job answers 500; what was staled before the failure is already out of the baseline | the facts staled so far stay stale | — | **no**: the job is not retried by design; the next day's run catches up (§5.5) |
| a janitor stale write finds the fact changed since the read | counted `stale_conflicts`, skipped | none for that fact | — | the next run |
| the janitor's breaker is tripped | nothing is staled for that account; one 🚨 per account per run, only when a due fact with a decision was held back | none | — | the next run, until a successful review lowers the backlog or the pool is back to 50 or fewer (§5.5) |
| a second janitor run starts while one is going | the run guard is held: it does nothing and answers 200 `busy` | none | — | no; the next day's run |
| the 🧹 line's claim or post fails, or the consolidator's `expiry_tool_errors` increment fails | WARNING with the exception type; the run or pass goes on; a failed post loses that day's line (the snapshots stay), a failed increment loses that pass's count | stored | — | — |
| a review call is refused (`update_fact` or `ask_owner`: a basis the rules do not accept, a fact outside the batch or already decided, an ask that is not allowed) | the tool result says what to fix and is tagged `refused: basis`, `scope` or `ask`; nothing is written. A scope or born-due refusal nobody answers is abandoned (🚨, §3.11); a basis or ask refusal leaves the fact kept | none | — | the model fixes the call or leaves the fact; **a revive without evidence or a reply is never written** |
| the review pass fails (agent failure or exception) | the run answers 200 `failed`, `review_failed` is counted, `last_failed_at` is set, the send of the next question and the digest still run; writes already made stand and the cache is refreshed | stand | — | no Cloud Tasks retry; the janitor enqueues again (the `unsuccessful_since` alert at 48 h) |
| a review run finds the memory-write lease taken | it enqueues itself again, 120 s later, under a name made of the account and the lease bucket, and ends | none | — | yes, by the deferral |
| an owner's answer finds the fact changed since the question (a rewritten fact, a different current text) or its write conflicts | `changed` or `conflict`: nothing is written, the owner gets "That fact has changed in the meantime…" (a button keeps its buttons), the question stays open | none | — | the owner may answer again; the review or the timeout handles it otherwise |
| an owner's answer is refused by the write rules (`not_applied`) | WARNING, `answers_not_applied` counted, the question stays open; a digit reply is left to ordinary conversation | none | — | no |
| a question cannot be sent (its pair is not stored, or the post fails) | pair not stored: the question goes back to the front of the queue, nothing posted. Post failed: a "not delivered" pair is appended, the question goes back to the front, no more are sent that day, `not_delivered` is counted | none | — | the next day's run |
| the post succeeded but a mark failed (a crash between the post and `asked`) | housekeeping of the next run completes a pending question that has a message reference as `asked`; it is never sent twice | none | — | — |
| an error in the reply intake | ERROR, `reply_intake_failed` counted (a 🚨 the next morning), the message goes on as ordinary conversation | none | — | no |
| a Slack click arrives with a bad signature, or the queue is down | 401 with nothing parsed; 503 or 500 when the task is not queued (Slack shows the click as failed) | none | — | the owner clicks again |
| the digest is not delivered or cannot be read | the claim is released and the marks stay, so the next run tries again; an error is logged and never fails the run | none | — | yes, the next run |
| two runs of one account at once | prevented by the memory-write lease (§3 step 3): the second finds it taken and defers itself (a named task, 120 s later) | none from the loser | the loser does nothing; the winner owns the batch | yes, by the deferral |
| lease taken and the user has no open batch | the loser stops; nothing to do | — | — | — |
| less than 60 s of the run budget left when the batch would start | the batch stays `pending`, the lease is released, a deferral is enqueued (`consolidation_service.py:261-262`) | none | untouched | yes |
| lease acquire, open-batch check or deferral enqueue fails | the error propagates, the worker answers 5xx | none | untouched | yes, Cloud Tasks retries |
| lease release fails | WARNING `releasing the memory-write lease failed`; the lease expires on its own | stored | succeeds | — |
| user has no profile, or no account id | the oldest pending batch is marked `failed` ("user has no profile" / "user has no account"), `CONSOLIDATION_FAILED` is logged, no lease, nothing routed or written (`consolidation_service.py:182-200`) | none | `failed` | no 5xx loop; the sweep's bounded revivals fail it again at no model cost |
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

**A fact with an end** ("my checkup is on Sep 25", "I am evaluating tool X").
- Consolidated with its batch like any fact. The model decides `expiry` from the content: a `date` (the checkup's day) or a
  `horizon` (a few weeks for the evaluation). Code stores `expires_at` (the end of that day in the owner's zone, or
  `reported_date` + N days) and keeps `reported_date` inside the batch's span (§2.7). A decision that would already be over is
  refused or, on a cleanup pass, floored to review in two days.
- Until its end the fact is an ordinary current fact. If the owner says it again and the conversation confirms it held all
  along, the extension rule moves `reported_date` and a horizon counts from the new date.
- The first janitor run after `expires_at` (09:30 Europe/Madrid) finds it due. In log-only mode it only counts it (`would_stale`). With
  writes on, it becomes `stale` with a `stale_since` and a trail entry, the baseline of the account is rebuilt, and the fact
  leaves the baseline, the prefetch and `search_memory` from the next message (§5.5).
- A stale fact keeps its text in the store; nothing deletes it. The review pass decides what it is now (below).

**A fact that went stale** (the janitor staled "the planning permission for the garden shed is expected by Oct 30").
1. *Day 0, 09:30:* the janitor stales it, sees a stale fact and a review interval elapsed, and enqueues the account's `fact_review`.
2. *Minutes later:* the review takes the memory-write lease, reads the fact with its neighbours. A neighbour "the permission
   for the garden shed was received on Oct 28" decides it: the fact is superseded by that one (before P2 it could also be
   archived on the plan's date; **P2** never archives it). Nothing is asked. A fact no neighbour decides ("Alex goes to
   the FitLife gym") is queued instead: it becomes `current` again at once, with its old expiry, and a question about it is
   queued.
3. *Same run or a later one:* if no question is out and fewer than 5 went today, the question is posted to the primary chat,
   one fact, the options of its kind (four for `still_true`). The owner replies "4"… or "3 — it's Iron Temple now", or presses a button, or answers in the
   Cabinet. A digit, a button and a Cabinet click are applied at once; words are recorded, release the next question at once, and are read by the review on its next run.
4. *If he does not answer:* the next local day the question stays open as `unanswered` (and the next one may be sent); after 14
   days from the send the fallback applies: a lasting fact is revived for twice the window it last had (between 14 and 365 days),
   noise is archived (**P2**: a plan is written as history with an unknown outcome; before P2 a dated plan was archived).
   Nothing is asked about it again until it falls due again.
5. *Weekly:* what the review and the owner set aside in the week is listed in one digest message.

**A plan whose date passed (P2 revised, built, not deployed)** ("the user will attend a concert on Dec 5", a `date` fact).
1. *Dec 6, 09:30:* the janitor (once switched on) stales it; the review runs the same morning.
2. *The review decides from memory when it can:* a neighbour "sold the concert tickets" → history on evidence, "The user planned
   to attend a concert on Dec 5, 2026, and did not go."; a fact whose own text already reports the outcome (an invoice received,
   a subscription cancelled) → history on its own text (`date_passed`). Either way a new version at `archival`, `expiry none`,
   out of the baseline and found by search. Nothing is asked.
3. *Otherwise it asks* "Did this happen?" with the three texts written now (happened, did not, unknown); the parts of one plan
   are folded into one question. A membership or a document that ended is asked "Was this renewed?"; a parking fine "Was this
   paid?". A routine phone bill is not asked: it becomes "received; payment not confirmed" history and a line in the next
   digest's invoice block, ticked in the Cabinet.
4. *An answer* (digit, button, Cabinet) writes that answer's text at once, with no model. *Silence* for 14 days writes the
   "unknown" text, and the next weekly digest lists it under "Recorded as history without your answer". It is never archived
   and never asked twice.

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
- S3 additions (estimates from the plan, not measured on live batches): the expiry schema (~180 tokens in the cached tool
  block), the `Today` line, the longer stamps and `expiry` / `due` / `reported` per Stage 2a entry add well under 1 % of a pass.
  The required `expiry` costs a turn only when the model omits or mistypes it; Stage 2a's 15-turn cap was not raised. The
  model dry run before the deploy (step M of the rollout notes, ops) costs about $6-12 on Sonnet 5.5 (about $12-24 on
  Opus 5.5), capped at $30 cumulative by its own meter; its calls are recorded only in its ledger, not in the account's
  counters.
- The janitor calls no model: a few collection-wide queries, one transaction per staled fact, one cache rebuild per changed
  account.
- The review pass (S4, §5.6) is one more model loop, run only when a fact is stale or a reply in words waits. Its fixed
  prompt is about 25,000 tokens and is paid per pass, so a pass of 2 facts costs about $0.08–0.10 on Sonnet 5.5 ($0.15–0.20 on
  Opus 5.5) and a pass of 20 facts about $0.22–0.30 ($0.45–0.60). Mirror mode adds about 400 input tokens a pass and, once per
  new language, about 100 output tokens of labels. No model call is made for an empty selection, a send, a digest, a timeout
  with nothing to look at, a digit, a button or a Cabinet click. A rewrite or an "ended" history fact embeds three vectors
  (cents). Steady state, with the owner's estimate of 0–3 questions a week, is about $1.3–2.6 a month; if 0–5 facts go stale
  every day, about $2.5–6 a month. The model dry run of the review (step M benches 6 and 7) is capped, with the S3 benches, at
  $30 by the same ledger.
- S5 classify (one-time, owner-run): one structured call per batch of 25 through the live consolidation agent, about 500 facts
  to the model, 21 batches, so about $2–4 on Opus 5.5 and $1–2 on Sonnet 5.5 (an estimate, not measured). The model is the one the
  live agent resolves; `--expect-model` must name it, and the run refuses otherwise. Without `--budget-usd` it only estimates and
  prints the spend so far; the cap is lifetime over classify's own ledger (`scripts/memory/fact_s5_classify_spend.json`, not the
  S3 kit's), so a re-run starts from what was spent. Apply and a small `--ids-file` re-run cost cents.

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
| What the janitor staled or would stale, the breaker, the backlog | the 🧹 morning line in the ops channel; `{prefix}fact_lifecycle_daily` in the Firestore console (one snapshot per account per day); the `[FactLifecycle]` log lines |
| What the review did yesterday, what is queued or unanswered, why a question was not sent | the review segments of the 🧹 line (§5.5); `{prefix}fact_review_state` (the current question, today's count `sent` and `sent_on`, `delivery_failed_on`, `unsuccessful_since`) and `{prefix}fact_review_questions` (each question's status, `closed_by`, `answer`) in the Firestore console; the `[FactReview]` and `[FactAnswer]` log lines |

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
- janitor (§5.5): startup `fact lifecycle sweep: log-only` / `: writes`; per account `[FactLifecycle] account=<8> due=… newly_due=… no_decision=… staled=… would_stale=… deferred=… conflicts=… backlog=… breaker=<ok|reason> review_enqueued=0 dry_run=<bool>`; `[FactLifecycle] breaker tripped for account <8>: …` (WARNING); `[FactLifecycle] the run failed (<Type>) at <stage>` (ERROR) and `[Worker] fact_lifecycle_sweep failed (<Type>)`; `an ops message was cut after 10s` / `was not delivered` (WARNING)
- review (§5.6): `[FactReview] …` lines: `user <8> is not the owner of account <8> … nothing done`, `memory-write lease busy: deferred
  the review`, `question <id> timed out; …`, `N facts … kept twice: queued for the owner`, `📨 question <id> sent to account <8>
  on <platform>`, `question <id> not delivered on <platform>`, `… was posted … but closed meanwhile`, `the review pass failed for
  account <8>: …`, `📬 digest of account <8>: N facts`, `the digest of account <8> was not delivered; tried again next run`;
  answers: `[FactAnswer] question <id> answered <kind> (<source>); applied`, `… the write conflicted; nothing written`, `… the
  answer's write was refused (…)`, `reply to <platform> message <id> not taken: …` (ERROR, counted), `… fact … changed since it
  was asked`; the Slack route: `[SlackInteractivity] queued a fact_question choice`, `… no task queue`, and the worker's
  `interactive_choice` lines. The 📊 line of a review pass carries the label `review` (§3.11). The janitor adds
  `review enqueued` / `already queued today` for an account and `another run holds the run guard; this run does nothing`.
- expiry: `[OwnerZone] owner timezone unresolved for account <8>: …; using UTC` (WARNING); `⚠️ [ConsolidationAgent] Pass close-out cut after 10s` / `failed: <Type>` and `Lifecycle counters not updated: <Type>` (WARNING); the INFO line for an ignored `expiry` on a closing state
- search: `🔍 [SearchEnrichment] Final: N facts (semantic_dedup: …, bio_dedup: …)`, `🔍 [find_nearest] START/DONE`

### 9.2 Why did the bot not remember X?

1. Was it said in a channel that consolidates? Companion-bound channels do not (§1.4).
2. Has that channel overflowed since? Count messages since (§7). If not: it is waiting (`$consolidate` forces it).
3. Was the batch consumed? Grep the batch id: `DELETED`, `failed`, or still in `{prefix}consolidation_queue`.
4. What did Stage 1 decide? BigQuery `prompt_content` for that run: the candidate list, the searches, the DISCARD
   reasons in the thinking and the final report. Usual reasons: echo trap, Trivial_Exclusions, judged a duplicate.
5. Is the fact stored but not `current` (archived, invalidated, superseded, or `stale` because its expiry passed and the janitor was
   on, §5.5; its `stale_since` and last trail entry say when)? Readers show only `current`. A fact that was
   updated is a **chain**: the closed document keeps the old wording and its `superseded_by` leads to the live one (§2.4),
   so look for the newest `version` of its `lineage_id`.
6. Stored and current but not used: not in the baseline (§4.1 selection), and this message's prefetch did not rank it
   (§4.3). Try `search_memory` with other words; check its domain and priority.
7. A fact that left memory without the owner saying so: look at its last trail entry (`actor`, `reason`). `janitor` = its
   expiry passed (it is `stale`); `review` or `timeout` = the review set it aside, and it is in the next weekly digest and in the
   Cabinet's "set aside lately" for 30 days; `owner` = his own answer. A set-aside fact is brought back by telling the bot in
   chat that it is still true (the Cabinet cannot).

### 9.2b Why did no question arrive, or why did one not move?

1. Is there a stale fact? The Cabinet's "waiting for review" view, or the 🧹 line's `backlog`. A review runs only when a stale
   fact, a reply in words, a timed-out question, an unfinished send, a due digest or a queued question needs it (§5.5 step 7).
2. Did the review run? `{prefix}fact_review_state`: `enqueued_at`, `started_at`, `last_review_at`, `last_failed_at`. A 🚨 "no
   successful review for N h" means it has not succeeded for two days.
3. Did the review decide the facts itself? Most facts are decided from their neighbours and ask nothing. Look at the counters
   (`revived`, `archived_by_review`, `queued`).
4. Is a question waiting? `queued` questions wait for the current one to be answered, or for the next local day; at most 5 go per
   local day; a failed delivery stops that day (`delivery_failed_on`); with no primary channel, or a primary channel bound to a
   companion, nothing is sent (`no question sent … the queue waits` in the log). The Cabinet's question list shows the same queue.
5. Did a button do nothing? Interactivity must be enabled in the Slack app with the main service's `/slack/interactivity` as its
   request URL; a click that failed leaves no `[SlackInteractivity] queued …` line (a bad signature is a 401). A thread reply
   works without it.
6. Did a digit get "That fact has changed in the meantime"? The fact was rewritten or changed after the question was asked; the
   review or the timeout handles that question.
7. (**P2**) An invoice never arrives as a chat question: a routine invoice is a digest question, listed in the weekly digest's
   invoice block and answered in the Cabinet's "Invoices with no payment confirmation" group. A fact the review rewrote as
   history from evidence or its own text asks nothing either (the `to history` figure of the 🧹 line).

### 9.3 Why did it remember something wrong?

- From the bot's own words: a bot reply reaches the consolidator only as its summary (§3 step 1); `Domain_Scope`
  forbids storing assistant statements unless the user adds new information. When this happens it is a model error;
  the run's BigQuery rows show which message it came from.
- A stale value: before S5 classifies the existing facts they have no expiry, and the janitor stales only facts that have one
  (`expiry_kind` set); a newer fact's wrong `date` or `horizon` shows in the consolidator's `prompt_content` row (§9.4). An update wrote a new version; the
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
| the five benches (stage 1 classification, cluster audit, decomposition, directive review in `scripts/consolidation/`, and the email bench `scripts/email/test_consolidation_dryrun.py`) with `--s3-kit` (`scripts/consolidation/s3_dry_run_kit.py`, `scripts/consolidation/s3_bench_support.py`) | the **model dry run of the S3 prompts** (deploy step M, owner-run on the Mac, cap $30): each bench runs the pass its gate reads, on the real agent and adapter over a no-write store |
| `scripts/migration/fact_s3_stamp_legacy_stale.py` | deploy step 6a: stamps `stale_since` on stale facts that have none (dry run first; `--apply` writes) |
| `scripts/migration/fact_s5_classify.py --expect-model NAME [--budget-usd X] [--ids-file PATH]` / `--apply PLAN` | S5's one-time expiry decision for the current facts that have none. Dry run first: it reads, asks the live consolidation agent once per batch of 25 and writes a report and a plan under `scripts/memory/`, with nothing written to the database; the owner reads the report; then `--apply` replays that plan exactly, with no model, under the change token recorded at the scan (a fact changed since is skipped and listed in `…_changed.txt`). `--ids-file` classifies only the listed ids (the skipped ones, or documents an old revision rewrote); with `--include-closed` it also classifies the listed archived and invalidated facts, for a re-review (P2 revised). A run without `--budget-usd` only estimates. Unsure → `none`; a run that gets no answer stops and lists the undecided ids rather than writing `none`. Writes `expiry_kind`, `ttl_days`, `expires_at` and one trail entry (actor `migration`) |
| `scripts/consolidation/test_review_answers_dryrun.py --s3-kit` | step M bench 6: Stage 1 on windows with the real history shapes of a question and a reply (a digit, words, a correction, a bare digit with no marker, a not-delivered pair): the quoted fact must be left alone and only a correction's true value recorded (gate G4); `--case NAME` runs one case, e.g. `correction_translated_quote` (the question quoted in Ukrainian) |
| `scripts/consolidation/test_review_dryrun.py --s3-kit --stage N --runs R [--mirror CODE]` | step M bench 7: the real `FactReviewService.run` on the kit over the owner's live stale facts, `N` more live facts staled in the kit's overlay, and seeded cases (a plan whose date passed, a revive with no basis, two stale duplicates, an undecidable fact, three replies in words); no stale fact, question, message or counter is written live (gates G5 and G6; `--mirror` checks the language the review reports; `--fixed CODE` runs fixed mode and lists the translations the pass kept). **P2:** the turn bar is 12 of the review's 15, the plan case expects a `happened` question or a history on evidence (never an archive, never a `date_passed` history), and the G5 detector counts no history rewrite as an unbased revive. `--stage-ids PATH` (ids under `scripts/memory/`) replaces all the staging: exactly the listed facts, in any state (archived, invalidated, current), staged stale in the overlay; `--expiry-plan PLAN` gives them a classify plan's expiry; `--max-passes N` (default 6) runs review passes until every listed fact is decided; the report gains a human `<report>_listed.md` (each fact's original state and text, its outcome, the text or question the review would write) |
| `scripts/migration/fact_p2_reopen_archives.py` (`--ids-file PATH`, `--expiry-plan PLAN`, `--apply`) | P2 revised: re-opens the facts of the owner's reviewed list (ids under `scripts/memory/`, the only selection) that are `archived` or `invalidated` as `stale`, so the daily review decides them again under the new rule. Dry run by default (counts on stdout; ids, state, text and the last trail entry to `scripts/memory/`). `--apply` writes under the scanned `update_time` (a changed document is skipped): state `stale`, `last_updated`, the restored `stale_since` and one trail entry (`<its real state> → stale`, actor `migration`, "reopened under P2 revised"); with `--expiry-plan` also the plan's expiry (a fact absent from it is reopened without and listed). A missing or `SYSTEM` fact, or one in any other state (`not_closed`), is counted and left alone. Text and vectors are never touched; a second apply reopens nothing |
| `scripts/migration/fact_p2_owner_decisions.py` (`--decisions PATH`, `--apply`) | P2 revised: writes the owner's **own** decision on each listed closed fact, for a re-review in which he decided every fact himself. The file (under `scripts/memory/`) lists `{fact_id, account_id, action, text?, successor_id?}`, `action` one of `history` (the text as a history version), `current` (the text as a current fact, `none`), `supersede` (by `successor_id`, current or written earlier in the run), `invalidate`, `revive` (current, text unchanged, `none`); it is validated whole before any write. Per fact: the reopen script's own reopen, then one typed write as the owner, reason `owner decided on review` (a rewrite for every reader, never a revive), with `expected_id`, so a fact whose chain moved on is skipped and listed, never followed. Dry run by default (id, action, old and new text to `scripts/memory/`); a changed fact is skipped; a failure never stops the run; a half-done fact (reopened, not written) is `resumed` next run; a second apply finds every fact `already_decided` |
| `scripts/migration/fact_p2_rollback_questions.py` (`--apply`) | P2 revised, before a rollback to the S4 revision only: removes the five P2 keys from every question document (the old model refuses even default-valued ones), deletes the invoice digest questions, closes the closed P2 ones as `resolved_elsewhere` with no answer, **closes the sent ones** (`pending`, `asked`, `unanswered`: their message carried P2 options, so the old code would misread a digit) as `resolved_elsewhere`, and gives only the `queued` ones fallback `revive`. Run it again after the traffic switch. Dry run by default; ids only, no text |

**A re-review of closed facts (P2 revised; planned, owner-run).** Every archived fact and every invalidated fact that is not a
directive is looked at again under the new rule, always dry run first: classify with `--include-closed --ids-file` writes an
expiry plan for them; bench 7 with `--stage-ids` (the same ids), `--expiry-plan` and `--max-passes` shows, in its `_listed.md`,
what the review would write for each; the owner reads it; only then either the reopen script with `--ids-file` and
`--expiry-plan` makes the kept ones (archived or invalidated) stale and the next daily review decides them (their questions
join the pool, at most 50), or, for the facts he decided himself, the owner-decisions script writes each decision. (Historical: the janitor stayed log-only meanwhile; it writes since 2026-10-27.)

The `scripts/consolidation/` benches run the real agent against live reads: fact writes are intercepted and the cache
refresh is stubbed, but the model calls are real (paid) and may be recorded in the account's billing counters (the
confirmed-facts script says so in its header). Each needs the owner's OK, credentials and `.env`. Exports go to
gitignored folders only.

**The S3 kit (`--s3-kit`) is different.** It puts a no-write fact store under the **real** agent and the real
`FactManagementAdapter` (so parse, expiry, pin, born-due, `derived_from` and the notes run as in production), a no-write email
repository, a recording counters store and the five edited prompt tokens read from `prompts_snapshot/`. No fact, cache
document, email mark or lifecycle counter is written. No account usage counter is written either (the benches never open a
billing scope), so the spend appears only in the kit's ledger `scripts/memory/s3_dry_run_spend.json` (stops every run at $30
cumulative; it prices the requested model only). What does leave the Mac: the model and embedding calls, `prompt_content` rows
(account id empty) only when `DEBUG_PROMPTS=true` with a prompt dataset set, and no Logfire span. It needs `CONSOLIDATION__PROMPT_VERSION` v3 and runs one
bench at a time; bench 1 reads a fresh window only, so the late-batch case (a horizon said days ago) is not exercised. The
report lists the served tokens, one row per write and the three gates; the commands, costs and gates are in
the rollout notes (ops), step M.

### 9.6 The data audit

`python -m scripts.data_audit.run facts [--account <id>] [--limit N] [--s2-cutoff <ISO time>]` streams the facts
collection read-only and checks every stored document against the invariants: stored `id` equals the document id,
`account_id` and `state` present and valid, `is_current` / `valid_to` matching `state` (§2.3), `created_at` present,
`reported_date` and `last_updated` timestamps, all three vectors stored as vectors, the document parses as a
`FactEntity`, and no lineage with more than one `is_current` document. It prints counts only and writes the offending
ids per class to `scripts/memory/data_audit_facts_<time>.json` (gitignored; no text or vectors). The S3 fields (`expiry_kind`, `expires_at`, `stale_since`, `lifecycle_events`) are checked only as
part of that parse (a malformed value is `invalid_entity`); the expiry invariants are a second check (`check_expiry`) with five classes:
- `current_without_expiry_kind`: a current fact of a non-`SYSTEM` account with no `expiry_kind`. A violation only with
  `--expiry-required` (use it after classify is applied); without the flag the facts are counted, not reported.
- `pinned_expiry_not_none`: a directive, principle or `mindset` fact whose expiry is not `none`.
- `current_with_stale_since`: a current fact that still has `stale_since`.
- `horizon_not_counted_from_reported_date`: a horizon without a positive `ttl_days`, `expires_at` or `reported_date`, or with
  `expires_at` earlier than `reported_date + ttl_days`. The rule is `>=`, not equality: the cleanup pass floors a due horizon
  to now + 2 days and a re-mention never moves `expires_at` earlier, so a later date is legal; an earlier one is the defect.
- `current_past_expiry_not_waiting`: a current fact past its `expires_at` that no open review question holds. Judged only with
  `--janitor-ran-at <time>`, for use while the janitor writes and after that day's run (in log-only mode, and between a fact
  falling due and the next run, it need not hold). Facts that a pool cap deferred or a breaker held back are past their
  `expires_at` and in no question, so they appear here too: **compare the count with that run's `deferred` and breaker** (the
  janitor's 🧹 line) before reading it as a defect.
The stored `reported_date` distribution is not an audit class (there is no baseline to compare with); the classify apply
test pins that apply writes no `reported_date`. Run it after any migration or backfill.

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
document the old revision closed in the mixed window is reported as a post-S2 violation (the rollout notes, ops).
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
blueprint `consolidation_agent_v1.yaml`, the six `CONSOLIDATION_*` tokens; the review pass has its own profile
`consolidation_review.yaml` and blueprint `consolidation_review_v1.yaml` over `CONSOLIDATION_TAXONOMY` and
`CONSOLIDATION_LIFECYCLE`, and Smart's profile carries `PROTOCOL_MEMORY_REVIEW_MARKERS`; `COGNITIVE_PROCESS_CONSOLIDATION.groovy`
is an older token the profile does not render). Edit there and upload with `firestore_utils/snapshot_upload.py`
(`prompts_snapshot/README.md`). `tests/unit/prompts/test_consolidation_vocabulary.py` checks every enum value and tool field
the tokens name against `entities.py` and `_get_tool_declarations` (and the search `returns:` keys against a real adapter
result), and `tests/unit/prompts/test_consolidation_expiry_prompt.py` pins the S3 contract: kinds named only as
`date` / `horizon` / `none`, no token offers `ttl_days`, every instruction to retire a fact with `SUPERSEDED` names
`superseded_by`, every `create_fact` / `merge_facts` example and every content-changing `update_fact` example carries an
`expiry`, and the extension rule is worded in the TAXONOMY and COGNITIVE_PROCESS tokens. S3 edits five tokens
(`CONSOLIDATION_TAXONOMY`, `_COGNITIVE_PROCESS`, `_TOOLS`, `_EXAMPLES`, `_POLICIES`; `_OUTPUT_SPEC` is untouched) and the
snapshot already says `ARCHIVAL`; **until the five are uploaded (deploy step 4) the live prompt is the pre-S3 one**: it offers
`ttl_days` (ignored by the new code), never mentions `expiry` (only the tool schema requires it), tells the model to retire a split fact with `SUPERSEDED` alone (refused) and says `HISTORICAL` (read as
`ARCHIVAL`). The static template is cached for 24 hours per instance,
so the upload goes in before the switch (the rollout notes, ops). S4 adds `CONSOLIDATION_LIFECYCLE`,
`PROTOCOL_MEMORY_REVIEW_MARKERS`, the review profile and blueprint, and edits `CONSOLIDATION_POLICIES`, `_TAXONOMY`, `_EXAMPLES`,
`_COGNITIVE_PROCESS`, `_TOOLS` and the Smart profile; the review pass cannot run without its profile, and the echo-trap
exception (§3 step 5) is live only after the upload.

---

## 10. Not built yet

`docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (rev 2, accepted 2026-10-21) — S1 (defect fixes) is live. **S2, S3 and S4 were deployed
2026-10-27** (S2: real versions, validation before any write, preconditioned writes, the memory-write lease; §2.4, §3 step 3,
§5.2, `decisions/fact_versions_one_writer.md`. S3: expiry decided from content, `reported_date` ownership, the lifecycle trail,
the five prompt tokens and the code-built messages, the owner-zone timestamps, tool-error accounting, and the daily janitor in
log-only mode; §2.7, §3.11, §5.5, `decisions/fact_expiry_and_janitor.md`. S4: the review pass, the question queue, the owner's
answers by digit, button, reply or Cabinet, the weekly digest, the Cabinet views, the janitor's pool, guard and alerts; §5.5,
§5.6, `decisions/fact_review_and_owner_questions.md`). **S5's classify is built, not run** (the classify script, §9.5; "S5 as
built" in the RFC), and the audit has its expiry classes (§9.6). **P2 revised is deployed (2026-10-27)** (a fact whose date
passed becomes history; the question kinds, the invoice digest questions, the digest's two new blocks, the reopen and rollback
scripts; §2.7, §5.6, §9.5, RFC §9 "P2 revised"); its release order is code, then the `CONSOLIDATION_LIFECYCLE` upload the same
day, then the reopen script, classify apply and a bench 7 re-run, and the janitor was switched on after it shipped (2026-10-27). Still
to come:
- **S5 steps.** The order, set by the owner: classify (dry run → the owner reads the report → apply) → the review dry run (step M
  bench 7 is its tool; its G5 detector no longer counts the history fact of an "ended" answer) → the janitor stayed log-only on
  the classified facts until the owner's word → janitor on (done 2026-10-27, `FACT_LIFECYCLE_SWEEP_DRY_RUN=0`). Classify defaults to `none` when unsure, accepts a filtered set (`--ids-file`), and a pool of 50 or fewer
  bounds the drain: the review gets at most 50 facts at a time (about three days of passes of 20), then questions go one at a
  time, up to 5 a day, answered by digit, button or the Cabinet (which can answer any number at once). Bench 7 stages a
  sample, not the whole set that classify makes due, so the first live wave is bounded by the pool cap, oldest expiry first.
- **Follow-ups outside S4:** an answer given on a phone call counts as silence (no Lelik filter on the question pair); a
  Cabinet "bring back" for a closed fact; Telegram inline keyboards on the same interactive-message port; Smart sending
  interactive blocks through it.

Since the deploy, new facts carry an `expiry_kind`; the facts stored before it have none, so the janitor has nothing due
on them even when it is switched on. That is what classify fixes. S2 does not ship alone: it refuses `SUPERSEDED` without a
successor while the live consolidation prompts still tell the model to retire a split fact that way.

---

## 11. Code map

| File | Role |
|---|---|
| `src/domain/entities.py` | `FactEntity` (with `superseded_by`, `expiry_kind`, `stale_since`, `lifecycle_events`), the four enums and `ExpiryKind`, `normalize_fact_taxonomy` (`historical` → `archival`), `FACT_SYSTEM_TAGS` |
| `src/domain/fact_lifecycle.py` | state → `is_current` / `valid_to`; forward resolution (`forward_step`), the accepted end states per caller, `IN_PLACE_FIELDS`, `closure_fields`; `compute_expiry`, `born_due`, `expiry_pinned` / `check_pin`, `lifecycle_event` / `with_event` |
| `src/domain/fact_update.py`, `src/domain/fact_update_plan.py` | `update_fact` arguments parsed and validated; the plan: version document or in-place fields |
| `src/domain/fact_expiry.py` | the `expiry` argument: `Expiry`, `parse_expiry`, `expiry_view` (the stored decision as the model sends it), the refusal texts |
| `src/domain/fact_write_context.py` | `FactWriteContext` (actor, owner's zone, span, who decides dates), `as_utc`, the `reported_date` rules per write |
| `src/domain/fact_update_lifecycle.py` | `finish_fact_update`: the expiry, date and trail rules applied to an update's plan |
| `src/domain/local_time.py` | the one timestamp formatter (`local_stamp`, `local_day`) shared by Smart and the consolidator, `parse_local_stamp` |
| `src/domain/new_fact_attributes.py`, `src/domain/fact_argument_parsing.py` | a new fact's arguments parsed before any embedding; enum and date parsing |
| `src/domain/stored_fact.py`, `src/domain/consolidation_run.py` | a document with its change token; the outcome of one consolidation request |
| `src/domain/memory_write_lease.py` | lease margin, deferral delay, `lease_until`, the run timeout left, the deferral bucket |
| `src/domain/exceptions.py` | `FactResolutionError`, `FactWriteConflict`, `InPlaceFieldRefused` (none is a `ValueError`) |
| `src/domain/fact_embedding.py` | the three embedding texts |
| `src/domain/consolidation.py` | `ConsolidationBatch` (with `revivals`), `BatchStatus`, `MAX_FAILED_REVIVALS` |
| `src/domain/consolidation_serialization.py` | messages → batch (summary vs full text) |
| `src/domain/consolidation_pass_stats.py` | `PassCall`, tool-error turns, abandoned refusals (the 📊 / 🚨 texts are the agent's) |
| `src/domain/fact_lifecycle_sweep.py`, `src/domain/fact_lifecycle_day.py`, `src/domain/due_fact.py` | the janitor's rules (breaker, per-run headroom, stale fields, log-only parse, the 🧹 line), a day's snapshot row, the projected due fact |
| `src/domain/settings.py` | `ConsolidationSettings` (70/50), `SearchConfig` (limits, baseline 65/20, directives 15) |
| `src/domain/search.py`, `src/domain/rrf.py`, `src/domain/deduplication_service.py`, `src/domain/vector_math.py` | search DTOs, RRF, number-aware dedup, cosine |
| `src/domain/memory_fact.py` | `$memory save` draft, preview and history pair |
| `src/adapters/firestore_session_store.py` | sessions, sliding window, 90-day TTL |
| `src/services/overflow_routing_service.py` | batch → Alek's queue or a companion's |
| `src/adapters/firestore_consolidation_queue.py` | queue, oldest first, zombie reset (threshold per caller), bounded failed revival (`revive_failed`), `has_open_batches`, sweep query |
| `src/adapters/gcp_task_queue.py` | `enqueue_consolidation_task` |
| `src/handlers/worker_handler.py` | `consolidation`, `sweep_consolidation` (revives FAILED, then enqueues), `fact_lifecycle_sweep` (runs the janitor; 500 on a failed run), `fact_review` (`daily` or `next`), `interactive_choice` |
| `src/services/consolidation_service.py` | one batch under the lease: run, delete or retry; deferral; `consolidate_now` (`$consolidate`); `revive_failed_batches` (sweep); the give-up log; the owner's timezone |
| `src/services/fact_lifecycle_sweep_service.py` | the janitor (§5.5): guard, visit set, pool, enqueue, alerts, line |
| `src/services/fact_review_service.py`, `src/services/fact_answer_service.py`, `src/services/fact_question_texts.py` | the review run, the owner's answers and every question close, the question's texts and language (§5.6) |
| `src/infrastructure/fact_review_scope.py` | the review pass's tool scope (`update_fact`, `ask_owner`) |
| `src/domain/fact_review_rules.py` | selection, queue order, when a question may go out, timeouts, fallbacks, kept twice, basis and ask rules, the pool, `review_due`, the revive alert |
| `src/domain/fact_review_question.py`, `fact_review_state.py`, `fact_review_frame.py`, `fact_review_reply.py` | the question record and its statuses, the account's review state, the history frames (`[System: memory review q=…]`, `[Reply to memory review q=…]`), the reply and click paths |
| `src/domain/fact_review_answers.py`, `fact_review_answer_kind.py`, `fact_review_answer_change.py`, `fact_review_reasons.py` | what each answer writes (P2: `answer_fits`, `history_fallback`, the kept-twice texts), and the trail reasons |
| `src/domain/fact_review_options.py`, `fact_review_ask.py`, `fact_review_invoice.py`, `fact_review_basis.py`, `fact_words.py` | P2: the options per question kind, the `ask_owner` arguments (`question`, `texts`, `folds`), the routine invoice request, the basis and `payment` parsing, the one word counter and the written-text checks |
| `src/domain/fact_review_language.py`, `fact_review_digest.py`, `fact_review_task.py`, `fact_review_task_keys.py` | the message language and labels, the digest, the code-built message to the model, the `fact_review` task keys |
| `src/domain/fact_lifecycle_alerts.py`, `fact_lifecycle_review_line.py`, `fact_lifecycle_run_guard.py`, `pass_actor.py` | the janitor's alerts, the review segments of the 🧹 line, the guard timing, the table of pass actors |
| `src/domain/interactive_message.py`, `interactive_option.py`, `interactive_binding.py`, `interactive_choice.py`, `choice_outcome.py` | the platform-neutral interactive message and the choice |
| `src/ports/fact_review_store.py`, `src/adapters/firestore_fact_review_store.py` | question and state storage, the queue claim, the digest claim |
| `src/ports/interactive_message_port.py`, `src/ports/choice_handler_port.py`, `src/infrastructure/interactive_message_router.py`, `src/services/interactive_choice_service.py` | posting a choice message, showing a choice, picking the platform's adapter, routing a click by binding kind |
| `src/adapters/slack/interactive_message_adapter.py`, `interactivity_route.py`, `signature.py`, `binding_codec.py`; `src/adapters/plain_text_interactive_message_adapter.py` | Slack buttons and the `/slack/interactivity` route (signature before parsing), the shared signature check, the block id codec; Telegram's plain text with digits |
| `src/ports/fact_lifecycle_counters_port.py`, `src/adapters/firestore_fact_lifecycle_counters.py` | the day's snapshots, the tool-error increments, the posted-line mark |
| `src/ports/memory_write_lease.py`, `src/adapters/firestore_memory_write_lease.py` | the per-account lease (acquire, release by owner token) |
| `src/agents/consolidation_agent.py` | all stages, tools, loop, backstop, cluster and directive messages |
| `src/infrastructure/agent_config.py` | `ConsolidationAgentConfig`, `MemorySearchAgentConfig`, Router and Smart windows |
| `src/composition/application_graph.py` (`build_application_graph`; the janitor and its log-only knob are built there too), `src/composition/{slack,telegram}_adapter_factory.py` | the one `ConsolidationService` is built here, with the lease, the task queue and the 900 s run timeout; the chat adapters get its `consolidate_now` |
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
| `src/handlers/conversation_handler.py` | `$consolidate` (and its outcome wording), `$memory save`, memory preview delivery, the reply to a memory-review question taken at intake |
| `src/web/user_cabinet_app.py`, `src/web/static/cabinet.html` | Cabinet fact routes (invalidate follows the id forward, answers 409, rebuilds the baseline; the stale filter; recently closed; the question list and the bulk answers) |
| `prompts_snapshot/{profiles/consolidation.yaml,blueprints/consolidation_agent_v1.yaml,tokens/system/CONSOLIDATION_*.groovy}` | the consolidation prompt (mirror) |
| `prompts_snapshot/{profiles/consolidation_review.yaml,blueprints/consolidation_review_v1.yaml,tokens/system/CONSOLIDATION_LIFECYCLE.groovy,tokens/system/PROTOCOL_MEMORY_REVIEW_MARKERS.groovy}` | the review pass's prompt and Smart's memory-review protocol (mirror) |
| `scripts/consolidation/`, `scripts/data_audit/` | benches (with the S3 kit `s3_dry_run_kit.py`, `s3_bench_support.py`, and the review benches and their stand-ins `test_review_dryrun.py`, `test_review_answers_dryrun.py`, `review_bench_*.py`, `review_kit_*.py`); the audit with the version-chain classes (§9.6) |
| `scripts/migration/fact_s3_stamp_legacy_stale.py` | deploy step 6a (stamp `stale_since` on legacy stale facts) |
| `scripts/migration/fact_p2_reopen_archives.py`, `scripts/migration/fact_p2_owner_decisions.py`, `scripts/migration/fact_p2_rollback_questions.py`, `scripts/consolidation/plan_inputs.py` | P2: re-open archived or invalidated facts as stale; write the owner's own decisions on closed facts; bring P2 questions to the S4 shape before a rollback; the ids-file and expiry-plan readers they and bench 7 share (§9.5) |

Stale code comments, out of scope for this document (`src/`): `ConsolidationAgent._review_directives` says "hard cap of
10" and `biographical_context_service.py:155` says "soft cap ~12" (the code uses 15); the `BiographicalContextService`
docstring says CRITICAL facts are always included (the code does not).
