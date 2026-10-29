# Cloud Scheduler Reference

All Cloud Scheduler jobs for Alek-Core. Managed via `cloudbuild-dev.yaml` using the `describe && update || create` pattern (idempotent on every deploy).

Region: `us-central1`. Attempt deadline: `60s` for most `/worker` jobs; `start_daily_email_review` and
`start_drive_sync` use `180s`, `fact_lifecycle_sweep` uses `180s` and `repair_email_embeddings` uses `300s` (`cloudbuild-dev.yaml`).

## Two deadlines, do not confuse them

The Scheduler attempt deadline above bounds only the **fan-out** call — the tick that
claims due work and enqueues Cloud Tasks. It usually returns in seconds; `start_drive_sync`
may post a reconnect notice through the formatter agent (hence 180s).

The work itself runs under the **Cloud Tasks `dispatch_deadline`** of the task the tick
enqueued, and that is a different number with a different failure mode. Left unset,
Cloud Tasks applies **600s**. Any in-process budget above it is fiction: the dispatch is
cut mid-run, the cut reads as a task failure, and the queue retries the entire run.

Task types whose `NotificationSLA` can exceed 10 minutes must therefore pass
`deadline_seconds` to `enqueue_worker_task`:

| Task type | SLA ceiling | dispatch_deadline |
|-----------|-------------|-------------------|
| `execute_reminder` | 1500s (REMINDER × PERFORMANCE) | 1620s |
| `daily_email_review` | 1500s (DAILY_DIGEST) | 1620s |
| `drive_sync` | no new work after 8 min (`TICK_START_BUDGET`) | 1800s (`TICK_DEADLINE_S`) |
| `fact_review` (the janitor's enqueue and the review's own deferral) | one review pass (a model loop under the memory-write lease) | 1800s (`REVIEW_DEADLINE_S`); the `next` send released by an answer sets none (no model call) |
| everything else | ≤ 600s | Cloud Tasks default |

