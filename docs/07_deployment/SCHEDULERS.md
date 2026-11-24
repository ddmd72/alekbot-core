<!-- living-doc
covers:
  src/handlers/worker_handler.py
  src/composition/worker_setup.py
  src/web/worker_oidc_verifier.py
  src/domain/worker_task.py
  src/domain/cloud_task_delivery.py
  src/adapters/gcp_task_queue.py
  src/ports/task_queue.py
  src/services/task_dispatch_service.py
  src/adapters/firestore_task_claim_store.py
  src/ports/task_claim_store.py
  src/services/disabled_task.py
  src/services/billing_daily_summary_task.py
  src/services/deep_research_failed_task.py
  src/services/geo_mark_turn_task.py
  src/services/external_task_timer_task.py
  src/services/external_task_delivery_task.py
  src/services/external_task_wake_task.py
  scripts/infrastructure/cloudbuild_scheduler_jobs.sh
  scripts/infrastructure/setup-daily-email-review-scheduler.sh
  src/services/background_empty_answer_reporter.py
-->
# Worker hub, Cloud Scheduler jobs and queues

Everything that runs in the background reaches the main service as `POST /worker`: from Cloud Scheduler (a fan-out or
a sweep tick) or from Cloud Tasks (one unit of work). This doc owns the hub that receives it, the task-type map, the
Scheduler jobs, the drive queue and the deadlines. Manual triggers: root `CLAUDE.md` § Manual Triggers.

## The hub

- **Gate.** `/worker` verifies a Google OIDC token in the app (`src/web/worker_oidc_verifier.py::build_oidc_gate`): valid
  signature, an accepted service-account email, no audience pinning (several callers, so the audience is not part of the check; the build-managed Scheduler jobs
  send the target URL as the audience, as Cloud Tasks does by default).
  An empty accepted set refuses every call on Cloud Run and opens only locally. Refused: 401, the handler never runs
  (`decisions/worker_oidc_and_docx_sandbox.md`).
- **Dispatch.** `src/handlers/worker_handler.py::WorkerHandler.handle` claims the delivery, then `WorkerHandler._dispatch`
  looks the payload's `task_type` up in one registry. An unknown type is 400. A queued Slack event has no `task_type`
  and is recognised by its `event` and `session_id` keys.
- **Registry.** The handler's own entries plus the runners composition builds in
  `src/composition/worker_setup.py::build_worker_tasks` (each a `src/domain/worker_task.py::WorkerTask`, owning only
  its dependencies). A type served by both raises at construction. A feature whose dependency is absent is registered
  as `src/services/disabled_task.py::DisabledTask`, which answers the refusal the task always gave. A new task type is
  a runner plus one registry line plus a row in the map below.
- **Claims.** Every Cloud Tasks delivery is claimed by its task name (`src/domain/cloud_task_delivery.py::CloudTaskDelivery`,
  `src/ports/task_claim_store.py::TaskClaimStore`), so a redelivery of a finished task is acked as `duplicate`. Status
  decides the claim: 5xx and 429 release it (Cloud Tasks retries and the retry runs); any other status marks it done.
  A store error fails open (the task runs). A request without Cloud Tasks headers (Scheduler, curl) is never claimed.
  Why and the limits: `decisions/worker_task_dedup.md`.
- **Status contract.** 5xx asks Cloud Tasks to retry; 2xx and 4xx end the task. A handler answers 5xx only when a rerun
  is safe. Once a background turn has started its outcome is final and 200, even a failed one, because a rerun would
  repeat what the turn already did (`decisions/background_turn_started_is_final.md`).
- **Enqueue.** Services enqueue through the `src/ports/task_queue.py::TaskQueue` port
  (`src/adapters/gcp_task_queue.py::GcpTaskQueue`); some hold it directly, others through the wrapper
  `src/services/task_dispatch_service.py::TaskDispatchService`.
  `enqueue_worker_task(..., dedup_key=)` names the Cloud Tasks task `{task_type}-<sha256(key)[:32]>`; an
  `AlreadyExists` is a silent no-op (returns `None`). Cloud Tasks keeps a name only while the task is queued or
  running and for about an hour after, so a name merges tasks created close together; it never means "once a day".
  Rhythm comes from stored state (a mark or a deterministic document id), not from a name.

## Task-type map

