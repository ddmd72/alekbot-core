# Sliding Window Consolidation (Building Block)

## Purpose

Describes the memory pipeline that transforms short-term conversation history into
long-term structured knowledge via `ConsolidationAgent`.

## When to Read

- Before modifying the consolidation logic, batching strategy, or fact extraction prompt.
- When troubleshooting missing facts, consolidation delays, or memory usage issues.
- When adding new intents or stages to `ConsolidationAgent`.

## When to Update

This document MUST be updated when:

- [ ] The sliding window threshold or batch size logic changes.
- [ ] The consolidation trigger mechanism (overflow callback) is modified.
- [ ] The `ConsolidationAgent` prompt, tools, or stage logic changes.
- [ ] New fact types or metadata fields are added.
- [ ] The background processing infrastructure (Cloud Tasks) is updated.
- [ ] `ConsolidationAgentConfig` parameters change.

## Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Biographical Context Cache:** [../biographical_context_cache/README.md](../biographical_context_cache/README.md)
- **Fact Write Service:** [../fact_write_service/README.md](../fact_write_service/README.md)
- **Cluster Review RFC:** [../../10_rfcs/CLUSTER_REVIEW_RFC.md](../../10_rfcs/CLUSTER_REVIEW_RFC.md)

---

## 1. Overview

Alek-Core implements a **Dual Memory System** inspired by human cognitive processes:

1. **Short-Term Memory (Hot):** Recent conversation history (sliding window in Firestore).
2. **Long-Term Memory (Cold):** Consolidated facts and principles (vector storage).

**Sliding Window Consolidation** is the pipeline that bridges these two systems.

---

## 2. The 3-Stage Pipeline

`ConsolidationAgent` runs three stages in sequence when triggered by overflow or `$consolidate`:

```
Stage 1 — Conversation consolidation
  Reads conversation batch from session store → extracts/updates/merges facts via tool loop

Stage 2 — Inline cluster review  (inline_cluster_review=True)
  For each fact written in Stage 1 → semantic cluster search → merge/decompose/supersede
  Skipped automatically if Stage 1 wrote 0 facts.

Stage 3 — Email triage
  Fetches unconsolidated emails (IndexedEmail) → extracts facts from email content
  Runs independently of Stage 1/2 success count.
```

### 2.1 Trigger: Sliding Window Overflow

- `FirestoreSessionStore.append_messages_batch()` monitors history length.
- Threshold: default 100 messages. Batch size: default 50.
- On overflow → `overflow_callback` → `ConsolidationHandler` enqueues a Cloud Tasks
  `task_type=consolidation` worker request.
- Worker HTTP request stays alive for the entire run → full CPU allocation on Cloud Run.

### 2.2 Intent API

`ConsolidationAgent` exposes 4 internal intents (all `internal=True` — not visible to LLM):

| Intent | Payload | What it does |
|--------|---------|--------------|
| `consolidate` | `{messages?: List[Dict]}` | Stage 1 only (+ optional inline Stage 2). Messages auto-fetched from session store if absent. |
| `consolidate_cluster` | `{cluster: List[Dict]}` or `{limit: int}` | Stage 2 only. Pre-built cluster or auto-fetch top-N longest facts. |
| `consolidate_email` | `{number_of_batches?: int, batch_size?: int}` | Stage 3 only. Fetches unconsolidated emails internally. |
| `consolidate_full` | `{}` | Full pipeline: Stage 1 → Stage 2 → Stage 3. Auto-fetches everything. Triggered by overflow and `$consolidate`. |

`ConsolidationHandler` always sends `Intent.CONSOLIDATE_FULL`.

---

## 3. Stage 1 — Conversation Consolidation

### 3.1 Cognitive process

8-step deliberate process defined in `COGNITIVE_PROCESS_CONSOLIDATION.groovy` Firestore token:

