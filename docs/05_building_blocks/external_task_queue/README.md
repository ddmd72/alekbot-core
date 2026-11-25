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
  src/domain/front_matter.py
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
  src/adapters/outbound_webhook/
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
  src/web/static/workers_help.html
-->
# External task queue

## Purpose

The hand-off from the bot to a worker the owner runs elsewhere (a script, a scheduled agent, an automation service). Smart,
through the task specialist, puts a free-text task into a per-user queue; a registered worker claims it, does the work
outside the bot and submits a text result; the bot stores the result as outside content and tells the owner in the chat
the task came from. The queue does not know what a task is about: the owner's skills decide when and to which worker
work is handed (`agent_skills_workspace`). It never runs the task, and a worker never talks to the owner directly.

Examples use the invented user Alex Doe, the worker labels `research` and `calendar-watch`, and the task "compare three
community-garden irrigation kits for Shelbyville".

## The whole path

```
 owner's chat turn ─┐                                      owner: receipt file (+ `$task send` / «Send» in a chat turn)
 reminder's run ────┴─► Smart ──intent──► ExternalTaskAgent ──create──► queue (held │ queued)
                                                                            │
                     wake ping {"event":"tasks_available"} ◄── wake task ◄──┤ (task became queued, worker idle)
                                    │                                       │
                                    ▼                                       │
          worker (key or OAuth grant) ── MCP /tasks-mcp or REST ──► list / claim / renew / submit / release / fail
                                                                            │
                                         submit / fail / expiry ──► external_task_deliver (Cloud Task)
                                                                            │
                            workspace file references/external_tasks/… ◄───┤
                                                                            ▼
                                      system alert (+ the task's note) ──► Smart, background run in the origin channel
                                                                            ▼
                                                                    owner reads Smart's answer
```

## Task lifecycle

Statuses: `src/domain/external_task.py::ExternalTaskStatus`; every transition is one store transaction applying a pure
rule of `src/domain/external_task_rules.py`; timers are `external_task_timer` Cloud Tasks
(`docs/07_deployment/SCHEDULERS.md`). Limits are the named constants of `src/domain/external_task.py`.

| Status | Entered by | Leaves to | Timer |
|---|---|---|---|
| `held` | a create in a chat turn: a confirmation code returned once (stored as a keyed digest), `hold_until` = now + `HOLD_S` | `queued` by `$task send` or the «Send» button; `cancelled`; `expired` by the hold timer, silently (no alert) | hold |
| `queued` | a create in a reminder's run; a confirm; a release; a lease timer on a lapsed lease | `leased` by a claim; `cancelled`; `expired`; `failed` (`claim_loop`) | deadline (enqueued at the reminder create or at the confirm) |
| `leased` | a claim: a fresh lease id, `until` = now + `LEASE_S`; a renew extends it by `LEASE_S` while the lease is live, at most `MAX_RENEWALS_PER_TASK` times | `done` (submit), `failed` (fail), `queued` (release, lease timer), re-claimed when lapsed, `cancelled`, `expired` | lease (one per claim and per renewal) |
| `done` `failed` `cancelled` `expired` | final; `purge_after` = now + `RETENTION_S` | nothing: only the `delivery` fields still change | — |

- **Deadline.** `deadline_at` = `created_at` + `TASK_MAX_AGE_S` for every task (a held task's clock starts at its
  creation, not at the confirm). The deadline timer expires any non-final task; a claim that meets a candidate past its
  deadline expires it instead of leasing it. A timer further out than `TIMER_MAX_DELAY_S` re-enqueues itself.
- **Claim.** The worker's oldest `queued` task, else its oldest lapsed lease (lazy recovery of a lost lease timer). A
  candidate whose `claims` reached `MAX_CLAIMS_PER_TASK` fails with `claim_loop`; a release counts through the claim
  before it. A held task is invisible to every worker operation (`WORKER_VISIBLE_STATUSES`).
- **Worker precondition** for renew, submit, release, fail: status `leased` and the presented lease id. A lapsed lease
  nobody re-claimed still accepts a submit; a renew of a lapsed lease is `lease_expired`. A second submit of the same
  body (same sha256) is an idempotent replay; another body after `done` is `already_submitted`.
