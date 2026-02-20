#!/bin/bash
# ==============================================================================
# Script: create_prod_multi_vector_indexes.sh
# Purpose: Create missing tags_vector and metadata_vector indexes for domain_facts_v2
# Database: us-production
# Author: AI (Cline)
# Date: 2026-02-09
# ==============================================================================

PROJECT_ID="gen-lang-client-0554950952"
DATABASE_ID="us-production"
COLLECTION="domain_facts_v2"

echo "================================================================================"
echo "🔧 Creating Multi-Vector Indexes for PROD"
echo "📦 Project: $PROJECT_ID"
echo "💾 Database: $DATABASE_ID"
echo "📊 Collection: $COLLECTION"
echo "================================================================================"
echo ""

# Index 1: tags_vector (for category/domain queries)
echo "📌 Creating tags_vector index..."
gcloud firestore indexes composite create \
  --database="$DATABASE_ID" \
  --project="$PROJECT_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config field-path=account_id,order=ascending \
  --field-config field-path=is_current,order=ascending \
  --field-config field-path=tags_vector,vector-config='{"dimension":"768"}' \
  --async

echo ""
echo "✅ tags_vector index creation started (async)"
echo ""

# Wait a bit to avoid rate limiting
sleep 2

# Index 2: metadata_vector (for structured data queries)
echo "📌 Creating metadata_vector index..."
gcloud firestore indexes composite create \
  --database="$DATABASE_ID" \
  --project="$PROJECT_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config field-path=account_id,order=ascending \
  --field-config field-path=is_current,order=ascending \
  --field-config field-path=metadata_vector,vector-config='{"dimension":"768"}' \
  --async

echo ""
echo "✅ metadata_vector index creation started (async)"
echo ""
echo "================================================================================"
echo "⏳ IMPORTANT: Indexes are being created asynchronously"
echo "   This will take 5-15 minutes depending on data volume (263 facts)"
echo ""
echo "   Check status with:"
echo "   gcloud firestore indexes composite list --database=us-production"
echo ""
echo "   Look for STATE: CREATING → READY"
echo "================================================================================"
