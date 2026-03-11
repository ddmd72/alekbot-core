# RFC: Cluster Review — Periodic Knowledge Base Maintenance

**Status:** Approved — Ready for Implementation
**Date:** 2026-03-05
**Owner:** AI Engineering

**Related:** ConsolidationAgent, WorkerHandler, Cloud Scheduler
**POC:** `scripts/consolidation/test_anchor_cluster_dryrun.py`

---

## 1. Problem Statement

ConsolidationAgent incrementally extracts facts from conversations. Each consolidation
run is aware only of its current batch — it cannot see the full picture of what has
accumulated in the knowledge base over time. As a result:

- **Compound facts accumulate** — a fact created months ago may now cover 3-4 distinct
  concepts that each warrant their own entry.
- **Duplicates emerge across batches** — two slightly different facts about the same
  topic are created in separate consolidation runs and never merged.
- **Inconsistencies persist** — an older fact is never superseded when a newer,
  contradicting fact is created in a different batch.

The incremental `SIZE_TRIGGERS_REVIEW` gate in `COGNITIVE_PROCESS_CONSOLIDATION.groovy`
partially addresses atomicity — but only when a compound fact is touched by an incoming
candidate. Facts that are never touched again are never reviewed.

---

## 2. Proposed Solution

A separate background maintenance job — **Cluster Review** — that periodically takes
a batch of recently modified facts, builds a similarity cluster around each one, and
runs `ConsolidationAgent` against the pre-fetched cluster. The agent analyses the
cluster holistically: decomposing compound facts, merging duplicates, superseding stale
entries.

Key design decisions:

1. **Decoupled from consolidation** — Cluster Review is maintenance, not extraction.
   Running it inline would increase consolidation latency by 7–22 minutes per batch.
2. **Event-driven anchor selection** — anchors are facts that were recently created or
   updated by consolidation, not a static heuristic (word count). This gives organic
   variety and focuses effort on hot spots.
3. **Configurable cadence** — default schedule set at system level; can be overridden
   per account via user settings.
4. **Same agent, different prompt context** — `ConsolidationAgent` is reused as-is.
   The system alert changes the task framing: "review this pre-fetched cluster" instead
   of "process these messages."

---

## 3. Architecture

### 3.1 Flow

```
ConsolidationHandler
  └─ after consolidation run
       └─ writes ClusterReviewBatch to Firestore
            {account_id, fact_ids: [touched_ids], status: pending, created_at}

Cloud Scheduler (per account cadence)
  └─ POST /worker  {task_type: "cluster_review", account_id, user_id}

WorkerHandler.handle_cluster_review()
  └─ ClusterReviewService.run_next_batch(account_id, user_id)
       ├─ reads oldest pending ClusterReviewBatch
       ├─ marks batch status: processing
       ├─ for each fact_id in batch (round by round):
       │    ├─ fetch anchor fact from Firestore by fact_id
       │    │    (skip if fact no longer active — may have been superseded)
       │    ├─ prefetch_cluster(fact_id, cluster_size=30) via search_existing_facts
       │    └─ ConsolidationAgent.execute(cluster_message)
       └─ marks batch status: done
```

### 3.2 Firestore Collection: `cluster_review_queue`

```
{account_id}_cluster_review_queue/{batch_id}
  account_id:   str
  fact_ids:     List[str]     # fact IDs touched in consolidation run
  status:       pending | processing | done | failed
  created_at:   datetime
  started_at:   datetime | null
  completed_at: datetime | null
  rounds_total: int           # len(fact_ids) processed
  operations:   {CREATE: int, UPDATE: int, MERGE: int, DISCARD: int}
```

### 3.3 ClusterReviewService

New service: `src/services/cluster_review_service.py`

Encapsulates the round loop from the POC (`test_anchor_cluster_dryrun.py`):

```python
class ClusterReviewService:
    async def run_next_batch(self, account_id: str, user_id: str) -> ClusterReviewResult
    async def _run_round(self, anchor_fact_id: str, ...) -> RoundResult
    async def _prefetch_cluster(self, anchor, cluster_size: int) -> List[Dict]
    async def _build_message(self, anchor, cluster) -> AgentMessage
```

