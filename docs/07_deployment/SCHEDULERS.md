# Cloud Scheduler Reference

All Cloud Scheduler jobs for Alek-Core. Managed via `cloudbuild-dev.yaml` using the `describe && update || create` pattern (idempotent on every deploy).

Region: `us-central1`. Attempt deadline: `60s` for all `/worker` jobs.

## Two deadlines, do not confuse them

The Scheduler attempt deadline above bounds only the **fan-out** call — the tick that
claims due work and enqueues Cloud Tasks. It returns in milliseconds and never runs an
agent.

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
| everything else | ≤ 600s | Cloud Tasks default |

The value is derived, not hardcoded: `dispatch_deadline_s(kind)` in
`src/infrastructure/notification_sla.py` takes the worst case across the kind's tier
overrides and adds 2 minutes of handler headroom, clamped to the Cloud Tasks maximum of
1800s (which is also this service's Cloud Run request timeout). Raising a budget in
`NOTIFICATION_SLA` therefore raises its transport automatically.

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

### Fire Due Reminders

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{env}-fire-due-reminders` |
| **Schedule** | `*/5 * * * *` (every 5 min) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "fire_due_reminders"}` |
| **Purpose** | Fires proactive self-reminders whose `due <= now`. Idempotency guard: skips notes fired within the last 4 min. |
| **Handler** | `WorkerHandler._handle_fire_due_reminders()` |
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
| **Schedule** | `0 * * * *` (every hour, on the hour) |
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
| **Schedule** | `0 * * * *` (every hour, on the hour) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "start_daily_email_review"}` |
| **Purpose** | Fan-out: for every Gmail user with `config.gmail_daily_review=True`, checks if `current_hour_in_user_tz == config.gmail_daily_review_hour`. If yes, enqueues a `daily_email_review` Cloud Task. Worker fetches last 24h emails (up to 200, full body via BS4 HTML→text + invisible char stripping) and passes structured JSON to SmartAgent via `notify(save_history=False)`. SmartAgent runs Phase 0 triage → Phase 1 deep reads (`get_email_details`) → Phase 2 research (`search_web`) → delivers HTML report (GCS link) + short chat message. After HTML delivery, URL is saved to session history via `notify_document_link()`. |
| **Handler** | `WorkerHandler._handle_start_daily_email_review()` → `WorkerHandler._handle_daily_email_review()` |
| **User setting** | Toggle + hour picker in Cabinet UI > Integrations > Gmail |
| **Env** | dev + prod |

---

### Email Indexing Watchdog

| Field | Value |
|-------|-------|
| **Job name** | _(not yet in cloudbuild — set up manually)_ |
| **Schedule** | `0 */2 * * *` (every 2 hours) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "email_indexing_watchdog"}` |
| **Purpose** | Scans all `RUNNING` indexing jobs older than 2 hours and marks them `FAILED`. Handles Cloud Tasks timeouts and crash-recovery. |
| **Handler** | `WorkerHandler._handle_watchdog()` |
| **Env** | dev + prod |

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

### Email Embedding Repair

| Field | Value |
|-------|-------|
| **Job name** | `alek-bot-{dev,prod}-repair-email-embeddings` |
| **Schedule** | `0 * * * *` (hourly, top of the hour — aligned with `start_email_indexing` cadence so failures from any indexing tick are detected within ≤1h) |
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
| **Schedule** | `0 * * * *` (hourly, top of the hour) |
| **HTTP** | `POST /worker` |
| **Payload** | `{"task_type": "sweep_consolidation"}` |
| **Purpose** | Re-triggers consolidation for every user with a batch still in the queue. Consolidation is otherwise driven only by session overflow and the `$consolidate` command, so a batch that stalls (e.g. provider billing exhaustion exhausts its 3 attempts → `FAILED`, and the re-enqueue chain stops) would wait for the next overflow — potentially days. That batch's messages were already extracted from session history but not yet written to memory: a data hole between history and memory. This sweep closes it within ≤1h. |
| **Handler** | `WorkerHandler._handle_sweep_consolidation()` → `ConsolidationService.find_stuck_users()` → per user `enqueue_consolidation_task` (reuses the normal consolidation path; `reset_recoverable_batches` revives `FAILED`/zombie batches). |
| **Fan-out** | One distinct query (`FirestoreConsolidationQueue.get_stuck_batch_user_ids`, `user_id` projection) → one `consolidation` Cloud Task per stuck user. No-op when the queue is empty. |
| **Env** | dev + prod |

---

### Companion Extraction (RFC `docs/10_rfcs/COMPANION_AGENTS_RFC.md` §6)

Session-keyed analog of consolidation: `companion_consolidation` processes one companion
extraction batch and re-enqueues itself while more remain (`WorkerHandler._handle_companion_consolidation()`
→ `CompanionExtractionService.process_session_batches()`), mirroring `consolidation` above but keyed
by `session_id` instead of `user_id`. Driven by `TutorExtractorAgent` (RFC §6, Phase D) via
`CompanionExtractorRunner`.

`sweep_companion_consolidation` mirrors `sweep_consolidation`: it re-triggers extraction for every
session with a batch stuck in the queue (`WorkerHandler._handle_sweep_companion_consolidation()` →
`CompanionExtractionService.find_stuck_sessions()`). **No Cloud Scheduler job exists for it yet** —
unlike `sweep_consolidation`, which has the hourly job above. Provisioning that job is deploy-time
infra deliberately deferred to Phase F of the RFC; the handler is wired and callable manually
(`POST /worker {"task_type": "sweep_companion_consolidation"}`) in the meantime.

| Field | Value |
|-------|-------|
| **Job name** | none provisioned yet (Phase F) |
| **Payload** | `{"task_type": "companion_consolidation", "session_id": "..."}` / `{"task_type": "sweep_companion_consolidation"}` |
| **Handler** | `WorkerHandler._handle_companion_consolidation()` / `_handle_sweep_companion_consolidation()` |
| **Env** | dev + prod (handler only; no scheduled trigger) |

---

**Last Updated:** 2026-08-27
