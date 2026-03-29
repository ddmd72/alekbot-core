#!/bin/bash
#
# Setup Cloud Scheduler for Daily Email Review
# Hourly fan-out: for each Gmail user with gmail_daily_review=True, checks if
# current_hour_in_user_tz == gmail_daily_review_hour. If yes, enqueues a
# daily_email_review Cloud Task → SmartAgent inbox analysis → HTML page.
#
# Usage:
#   ENV=dev CLOUD_RUN_URL=https://... SERVICE_ACCOUNT=...@....iam.gserviceaccount.com ./setup-daily-email-review-scheduler.sh
#

set -e

ENV="${ENV:-dev}"
PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID required}"
SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-us-central1}"
CLOUD_RUN_URL="${CLOUD_RUN_URL:?CLOUD_RUN_URL required}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:?SERVICE_ACCOUNT required}"

JOB_NAME="alek-bot-${ENV}-start-daily-email-review"
SCHEDULE="0 * * * *"
ENDPOINT="${CLOUD_RUN_URL}/worker"

PAYLOAD='{"task_type":"start_daily_email_review"}'

echo "🔧 Setting up Daily Email Review Scheduler (${ENV})"
echo "   Job:      ${JOB_NAME}"
echo "   Schedule: every hour"
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

echo "✅ Daily email review scheduler created: ${JOB_NAME}"
