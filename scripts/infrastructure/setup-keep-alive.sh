#!/bin/bash
#
# Setup Cloud Scheduler for Production Keep-Alive
# Prevents Cloud Run scale-to-zero by pinging /health every 10 minutes
#
# Cost: ~$0.10/month (Cloud Scheduler free tier: 3 jobs)
# Effect: Keeps in-memory cache warm, avoids cold starts
#

set -e

# Configuration
PROJECT_ID="${GCP_PROJECT_ID:-alek-core-prod}"
SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-europe-west1}"
JOB_NAME="alek-bot-keep-alive"
SCHEDULE="*/10 * * * *"  # Every 10 minutes
CLOUD_RUN_URL="${CLOUD_RUN_URL:-https://alek-bot-prod-xxxxx.a.run.app}"
HEALTH_ENDPOINT="/health"

echo "🔧 Setting up Cloud Scheduler Keep-Alive for Production"
echo "=================================================="
echo "Project: $PROJECT_ID"
echo "Location: $SCHEDULER_LOCATION"
echo "Schedule: Every 10 minutes"
echo "Target: $CLOUD_RUN_URL$HEALTH_ENDPOINT"
echo ""

# Check if job already exists
if gcloud scheduler jobs describe "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$SCHEDULER_LOCATION" &>/dev/null; then
    
    echo "⚠️  Job '$JOB_NAME' already exists. Updating..."
    
    gcloud scheduler jobs update http "$JOB_NAME" \
        --project="$PROJECT_ID" \
        --location="$SCHEDULER_LOCATION" \
        --schedule="$SCHEDULE" \
        --uri="$CLOUD_RUN_URL$HEALTH_ENDPOINT" \
        --http-method=GET \
        --attempt-deadline=30s \
        --quiet
    
    echo "✅ Job updated successfully"
else
    echo "📝 Creating new job..."
    
    gcloud scheduler jobs create http "$JOB_NAME" \
        --project="$PROJECT_ID" \
        --location="$SCHEDULER_LOCATION" \
        --schedule="$SCHEDULE" \
        --uri="$CLOUD_RUN_URL$HEALTH_ENDPOINT" \
        --http-method=GET \
        --attempt-deadline=30s \
        --description="Keep-alive ping to prevent scale-to-zero" \
        --quiet
    
    echo "✅ Job created successfully"
fi

# Test the job immediately
echo ""
echo "🧪 Testing job..."
gcloud scheduler jobs run "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$SCHEDULER_LOCATION" \
    --quiet

echo ""
echo "✅ Setup complete!"
echo ""
echo "📊 Job details:"
gcloud scheduler jobs describe "$JOB_NAME" \
    --project="$PROJECT_ID" \
    --location="$SCHEDULER_LOCATION"

echo ""
echo "💡 Next steps:"
echo "1. Verify job execution: gcloud scheduler jobs describe $JOB_NAME --location=$SCHEDULER_LOCATION"
echo "2. Check logs: gcloud logging read 'resource.type=cloud_scheduler_job AND resource.labels.job_id=$JOB_NAME'"
echo "3. Monitor Cloud Run: Your app should stay warm (no scale-to-zero)"
echo ""
echo "💰 Cost: ~$0.10/month (Cloud Scheduler free tier: first 3 jobs free)"