The Scheduler column says who sends it; the cron values are in `scripts/infrastructure/cloudbuild_scheduler_jobs.sh`.
All Scheduler jobs POST to the main service, with OIDC (`/health` keep-alive excepted).

| `task_type` | Sent by | Runs | Notes |
|---|---|---|---|
| `fire_due_reminders` | Scheduler | `WorkerHandler._handle_fire_due_reminders` → `RemindersService.fire_due_reminders` | Claims each due note, enqueues `execute_reminder` |
| `execute_reminder` | Cloud Task | `ExecuteReminderTask` | One claimed fire |
| `start_email_indexing` | Scheduler | `WorkerHandler._handle_start_email_indexing` | Fan-out; enqueues `email_indexing` per job |
| `email_indexing` | Cloud Task | `WorkerHandler._handle_email_indexing` | One page, re-enqueues itself while pages remain |
| `email_indexing_watchdog` | Scheduler | `WorkerHandler._handle_watchdog` | Fails `running` jobs not updated for the stale threshold |
| `start_daily_email_review` | Scheduler | `StartDailyEmailReviewTask` | Fan-out |
| `daily_email_review` | Cloud Task | `DailyEmailReviewTask` | Review flow: `docs/05_building_blocks/gmail_email_indexing/README.md` |
| `renew_all_task_subscriptions` | Scheduler | `WorkerHandler._handle_renew_all_task_subscriptions` | Fan-out: `renew_task_subscriptions` per To Do user (Microsoft webhook subscriptions expire; this keeps them alive) |
| `renew_task_subscriptions`, `setup_microsoft_todo`, `reindex_task_list` | Cloud Task | `WorkerHandler._handle_*` | To Do onboarding and upkeep; 501 without the To Do services |
| `reconnect_notice_sweep` | Scheduler | `WorkerHandler._handle_reconnect_notice_sweep` → `IntegrationReconnectService.sweep` | See below |
| `start_drive_sync` | Scheduler | `WorkerHandler._handle_start_drive_sync` → `DriveSyncSchedulerService.fan_out` | See § Start Drive Sync |
| `drive_sync` | Cloud Task on the drive queue | the indexer service (`indexer_main.py`), not this hub | See § Start Drive Sync |
| `sweep_consolidation`, `consolidation` | Scheduler, Cloud Task | `WorkerHandler._handle_sweep_consolidation`, `_handle_consolidation` | See § Consolidation sweeps |
| `sweep_companion_consolidation`, `companion_consolidation` | Scheduler, Cloud Task | `WorkerHandler._handle_sweep_companion_consolidation`, `_handle_companion_consolidation` | Session-keyed twin (`docs/10_rfcs/COMPANION_AGENTS_RFC.md` §6) |
| `fact_lifecycle_sweep` | Scheduler | `WorkerHandler._handle_fact_lifecycle_sweep` | See § Fact Lifecycle Sweep |
| `fact_review`, `interactive_choice` | Cloud Task | `WorkerHandler._handle_fact_review`, `_handle_interactive_choice` | See § Fact Lifecycle Sweep |
| `billing_daily_summary` | Scheduler | `BillingDailySummaryTask` | Daily per-account usage report; 501 without the alert sink or account repo |
| `deep_research_failed`, `geo_mark_turn` | Cloud Task | `DeepResearchFailedTask`, `GeoMarkTurnTask` | See below |
| `external_task_timer` | Cloud Task | `ExternalTaskTimerTask` → `ExternalTaskService.on_timer` | Lease, deadline and hold timers of the external task queue; no Scheduler job; 501 without a task queue |
| `external_task_deliver` | Cloud Task | `ExternalTaskDeliveryTask` | A final external task's workspace file and its system alert to Smart; 501 without a task queue |
| `external_task_wake` | Cloud Task | `ExternalTaskWakeTask` → `ExternalTaskService.on_wake` | The content-free wake-up POST to a worker's target through the outbound-request guard; no Scheduler job; 501 without a task queue |
| `agent_execution`, `video_generation_polling`, `telegram_update`, queued Slack event | Cloud Task | agent worker, `VideoGenerationPollingTask`, platform workers | Platform entries answer 503 when the platform is not wired |

## Deadlines: two numbers, do not confuse them

