# ADR-008: External Service as Source of Truth with Thin Local Search Index

**Status:** Accepted
**Date:** 2026-03-18
**Author:** Solo developer

---

## Context

When implementing Microsoft To Do integration, a fundamental architectural question arose:
where does task data live?

The previous Google Tasks integration stored task data client-side and performed substring
search entirely in memory. That approach broke down as the task list grew and provided no
semantic understanding. The question was whether the new integration should mirror full task
data in Firestore, rely entirely on Graph API for all reads, or find a middle ground.

Three options were evaluated:

**Option A — Full Firestore mirror.** Replicate every task field into Firestore on every
change. Firestore becomes the read path; MS To Do is the write source.

**Option B — Client-side search only.** No Firestore storage. Every `search_tasks` call
fetches all tasks from Graph API and filters in Python.

**Option C — External service as source of truth, thin local vector index.** MS To Do holds
full task data. Firestore stores only vector embeddings and task ID pointers. Agent fetches
full task details from Graph API on demand.

Option C was chosen.

---

## Decision

**Microsoft To Do is the source of truth. Firestore holds a thin search index only.**

The Firestore search index (`{env}_task_search_index`) stores:

- `short_id` — stable 8-char reference (md5 prefix of the MS task ID)
- `task_id`, `list_id` — pointers back to Graph API
- `title`, `status`, `tags`, `importance` — display fields for search result rendering
- `content_vector` — embedding of title + body + checklist items
- `context_vector` — embedding of list name + tags + importance

Full task data (body, recurrence, reminders, checklist items, linked resources, dates) is
never persisted in Firestore. When the agent needs this data — after a `search_tasks` call
or a `list_tasks` call — it fetches full `Task` objects from Graph API via `batch_get_tasks`.

The index is kept fresh via Graph API webhook subscriptions (change notifications):
`created` and `updated` events trigger re-embedding and upsert; `deleted` events remove the
entry from the index.

---

## Rationale

### Why not Option A (full mirror)?

A full Firestore mirror introduces a two-database consistency problem with no equivalent
payoff. Every change in MS To Do must be reflected in Firestore; every webhook failure
creates a stale record. When MS To Do is the system users edit directly (via the app, web,
or other integrations), any local mirror is structurally behind. The added complexity of
sync logic, conflict resolution, and freshness validation is not justified when the
authoritative source is always Graph API.

Storage cost is also a real concern for a solo-developer budget. Firestore charges per
stored document and per read. Mirroring 500+ tasks with full bodies, checklist items, and
linked resources would substantially increase both storage and read costs compared to a thin
index.

### Why not Option B (client-side search only)?

Fetching all tasks on every search request has O(n) Graph API calls for a user with tasks
spread across many lists. More importantly, substring matching produces poor recall for
semantic queries. "Find tasks about my Prague trip" requires understanding meaning, not just
string matching. The Firestore vector index provides this semantic layer at low latency.

### Why Option C?

- **No double bookkeeping.** MS To Do is the real database; Firestore is a search layer.
  There is no sync problem because there is nothing to sync — full data is never in two places.
- **Semantic search at the right cost.** Firestore holds only what is needed for search:
  vectors and a few display fields. Full task details are fetched on demand, only when needed.
- **Authoritative writes are always in MS To Do.** Changes made in the MS To Do app are
  immediately authoritative. The webhook keeps the search index eventually consistent.
- **Offline / degraded mode is safe.** If Graph API is unavailable, semantic search still
  works. Users get search results showing titles and refs. Full task details fail gracefully
  with an error from the `batch_get_tasks` call. The search index itself is never the source
  of mutation truth.
- **Provider-agnostic pattern.** The pattern generalizes cleanly. `TasksProviderPort` abstracts
  the Graph API; `TaskSearchIndex` abstracts the vector store. Adding a second task provider
  (Google Tasks, Apple Reminders) requires a new adapter, not a change to the indexing or
  agent layer.

