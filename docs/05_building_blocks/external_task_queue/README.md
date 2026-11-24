<!-- living-doc
audited: 2026-11-24
covers:
  src/domain/external_task.py
  src/domain/external_task_entry.py
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
  src/domain/wake_refusal.py
  src/domain/wake_target.py
  src/domain/wake_curl.py
  src/ports/external_task_store.py
  src/ports/external_worker_store.py
  src/ports/window_counter.py
  src/ports/outbound_webhook_port.py
  src/adapters/outbound_webhook/guarded_webhook_adapter.py
  src/adapters/firestore_external_task_store.py
  src/adapters/firestore_external_worker_store.py
  src/adapters/firestore_window_counter.py
  src/services/external_task_service.py
  src/services/external_task_rate_limiter.py
  src/services/external_worker_user_gate.py
  src/infrastructure/external_task_tools.py
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
not a way for a worker to talk to the user (the result is stored and announced, never answered in a model turn), and
not a push of work: a wake-up tells a worker that work exists, never what it is.

Spec and decisions: `docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`. Examples below use the invented user Alex Doe, the
label `research` and the task "compare three community-garden irrigation kits for Shelbyville".

## State of the build

Steps 1 and 2 of the RFC are in. Built: the queue and `ExternalTaskService`; Smart's five tools (create, list, get,
cancel, list workers) where every task created in a chat turn is held until the owner confirms it with `$task send`; the REST worker endpoint `/external-tasks/v1/*`
with per-worker keys and limits; the Cabinet "External workers" card (register and remove a worker, mint and revoke
keys, set and clear a wake-up target; the target is entered as a pasted `curl` command plus a separate secret, see below); lease, deadline and hold timers; delivery of a result to the workspace; the
wake-up POST behind the outbound-request guard; the operator flag script.

Step 3 is in too: the worker MCP resource `/tasks-mcp` on worker keys (below).

Not built (RFC steps 4-5): OAuth grants for the MCP resource, the primary drive's Inbox as a destination. Code that
mentions them (`WorkerDestination.DRIVE`, `SubmitVia.OAUTH`) is placeholder for those steps.

## Owned data

Firestore, database `us-production`, names from `EnvironmentConfig` (`external_tasks_collection`,
`external_task_results_collection`, `external_workers_collection`, `external_worker_keys_collection`), env prefix.

- **tasks**: one `ExternalTask` per document (`domain/external_task.py`): status, `origin` (the creating turn's
  channel; notices go there), `hold`, `lease`, `result_meta`, `failure`, `delivery` (a stored `created_tainted` of an older record is ignored on read and dropped at the next write). The result body
  is a separate document so a listing never reads it.
- **results**: the submitted text, one per done task.
- **workers** and **worker keys**: a worker is a label plus a description, bound to the registering account. A key is
  stored only as a keyed digest; the raw key is shown once, at minting. A worker's wake-up target is one `CipherPort`
  ciphertext of `{url, headers}` bound to the record (AAD `external_workers/{user_id}:{worker_id}`); only its host is
  stored in clear (`wake_host`), and `wake_generation` counts the pings started.
- **rate windows**: per-worker and `401` counters in the `worker_task_dedup` collection through `WindowCounter`.

Two of these carry `purge_after` (tasks, results) and are deleted by a TTL policy 30 days after the final state
(`config/ttl_registry.json`). A task still open has none.

## Flows

