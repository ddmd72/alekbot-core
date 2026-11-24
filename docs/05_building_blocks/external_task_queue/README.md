<!-- living-doc
audited: 2026-11-24
covers:
  src/domain/external_task.py
  src/domain/external_task_alert.py
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
  src/services/external_task_send_choice.py
  src/services/external_task_rate_limiter.py
  src/services/external_worker_user_gate.py
  src/infrastructure/external_task_tools.py
  src/agents/external_task_agent.py
  src/composition/external_task_setup.py
  src/composition/external_task_api_setup.py
  src/web/external_task_api.py
  src/web/cabinet_external_workers.py
  scripts/set_external_tasks_flag.py
-->
# External task queue

## Purpose

Some work is better done by an agent or a service the user already runs elsewhere: a long piece of research, or a job
that needs tools or models the bot does not have. This block is the hand-off. In a chat turn the bot (Smart, through
the task specialist) puts a free-text task into a per-user queue; a worker the user registered claims it, does it elsewhere and submits a text
result; the bot stores that result as untrusted content and tells the user where it is. A worker is anything outside
the bot: a script, a scheduled agent, an automation service.

The queue does not know what a task is about. The user's own skills decide when Smart hands work over and to which
worker;
a new kind of hand-off is a new skill, not new code (`agent_skills_workspace`).

What it is not: not a worker runtime (the bot runs nothing of the task), not the voice or MCP surface (not built yet),
not a way for a worker to talk to the user (the result is stored and announced, never answered in a model turn), and
not a push of work: a wake-up tells a worker that work exists, never what it is.

Spec and decisions: `docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`. Examples below use the invented user Alex Doe, the
label `research` and the task "compare three community-garden irrigation kits for Shelbyville".

## State of the build

Steps 1 and 2 of the RFC are in. Built: the queue and `ExternalTaskService`; the task specialist
(`ExternalTaskAgent`, intent `work_with_external_tasks`) with the five queue tools (create, list, get, cancel, list
workers), where every task created in a chat turn is held until the owner confirms it with `$task send` (on Slack
also with the «Send» button under the command); the REST
worker endpoint `/external-tasks/v1/*`
with per-worker keys and limits; the Cabinet "External workers" card (register and remove a worker, mint and revoke
keys, set and clear a wake-up target; the target is entered as a pasted `curl` command plus a separate secret, see below); lease, deadline and hold timers; delivery of a result to the workspace; the
wake-up POST behind the outbound-request guard; the operator flag script.

Step 3 is in too: the worker MCP resource `/tasks-mcp` on worker keys (below).

Step 4 is in too: OAuth for `/tasks-mcp` (a claude.ai custom connector). A client may register the scope
`external_tasks` (never a default); the consent page asks which worker the client acts as and the grant is bound to it
(`wid`); the flag is checked at consent, code exchange and refresh, and every tool call re-checks the grant; the
Cabinet lists each worker's OAuth grants (client name, granted time, last refresh) with Revoke, and removing a worker
revokes its grants. The live check of §10 Q1 (the authorize-time scope equals the registered one) is run after the
deploy.

Not built: step 5, the primary drive's Inbox as a destination. Code that mentions it (`WorkerDestination.DRIVE`) is
placeholder for that step.

## Owned data

Firestore, database `us-production`, names from `EnvironmentConfig` (`external_tasks_collection`,
`external_task_results_collection`, `external_workers_collection`, `external_worker_keys_collection`), env prefix.

- **tasks**: one `ExternalTask` per document (`domain/external_task.py`): status, `origin` (the creating turn's
  channel; the delivery alert goes there), `hold`, `lease`, `result_meta`, `failure`, `delivery` (a stored `created_tainted` of an older record is ignored on read and dropped at the next write). The result body
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

**Hand-off** (`decisions/external_task_specialist.md`). Smart has no queue tool. On the owner's own chat turn or while one
of the owner's reminders runs (`task_entry`, `src/domain/external_task_entry.py`), for a flagged user, not reached by
delegation, it is offered one internal intent, `work_with_external_tasks`,
and writes the task specialist a natural-language instruction: the worker's name and the whole brief, or which task to
read or cancel. `ExternalTaskAgent` (`src/agents/external_task_agent.py`) repeats that gate in code before any model
call (a run with a task entry, a call chain of itself alone, a service, a user, then the service's fresh flag and lock
check) and answers a refusal with one code-written line. It runs its own engine loop over the five tools, delegating
nothing; its system prompt ends, after the cache boundary, with a `queue_state` block of worker labels. Its answer is
composed by code from a pass-through record of every tool call: each result word for word, then the model's short note,
then a line when the run was cut. The receipts come from that record, so a run cut by its soft deadline, the turn cap or
an engine exception after a create still posts the owner's receipt; it answers success whenever a tool ran (a failure
would drop the delivery items).