---

## Consequences

### Positive

- No double bookkeeping or sync complexity. One source of truth per data type.
- Firestore storage stays cheap: only vectors and minimal metadata, no full task objects.
- Changes made in MS To Do app are always authoritative. No stale data risk in the write path.
- Semantic search quality is equivalent to the memory/email search pipelines (same RRF pattern).
- The pattern is reusable across future external service integrations.

### Negative

- Semantic search requires the Firestore index to be reasonably fresh. Webhook delivery
  latency (typically seconds) means a task edited in the MS To Do app is not immediately
  findable by new content — until the webhook fires and re-indexes. This is acceptable for
  the use case.
- `batch_get_tasks` is O(k) Graph API calls where k is the number of search results. For
  a limit of 10 results, this means up to 10 individual HTTP calls. This is partially
  offset by Graph API's own response caching and the semaphore-bounded concurrency (max 5
  parallel calls) in `MicrosoftToDoAdapter`. Latency is acceptable in practice.
- If Graph API is unavailable at search time, the full task details fetch fails even though
  the search step succeeded. The agent receives an error from `batch_get_tasks` and reports
  it to the user. This is a degraded but non-corrupt state.
- The `short_id` indirection (md5 prefix) adds a lookup step for mutations. `resolve_short_id`
  queries Firestore before every `update_task` and `delete_task`. This is one Firestore read,
  not a performance concern, but it is an extra dependency on index freshness: if a task is
  not yet indexed or was deindexed erroneously, the mutation fails with `ValueError`.

---

## Rejected Alternatives

### Full Firestore Mirror

- Sync complexity and two-database consistency are the disqualifying factors.
- Webhook failures create stale data with no automatic correction path other than periodic
  full reindex.
- Storage and read costs scale with task count and field richness (body, checklist items,
  linked resources).
- Not implemented. Google Tasks adapter (retained as frozen reference) does not mirror data
  either — it performs client-side filtering on live API responses.

### Client-Side Search Only

- No semantic understanding. Substring matching is insufficient for natural language queries
  like "tasks related to my house renovation project".
- O(n) Graph API calls on every search with multiple lists.
- Rejected immediately once the semantic search requirement was confirmed.

### Google Tasks (Alternative Provider)

Evaluated as the primary provider before MS To Do. Disqualifying factors:

- No `importance` field.
- No recurrence support in the API.
- No reminders.
- No checklist items (sub-tasks).
- No linked resources.
- OAuth token expiry issues in the development environment.

Google Tasks adapter is retained in the codebase as a frozen reference implementation but
is not the active provider.

---

## Reusable Pattern

This pattern is not specific to MS To Do. It applies to any future external service
integration where:

1. The external service has a rich API and is the system users interact with directly.
2. Semantic search is required but the external service does not support it natively.
3. Full data mirroring would introduce unacceptable sync complexity or cost.

**Pattern template:**

- Use the external API as the source of truth for all full data reads and writes.
- Build a thin vector index in Firestore: store only embeddings + ID pointers + display fields.
- Use webhooks (or polling if webhooks are unavailable) to keep the index eventually consistent.
- On agent search: query Firestore index → get IDs → fetch full objects from external API.
- On agent mutation: write to external API → update Firestore index.

Candidate future integrations where this pattern applies: Google Calendar, Notion pages,
Linear issues, GitHub issues.

---

## References

- RFC: `docs/10_rfcs/TASKS_LOCAL_FIRST_RFC.md`
- Building block: `docs/05_building_blocks/tasks_integration/README.md`
- `src/ports/tasks_provider_port.py` — CRUD port (external API abstraction)
- `src/ports/task_search_index.py` — vector index port (Firestore abstraction)
- `src/services/task_indexing_service.py` — embed-to-index pipeline
- `src/adapters/microsoft_todo_adapter.py` — Graph API implementation
- `src/adapters/firestore_task_search_index.py` — Firestore vector index implementation
