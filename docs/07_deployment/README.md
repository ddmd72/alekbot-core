# Deployment Documentation

Quick reference for deploying and managing Alek-Core.

> First-time setup? See [`../../BOOTSTRAP.md`](../../BOOTSTRAP.md) for the full from-scratch runbook
> (prerequisites, local env, Firestore indexes, deploy). This file is the operational reference.

---

## Available Guides

### Core Deployment

- **[SCHEDULERS.md](SCHEDULERS.md)** - All Cloud Scheduler jobs: schedule, payload, purpose, cost
- **[KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md)** - Cloud Scheduler setup to prevent scale-to-zero ($0.10/month)
- **[LOGGING.md](LOGGING.md)** - Structured logging setup, Cloud Logging queries for developer and AI agents

### Setup

- **[../../BOOTSTRAP.md](../../BOOTSTRAP.md)** — from-scratch deployment runbook (prerequisites,
  local env, `.env`, Firestore indexes, Cloud Run deploy, verify)

---

## Quick Commands

### Deploy

Single live environment; deploy is manual by choice (see
[`../04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md`](../04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md)).

```bash
make deploy   # build + deploy alek-bot-dev (cloudbuild-dev.yaml)
```

### Setup Keep-Alive (Recommended)

```bash
export GCP_PROJECT_ID="your-project-id"
export CLOUD_RUN_URL="https://your-service.run.app"

./scripts/infrastructure/setup-keep-alive.sh
```

### Check Service Status

```bash
# Cloud Run service (region: us-central1; service: alek-bot / alek-bot-dev)
gcloud run services describe alek-bot --region=us-central1

# Cloud Scheduler jobs
gcloud scheduler jobs list --location=us-central1

# Recent logs
gcloud logging read 'resource.type=cloud_run_revision' --limit=50
```

---

## Cloud Run Deploy Units

All three units share one image (`gcr.io/$PROJECT_ID/alek-bot-dev:latest`) and one build config
(`cloudbuild-dev.yaml`, deployed together by a single `make deploy`); a job/service is distinguished
only by `--command`/`--args` at deploy time.

| Unit | Type | Region | Timeout | Scaling | Secrets it reads | `make` targets |
|------|------|--------|---------|---------|-------------------|-----------------|
| `alek-bot-dev` | Cloud Run service | us-central1 | 1800s | min=0, max=1 | ~30 (Slack, Telegram, LLM providers, OAuth, etc. — see cloudbuild-dev.yaml) | `logs`, `fetch-logs`, `logs-tail` |
| `alek-research-job-dev` | Cloud Run Job | us-central1 | task-timeout 18000s | on-demand (no min/max-instances — Jobs run to completion) | `ANTHROPIC_API_KEY`, `SERVICE_ACCOUNT_EMAIL` | `logs-job`, `fetch-logs-job`, `list-jobs`, `logs-execution`, `cancel-job` |
| `alek-voice-relay-dev` | Cloud Run service | us-central1 | **3600s** | min=0, max=1 | `OPENAI_API_KEY`, `BILLING_SLACK_WEBHOOK_URL` | `logs-relay`, `fetch-logs-relay` |

**`alek-voice-relay-dev`** (added for Voice Companion Slice 1, see
[`../10_rfcs/VOICE_COMPANION_RFC.md`](../10_rfcs/VOICE_COMPANION_RFC.md) §4.14) is `relay_main.py` on
the same image, running as a plain `websockets` server that speaks Twilio Media Streams and drives
`VoiceSessionService` for the lifetime of one phone call — hence the 3600s timeout (vs. the main
service's 1800s) and its own Cloud Run service rather than folding into `alek-bot-dev` (an open
WebSocket is one long *billed* request under Cloud Run's request-based CPU/memory billing, and
`alek-bot-dev` still needs to serve Slack/Telegram concurrently on a 1 vCPU box).

