# Memory (Building Block)

The owner's map of long-term memory: how a conversation becomes facts, how facts reach every prompt, how they are
searched, what fails and what it costs. Current state only. The code paths (`path:line`) are the ground truth; when this
text and the code disagree, the code wins and this file is wrong. Every session that touches memory facts,
consolidation, the biographical baseline, memory search, `$memory save`, standing directives or the fact lifecycle ends
by updating this file.

Design records: `docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md` (taxonomy, tools, cognitive process),
`docs/10_rfcs/CLUSTER_REVIEW_RFC.md` (Stage 2a), `docs/10_rfcs/STANDING_DIRECTIVES_RFC.md` (Stage 2b),
`docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md` (`$memory save`), `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (S1 done, S2–S5
not built), decisions `docs/04_solution_strategy/decisions/{fact_s1_defects,standing_directives,directive_applicability_gate,memory_facts_owner_confirmed,consolidation_one_batch_per_task,sonnet55_adaptive_thinking_stall,consolidation_provider_model_eval}.md`.

## 0. How to answer questions about memory

| Question | Read |
|---|---|
| What is in memory; what a fact holds; what `domain`, `temporal_class`, `state`, `context_priority` mean | §2 |
| What `is_current`, `valid_to`, `lineage_id`, `version` mean today; what an update or a merge does to them | §2.3, §2.4 |
| When does a conversation become facts; why nothing happened after I told it something | §3 steps 1–3, §7 |
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
| Owner scripts, dry-run benches, the data audit, the prompt snapshot and its vocabulary tests | §9.5–§9.7 |
| Expiry, janitor, review pass, owner questions, real versions | §10 (not built) |
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
invalidate a fact from the Cabinet (§5.2).

### 1.2 The stores

| Store | Collection | What it holds | Written by |
|---|---|---|---|
| Facts | `{prefix}domain_facts_v2` (`src/config/environment.py:114-120`) | one document per fact (an update overwrites it, §2.4), three 768-dim vectors each | consolidation tools (§3), Cabinet invalidate (§5.2) |
| Sessions | `{prefix}sessions` (`environment.py:455-463`), doc id `{user_id}:{channel_id}` | the channel's recent messages (the sliding window) | `ConversationHandler`, notifications, `$memory save` |
| Consolidation queue | `{prefix}consolidation_queue` (`environment.py:465-472`) | batches cut from sessions, until consolidated | overflow routing (§3 step 3), `$consolidate` |
| Baseline cache | `{prefix}user_context`, doc id = account id (`environment.py:516-523`) | `biographical_facts`, `principles`, `directives` lists | `refresh_biographical_context_cache` (§3 step 9) |
| Memory drafts | `{prefix}memory_fact_drafts`, doc id `{user_id}:{code}` (`environment.py:350-354`) | facts Smart proposed, 7-day TTL | `propose_memory_facts` (§5.1) |

### 1.3 The actors

| Actor | Role | Code |
|---|---|---|
| `FirestoreSessionStore` | appends messages, cuts overflow batches | `src/adapters/firestore_session_store.py` |
| `OverflowRoutingService` | sends a batch to Alek's queue or a companion's | `src/services/overflow_routing_service.py` |
| `ConsolidationService` | takes one pending batch, runs the agent, deletes or retries the batch | `src/services/consolidation_service.py` |
| `ConsolidationAgent` | the model loop, all stages | `src/agents/consolidation_agent.py` |
| `FactManagementAdapter` | the tools: search, create, update, merge | `src/adapters/fact_management_adapter.py` |
| `FactWriteService` | builds a `FactEntity` with its three vectors and stores it | `src/services/fact_write_service.py` |
| `FirestoreFactRepository` | every fact query; the baseline cache document | `src/adapters/firestore_repo.py` |
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

`FactEntity` (`src/domain/entities.py:97-174`):

| Field | Meaning today |
|---|---|
| `id` | document id (UUID) |
| `account_id`, `created_by_user_id` | tenant (every query filters `account_id`); author |
| `text` | the fact, one self-contained sentence; the only thing most readers show |
| `vector`, `tags_vector`, `metadata_vector` | the three embeddings (§2.6) |
| `tags` | keywords; `mindset` makes the fact a principle (§4.1); `consolidated` is added to every fact, `anchor` to principles (`fact_write_service.py:142-148`) |
| `type` | `state`, `event`, `principle`, `system`, `alert` (`entities.py:7-12`); the consolidator does not set it, so it defaults to `event` (`fact_management_adapter.py:181`) |
| `metadata` | structured values (numbers, dates) |
| `domain`, `temporal_class`, `state`, `context_priority` | the four axes (§2.2) |
| `context` | a short label for why it was recorded; shown by `search_memory` |
| `reported_date` | when the fact was told; shown by `search_memory` as `reported: YYYY-MM-DD` |
| `created_at` | ordering of the baseline and the domain channel |
| `version`, `lineage_id`, `is_current`, `valid_from`, `valid_to` | §2.3–§2.4 |
| `ttl_days`, `expires_at`, `replaces_fact_id`, `last_updated` | written, read by no code (expiry is §10) |
| `visibility` | defaults to `account_shared`; never filtered on read: every member of an account sees every fact |

The adapter also stores `context_priority_rank` (critical 1 … archival 5) next to the entity for ordering
(`firestore_repo.py:32-38`, written at `:174-176` and `:449-451`).

### 2.2 The four axes

| Axis | Values (`entities.py`) | What code does with it |
|---|---|---|
| `domain` | `biographical`, `possession`, `health`, `medical_records`, `location`, `work`, `network`, `preference`, `agent_directive`, `skill`, `project`, `finance`, `education`, `legal`, `entertainment`, `communication` (`:18-35`) | baseline order (biographical first), standing directives (`agent_directive`), Router domain channel, Lelik's filter |
| `temporal_class` | `permanent`, `stable`, `dynamic`, `ephemeral` (`:38-43`) | none; shown to the consolidator. The prompt's TTLs (90 / 14 days) are not enforced by any code |
| `state` | `current`, `stale`, `archived`, `superseded`, `invalidated` (`:46-52`) | **the only visibility switch**: every reader filters `state == "current"` (`firestore_repo.py:223,277,383,475,680,962`). No code ever sets `stale` |
| `context_priority` | `critical`, `high`, `medium`, `low`, `archival` (`:55-61`) | baseline order; `archival` (rank 5) is never in the baseline or the rulebook (`firestore_repo.py:483`) |

The model writes these in upper case; `normalize_fact_taxonomy` lowercases them before the enums parse
(`entities.py:83-94`). A value outside the enum fails the tool call (§6). The prompt (as mirrored in `prompts_snapshot/` on main) tells the model to use
`HISTORICAL` as a priority, which is not an enum member: such a create fails. In the 30 days before 2026-10-21 none
of 87 written priorities was `historical` (`decisions/fact_s1_defects.md`); the vocabulary fix is in §9.7.

### 2.3 States and the SCD2 flags

`domain/fact_lifecycle.py:10-25` maps a state to its flags; every retirement path uses it (adapter update and merge,
Cabinet invalidate, the directive backstop):

| State | `is_current` | `valid_to` | Typical cause |
|---|---|---|---|
| `current`, `stale` | true | none | created; `stale` is never set |
| `archived` | true | now | "was true, no longer" (the prompt's Negation rule) |
| `invalidated` | true | now | "was never true", Stage 2b removal, Cabinet, backstop |
| `superseded` | **false** | now | a merge source, or the model marks a compound fact superseded after splitting it |

`is_current` means "latest document of its lineage", not "visible". Visibility is `state` alone.

### 2.4 Updates, merges, versions, lineage

- **An update overwrites the document in place.** `update_fact` changes `text`, `tags` (replaced wholesale), `metadata`
  (merged key by key), `temporal_class`, `state`, `reported_date` (`fact_management_adapter.py:283-343`). It cannot
  change `domain` or `context_priority`: those need a new fact. The old text is gone; there is no history of versions
  (TD-4; real versions are FACT_LIFECYCLE S2, §10).
- **`version` counts content changes**: it rises when text, tags, metadata or `temporal_class` change, not on a
  state-only update (`:312-336`). A content change also resets `reported_date` to now unless the call sends one
  (`:337-341`).
- **Vectors follow content**: any change to text, tags or metadata re-embeds all three before anything is written
  (`:317-322`); an embedding failure aborts the update with nothing stored.
- **A merge is a new fact with a new lineage.** `merge_facts` first marks each source `superseded` (skipping a source
  of another account or one not found), then creates the merged fact through `FactWriteService` with a fresh
  `lineage_id` (`fact_management_adapter.py:376-430`, `fact_write_service.py:169`). If the create fails after the supersedes, the sources stay
  superseded without a successor (S1 residual, `decisions/fact_s1_defects.md`).
- Concurrent tool calls of one model turn on the same fact are serialised by a process-local lock (`fact_management_adapter.py:218-231`).

### 2.5 Tenancy

Every read filters `account_id`. `update_fact` and `merge_facts` load by id and answer `not_found` / skip when the fact
belongs to another account (`fact_management_adapter.py:272-281,385-390`). Cabinet invalidate checks the account and
returns 403 (`firestore_repo.py:307-319`). `visibility` is not enforced (owner, 2026-10-21).

### 2.6 The three vectors

`domain/fact_embedding.py:11-18` defines the texts, shared by creation and re-embedding: `text`; `tags` joined (system
tags `consolidated`/`anchor` left out; the text when no tag is left); `metadata` as JSON (the text when empty). All
three are embedded with `RETRIEVAL_DOCUMENT` in one batch call (`fact_write_service.py:259-261`), model
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
  The values are global: `UserBotConfig.consolidation_threshold` / `consolidation_batch_size` are read only by
  `$consolidate` (§5.4).
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
`ConsolidationService.process_user_batches(max_batches=1)` (`consolidation_service.py:55-166`):

- resets recoverable batches first: any `failed` batch, and any `processing` batch older than 1800 s (a zombie), go
  back to `retry_pending` with attempts 0 (`firestore_consolidation_queue.py:100-162`);
- takes the **oldest** pending batch (`pending` or `retry_pending`, `order_by created_at`, `:37-44`; the composite
  index in `config/firestore.indexes.json` must be `READY`);
- marks it `processing`, runs the agent with `consolidate_full`, and on success **deletes the batch**;
- one batch per task: if more remain, the worker enqueues the next task at once (`worker_handler.py:457-459`; why:
  `decisions/consolidation_one_batch_per_task.md` — Cloud Run throttles CPU after a response, so each batch gets its
  own request).

An hourly sweep (`sweep_consolidation`, Cloud Scheduler at minute 17, `docs/07_deployment/SCHEDULERS.md`) enqueues a
task for every user with any batch left in the queue (`worker_handler.py:462-482`).

### Step 4 — the consolidation run

`ConsolidationAgent.execute` (`consolidation_agent.py:194-269`) needs `account_id` from the request context, loads the
baseline cache (`get_biographical_context_cached`, the whole cache: facts, principles, directives) and dispatches
`consolidate_full` → Stage 1, 2a, 2b (`_execute_deliberate_process_v3`, `:705-796`), then Stage 3 (`:634-681`). The
agent also accepts `consolidate` (Stage 1–2b only), `consolidate_cluster` and `consolidate_email` (`:189-192`); the
queue and `$consolidate` always send `consolidate_full`.

- **Model:** agent type `consolidation`, default provider Claude (fallback Gemini, `src/services/agent_context_builder.py:56-61`)
  at tier PERFORMANCE (`src/domain/user.py:82`), which on Claude is `claude-opus-5-5` unless the live env var
  `CLAUDE_PERFORMANCE_MODEL` pins another model (`src/adapters/claude_adapter.py:166-167,249-252`; `make claude-model`
  shows the live value). A user's `agent_providers` / `agent_tiers` can override. Thinking `medium`, 64 000 output
  tokens, 15 turns per pass, 15-minute agent timeout (`src/infrastructure/agent_config.py:166-190`).
- **Prompt:** one system prompt per pass, built by `PromptBuilder.build_for_agent(agent_type="consolidation",
  include_directives=False)`. The profile `prompts_snapshot/profiles/consolidation.yaml` assembles six tokens in order:
  `CONSOLIDATION_TAXONOMY`, `_COGNITIVE_PROCESS`, `_TOOLS`, `_EXAMPLES`, `_POLICIES`, `_OUTPUT_SPEC` (blueprint
  `consolidation_agent_v1`). Then a `knowledge_base {}` block with the baseline facts and, in Stage 1 only, the
  conversation (`src/services/prompt_v3/prompt_assembly_service.py:449-459`). Standing directives are never in this
  prompt as rules: the consolidator curates them as records (Stage 2b).
- **Loop:** `_run_consolidation_loop` (`consolidation_agent.py:867-961`): each turn sends the whole history with the five tool
  declarations; tool calls of one turn run in parallel (`:1251-1257`); a turn with no tool call is the final report.

### Step 5 — Stage 1: conversation → facts

The conversation sits in the system prompt as lines `User (Oct 22, 10:00 UTC): …` / `Assistant (…): …`
(`src/services/prompt_v3/context_formatter.py:52-63`); the user message is just "Begin deliberate consolidation."
(`consolidation_agent.py:751-757`). The token's 8 steps: extract candidates → classify on the four axes → **search
once per candidate** → analyse → decide → execute → verify → report (`CONSOLIDATION_COGNITIVE_PROCESS.groovy`).

| Tool | What the code does (`consolidation_agent.py:1155-1249`, adapter `fact_management_adapter.py`) |
|---|---|
| `search_existing_facts(keywords, primary_query, alternative_query, limit=20)` | `enrich_context` with 6 vector channels (phrase 2 = alternative, else primary), per-channel limits 7/10/10, total `limit`, **no semantic dedup** (`fact_management_adapter.py:89-100`); returns id, text, domain, temporal class, state, priority, tags, metadata, reported date, version. `similarity` is **null** for every vector hit (§4.3) |
| `create_fact(content, fact_attributes)` | requires `domain`, `temporal_class`, `context_priority` (`fact_management_adapter.py:165-173`); state defaults to `current`, priority to `medium`; written **without deduplication** (`skip_deduplication=True`, `fact_management_adapter.py:192-197`) |
| `update_fact(fact_id, updates)` | §2.4 |
| `merge_facts(fact_ids, merged_content, fact_attributes)` | §2.4 |
| `count_words(text)` | word count against the 40-word fact limit (`consolidation_agent.py:1223-1235`) |

**Deduplication is the model's decision.** Code does not reject a near-duplicate on this path; the prompt makes the
model search first and DISCARD (no tool call) what is already known. The number-aware 0.96 dedup of
`add_fact_if_unique` (`firestore_repo.py:641-732`) runs only on the legacy v2 path (`prompt_version="v2"`), which is
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
  `superseded`).
- `Directive_Maintenance` (TAXONOMY): `agent_directive` only when the rule is about **how the agent behaves** AND is
  **in force across most requests**; a conditional agent rule becomes `preference` (§4.2).

The tracker `_TrackingFactManagement` records every fact Stage 1 created, updated or merged (`consolidation_agent.py:47-99`).

### Step 6 — Stage 2a: cluster review (only if Stage 1 wrote)

Skipped when Stage 1 wrote nothing (`consolidation_agent.py:766-768`). For each written fact it searches the store (words longer than 3
characters among the first 10 as keywords, the text as phrase, limit 20), unions the hits by id, sorts by length and keeps 30
(`_build_review_cluster`, `:972-1018`). A fresh loop gets the "FACT CLUSTER REVIEW" message: merge repeats, decompose
compounds, fix inconsistencies, no fact over 40 words, keep numbers and dates (`:1111-1138`). It sees no conversation.

### Step 7 — Stage 2b: the directive rulebook (every run)

Unconditional, even when Stage 1 did nothing (`consolidation_agent.py:792-794`); a no-op only when the rulebook is
empty. It reads up to 40 current non-archival `agent_directive` facts (`_review_directives`, `:798-843`) and sends the whole rulebook with the "STANDING DIRECTIVES REVIEW" message
(`:1021-1108`): one checklist line per record (`scope_ok`, `reminder_dup`, `unusable`), then per record DEMOTE
(create a `preference` fact stating the condition + invalidate the directive), INVALIDATE, MERGE, or rewrite to the
target form (imperative, English, terse, one rule), hard cap 15. Then the **code backstop** `_enforce_directive_cap`
(`:845-865`): if more than 15 current directives remain, the ones after the 15th in (priority, newest first) order are
invalidated. Run-to-run results differ on identical input (`decisions/directive_applicability_gate.md` § Variance).

### Step 8 — Stage 3: indexed emails

Runs only after Stage 1–2 succeeded (`consolidation_agent.py:648-652`). Up to 100 unconsolidated `IndexedEmail` facts (1 pass,
`agent_config.py:186-187`) go to a fresh loop as a numbered candidate list under a "[system_alert] … scanned the
user's email inbox" message; afterwards all of them are marked consolidated, whatever the model did (`consolidation_agent.py:568-614`).
Skipped when the repository is not wired or nothing is pending.

### Step 9 — the baseline is rebuilt; the batch is deleted

After Stage 1–2b, `refresh_biographical_context_cache` rebuilds the `{prefix}user_context` document
(`consolidation_agent.py:335-346`, `firestore_repo.py:781-842`); Stage 3 rebuilds it again when it processed emails.
The refresh logs and swallows its own errors. `ConsolidationService` then deletes the batch (§3 step 3).

### Step 10 — the next conversation

Smart reads the cache document on every request (no in-process copy, `smart_response_agent.py:425-435`), so the new
baseline applies from the next message after the run. A new fact is searchable by `search_memory` from the moment it
is written. Whether it is in the baseline depends on §4.1.

### 3.11 Loop alerts and stats lines

`ConsolidationPassStats` (`src/domain/consolidation_pass_stats.py`) counts each pass's tool calls (no arguments, no
fact text, 8 characters of the user id):
- 🚨 permanent: a pass that uses all 15 turns without a final report logs an ERROR and posts to the ops sink (`:108-115`,
  `consolidation_agent.py:958-961`). The pass returns no operations; the writes it made stay.
- 📊 temporary: while `CONSOLIDATION_STATS_ALERTS` is on (`os.getenv`, default on), every pass that ends with a report
  posts one line: pass label (`stage1`, `stage2a`, `stage2b`, `email`, `cluster`), model, effort, turns, tool counts,
  repeated turns, writes, report ops, seconds, per-turn sequence (`:98-106`).

Why: `decisions/sonnet55_adaptive_thinking_stall.md`.

---

## 4. Reading memory

### 4.1 The baseline: what the bot sees in every message

**Who gets it.** Every agent whose prompt is built with the biographical cache: Smart and Quick (merged with the
Router's prefetch), Notes, Tasks, EmailSearch, PdfGenerator, the consolidator. Lelik gets a filtered copy (§4.6). The
Router, MemorySearch, web, compute, document and drive agents build without it (`include_biographical=False`).

**How it is chosen** (`BiographicalContextService.refresh_context`, `biographical_context_service.py:72-208`):
1. Limits: `biographical_cache_limit` (default **65**) and `principles_cache_limit` (default **20**)
   (`src/domain/settings.py:49-50`), overridable only at account level (`account.account_defaults`, `biographical_context_service.py:104-122`; the user
   profile's own fields are not read here).
2. Up to 65 current, non-archival `biographical` facts, ordered by priority rank then newest first
   (`get_active_facts_ordered`, `firestore_repo.py:456-511`).
3. If fewer than 65, fill from the 65 top facts of all domains in the same order, minus those already taken and minus
   directives (`biographical_context_service.py:150-159`). So other domains enter only when biographical facts leave room.
4. Facts tagged `mindset` become **principles** (up to 20; extras are dropped, not moved to facts); the rest are facts
   (`biographical_context_service.py:188-194`).
5. Directives are fetched separately: up to 15 current non-archival `agent_directive` facts (`biographical_context_service.py:144-148`).
6. Each cached item carries only id, text, domain, tags, priority, created_at (`biographical_context_service.py:178-186`).

**How it is rendered.** `PromptBuilder.build_for_agent` splits the list (`src/services/prompt_builder.py:183-202`):
directives → `standing_directives {}` (§4.2); items tagged `semantic_lens` (the Router's prefetch) → `query_specific_context`
after the cache boundary; the rest → `knowledge_base { biographical_context }`, grouped by domain, biographical first
(oldest → newest), other domains alphabetically (newest → oldest), each line with its recorded date, a `preference`
principle prefixed `[MINDSET]` (`src/services/prompt_v3/biographical_formatter.py:56-180`),
under the note "Dates = when recorded … conversation history takes precedence if contradictory"
(`prompt_assembly_service.py:452-454`). Facts and directives pass the security validator as untrusted text.

**When it changes:** only at the end of a consolidation run (§3 step 9). A Cabinet invalidate does not rebuild it (§5.2).

### 4.2 Standing directives

`agent_directive` facts are the owner's standing orders to the agent, rendered verbatim as the last static block,
`standing_directives {}`, newest first (`biographical_formatter.py:87-98`, `prompt_assembly_service.py:477-483`).
Every agent that gets the baseline gets them, unless it passes `include_directives=False` (the consolidator, Lelik and
his summarizer, Tutor and its extractor, WebSearchLight, the drive model agents). Bounds: the rulebook is curated to 15
by Stage 2b and its backstop; the cache holds at most 15 (`DEFAULT_DIRECTIVES_CACHE_LIMIT`, `settings.py:55`).

**Why a directive disappeared**, in order of likelihood: Stage 2b demoted it to `preference` (a new fact exists, the
directive is `invalidated`); Stage 2b invalidated it (reminder duplicate, unusable, superseded); Stage 2b merged it (a
new fact with a new id; the sources are `superseded`); the backstop cut it (over 15, lowest priority, then oldest);
its priority is `archival`; the owner invalidated it in the Cabinet. The 📊 `stage2b` line of that run shows the
writes; BigQuery shows the checklist (§9.4).

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
`find_nearest` calls at 30 (`firestore_repo.py:44`).

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
(`lelik_agent.py:105`). Anything else he reaches through `search_memory` (in his allowlist) or by asking Alek.

### 4.7 The claude.ai connector (MCP)

`get_user_context(query, alternate_phrasing, keywords)` (`src/composition/mcp_setup.py:255-332`) calls
`enrich_context` directly (no model, no Router) with phrase 2 = alternate phrasing or the query, dedup at 0.98, total
30 (`main.py:1291-1298`), as the token's account. The answer groups facts by domain, one line each (`mcp_setup.py:128-144`).

### 4.8 The Cabinet fact page

Read routes (`src/web/user_cabinet_app.py:766-847`): all current facts; a paginated browser, newest first, optional
domain filter; a single-vector search over `vector` (50 results). They show text, domain, type and created date.

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
   `$memory save <code>` message (`conversation_handler.py:397-420`).
3. Pasting the command (`_handle_memory_command`, `conversation_handler.py:1820+`) runs `MemoryFactService.confirm`
   (`memory_fact_service.py:43-77`): it appends a **user/model pair** to the origin session — the user part
   "[System: the owner reviewed and confirmed these facts … read from: <sources>.]" plus one line per fact
   (`memory_fact.py:66-80`) — and only then deletes the draft. A failed append keeps the draft for a retry.
4. From there the pair is an ordinary pair of messages: it becomes facts at the channel's next overflow (§7). Until
   then `search_memory` cannot find them; Smart sees the pair only while it is among the last 30 messages.

### 5.2 Cabinet invalidate

`POST /api/user/facts/<id>/invalidate` (`user_cabinet_app.py:849-863`) → `invalidate_fact` (`firestore_repo.py:307-319`):
account check, `state=invalidated`, `valid_to` and `last_updated` now. Search stops returning it at once. **The
baseline cache is not rebuilt**: a baseline fact stays in every prompt until the next consolidation run of that
account (§7).

### 5.3 The directive cap backstop

§3 step 7. It writes through the same `update_fact` (state only, so no version bump, no re-embed).

### 5.4 `$consolidate`

`ConversationHandler` (`conversation_handler.py:1506-1600`) cuts the oldest `batch_size` messages of the current
session (the user's `consolidation_batch_size` override, else 50), queues them, saves the trimmed session, posts a
"started" note, then processes **all** pending batches of the user in the same request and notifies the channel. It
uses its own copy of the serializer (same rules as §3 step 3).

---

## 6. Outcomes: tool calls and failures

| Where | What happens | Facts | Batch | Retried? |
|---|---|---|---|---|
| tool call ok | result JSON back to the model | written at once | — | — |
| tool raises (Firestore, embedding) | `{"error": …}` returned to the model (`consolidation_agent.py:1244-1249`) | nothing of that call | — | the model may retry in a later turn |
| `create_fact` missing a required attribute | `status: failed`, message names the fields (`fact_management_adapter.py:165-173`) | none | — | the model may retry |
| bad enum (`domain`, `state`, `temporal_class`, `context_priority`, e.g. `HISTORICAL`) | `ValueError` caught → `status: failed` with the message (`fact_management_adapter.py:210-216,250-256`; `fact_write_service.py:156-163`) | none for that call | — | the model may retry |
| `update_fact` on an unknown or foreign id | `status: not_found` (`fact_management_adapter.py:263-281`) | none | — | — |
| `merge_facts` with no source supersedable | `status: failed` (`fact_management_adapter.py:398-406`) | none | — | — |
| `merge_facts` create fails after the supersedes | `status: failed` | **sources stay superseded, no successor** | — | no |
| final report not JSON | parsed loosely, else `[]` (`consolidation_agent.py:1395-1445`) | writes stand | succeeds | no (the report is cosmetic) |
| a pass hits 15 turns | 🚨, pass returns `[]` (`consolidation_agent.py:958-961`) | writes so far stand; candidates not yet written are lost | **succeeds and is deleted** | no |
| Stage 1/2a/2b raises (provider down after retries, prompt build fails, timeout of 15 min) | `AgentResponse.failure` | writes so far stand | attempts +1 → `retry_pending`; the worker re-enqueues at once; at 3 → `failed` + `CONSOLIDATION_FAILED` log (`consolidation_service.py:129-153`) | yes, the whole batch; Stage 1 runs again over facts it already wrote (the model should find and discard them) |
| Stage 3 raises | not caught inside the email stage, so the run fails (`consolidation_agent.py:524-632`) | Stage 1–2b writes stand | as above | yes, Stage 1 again |
| batch `failed` | revived by the next task for the user or the hourly sweep: attempts back to 0 (`firestore_consolidation_queue.py:100-162`) | — | 3 more attempts each hour, without end | yes |
| worker crashes mid-batch | batch stays `processing`; after 1800 s it is a zombie and is reset | partial writes stand | retried | yes |
| baseline refresh fails | logged, swallowed (`firestore_repo.py:838-842`) | stored | succeeds | old baseline until the next run |
| enqueue of the batch fails at overflow | logged `Error in overflow routing` (`overflow_routing_service.py:64-69`) | — | **lost**: the messages were already cut from the session | no |
| batch stored but the Cloud Task enqueue fails | logged | — | waits in the queue | the hourly sweep |
| provider failover | `_call_llm` fails over to Gemini only before the first tool call; after it the transcript is locked to one provider (`src/agents/CLAUDE.md` → Transcript integrity) | — | — | same-provider retries |

---

## 7. Timelines

**A fact told today.**
1. *Seconds:* it is in the session. Smart sees it while it is among the channel's **last 30 messages**
   (`agent_config.py:104`), and Lelik if it is the primary channel.
2. *Gap:* once 30 newer messages exist it is out of Smart's history but not yet in memory; nothing finds it.
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
- At consolidation: the consolidator searches, finds the old fact and either updates it in place (version +1, the old
  text is gone, vectors recomputed) or archives it and creates a new one. A stable identity fact needs an explicit
  correction (`Core_Identity_Caution`).
- After the run: search returns the new text at once; the baseline shows it from the next message.

**A Cabinet invalidate.** Search stops returning the fact immediately; the baseline keeps showing it until the next
consolidation run of the account rebuilds the cache.

**A directive** ("always answer in bullet points").
- Consolidated with its batch (above). Stage 1 classifies it: `agent_directive` only if it is about agent behaviour
  and holds for most requests; otherwise a conditional `preference` reached by search.
- Stage 2b of the same run already sees it (it reads the rulebook after Stage 1) and may rewrite, merge or demote it.
- It is in `standing_directives` from the next message after the run.

---

## 8. Costs

**Which model.** Claude PERFORMANCE: `claude-opus-5-5` by code default, or the model pinned by the live
`CLAUDE_PERFORMANCE_MODEL` (`make claude-model`; `make claude-forward` pins `claude-sonnet-5-5`, `make claude-rollback`
`claude-sonnet-5`). The decision of 2026-10-20 records consolidation on Sonnet 5.5
(`decisions/sonnet55_adaptive_thinking_stall.md`); whether that pin is live is not in the repo.

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
- `♻️ Reset N stale PROCESSING batches` / `♻️ Reset N FAILED batches` / `⏳ Skipped N live PROCESSING batches`
- `📦 [Librarian] Processing batch <batch_id> (<n> messages)`
- `👨‍🏫 [ConsolidationAgent] Turn i/15`, `Stage 1 done: N ops, M facts written`, `Stage 2a: reviewing cluster of N facts`,
  `Stage 2b: reviewing N directives`, `⚖️ … Directive cap backstop`, `⚠️ … Max turns reached without final report`
- `🔧 [FactManagement] create_fact() TOOL CALL:` (logs the content and metadata), `✅ … Updated fact <id8> to version N`,
  `🔀 … Merging N facts`, `❌ Tool <name> failed`
- `✅ [BiographicalContext] Cache refreshed for <acct>: N facts, M principles, K directives`
- `✅ [Librarian] Batch <batch_id> consolidated: stage1_ops=…, email_batches=… → DELETED`; or
  `⚠️ [Librarian] Batch <batch_id> failed (attempt n/3)`; or `❌ [Librarian] CONSOLIDATION_FAILED batch_id=… attempts=3 …`
- `[Worker] sweep_consolidation complete: swept=N`
- search: `🔍 [SearchEnrichment] Final: N facts (semantic_dedup: …, bio_dedup: …)`, `🔍 [find_nearest] START/DONE`

### 9.2 Why did the bot not remember X?

1. Was it said in a channel that consolidates? Companion-bound channels do not (§1.4).
2. Has that channel overflowed since? Count messages since (§7). If not: it is waiting (`$consolidate` forces it).
3. Was the batch consumed? Grep the batch id: `DELETED`, `failed`, or still in `{prefix}consolidation_queue`.
4. What did Stage 1 decide? BigQuery `prompt_content` for that run: the candidate list, the searches, the DISCARD
   reasons in the thinking and the final report. Usual reasons: echo trap, Trivial_Exclusions, judged a duplicate.
5. Is the fact stored but not `current` (archived, invalidated, superseded)? Readers show only `current`.
6. Stored and current but not used: not in the baseline (§4.1 selection), and this message's prefetch did not rank it
   (§4.3). Try `search_memory` with other words; check its domain and priority.

### 9.3 Why did it remember something wrong?

- From the bot's own words: a bot reply reaches the consolidator only as its summary (§3 step 1); `Domain_Scope`
  forbids storing assistant statements unless the user adds new information. When this happens it is a model error;
  the run's BigQuery rows show which message it came from.
- A stale value: no expiry exists (§10); `temporal_class` TTLs are not enforced. An update overwrote the text in place
  and the earlier wording is gone (§2.4).
- From an email: Stage 3 writes facts from indexed emails (§3 step 8).
- Fix: invalidate it in the Cabinet (then the baseline catches up at the next run), or tell the bot the correction.

### 9.4 What did consolidation do in a given batch?

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

### 9.6 The data audit (added by the data-audit PR)

`python -m scripts.data_audit.run facts [--account <id>] [--limit N]` (branch `claude/gallant-mendel-ocl9xp-p21`,
not on main at the time of writing) streams the facts collection read-only and checks every stored document against
the invariants: stored `id` equals the document id, `account_id` and `state` present and valid, `is_current` /
`valid_to` matching `state` (§2.3), `created_at` present, `reported_date` a timestamp, all three vectors stored as
vectors, the document parses as a `FactEntity`, and no lineage with more than one `is_current` document. It prints
counts only and writes the offending ids per class to `scripts/memory/data_audit_facts_<time>.json` (gitignored; no
text or vectors). Run it after any migration or backfill.

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

`docs/10_rfcs/FACT_LIFECYCLE_RFC.md` (rev 2, accepted 2026-10-21) — S1 (defect fixes) is live; S2–S5 land together:
- **S2** real versions (a new document per content change, `superseded_by`), one writer, transactions, validation
  before any write.
- **S3** expiry decided from the content (date / horizon / none), a daily zero-LLM janitor (log-only first),
  `HISTORICAL` → `ARCHIVAL`, prompt changes.
- **S4** a review pass over overdue facts and questions to the owner in chat.
- **S5** one-time classification of all current facts, review dry run, janitor on.

Until then: nothing expires, `stale` is never set, updates overwrite in place.

---

## 11. Code map

| File | Role |
|---|---|
| `src/domain/entities.py` | `FactEntity`, the four enums, `normalize_fact_taxonomy` |
| `src/domain/fact_lifecycle.py` | state → `is_current` / `valid_to` |
| `src/domain/fact_embedding.py` | the three embedding texts |
| `src/domain/consolidation.py` | `ConsolidationBatch`, `BatchStatus` |
| `src/domain/consolidation_serialization.py` | messages → batch (summary vs full text) |
| `src/domain/consolidation_pass_stats.py` | 📊 / 🚨 texts |
| `src/domain/settings.py` | `ConsolidationSettings` (70/50), `SearchConfig` (limits, baseline 65/20, directives 15) |
| `src/domain/search.py`, `src/domain/rrf.py`, `src/domain/deduplication_service.py`, `src/domain/vector_math.py` | search DTOs, RRF, number-aware dedup, cosine |
| `src/domain/memory_fact.py` | `$memory save` draft, preview and history pair |
| `src/adapters/firestore_session_store.py` | sessions, sliding window, 90-day TTL |
| `src/services/overflow_routing_service.py` | batch → Alek's queue or a companion's |
| `src/adapters/firestore_consolidation_queue.py` | queue, oldest first, zombie and failed reset, sweep query |
| `src/adapters/gcp_task_queue.py` | `enqueue_consolidation_task` |
| `src/handlers/worker_handler.py` | `consolidation`, `sweep_consolidation` |
| `src/services/consolidation_service.py` | one batch: run, delete or retry |
| `src/handlers/consolidation_handler.py` | shim used by `$consolidate` |
| `src/agents/consolidation_agent.py` | all stages, tools, loop, backstop, cluster and directive messages |
| `src/infrastructure/agent_config.py` | `ConsolidationAgentConfig`, `MemorySearchAgentConfig`, Router and Smart windows |
| `src/composition/user_agent_factory.py` | builds the consolidator, the per-user `SearchEnrichmentService`, MemorySearch |
| `src/services/agent_context_builder.py`, `src/domain/user.py` | provider and tier per agent |
| `src/ports/fact_management_port.py`, `src/adapters/fact_management_adapter.py` | consolidator tools |
| `src/ports/fact_write_port.py`, `src/services/fact_write_service.py` | fact creation with vectors |
| `src/ports/repository.py`, `src/adapters/firestore_repo.py` | every fact query, the cache document, Cabinet invalidate |
| `src/services/biographical_context_service.py`, `src/services/configuration_service.py` | baseline selection and limits |
| `src/services/prompt_builder.py`, `src/services/prompt_v3/{prompt_assembly_service,biographical_formatter,context_formatter}.py` | channel split, rendering of baseline, directives, conversation |
| `src/ports/search_enrichment_port.py`, `src/services/search_enrichment_service.py` | multi-channel search |
| `src/agents/core/router_agent.py` | prefetch |
| `src/agents/core/smart_response_agent.py` | baseline + prefetch merge, `propose_memory_facts` |
| `src/agents/memory_search_agent.py` | `search_memory` |
| `src/services/lelik_persona_service.py` | Lelik's facts |
| `src/composition/mcp_setup.py` | `get_user_context` |
| `src/infrastructure/memory_fact_tools.py`, `src/services/memory_fact_service.py`, `src/adapters/firestore_memory_fact_draft_repository.py` | `$memory save` |
| `src/handlers/conversation_handler.py` | `$consolidate`, `$memory save`, memory preview delivery |
| `src/web/user_cabinet_app.py` | Cabinet fact routes |
| `prompts_snapshot/{profiles/consolidation.yaml,blueprints/consolidation_agent_v1.yaml,tokens/system/CONSOLIDATION_*.groovy}` | the consolidation prompt (mirror) |
| `scripts/consolidation/`, `scripts/data_audit/` (PR) | benches, audit |