**Create to deliver.** Smart's `create_external_task` handler (`src/infrastructure/external_task_tools.py:187`) calls
`ExternalTaskService.create` (`src/services/external_task_service.py:328`): the `entry` decides: a chat create is `held` (below), a reminder create is `queued` with a
deadline timer (Smart's local tool always passes `TaskEntry.CHAT`); a worker claims it under a lease (`:440`), may renew, then submits (`:483`), releases or fails. The
final state enqueues an `external_task_deliver` Cloud Task. Delivery writes the result into the workspace as a
copy under an outside-content header, posts one literal notice to the origin channel and appends a code-written pair to that channel's history;
it reads no model. A failed or expired task gets the notice and no copy. Delivery is idempotent through
`delivery.state` (Cloud Tasks may run it twice). Task types: `docs/07_deployment/SCHEDULERS.md`.

**Held to sent.** Every task created in a chat turn waits for the owner's confirmation: it is `held`, not queued, whatever the turn read
(`decisions/external_task_confirmation_and_alerts.md`). The service returns a
confirmation code once (stored as a keyed digest). The owner's channel gets the task's full text and the command from
the tool's own delivery item, never through the model; the model's result holds the task id only. The owner confirms
with `$task send CODE` (`ConversationHandler`, the `task` command); the service moves it to `queued`. A held task
that is not confirmed expires through its hold timer. A wrong, used or expired code gets one neutral line. A held task
is invisible to every worker surface.

**Timers.** Lease, deadline and hold timers are Cloud Tasks scheduled for the instant; `on_timer` (`:518`) applies the
kind's precondition, so a stale timer changes nothing. A timer further than Cloud Tasks can schedule re-enqueues itself.

**Wake-up.** A worker with a target is pinged when work appears, instead of polling. When a task becomes `queued` (a
reminder create or `$task send`) and it is the worker's oldest queued task, and no task of the worker holds an
unexpired lease (the worker is draining), the service moves the worker's `wake_generation` on and enqueues
`external_task_wake` for that generation; a lease timer that re-queues a task pings the same way. A held task never
pings. `ExternalTaskWakeTask` calls `on_wake`, which re-reads the worker (a changed generation, a cleared target or no
queued work sends nothing), decrypts the target and POSTs `{"event": "tasks_available", "worker": <worker id>}` through
`OutboundWebhookPort`: no task text, title, label or count. A 5xx or a timeout is retried by Cloud Tasks up to
`WAKE_MAX_ATTEMPTS`; a guard refusal, a 3xx or a 4xx is final and the owner gets one literal notice naming the label and
the closed `WakeRefusal` code. The task stays queued either way, for polling or the next ping.

**Outbound-request guard** (`src/adapters/outbound_webhook/guarded_webhook_adapter.py`, the rules in
`src/domain/wake_target.py`). The only code that sends to a user-entered URL. Before any DNS query: `https`, port 443,
no userinfo, a DNS name of two labels or more (no IP literal, no numeric form), not under `.internal`, `.localhost`,
`.local` or `.arpa`, not one of the bot's own hosts (built at boot from every deploy unit's configured URL,
`wake_own_hosts` in `src/composition/external_task_setup.py`) and not under `run.app`; header names are tokens, values
printable, framing, hop-by-hop and `Accept-Encoding` headers refused, count and size capped. Then every resolved address
must be public (no private, loopback, link-local or metadata, CGNAT, multicast, reserved, documentation, ULA, IPv6
site-local, nor any range that embeds IPv4: NAT64, 6to4, Teredo), the connection goes to the vetted address with the
name as `Host` and SNI (no second resolution, so no rebinding), no redirect is followed, no proxy is taken from the
environment, and the answer is asked for uncompressed and read as raw bytes up to a small cap, then dropped (a
compressed body cannot expand past the cap). The Cabinet save runs the name-level rules only; every send runs them all
again.

**Worker REST.** `create_external_task_api_blueprint` (`src/web/external_task_api.py:73`), not under `/api/`, not
behind the Cabinet cookie. Per request: the `401` limits are checked first, then the key's shape, then `resolve_key`,
then the stored user (active, whitelisted: `ExternalWorkerUserGate`, `src/services/external_worker_user_gate.py`, the
one whitelist rule of every worker-key surface; the core leaves it out), then the worker's own limits, then the body. A task of another worker, a
held task and a missing task are one `404`. Routes are in `docs/06_runtime/API_REFERENCE.md`.

