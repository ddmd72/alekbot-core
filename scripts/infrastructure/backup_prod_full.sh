#!/bin/bash
# ==============================================================================
# Script: backup_prod_full.sh
# Purpose: Create a full snapshot of Firestore Production database
# Author: Cline (AI)
# Date: 2026-02-05
# ==============================================================================

PROJECT_ID="gen-lang-client-0554950952"
BACKUP_BUCKET="gs://alek-core-backups"
TIMESTAMP=$(date +"%Y-%m-%d-%H%M")
BACKUP_PATH="$BACKUP_BUCKET/prod-full-backup-$TIMESTAMP"

echo "=============================================================================="
echo "🛡️  Firestore Full Production Backup"
echo "🌍 Project: $PROJECT_ID"
echo "📦 Bucket:  $BACKUP_BUCKET"
echo "📂 Path:    $BACKUP_PATH"
echo "=============================================================================="

echo ""
echo "🚀 Starting export of ALL collections..."
gcloud firestore export "$BACKUP_PATH" \
  --project="$PROJECT_ID" \
  --async

echo ""
echo "✅ Backup operation submitted (ASYNC)."
echo "   Operation ID allows tracking progress."
echo ""
echo "⚠️  IMPORTANT:"
echo "   Wait for backup to finish (check 'done: true') before modifying data!"
echo "   Check status with: gcloud firestore operations list"
echo "=============================================================================="