Port: not needed — single implementation, no system boundary.

### 3.4 prefetch_cluster — critical implementation details

```python
async def _prefetch_cluster(self, anchor: Dict, cluster_size: int = 30) -> List[Dict]:
    results = await fact_management.search_existing_facts(
        keywords=anchor["tags"][:10],
        primary_query=anchor["content"][:300],
        alternative_query=f"{anchor['domain']} {' '.join(anchor['tags'][:5])}",
        limit=cluster_size + 1,  # +1 to account for self-inclusion
    )
    return [r for r in results if r.get("fact_id") != anchor["fact_id"]][:cluster_size]
```

**No similarity threshold.** Do not add `min_similarity` filtering. RRF scores are low
by nature; any absolute threshold eliminates entire clusters. Use top-N by rank only.

**Anchor treated as peer.** The anchor is placed first in the fact list but is not
labelled as special — the agent sees all facts as equal peers for review.

**Fact format in message:**
```python
{
    "fact_id": fact.get("fact_id"),
    "content": fact.get("content"),
    "similarity": round(fact.get("similarity") or 0, 3),
    "source": fact.get("source"),
}
```

### 3.5 ConsolidationHandler changes

After each successful consolidation run, collect touched `fact_ids` from
`add_facts_batch` return value and write a `ClusterReviewBatch`:

```python
saved_count, skipped_count, saved_ids = await fact_management.add_facts_batch(...)
if saved_ids:
    await cluster_review_queue.enqueue(account_id, fact_ids=saved_ids)
```

`ConsolidationHandler` already receives `fact_management` — no new dependencies.

### 3.6 WorkerHandler new task type

```python
# task_type: "cluster_review"
elif task_type == "cluster_review":
    await self._handle_cluster_review(payload)
```

Mirrors `email_indexing` dispatch pattern exactly.

### 3.7 Scheduler configuration

Default: Cloud Scheduler fires `cluster_review` once per day per active account.

Per-account override (stored in account settings):

```
cluster_review_enabled:     bool    (default: true)
cluster_review_schedule:    str     (cron, default: "0 3 * * *")  # 3am daily
cluster_review_batch_limit: int     (default: 10 fact_ids per run)
```

Scheduler reads these settings before enqueueing the Cloud Task. If
`cluster_review_enabled=false` → skip.

---

## 4. Agent Message Format

`ClusterReviewService._build_message()` constructs the message as follows:

- `system_alert` field in payload contains the text below
- All facts (anchor + cluster) are listed as a numbered JSON array
- Anchor is fact #1; no special labelling — agent treats all facts as peers

System alert (final validated version — do not modify without re-running POC):

```
SYSTEM MAINTENANCE — FACT CLUSTER REVIEW

The system has flagged the following cluster of facts for quality review.
This cluster may contain: repeated or overlapping facts (these must be merged),
facts that span multiple distinct concepts (these must be decomposed, with the
original superseded), mutually inconsistent facts, or facts that have grown
too large to serve as atomic memory units.

Review and refactor this cluster according to your consolidation rules.
When creating new facts, ensure they do not duplicate information already
present in other facts in this cluster.

Hard limit: no fact may exceed 40 words. Every fact in this cluster that
exceeds 40 words must be either rephrased to fit within 40 words, or
decomposed into atomic facts each under 40 words. Co-location is not a
valid justification for exceeding this limit.

Important: do not lose specific numeric values, dates, or amounts —
they are critical for long-term memory accuracy.
```

**System alert design principle — WHAT not HOW.** The system alert describes the task
context only. It never instructs which tools to use or how to structure operations.
The HOW is fully handled by `COGNITIVE_PROCESS_CONSOLIDATION.groovy`. Adding tool
instructions to the system alert causes the agent to ignore the cognitive process
and produce degraded output.

The round-by-round selection mirrors the POC: re-fetch candidate list each round so
that facts superseded in round N-1 no longer appear in round N.

---

## 5. POC Validation

Full POC history (`scripts/consolidation/test_anchor_cluster_dryrun.py`):

