#!/usr/bin/env bash
# Build / push / deploy the untrusted-content sandbox light (parse) service (cloudbuild-dev.yaml, the four "sandbox-light" steps).
# docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.3, §4.12, §11 (light row).
#
# Usage: cloudbuild_sandbox_light.sh pull|build|push|deploy
#
# Lives in a file because Cloud Build limits each step arg to 10 000 characters and substitutions are NOT applied inside
# files: the step passes PROJECT_ID, SANDBOX_LIGHT_SA, SANDBOX_LIGHT_URL, SANDBOX_LIGHT_CALLERS_MAIN and
# SANDBOX_LIGHT_CALLERS_INDEXER through `env:`. Nothing is hardcoded here (secrets rule).
#
# Every mode is a no-op while SANDBOX_LIGHT_SA is empty: the service identity does not exist yet (first deploy), and an
# empty --service-account= would fail every `make deploy` of the bot. The sandbox steps also run after the indexer steps,
# so a failing sandbox step never blocks a hotfix of the bot.
#
# Identity fence (ruling R11): the light service runs as a dedicated zero-role service account whose local part starts with
# `alek-sandbox-`. Any other SANDBOX_LIGHT_SA (e.g. the main runtime's, from a mis-set .env value) is refused with a
# non-zero exit before any docker or gcloud call, so the "holds nothing" service can never be given real IAM by accident.
#
# Network (RFC §4.5, S6): SANDBOX_VPC_NETWORK and SANDBOX_VPC_SUBNET come from the step env; deploy refuses when either is empty.
#
# The service holds no secret (nothing is mounted from Secret Manager; --clear-secrets makes every deploy drop any secret
# attached by hand), admits authenticated callers only (the platform's invoker check stays on) and gets no public IAM binding.
set -euo pipefail

mode="${1:?usage: cloudbuild_sandbox_light.sh pull|build|push|deploy}"
case "$mode" in pull|build|push|deploy) ;; *) echo "unknown mode: $mode" >&2; exit 2 ;; esac

if [ -z "${SANDBOX_LIGHT_SA:-}" ]; then
  echo "sandbox light ($mode): SANDBOX_LIGHT_SA is empty, skipping (the service identity is not set up yet)"
  exit 0
fi

case "${SANDBOX_LIGHT_SA%%@*}" in
  alek-sandbox-?*) ;;
  *)
    echo "sandbox light ($mode): refusing SANDBOX_LIGHT_SA: its name must start with 'alek-sandbox-' (a dedicated zero-role account)" >&2
    exit 3
    ;;
esac

: "${PROJECT_ID:?PROJECT_ID must be set by the Cloud Build step env}"
# No-egress lock-down (RFC §4.5, S6): the service deploys onto Direct VPC egress `all-traffic` in a custom VPC with no NAT and a
# deny-all egress firewall. Fail closed: a deploy without the network would silently leave the internet open.
if [ "$mode" = "deploy" ]; then
  for var in SANDBOX_VPC_NETWORK SANDBOX_VPC_SUBNET; do
    if [ -z "${!var:-}" ]; then
      echo "sandbox light (deploy): $var is empty: refusing to deploy with open egress (docs/07_deployment/README.md § Sandbox network)" >&2
      exit 4
    fi
  done
fi
SERVICE="alek-bot-sandbox-light-dev"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:latest"
# The classic builder builds every stage of Dockerfile.sandbox up to the target, so `--target light` also builds the
# `render` stage before it. Without the render image as a second cache source that stage (Node, npm, puppeteer's Chromium)
# was rebuilt on every deploy (~4 min). A missing image is no error: the build just runs without that cache.
RENDER_IMAGE="gcr.io/${PROJECT_ID}/alek-bot-sandbox-render-dev:latest"
# Size of the in-memory work volume. Instance memory is 2Gi and the volume counts against it. Worst case on the volume:
# the request body (<= REQUEST_CAP_BYTES, 101 MiB) + the response windows (<= RESPONSE_CAP_BYTES, 101 MiB) + one child
# output file (<= 64 MiB FSIZE) = 266 MiB, rounded to 320Mi. With the 101 MiB copy-out, the 1 GiB child and the server
# process, the instance stays under 2 GiB. A fuller volume ends as the child's `error` outcome.
WORK_VOLUME_SIZE="${SANDBOX_WORK_VOLUME_SIZE:-320Mi}"

case "$mode" in
  pull)
    docker pull "$IMAGE" || true   # first deploy has no image to cache from
    docker pull "$RENDER_IMAGE" || true   # cache for the render stage the classic builder builds on the way
    ;;
  build)
    docker build -f Dockerfile.sandbox --target light --cache-from "$RENDER_IMAGE" --cache-from "$IMAGE" -t "$IMAGE" .
    ;;
  push)
    docker push "$IMAGE"
    ;;
  deploy)
    gcloud run deploy "$SERVICE" \
      --image="$IMAGE" \
      --region=us-central1 \
      --network="$SANDBOX_VPC_NETWORK" \
      --subnet="$SANDBOX_VPC_SUBNET" \
      --vpc-egress=all-traffic \
      --service-account="$SANDBOX_LIGHT_SA" \
      --no-allow-unauthenticated \
      --clear-secrets \
      --no-cpu-throttling \
      --use-http2 \
      --execution-environment=gen2 \
      --concurrency=1 \
      --min-instances=0 \
      --max-instances=5 \
      --memory=2Gi \
      --cpu=2 \
      --timeout=900 \
      --add-volume=name=sandbox-work,type=in-memory,size-limit="$WORK_VOLUME_SIZE" \
      --add-volume-mount=volume=sandbox-work,mount-path=/work \
      --set-env-vars="APP_ENV=development,SANDBOX_SERVICE=light,SANDBOX_RECYCLE=abnormal,SANDBOX_URL=${SANDBOX_LIGHT_URL:-},SANDBOX_CALLERS_MAIN=${SANDBOX_LIGHT_CALLERS_MAIN:-},SANDBOX_CALLERS_INDEXER=${SANDBOX_LIGHT_CALLERS_INDEXER:-}"
    ;;
esac
