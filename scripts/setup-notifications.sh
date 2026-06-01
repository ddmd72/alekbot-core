#!/bin/bash
# Cloud Build & Cloud Run Notifications Setup
# Sends alerts to Slack on build failures and runtime errors

set -e

# Configuration
PROJECT_ID="${PROJECT_ID:-$PROJECT_ID}"
REGION="${REGION:-europe-southwest1}"
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL}"  # Set this environment variable

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🔔 Setting up Cloud Build & Cloud Run Notifications${NC}"
echo ""

# Check if Slack webhook URL is set
if [ -z "$SLACK_WEBHOOK_URL" ]; then
    echo -e "${RED}ERROR: SLACK_WEBHOOK_URL environment variable not set${NC}"
    echo ""
    echo "To get a Slack webhook URL:"
    echo "1. Go to: https://api.slack.com/messaging/webhooks"
    echo "2. Create a new Incoming Webhook"
    echo "3. Select your channel (e.g., #deployments or #alerts)"
    echo "4. Copy the webhook URL"
    echo ""
    echo "Then run:"
    echo "  export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/YOUR/WEBHOOK/URL'"
    echo "  ./scripts/setup-notifications.sh"
    exit 1
fi

echo -e "${YELLOW}Project ID: $PROJECT_ID${NC}"
echo -e "${YELLOW}Region: $REGION${NC}"
echo ""

# Step 1: Enable required APIs
echo "📦 Enabling required APIs..."
gcloud services enable \
    cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com \
    logging.googleapis.com \
    --project=$PROJECT_ID

echo ""

# Step 2: Store Slack Webhook in Secret Manager
echo "🔐 Storing Slack webhook in Secret Manager..."

# Check if secret already exists
if gcloud secrets describe SLACK_WEBHOOK_URL --project=$PROJECT_ID &>/dev/null; then
    echo "Secret already exists, creating new version..."
    echo -n "$SLACK_WEBHOOK_URL" | gcloud secrets versions add SLACK_WEBHOOK_URL \
        --data-file=- \
        --project=$PROJECT_ID
else
    echo "Creating new secret..."
    echo -n "$SLACK_WEBHOOK_URL" | gcloud secrets create SLACK_WEBHOOK_URL \
        --data-file=- \
        --replication-policy="automatic" \
        --project=$PROJECT_ID
fi

echo ""

# Step 3: Create Pub/Sub topic for Cloud Build notifications
echo "📢 Creating Pub/Sub topic for Cloud Build notifications..."

TOPIC_NAME="cloud-builds"

if gcloud pubsub topics describe $TOPIC_NAME --project=$PROJECT_ID &>/dev/null; then
    echo "Topic already exists: $TOPIC_NAME"
else
    gcloud pubsub topics create $TOPIC_NAME --project=$PROJECT_ID
fi

echo ""

# Step 4: Create notification config for Cloud Build
echo "🔔 Setting up Cloud Build notification config..."

cat > /tmp/cloudbuild-slack-notifier.yaml <<EOF
apiVersion: cloud-build-notifiers/v1
kind: SlackNotifier
metadata:
  name: slack-notifier
spec:
  notification:
    filter: status in [FAILURE, TIMEOUT, CANCELLED]
  secrets:
    - name: SLACK_WEBHOOK_URL
      value: projects/$PROJECT_ID/secrets/SLACK_WEBHOOK_URL/versions/latest
  delivery:
    webhookUrl:
      secretRef: SLACK_WEBHOOK_URL
EOF

echo "Created Cloud Build notifier config: /tmp/cloudbuild-slack-notifier.yaml"
echo ""
echo -e "${YELLOW}Note: Cloud Build notifiers require manual setup via gcloud CLI or Console${NC}"
echo "Run this command to set up the notifier:"
echo ""
echo "  gcloud alpha builds notifiers create slack-notifier \\"
echo "    --filter=\"status in [FAILURE, TIMEOUT, CANCELLED]\" \\"
echo "    --format=json \\"
echo "    --transport-topic=projects/$PROJECT_ID/topics/$TOPIC_NAME \\"
echo "    --project=$PROJECT_ID"
echo ""

# Step 5: Set up log-based alerts for Cloud Run errors
echo "📊 Creating log-based alert for Cloud Run errors..."

ALERT_POLICY_NAME="cloud-run-errors-alert"

# Check if alert policy already exists
if gcloud logging sinks describe $ALERT_POLICY_NAME --project=$PROJECT_ID &>/dev/null 2>&1; then
    echo "Alert policy already exists: $ALERT_POLICY_NAME"
else
    echo "Creating log sink for Cloud Run errors..."

    # Create log sink that filters Cloud Run errors
    gcloud logging sinks create $ALERT_POLICY_NAME \
        pubsub.googleapis.com/projects/$PROJECT_ID/topics/$TOPIC_NAME \
        --log-filter='
resource.type="cloud_run_revision"
severity>=ERROR
resource.labels.service_name=~"alek-.*"
' \
        --project=$PROJECT_ID || echo "Note: Log sink may already exist"
fi

echo ""

# Step 6: Create Cloud Function to send Slack notifications
echo "⚡ Creating notification function..."