1. **EXTRACT** — identify candidate facts from messages
2. **CLASSIFY** — assign domain, type, tags
3. **SEARCH** — search existing facts via `search_existing_facts` tool (multi-vector RRF)
4. **ANALYZE** — compare candidates against existing; check for duplicates
5. **SIZE GATE** — if existing fact has `word_count > 40` AND planned op is UPDATE:
   - Co-location test: do all sub-claims belong together?
   - If not → decompose into atomic facts + SUPERSEDE original
6. **DECIDE** — finalize operations (CREATE / UPDATE / MERGE / DISCARD / SUPERSEDE)
7. **EXECUTE** — call fact management tools
8. **REPORT** — emit JSON `{"operations": [...]}` as final response

### 3.2 Multi-turn loop

`_run_consolidation_loop()` — shared by all three stages:

```python
for turn in range(MAX_CONSOLIDATION_TURNS):  # default 15
    response = await self._call_llm(request)
    if response.tool_calls:
        # execute tools, append results, continue loop
    else:
        # parse JSON report → done
```

**Turn budget:** `MAX_CONSOLIDATION_TURNS = 15` (raised from 10 in 2026-03-06 after Stage 2
on large clusters hit the limit; 25-fact cluster needs ~12 turns).

### 3.3 Fact management tools

Provided via `FactManagementPort` (injected at construction):

| Tool | Description |
|------|-------------|
| `search_existing_facts` | Multi-vector RRF search (text + tags + metadata vectors) |
| `count_words` | Returns word count of a fact's content |
| `create_fact` | Write new fact via `FactWriteService.add_facts_batch` |
| `update_fact` | SCD2 update (new version, old marked inactive) |
| `merge_facts` | Merge N facts into one (new content, old superseded) |
| `discard_fact` | Mark fact as discarded (soft delete) |

### 3.4 `_TrackingFactManagement`

Module-level pass-through wrapper active only during Stage 1. Records `(fact_id, content)`
for every CREATE / UPDATE / MERGE. `tracker.changed` seeds Stage 2 cluster selection.
Does not affect production writes.

### 3.5 `MessagePart.consolidation_text` — Explicit-Save Injection

**Problem:** Native binary parts (`file_data`) are dropped from history after the turn. When the
consolidation serializer runs `[{"text": p.full_text or p.text} for p in msg.parts ...]`, file-only
user messages produce an empty list. `ConsolidationAgent`'s `Domain_Scope` policy ("NEVER process
ASSISTANT statements as facts unless USER confirms with NEW information") then discards any model
commentary about the file. Facts extracted from uploaded files do not survive consolidation.

**Solution:** `MessagePart.consolidation_text` is an optional field on every `MessagePart`. It is:
- Set by `ConversationHandler` when `FactsMemoryAgent` returns `history_context={"consolidation_text": text}` (see `save_to_memory` intent).
- Appended as a new `MessagePart(consolidation_text=combined)` to the user message before it is persisted.
- Serialized and deserialized by `FirestoreSessionStore` alongside other part fields.
- Read by both consolidation serialization paths:

```python
# overflow_callback (main.py) and $consolidate (conversation_handler.py)
"parts": [
    {"text": p.full_text or p.consolidation_text or p.text}
    for p in msg.parts
    if p.full_text or p.consolidation_text or p.text
]
```

**Invariant:** `consolidation_text` is **never read by any LLM adapter**. All adapters iterate
`part.text` / `part.full_text` / `part.file_data` only. The field is consolidation-pipeline-only
and cannot leak into agent or LLM context under any circumstances.

**Why this works for `Domain_Scope`:** The passage is attached to the *user* message's parts, not
the model message. `ConsolidationAgent` sees it as user-side content → the policy does not block it.

---

## 4. Stage 2 — Inline Cluster Review

### 4.1 When it runs

After Stage 1, if `INLINE_CLUSTER_REVIEW = True` (default) AND `tracker.changed` is non-empty.

Toggle: `ConsolidationAgentConfig.inline_cluster_review: bool = True`

### 4.2 Cluster construction

`_build_review_cluster()` — for each `(fact_id, content)` from `tracker.changed`:

```python
results = await fact_management.search_existing_facts(
    keywords=tags[:10],
    primary_query=content[:300],
    alternative_query=f"{domain} {' '.join(tags[:5])}",
    limit=cluster_size + 1,   # +1 to exclude self
)
# exclude anchor from results, keep top-N by RRF rank, deduplicate
```

**No similarity threshold** — RRF scores are low by nature; any absolute threshold
eliminates entire clusters. Top-N by rank only.

### 4.3 Cluster message

`_build_cluster_message()` sends the system alert from the Cluster Review RFC (§4):

```
SYSTEM MAINTENANCE — FACT CLUSTER REVIEW
...
Hard limit: no fact may exceed 40 words. Every fact that exceeds 40 words must be
either rephrased or decomposed into atomic facts each under 40 words.
Co-location is not a valid justification for exceeding this limit.
```

The system alert describes WHAT to do, not HOW. The HOW is handled by the cognitive
process. Adding tool instructions to the system alert degrades output.

### 4.4 Relationship to scheduled ClusterReviewService

Two complementary review mechanisms:

| Mechanism | When | Scope |
|-----------|------|-------|
| Inline Stage 2 (this) | After every consolidation run | Facts touched in current batch |
| Scheduled ClusterReviewService (planned) | Daily via Cloud Scheduler | All accumulated facts |

---

## 5. Stage 3 — Email Triage

### 5.1 What it does

Fetches `IndexedEmail` records with `is_consolidated=False` in batches, processes each
batch through the consolidation LLM loop, marks emails as consolidated.

### 5.2 Configuration

```python
# ConsolidationAgentConfig defaults
email_triage_passes: int = 1       # number of passes (batches)
email_triage_batch_size: int = 100 # emails per batch
```

Both can be overridden in the payload: `{number_of_batches: int, batch_size: int}`.

### 5.3 Skipped when

- `IndexedEmailRepository` not injected (not wired in factory).
- No unconsolidated emails for this account.

---

## 6. Configuration Reference