The value is derived, not hardcoded: `dispatch_deadline_s(kind)` in
`src/infrastructure/notification_sla.py` takes the worst case across the kind's tier
overrides and adds 2 minutes of handler headroom, clamped to the Cloud Tasks maximum of
1800s (which is also this service's Cloud Run request timeout). Raising a budget in
`NOTIFICATION_SLA` therefore raises its transport automatically.

`drive_sync` is the exception: it is not a notification kind, so every enqueue (fan-out, continuation, nudge,
toggle) passes `TICK_DEADLINE_S` directly, on its own queue (see Start Drive Sync below). A manual
`gcloud tasks create-http-task` sets no deadline and gets the 600s default.

Both budgets were unreachable from the day they were written until 2026-08-15 — see
`docs/04_solution_strategy/decisions/grok_revival_2026_08.md`.

---

## Jobs

### Keep-Alive (dev only)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-keep-alive` |
| **Schedule** | `*/10 * * * *` (every 10 min) |
| **HTTP** | `GET /health` |
| **Purpose** | Prevents cold starts on the dev instance (min-instances=0) |
| **Env** | dev only — prod uses `min-instances=0` and accepts cold starts |

---

### Voice Relay Keep-Alive (dev)

| Field | Value |
|-------|-------|
| **Job name** | `alek-voice-relay-dev-keep-alive` |
| **Schedule** | `*/10 * * * *` (every 10 min) |
| **HTTP** | `POST /voice/relay-warmup` on the **main** service, OIDC (the invoker identity from `SERVICE_ACCOUNT_EMAIL`, audience = the route URL), empty body. Build-managed in `cloudbuild-dev.yaml` with the other jobs. |
| **Purpose** | Keeps the voice relay warm. Its cold start (up to ~67 s) outlasts Cloudflare's ~8 s WebSocket handshake, so the first web call after idle failed. |
| **Why not the relay directly** | Scheduler's request carries a `Content-Length`, which the relay's `websockets` parser rejects before any handler (503 + ERROR). The main route sends a plain GET instead and awaits it. |
| **Handler** | `voice_control_plane_app.warm_relay` → `main._relay_warmup` |

---

### Fire Due Reminders

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{env}-fire-due-reminders` |
| **Schedule** | `*/5 * * * *` (every 5 min) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "fire_due_reminders"}` |
| **Purpose** | Fires proactive self-reminders whose `due <= now`. Idempotency guard: skips notes fired within the last 4 min. |
| **Handler** | `WorkerHandler._handle_fire_due_reminders()` |
| **Cadence is a contract** | The 5-min cadence is the floor for sub-hourly rules (`FREQ=MINUTELY;INTERVAL≥5`, `_MIN_MINUTELY_INTERVAL` in `dateutil_recurrence_adapter.py`). Change both together. Missed occurrences are skipped, not replayed. |
| **Env** | dev + prod |

---

### Renew Task Subscriptions (Microsoft To Do)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{env}-renew-task-subscriptions` |
| **Schedule** | `0 2 * * *` (daily at 02:00 UTC) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "renew_all_task_subscriptions"}` |
| **Purpose** | Fan-out: enqueues `renew_task_subscriptions` Cloud Task for every user with MS To Do connected. Microsoft webhook subscriptions expire; this keeps them alive. |
| **Handler** | `WorkerHandler._handle_renew_all_task_subscriptions()` |
| **Env** | dev + prod |

---

### Start Email Indexing (Gmail auto-index)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{env}-start-email-indexing` |
| **Schedule** | `7 * * * *` (hourly at :07 — staggered off :00 so the hourly fan-outs do not collide with each other, `fire-due-reminders` and the keep-alives; prod log audit C-01) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "start_email_indexing"}` |
| **Purpose** | Fan-out: for every Gmail user with `config.gmail_auto_index=True`, checks if `current_hour_in_user_tz == config.gmail_auto_index_hour`. If yes, creates an incremental indexing job and enqueues it. Skips users with a job already running. |
| **Handler** | `WorkerHandler._handle_start_email_indexing()` |
| **User setting** | Toggle + hour picker in Cabinet UI > Integrations > Gmail |
| **Env** | dev + prod |

---

### Daily Email Review

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{env}-start-daily-email-review` |
| **Schedule** | `12 * * * *` (hourly at :12 — staggered, see Start Email Indexing) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "start_daily_email_review"}` |
| **Purpose** | Fan-out: for every Gmail user with `config.gmail_daily_review=True`, finds the review slot open at this tick: the first 3 hours from `config.gmail_daily_review_hour` in the user's timezone (wraps midnight; an invalid timezone counts as UTC). It claims the mark `daily_review:{user}:{slot_date}` (48 h) so the review runs once per local date, then enqueues a `daily_email_review` Cloud Task named after the mark. Worker fetches last 24h emails (up to 200, full body via BS4 HTML→text + invisible char stripping) and passes structured JSON to SmartAgent via `notify(save_history=False)`. SmartAgent runs Phase 0 triage → Phase 1 deep reads (`get_email_details`) → Phase 2 research (`search_web`) → delivers HTML report (GCS link) + short chat message. After HTML delivery, URL is saved to session history via `notify_document_link()`. |
| **Handler** | `WorkerHandler._handle_start_daily_email_review()` → `WorkerHandler._handle_daily_email_review()` |
| **User setting** | Toggle + hour picker in Cabinet UI > Integrations > Gmail |
| **Env** | dev + prod |

---

### Email Indexing Watchdog

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-email-indexing-watchdog` |
| **Schedule** | `27 */2 * * *` (every 2 hours at :27 — staggered off the `3-59/10` drive sync fan-out and the other hourly jobs) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "email_indexing_watchdog"}` |
| **Purpose** | Scans all `RUNNING` indexing jobs older than 2 hours and marks them `FAILED`. Handles Cloud Tasks timeouts and crash-recovery. |
| **Handler** | `WorkerHandler._handle_watchdog()` |
| **Env** | dev + prod |

---

### Reconnect Notice Sweep (R1)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-reconnect-notice-sweep` |
| **Schedule** | `37 * * * *` (hourly at :37 — collides with no other job) |
| **HTTP** | `POST /worker` on the main service, OIDC (worker SA) |
| **Payload** | `{"task_type": "reconnect_notice_sweep"}` |
| **Attempt deadline** | `180s` — one pass may post notices through the formatter agent |
| **Purpose** | Lists users from the feature stores (Gmail indexing states, To Do configs), not from the credential store. For each whose feature state shows a past connection and has no credential, and who did not disconnect on purpose, sends the one reconnect notice and raises a flag on the feature state; a credential that is back lowers it. Drive's notice runs in the drive fan-out below. |
| **Handler** | `WorkerHandler._handle_reconnect_notice_sweep()` → `IntegrationReconnectService.sweep()` |
| **Unconfigured** | Answers `200 {"status": "disabled"}`, not 5xx (an ERROR request log would page every hour). |
| **Decision** | `docs/04_solution_strategy/decisions/reconnect_notice_r1.md` |