- **Caps.** `MAX_OPEN_TASKS_PER_USER` (held tasks count), `MAX_WORKERS_PER_USER`, `MAX_KEYS_PER_WORKER`.
- **Which end gets an alert.** `done` (on submit and on every replay, dedup `deliver:<task_id>`), `failed` (a worker's
  fail, `claim_loop`), `expired` by the deadline. A cancelled task and an expired hold get none.

## The two entries

`task_entry` (`src/domain/external_task_entry.py`) reads the entry from the run's context in code, never from a model:
`CHAT` on the owner's interactive turn, `REMINDER` while one of the owner's reminders runs (`run_origin`
`background:execute_reminder`), otherwise none and no queue at all.

| | Chat turn (`TaskEntry.CHAT`) | Reminder's run (`TaskEntry.REMINDER`) |
|---|---|---|
| Created as | `held`, hold timer only | `queued`, deadline timer, a wake-up when due |
| Why | nothing leaves without the owner's confirmation, whatever the turn read | the owner approved the reminder when it was set |
| Owner sees | the receipt file `task-<id>.txt`, a literal line (worker, title, id), then the command `$task send <code>` as its own message; on Slack that message carries a «Send» button | the same file and a literal line that the task was handed over from a reminder (`EXTERNAL_TASK_QUEUED_FROM_REMINDER`); no command, no button |
| Posted by | `ConversationHandler` after the answer | `UserNotificationService.notify` after the reminder run's answer; not in history |
| Model reads | a tool note with the task id and the hold; never the code | a tool note that the task is queued |

**The receipt** (`src/domain/external_task_receipt.py`) is a literal notice (`src/domain/literal_notice.py`): YAML front
matter with the title, the worker label and the note, each one double-quoted line (`src/domain/front_matter.py`, so no
note text can end the block or add a key), a `---` line, then the task text byte for byte. It is what leaves the system,
shown before it leaves. If the receipt cannot be posted, the command is not sent.

**The Send button.** On Slack (`SEND_BUTTON_PLATFORMS`) the command message is an interactive message of the shared
mechanism, posted in the receipt's thread when it has one: binding `external_task_send:<task_id>`, the option id is the
code. A press goes `POST /slack/interactivity` → `interactive_choice` task → `InteractiveChoiceService` (IAM read fresh:
an unknown or locked user stops there) → `ExternalTaskSendChoice`: the task must be the clicker's (`is_owner`; another
user's press does and shows nothing), then the command's own `confirm(user_id, code)`, and its answer replaces the
button (`send_refusal_reply`, shared with the command). Not found (a second press, already sent, a lapsed hold) removes
the button; unavailable (flag off, account locked) keeps it. `confirm` is one transaction on the hold, so no number of
presses sends a task twice. Telegram has the command only.

**The brief, word for word.** The create handler refuses a text that is not a part of the specialist's instruction after
whitespace is normalised (`ERROR_NOT_THE_BRIEF`): the brief is Smart's; the specialist picks the title and the worker
label only.

## The note

A result returns hours later to a turn that has none of the conversation, so every task carries the bot's own return
instructions (decision record `external_task_confirmation_and_alerts.md`).

