# Bootstrap — deploying Alek-Core from scratch

How to stand up Alek-Core locally and on GCP. Solo-developer setup; one live environment
(the `development_`-prefixed collections in the `us-production` Firestore database).

> **Secrets:** every credential lives in `.env` (gitignored) or `config/secrets/` (gitignored)
> or GCP Secret Manager. Never commit real values. Start from [`.env.example`](.env.example).

---

## 1. Prerequisites

- **Python 3.11** (matches the Docker base image `python:3.11-slim`)
- **Node.js 18+** — for the document-generation subprocesses (`docx_generator/`, `pdf_generator/`)
- **gcloud CLI** authenticated against your GCP project (`gcloud auth login` +
  `gcloud auth application-default login`)
- A GCP project with: Firestore (named database `us-production`), Cloud Run, Cloud Tasks,
  Cloud Scheduler, Cloud Build enabled
- Firebase Admin service-account key → `config/secrets/firebase-admin-key.json`

---

## 2. Local setup

```bash
git clone <repo> && cd alekbot-core

python3.11 -m venv venv
source venv/bin/activate
make install-dev          # prod deps + pytest/black/flake8/mypy

# Node deps for document generation
(cd docx_generator && npm install --omit=dev)
(cd pdf_generator  && npm install --omit=dev)   # downloads Chromium (~170 MB)

cp .env.example .env      # then fill in real values — see §3
```

---

## 3. Configure `.env`

Copy [`.env.example`](.env.example) and fill it in. Minimum to boot locally:

- `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, `FIRESTORE_DATABASE=us-production`
- `GEMINI_API_KEY` (default provider; others optional)
- One channel: `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` (Socket Mode) **or** `TELEGRAM_BOT_TOKEN`

The full annotated list (LLM providers, auth, Gmail/Tasks integrations, MCP, optional features)
is in `.env.example`.

---

## 4. Firestore indexes

Composite + vector indexes are defined in [`config/firestore.indexes.json`](config/firestore.indexes.json).

```bash
make deploy-indexes       # python scripts/infrastructure/deploy_firestore_indexes.py
```

Index builds are asynchronous — vector search returns errors until they finish (minutes).

---

## 5. Test & lint

```bash
make check                # ruff lint + unit/architecture tests — the CI gate, run before every commit
make test                 # full suite
```

There is no local run mode: several capabilities (Cloud Tasks, schedulers, GCS, Cloud Run
Jobs) need the cloud environment.

---

## 6. Deploy to Cloud Run

Single live environment; deploy is manual by choice (see
[`docs/04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md`](docs/04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md)).

```bash
make deploy               # build + deploy alek-bot-dev (cloudbuild-dev.yaml)
```

Cloud Scheduler jobs (consolidation watchdog, daily email review, reminder firing,
subscription renewal, billing summary) are documented in
[`docs/07_deployment/SCHEDULERS.md`](docs/07_deployment/SCHEDULERS.md). Keep-alive setup:
[`docs/07_deployment/KEEP_ALIVE_SETUP.md`](docs/07_deployment/KEEP_ALIVE_SETUP.md).

---

## 7. Verify

```bash
curl -s "$SERVICE_URL_DEV/health"          # → ok
make logs                                  # recent Cloud Run logs
```

Send a message to the connected Slack/Telegram channel; confirm a reply and, after the
consolidation window, that new facts appear in `development_domain_facts_v2`.

---

See [`docs/07_deployment/`](docs/07_deployment/) for schedulers, logging, and cost notes,
and [`docs/08_concepts/DATABASE_SCHEMA.md`](docs/08_concepts/DATABASE_SCHEMA.md) for the
collection/index reference.