**Worker MCP.** `/tasks-mcp` (`src/composition/tasks_mcp_setup.py`; how it sits beside the facts and drive connectors:
[`remote_mcp_server`](../remote_mcp_server/README.md)) serves six tools over the same service operations as REST:
`list_tasks`, `claim_task`, `renew_lease`, `submit_task_result`, `release_task`, `fail_task`. A client sends a worker
key as its bearer. The key is checked as REST checks it (`resolve_key`, then `ExternalWorkerUserGate`), but every
refusal at that point is `401` (the SDK's verifier returns a token or nothing), and the `401` address limits of REST
are not applied. The worker's limits are the REST buckets, shared. Answers are text from two prompt tokens; a claim
prints the task id, created time, lease and "written by the user's assistant" line before the title and the brief, and
the brief is plain. A refusal is an `isError` result naming its code; no task is a success.

**Token upload order.** `MCP_TASK_TOOL_TEXTS`, then `MCP_TASK_RESULT_TEXTS`
(`prompts_snapshot/tokens/system/`, `firestore_utils/snapshot_upload.py`), both before the deploy that builds the app:
without them the app is not built and `/tasks-mcp` answers 404 (one ops alert).

## Security rules enforced in code

- **A chat create always waits.** Every task a chat turn creates is held for the owner's `$task send`, whatever the
  turn read; a reminder run's create is queued. There is no per-execution taint tracking (decision record
  `external_task_confirmation_and_alerts.md`).
- **Nested Smart gets no tools.** The five tools are added only to an interactive-delivery turn that was not reached by
  delegation and whose user has the flag; the service re-checks flag and lock, so a stale cached profile gains nothing.
- **A worker's text is wrapped where a model reads it.** `get_external_task` returns a result or a failure reason
  inside an outside-content wrapper after the reminder; a delivered result is an ordinary workspace file under an
  outside-content header (no file-level mark).
- **Results never in a model turn on delivery.** Notices are literal and code-written; a worker's text and a failure
  reason are never quoted in them.
- **One refusal for the unknown.** A label that does not exist, another user's task and a missing task give the same
  tool error or `404`; nothing reveals existence.
- **`401` limits live in memory.** Per address (rightmost `X-Forwarded-For` entry) and global, held by the rate limiter
  over `WindowCounter`; the per-worker limits count calls, claims and submits. Logs name a worker by
  `fingerprint(worker_id)`, never a key, text, result or header.
- **Inactive user.** Refused at the Cabinet routes, the REST endpoint and `$task send`.
- **A worker's description never reaches a model.** It is free text typed in the Cabinet; a tool or parameter
  description is read with the schema's authority, so Smart's tools carry labels only (the `worker` enum and its
  lead line, the `list_external_workers` result). The description is shown to the owner in the Cabinet and nowhere else.
- **Wake-up secrets are never shown back or logged.** The Cabinet gets the host only after a save; logs name the host
  and a code or an error class, never the URL, a header or the target's answer. httpx's own request log line is dropped
  while a send runs.

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
- **The guard assumes no VPC egress.** The main service reaches only the public internet; a VPC connector would let a
  wake-up URL reach a private network the address rules do not know. The deploy trap: `docs/07_deployment/README.md`,
  "Main service egress".
- **A worker on the platform's default domain cannot be woken.** Every name under `run.app` is refused (the bot's own
  services live there); such a worker needs a custom domain, or polls.
- **Not a place for a worker's secrets.** A task text is plain stored text capped to fit one literal receipt.

## Links

`docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`, `docs/07_deployment/SCHEDULERS.md` (timer, deliver and wake task types),
`docs/05_building_blocks/agent_skills_workspace/README.md` (workspace flag), `docs/06_runtime/API_REFERENCE.md`
(worker routes).

## Wake-up target form (Cabinet)

The worker card takes two fields: a `curl` command as a webhook provider shows it, and the secret. The server parses the
command (`src/domain/wake_curl.py`) into the URL and the header lines, then stores them through the same path as a typed
target (`save_wake_target`, `check_headers`). Rules: the command starts with `curl`; only `-X POST`, `-H`, the data
options (their value is ignored: the bot writes its own body), `--url` and the quiet/fail flags are read, and any other
option is refused (`wake_curl_option`), never skipped; a `Content-Type` header is dropped. A placeholder `$NAME` or
`${NAME}` in the URL or a header value is replaced by the secret; all placeholders must share one name, a placeholder
needs a non-empty secret and a secret needs a placeholder (leave it empty when the command already holds it). The old
`{url, headers}` body of `PUT /api/external-workers/<id>/wake` is still accepted. Neither the command nor the secret is
logged or sent back.
