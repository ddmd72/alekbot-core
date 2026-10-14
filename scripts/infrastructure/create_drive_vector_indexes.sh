#!/bin/bash
# ==============================================================================
# Script: create_drive_vector_indexes.sh
# Purpose: Create the card and chunk vector indexes of the user-drive index
#          (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §12.5, §24.6). `make deploy-indexes` skips vector indexes,
#          so these four are created here. They are also declared in config/firestore.indexes.json.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] ./scripts/infrastructure/create_drive_vector_indexes.sh
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="us-production"
COLLECTION="${COLLECTION_PREFIX-development_}drive_index_records"
CHUNK_COLLECTION="${COLLECTION_PREFIX-development_}drive_index_chunks"
VECTOR_CONFIG='{"dimension":"768", "flat": "{}"}'

# An index that already exists is fine (re-runs); any other failure stops the script with a non-zero exit.
create_index() {
  local label="$1"
  shift
  local out
  if out=$(gcloud firestore indexes composite create "$@" 2>&1); then
    echo "   ✅ $label: create request issued"
    return 0
  fi
  if grep -qiE "ALREADY_EXISTS|already exists" <<<"$out"; then
    echo "   ✅ $label: already exists"
    return 0
  fi
  echo "$out" >&2
  echo "❌ $label: index creation failed" >&2
  return 1
}

echo "🔧 Drive card and chunk vector indexes"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $COLLECTION, $CHUNK_COLLECTION"
echo ""

# find_nearest_cards(user_id, ...) — pre-filter user_id
echo "📌 card_vector under user_id..."
create_index "card_vector (user_id)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=card_vector \
  --async

# find_nearest_cards(user_id, ..., kind=...) — pre-filter user_id + kind
echo "📌 card_vector under user_id + kind..."
create_index "card_vector (user_id, kind)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=order=ASCENDING,field-path=kind \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=card_vector \
  --async

# find_nearest_chunks(user_id, ...) — pre-filter user_id
echo "📌 chunk vector under user_id..."
create_index "vector (user_id)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$CHUNK_COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=vector \
  --async

# find_nearest_chunks(user_id, ..., kind=...) — pre-filter user_id + kind
echo "📌 chunk vector under user_id + kind..."
create_index "vector (user_id, kind)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$CHUNK_COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=order=ASCENDING,field-path=kind \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=vector \
  --async

echo ""
echo "⏳ Indexes build asynchronously. Check status with (once per collection):"
for col in "$COLLECTION" "$CHUNK_COLLECTION"; do
  echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
  echo "     --filter='collectionGroup:$col'"
done
