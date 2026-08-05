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
