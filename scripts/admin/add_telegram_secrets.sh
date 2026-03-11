#!/bin/bash
# Add Telegram Secrets to Google Cloud Secret Manager (DEV environment)
# Session: 2026-02-09 Telegram Integration Phase 3
# Usage: ./scripts/admin/add_telegram_secrets.sh

set -e

PROJECT_ID="$PROJECT_ID"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN env var is required}"
WEBHOOK_SECRET="${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET env var is required}"

echo "🔐 Adding Telegram secrets to Secret Manager for DEV environment..."
echo "Project: $PROJECT_ID"
echo ""

# Function to create or update secret
create_or_update_secret() {
    local secret_name=$1
    local secret_value=$2
    
    echo "📝 Processing secret: $secret_name"
    
    # Check if secret exists
    if gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
        echo "   ℹ️  Secret exists, adding new version..."
        echo -n "$secret_value" | gcloud secrets versions add "$secret_name" \
            --project="$PROJECT_ID" \
            --data-file=-
        echo "   ✅ New version added"
    else
        echo "   ℹ️  Secret doesn't exist, creating..."
        echo -n "$secret_value" | gcloud secrets create "$secret_name" \
            --project="$PROJECT_ID" \
            --replication-policy="automatic" \
            --data-file=-
        echo "   ✅ Secret created"
    fi
    
    echo ""
}

# Add Telegram Bot Token (DEV)
create_or_update_secret "TELEGRAM_BOT_TOKEN_DEV" "$BOT_TOKEN"

# Add Telegram Webhook Secret (DEV)
create_or_update_secret "TELEGRAM_WEBHOOK_SECRET_DEV" "$WEBHOOK_SECRET"

echo "✅ All Telegram secrets added successfully!"
echo ""
echo "📋 Next steps:"
echo "   1. Deploy to Cloud Run: gcloud builds submit --config cloudbuild-dev.yaml"
echo "   2. Set webhook: curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"url\": \"$_DEV_SERVICE_URL/telegram/webhook\", \"secret_token\": \"<WEBHOOK_SECRET>\"}'"
echo ""
