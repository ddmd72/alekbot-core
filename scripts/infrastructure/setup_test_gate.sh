#!/usr/bin/env bash
# One-time owner step for the cloud test gate (TEST_COVERAGE_SYSTEMIC_RFC §4): an isolated Firestore database, a test
# service account that can touch only that database, the owner's right to impersonate it, a versioned test bucket and
# a paused test queue. Every name comes from .env; nothing here names live resources.
#
#   bash scripts/infrastructure/setup_test_gate.sh            # print the commands (default)
#   bash scripts/infrastructure/setup_test_gate.sh --apply    # run them (idempotent: existing resources are kept)
#   add --with-ci to also create the GitHub Actions Workload Identity binding (main branch only)
#
# Required in .env: GOOGLE_CLOUD_PROJECT, FIRESTORE_DATABASE (the live one, used only by the guard),
#   TEST_FIRESTORE_DATABASE (must end in -test), TEST_SA_NAME (service account id),
#   TEST_GCS_BUCKET (globally unique, must end in -test), TEST_TASKS_QUEUE (must end in -test).
# Optional: TEST_REGION (default us-central1), TEST_FIRESTORE_LOCATION (default nam5).
# For --with-ci: GITHUB_REPO (owner/name), TEST_WIF_POOL (pool id), TEST_WIF_PROVIDER_ID (provider id).
set -euo pipefail

APPLY=0; WITH_CI=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --with-ci) WITH_CI=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

ENV_FILE="${ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || { echo "no $ENV_FILE in $(pwd)" >&2; exit 2; }
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a

need() { [ -n "${!1:-}" ] || { echo "missing $1 in $ENV_FILE" >&2; exit 2; }; }
for v in GOOGLE_CLOUD_PROJECT FIRESTORE_DATABASE TEST_FIRESTORE_DATABASE TEST_SA_NAME TEST_GCS_BUCKET TEST_TASKS_QUEUE; do
  need "$v"
done
P="$GOOGLE_CLOUD_PROJECT"; DB="$TEST_FIRESTORE_DATABASE"; REGION="${TEST_REGION:-us-central1}"
LOC="${TEST_FIRESTORE_LOCATION:-nam5}"; BUCKET="$TEST_GCS_BUCKET"; QUEUE="$TEST_TASKS_QUEUE"
SA="${TEST_SA_NAME}@${P}.iam.gserviceaccount.com"
OWNER="$(gcloud config get-value account 2>/dev/null </dev/null)"

# Guards: never point the test gate at live resources.
case "$DB" in *-test) ;; *) echo "TEST_FIRESTORE_DATABASE must end in -test" >&2; exit 2 ;; esac
case "$BUCKET" in *-test) ;; *) echo "TEST_GCS_BUCKET must end in -test" >&2; exit 2 ;; esac
case "$QUEUE" in *-test) ;; *) echo "TEST_TASKS_QUEUE must end in -test" >&2; exit 2 ;; esac
[ "$DB" != "$FIRESTORE_DATABASE" ] || { echo "test database equals the live database" >&2; exit 2; }
[ "$DB" != "(default)" ] || { echo "the (default) database is never the test database" >&2; exit 2; }

run() { if [ "$APPLY" = 1 ]; then echo "+ $*"; "$@"; else echo "  $*"; fi; }
exists() { timeout 60 "$@" --quiet </dev/null >/dev/null 2>&1; }

echo "== Firestore test database"
if exists gcloud firestore databases describe --database="$DB" --project="$P"; then
  echo "  exists: $DB"
else
  run gcloud firestore databases create --database="$DB" --location="$LOC" --type=firestore-native --project="$P"
fi

echo "== Test service account"
if exists gcloud iam service-accounts describe "$SA" --project="$P"; then
  echo "  exists: $SA"
else
  run gcloud iam service-accounts create "$TEST_SA_NAME" --display-name="alekbot test gate" --project="$P"
