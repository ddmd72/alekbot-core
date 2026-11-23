<!-- living-doc
audited: 2026-11-23
covers:
  src/domain/external_task.py
  src/domain/external_task_delivery.py
  src/domain/external_task_http.py
  src/domain/external_task_jobs.py
  src/domain/external_task_receipt.py
  src/domain/external_task_refusal.py
  src/domain/external_task_rules.py
  src/domain/external_task_text.py
  src/domain/external_task_texts.py
  src/domain/external_task_views.py
  src/domain/external_worker.py
  src/domain/external_worker_key.py
  src/domain/external_worker_principal.py
  src/domain/execution_taint.py
  src/ports/external_task_store.py
  src/ports/external_worker_store.py
  src/ports/window_counter.py
  src/adapters/firestore_external_task_store.py
  src/adapters/firestore_external_worker_store.py
  src/adapters/firestore_window_counter.py
  src/services/external_task_service.py
  src/services/external_task_rate_limiter.py
  src/infrastructure/external_task_tools.py
  src/infrastructure/taint_tracker.py
  src/composition/external_task_setup.py
  src/composition/external_task_api_setup.py
  src/web/external_task_api.py
  src/web/cabinet_external_workers.py
  scripts/set_external_tasks_flag.py
-->
# External task queue

## Purpose

Some work is better done by an agent or a service the user already runs elsewhere: a long piece of research, or a job
that needs tools or models the bot does not have. This block is the hand-off. In a chat turn the bot (Smart) puts a
free-text task into a per-user queue; a worker the user registered claims it, does it elsewhere and submits a text
result; the bot stores that result as untrusted content and tells the user where it is. A worker is anything outside
the bot: a script, a scheduled agent, an automation service.

The queue does not know what a task is about. The user's own skills decide when Smart enqueues and for which worker;
a new kind of hand-off is a new skill, not new code (`agent_skills_workspace`).

What it is not: not a worker runtime (the bot runs nothing of the task), not the voice or MCP surface (not built yet),
not a way for a worker to talk to the user (the result is stored and announced, never answered in a model turn).

Spec and decisions: `docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`. Examples below use the invented user Alex Doe, the
label `research` and the task "compare three community-garden irrigation kits for Shelbyville".

## State of the build

Step 1 of the RFC is in. Built: the queue and `ExternalTaskService`; Smart's five tools (create, list, get, cancel, list
workers) with taint tracking, held tasks and `$task send`; the REST worker endpoint `/external-tasks/v1/*` with
per-worker keys and limits; the Cabinet "External workers" card (register and remove a worker, mint and revoke keys);
lease, deadline and hold timers; delivery of a result to the workspace; the operator flag script.

Not built (RFC steps 2-5): the wake-up POST and its outbound-request guard, the remote MCP resource, its OAuth grants,
the primary drive's Inbox as a destination. Code that mentions them (`WAKE_*` limits, `WorkerDestination.DRIVE`) is
placeholder for those steps.

## Owned data

Firestore, database `us-production`, names from `EnvironmentConfig` (`external_tasks_collection`,
`external_task_results_collection`, `external_workers_collection`, `external_worker_keys_collection`), env prefix.

- **tasks**: one `ExternalTask` per document (`domain/external_task.py`): status, `origin` (the creating turn's
  channel; notices go there), `hold`, `lease`, `result_meta`, `failure`, `delivery`, `created_tainted`. The result body
  is a separate document so a listing never reads it.
- **results**: the submitted text, one per done task.
- **workers** and **worker keys**: a worker is a label plus a description, bound to the registering account. A key is
  stored only as a keyed digest; the raw key is shown once, at minting.
- **rate windows**: per-worker and `401` counters in the `worker_task_dedup` collection through `WindowCounter`.

Two of these carry `purge_after` (tasks, results) and are deleted by a TTL policy 30 days after the final state
(`config/ttl_registry.json`). A task still open has none.

## Flows

**Create to deliver.** Smart's `create_external_task` handler (`src/infrastructure/external_task_tools.py:186`) calls
`ExternalTaskService.create` (`src/services/external_task_service.py:262`): an untainted task is `queued` with a
deadline timer; a worker claims it under a lease (`:371`), may renew, then submits (`:414`), releases or fails. The
final state enqueues an `external_task_deliver` Cloud Task. Delivery writes the result into the workspace as a
flagged copy, posts one literal notice to the origin channel and appends a code-written pair to that channel's history;
it reads no model. A failed or expired task gets the notice and no copy. Delivery is idempotent through
`delivery.state` (Cloud Tasks may run it twice). Task types: `docs/07_deployment/SCHEDULERS.md`.

