# Microsoft To Do Integration (Building Block)

## HowTo: Using This Document

### Purpose

Describes the Microsoft To Do integration: OAuth authorization, local-first architecture,
task CRUD via Graph API, semantic search via Firestore vector index, webhook-driven freshness,
and subscription lifecycle management.

### When to Read

- **For AI Agents:** Before modifying `TasksAgent`, `TaskIndexingService`, or `TaskSetupService`.
- **For Developers:** When troubleshooting OAuth flows, webhook delivery, subscription expiry,
  or search quality issues.

### When to Update

This document MUST be updated when:

- [ ] The short_id algorithm or TaskSearchEntry schema changes.
- [ ] New recurrence patterns are added or removed.
- [ ] `TasksAgent` intents, tools, or max turns change.
- [ ] The OAuth flow or Azure tenant configuration changes.
- [ ] Subscription renewal thresholds or expiry limits change.
- [ ] Cabinet API endpoints for MS To Do change.

### Cross-References

- **RFC:** [../../10_rfcs/TASKS_LOCAL_FIRST_RFC.md](../../10_rfcs/TASKS_LOCAL_FIRST_RFC.md)
- **ADR:** [../../09_decisions/adr-008-local-first-external-provider/README.md](../../09_decisions/adr-008-local-first-external-provider/README.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **OAuth Web API:** [../oauth_web_api/README.md](../oauth_web_api/README.md)

---

## 1. Overview

The **Microsoft To Do integration** adds personal task management to Alek-Core. Users interact
via natural language in Slack or Telegram; `TasksAgent` translates intent into Graph API calls.

**Architecture principle: local-first.** Microsoft To Do is the source of truth. Firestore
stores only a thin search index — embedding vectors and task ID pointers. No full task data
is mirrored in Firestore. When the agent needs task details, it fetches them from Graph API
on demand. This avoids double bookkeeping and keeps the two systems from diverging.

**Semantic search without full sync.** Firestore holds vector embeddings of task content
and context. When the user asks "find my Prague tasks", the agent queries the Firestore index
to retrieve matching task IDs, then fetches full task objects from Graph API. The index stays
fresh via Graph API webhooks (change notifications) that trigger re-indexing on create/update
and de-indexing on delete.

---

## 2. Architecture

### 2.1 Hexagonal Position

```
Driving side (user initiates):
  Cabinet UI → GET /auth/connect-microsoft-todo → OAuth consent (Azure consumers tenant)
             → GET /auth/connect-microsoft-todo/callback → OAuthCredentials (Firestore)
             → enqueue setup_microsoft_todo Cloud Task

  Cabinet UI → POST /api/tasks/microsoft/reindex → TaskSetupService.reindex_all()
                                                  → enqueue reindex_task_list per list

  Slack/Telegram → User message → RouterAgent → TasksAgent (intent: manage_user_tasks)

Driven side (background processing):
  Cloud Tasks POST /worker (task_type=setup_microsoft_todo)
    → WorkerHandler → TaskSetupService.setup(user_id)
        → TaskLifecyclePort.ensure_primary_list()
        → TaskConfigPort.set_primary_list_id_if_absent()
        → TaskSetupService.ensure_subscriptions()
            → TaskLifecyclePort.register_subscription() (per list)
            → enqueue reindex_task_list per new subscription

  Cloud Tasks POST /worker (task_type=reindex_task_list)
    → WorkerHandler → TaskIndexingService.reindex_list(user_id, list_id)
        → TasksProviderPort.list_tasks() (including completed)
        → EmbeddingService (2 vectors per task, concurrency=5)
        → TaskSearchIndex.upsert()

  Cloud Tasks POST /worker (task_type=renew_task_subscriptions)
    → WorkerHandler → TaskSetupService.renew_expiring_subscriptions(user_id)

  Cloud Tasks POST /worker (task_type=renew_all_task_subscriptions)
    → WorkerHandler → fan-out: enqueue renew_task_subscriptions for all MS To Do users

Webhook side (MS Graph push):
  POST /webhook/microsoft-tasks/{user_id}
    → validate clientState
    → created/updated → TaskIndexingService.index_task_by_ref(user_id, list_id, task_id)
    → deleted         → TaskIndexingService.deindex_task(user_id, task_id)
    → self-healing    → TaskSetupService.handle_subscription_renewal(user_id, sub_id)
    → return 202 Accepted
```

### 2.2 Ports Introduced

| Port | Adapter | Purpose |
|---|---|---|
| `TasksProviderPort` | `MicrosoftToDoAdapter` | Task CRUD via Graph API |
| `TaskLifecyclePort` | `MicrosoftToDoAdapter` | Subscription management + primary list setup |
| `TaskSearchIndex` | `FirestoreTaskSearchIndex` | Vector search index in Firestore |
| `TaskConfigPort` | `FirestoreTaskConfigRepository` | Per-user config (primary list ID, subscriptions) |
| `OAuthCredentialsPort` | `FirestoreOAuthCredentialsAdapter` | Token storage and refresh |

`MicrosoftToDoAdapter` implements both `TasksProviderPort` and `TaskLifecyclePort`. This is
intentional — both concern the same Graph API client, and splitting them into two adapters
would duplicate auth/HTTP infrastructure without adding value.

---

## 3. Data Model

### 3.1 Task (MS To Do — not stored in Firestore)

`Task` in `src/domain/task.py` is the full MS To Do task object returned by `TasksProviderPort`
methods. It is never persisted in Firestore — it lives in MS To Do.

Key fields:

| Field | Type | Notes |
|---|---|---|
| `task_id` | `str` | MS-assigned ID (~180 chars). Never shown to the LLM directly. |
| `list_id` | `str` | MS-assigned list ID. |
| `title` | `str` | Task title. |
| `body` | `Optional[str]` | Rich text notes (plain text extracted). |
| `due_datetime` | `Optional[datetime]` | UTC. |
| `importance` | `TaskImportance` | low / normal / high |
| `status` | `TaskStatus` | notStarted / inProgress / waitingOnOthers / deferred / completed |
| `tags` | `List[str]` | MS To Do categories — primary classification mechanism. |
| `recurrence` | `Optional[TaskRecurrence]` | Pattern + range. |
| `checklist_items` | `List[ChecklistItem]` | Sub-tasks. |
| `attachments` | `List[TaskAttachment]` | Attachment metadata (see Known Limitations). |
| `linked_resources` | `List[LinkedResource]` | External links. |

### 3.2 TaskSearchEntry (Firestore search index)

Stored in `{env}_task_search_index`. Document ID: `{user_id}_{task_id}`.

| Field | Notes |
|---|---|
| `task_id` | Full MS task ID — used to fetch full task from Graph API after search. |
| `list_id` | Full MS list ID — required by `batch_get_tasks`. |
| `short_id` | `md5(task_id)[:8]` — stable 8-char reference shown to the LLM instead of full 180-char IDs. |
| `title` | Task title — stored for display in search results without a Graph round-trip. |
| `status` | Current status — used for `show_completed` filter in Firestore query. |
| `tags` | MS To Do categories. |
| `importance` | Stored for display. |
| `content_vector` | Embedding of `title + body + checklist items`. |
| `context_vector` | Embedding of `list_name + tags + importance`. |
| `indexed_at` | Timestamp of last indexing. |

### 3.3 TaskUserConfig (Firestore — infrastructure config)

Stored in `{env}_task_config/{user_id}`.

| Field | Notes |
|---|---|
| `primary_list_id` | ID of the "Alek Bot Tasks" list. Set once; persisted atomically by `set_primary_list_id_if_absent`. |
| `subscriptions` | `List[TaskSubscriptionConfig]` — active Graph webhooks (sub_id, list_id, expires_at). |

### 3.4 short_id System

MS To Do task IDs are opaque strings of ~180 characters. Passing them to the LLM wastes
tokens and causes formatting issues. The integration uses a stable 8-char `short_id`:

```
short_id = md5(task_id).hexdigest()[:8]
```

`TasksAgent` returns `short_id` as the `ref` field in all tool responses. The LLM uses
`task_ref` (short_id) in `update_task` and `delete_task` calls. `TaskIndexingService.resolve_short_id()`
translates `short_id` back to `(list_id, task_id)` before calling Graph API.

---

## 4. Ports

### 4.1 TasksProviderPort (`src/ports/tasks_provider_port.py`)

CRUD interface for task management. All methods receive `user_id`; adapters resolve OAuth
credentials internally via `OAuthCredentialsPort`.

| Method | Description |
|---|---|
| `list_task_lists(user_id)` | Return all task lists. |
| `list_tasks(user_id, list_id, show_completed)` | List tasks. `list_id=None` uses primary list. |
| `get_task(user_id, list_id, task_id)` | Fetch a single task. |
| `batch_get_tasks(user_id, task_refs)` | Fetch multiple tasks as `(list_id, task_id)` tuples. Used after search index lookup. |
| `create_task(user_id, task)` | Create a task. Returns the created `Task` with MS-assigned ID. |
| `update_task(user_id, list_id, task_id, updates)` | PATCH semantics — only set fields sent. |
| `delete_task(user_id, list_id, task_id)` | Permanently delete a task. |

Design note: `search_tasks` was intentionally removed from this port. Semantic search is
handled by `TaskSearchIndex` (Firestore vector queries), not by the provider. This keeps
the port focused on CRUD and avoids coupling the port interface to MS To Do's client-side
filtering capabilities.

### 4.2 TaskSearchIndex (`src/ports/task_search_index.py`)

Vector search interface backed by Firestore.

| Method | Description |
|---|---|
| `upsert(entry)` | Insert or replace index entry. |
| `delete(user_id, task_id)` | Remove from index. |
| `delete_by_list(user_id, list_id)` | Remove all tasks in a list (used on list deletion). |
| `find_nearest(user_id, vectors, limit, show_completed, list_id)` | Multi-vector RRF search. Returns `TaskSearchEntry` list. |
| `get_by_short_id(user_id, short_id)` | Look up a single entry by stable short_id. Returns `None` if not found. |
| `delete_all_for_user(user_id)` | Remove all entries for a user (called on disconnect). |

### 4.3 TaskConfigPort (`src/ports/task_config_port.py`)

Per-user integration config (primary list ID, active subscriptions).

| Method | Description |
|---|---|
| `get_config(user_id)` | Load config. Returns empty `TaskUserConfig` if not found. |
| `save_config(user_id, config)` | Overwrite config. |
| `set_primary_list_id_if_absent(user_id, list_id)` | Atomic create-if-not-exists via Firestore transaction. Returns existing value if already set, or writes and returns the new value. Safe under concurrent calls. |

### 4.4 TaskLifecyclePort (`src/ports/task_lifecycle_port.py`)

Graph API lifecycle operations for subscription management and initial setup. Separated from
`TasksProviderPort` to keep task CRUD distinct from subscription concerns. Config persistence
is the caller's responsibility — these methods only interact with Graph API.

| Method | Description |
|---|---|
| `ensure_primary_list(user_id)` | Find or create "Alek Bot Tasks" list. Returns `list_id`. Does not persist. |
| `register_subscription(user_id, list_id, notification_url_base)` | POST /subscriptions. Returns `TaskSubscriptionConfig`. Does not persist. |
| `renew_subscription(user_id, sub_id)` | PATCH subscription with new expiry. Returns updated config. Does not persist. |
| `delete_subscription(user_id, sub_id)` | DELETE /subscriptions/{sub_id}. |

---

## 5. Adapters

### 5.1 MicrosoftToDoAdapter (`src/adapters/microsoft_todo_adapter.py`)

Implements `TasksProviderPort` and `TaskLifecyclePort` via MS Graph API.

**Auth:** OAuth tokens fetched per request via `OAuthCredentialsPort`. Token refresh
happens transparently when the access token expires within 5 minutes. Refresh token
endpoint: `https://login.microsoftonline.com/consumers/oauth2/v2.0/token`.

**Primary list caching:** An in-process `{user_id: list_id}` dict avoids repeated
list-lookup calls. Populated from `TaskConfigPort` on first call, then cached for the
instance lifetime.

**batch_get_tasks:** Individual `GET /tasks/{id}` per ref. Bounded concurrency: semaphore=5
(Graph rate limit for personal accounts). 429 responses trigger exponential backoff with
up to 3 retries per task. Failed individual tasks are dropped (not fatal).

**list_tasks across lists:** Graph API has no cross-list endpoint. When `list_id=None`,
the adapter resolves the primary list and fetches its tasks. Multi-list listing follows
the same per-list pattern.

**Checklist diff on update:** Full array replace destroys `checked_at` timestamps.
Instead: diff desired vs. existing by `item_id`, then PATCH changed items, DELETE removed
items, POST new items.

**Subscription expiry:** Graph API allows at most 4320 minutes (3 days) for personal
account subscriptions. The adapter uses 4319 minutes to stay within the limit.

### 5.2 FirestoreTaskSearchIndex (`src/adapters/firestore_task_search_index.py`)

Implements `TaskSearchIndex` using Firestore vector queries.

**Collection:** `{env}_task_search_index`. Document ID: `{user_id}_{task_id}`.

**Search algorithm:** Multi-vector Reciprocal Rank Fusion (RRF). For each active vector
field, an independent `find_nearest` Firestore query runs concurrently (semaphore=10).
Results are merged using RRF with K=60. Cosine distance threshold: 0.4 (documents more
distant are excluded). The top `limit` entries by RRF score are returned.

**Vector fields:** `content_vector` and `context_vector`. Both are queried with the same
embedding of the user's search query.

### 5.3 FirestoreTaskConfigRepository (`src/adapters/firestore_task_config_repository.py`)

Implements `TaskConfigPort`. Collection: `{env}_task_config`. Document ID: `{user_id}`.

`set_primary_list_id_if_absent` uses a Firestore transaction to atomically set
`primary_list_id` only when the field is absent. Concurrent setup calls are safe.

---

## 6. TaskIndexingService (`src/services/task_indexing_service.py`)

Encapsulates the embed-to-index pipeline. No port needed — single implementation.
Used by `TasksAgent` (CRUD hooks), the webhook handler, and `WorkerHandler` (reindex jobs).

### 6.1 index_task(task)

Embeds two text representations in parallel:

- **content_vector:** `title + body + checklist item titles` — captures what the task is about.
- **context_vector:** `list_name + tags + importance` — captures classification and grouping.

Computes `short_id = md5(task_id)[:8]`. Upserts `TaskSearchEntry` into Firestore.

### 6.2 deindex_task(user_id, task_id)

Removes the entry from the search index. Called on task deletion and webhook `deleted` events.

### 6.3 index_task_by_ref(user_id, list_id, task_id)

Fetches the full task from Graph API via `TasksProviderPort.get_task()`, then calls
`index_task()`. Used by the webhook handler on `created` and `updated` notifications.

### 6.4 reindex_list(user_id, list_id)

Fetches all tasks in a list (including completed) and re-indexes them. Bounded concurrency=5.
Used by `WorkerHandler` for the `reindex_task_list` Cloud Task.

### 6.5 search(user_id, query, show_completed, list_id, limit)

Embeds the query string and calls `TaskSearchIndex.find_nearest()` with the same query
vector for both `content_vector` and `context_vector`. Returns `List[TaskSearchEntry]`.
Used by `TasksAgent` to implement the `search_tasks` tool.

### 6.6 resolve_short_id(user_id, short_id)

Calls `TaskSearchIndex.get_by_short_id()` and returns `(list_id, task_id)`. Raises
`ValueError` if not found. Used by `TasksAgent` before `update_task` and `delete_task`
to translate LLM-facing `task_ref` values back to Graph API IDs.

---

## 7. TaskSetupService (`src/services/task_setup_service.py`)

Orchestrates the MS To Do integration lifecycle. No port needed — single implementation.
Called by `WorkerHandler`, the webhook handler, and the Cabinet API.

### 7.1 setup(user_id)

Full onboarding sequence, triggered once after OAuth connection:

1. `TaskLifecyclePort.ensure_primary_list(user_id)` — find or create "Alek Bot Tasks".
2. `TaskConfigPort.set_primary_list_id_if_absent(user_id, list_id)` — persist atomically.
3. `ensure_subscriptions(user_id)` — register webhooks for all lists.

Idempotent — safe to call multiple times.

### 7.2 ensure_subscriptions(user_id)

For each task list, checks whether an active (non-expired) subscription already exists.
Registers a new subscription for any list without one. Enqueues `reindex_task_list` for
each newly subscribed list. Saves the updated config. Idempotent.

### 7.3 handle_subscription_renewal(user_id, sub_id)

Called on every webhook receipt (self-healing layer). Renews the subscription only if
it expires within 48 hours. Persists the updated config.

### 7.4 renew_expiring_subscriptions(user_id)

Sweeps all subscriptions expiring within 24 hours and renews them. Called by Cloud Scheduler
(daily) via `WorkerHandler` (`task_type=renew_task_subscriptions`). On renewal failure,
the old config is kept and retried next day.

### 7.5 disconnect(user_id)

Full teardown:

1. Delete all Graph API webhook subscriptions (best-effort — errors are logged, not fatal).
2. Revoke OAuth credentials via `OAuthCredentialsPort.revoke_credentials()`.
3. Delete all vector search index entries via `TaskSearchIndex.delete_all_for_user()`.
4. Clear persisted config (`TaskUserConfig()` empty object).

### 7.6 reindex_all(user_id)

Calls `ensure_subscriptions(user_id)` to heal any missing subscriptions, then enqueues
`reindex_task_list` for every subscribed list. Used from Cabinet UI and manual admin tasks.

---

## 8. TasksAgent (`src/agents/tasks_agent.py`)

Specialist agent for task management. Intent: `manage_user_tasks`.

### 8.1 Agent Loop

Tool-calling loop, up to `_MAX_TURNS = 4`:

1. Build system prompt with biographical context via `PromptBuilderPort` (`agent_type="tasks"`).
   On prompt build failure: log warning, proceed without biographical context (non-fatal).
2. Call LLM with 5 tool declarations.
3. Execute each tool call and append results to message history.
4. Repeat until LLM produces a final text response (no tool calls).

When `_MAX_TURNS` is reached without a final text response, a forced formatting call is
made with `tools=[]` and the instruction "Summarise the results concisely."

### 8.2 Tools

| Tool | Description |
|---|---|
| `list_tasks` | Return all tasks from the primary list. `show_completed=true` for completed tasks. |
| `search_tasks` | Semantic search via `TaskIndexingService.search()` + `batch_get_tasks()`. Returns matching tasks with their `ref` (short_id). |
| `create_task` | Create via Graph API, then index. Supports title, body, due_datetime, importance, tags, recurrence. |
| `update_task` | PATCH via Graph API, then re-index. Requires `task_ref` from prior `search_tasks` or `list_tasks` result. |
| `delete_task` | DELETE via Graph API, then deindex. Requires `task_ref`. |

### 8.3 search-before-mutate

`update_task` and `delete_task` require `task_ref` (short_id). The LLM must call
`search_tasks` or `list_tasks` first to obtain `ref` values, then use them in mutation
calls. The agent never accepts a raw MS task ID from the user.

`resolve_short_id` translates the `task_ref` back to `(list_id, task_id)` before Graph API
calls. Raises `ValueError` if the ref is not found in the index.

### 8.4 Tool Result Format

`_format_task_list()` serializes tasks to a compact JSON structure. The `ref` field is
`md5(task_id)[:8]` — identical to `TaskSearchEntry.short_id`. Only non-null fields are
included to minimize token usage.

### 8.5 Auto-Tagging

The `create_task` tool description instructs the LLM to infer classification tags from
context (e.g. "remind me about Prague hotel" → `['prague', 'trip']`). Tags map to MS To Do
categories and are indexed in `context_vector` for semantic filtering.

### 8.6 Biographical Context

`PromptBuilderPort.build_for_agent(agent_type="tasks", include_biographical=True)` injects
user biographical facts into the system prompt. This allows the agent to apply personal
context (e.g. known preferences, ongoing projects) when creating or interpreting tasks.

---

## 9. OAuth Flow

```
User clicks "Connect Microsoft To Do" in Cabinet
  → GET /auth/connect-microsoft-todo (requires auth JWT cookie)
       → Validate session → extract user_id
       → Generate CSRF state token
       → Set cookies: microsoft_todo_oauth_state, microsoft_todo_connect_user_id (10-min TTL)
       → 302 → Microsoft consent page
              Tenant: consumers (personal accounts)
              Scopes: Tasks.ReadWrite offline_access
              Response mode: query

User approves → GET /auth/connect-microsoft-todo/callback
  → Validate CSRF state (cookie vs. query param)
  → Validate authorization code present
  → POST https://login.microsoftonline.com/consumers/oauth2/v2.0/token
       → exchange code for access_token + refresh_token
  → Persist OAuthCredentials (provider="microsoft_todo") to Firestore
  → Delete CSRF cookies
  → Enqueue setup_microsoft_todo Cloud Task (user_id)
  → 302 → /cabinet?microsoft_todo_connected=1
```

Token storage fields: `access_token`, `refresh_token`, `token_expiry`, `provider="microsoft_todo"`,
`scopes=["Tasks.ReadWrite", "offline_access"]`.

Token refresh is handled transparently by `MicrosoftToDoAdapter._get_headers()` on every
Graph API request. The refresh token endpoint is
`https://login.microsoftonline.com/consumers/oauth2/v2.0/token`.

---

## 10. Webhook

**Endpoint:** `POST /webhook/microsoft-tasks/{user_id}`

The `user_id` is embedded in the webhook URL path at subscription registration time,
enabling O(1) routing to the correct user without any database lookup.

### 10.1 Graph Validation Challenge

On subscription registration, Graph API sends a one-time validation request:

```
POST /webhook/microsoft-tasks/{user_id}?validationToken=XYZ
```

The handler responds with `200 text/plain` containing the raw `validationToken` value.
No other processing occurs for validation requests.

### 10.2 Change Notification Processing

For real change notifications:

1. Parse JSON body. Return `400` on invalid JSON.
2. For each notification in `value[]`:
   a. Verify `clientState == MICROSOFT_TASKS_WEBHOOK_SECRET`. On mismatch: log warning, skip notification. If no secret configured: skip verification (dev mode).
   b. Extract `list_id` and `task_id` from the `resource` path using regex `r"/me/todo/lists/([^/]+)/tasks/([^/]+)"`.
   c. `changeType == "deleted"` → `TaskIndexingService.deindex_task(user_id, task_id)`
   d. `changeType == "created"` or `"updated"` → `TaskIndexingService.index_task_by_ref(user_id, list_id, task_id)`
   e. Trigger self-healing subscription renewal: `TaskSetupService.handle_subscription_renewal(user_id, sub_id)`.
3. Return `202 Accepted`. Graph requires 202, not 200, for change notifications.

### 10.3 Security

`clientState` in each notification is compared against the `MICROSOFT_TASKS_WEBHOOK_SECRET`
environment variable. This provides CSRF-equivalent protection: only Graph API (which knows
the secret set at subscription time) can trigger index updates.

---

## 11. Subscription Lifecycle

Graph API webhook subscriptions for personal accounts expire after a maximum of 3 days
(4320 minutes). The integration uses three overlapping renewal mechanisms:

**Layer 1 — Self-healing on webhook receipt:**
Every notification triggers `TaskSetupService.handle_subscription_renewal()`. The subscription
is renewed if it expires within 48 hours. This is the primary renewal path for active users
who receive task changes regularly.

**Layer 2 — idempotent ensure_subscriptions:**
Called during `setup()` and `reindex_all()`. Registers a new subscription for any list that
has no active subscription. Acts as recovery for lists that lose their subscription due to
Graph API errors or missed renewals.

**Layer 3 — Cloud Scheduler daily sweep:**
`task_type=renew_all_task_subscriptions` → fan-out: one `renew_task_subscriptions` task
per MS To Do user. Each task calls `TaskSetupService.renew_expiring_subscriptions()`, which
renews any subscription expiring within 24 hours. This catches inactive users whose webhooks
have not triggered self-healing recently.

---

## 12. Worker Tasks

| `task_type` | Handler | Description |
|---|---|---|
| `setup_microsoft_todo` | `TaskSetupService.setup(user_id)` | Onboarding: ensure primary list + subscriptions + enqueue reindex. Enqueued after OAuth callback. |
| `reindex_task_list` | `TaskIndexingService.reindex_list(user_id, list_id)` | Fetch all tasks in a list, embed, upsert index. Enqueued by `ensure_subscriptions` for new lists. |
| `renew_task_subscriptions` | `TaskSetupService.renew_expiring_subscriptions(user_id)` | Renew subscriptions expiring within 24h for one user. |
| `renew_all_task_subscriptions` | Fan-out via `OAuthCredentialsPort.list_users_by_provider("microsoft_todo")` | Daily scheduler task: enqueues `renew_task_subscriptions` for every connected user. |

---

## 13. Cabinet API

| Endpoint | Method | Description |
|---|---|---|
| `/api/tasks/microsoft/status` | `GET` | Connection state and active subscription list (list_id + expires_at per sub). |
| `/api/tasks/microsoft/reindex` | `POST` | Trigger full re-index: `ensure_subscriptions` + enqueue `reindex_task_list` per list. |
| `/api/tasks/microsoft/lists` | `GET` | Return all MS To Do task lists for the user (live Graph API call). |
| `/api/tasks/microsoft/disconnect` | `DELETE` | Full teardown: delete subscriptions, revoke token, clear search index, clear config. |

---

## 14. Recurrence Support

Five recurrence patterns are supported, matching the MS Graph API `recurrencePattern.type`
values that work reliably for personal accounts:

| Pattern | Behavior | Smart defaults from due_datetime |
|---|---|---|
| `daily` | Every N days. Interval configurable. | None needed. |
| `weekdays` | Every Monday through Friday. Convenience alias — translated to `weekly` with `days_of_week=["monday","tuesday","wednesday","thursday","friday"]`. | — |
| `weekly` | On specific days of the week. | If `days_of_week` omitted: derived from weekday of `due_datetime`. |
| `absoluteMonthly` | On a specific day of the month. | If `day_of_month` omitted: derived from day of `due_datetime`. |
| `absoluteYearly` | On a specific day of a specific month. | If `day_of_month` or `month` omitted: derived from `due_datetime`. |

`RecurrenceRange` is always `noEnd` — tasks recur indefinitely. Start date is set to the
current UTC date at creation time.

**Excluded patterns:** `relativeMonthly` and `relativeYearly` (e.g. "third Wednesday of the
month") are not exposed in the agent tools. These patterns are affected by a known Graph API
bug for personal accounts where they are silently converted to `daily`. Excluding them
prevents silent misbehavior.

---

## 15. Known Limitations

**Attachments not fetched inline.** The `Task` domain model has an `attachments: List[TaskAttachment]`
field, but `MicrosoftToDoAdapter._task_from_ms()` does not populate it. Fetching attachment
metadata requires a separate `GET /me/todo/lists/{list_id}/tasks/{task_id}/attachments` call
which is not implemented. `Task.attachments` always returns an empty list.

This is intentional tech debt: attachment content retrieval requires a further download step,
and the use case has not been prioritized. The field structure is in place for future
implementation.

---

## 16. Code References

- `src/domain/task.py` — All task domain models (Task, TaskCreate, TaskUpdate, TaskSearchEntry, TaskUserConfig, recurrence types).
- `src/ports/tasks_provider_port.py` — CRUD port interface.
- `src/ports/task_search_index.py` — Vector search port interface.
- `src/ports/task_config_port.py` — Per-user config port.
- `src/ports/task_lifecycle_port.py` — Subscription management port.
- `src/adapters/microsoft_todo_adapter.py` — Graph API client (TasksProviderPort + TaskLifecyclePort).
- `src/adapters/firestore_task_search_index.py` — Firestore vector search adapter.
- `src/adapters/firestore_task_config_repository.py` — Firestore config adapter.
- `src/services/task_indexing_service.py` — Embed-to-index pipeline.
- `src/services/task_setup_service.py` — Integration lifecycle orchestration.
- `src/agents/tasks_agent.py` — Specialist agent (intent: manage_user_tasks).
- `src/handlers/worker_handler.py` — Cloud Tasks dispatcher (MS To Do task types).
- `src/web/oauth_app.py` — `/auth/connect-microsoft-todo` and callback.
- `src/web/user_cabinet_app.py` — `/api/tasks/microsoft/*` endpoints.
- `src/web/microsoft_tasks_webhook.py` — Graph change notification handler.

---

## 17. Status

**Status:** Production Ready

**Last Updated:** 2026-03-19