---

### Start Drive Sync (drive index fan-out)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-start-drive-sync` |
| **Schedule** | `3-59/10 * * * *` (every 10 min = `TICK_INTERVAL`, at :03, :13, … — staggered off the `*/10` keep-alive, the `*/5` reminders and the hourly jobs at :02/:07/:12/:17, and inside each 10-minute `tick_key` bucket rather than on its boundary) |
| **HTTP** | `POST /worker` on the **main** service, OIDC (worker SA) |
| **Payload** | `{"task_type": "start_drive_sync"}` |
| **Attempt deadline** | `180s` — one pass may post a reconnect notice through the formatter agent |
| **Purpose** | Fan-out: one `drive_sync` Cloud Task per user with indexing on, **or** with a stored `DriveSyncState` (indexing off, credentials gone, user deleted), so an orphaned index always gets purged. Candidates = users with drive credentials ∪ users with a sync state. Skips a user whose continuation was enqueued less than `TICK_DEADLINE_S` (1800 s) ago. Task key `{user_id}:tick:{10-min bucket}`, so a scheduler retry in the same bucket is a no-op. After every tick is enqueued, a second pass raises the staleness alert (ops Slack, once per incident, §12.1) and the one reconnect notice to the user (§7.7, RFC Δ6, retried on a later fan-out only when no send was attempted). It writes those two flags, and a minimal sync state (`created_at` only) for a user with indexing on, a working grant and no state, so a user whose ticks never land is alerted too (RFC Δ28). The queue holds about one task per user plus nudges **while it drains**; while it is stuck (indexer down, queue paused) each fan-out adds one task per user under a new bucket key, and after recovery the backlog runs as cheap no-op ticks (staleness alerts meanwhile). |
| **Handler** | `WorkerHandler._handle_start_drive_sync()` → `DriveSyncSchedulerService.fan_out()` |
| **Unconfigured** | Without `DRIVE_INDEXER_URL` (first pass of the two-pass first deploy) it answers `200 {"status": "disabled"}` — not 501, which would be an ERROR request log every 10 min. |
| **RFC** | `docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §7.1, §12.1, §12.5 |

The ticks themselves do not run on the main service. They go to a dedicated queue and a dedicated
service:

| Field | Value |
|-------|-------|
| **Queue** | `drive-sync-dev` (Cloud Tasks, `us-central1`), provisioned in `cloudbuild-dev.yaml` with `describe → update \|\| create` |
| **Concurrency** | `max-concurrent-dispatches=1`, `max-dispatches-per-second=1` — a CPU/memory knob for the indexer instance; the per-user Firestore lease is the correctness mechanism (§7.1). Raising it is an extension (§14). The app never calls `create_queue_if_not_exists` on it (that would create it with concurrency 5). |
| **Retry** | `max-attempts=3`, backoff 60 s → 600 s. Only an unhandled exception answers 500; every handled tick outcome is 200 and the tick enqueues its own continuation. |
| **Target** | `alek-bot-indexer-dev` `POST /worker` (`indexer_main.py`): `--ingress=internal` (Cloud Tasks in the same project counts as internal), plus the in-app OIDC check with the audience pinned to the service URL, failing closed on Cloud Run when no accepted identity is configured (`DRIVE_INVOKER_SA_EMAIL`, else `SERVICE_ACCOUNT_EMAIL`, plus the optional comma list `OIDC_TRANSITION_SA_EMAILS`; RFC Δ21). A manual `drive_sync` goes through the queue: `gcloud tasks create-http-task --queue=drive-sync-dev --location=us-central1 --url=<indexer URL>/worker --oidc-service-account-email=<SA> --body-content='{"task_type":"drive_sync","user_id":"<id>"}' --header=Content-Type:application/json`. It gets the 600 s default deadline, not 1800 s. `<indexer URL>` must be the URL in `INDEXER_URL_DEV`: the audience is pinned, so another run.app URL form of the same service gets 401. |
| **IAM prerequisite** | `cloudbuild-dev.yaml` creates and updates the queue, so the identity the build runs as needs `roles/cloudtasks.queueAdmin` on this queue (granted once by the owner, to that identity, not to a runtime SA). The tasks' OIDC identity is `DRIVE_INVOKER_SA_EMAIL` when that secret is set, else `SERVICE_ACCOUNT_EMAIL`; the enqueuing runtime needs `actAs` on it. |
| **dispatch_deadline** | 1800 s (`TICK_DEADLINE_S`) on every `drive_sync` enqueue: fan-out, continuation, nudge, toggle |
| **Producers** | the fan-out above; `UserDriveService` nudges after each bot mutation (`{user_id}:nudge:{mutation_id}`, re-enqueued as `…:r{n}` on a busy lease); the Cabinet toggle (`{user_id}:toggle:{ts}`); the indexer's own continuations (`{user_id}:cont:{tick_id}`, enqueued after the tick releases its lease) and busy retries (`{user_id}:cont:busy-{holder}`, once per lease holder; RFC Δ27) |
| **Deploy order** | `cloudbuild-dev.yaml` creates the queue and deploys the indexer and its IAM step after the main service, so a failing drive step never blocks a fix of the bot (RFC Δ43). `make deploy` refuses an empty `INDEXER_URL_DEV` unless `DRIVE_FIRST_PASS=1` (the first pass of the two-pass first deploy, [README.md](README.md) § Indexer URL). |

---

## Adding a New Scheduler Job

Pattern used in all jobs:

```bash
gcloud scheduler jobs describe {JOB_NAME} \
  --location=us-central1 --project=$PROJECT_ID &>/dev/null \
