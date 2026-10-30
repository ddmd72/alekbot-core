#!/bin/bash
# ==============================================================================
# Script: create_drive_scoped_vector_indexes.sh
# Purpose: Create the two vector indexes of the folder-scoped kNN (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md Δ246):
#          the chunk `vector` and the hint `hint_vector` under connection_id + item_id, which `find_nearest_chunks_in` and
#          `find_nearest_hints_in` pre-filter with `connection_id ==` and `item_id in [...]` (at most 30 ids).
#          Per connection since MULTI_DRIVE_RFC §8 (slice 1b); the user_id forms stay live until acceptance.
#          `make deploy-indexes` skips vector indexes, so they are created here; they are also declared in
#          config/firestore.indexes.json. Both must be READY before the code that queries them is deployed.
# Owner-run, once, before the deploy; wait for READY (the status commands are printed at the end). A dry run by default:
# it prints the gcloud commands; APPLY=1 creates the indexes.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] [APPLY=1] bash ./scripts/infrastructure/create_drive_scoped_vector_indexes.sh
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="us-production"
CHUNK_COLLECTION="${COLLECTION_PREFIX-development_}drive_index_chunks"
HINT_COLLECTION="${COLLECTION_PREFIX-development_}drive_index_hints"
VECTOR_CONFIG='{"dimension":"768", "flat": "{}"}'

APPLY="${APPLY:-0}"

# A dry run unless APPLY=1: the command is printed, nothing is created.
# An index that already exists is fine (re-runs); any other failure stops the script with a non-zero exit.
create_index() {
  local label="$1"
  shift
  local out
  if [[ "$APPLY" != "1" ]]; then
    echo "   🔎 $label (dry run, APPLY=1 creates it):"
    echo "      gcloud firestore indexes composite create $*"
    return 0
  fi
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

echo "🔧 Drive folder-scoped vector indexes ($([[ "$APPLY" == "1" ]] && echo apply || echo dry run))"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $CHUNK_COLLECTION, $HINT_COLLECTION"
echo ""

# find_nearest_chunks_in(scope, ..., item_ids) — pre-filter connection_id + item_id in [...]
echo "📌 chunk vector under connection_id + item_id..."
create_index "vector (connection_id, item_id)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$CHUNK_COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=connection_id \
  --field-config=order=ASCENDING,field-path=item_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=vector \
  --async

# find_nearest_hints_in(scope, ..., item_ids) — pre-filter connection_id + item_id in [...]
echo "📌 hint_vector under connection_id + item_id..."
create_index "hint_vector (connection_id, item_id)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$HINT_COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=connection_id \
  --field-config=order=ASCENDING,field-path=item_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=hint_vector \
  --async

echo ""
echo "⏳ Indexes build asynchronously. Check status with (once per collection):"
for col in "$CHUNK_COLLECTION" "$HINT_COLLECTION"; do
  echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
  echo "     --filter='collectionGroup:$col'"
done
