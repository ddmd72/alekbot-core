# Decision: the external service is the source of truth; Firestore holds a thin search index (formerly ADR-008)

**Status:** Accepted (2026-03-18)
**Applies to:** the Microsoft To Do integration; written as a pattern for later external-service integrations.
**Detail:** `docs/05_building_blocks/tasks_integration/README.md`, `docs/10_rfcs/TASKS_LOCAL_FIRST_RFC.md`.

## Context

Where does task data live? The previous Google Tasks integration kept task data client-side and searched by substring in
memory. That broke down as lists grew and gave no semantic understanding. Three options were weighed for the new
Microsoft To Do integration.

## Decision

**Microsoft To Do is the source of truth; Firestore holds only a search index.** An index entry stores:

- a stable short reference (md5 prefix of the provider's task id) and the provider's `task_id` / `list_id` pointers;
- the display fields needed to render a result: title, status, tags, importance;
- two embeddings: content (title, body, checklist items) and context (list name, tags, importance).

Bodies, recurrence, reminders, checklist items, linked resources and dates are never persisted. After a search or list
the agent fetches full task objects from the provider in a batch. The index stays fresh through the provider's change
notifications: created and updated events re-embed and upsert, deleted events remove the entry.

## Alternatives

- **A. Full Firestore mirror.** Rejected. Two databases that must agree: every missed webhook leaves a stale record with
  no correction path but a periodic full reindex, and users edit in the provider's own apps, so a mirror is
  structurally behind. Sync, conflict and freshness logic buy nothing when the provider is authoritative. Storage and
  read cost also scale with task count and field richness.
- **B. Client-side search only.** Rejected immediately once semantic search was required. Substring matching has poor
  recall for natural-language queries, and every search would fetch all tasks from every list.
- **C. External truth plus thin vector index.** Chosen.
- **Google Tasks as the provider.** Rejected for lacking importance, recurrence, reminders, checklist items, linked
  resources, and for token-expiry trouble in development. Its adapter stays as a frozen reference.

## Why C

- No double bookkeeping: full data lives in one place, so there is nothing to synchronise.
- Semantic search at the right cost: only vectors and a few display fields are stored; details are fetched on demand.
- Writes are always authoritative in the provider; the index is eventually consistent and never the source of
  mutation truth.
- Degraded mode is safe: if the provider is down, search still returns titles and references, and only the detail fetch
  fails, with an error the agent reports.
- Provider-agnostic: the provider port abstracts the external API and the index port abstracts the vector store, so a
  second provider needs a new adapter, not changes to indexing or agents.

## Costs accepted

- A task edited in the provider's app is findable by its new content only after the webhook fires and re-indexes
  (seconds in practice).
- The detail fetch costs one provider call per result (up to the result limit), softened by bounded concurrency in the
  adapter (5 parallel calls).
- A mutation resolves the short reference with one Firestore read, so it depends on index freshness: a task not yet
  indexed, or de-indexed by mistake, fails the mutation with a `ValueError`.

## Reusable pattern

Use it for any integration where the external service has a rich API and is where users work directly, semantic search
is needed but not offered natively, and a full mirror would bring unacceptable sync complexity or cost:

1. The external API is the source of truth for all full reads and writes.
2. Firestore keeps embeddings, ID pointers and display fields only.
3. Webhooks (or polling where none exist) keep the index eventually consistent.
4. Search: query the index, get IDs, fetch full objects from the external API. Mutation: write to the external API,
   then update the index.

## Revisit when

A provider offers no change notifications and polling cost rivals a mirror's, or detail-fetch latency becomes a visible
problem for result sets much larger than the current limit.

## Code

`src/ports/tasks_provider_port.py`, `src/ports/task_search_index.py`, `src/services/task_indexing_service.py`,
`src/adapters/microsoft/todo_adapter.py`, `src/adapters/firestore_task_search_index.py`.
