# Cloud Scheduler Reference

All Cloud Scheduler jobs for Alek-Core. Managed via `cloudbuild-dev.yaml` and `cloudbuild-prod.yaml` using `describe && update || create` pattern (idempotent on every deploy).

Region: `us-central1`. Attempt deadline: `60s` for all `/worker` jobs.

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
| **Env** | dev only (add to cloudbuild-prod.yaml when ready for prod) |

---

**Last Updated:** 2026-03-29
