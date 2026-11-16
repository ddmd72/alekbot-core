#!/bin/bash
# ==============================================================================
# Script: create_email_card_vector_index.sh
# Purpose: Create the email card vector index of email search v2 (email index v2, slice 8). `make deploy-indexes`
#          skips vector indexes, so this one is created here; it is also declared in config/firestore.indexes.json
#          (card_state, user_id, card_vector: v1's live field order on the card's own state field). Without it the
#          meaning list of every search is skipped (one WARNING per search) and only the key lists answer.
#          Owner-run, before the deploy that serves the card search.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] ./scripts/infrastructure/create_email_card_vector_index.sh
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="us-production"
COLLECTION="${COLLECTION_PREFIX-development_}domain_email_facts_v1"
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

echo "🔧 Email card vector index"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $COLLECTION"
echo ""

# FirestoreEmailCardIndex.find_nearest(user_id, ...) — pre-filter card_state == carded, user_id
echo "📌 card_vector under card_state + user_id..."
create_index "card_vector (card_state, user_id)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=card_state \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=card_vector \
  --async

echo ""
echo "⏳ The index builds asynchronously. Check status with:"
echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
echo "     --filter='collectionGroup:$COLLECTION'"