- **Who writes it.** Smart says what the owner asked and why, how to present the result and what comes next (the
  intent's capability line in `agent_manifest.py`); the specialist writes the required `note` argument of
  `create_external_task` from it. Capped at `NOTE_MAX_CHARS`, refused over it and never cut, normalised like every
  stored text; not checked against the instruction (it is the specialist's own text). Stored with the task's `entry`.
- **Where it is shown.** To the owner in the receipt's front matter, at creation, for both entries.
- **How the alert frames it.** A task with a note and an entry gets the alert framed like a fired reminder's
  (`src/domain/reminder_alert.py`): you handed this over earlier and wrote this note as your own instructions for now
  (per entry: the owner confirmed it with the brief / was shown it as the reminder handed the task over); the note as
  plain lines; after a `---` line, what happened, the worker's file marked as data only, what the note may drive for
  this outcome (done: act on it; done without a file: what waits for a chat turn; no result: only what the outcome
  means for the owner's request), and the turn's limits (no task queue, no memory proposal).
- **Never to a worker.** No worker surface carries it: not `ClaimedTask`, not `WorkerTaskSummary`, not REST, not MCP.
- **Never in saved history.** The history pair keeps the code-made `alert_summary` and the alert rendered without the
  note (`notify(..., system_alert_history=...)`), because the consolidator reads a user-role line as the owner's words.
- A task stored before the note existed has `note` and `entry` None and gets the plain alert.

## Surfaces

**The specialist** (`src/agents/external_task_agent.py`, intent `work_with_external_tasks`,
`decisions/external_task_specialist.md`). Smart has no queue tool. It offers the internal intent only when
`task_entry` gives an entry, the turn was not reached by delegation and the cached profile has the flag
(`smart_response_agent.py`); Quick never sees it. The specialist repeats the gate in code before any model call (an
entry, a call chain of itself alone, a service, a user; then the service's fresh flag and lock check) and answers a
refusal with one code-written line, so a nested Smart or the voice delegate naming the intent gets no model call. It
runs its own engine loop, delegates nothing, and its prompt carries no memory facts, directives or location; the
system prompt ends, after the cache boundary, with a `queue_state` block of worker labels. Its five tools
(`src/infrastructure/external_task_tools.py`): `create_external_task` (left out when no worker exists; the `worker`
enum is the labels), `list_external_tasks`, `get_external_task`, `cancel_external_task`, `list_external_workers`.
Its answer is composed by code from a record of every tool call (each result verbatim, then the model's short note,
then a line when the run was cut), so a run cut by its soft deadline, the turn cap or an exception after a create still
posts the receipt; it answers success whenever it got past the prompt build (no whole-run retry: it would create twice).

**Worker MCP** `/tasks-mcp` (`src/composition/tasks_mcp_setup.py`; beside the facts and drive connectors:
[`remote_mcp_server`](../remote_mcp_server/README.md)). Six tools over the same service operations as REST:
`list_tasks`, `claim_task`, `renew_lease`, `submit_task_result`, `release_task`, `fail_task`; none takes a user, worker
or label. Two credentials:
- a worker key as the bearer: `resolve_key` then `ExternalWorkerUserGate`, as REST, but every refusal is `401` (the
  SDK's verifier returns a token or nothing) and REST's `401` address limits are not applied;
- an OAuth access token of the tasks resource. A client must register the scope `external_tasks` (never a default, so an
  existing connector never gains it); the consent page (`src/web/mcp_consent_app.py`) lists the owner's active workers
  and Approve needs one, and the grant is bound to it (`worker_id` on the code and refresh token, `wid` in the access
  token). The flag is checked at consent, code exchange and every refresh; each tool call re-checks the grant, so a
  grant revoked in the Cabinet is refused on its next call.
Each call: the lock, the grant (OAuth), the worker's limits (the REST buckets, shared by keys and grants of one
worker), then the service. Answers are text from the tokens `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`; a claim
prints the task id, created time and lease and a "written by the user's assistant" line before the title and the brief.
A refusal is an `isError` result; no task is a success.

**Worker REST** `/external-tasks/v1/*` (`src/web/external_task_api.py`; routes in `docs/06_runtime/API_REFERENCE.md`).
Not under `/api/`, not behind the Cabinet cookie; a worker key in `Authorization: Bearer` only. Per request: the `401`
limits (`429` + `Retry-After`, the key store not read), the key's shape, `resolve_key` (`401` for a bad key, `403` for
locked, flag off or a revoked worker), `ExternalWorkerUserGate` (stored user active, with an email, whitelisted), the
worker's limits (`CALLS_PER_WINDOW`, plus `CLAIMS_PER_WINDOW` / `SUBMITS_PER_WINDOW`), then the body. Answers are
JSON codes; another worker's task, a held task and a missing task are one `404`.

**Cabinet card** "External workers" (`src/web/cabinet_external_workers.py`, UI in `cabinet-settings.js`; hidden for a
user without the flag). Register a worker (label + one-line description) and remove it (revokes its keys and OAuth
grants, cancels its open tasks); mint a key (shown once, stored as a keyed digest) and revoke one; list each worker's
OAuth grants (client name, granted, last refresh) with Revoke; set a wake-up target by pasting a provider's `curl`
command plus a separate secret, or clear it (the worker then polls). Labels and descriptions are not editable.

**Help page** `/cabinet/help/workers` (`src/web/static/workers_help.html`, linked from the card): the task's journey,
keys and the connector, the wake-up, several connections, recipes, troubleshooting, in plain language.

**`$task send <code>`** (`ConversationHandler`, the `task` command): a wrong, used or expired code gets one neutral line;
the service re-checks flag and lock.

## Wake-up

A worker with a target is pinged when it has work, instead of polling. `ExternalTaskService._wake_if_due` and
`_wake_for_next` decide; the ping is an `external_task_wake` Cloud Task; `on_wake` sends it.

| Pings | Does not ping |
|---|---|
| a reminder create, when the task is the worker's oldest queued one | a held task (a chat create), ever |
| a confirm (`$task send` or «Send»), when the task is the worker's oldest queued one | a create or confirm of a task that is not the worker's oldest queued one (an earlier task already pinged) |
| a lease timer that re-queues a lapsed task (oldest or not) | any task of the worker holding an unexpired lease (the worker is draining) |
| a submit, fail or release while the worker still has queued tasks: a ping for the oldest (a released task may be it) | a replayed submit |
| | a worker with no target, or not active; a lapsed lease recovered by a claim |

- **Generation.** Each ping moves the worker's `wake_generation` on (its own worker transaction, after the task write)
  and enqueues the task under dedup `external_task_wake:<worker_id>:<generation>`. `on_wake` re-reads the worker: a
  changed generation, another account, a cleared target, a removed worker or no queued work sends nothing. A crash
  between the two writes loses one ping; the task stays queued. A failed check or enqueue is logged, never raised.
- **What is sent.** The target is decrypted (AAD `external_workers/{user_id}:{worker_id}`) and POSTed
  `{"event": "tasks_available", "worker": <worker id>}`: no task text, title, label or count.
- **Outbound-request guard** (`src/adapters/outbound_webhook/guarded_webhook_adapter.py`, rules in
  `src/domain/wake_target.py`), the only code that sends to a user-entered URL, all within `WAKE_TIMEOUT_S`. Before any
  DNS query: `https`, port 443, no userinfo, a DNS name of two labels or more (no IP literal, no numeric form), not under
  `.internal`, `.localhost`, `.local` or `.arpa`, not one of the bot's own hosts (`wake_own_hosts`, built at boot from
  every deploy unit's configured URL) and not under `run.app`; header names are tokens, values printable ASCII, framing,
  hop-by-hop, `Content-Type` and `Accept-Encoding` refused, count and size capped. Then every resolved address must be
  public (no private, loopback, link-local or metadata, CGNAT, multicast, reserved, documentation, ULA, site-local, nor
  NAT64, 6to4 or Teredo ranges); the connection goes to the vetted address with the name as `Host` and SNI (no second
  resolution), no redirect is followed, no proxy is read from the environment, and the answer is asked uncompressed and
  read as raw bytes up to `WAKE_RESPONSE_READ_MAX`, then dropped. The Cabinet save runs the name-level rules; every send
  runs them all again.
- **Retries vs refusal** (`src/services/external_task_wake_task.py`). A 5xx or no answer in time (`server_error`,
  `unreachable`) answers 503 and Cloud Tasks retries, until the attempt count reaches `WAKE_MAX_ATTEMPTS`, which gives up
  silently. Any other `WakeRefusal` (a guard rule, a 3xx, a 4xx) is final: one literal notice to the origin channel of
  the worker's oldest queued task naming the label and the closed code; no agent turn, no history. The task stays
  queued either way, for polling or the next ping.
- **The form.** `src/domain/wake_curl.py` reads the pasted command: it starts with `curl`; only `-X POST`, `-H`, the data
  options (their value is ignored: the bot writes its own body), `--url` and a few output flags are read, and any other
  option is refused (`curl_option`), never skipped; a `Content-Type` header is dropped. A `$NAME` or `${NAME}`
  placeholder in the URL or a header is replaced by the secret: one name only, a placeholder needs a secret and a secret
  needs a placeholder. `PUT /api/external-workers/<id>/wake` also takes the older `{url, headers}` body.

## Delivery of results

`ExternalTaskDeliveryTask` (`src/services/external_task_delivery_task.py`) runs the `external_task_deliver` Cloud Task,
enqueued with the dispatch deadline `DELIVER_DEADLINE_S` (the alert is a Smart run).

1. **File.** A done task's result, or the reason a worker gave when it failed the task, is written as an ordinary
   workspace file `references/external_tasks/<date>-<short id>.md` (the full id when the short name holds a file of
   another provenance) below a code-written outside-content header naming the task, title, worker and time
   (`src/domain/external_task_delivery.py`). A workspace refusal (caps, size) or no workspace means no file; the text
   stays in the task record.
2. **Alert** (`src/domain/external_task_alert.py`). `UserNotificationService.notify` with kind `EXTERNAL_TASK`, Smart as
   the recipient, the task's origin channel, origin `external_task_deliver`: the alert is the user turn of a background
   Smart run and Smart's answer is what the owner reads. Alert kinds: `done` (file), `done_no_copy`, `failed_file`,
   `failed_no_file`, `claim_loop`, `expired`. It carries code-made values only (task id, label, file path, failure code,
   counts) plus the note; never the title, the result or a reason.
3. **History.** The pair is kept in that channel: the user line is `alert_summary` (`External task <id> for <label>
   ended: <outcome>; workspace:<path>`), its full text the alert without the note.
4. **Record.** `delivery.state` = `delivered`, so a second Cloud Tasks run is `already`. Every outcome of the turn is
   final, as for a reminder: no channel is logged, an empty answer is reported to ops, a failed turn is not run again.

## Owned data

Firestore `us-production`, env-prefixed names from `EnvironmentConfig`:

| Collection | Holds |
|---|---|
| `external_tasks` | one `ExternalTask`: status, `origin` (platform, channel, session), `note`, `entry`, `hold`, `lease`, `claims`, `result_meta`, `failure`, `delivery`, `deadline_at`, `purge_after` |
| `external_task_results` | the submitted text of a done task, apart so a listing never reads it |
| `external_workers` | doc `{user_id}:{worker_id}`: label, description, account, `wake_host` (clear), `wake_secret` (one `CipherPort` ciphertext of `{url, headers}`), `wake_generation` |
| `external_worker_keys` | doc id = the key's keyed digest; hint, worker, `last_used_at` (written at most every `KEY_TOUCH_S`), revocation |

Tasks and results carry `purge_after` (`RETENTION_S` after the final state) and are deleted by TTL policy
(`config/ttl_registry.json`); an open task has none. Per-worker rate windows are `WindowCounter` documents
(`xtrate:<key>:<window>`) in the `worker_task_dedup` collection, shared by every instance; the `401` counters are in
each instance's memory only.

## Security rules enforced in code

- **A chat create always waits** for the owner's `$task send` or «Send», whatever the turn read; only a reminder's run
  queues at once. There is no per-turn taint tracking (`external_task_confirmation_and_alerts.md`).
- **Only the owner's own chat turn and the owner's reminders reach the queue.** The daily review, a voice errand, a
  delegated Smart and the delivery's alert turn (`background:external_task_deliver`) have no entry, so a worker's result
  never starts new work. The service re-checks flag and lock on every owner, worker and registry call.
- **A worker's text is wrapped wherever a model reads it.** `get_external_task` returns a result or reason inside an
  outside-content wrapper after the reminder; a delivered one is a workspace file under the outside-content header,
  named by the alert, never inlined.
- **The note never reaches a worker or saved history** (see The note).
- **A worker's description never reaches a model.** It is free text from the Cabinet, and a tool or parameter
  description reads with the schema's authority, so the specialist's tools carry labels only.
- **One refusal for the unknown.** A label that does not exist, another user's task and a missing task give the same
  tool error or `404`.
- **Inactive user** refused at the Cabinet routes (`423`), REST, MCP (every call), `$task send`, the Send button (IAM at
  the click, then `confirm`) and `/worker` (timer, delivery and wake bodies name the user).
- **Keys and codes are stored as keyed digests**; a raw key is shown once. Logs name a worker by
  `fingerprint(worker_id)`, a user by 8 characters; never a key, code, text, title, note, result, reason, URL or header.
- **Wake-up secrets are never shown back or logged.** The Cabinet gets the host only; httpx's request log line is
  dropped while a send runs.

## Traps

- **Indexes first.** The queue's composite indexes in `config/firestore.indexes.json` must be `READY` before the code
  deploys; a missing one fails at claim or list time.
- **The flag is operator-only.** `config.external_tasks_enabled` is set by `scripts/set_external_tasks_flag.py` (dry run
  unless `--apply`) or the console; no route, command or API writes it. Smart's offer follows the flag within the agent
  factory's profile cache (about an hour); the specialist, the service and OAuth read it fresh. Switching off deletes
  nothing: open tasks are not served and run to their deadlines.
- **Upload before the deploy.** The specialist's profile `external_tasks`, blueprint `external_task_agent_v1`,
  `COGNITIVE_PROCESS_EXTERNAL_TASKS` (it says how to write the note), `OUTPUT_FORMAT_EXTERNAL_TASKS` and the
  `PROTOCOL_AGENT_SELECTION` clause ship by token upload, as do `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`
  (`prompts_snapshot/tokens/system/`, `firestore_utils/snapshot_upload.py`). Without the MCP texts `/tasks-mcp` is not
  built and answers 404 (one ops alert); without the prompt the specialist runs on an empty profile. The tool texts in
  `src/domain/external_task_texts.py` ship by code deploy.
- **A skill names the hand-off, not a tool.** Smart has no `create_external_task`.
- **A new collection of this block needs a TTL registry entry**; a test scans for `purge_after` writers.
- **Limits are placeholders** set at implementation review, none measured: change the constant, and the help page's
  numbers with it.
- **The guard assumes no VPC egress** on the main service; a VPC connector would let a wake-up URL reach a private
  network (`docs/07_deployment/README.md`, "Main service egress").
- **A worker on the platform's default domain cannot be woken** (every name under `run.app` is refused); it needs a
  custom domain, or polls.
- **Not a place for a worker's secrets.** A task text is plain stored text, capped to fit one receipt.
- **Not built:** the drive Inbox destination (`WorkerDestination.DRIVE` is a placeholder; a worker registered with it is refused).

## Where things live

| File | Responsibility |
|---|---|
| `src/services/external_task_service.py` | every rule applied: create, confirm, claim, renew, submit, release, fail, cancel, timers, wake decisions, `on_wake`, worker registry |
| `src/domain/external_task.py`, `src/domain/external_task_rules.py` | the record, statuses, limits; the pure transitions |
| `src/domain/external_task_entry.py` | chat vs reminder entry |
| `src/domain/external_task_jobs.py` | timer, deliver and wake payloads and dedup keys |
| `src/domain/external_task_text.py` | normalisation and caps of every stored text |
| `src/agents/external_task_agent.py`, `src/infrastructure/external_task_tools.py`, `src/domain/external_task_texts.py` | the specialist, its five tools, their texts |
| `src/domain/external_task_receipt.py`, `src/services/external_task_send_choice.py` | the receipt, the command and the Send button |
| `src/services/external_task_delivery_task.py`, `src/domain/external_task_delivery.py`, `src/domain/external_task_alert.py` | delivery: file, alert, history label |
| `src/services/external_task_timer_task.py`, `src/services/external_task_wake_task.py` | `/worker` runners of timers and wake-ups |
| `src/domain/wake_target.py`, `src/domain/wake_curl.py`, `src/adapters/outbound_webhook/guarded_webhook_adapter.py` | target rules, the pasted command, the guarded send |
| `src/web/external_task_api.py`, `src/services/external_task_rate_limiter.py`, `src/services/external_worker_user_gate.py` | REST, limits, whitelist gate |
| `src/composition/tasks_mcp_setup.py` | the MCP resource and its credentials |
| `src/web/cabinet_external_workers.py`, `src/web/static/workers_help.html` | Cabinet card routes, help page |
| `src/composition/external_task_setup.py`, `src/composition/external_task_api_setup.py` | wiring, own hosts, limiter |

## Links

`docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md`; decision records `external_task_specialist.md`,
`external_task_confirmation_and_alerts.md` (in `docs/04_solution_strategy/decisions/`);
`docs/07_deployment/SCHEDULERS.md` (timer, deliver and wake task types); `docs/06_runtime/API_REFERENCE.md` (worker
routes); [`remote_mcp_server`](../remote_mcp_server/README.md) (OAuth and `/tasks-mcp`);
`docs/05_building_blocks/agent_skills_workspace/README.md` (the workspace results land in); help page
`src/web/static/workers_help.html`.
