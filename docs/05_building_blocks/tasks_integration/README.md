<!-- living-doc
covers:
  src/adapters/microsoft/todo_adapter.py
  src/adapters/google_tasks_adapter.py
  src/adapters/firestore_task_config_repository.py
  src/adapters/firestore_task_search_index.py
  src/ports/task_config_port.py
  src/ports/task_lifecycle_port.py
  src/ports/task_search_index.py
  src/ports/tasks_provider_port.py
  src/services/task_indexing_service.py
  src/services/task_setup_service.py
  src/agents/tasks_agent.py
  src/domain/task.py
  src/web/microsoft_tasks_webhook.py
  scripts/infrastructure/create_task_search_vector_indexes.sh
  scripts/debug_task_index.py
-->
# Microsoft To Do Integration

## Purpose

This block lets the user manage personal tasks in chat. A specialist agent (`TasksAgent`, intent `manage_user_tasks`)
turns a natural-language delegation into Microsoft Graph calls against the user's To Do account, and finds tasks by
meaning on long lists.

**Local-first.** Microsoft To Do is the only source of truth for a task. Firestore keeps a thin search index (a few
display fields, two vectors, a short reference) and the per-user integration config. A task's full content is always
read from Graph on demand, so the two systems cannot diverge on content; the index is kept fresh by Graph change
notifications and by full re-reads of the list.

What it is not: it does not write memory facts, it does not mirror tasks, and it works on one list only, the primary
list the bot creates for itself (`src/adapters/microsoft/todo_adapter.py::_PRIMARY_LIST_NAME`). Google Tasks is frozen:
`GoogleTasksAdapter` still implements `TasksProviderPort` but is not wired (`src/composition/service_container.py`
keeps it commented out), and its consent runs no setup.

## Owned data

Firestore, `us-production` database, environment prefix on collection names.

**Search index** — `EnvironmentConfig.task_search_index_collection`, document id `{user_id}_{task_id}`
(`src/adapters/firestore_task_search_index.py::FirestoreTaskSearchIndex`). Written by `TaskIndexingService.index_task`,
read by `TaskIndexingService.search` and `resolve_short_id`. One `TaskSearchEntry` (`src/domain/task.py`) holds:
- identity and display: `task_id`, `list_id`, `list_name`, `title`, `status`, `tags`, `importance`, `indexed_at`;
- `short_id`: a short hash of `task_id` (`TaskIndexingService.index_task`), the only task reference the model sees (a Graph id is ~180 characters);
- `content_vector`: embedding of title, body and checklist item titles (`TaskIndexingService._content_text`);
- `context_vector`: embedding of list name, tags and importance (`TaskIndexingService._context_text`).

The body is not stored. Vector indexes must exist before search works: `config/firestore.indexes.json` and
`scripts/infrastructure/create_task_search_vector_indexes.sh`; `scripts/debug_task_index.py` inspects the index.

**Config** — `EnvironmentConfig.task_config_collection`, document id `{user_id}`
(`src/adapters/firestore_task_config_repository.py::FirestoreTaskConfigRepository`, `TaskUserConfig`):
`primary_list_id` (set once), `subscriptions` (Graph webhook subscriptions: `sub_id`, `list_id`, `expires_at`), and
`reconnect_notified` (the one reconnect notice of a lost grant was sent). A deliberate disconnect resets the whole
document.

**Credential** — the user's grant is a record of kind `microsoft_todo` in the shared credential store
(`src/composition/connector_registry.py::MICROSOFT_TODO`, its `required_scopes`). Microsoft grants are never revoked at
the provider (`src/adapters/microsoft/microsoft_oauth_provider.py::MicrosoftOAuthProvider.revoke` does nothing);
disconnect only deletes the stored record.

## Flows

### Connect

1. The Cabinet sends the user to `/auth/connect/<kind>` (`src/web/oauth_app.py::create_oauth_blueprint`); the shared
   callback stores the credential and runs the integration's activation.
2. `src/composition/oauth_connect_setup.py::build_connect_activations` (its `tasks` activation) clears the reconnect
   state and enqueues the Cloud Task `setup_microsoft_todo`. Google Tasks consents do nothing here.
3. `WorkerHandler._handle_setup_microsoft_todo` calls `TaskSetupService.setup`: find or create the primary list
   (`TaskLifecyclePort.ensure_primary_list`), persist it with `set_primary_list_id_if_absent`, then re-subscribe.

### Setup is a re-subscribe, not a no-op

A consent is a new grant; per the `setup` docstring, Graph may have dropped subscriptions a stored config still calls
active. `setup` therefore
deletes every stored subscription (best effort), registers one for the primary list, saves the config and enqueues
`reindex_task_list` once (`TaskSetupService._resubscribe`). The reindex is an upsert that deletes nothing, so changes
made while the grant was gone are read back. `ensure_subscriptions`, by contrast, keeps an active subscription and
re-reads nothing; it runs from `reindex_all` and heals a missing one.