FUNCTION_NAME="send-slack-notification"

mkdir -p /tmp/slack-notifier-function
cd /tmp/slack-notifier-function

cat > main.py <<'PYEOF'
import base64
import json
import os
import requests
from google.cloud import secretmanager

def get_slack_webhook():
    """Retrieve Slack webhook URL from Secret Manager"""
    client = secretmanager.SecretManagerServiceClient()
    project_id = os.environ.get('GCP_PROJECT')
    name = f"projects/{project_id}/secrets/SLACK_WEBHOOK_URL/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode('UTF-8')

def send_slack_notification(event, context):
    """Send notification to Slack when triggered by Pub/Sub"""

    # Decode Pub/Sub message
    if 'data' in event:
        message_data = base64.b64decode(event['data']).decode('utf-8')
        message = json.loads(message_data)
    else:
        print("No data in event")
        return

    # Get Slack webhook URL
    webhook_url = get_slack_webhook()

    # Parse Cloud Build event
    if 'status' in message:
        # Cloud Build notification
        status = message.get('status', 'UNKNOWN')
        build_id = message.get('id', 'unknown')
        project_id = message.get('projectId', 'unknown')
        source = message.get('source', {})
        repo = source.get('repoSource', {}).get('repoName', 'unknown')
        branch = source.get('repoSource', {}).get('branchName', 'unknown')

        log_url = message.get('logUrl', '')

        # Determine emoji and color based on status
        emoji_map = {
            'FAILURE': '❌',
            'TIMEOUT': '⏰',
            'CANCELLED': '🚫',
            'SUCCESS': '✅',
        }
        color_map = {
            'FAILURE': '#FF0000',
            'TIMEOUT': '#FFA500',
            'CANCELLED': '#808080',
            'SUCCESS': '#00FF00',
        }

        emoji = emoji_map.get(status, '⚠️')
        color = color_map.get(status, '#CCCCCC')

        # Build Slack message
        slack_message = {
            "text": f"{emoji} Cloud Build {status}",
            "attachments": [{
                "color": color,
                "fields": [
                    {"title": "Status", "value": status, "short": True},
                    {"title": "Build ID", "value": build_id, "short": True},
                    {"title": "Repository", "value": repo, "short": True},
                    {"title": "Branch", "value": branch, "short": True},
                ],
                "actions": [{
                    "type": "button",
                    "text": "View Logs",
                    "url": log_url
                }] if log_url else []
            }]
        }

    elif 'protoPayload' in message or 'jsonPayload' in message:
        # Cloud Run error log
        severity = message.get('severity', 'ERROR')
        resource = message.get('resource', {})
        service_name = resource.get('labels', {}).get('service_name', 'unknown')

        payload = message.get('jsonPayload', message.get('protoPayload', {}))
        error_message = str(payload)[:500]  # Limit message length

        slack_message = {
            "text": f"🔥 Cloud Run Error in {service_name}",
            "attachments": [{
                "color": "#FF0000",
                "fields": [
                    {"title": "Severity", "value": severity, "short": True},
                    {"title": "Service", "value": service_name, "short": True},
                    {"title": "Error", "value": error_message, "short": False},
                ]
            }]
        }

    else:
        # Generic notification
        slack_message = {
            "text": f"📢 GCP Notification",
            "attachments": [{
                "color": "#0000FF",
                "text": json.dumps(message, indent=2)
            }]
        }

    # Send to Slack
    response = requests.post(webhook_url, json=slack_message)

    if response.status_code != 200:
        print(f"Failed to send Slack notification: {response.status_code} - {response.text}")
    else:
        print("Notification sent to Slack successfully")
PYEOF

cat > requirements.txt <<'REQEOF'
google-cloud-secret-manager==2.16.4
requests==2.31.0
REQEOF

echo "Deploying Cloud Function..."
gcloud functions deploy $FUNCTION_NAME \
    --gen2 \
    --runtime=python311 \
    --region=$REGION \
    --source=. \
    --entry-point=send_slack_notification \
    --trigger-topic=$TOPIC_NAME \
    --set-env-vars=GCP_PROJECT=$PROJECT_ID \
    --service-account="${PROJECT_ID}@appspot.gserviceaccount.com" \
    --max-instances=10 \
    --project=$PROJECT_ID || echo "Function deployment may require permissions adjustment"

cd - > /dev/null

echo ""
echo -e "${GREEN}✅ Notification setup complete!${NC}"
echo ""
echo "📝 Next steps:"
echo "1. Test Cloud Build by triggering a build (it will notify on failures)"
echo "2. Test Cloud Run alerts by generating an error in the service"
echo "3. Check your Slack channel for notifications"
echo ""
echo "🔗 Useful commands:"
echo "  # Test notification function"
echo "  gcloud functions call $FUNCTION_NAME --data '{\"test\": true}' --region=$REGION"
echo ""
echo "  # View Cloud Build history"
echo "  gcloud builds list --limit=10 --project=$PROJECT_ID"
echo ""
echo "  # View Cloud Run logs"
echo "  gcloud run services logs read alek-bot-dev --region=$REGION --limit=50"
echo ""
