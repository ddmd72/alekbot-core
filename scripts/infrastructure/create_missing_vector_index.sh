#!/bin/bash
# Script to create required Firestore indexes
# Auto-generated based on missing index errors

PROJECT_ID="$PROJECT_ID"
DATABASE="us-production"

echo "🔥 Creating missing vector index for development_domain_facts_v2..."

gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE" \
  --collection-group=development_domain_facts_v2 \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=account_id \
  --field-config=order=ASCENDING,field-path=is_current \
  --field-config=vector-config='{"dimension":"768","flat": "{}"}',field-path=vector \
  --async

echo "✅ Index creation command submitted!"