&& gcloud scheduler jobs update http {JOB_NAME} \
  --schedule="..." \
  --uri="$_SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"..."}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http {JOB_NAME} \
  --schedule="..." \
  --uri="$_SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"..."}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID
```

Note: `update` uses `--update-headers`, `create` uses `--headers`. This is a `gcloud` API difference.

---

## Deep Research Failed (task, no schedule)

| Field | Value |
|-------|-------|
| **Payload** | `{"task_type": "deep_research_failed", "user_id", "account_id", "session_id", "origin_platform", "query", "reason"}` |
| **Enqueued by** | `job_main.py` on an agent failure, an empty result or a failed delivery (`reason`: `agent_failed` / `empty_result` / `delivery_failed`), with `dedup_key` `deep_research_failed:<job_id>` |
| **Handler** | `WorkerHandler._handle_deep_research_failed()`: tells the user in the origin channel; 400 without user/account, 500 only when the notice was not sent at all (Cloud Tasks retries); a sent notice is never resent. `reason=delivery_failed` uses the "report could not be prepared" wording |
| **Residual** | A job killed by its task timeout or by the OS (out of memory, SIGKILL) sends nothing |

---

## Deep Research Webhook Event (task, no schedule)

| Field | Value |
|-------|-------|
| **Payload** | `{"task_type": "deep_research_webhook_event", "event_type", "job_id", "user_id", "account_id", "session_id", "origin_platform", "query"}` (no report text) |
| **Enqueued by** | `POST /webhooks/openai/deep-research` after the signature check, the 72 h job claim `openai_deep_research:<job_id>` and the empty-user filter, with `dedup_key` `<job_id>`; an enqueue failure releases the claim and answers 500 |
| **Handler** | `WorkerHandler._handle_deep_research_webhook_event()`: re-reads the response by job id, then delivers the report (`response.completed`) or sends one notice with the query (`failed` / `incomplete` / `cancelled`). 400 on a missing field or unknown event, 422 without OpenAI retrieval (no retry), 500 on a retrieval failure or a notice that was not sent at all (Cloud Tasks retries); a report task that could not be enqueued is 200 (the user was already told) |
| **Residuals** | Local dev without `OPENAI_API_KEY` cannot deliver webhook events (422). An event that exhausts its Cloud Tasks retries (retrieval keeps failing) loses the report silently: OpenAI was already acked |
| **Report task** | The HTML report agent task has the dedup key `deep_research_report:<job_id>` (also from `job_main.py`); its Cloud Tasks name is `agent_execution-<sha256(key)[:32]>`, so a retried create is idempotent; `deliver_deep_research` retries the create by that name twice more (0.5 s, 1 s), `AlreadyExists` counts as handed off, and only then tells the user |

---

## Cost

Each scheduler job invocation costs ~$0.10/month per job (first 3 jobs free).
Current active jobs: 4 (prod) / 5 (dev with keep-alive).

---

### Billing Daily Summary

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-billing-daily-summary` |
| **Schedule** | `0 9 * * *` (daily at 09:00 Europe/Madrid, DST-aware) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "billing_daily_summary"}` |
| **Purpose** | Sends a daily billing report to each account owner with activity today. Shows daily / monthly / total token consumption and cost per account. Skips accounts with zero daily usage. |
| **Handler** | `WorkerHandler._handle_billing_daily_summary()` |
| **Env** | active on the single live service |

---

### Fact Lifecycle Sweep (the fact janitor)

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-fact-lifecycle-sweep` |
| **Schedule** | `30 9 * * *` (daily at 09:30 Europe/Madrid, DST-aware; after the 09:00 billing summary) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "fact_lifecycle_sweep"}` |
| **Attempt deadline** | `180s`; **no retry on purpose** (the job sets no `--max-retry-attempts`) |
| **Purpose** | The fact janitor (`docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D4, owner's map `docs/05_building_blocks/memory/README.md` §5.5): moves every current fact whose `expires_at` has passed to `stale`, unless the account's breaker is tripped, at most `50 − backlog` per account per run (oldest expiry first, the rest `deferred`). Zero LLM calls. Rebuilds the baseline cache of every account it changed. Stores one snapshot per account per day and posts one 🧹 ops line a day. The line's backlog age counts from `stale_since`, so the two legacy stale facts that step 6a of the S3 rollout stamps show `oldest 0d` on the stamp day and count up from there, although they have been stale for months by `last_updated`. **Since S4** it also: takes a run guard first; visits every account the memory review has something to do with; leaves the due facts of open review questions alone; counts the *pool* (stale facts plus the facts of open questions) for the cap `50 − pool` and the breaker (the review interval + 72 h without a successful review, or a pool over 50); enqueues the account's `fact_review` task (below); raises the review's alerts; and adds the review's figures to the 🧹 line (owner's map §5.5, §5.6). |
| **Handler** | `WorkerHandler._handle_fact_lifecycle_sweep()` → `FactLifecycleSweepService.sweep()`; the totals are the 200 body (`accounts`, `due`, `staled`, `would_stale`, `deferred`, `conflicts`, `no_decision`, `breaker_tripped`, `review_enqueued`, `purge_overdue`, `dry_run`), or `{"status": "busy"}` when another run holds the guard |
| **Log-only** | Unless the main service has `FACT_LIFECYCLE_SWEEP_DRY_RUN` set to an explicit off value (`0`, `false`, `no`, `off`) the run writes nothing to facts or the cache and counts `would_stale` instead. The deploy sets it to `0`, so the janitor has written since the deploy of 2026-10-27 (owner's word; S5 classify applied the same day). Rollback: remove the variable (a deploy; `--set-env-vars` replaces every variable). |
| **Index** | needs the composite `(state ASC, expires_at ASC)` on the facts collection (`config/firestore.indexes.json`), `READY` before the code ships; without it the due query fails and the run answers 500. The review's "set aside lately" read needs `(account_id ASC, state ASC, valid_to DESC)` on the same collection; the review store's own queries need no composite. |
| **Env** | active on the single live service |

Failure handling: a failure other than a write conflict stops the run, is logged with its type and the stage reached, posts one
"🚨 Fact lifecycle run failed" to the ops sink (every ops post is capped at 10 s) and answers 500. Scheduler does not retry it:
a retry after the 180 s deadline would run beside a run still going. Since S4 a **run guard** (`{prefix}fact_lifecycle_run`, one
document held for 5 minutes, taken in a transaction before anything is read) makes an overlapping run harmless: it answers 200
`busy` and does nothing, so a hand run during the scheduled one is safe. A hand run at another time is safe too: the last run of
the day replaces the day's snapshot, and the 🧹 line is posted once per **UTC day**, so a hand run before 09:30
Europe/Madrid on the same UTC day takes the day's line and the scheduled run then posts none (its numbers are in the snapshot). Unlike
the fan-out jobs under "Two deadlines", the sweep does its work inside the Scheduler's own request: a run past 180 s shows as a
failed attempt in Scheduler while it goes on. The next day's run catches up after a failure. The job takes no memory-write lease and
is not a Cloud Task, so there is no task-claim record. The collections it writes
(`{prefix}fact_lifecycle_daily`, `{prefix}fact_lifecycle_lines`, `{prefix}fact_lifecycle_run`) carry no field named `expires_at` and **no TTL policy**.

#### Enqueue-only task types of the memory review (Facts S4)

Neither has a Scheduler job; both are Cloud Tasks on the main queue, delivered to `/worker` like any task and claimed by task name
(`decisions/worker_task_dedup.md`). Cloud Tasks keeps a task name only while the task is queued or running and for about an hour
after it ran, so a name merges tasks created close together; it does not make anything happen once a day.

| Task type | Enqueued by | Payload | Name (dedup key) | Handler |
|-----------|-------------|---------|------------------|---------|
| `fact_review` | the janitor, when `review_due` names a reason (stale facts and the interval elapsed, a reply in words waiting, a question past its timeout, an unfinished send, the digest due, or a queued question that may go out); the review itself, to defer when the memory-write lease is taken (120 s later); `FactAnswerService`, to send the next question after the current one closes | `{user_id, account_id, mode}`; `mode` is `daily` (a whole run) or `next` (only the send of the next question; no lease, no model) | `fact-review:<account>:<owner's local date>` (janitor); `fact-review-defer:<account>:<120 s bucket>` (deferral); `fact-review-next:<question id>` (release) | `WorkerHandler._handle_fact_review()` → `FactReviewService.run(user_id, account_id, mode)`; every answer is 200 (`completed`, `deferred`, `failed`, `not_owner`); a lease, store or queue error raises (5xx, Cloud Tasks retries); a mode that is neither is 400 |
| `interactive_choice` | the Slack interactivity route (`POST /slack/interactivity`), after it has verified the Slack signature on the raw body; one task per button press | the choice: `{binding {kind, id}, option_id, platform, platform_user_id, message_ref, at}` | `interactive_choice-` + a hash of `action_ts:user:block` (a repeated press of the same action is one task) | `WorkerHandler._handle_interactive_choice()` → `InteractiveChoiceService.handle(choice)`: the user is resolved through IAM (an unknown one is `unknown_user`), the handler is picked by the binding's kind (only `fact_question`), then the message is re-rendered; a payload that is not a choice is 400 |

Without the service wired, `/worker` answers `fact_review` or `interactive_choice` with 501.

---

### Email Embedding Repair

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{dev,prod}-repair-email-embeddings` |
| **Schedule** | `2 * * * *` (hourly at :02 — staggered; still within ≤1h of any `start_email_indexing` tick, so failures from an indexing tick are detected within ≤1h) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "repair_email_embeddings"}` |
| **Purpose** | Re-embeds `IndexedEmail` docs where `embedding_pending=True` (set on transient embedding failures during initial indexing). Without this job those emails stay invisible to `find_nearest` search forever. |
| **Handler** | `WorkerHandler._handle_repair_email_embeddings()` |
| **Batch cap** | `EmailEmbeddingRepairService.batch_size = 100` per run (cross-user). When the batch saturates, the handler re-enqueues another tick immediately via `enqueue_worker_task` — queue drains on demand without waiting for the next scheduler interval (same pattern as consolidation and email indexing). |
| **Env** | dev + prod |
| **RFC** | `docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md` §2.5 |

---

### Sweep Stuck Consolidation

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{dev,prod}-sweep-consolidation` |
| **Schedule** | `17 * * * *` (hourly at :17 — staggered, see Start Email Indexing) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "sweep_consolidation"}` |
| **Purpose** | Re-triggers consolidation for every user with a batch still in the queue. Consolidation is otherwise driven only by session overflow and the `$consolidate` command, so a batch that stalls (e.g. provider billing exhaustion exhausts its 3 attempts → `FAILED`, and the re-enqueue chain stops) would wait for the next overflow — potentially days. That batch's messages were already extracted from session history but not yet written to memory: a data hole between history and memory. This sweep closes it within ≤1h. |
| **Handler** | `WorkerHandler._handle_sweep_consolidation()` → `ConsolidationService.find_stuck_users()` → per user `enqueue_consolidation_task` (reuses the normal consolidation path; `reset_recoverable_batches` revives `FAILED`/zombie batches). |
| **Fan-out** | One distinct query (`FirestoreConsolidationQueue.get_stuck_batch_user_ids`, `user_id` projection) → one `consolidation` Cloud Task per stuck user. No-op when the queue is empty. |
| **Env** | dev + prod |

**The `consolidation` task may defer itself (Facts S2).** Every batch runs under the account's memory-write lease. A task
that finds the lease taken, while the user has an open batch (`pending`, `retry_pending` or `processing`), enqueues one
more `consolidation` task with a 120 s delay and a 1800 s deadline through `enqueue_worker_task`, named from the key
`consolidation-defer:<user>:<120 s bucket>` (Cloud Tasks name `consolidation-<sha256(key)[:32]>`), and answers 200
without enqueueing the next batch itself. Tasks that lose in the same bucket collapse into one task. The name is never
fixed, because Cloud Tasks keeps a name for about an hour. The sweep takes no lease: it revives `failed` batches and
zombies older than 1800 s (under the lease the zombie threshold is 60 s). A zombie reset counts the lost run on both paths (`attempts` + 1, `failed` at 3, then the sweep's bounded revivals), so a batch that kills its worker ends `failed` instead of looping. See memory map §3 step 3.

---

### Companion Extraction (RFC `docs/10_rfcs/COMPANION_AGENTS_RFC.md` §6)

Session-keyed analog of consolidation: `companion_consolidation` processes one companion
extraction batch and re-enqueues itself while more remain (`WorkerHandler._handle_companion_consolidation()`
→ `CompanionExtractionService.process_session_batches()`), mirroring `consolidation` above but keyed
by `session_id` instead of `user_id`. Driven by `TutorExtractorAgent` (RFC §6, Phase D) via
`CompanionExtractorRunner`.

`sweep_companion_consolidation` mirrors `sweep_consolidation`: it re-triggers extraction for every
session with a batch stuck in the queue (`WorkerHandler._handle_sweep_companion_consolidation()` →
`CompanionExtractionService.find_stuck_sessions()`). A Cloud Scheduler job
(`alek-bot-dev-sweep-companion-consolidation`, hourly at :47, created by `cloudbuild_scheduler_jobs.sh`)
runs it, so a batch that exhausts its 3 retry attempts (`BatchStatus.FAILED`) is revived within about
an hour. It can also be triggered manually (`POST /worker {"task_type": "sweep_companion_consolidation"}`).

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-dev-sweep-companion-consolidation` (sweep only; `companion_consolidation` is enqueued as a Cloud Task) |
| **Schedule** | `47 * * * *` (hourly at :47, sweep) |
| **Payload** | `{"task_type": "companion_consolidation", "session_id": "..."}` / `{"task_type": "sweep_companion_consolidation"}` |
| **Handler** | `WorkerHandler._handle_companion_consolidation()` / `_handle_sweep_companion_consolidation()` |
| **Env** | dev + prod |

---

**Last Updated:** 2026-10-26
