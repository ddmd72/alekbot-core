#!/bin/bash
# ==============================================================================
# Script: create_drive_vector_indexes.sh
# Purpose: Create the card vector indexes of the user-drive index
#          (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §12.5). `make deploy-indexes` skips vector indexes,
#          so these two are created here. They are also declared in config/firestore.indexes.json.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] ./scripts/infrastructure/create_drive_vector_indexes.sh
# ==============================================================================

set -u

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="us-production"
COLLECTION="${COLLECTION_PREFIX-development_}drive_index_records"
VECTOR_CONFIG='{"dimension":"768", "flat": "{}"}'

echo "🔧 Drive card vector indexes"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $COLLECTION"
echo ""

# find_nearest_cards(user_id, ...) — pre-filter user_id
echo "📌 card_vector under user_id..."
gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=card_vector \
  --async \
  || echo "⚠️  Index creation failed or already exists"

# find_nearest_cards(user_id, ..., kind=...) — pre-filter user_id + kind
echo "📌 card_vector under user_id + kind..."
gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=order=ASCENDING,field-path=kind \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=card_vector \
  --async \
  || echo "⚠️  Index creation failed or already exists"

echo ""
echo "⏳ Indexes build asynchronously. Check status with:"
echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
echo "     --filter='collectionGroup:$COLLECTION'"
