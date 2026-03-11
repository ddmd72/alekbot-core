#!/bin/bash
#
# Setup Cloud Scheduler for Email Indexing Watchdog
# Detects and marks zombie "running" jobs (stuck > 2h without update) as failed.
#
# Runs every 30 minutes. Posts to /worker with task_type=email_indexing_watchdog.
# Cloud Run handles auth via OIDC.
#
# Usage:
#   ENV=dev CLOUD_RUN_URL=https://... SERVICE_ACCOUNT=...@....iam.gserviceaccount.com ./setup-email-watchdog-scheduler.sh
#

set -e

ENV="${ENV:-dev}"
PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID required}"
SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-us-central1}"
CLOUD_RUN_URL="${CLOUD_RUN_URL:?CLOUD_RUN_URL required}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:?SERVICE_ACCOUNT required}"

JOB_NAME="alek-email-watchdog-${ENV}"
SCHEDULE="*/30 * * * *"
ENDPOINT="${CLOUD_RUN_URL}/worker"

PAYLOAD='{"task_type":"email_indexing_watchdog"}'

echo "🔧 Setting up Email Indexing Watchdog Scheduler (${ENV})"
echo "   Job:      ${JOB_NAME}"
echo "   Schedule: every 30 min"
echo "   Target:   ${ENDPOINT}"

gcloud scheduler jobs delete "${JOB_NAME}" \
  --location="${SCHEDULER_LOCATION}" \
  --project="${PROJECT_ID}" \
  --quiet 2>/dev/null || true

gcloud scheduler jobs create http "${JOB_NAME}" \
  --location="${SCHEDULER_LOCATION}" \
  --project="${PROJECT_ID}" \
  --schedule="${SCHEDULE}" \
  --uri="${ENDPOINT}" \
  --http-method=POST \
  --message-body="${PAYLOAD}" \
  --headers="Content-Type=application/json" \
  --oidc-service-account-email="${SERVICE_ACCOUNT}" \
  --oidc-token-audience="${CLOUD_RUN_URL}"

echo "✅ Watchdog scheduler created: ${JOB_NAME}"