**Held to sent.** A task created in an execution that read outside content is `held`, not queued. The service returns a
confirmation code once (stored as a keyed digest). The owner's channel gets the task's full text and the command from
the tool's own delivery item, never through the model; the model's result holds the task id only. The owner confirms
with `$task send CODE` (`ConversationHandler`, the `task` command); the service moves it to `queued`. A held task
that is not confirmed expires through its hold timer. A wrong, used or expired code gets one neutral line. A held task
is invisible to every worker surface.

**Timers.** Lease, deadline and hold timers are Cloud Tasks scheduled for the instant; `on_timer` (`:449`) applies the
kind's precondition, so a stale timer changes nothing. A timer further than Cloud Tasks can schedule re-enqueues itself.

**Worker REST.** `create_external_task_api_blueprint` (`src/web/external_task_api.py:73`), not under `/api/`, not
behind the Cabinet cookie. Per request: the `401` limits are checked first, then the key's shape, then `resolve_key`,
then the stored user (active, whitelisted), then the worker's own limits, then the body. A task of another worker, a
held task and a missing task are one `404`. Routes are in `docs/06_runtime/API_REFERENCE.md`.

## Security rules enforced in code

- **Taint.** An execution is tainted when outside content entered it. `domain/execution_taint.py` is the pure rule and
  `TaintTracker` (`src/infrastructure/taint_tracker.py:25`) applies it per execution attempt, fed by the delegation
  engine after every tool batch. Sources: a wrapped result or attached-file reminder, any file part, any tool result
  not on the closed allowlist (`call_is_trusted`, `src/domain/execution_taint.py:60`; web search is not wrapped, which is
  why the list is an allowlist), a forwarded message, an earlier saved model turn that was itself tainted
  (`Message.execution_tainted`), an untrusted agent note, and every read of a result or failure reason
  (`get_external_task` is not on the list).
- **The bit travels.** The final flag is stored on the saved model message, so the next turn builds on a tainted
  answer as tainted. A list that shows a task created in a tainted execution taints the reader (`created_tainted`).
- **Nested Smart gets no tools.** The five tools are added only to an interactive-delivery turn that was not reached by
  delegation and whose user has the flag; the service re-checks flag and lock, so a stale cached profile gains nothing.
- **Untrusted flag on stored copies.** A delivered result is a workspace file marked `external_task:<id>`; agent
  notes written by a tainted execution carry `untrusted`. A later read is wrapped (`agent_skills_workspace`, "the
  untrusted flag").
- **Results never in a model turn on delivery.** Notices are literal and code-written; a worker's text and a failure
  reason are never quoted in them.
- **One refusal for the unknown.** A label that does not exist, another user's task and a missing task give the same
  tool error or `404`; nothing reveals existence.
- **`401` limits live in memory.** Per address (rightmost `X-Forwarded-For` entry) and global, held by the rate limiter
  over `WindowCounter`; the per-worker limits count calls, claims and submits. Logs name a worker by
  `fingerprint(worker_id)`, never a key, text, result or header.
- **Inactive user.** Refused at the Cabinet routes, the REST endpoint and `$task send`.

## Traps

- **Indexes first.** The queue's composite indexes in `config/firestore.indexes.json` must be `READY` before the code
  deploys (`make deploy-indexes`); a missing one fails at claim or list time, not at deploy.
- **The flag is operator-only.** `config.external_tasks_enabled` is set by `scripts/set_external_tasks_flag.py` (dry run
  unless `--apply`) or the console. No Cabinet route, command or API writes it. Smart's tools follow the flag within
  about an hour (the factory caches a profile); the service reads it fresh. Switching off deletes nothing.
- **Two `purge_after` collections**: tasks and results; both are in the TTL registry and a test scans for it. A new
  collection of this block needs its own entry.
- **Limits are placeholders** set at implementation review (named constants in `domain/external_task.py`); none was
  measured. Change the constant, not a copy.
- **Not a place for a worker's secrets.** A task text is plain stored text capped to fit one literal receipt.

## Links

`docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`, `docs/07_deployment/SCHEDULERS.md` (timer and deliver task types),
`docs/05_building_blocks/agent_skills_workspace/README.md` (workspace flag), `docs/06_runtime/API_REFERENCE.md`
(worker routes).
