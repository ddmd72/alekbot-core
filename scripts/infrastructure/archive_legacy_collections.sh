#!/bin/bash
# ==============================================================================
# Script: archive_legacy_collections.sh
# Purpose: Export and delete legacy collections to clean up Firestore
# Author: Cline (AI)
# Date: 2026-02-05
# ==============================================================================

PROJECT_ID="$PROJECT_ID"
BACKUP_BUCKET="gs://alek-core-backups"
TIMESTAMP=$(date +"%Y-%m-%d-%H%M")
BACKUP_PATH="$BACKUP_BUCKET/legacy-cleanup-$TIMESTAMP"

# List of collections to archive (Legacy & Deprecated)
# Based on migration to Semantic Naming (ADR-006)
LEGACY_COLLECTIONS=(
  "development_users"
  "development_users_oauth"
  "development_accounts_oauth"
  "development_facts"
  "development_facts_oauth"
  "development_user_context_oauth"
  "development_observations_archive"
  "development_prompt_components"
  "dev_prompt_system_tokens"
  "dev_prompt_user_tokens"
  "dev_prompt_blueprints"
  "dev_prompt_blueprints_v3"
  "dev_prompt_agent_profiles"
  "dev_prompt_agent_profile_user_overrides"
)

echo "=============================================================================="
echo "🧹 Firestore Legacy Cleanup Tool"
echo "🌍 Project: $PROJECT_ID"
echo "📦 Bucket:  $BACKUP_BUCKET"
echo "📂 Path:    $BACKUP_PATH"
echo "=============================================================================="

# Join collections with comma
IFS=,
COLLECTION_STRING="${LEGACY_COLLECTIONS[*]}"
unset IFS

echo ""
echo "🔍 Collections to archive:"
for col in "${LEGACY_COLLECTIONS[@]}"; do
  echo "   - $col"
done

echo ""
echo "🚀 Step 1: Exporting to GCS (Backup)..."
gcloud firestore export "$BACKUP_PATH" \
  --project="$PROJECT_ID" \
  --collection-ids="$COLLECTION_STRING" \
  --async

echo ""
echo "✅ Export operation submitted (ASYNC)."
echo "   Operation ID allows tracking progress."
echo ""
echo "⚠️  IMPORTANT:"
echo "   Wait for export to finish BEFORE deleting collections!"
echo "   Check status with: gcloud firestore operations list"
echo ""
echo "🚀 Step 2: Deletion (Manual Trigger)"
echo "   Once export is complete, run the following commands to delete:"
echo ""

for col in "${LEGACY_COLLECTIONS[@]}"; do
  echo "   gcloud firestore operations delete-collection-data --collection-ids=$col --project=$PROJECT_ID --async"
done

echo ""
echo "=============================================================================="