fi

COND="expression=resource.name==\"projects/${P}/databases/${DB}\",title=test-db-only,description=cloud test gate only"
echo "== Firestore roles, limited to the test database by an IAM condition"
for ROLE in roles/datastore.user roles/datastore.indexAdmin; do
  run gcloud projects add-iam-policy-binding "$P" --member="serviceAccount:${SA}" --role="$ROLE" \
      --condition="$COND" --quiet
done

echo "== Owner may impersonate the test service account (local make test-cloud)"
run gcloud iam service-accounts add-iam-policy-binding "$SA" --member="user:${OWNER}" \
    --role=roles/iam.serviceAccountTokenCreator --project="$P" --quiet

echo "== Versioned test bucket (objects and old versions deleted after 1 day)"
if exists gcloud storage buckets describe "gs://${BUCKET}" --project="$P"; then
  echo "  exists: gs://${BUCKET}"
else
  run gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access --project="$P"
fi
LIFECYCLE="$(mktemp)"
cat > "$LIFECYCLE" <<'JSON'
{"rule": [
  {"action": {"type": "Delete"}, "condition": {"age": 1}},
  {"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 1}}
]}
JSON
run gcloud storage buckets update "gs://${BUCKET}" --versioning --lifecycle-file="$LIFECYCLE" --project="$P"
run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${SA}" \
    --role=roles/storage.objectAdmin --project="$P"

echo "== Paused test Cloud Tasks queue (tasks are created, never dispatched)"
if exists gcloud tasks queues describe "$QUEUE" --location="$REGION" --project="$P"; then
  echo "  exists: $QUEUE"
else
  run gcloud tasks queues create "$QUEUE" --location="$REGION" --project="$P"
fi
run gcloud tasks queues pause "$QUEUE" --location="$REGION" --project="$P"
run gcloud tasks queues add-iam-policy-binding "$QUEUE" --location="$REGION" --member="serviceAccount:${SA}" \
    --role=roles/cloudtasks.admin --project="$P"

if [ "$WITH_CI" = 1 ]; then
  need GITHUB_REPO; need TEST_WIF_POOL; need TEST_WIF_PROVIDER_ID
  POOL="$TEST_WIF_POOL"; PROVIDER="$TEST_WIF_PROVIDER_ID"
  PNUM="$(gcloud projects describe "$P" --format='value(projectNumber)')"
  echo "== GitHub Actions Workload Identity (repository ${GITHUB_REPO}, main branch only)"
  if exists gcloud iam workload-identity-pools describe "$POOL" --location=global --project="$P"; then
    echo "  exists: pool $POOL"
  else
    run gcloud iam workload-identity-pools create "$POOL" --location=global --display-name="alekbot test gate CI" \
        --project="$P"
  fi
  if exists gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" \
      --location=global --project="$P"; then
    echo "  exists: provider $PROVIDER"
  else
    run gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --workload-identity-pool="$POOL" \
        --location=global --issuer-uri="https://token.actions.githubusercontent.com" \
        --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
        --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'" \
        --project="$P"
  fi
  run gcloud iam service-accounts add-iam-policy-binding "$SA" --role=roles/iam.workloadIdentityUser \
      --member="principalSet://iam.googleapis.com/projects/${PNUM}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
      --project="$P" --quiet
  echo "  GitHub repository secrets (masked in public logs): TEST_WIF_PROVIDER, TEST_SA_EMAIL"
  run gh secret set TEST_WIF_PROVIDER --repo "$GITHUB_REPO" \
      --body "projects/${PNUM}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
  run gh secret set TEST_SA_EMAIL --repo "$GITHUB_REPO" --body "$SA"
fi

rm -f "$LIFECYCLE"
echo
echo "Add to .env if not there yet:  TEST_SA_EMAIL=${SA}"
[ "$APPLY" = 1 ] || echo "(dry run: nothing was changed; re-run with --apply)"
