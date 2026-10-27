#!/usr/bin/env bash
# Cloud Scheduler jobs for the dev deploy (cloudbuild-dev.yaml, "scheduler jobs" step).
#
# Lives in a file because Cloud Build limits each step arg to 10 000 characters and the inline
# script outgrew it. Cloud Build substitutions do NOT apply inside files, so the step passes the
# two values it needs through `env:`; nothing is hardcoded here (secrets rule).
set -euo pipefail
: "${PROJECT_ID:?PROJECT_ID must be set by the Cloud Build step env}"
: "${SERVICE_URL:?SERVICE_URL must be set by the Cloud Build step env (from _SERVICE_URL)}"

# OIDC identity for /worker scheduler jobs: read from the SERVICE_ACCOUNT_EMAIL secret (the same
# value the service verifies against), so no infra id is hardcoded (secrets rule) and the
# identity is not derived from the project number. Needs secretAccessor for the build's SA.
# The audience is stated explicitly: main's /worker does not pin it, but the default
# (the job URI) should not be relied on. Under `set -e` a failed create now fails the build
# instead of leaving a job missing.
SA="$(gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL --project=$PROJECT_ID)"
gcloud scheduler jobs update http alek-bot-dev-keep-alive \
  --schedule="*/10 * * * *" \
  --uri="$SERVICE_URL/health" \
  --http-method=GET \
  --location=us-central1 \
  --attempt-deadline=30s \
  --project=$PROJECT_ID 2>/dev/null \
|| gcloud scheduler jobs create http alek-bot-dev-keep-alive \
  --schedule="*/10 * * * *" \
  --uri="$SERVICE_URL/health" \
  --http-method=GET \
  --location=us-central1 \
  --attempt-deadline=30s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-renew-task-subscriptions \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-renew-task-subscriptions \
  --schedule="0 2 * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"renew_all_task_subscriptions"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-renew-task-subscriptions \
  --schedule="0 2 * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"renew_all_task_subscriptions"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-start-email-indexing \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-start-email-indexing \
  --schedule="7 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_email_indexing"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-start-email-indexing \
  --schedule="7 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_email_indexing"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-fire-due-reminders \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-fire-due-reminders \
  --schedule="*/5 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"fire_due_reminders"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-fire-due-reminders \
  --schedule="*/5 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"fire_due_reminders"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-billing-daily-summary \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-billing-daily-summary \
  --schedule="0 9 * * *" \
  --time-zone="Europe/Madrid" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"billing_daily_summary"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-billing-daily-summary \
  --schedule="0 9 * * *" \
  --time-zone="Europe/Madrid" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"billing_daily_summary"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

# Fact janitor (FACT_LIFECYCLE_RFC D4): once a day, after the 09:00 billing summary, stales every fact past its
# `expires_at`. Writes since 2026-10-27 (the main service has FACT_LIFECYCLE_SWEEP_DRY_RUN=0). 180s: one pass per account.
# No retry on purpose (no --max-retry-attempts): a retry could overlap a running sweep and double its per-run cap.
# A failed run posts a 🚨 and the next day's run catches up.
gcloud scheduler jobs describe alek-bot-dev-fact-lifecycle-sweep \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-fact-lifecycle-sweep \
  --schedule="30 9 * * *" \
  --time-zone="Europe/Madrid" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"fact_lifecycle_sweep"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-fact-lifecycle-sweep \
  --schedule="30 9 * * *" \
  --time-zone="Europe/Madrid" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"fact_lifecycle_sweep"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-repair-email-embeddings \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-repair-email-embeddings \
  --schedule="2 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"repair_email_embeddings"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=300s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-repair-email-embeddings \
  --schedule="2 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"repair_email_embeddings"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=300s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-sweep-consolidation \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-sweep-consolidation \
  --schedule="17 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"sweep_consolidation"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-sweep-consolidation \
  --schedule="17 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"sweep_consolidation"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-email-indexing-watchdog \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-email-indexing-watchdog \
  --schedule="27 */2 * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"email_indexing_watchdog"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-email-indexing-watchdog \
  --schedule="27 */2 * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"email_indexing_watchdog"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-sweep-companion-consolidation \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-sweep-companion-consolidation \
  --schedule="47 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"sweep_companion_consolidation"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-sweep-companion-consolidation \
  --schedule="47 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"sweep_companion_consolidation"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=60s \
  --project=$PROJECT_ID

gcloud scheduler jobs describe alek-bot-dev-start-daily-email-review \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-start-daily-email-review \
  --schedule="12 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_daily_email_review"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-start-daily-email-review \
  --schedule="12 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_daily_email_review"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID

# Drive index fan-out (USER_DRIVE_INDEXING_RFC §7.1, §12.1): every TICK_INTERVAL the MAIN
# service enqueues one drive_sync per user on drive-sync-dev (run by alek-bot-indexer-dev),
# then checks staleness and the reconnect notice. 3-59/10 (:03, :13, ...) collides with no
# other job (keep-alive */10, reminders */5, hourly jobs at :02/:07/:12/:17) and fires inside
# each 10-minute tick_key bucket, not on its boundary. 180s: one pass may post a notice
# through the formatter agent. Without DRIVE_INDEXER_URL it answers 200 "disabled".
gcloud scheduler jobs describe alek-bot-dev-start-drive-sync \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-bot-dev-start-drive-sync \
  --schedule="3-59/10 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_drive_sync"}' \
  --update-headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-bot-dev-start-drive-sync \
  --schedule="3-59/10 * * * *" \
  --uri="$SERVICE_URL/worker" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/worker" \
  --http-method=POST \
  --message-body='{"task_type":"start_drive_sync"}' \
  --headers="Content-Type=application/json" \
  --location=us-central1 \
  --attempt-deadline=180s \
  --project=$PROJECT_ID

# Voice relay keep-alive: main's /voice/relay-warmup (POST, empty body) warms the relay, whose
# cold start outlasts Cloudflare's WebSocket handshake. The audience is the route URL, which is
# what the control plane accepts for the invoker identity. Previously created by hand
# (docs/07_deployment/SCHEDULERS.md); now build-managed with the other jobs. 180 s, as the live job:
# main's warmup handler waits up to 120 s for the relay, so the attempt must outlast it.
gcloud scheduler jobs describe alek-voice-relay-dev-keep-alive \
  --location=us-central1 --project=$PROJECT_ID 2>/dev/null \
&& gcloud scheduler jobs update http alek-voice-relay-dev-keep-alive \
  --schedule="*/10 * * * *" \
  --uri="$SERVICE_URL/voice/relay-warmup" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/voice/relay-warmup" \
  --http-method=POST \
  --location=us-central1 \
  --attempt-deadline=180s \
  --description="Keeps the voice relay warm via the main service /voice/relay-warmup (relay cold start outlasts the WebSocket handshake)" \
  --project=$PROJECT_ID \
|| gcloud scheduler jobs create http alek-voice-relay-dev-keep-alive \
  --schedule="*/10 * * * *" \
  --uri="$SERVICE_URL/voice/relay-warmup" \
  --oidc-service-account-email="$SA" \
  --oidc-token-audience="$SERVICE_URL/voice/relay-warmup" \
  --http-method=POST \
  --location=us-central1 \
  --attempt-deadline=180s \
  --description="Keeps the voice relay warm via the main service /voice/relay-warmup (relay cold start outlasts the WebSocket handshake)" \
  --project=$PROJECT_ID