| Run | Model | Config | Result |
|-----|-------|--------|--------|
| 20260305_132459 | Gemini Pro | bio=true, threshold=0.55 | cluster_size=0 for all anchors (threshold killed clusters) |
| 20260305_134224 | Claude Sonnet | bio=true, threshold=0.55 | cluster_size=0 for all anchors |
| 20260305_140631 | Claude Sonnet | bio=false, no threshold | 0 operations on language anchor (bio required) |
| 20260305_142744 | Claude Sonnet | bio=true, no threshold | correct ops, old system alert (HOW instructions) |
| 20260305_150633 | Claude Sonnet | bio=true, no threshold, per-round refetch | 18 CREATE, 3 UPDATE, 0 MERGE — decomposition works |
| 20260305_151636 | Claude Sonnet | bio=true, no threshold, per-round refetch | 17–24 cluster size, MERGE + cross-cluster decomposition |
| 20260305_160515 | Claude Sonnet | WHAT-only alert, no hard cap | anchor A left intact (co-location justification) |
| 20260305_163254 | Claude Sonnet | + "must be merged/decomposed" | anchor still left intact — co-location argument persists |
| 20260305_164747 | Claude Sonnet | + hard 40-word cap | anchor A finally decomposed: 8 CREATEs + SUPERSEDED ✓ |

**Finding 1: Biographical context is required.**
Without bio context (`include_biographical=False`), the agent fails on behavioral facts:
language preference anchor produced 0 operations, incident logs discarded. Bio context
must remain enabled — same as regular consolidation.

**Finding 2: No similarity threshold on cluster selection.**
`min_similarity=0.55` eliminated all clusters (all anchors returned 0 facts). RRF scores
are low by nature. `prefetch_cluster` must use no threshold — top N by RRF rank only,
excluding the anchor itself.

**Finding 3: System alert must be WHAT only.**
Early versions included HOW instructions (MERGE/CREATE/UPDATE guidance). This caused the
agent to bypass the cognitive process and produce mechanical output. The system alert
must describe only why the cluster was flagged. The cognitive process handles the rest.

**Finding 4: Hard 40-word cap is required.**
Without an explicit hard limit, the agent invokes co-location to justify leaving large
compound facts intact (e.g. a 200-word income fact across 6 time periods). With
`"Hard limit: no fact may exceed 40 words... Co-location is not a valid justification"`,
the agent correctly decomposed the same fact into 8 atomic entries.

**Finding 5: Cross-cluster operations emerge naturally.**
The agent performs MERGE, cross-fact SUPERSEDE, and decomposition on facts outside the
anchor — not just the anchor itself. No extra prompt instructions needed. Round 2 in the
final run touched 15+ facts across citizenship, persona, addresses, financial support,
solo-mode context.

**Finding 6: Cross-round duplication is a dry-run artifact.**
In dry-run mode, writes are intercepted and not persisted. Round N+1 does not see facts
created in Round N, so near-duplicate facts can be created across rounds. In production
this is not an issue: Round N's CREATEs are written to Firestore before Round N+1 runs,
and `prefetch_cluster` will find them.

**Cost estimate (Claude Sonnet with prompt caching):**
- ~80k token static prompt cached → ~$0.88 per 3-anchor run
- Production: 10 fact_ids/batch × ~90s/round → ~15 min total, ~$3/batch

---

## 6. Implementation Plan

Touch files in this order. Do not skip steps.

### Phase 1 — Core infrastructure

1. **`src/domain/entities.py`** — add `ClusterReviewBatch` entity:
   ```python
   class ClusterReviewBatch(BaseModel):
       id: str
       account_id: str
       fact_ids: List[str]
       status: str  # pending | processing | done | failed
       created_at: datetime
       started_at: Optional[datetime] = None
       completed_at: Optional[datetime] = None
       rounds_total: int = 0
       operations: Dict[str, int] = field(default_factory=dict)
   ```