### Task lookup and change (chat turn)

1. The router delegates to `src/agents/tasks_agent.py::TasksAgent.execute`: a tool loop of at most `_MAX_TURNS`
   turns over the tools in `_TOOL_DECLARATIONS`: a lookup (`list_tasks`), a search for long lists, and create, update and
   delete. If the prompt build fails, the run goes on with an empty system prompt (not only without the biographical
   facts), and the failure is only logged.
2. `list_tasks` fetches the primary list from Graph and filters, orders and pages in the agent
   (`TasksAgent._list_page`, `_PAGE_SIZE`): Graph's To Do list call is read as a full fetch, with only a status
   filter sent. Dated tasks come first (soonest first), then undated (newest first). The result carries `total`,
   `page`, `pages` and a `note` that says whether the model has seen every task.
3. `search_tasks` is for lists longer than one page: `TaskIndexingService.search` embeds the query and sends the same
   vector for both fields to `FirestoreTaskSearchIndex.find_nearest`; the agent then loads the hits with
   `TasksProviderPort.batch_get_tasks`. A miss returns a `note` that another wording will not find more.
4. `create_task` / `update_task` write to Graph, then re-index the returned task; `delete_task` deletes at Graph, then
   `deindex_task`. A new due date on a task that had none, or on a new task with no explicit reminder, gets an
   automatic reminder in the evening before the due date, in the user's zone (`TasksAgent._derive_reminder`).
5. Times: a time of day without an offset is the user's wall time and is converted with `wall_time_instant`; a date
   alone stays that day (`TasksAgent._parse_user_time`).
6. Recurrence: the tool enum exposes the daily, weekly and absolute monthly/yearly patterns, plus a `weekdays` alias
   sent as weekly Monday to Friday, always with no end date (`TasksAgent._parse_recurrence`). The relative
   monthly/yearly patterns are left out on purpose: the recorded reason is that Graph turns them into daily for
   personal accounts, so they would silently misbehave.

**Time budget.** A run's budget is its timeout. No tool turn starts past the budget minus `_ANSWER_RESERVE_S`, and the model call
of each turn is bounded by `asyncio.wait_for` (the tool executions of a turn are not, so a slow tool can run past the
budget). When the turn cap or the budget stops the loop, one tool-less turn answers
(`_FORCE_ANSWER`, ending `_FINAL_MARGIN_S` before the timeout); if that cannot run or returns no text, the answer is the
tool results themselves (`TasksAgent._facts_answer`). A run that has tool results never ends as a timeout, because the
orchestrator could repeat a write. A run with no tool results whose first turn times out, or whose text is cut at the
output limit, fails (`AgentResponse.failure`).

### Index freshness (webhook)

1. Subscription: `MicrosoftToDoAdapter.register_subscription` sets the notification URL to
   `/webhook/microsoft-tasks/{user_id}` (the user id in the path routes without a lookup) and passes the shared
   `clientState` secret.
2. Graph first sends `?validationToken=`; the handler echoes it as `text/plain`
   (`src/web/microsoft_tasks_webhook.py::create_microsoft_tasks_webhook_blueprint`).
3. For each notification: check `clientState`; parse the list id (and, if present, the task id) from `resource`;
   require `TaskSetupService.owns_subscription(user_id, sub_id, list_id)`; then update the index.
   - With a task id: `deleted` calls `deindex_task`; `created` and `updated` call `index_task_by_ref`.
   - Without one (per the code comments, what consumer accounts send: a list-level resource): enqueue `reindex_task_list` for the list
     (`TaskSetupService.enqueue_reindex_list`).
   - Then `TaskSetupService.handle_subscription_renewal` (self-healing). Once the body parses, the reply is `202`; a body that is not JSON gets `400`.

### Subscription lifetime

Graph subscriptions are short-lived (`_SUB_EXPIRY_MINUTES` in the adapter). Three layers keep them alive:
1. every notification renews a subscription that expires within `_SUB_RENEWAL_THRESHOLD_H`;
2. `ensure_subscriptions` (from `reindex_all`) registers one for a primary list that has no unexpired subscription;
3. a daily Cloud Scheduler task `renew_all_task_subscriptions` fans out `renew_task_subscriptions` per To Do user
   (`WorkerHandler._handle_renew_all_task_subscriptions`), which renews those expiring within
   `_SUB_EXPIRING_THRESHOLD_H` (`TaskSetupService.renew_expiring_subscriptions`). A failed renewal keeps the old entry
   for the next day.

