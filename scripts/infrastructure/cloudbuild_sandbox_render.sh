#!/usr/bin/env bash
# Build / push / deploy the untrusted-content sandbox render service (cloudbuild-dev.yaml, the four "sandbox" steps).
# docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.3, §4.12; plan G17.
#
# Usage: cloudbuild_sandbox_render.sh pull|build|push|deploy
#
# Lives in a file because Cloud Build limits each step arg to 10 000 characters and substitutions are NOT applied inside
# files: the step passes PROJECT_ID, SANDBOX_RENDER_SA, SANDBOX_RENDER_URL and SANDBOX_RENDER_CALLERS through `env:`.
# Nothing is hardcoded here (secrets rule).
#
# Every mode is a no-op while SANDBOX_RENDER_SA is empty: the service identity does not exist yet (first deploy), and an
# empty --service-account= would fail every `make deploy` of the bot. The sandbox steps also run after the indexer steps,
# so a failing sandbox step never blocks a hotfix of the bot.
#
# The service holds no secret (nothing is mounted from Secret Manager), admits authenticated callers only (the platform's
# invoker check stays on) and gets no public IAM binding.
set -euo pipefail

mode="${1:?usage: cloudbuild_sandbox_render.sh pull|build|push|deploy}"
case "$mode" in pull|build|push|deploy) ;; *) echo "unknown mode: $mode" >&2; exit 2 ;; esac

if [ -z "${SANDBOX_RENDER_SA:-}" ]; then
  echo "sandbox render ($mode): SANDBOX_RENDER_SA is empty, skipping (the service identity is not set up yet)"
  exit 0
fi

: "${PROJECT_ID:?PROJECT_ID must be set by the Cloud Build step env}"
SERVICE="alek-bot-sandbox-render-dev"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}:latest"
# Size of the in-memory work volume (instance memory is 2Gi and the engine ceilings are 1 GiB per process, 64 MiB per file);
# S0-f (the spike result) is the authority: update here when it says otherwise.
WORK_VOLUME_SIZE="${SANDBOX_WORK_VOLUME_SIZE:-256Mi}"

case "$mode" in
  pull)
    docker pull "$IMAGE" || true   # first deploy has no image to cache from
    ;;
  build)
    docker build -f Dockerfile.sandbox --target render --cache-from "$IMAGE" -t "$IMAGE" .
    ;;
  push)
    docker push "$IMAGE"
    ;;
  deploy)
    gcloud run deploy "$SERVICE" \
      --image="$IMAGE" \
      --region=us-central1 \
      --service-account="$SANDBOX_RENDER_SA" \
      --no-allow-unauthenticated \
      --use-http2 \
      --execution-environment=gen2 \
      --concurrency=1 \
      --min-instances=0 \
      --max-instances=3 \
      --memory=2Gi \
      --cpu=2 \
      --timeout=300 \
      --add-volume=name=sandbox-work,type=in-memory,size-limit="$WORK_VOLUME_SIZE" \
      --add-volume-mount=volume=sandbox-work,mount-path=/work \
      --set-env-vars="APP_ENV=development,SANDBOX_RECYCLE=always,SANDBOX_URL=${SANDBOX_RENDER_URL:-},SANDBOX_CALLERS_RENDER=${SANDBOX_RENDER_CALLERS:-}"
    ;;
esac