**The brief, word for word.** The create handler refuses a text that is not a part of the instruction after whitespace
is normalised (`ERROR_NOT_THE_BRIEF`), so what leaves the system is Smart's brief; the specialist chooses the title and
the worker label only. The brief's language follows the owner's skill or words, not "Always English"
(`PROTOCOL_AGENT_SELECTION`, `query_language`).

**Create to deliver.** The specialist's `create_external_task` handler (`src/infrastructure/external_task_tools.py`)
calls `ExternalTaskService.create` (`src/services/external_task_service.py:328`): the `entry` decides: a chat create is
`held` (below), a reminder create is `queued` with a deadline timer (the specialist computes the entry once per run from the context
with `task_entry`, never from the model: `CHAT` on the owner's chat turn, `REMINDER` while a reminder runs); a
worker claims it under a lease (`:440`), may renew, then submits (`:483`), releases or fails. The
final state enqueues an `external_task_deliver` Cloud Task (dispatch deadline `DELIVER_DEADLINE_S`, the
`EXTERNAL_TASK` notification budget plus headroom).

**Delivery is a system alert** (`src/services/external_task_delivery_task.py`, since 2026-11-24). A finished task comes
back to the bot the way a fired reminder does. The runner first writes a done task's result, or the reason a worker gave
when it failed the task, as an ordinary workspace file under `references/external_tasks/`, below a code-written header
that names the task, its title, the worker and the time (`src/domain/external_task_delivery.py`). Then it calls
`UserNotificationService.notify` with kind `EXTERNAL_TASK`, Smart as the recipient, the task's origin channel and
`origin_detail="external_task_deliver"`: the alert is the user turn of a background Smart run, and Smart's answer is what
the owner reads; the alert and the answer are kept as that channel's history pair. The alert
(`src/domain/external_task_alert.py`) carries only code-made values: the task id, the worker label, the file's path, a
failure code and counts; never the title, the result or a reason. It says the file holds the worker's own text, to be
read with the workspace tool when the owner needs it and reported as data. Six alerts: done (file), done without a file
(a refused copy or no workspace: the result stays in the task record), failed with the reason's file, failed with no
file, given up after repeated claims (`claim_loop`), expired. A cancelled task gets none. Every outcome of the run is
final and recorded, as for a reminder: no channel is logged, an empty answer is reported to ops, a failed turn is not run
again. Delivery is idempotent through `delivery.state` (Cloud Tasks may run it twice). Task types:
`docs/07_deployment/SCHEDULERS.md`.

**Held to sent.** Every task created in a chat turn waits for the owner's confirmation: it is `held`, not queued, whatever the turn read
(`decisions/external_task_confirmation_and_alerts.md`). The service returns a
confirmation code once (stored as a keyed digest). The owner's channel gets the task's full text and the command from
the tool's own delivery item, never through a model; the result the specialist's model and Smart read holds the task id
only. The receipt is a literal notice (`src/domain/literal_notice.py`, shared with the `$memory save` preview and the
reminder notices): the task text byte for byte as the file `task-<id>.txt` (so nothing in it can be cut, formatted or
pose as the receipt's own lines), then a literal message with the worker, the title and the task id; the command
follows as its own message, after the file. The owner confirms
with `$task send CODE` (`ConversationHandler`, the `task` command); the service moves it to `queued`. A held task
that is not confirmed expires through its hold timer. A wrong, used or expired code gets one neutral line. A held task
is invisible to every worker surface.

**The Send button (Slack).** On Slack the command message carries one «Send» button: it is an interactive message of
the memory review's mechanism (`external_task_receipt.send_button`; the same command text as its prompt), posted in the
receipt's thread when the receipt has one. The binding names the task (`external_task_send:<task_id>`), the button's
option id is the code, so the code is carried only on the message that already shows it. A press goes the shared click
path (`POST /slack/interactivity` → `interactive_choice` task → `InteractiveChoiceService`, IAM read fresh, so an
unknown or locked user stops there) to `ExternalTaskSendChoice`: the task must be the clicker's
(`ExternalTaskService.is_owner`; another user's press in a shared channel does and shows nothing), then the command's
own `confirm(user_id, code)`, and the command's own answer replaces the button (`send_refusal_reply` is shared): sent;
not found for a second press, a task the command already sent or a lapsed hold (the button goes); unavailable when the
flag is off or the account was locked after the press was authorised (the button stays). `confirm` is one store
transaction on the hold, so no number of presses sends a task twice. If the button cannot be posted, the command goes
alone. Telegram keeps the command only, and the command keeps working on Slack.

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
- **Only the owner's own chat turn and the owner's reminders reach the queue.** Smart offers the specialist's intent only
  on an interactive-delivery turn or a reminder's run (`run_origin` `background:execute_reminder`) that was not reached
  by delegation and whose user has the flag; Quick never sees it (the
  intent is internal). The specialist repeats the gate in code: the coordinator checks no allowlist, so a nested Smart
  or Lelik's `/voice/delegate` naming the intent gets a code-written refusal, no model call. The service re-checks flag
  and lock, so a stale cached profile gains nothing.
  A reminder's create goes straight to the queue with a tool note and no receipt (the owner approved that reminder when
  it was set), and its task's origin is the reminder's channel, so the completion alert lands where the reminder spoke.
  The daily review, a voice errand and the delivery's alert turn have no entry. The delivery's alert turn is a background run (`background:external_task_deliver`, no `interactive_delivery`), so a
  worker's result never starts new work.
- **A worker's text is wrapped where a model reads it through the queue.** `get_external_task` returns a result or a
  failure reason inside an outside-content wrapper after the reminder; a delivered result or reason is an ordinary
  workspace file under an outside-content header (no file-level mark).
- **Results never in a system slot on delivery.** The alert is code-written from code-made values; a worker's text and
  a failure reason reach the model only as the workspace file the alert names.
- **One refusal for the unknown.** A label that does not exist, another user's task and a missing task give the same
  tool error or `404`; nothing reveals existence.
- **`401` limits live in memory.** Per address (rightmost `X-Forwarded-For` entry) and global, held by the rate limiter
  over `WindowCounter`; the per-worker limits count calls, claims and submits. Logs name a worker by
  `fingerprint(worker_id)`, never a key, text, result or header.
- **Inactive user.** Refused at the Cabinet routes, the REST endpoint, `$task send` and the Send button (IAM at the
  click, then the service's own check in `confirm`).
- **A worker's description never reaches a model.** It is free text typed in the Cabinet; a tool or parameter
  description is read with the schema's authority, so the specialist's tools carry labels only (the `worker` enum and
  its lead line, the `list_external_workers` result, the `queue_state` block). The description is shown to the owner in the Cabinet and nowhere else.
- **Wake-up secrets are never shown back or logged.** The Cabinet gets the host only after a save; logs name the host
  and a code or an error class, never the URL, a header or the target's answer. httpx's own request log line is dropped
  while a send runs.

## Traps

- **Indexes first.** The queue's composite indexes in `config/firestore.indexes.json` must be `READY` before the code
  deploys (`make deploy-indexes`); a missing one fails at claim or list time, not at deploy.
- **The flag is operator-only.** `config.external_tasks_enabled` is set by `scripts/set_external_tasks_flag.py` (dry run
  unless `--apply`) or the console. No Cabinet route, command or API writes it. Smart's offer follows the flag within
  about an hour (the factory caches a profile); the specialist's gate and the service read it fresh. Switching off
  deletes nothing.
- **Upload the specialist's prompt before the deploy.** Profile `external_tasks`, blueprint `external_task_agent_v1`,
  `COGNITIVE_PROCESS_EXTERNAL_TASKS`, `OUTPUT_FORMAT_EXTERNAL_TASKS` and the `PROTOCOL_AGENT_SELECTION` clause ship by
  token upload; a deploy before them runs the specialist on an empty profile (`docs/how_to/NEW_AGENT_PLAYBOOK.md`).
- **A skill names the hand-off, not a tool.** Smart has no `create_external_task`; a skill that names a queue tool
  points at nothing Smart can call.
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
