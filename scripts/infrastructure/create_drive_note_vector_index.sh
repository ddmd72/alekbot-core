#!/bin/bash
# ==============================================================================
# Script: create_drive_note_vector_index.sh
# Purpose: Create the vector index on the owner notes' `note_vector` (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.5 item 6,
#          §6.4 step 3): the deleted-files search's meaning fallback, `find_nearest` prefiltered on user_id, state and
#          embedding_space. `make deploy-indexes` skips vector indexes, so it is created here; it is not declared in
#          config/firestore.indexes.json (the composites 1-5 are).
# Owner-run, once, before the deploy; wait for READY (the status command is printed at the end). A dry run by default:
# it prints the gcloud command; APPLY=1 creates the index.
# Usage:   PROJECT_ID=<project> [COLLECTION_PREFIX=development_] [APPLY=1] bash ./scripts/infrastructure/create_drive_note_vector_index.sh
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID (from .env)}"
DATABASE_ID="us-production"
COLLECTION="${COLLECTION_PREFIX-development_}drive_notes"
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

echo "🔧 Drive note vector index ($([[ "$APPLY" == "1" ]] && echo apply || echo dry run))"
echo "   Database:   $DATABASE_ID"
echo "   Collection: $COLLECTION"
echo ""

# find_nearest(user_id, vector, state=..., embedding_space=...) — pre-filter user_id + state + embedding_space
echo "📌 note_vector under user_id + state + embedding_space..."
create_index "note_vector (user_id, state, embedding_space)" \
  --project="$PROJECT_ID" \
  --database="$DATABASE_ID" \
  --collection-group="$COLLECTION" \
  --query-scope=COLLECTION \
  --field-config=order=ASCENDING,field-path=user_id \
  --field-config=order=ASCENDING,field-path=state \
  --field-config=order=ASCENDING,field-path=embedding_space \
  --field-config=vector-config="$VECTOR_CONFIG",field-path=note_vector \
  --async

echo ""
echo "⏳ The index builds asynchronously. Check its status with:"
echo "   gcloud firestore indexes composite list --project=\$PROJECT_ID --database=$DATABASE_ID \\"
echo "     --filter='collectionGroup:$COLLECTION'"