A renewal that finds the subscription gone at Graph (`SubscriptionNotFoundError`) registers a replacement and enqueues a
reindex (`_replace_orphan`); if that fails the entry is dropped and `setup` or `ensure_subscriptions` recreates it.

### Cabinet and disconnect

The Cabinet endpoints under `/api/tasks/microsoft/` (`src/web/user_cabinet_app.py`) mostly call `TaskSetupService` (status, reindex via `reindex_all`, disconnect); the lists endpoint calls
`TasksProviderPort.list_task_lists` directly (a live Graph call). `TaskSetupService.disconnect` deletes
the Graph subscriptions (best effort), the stored credentials, every index entry of the user, and resets the config.

## Invariants and traps

- **The model never sees a Graph id.** `update_task` and `delete_task` take a `task_ref` (`short_id`) taken from an
  earlier `list_tasks` or `search_tasks` result and resolve it with `TaskIndexingService.resolve_short_id`, which raises
  `ValueError` for an unknown ref. `short_id` is not unique by construction: on a clash the index returns one of the
  matches, unspecified (`TaskSearchIndex.get_by_short_id`).
- **`clientState` is one secret for every subscription.** It proves the sender is Graph, not whose notification it is.
  Binding to the URL's user is `owns_subscription`; a notification for another user's subscription, or one whose check
  fails, touches nothing. With no secret configured the check is skipped locally, but a deployed service (`K_SERVICE`
  set) rejects every notification.
- **The webhook never blocks Graph.** Every per-notification error is logged and the reply stays `202` for a parsed body; a failed index
  update is repaired by the next notification or reindex.
- **Config writes are read-modify-write.** `save_config` replaces the whole document. `ensure_subscriptions` carries a
  freshly stored `primary_list_id` into the config it saves, or the save would erase it. Only
  `set_primary_list_id_if_absent` (a Firestore transaction) and `set_reconnect_notified` (a field update) are safe
  against a concurrent writer.
- **A 204 renewal has no body**, so the adapter returns an empty `list_id`; `TaskSetupService._preserve_list_id` keeps
  the stored one.
- **The list name and the first page of tasks are read concurrently** (`MicrosoftToDoAdapter._fetch_tasks_for_list`), so
  a lookup over one Graph page is one round trip (further pages add more) and two model turns; a task lookup must answer within seconds.
- **Checklists are diffed, never replaced** (`MicrosoftToDoAdapter._sync_checklist`): a full replace destroys the
  items' checked timestamps.
- **Graph throttling.** `batch_get_tasks` runs at most `_BATCH_SEMAPHORE` fetches at once, makes a bounded number of
  attempts (a 429 is retried with backoff) and drops a task that still fails instead of failing the search.
- **Search is two-vector RRF.** `find_nearest` runs one cosine query per vector field, drops entries beyond
  `_MAX_COSINE_DISTANCE`, fuses with `_RRF_K`, and filters `status != completed` unless completed tasks are requested.
  A missing vector index fails at query time, not at deploy.
- **Attachments are not read.** `Task.attachments` is always empty: `_task_from_ms` does not fetch them.
- **Reconnect notice.** A To Do user whose config has a `primary_list_id` but no credential gets one notice
  (`src/services/integration_reconnect_service.py::IntegrationReconnectService`); a new consent clears the flag.

## Links

- RFCs: [`TASKS_LOCAL_FIRST_RFC.md`](../../10_rfcs/TASKS_LOCAL_FIRST_RFC.md),
  [`TASKS_AGENT_RFC.md`](../../10_rfcs/TASKS_AGENT_RFC.md)
- Decisions: [`local_first_thin_search_index.md`](../../04_solution_strategy/decisions/local_first_thin_search_index.md),
  [`reconnect_notice_r1.md`](../../04_solution_strategy/decisions/reconnect_notice_r1.md)
- Neighbours: [OAuth web API](../oauth_web_api/README.md), [agent registry](../agent_registry/README.md),
  [multi-vector RRF search](../../08_concepts/multi_vector_rrf_search.md)

## Where to start reading

- `src/agents/tasks_agent.py::TasksAgent.execute` — the tool loop and time budget.
- `src/services/task_setup_service.py::TaskSetupService` — setup, subscriptions, disconnect.
- `src/services/task_indexing_service.py::TaskIndexingService` — embed, index, search, `resolve_short_id`.
- `src/web/microsoft_tasks_webhook.py::create_microsoft_tasks_webhook_blueprint` — notification handling.
- `src/adapters/microsoft/todo_adapter.py::MicrosoftToDoAdapter` — Graph calls and subscriptions.
- `src/adapters/firestore_task_search_index.py::FirestoreTaskSearchIndex` — vector search.
