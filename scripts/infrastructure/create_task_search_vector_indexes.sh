#!/bin/bash
# ==============================================================================
# Script: create_task_search_vector_indexes.sh
# Purpose: Create the four vector indexes of the task search index (FirestoreTaskSearchIndex:
#          content_vector / context_vector, pre-filtered by user_id and optionally status).
#          `make deploy-indexes` skips vector indexes, so they are created here. They are also
#          declared in config/firestore.indexes.json. The distance measure is not part of an index
#          (it is chosen per query); only dimension and flat are.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] ./scripts/infrastructure/create_task_search_vector_indexes.sh
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="${FIRESTORE_DATABASE:-us-production}"
COLLECTION="${COLLECTION_PREFIX-development_}task_search_index"
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

echo "🔧 Task search vector indexes"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $COLLECTION"
echo ""

for vector_field in content_vector context_vector; do
  echo "📌 $vector_field under user_id..."
  create_index "$vector_field (user_id)" \
    --project="$PROJECT_ID" \
    --database="$DATABASE_ID" \
    --collection-group="$COLLECTION" \
    --query-scope=COLLECTION \
    --field-config=order=ASCENDING,field-path=user_id \
    --field-config=vector-config="$VECTOR_CONFIG",field-path="$vector_field" \
    --async

  echo "📌 $vector_field under user_id + status..."
  create_index "$vector_field (user_id, status)" \
    --project="$PROJECT_ID" \
    --database="$DATABASE_ID" \
    --collection-group="$COLLECTION" \
    --query-scope=COLLECTION \
    --field-config=order=ASCENDING,field-path=user_id \
    --field-config=order=ASCENDING,field-path=status \
    --field-config=vector-config="$VECTOR_CONFIG",field-path="$vector_field" \
    --async
done

echo ""
echo "⏳ Indexes build asynchronously. Check status with:"
echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
echo "     --filter='collectionGroup:$COLLECTION'"