- **`--allow-unauthenticated`** — required, not a dev convenience like `alek-bot-dev`'s: Twilio's
  Media Streams WebSocket is the relay's only inbound caller and cannot present a Google-signed OIDC
  token, so Cloud Run ingress must stay open. The relay's *outbound* calls back into the main
  service's `/voice/session-config` and `/voice/submit-transcript` are still OIDC-protected — see
  next point.
- **No dedicated service account** — the relay runs as the same default compute service account as
  `alek-bot-dev`/`alek-research-job-dev` (neither of those overrides `--service-account` either).
  This is required, not just simplest: `relay_main.py` mints an OIDC identity token from its own
  Cloud Run runtime identity to call the main service, and `main.py` checks that token with the exact
  same `verify_worker_oidc(token, expected_sa_email=config.get("SERVICE_ACCOUNT_EMAIL"))` gate used
  for `/worker` — one shared expected email for every caller. A separate relay service account would
  401 every one of those calls unless `SERVICE_ACCOUNT_EMAIL` were also changed, which is out of
  scope here.
- **`min-instances=0`** — genuinely undecided, not a placeholder: RFC §9 open question 9 notes the
  main service is kept warm by a Cloud Scheduler keep-alive ping today, and the relay has no
  equivalent yet. Revisit once real call volume exists.
- **No `TWILIO_*` secrets** — those belong to the *main* service's webhook/telephony adapter
  (`src/web/voice_webhook_app.py`, `src/adapters/twilio_telephony_adapter.py`), not the relay;
  `relay_main.py` never imports the `twilio` package.

### Voice Relay Stream URL — two-pass first deploy

The main service must hand Twilio the relay's WebSocket URL (`<Connect><Stream url="wss://…">` in
`/voice/answer`), read from `VOICE_RELAY_STREAM_URL`. That value is genuinely a chicken-and-egg
problem, not something code can resolve: the relay's Cloud Run URL does not exist until the relay
has been deployed at least once, and both units deploy from the same `make deploy`.

**Until `VOICE_RELAY_URL_DEV` is set in `.env`, every call fails with an empty stream URL** — the
callback is answered, the persona is assembled, and the `<Stream>` points nowhere.

1. **Deploy once.** `make deploy` creates `alek-voice-relay-dev` (the main service ships with an
   empty stream URL on this pass — expected).
2. **Read the relay's URL back.** Cloud Run service URLs are stable once the service exists, so
   this is a one-time step:
   ```bash
   gcloud run services describe alek-voice-relay-dev \
     --region=us-central1 --format='value(status.url)'
   ```
3. **Put it in `.env`** as `VOICE_RELAY_URL_DEV=<that https:// URL>`. Keep the `https://` form —
   the `deploy` target converts it to `wss://` via `patsubst` and passes it to Cloud Build as
   `_VOICE_RELAY_URL`, which `cloudbuild-dev.yaml` sets as `VOICE_RELAY_STREAM_URL` on the main
   service. It follows `SERVICE_URL_DEV`'s convention exactly: a human-maintained `.env` value
   reaching the deploy only through a substitution, never a shared key read directly from local
   `.env` (CLAUDE.md, "Deploy-substitution trap").
4. **`make deploy` again.** The main service now picks it up.

### Twilio number configuration (Lelik's number)

Set these by hand in the Console. The classic REST `IncomingPhoneNumbers` write did not reliably
reach live routing (spike 0.6):
- **Active Region: United States (US1).** Webhook signatures use the auth token of the region that
  processes the call, and `TWILIO_AUTH_TOKEN` is the US1 token. In IE1, every `/voice/auth` 403s.
- **A call comes in:** Webhook, `https://<SERVICE_URL_DEV>/voice/auth`, POST.
- **Call status changes:** `https://<SERVICE_URL_DEV>/voice/inbound-status`, POST. The callback is
  placed only once the inbound dial reports `completed`. Without this URL the dial is answered and
  hung up, and nobody ever calls back.