The Scheduler attempt deadline bounds only the fan-out call (claim due work, enqueue tasks). Most jobs use the short
default; the ones that may post a notice or do a pass of work inside the request use a longer one (the script holds
each value). The work itself runs under the Cloud Tasks `dispatch_deadline` of the task the tick enqueued.
Left unset it is the Cloud Tasks default (600 s); an in-process budget above it is cut mid-run, the cut reads as a failure and the queue retries
the whole run.

A task type whose budget can exceed 10 minutes must pass `deadline_seconds` to `enqueue_worker_task`:

| Task | Deadline |
|---|---|
| `execute_reminder`, `daily_email_review` | `src/infrastructure/notification_sla.py::dispatch_deadline_s` of the kind (`REMINDER`, `DAILY_DIGEST`) |
| `external_task_deliver` | `src/domain/external_task_jobs.py::DELIVER_DEADLINE_S`, pinned by a test to `dispatch_deadline_s(EXTERNAL_TASK)` |
| `drive_sync` (every enqueue) | `src/domain/drive_index_params.py::TICK_DEADLINE_S` |
| `fact_review` (janitor enqueue, the review's own deferral) | `src/domain/fact_review_task_keys.py::REVIEW_DEADLINE_S`; the `next` send released by an answer sets none (it takes no lease and makes no model call); the review's own deferral is delayed by `LEASE_BUSY_RETRY_DELAY_S` |
| `consolidation` deferral, `geo_mark_turn` | `src/services/consolidation_service.py::_DEFERRAL_DEADLINE_S`, `src/services/geo_mark_service.py::TURN_DEADLINE_S` |
| anything else | what its enqueue method sets (`GcpTaskQueue`), else 600 s |

`dispatch_deadline_s` is derived, not hardcoded: the worst case across the kind's tier overrides in `NOTIFICATION_SLA`
plus handler headroom, clamped to the Cloud Tasks maximum (also this service's Cloud Run request timeout). Raising a
budget in `NOTIFICATION_SLA` raises its transport. A manual `gcloud tasks create-http-task` sets no deadline and gets
600 s.

## Scheduler jobs

Region `us-central1`. All jobs are created by the `scheduler-jobs` step of `cloudbuild-dev.yaml`, which runs
`scripts/infrastructure/cloudbuild_scheduler_jobs.sh` on every deploy: `describe && update || create` (the `/health`
keep-alive is `update || create`). `update` takes `--update-headers`, `create` takes `--headers`: a `gcloud` difference
to keep when adding a job. The OIDC identity is read from the `SERVICE_ACCOUNT_EMAIL` secret at build time, never
hardcoded. There is one live environment: every job is named `alek-bot-dev-<job>` (the voice keep-alive
`alek-voice-relay-dev-keep-alive`).

**Adding a job:** copy a block of the script, set its schedule, `--message-body` and `--attempt-deadline` (the longer
value only if a pass can exceed the short one), and add the task type to the map above and to this section if it
carries a trap. Keep the OIDC flags, or the job gets 401 at the `/worker` gate:

```bash
gcloud scheduler jobs describe <JOB_NAME> --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http <JOB_NAME> \
  --schedule="<CRON>" --uri="$SERVICE_URL/worker" --http-method=POST \
  --oidc-service-account-email="$SA" --oidc-token-audience="$SERVICE_URL/worker" \
  --message-body='{"task_type":"<TASK_TYPE>"}' --update-headers="Content-Type=application/json" \
  --location=us-central1 --attempt-deadline=<DEADLINE> --project=$PROJECT_ID \
|| gcloud scheduler jobs create http <JOB_NAME> ...   # same flags, with --headers instead of --update-headers
```

`$SERVICE_URL` and `$PROJECT_ID` come from the step's `env:` and `$SA` from the secret read at the top of the script;
Cloud Build substitutions do not apply inside the script file.

The job update runs after the `drive-queue` step, so a failing drive-queue step (for example a missing queue admin
grant) skips the job update too.

**Stagger.** Hourly fan-outs sit on distinct minutes so they do not collide with each other, `fire-due-reminders`
(every 5 min) and the keep-alives (every 10 min); the drive fan-out runs at `3-59/10` so it fires inside each 10-minute
`tick_key` bucket, not on its boundary. Pick a free minute when adding one.

**Do not run `scripts/infrastructure/setup-daily-email-review-scheduler.sh`.** It deletes and recreates the
daily-review job on a different minute and with a different audience (the bare service URL); the next deploy overwrites it
again. The build-managed job is the authority.

### Keep-alives

`alek-bot-dev-keep-alive` is `GET /health` without OIDC; it avoids dev cold starts. `alek-voice-relay-dev-keep-alive`
POSTs `/voice/relay-warmup` on the main service (`src/web/voice_control_plane_app.py`, OIDC, audience = that route's
URL, empty body), which GETs the relay and waits for it. The relay's cold start outlasts Cloudflare's WebSocket
handshake, and Scheduler cannot call the relay directly: its request carries a `Content-Length`, which the relay's
`websockets` parser rejects. The attempt deadline must outlast the warm-up's own wait.

### Fire Due Reminders

`WorkerHandler._handle_fire_due_reminders` → `src/services/reminders_service.py::RemindersService.fire_due_reminders`.
Per due note it claims the fire with an atomic precondition on the note's `due` (recurrent: reschedule; one-time: mark
fired, the note is deleted after delivery), and only a won claim enqueues `execute_reminder`, named by note and due
time. `src/services/execute_reminder_task.py::ExecuteReminderTask` answers 500 (retry) only when the turn never
started; once started every outcome is final and 200 (`ok`, `empty_answer_final`, `turn_cap_final`, `failed_final`),
a final outcome with nothing sent stamps the fire and tells the owner once, and a stamped fire answers
`already_delivered` on a redelivery.

**The cadence is a contract.** Sub-hourly recurrence rules are limited to a 5-minute floor
(`src/adapters/dateutil_recurrence_adapter.py::_MIN_MINUTELY_INTERVAL`); the job fires every 5 minutes. Change both
together. Missed occurrences are skipped, not replayed.

### Email jobs

- `start_email_indexing` (hourly): for each Gmail user with auto-index on, `EmailIndexingService.start_indexing_for_eligible_users`
  starts at most one job per local date (deterministic job id, no job already running, credentials present) in the
  hour slot of the user's setting; the handler enqueues one `email_indexing` Cloud Task per job.
- `email_indexing_watchdog` (every 2 h): marks `running` jobs not updated within the stale threshold as `failed`; covers a task timeout
  or a crash.
- `start_daily_email_review` (hourly): `StartDailyEmailReviewTask` finds users whose review slot is open at this
  tick (`src/domain/daily_review_window.py::review_slot_date`), claims the mark `daily_review:{user}:{slot_date}`, and
  enqueues `daily_email_review` named by that mark with `slot_date` in the payload. A failed claim is retried next
  tick; a failed enqueue releases the mark. `daily_email_review` answers 500 only when the turn never started (same
  contract as reminders).

### Reconnect Notice Sweep

`reconnect_notice_sweep` (hourly; the longer attempt deadline, because a pass may post through the formatter agent): lists users from the
feature stores (Gmail indexing states, To Do configs), not the credential store. For each with a past connection, no
credential and no deliberate disconnect it sends the one reconnect notice and raises a flag on the feature state; a
credential that is back lowers it. Drive's notice runs in the drive fan-out. Unconfigured it answers
`200 {"status": "disabled"}`, not 5xx: a 5xx request log is ERROR severity and would page every hour.
`decisions/reconnect_notice_r1.md`.

### Start Drive Sync

`start_drive_sync` (every 10 minutes = `src/domain/drive_index_params.py::TICK_INTERVAL`) is the fan-out:
`DriveSyncSchedulerService.fan_out` enqueues one `drive_sync` Cloud Task per connection, on the drive queue.
Spec: `docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §7.1, §12.1, §12.5.

- **Candidates:** `ACTIVE` connections with indexing on, plus every connection with a stored `DriveSyncState` (indexing
  off, disconnected, user deleted), so an orphaned index is always purged. A connection with neither is skipped.
- **Skips:** a connection whose continuation was enqueued less than `TICK_DEADLINE_S` ago, or whose lease is held by a
  running tick. Task key `{connection_id}:tick:{10-min bucket}` (`src/domain/drive_task_keys.py::tick_task_key`), so a
  Scheduler retry in the same bucket is a no-op.
- **Minimal state:** right after a connection's own enqueue, an `ACTIVE` indexing connection with no sync state gets
  one (`created_at` only), so a connection whose ticks never land is alerted.
- **Second pass**, after every tick is enqueued: the reconnect and inaccessible-file notices and the ops alerts
  (staleness, search-v2, caps). A failure here never costs anyone their tick.
- **Unconfigured:** without `DRIVE_INDEXER_URL` (the first pass of the two-pass first deploy) it answers
  `200 {"status": "disabled"}`, not 501, which would be an ERROR log every 10 minutes.
- **Backlog:** while the queue drains it holds about one task per connection plus nudges. While it is stuck (indexer
  down, queue paused) each fan-out adds one per connection under a new bucket key; after recovery the backlog runs as
  cheap no-op ticks.

The ticks run on the indexer service (`indexer_main.py`, Cloud Run service `alek-bot-indexer-dev`), through a
dedicated queue `drive-sync-dev`, both provisioned in `cloudbuild-dev.yaml` (steps `drive-queue` and the indexer
deploy):

- **Queue:** The concurrency and dispatch-rate limits are a CPU/memory knob for the indexer
  instance; the per-connection Firestore lease is the correctness mechanism. The app never calls
  `create_queue_if_not_exists` on it (that would create it with concurrency 5). Retry is bounded (a few attempts with
  backoff): only an unhandled exception answers 500; every handled tick outcome is 200 and the tick enqueues its own
  continuation. The values are in the `drive-queue` step.
- **Target:** `/worker` of the indexer, `--ingress=internal` plus the in-app OIDC gate
  (`src/web/indexer_worker_app.py::indexer_audiences`: audience pinned to the service URL, bare or with `/worker`;
  fails closed on Cloud Run with no accepted identity). The identity is `DRIVE_INVOKER_SA_EMAIL`, else
  `SERVICE_ACCOUNT_EMAIL`, plus the optional list `OIDC_TRANSITION_SA_EMAILS`.
- **IAM prerequisite:** the identity the build runs as needs `roles/cloudtasks.queueAdmin` on this queue (granted once
  by the owner), and the enqueuing runtime needs `actAs` on the task identity.
- **Deadline:** every `drive_sync` enqueue passes `TICK_DEADLINE_S`; the tick starts no new work after
  `TICK_START_BUDGET`.
- **Producers** (task-key helpers in `src/domain/drive_task_keys.py`): the fan-out; `UserDriveService` nudges after a bot
  mutation (re-enqueued as `…:r{n}` on a busy lease); the Cabinet toggle; the indexer's own continuations (after the
  tick releases its lease) and busy retries (once per lease holder).
- **Manual `drive_sync`:** needs the connection id (a body without it is refused with 400, never retried into success;
  the drive scripts list ids when `--connection` is missing) and goes through the queue:
  `gcloud tasks create-http-task --queue=drive-sync-dev --location=us-central1 --url=<indexer URL>/worker --oidc-service-account-email=<SA> --body-content='{"task_type":"drive_sync","user_id":"<id>","connection_id":"<connection id>"}' --header=Content-Type:application/json`.
  It gets the 600 s default deadline. `<indexer URL>` must be the value of `INDEXER_URL_DEV`: another `run.app` URL
  form of the same service gets 401.
- **Deploy order:** the queue, the indexer and its IAM step come after the main service, so a failing drive step never
  blocks a fix of the bot. `make deploy` refuses an empty `INDEXER_URL_DEV` unless `DRIVE_FIRST_PASS=1`
  ([README.md](README.md) § Indexer URL).

### Billing Daily Summary

`BillingDailySummaryTask` posts one message to the billing webhook (the alert sink) covering every account that used
tokens yesterday. Yesterday and its month are resolved against the calendar, not the raw snapshot, so an idle account is skipped; a day with no
activity posts nothing. The job runs in Europe/Madrid time (DST-aware). Prices: `src/domain/billing.py` is the authority.

### Fact Lifecycle Sweep (the fact janitor)

`WorkerHandler._handle_fact_lifecycle_sweep` → `FactLifecycleSweepService.sweep`, daily 09:30 Europe/Madrid (after the
09:00 billing summary), with the longer attempt deadline. What it does to facts, the breaker and the review: owner's map
`docs/05_building_blocks/memory/README.md` §5.5, §5.6; spec `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` D4. Hub-level facts:

- **No retry on purpose** (the job sets no `--max-retry-attempts`): a retry after the attempt deadline would run beside a
  run still going. A failed run logs its type and stage, posts one failure line to the ops sink and answers 500; the
  next day's run catches up.
- **Run guard:** `FactLifecycleSweepService` takes a Firestore guard (held `src/domain/fact_lifecycle_run_guard.py::RUN_GUARD_SECONDS`)
  before reading anything; an overlapping run answers 200 `{"status": "busy"}` and does nothing, so a hand run during
  the scheduled one is safe. The ops line is posted once per UTC day, and the last run of
  a day replaces the day's snapshot. A hand run before 09:30 Europe/Madrid on the same UTC day takes the day's line,
  so the scheduled run then posts none (its numbers are in the snapshot).
- **The sweep works inside Scheduler's own request**, unlike the fan-outs: a run past the attempt deadline shows as a failed attempt
  in Scheduler while it goes on. It is not a Cloud Task, so there is no claim record, and it takes no memory-write lease.
- **Log-only unless switched on:** `FACT_LIFECYCLE_SWEEP_DRY_RUN` is read with `os.getenv` in
  `src/composition/memory_setup.py` and parsed by `src/domain/fact_lifecycle_sweep.py::sweep_dry_run`; only an explicit off value (`0`, `false`, `no`, `off`) makes it
  write. The deploy sets `0`. Rollback: remove the variable in `cloudbuild-dev.yaml` and deploy (`--set-env-vars`
  replaces every variable).
- **Indexes** (`config/firestore.indexes.json`, `READY` before the code ships): `(state, expires_at)` on the facts
  collection for the due query (without it the run answers 500), and `(account_id, state, valid_to DESC)` for the
  review's "set aside lately" read. The collections the sweep writes carry no field named `expires_at` and no TTL policy.

It enqueues the two memory-review task types. Neither has a Scheduler job; both are Cloud Tasks claimed by task name:

| Task type | Enqueued by | Payload | Dedup key | Runs |
|---|---|---|---|---|
| `fact_review` | the janitor when `review_due` names a reason; the review itself, to defer when the memory-write lease is taken; `FactAnswerService`, to send the next question after one closes | `{user_id, account_id, mode}`, `mode` is `daily` or `next` | `src/domain/fact_review_task_keys.py` (`daily_key`, `defer_key`, `next_key`) | `WorkerHandler._handle_fact_review` → `FactReviewService.run`; 200 for every outcome, 5xx when a lease, store or queue error raises, 400 on a missing id or another mode |
| `interactive_choice` | the Slack interactivity route (`src/adapters/slack/interactivity_route.py`) after verifying the Slack signature, one task per button press | the choice (`InteractiveChoice.to_payload`) | `interactive_choice-` + hash of the action | `WorkerHandler._handle_interactive_choice` → `InteractiveChoiceService.handle`; 400 when the payload is not a choice |

Without the service wired, `/worker` answers either with 501.

### Consolidation sweeps

`sweep_consolidation` (hourly) closes a data hole: a batch whose attempts are exhausted (`FAILED`) stops the re-enqueue
chain, and its messages are already out of session history but not in memory. `WorkerHandler._handle_sweep_consolidation`
→ `ConsolidationService.find_stuck_users` (one projection query) → per user `revive_failed_batches` (bounded by
`src/domain/consolidation.py::MAX_FAILED_REVIVALS` over a batch's life) and an ordinary `consolidation` Cloud Task.
No-op when the queue is empty.

The `consolidation` task may defer itself: every batch runs under the account's memory-write lease. A task that finds
the lease taken while the user has an open batch enqueues one more `consolidation` task (delayed by `LEASE_BUSY_RETRY_DELAY_S`,
`_DEFERRAL_DEADLINE_S`) named from `consolidation-defer:<user>:<delay-width bucket>` and answers 200 without enqueueing the
next batch; losers in one bucket collapse into one task. The name is never fixed because Cloud Tasks keeps a name for
about an hour. The sweep takes no lease. A zombie reset (a `processing` batch past its threshold, shorter under the
lease) counts the lost run as an attempt, so a batch that kills its worker ends `failed` instead of looping. Memory map
`docs/05_building_blocks/memory/README.md` §3.

`sweep_companion_consolidation` (hourly) is the session-keyed twin but not the same: it enqueues one
`companion_consolidation` Cloud Task per session found by `CompanionExtractionService.find_stuck_sessions`; the handler
does no bounded revival, the companion queue resets `FAILED` batches without a cap (attempts back to 0), and the path
takes no memory-write lease.

## Tasks without a Scheduler job

- **`deep_research_failed`** is enqueued by `job_main.py` on an agent failure, an empty result or a failed delivery
  (`reason`: `agent_failed`, `empty_result`, `delivery_failed`), with dedup key `deep_research_failed:<job_id>`.
  `DeepResearchFailedTask` tells the user in the origin channel (the channel comes from `session_id`): 400 without user
  or account, 500 only when the notice was not sent at all (Cloud Tasks retries); a sent notice is never resent;
  `delivery_failed` uses the "report could not be prepared" wording. A job killed by its task timeout or by the OS
  sends nothing.
- **The report task** `deliver_deep_research` enqueues (an `agent_execution` of the HTML page agent,
  `src/services/deep_research_delivery.py`) carries dedup key `deep_research_report:<job_id>`, so a retried create is
  idempotent: the create is retried twice more by that name, `AlreadyExists` counts as handed off, and only then is the
  failure reported (the job then enqueues `deep_research_failed` with `delivery_failed`). There is no polling task type.
- **`external_task_timer` and `external_task_deliver`** are enqueued by `ExternalTaskService`
  (`docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md` §5.8, §5.9; bodies in `src/domain/external_task_jobs.py`). A timer's
  dedup key is `lease:<task_id>:<generation>:<renewals>`, `deadline:<task_id>:<epoch s>` or `hold:<task_id>`; the
  delivery's is `deliver:<task_id>`. The timer answers 200 `{"changed": bool}` (a stale timer is a no-op) and 400 on a
  malformed body. The delivery answers 400 on a malformed body, 200 `gone` (no such final task), `already` (delivered
  before), `no_notice` (a cancelled task) or `delivered`. It writes a done task's result, or a worker's failure reason,
  as a workspace file under `references/external_tasks/`, then sends Smart a system alert through `notify` (kind
  `EXTERNAL_TASK`, the task's origin channel, run origin `background:external_task_deliver`), as `execute_reminder`
  does (`docs/05_building_blocks/external_task_queue/README.md`). A refused file (caps) or no workspace changes the
  alert, not the outcome. Every outcome of the alert turn is final and recorded: no channel is logged at error level, an
  empty answer goes to the empty-answer report, a failed turn or a failed post is not run again. A store failure answers
  5xx and Cloud Tasks retries (the file of an earlier attempt, found by its origin, is reused).
- **`external_task_wake`** is enqueued by `ExternalTaskService` when a task becomes queued (an untainted create, a
  `$task send`) and is its worker's oldest queued task, or when a lease timer re-queues a task, and only for a worker
  that has a wake-up target and holds no unexpired lease (`docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md` §5.8). Body
  `{user_id, account_id, worker_id, generation}`, never task content; dedup key `external_task_wake:<worker_id>:
  <generation>`, where `generation` is the worker's stored `wake_generation` the enqueue moved on (rhythm from stored
  state, not from the name). The runner answers 400 on a malformed body; 200 `skipped` (a stale generation, a cleared
  target, a removed worker or no queued work) or `sent`; 503 `retry` when the target is down or slow (a 5xx, a
  connection failure, a timeout), so Cloud Tasks retries with the queue's backoff, up to `WAKE_MAX_ATTEMPTS` deliveries
  counted from `X-CloudTasks-TaskRetryCount`, after which it answers 200 `gave_up`; 200 `refused` when the guard refuses
  the target or it answers 3xx/4xx: final, logged once, and one literal notice naming the worker's label and the closed
  code goes to the origin channel of its oldest queued task. The task stays queued in every case.
- **`geo_mark_turn`** is enqueued by `GeoMarkService.redeem_link` (`POST /geo/<link_id>`) when the owner wrote text after
  `$geo`; a failed enqueue appends the question and the mark to the session instead. `GeoMarkTurnTask` hands the payload
  to the Slack adapter's turn runner, which re-authorizes the user and runs the turn: 400 on a missing key or another
  platform, 501 without a runner. Design: `docs/10_rfcs/GEO_MARKS_RFC.md` §4.5.