2. **`src/ports/cluster_review_port.py`** — `ClusterReviewQueuePort`:
   ```python
   class ClusterReviewQueuePort(ABC):
       async def enqueue(self, account_id: str, fact_ids: List[str]) -> str: ...
       async def dequeue_oldest_pending(self, account_id: str) -> Optional[ClusterReviewBatch]: ...
       async def mark_processing(self, batch_id: str) -> None: ...
       async def mark_done(self, batch_id: str, rounds_total: int, operations: Dict) -> None: ...
       async def mark_failed(self, batch_id: str, error: str) -> None: ...
   ```

3. **`src/adapters/firestore_cluster_review_repo.py`** — Firestore implementation.
   Collection name: `{account_id}_cluster_review_queue` (same env-prefix pattern as
   other collections). Use `database="us-production"` (or `FIRESTORE_DATABASE` env var).

4. **Firestore index** — composite index on collection:
   `(account_id ASC, status ASC, created_at ASC)` — required for `dequeue_oldest_pending`.

### Phase 2 — Service + handler wiring

5. **`src/services/cluster_review_service.py`** — implement round loop. Copy
   `prefetch_cluster` and `build_user_message` verbatim from POC. Use
   `include_biographical=True` (hardcoded — never False, see Finding 1).

6. **`src/composition/service_container.py`** — add `cluster_review_queue`:
   instantiate `FirestoreClusterReviewRepository`, expose as `self.cluster_review_queue`.

7. **`src/handlers/worker_handler.py`** — add `task_type: "cluster_review"` dispatch.
   Mirror the `email_indexing` dispatch block exactly (payload shape:
   `{task_type, account_id, user_id}`).

8. **`src/handlers/consolidation_handler.py`** — enqueue batch after successful run:
   ```python
   saved_count, skipped_count, saved_ids = await fact_management.add_facts_batch(...)
   if saved_ids:
       await cluster_review_queue.enqueue(account_id, fact_ids=saved_ids)
   ```

### Phase 3 — Scheduler

9. Cloud Scheduler job: fires `cluster_review` task daily per active account.
   Payload: `{task_type: "cluster_review", account_id, user_id}`.

10. Account settings keys: `cluster_review_enabled` (bool, default true),
    `cluster_review_schedule` (cron, default `"0 3 * * *"`),
    `cluster_review_batch_limit` (int, default 10).

### Phase 4 — Observability

11. **`UserNotificationService`** — optional summary notification after batch completes
    (operations count, rounds processed).

12. **Cabinet UI** — cluster review history panel (status, operations count, last run
    timestamp per account).

---

## 7. Out of Scope

- **Separate `ClusterReviewAgent`** — `ConsolidationAgent` is reused as-is. A new
  agent class would duplicate the tool loop for no benefit.
- **`cluster_reviewed_at` on `FactEntity`** — not needed since anchor selection is
  event-driven (touched_ids from consolidation), not LRU over all facts.
- **Real-time / inline execution** — originally rejected due to latency impact (7–22 min
  per batch). See Addendum §8 for the revised decision.
- **`min_similarity` on `prefetch_cluster`** — explicitly rejected. See Finding 2.

---

## 8. Addendum — 2026-03-06: Inline Hot Pass + Intent API

### 8.1 Revised decision on inline execution

Section 7 rejected inline execution due to latency. After POC validation
(`test_decomposition_dryrun.py --message`), inline Stage 2 was introduced as an
**optional hot pass** that runs immediately after Stage 1 in the normal overflow flow.

Rationale for the revision:
- Consolidation already runs as a Cloud Tasks worker (non-blocking for the user).
  The 7–22 min latency is invisible to the user — it only affects worker CPU time.
- Inline Stage 2 catches co-location violations and cross-batch duplicates in the
  same batch that created them, before they accumulate in the knowledge base.
- The scheduled `ClusterReviewService` (§3.3) remains the mechanism for periodic
  deep review across all accumulated facts. The two are complementary:
  - **Inline (hot pass):** catches issues in the current batch immediately.
  - **Scheduled (cold pass):** catches issues that accumulate across batches over time.

**Toggle:** `ConsolidationAgentConfig.inline_cluster_review: bool = True` in
`src/infrastructure/agent_config.py`. Set to `False` to revert to RFC §7 behaviour
(scheduled review only).

**Confirmed latency:** Stage 1 ~8 min + Stage 2 ~6 min = ~14 min per batch. Acceptable
for a Cloud Tasks worker. Revisit if worker timeout becomes an issue.