This is the **fourth** prerequisite for the voice companion's live verification, alongside: Twilio
secrets present in Secret Manager, the four Firestore prompt uploads for Lelik's persona
(`COGNITIVE_PROCESS_LELIK`, `SPOKEN_DELIVERY`, `lelik_agent_v1`, `lelik`; the character tokens are
Smart's existing ones), and the four for the end-of-call summarizer
(`COGNITIVE_PROCESS_LELIK_SUMMARIZER`, `lelik_summarizer_agent_v1`, `lelik_summarizer`).

---

## Cost Optimization

| Strategy             | Cost/Month | Pros        | Cons                      |
| -------------------- | ---------- | ----------- | ------------------------- |
| **Scale-to-zero**    | $0         | Cheapest    | Cold starts (2-4s)        |
| **Keep-alive pings** | $0.10-1    | Almost free | May still get cold starts |
| **min-instances=1**  | $15-30     | Always warm | Expensive                 |

**Recommendation:** Start with keep-alive pings ($0.10/mo), upgrade to min-instances if needed.

---

## Node.js Dependencies in the Docker Image

The image bundles two independent Node.js projects for document generation. Both are installed
during the Docker build via `npm install --omit=dev`:

| Directory | npm package | Purpose | Notes |
|-----------|------------|---------|-------|
| `docx_generator/` | `docx` | DOCX file generation (NodeDocxRunner) | Lightweight; no system-level dependencies |
| `pdf_generator/` | `puppeteer ^24.x` | PDF rendering via headless Chromium (NodePuppeteerRunner) | Downloads bundled Chromium (~170 MB) during install |

`pdf_generator/node_modules/` is excluded from the Docker build context via `.dockerignore` — the
`npm install` step in the `Dockerfile` installs it fresh inside the image layer.

Because Puppeteer downloads Chromium at install time, the first `docker build` (or Cloud Build)
after a Puppeteer version change will be slow (~3–5 min for the download). Subsequent builds use
the Docker layer cache as long as `pdf_generator/package.json` is unchanged.

---

## Artifact Registry Image Cleanup

The `gcr.io` Artifact Registry repository stores Docker images pushed by `make deploy` via
`cloudbuild-dev.yaml`. Every deploy overwrites the `:latest` tag but leaves the previous digest as
an untagged orphan. Without active cleanup, storage accumulates unbounded: the repo was **~120 GB**
in July 2026 (before cleanup), with 136 dead prod images + 489 dev images, costing ~$10.71/month.

**Automatic cleanup via `set-cleanup-policies` does not work reliably** in the current gcloud CLI
version (578.0.0+), so cleanup is manual:

```bash
# Dry-run: see what would be deleted
python3 scripts/infra/cleanup_gcr_images.py --dry-run

# Actual cleanup: delete untagged images older than 7 days
python3 scripts/infra/cleanup_gcr_images.py
```

The script deletes all untagged images older than 7 days, keeping only recent builds (typically
~10 images = one week of roughly-daily manual deploys), plus any images explicitly tagged with
`:dev` or similar.

**Recommended cadence:** Run monthly or after a week of active development (when `alek-bot-dev`
accumulates untagged digests). The source of truth for the policy is
[`scripts/infra/gcr-io-cleanup-policy.json`](../../scripts/infra/gcr-io-cleanup-policy.json)
(currently a placeholder for future `set-cleanup-policies` adoption; the Python script is the
working implementation).

---

## Related Documentation

- **[06_runtime/README.md](../06_runtime/README.md)** - Runtime architecture
- **[05_building_blocks/](../05_building_blocks/)** - Component documentation
- **[12_risks/IMPLEMENTATION_ROADMAP.md](../12_risks/IMPLEMENTATION_ROADMAP.md)** - Development roadmap

---

**Last Updated:** 2026-03-14