All parameters in `src/infrastructure/agent_config.py` → `ConsolidationAgentConfig`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_turns` | 15 | Max LLM turns per stage. Raised to 15 after Stage 2 on 25-fact cluster hit the 10-turn limit. |
| `temperature` | 1.0 | Model default. Zero-temp was abandoned — it degrades extraction quality on current models. |
| `facts_limit` | 50 | Biographical facts loaded into context per stage. |
| `principles_limit` | 15 | Principles loaded into context per stage. |
| `max_tokens` | 64_000 | Output token limit. Large headroom for Gemini 3 Pro thinking tokens + full fact JSON. |
| `thinking_effort` | `"medium"` | Claude extended thinking effort. `None` disables thinking. |
| `inline_cluster_review` | `True` | Run Stage 2 inline after Stage 1. |
| `email_triage_passes` | 1 | Stage 3 default passes. |
| `email_triage_batch_size` | 100 | Stage 3 default batch size. |
| `timeout_ms` | 900_000 | Agent timeout (15 min). Covers Stage 1 (~4 min) + Stage 2 (~6 min) + Stage 3 (~4 min). |

---

## 7. Cloud Tasks Wiring

### 7.1 Dispatch

`GcpTaskQueue.enqueue_consolidation_task(user_id)`:

```python
task = {
    "http_request": { "url": f"{service_url}/worker", "body": payload },
    "dispatch_deadline": duration_pb2.Duration(seconds=1800),  # 30 min
}
```

`dispatch_deadline = 1800s` (30 min) exceeds `timeout_ms = 900s` (15 min). This ensures
Cloud Tasks does not kill the HTTP request before the agent has time to finish.

### 7.2 Worker

`WorkerHandler` receives `task_type=consolidation` and calls:

```python
await consolidation_handler.run_consolidation_process(user_id)
```

which sends `Intent.CONSOLIDATE_FULL` to `ConsolidationAgent` via `AgentCoordinator`.

---

## 8. Debug Logging

Consolidation LLM turns are captured to the BigQuery content store like every other agent —
per LLM call, from the single call site `BaseAgent._call_llm()` → `PromptContentStore.record_turn()`.
All three stages (each turn of each stage) land as rows in
`alek_observability_dev.prompt_content` (day-partitioned, 30-day TTL), gated by `DEBUG_PROMPTS=true`
AND `BIGQUERY_PROMPT_DATASET` set.

Each row carries `agent_type`, `model`, `provider`, `turn`, full `request_text` / `response_text`,
`tool_calls`, and token counts. To read a single `consolidate_full` run, query by `trace_id` and
order by `turn`. See the [Agent Logging Guide](../agent_logging/README.md) for the `bq` query form.

---

## 9. Operational Notes

### 9.1 Cloud Run CPU Throttling (resolved 2026-02-24)

Cloud Run throttles CPU to ~5% when no HTTP request is active. The original
`asyncio.create_task()` for consolidation ended the HTTP request immediately → CPU starvation
→ `find_nearest` degraded from ~700ms to **74–180 seconds**.

**Fix:** overflow → `enqueue_consolidation_task()` (Cloud Tasks sends a separate
`POST /worker`). The worker HTTP request stays alive for the entire run.

### 9.2 Per-Session Worker Serialization

`HTTPModeAdapter._session_locks` (WeakValueDictionary) in `src/adapters/slack/http_adapter.py`
ensures only one worker per `thread_ts` runs at a time. Concurrent workers return HTTP 429 →
Cloud Tasks retries after backoff.

### 9.3 Startup Vector Index Warmup

`main.py` fires 3 parallel `find_nearest` calls against `_warmup` account at startup to
load all vector indexes before the first real consolidation run. Cold-start `find_nearest`
latency is 40–60s; warm latency is 700ms–1.2s.

### 9.4 Stage 2 turn budget

Stage 2 on a 25-fact cluster consumes turns as follows:
- **Turn 1:** `count_words` × N (one per cluster fact, batched) + 4.5 min LLM thinking
- **Turns 2–N:** `create_fact` / `update_fact` / `merge_facts` (2–5 per turn, ~15s each)
- **Final turn:** JSON REPORT (no tool calls)

A 25-fact cluster needs ~12 turns. `max_turns=15` gives adequate headroom.
If `max_turns` is hit before the REPORT is received: a warning is logged, all tool-call
writes still took effect, but `ops` count in the stage summary shows 0 (report not parsed).

---

## 10. Code References

| File | Role |
|------|------|
| `src/agents/consolidation_agent.py` | All 3 stages, tool loop, cluster build, intent dispatch |
| `src/handlers/consolidation_handler.py` | Sends `CONSOLIDATE_FULL`; wires user/account |
| `src/handlers/worker_handler.py` | Dispatches `task_type=consolidation` |
| `src/infrastructure/agent_config.py` → `ConsolidationAgentConfig` | All tunable parameters |
| `src/adapters/gcp_task_queue.py` → `enqueue_consolidation_task` | Cloud Tasks dispatch with `dispatch_deadline` |
| `src/adapters/firestore_session_store.py` | Sliding window, overflow callback |
| `src/services/fact_write_service.py` | Fact persistence (SCD2, embeddings) |
| `src/adapters/bigquery_prompt_content_adapter.py` → `BigQueryPromptContentAdapter` | BigQuery LLM content capture |

---

## 11. Status

**Status:** ✅ Production Ready

**Last Updated:** 2026-03-06

**Recent changes:**
- 2026-03-06: `max_turns` raised from 10 → 15 (Stage 2 on large clusters)
- 2026-03-06: `dispatch_deadline=1800s` added to Cloud Tasks consolidation task
- 2026-03-06: Debug logger: response files now include turn number, response type, and embedded JSON block extraction
- 2026-03-05: Stage 2 (inline cluster review) added; `_TrackingFactManagement`; 3-stage pipeline
- 2026-03-05: `_handle_consolidate_full`, `_handle_consolidate_email`, `_handle_consolidate_cluster` intents
- 2026-02-25: `_run_consolidation_loop` extracted as shared loop for all stages
- 2026-02-25: `add_facts_batch` returns `(saved_count, skipped_count, saved_ids)`