### 8.2 ConsolidationAgent intent API

`ConsolidationAgent` now exposes 4 internal intents (all `internal=True` — not visible
to LLM, but callable by other agents and scheduler via `AgentCoordinator.delegate()`):

| Intent | Payload | Description |
|--------|---------|-------------|
| `consolidate` | `{messages?: List[Dict]}` | Stage 1 only. If `messages` absent → auto-fetch from session store. If `messages` present → formatted as user turn (system prompt has empty `conversation_history`). |
| `consolidate_cluster` | `{cluster: List[Dict]}` or `{limit: int}` | Stage 2 only. Pre-built cluster or auto top-K by word count from Firestore. |
| `consolidate_email` | `{number_of_batches: int, batch_size: int}` | Stage 3 only. Batch params; agent fetches emails internally. |
| `consolidate_full` | `{}` | Full pipeline (Stage 1 → 2 → 3). Auto-fetches everything from Firestore. Triggered by overflow or `$consolidate` command. |

`biographical_context` removed from all payload contracts — agent always loads from
cache via `self._repo.get_biographical_context_cached()`.

### 8.3 _TrackingFactManagement

Module-level pass-through wrapper in `src/agents/consolidation_agent.py`. Wraps
`self._fact_management` during Stage 1 to record `(fact_id, content)` for every
CREATE / UPDATE / MERGE. Restored after Stage 1; `tracker.changed` seeds Stage 2
`_build_review_cluster()`. Does not affect production writes.

### 8.4 Implementation status (2026-03-06)

- [x] `_TrackingFactManagement` — implemented in `consolidation_agent.py`
- [x] `_run_consolidation_loop` — LLM loop extracted, used by all 3 stages
- [x] `_build_review_cluster` + `_build_cluster_message` — implemented
- [x] `inline_cluster_review` toggle — `ConsolidationAgentConfig`
- [x] Intent constants (`Intent.CONSOLIDATE*`) — declared in `agent_manifest.py`
- [x] `CONSOLIDATION_AGENT` `AgentDescriptor` — declared in `agent_manifest.py` (internal=True, not in ALL_DESCRIPTORS)
- [x] `can_handle` — accepts all 4 intent task strings
- [x] `execute` dispatcher — routes by `payload["task"]`
- [x] `_handle_consolidate` — Stage 1 + optional inline Stage 2
- [x] `_handle_consolidate_cluster` — Stage 2 cluster review, explicit cluster list + auto-fetch by `{"limit": int}`
- [x] `consolidation_handler.py` — uses `Intent.CONSOLIDATE_FULL`; email triage removed (moved to agent)
- [x] `consolidate_cluster` with `{"limit": int}` auto-fetch — `FactRepository.get_longest_facts(account_id, limit)` added to port + `FirestoreFactRepository` impl
- [x] `consolidate_email {number_of_batches, batch_size}` — `_handle_consolidate_email` implemented; email triage removed from `consolidation_handler.py`
- [x] `consolidate_full {}` — `_handle_consolidate_full` implemented: Stage 1 → Stage 2 (inline) → Stage 3 (email)
- [x] `UserAgentFactory` — passes `indexed_email_repo` to `ConsolidationAgent`
- [x] `max_turns` raised from 10 → 15 — Stage 2 on 25-fact clusters needs ~12 turns (confirmed from production logs 2026-03-06)
- [x] `dispatch_deadline=1800s` on Cloud Tasks consolidation task — matches increased `timeout_ms=900s`
- [ ] `UserAgentFactory` wiring for ConsolidationAgent via intent routing — `CONSOLIDATION_AGENT` descriptor still not in `_register_agents` (currently only reachable by recipient ID)
- [ ] Phase 1 (§6) `ClusterReviewBatch` entity + `ClusterReviewQueuePort` + Firestore adapter — not implemented
- [ ] Phase 2 (§6) `ClusterReviewService` + `WorkerHandler` `cluster_review` task type — not implemented
- [ ] Phase 3 (§6) Cloud Scheduler job for daily scheduled cluster review — not implemented
