# Logging Guide

How logs are structured, where to find them, and how to query them — for both the developer and AI agents working on this codebase.

---

## Architecture

| Environment | Handler | Format | Destination |
|-------------|---------|--------|-------------|
| **Local dev** | `StreamHandler` (INFO) + `FileHandler` (DEBUG) | Human-readable text | stdout + `alek_debug.log` |
| **Cloud Run** | `StructuredLogHandler` (INFO) | JSON (one object per line) | stdout → Cloud Logging |

**Detection:** `K_SERVICE` env var — automatically set by Cloud Run runtime, absent locally. No manual configuration needed.

**Privacy:** `DEBUG_PROMPTS` defaults to `false` in all cloud deployments. Prompt and response files are never written in cloud.

---

## Local Development

### Console output
```
📝 Debug logging enabled: alek_debug.log
🔍 [GrokAdapter] Request: model=grok-4... | trace_id=tr_abc session_id=sess_xyz user_id=U123
```

### Debug file
`alek_debug.log` — DEBUG level, full detail with timestamps and line numbers:
```
2026-02-18 14:32:01 | DEBUG    | src.adapters.grok_adapter | generate_content:174 | ...
```

### Prompt/response files
Enable with `DEBUG_PROMPTS=true` in `.env` → saved to `debug_prompts/` directory.

---

## Cloud Run (Production / Development environments)

### Log format

Each `logger.info(...)` / `logger.error(...)` call produces one JSON line:

```json
{
  "severity": "INFO",
  "message": "🔍 [SmartResponseAgent] Turn 2 - deliver_response received",
  "logging.googleapis.com/labels": {
    "user_id": "U0123ABC",
    "session_id": "slack_U0123ABC_thread_456",
    "event_id": "evt_xyz",
    "trace_id": "tr_abc123"
  }
}
```

Severity is parsed automatically by Cloud Logging — `logger.error()` → red `ERROR`, `logger.warning()` → yellow `WARNING`.

---

## Viewing Logs — For the Developer

### Option 1: Cloud Logging Console (UI)

1. Open [Cloud Logging Console](https://console.cloud.google.com/logs)
2. Select project: `$PROJECT_ID`
3. Use Log Explorer with filters:

```
resource.type="cloud_run_revision"
resource.labels.service_name="alek-bot-dev"
```

**Filter by severity:**
```
resource.type="cloud_run_revision" AND severity>=ERROR
```

**Filter by user:**
```
resource.type="cloud_run_revision" AND labels.user_id="U0123ABC"
```

**Filter by session:**
```
labels.session_id="slack_U0123ABC_thread_456"
```

### Option 2: Make commands

```bash
# Tail dev service logs (real-time)
make logs-dev

# Tail prod service logs (real-time)
make logs
```

### Option 3: gcloud CLI

```bash
# Last 50 errors from dev service
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="alek-bot-dev" AND severity>=ERROR' \
  --project=$PROJECT_ID \
  --limit=50 \
  --format="table(timestamp,severity,textPayload)"

# All logs for a specific user (last hour)
gcloud logging read \
  'labels.user_id="U0123ABC"' \
  --project=$PROJECT_ID \
  --freshness=1h \
  --limit=100

# All logs for a specific session
gcloud logging read \
  'labels.session_id="slack_U0123ABC_thread_456"' \
  --project=$PROJECT_ID \
  --limit=200 \
  --format=json
```

---

## Viewing Logs — For AI Agents (Claude)

When debugging a production issue, Claude can query Cloud Logging directly via `gcloud` CLI. The machine is pre-authenticated (your GCP account, configured project).

### Standard queries

```bash
# Recent errors — dev environment
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="alek-bot-dev" AND severity>=ERROR' \
  --project=$PROJECT_ID --limit=30 --format=json

# Recent errors — prod environment
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="alek-bot-prod" AND severity>=ERROR' \
  --project=$PROJECT_ID --limit=30 --format=json

# All logs for a specific user (last 2 hours)
gcloud logging read \
  'labels.user_id="<USER_ID>"' \
  --project=$PROJECT_ID --freshness=2h --limit=200 --format=json

# All logs for a specific session
gcloud logging read \
  'labels.session_id="<SESSION_ID>"' \
  --project=$PROJECT_ID --limit=500 --format=json
```

### Grep through log messages
```bash
# Find specific adapter errors
gcloud logging read \
  'resource.labels.service_name="alek-bot-dev" AND textPayload=~"GrokAdapter"' \
  --project=$PROJECT_ID --limit=50 --format=json
```

### Service names

| Make target | Cloud Run service name |
|-------------|----------------------|
| `make deploy-dev` | `alek-bot-dev` |
| `make deploy` | `alek-bot-prod` |

---

## Cloud Run Jobs (Deep Research)

Deep research runs in Cloud Run Jobs (`job_main.py`), not in the service. Job logs use a
**different resource type** — `cloud_run_job` — and are NOT captured by `make logs-dev-tail`.

### Make commands

```bash
# Tail research job logs (real-time)
make logs-research-job-dev-tail
```

### gcloud queries

```bash
# Recent job logs
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="alek-research-job-dev"' \
  --project=$PROJECT_ID --limit=50 --format="value(textPayload)"

# Errors only
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="alek-research-job-dev" AND severity>=ERROR' \
  --project=$PROJECT_ID --limit=20 --format="value(textPayload)"
```

### Cloud Logging Console filter

```
resource.type="cloud_run_job"
resource.labels.job_name="alek-research-job-dev"
```

### Debug prompts (GCS)

When `DEBUG_PROMPTS=true` and `DEBUG_PROMPTS_BUCKET` is set, the runner saves full prompts
and responses to GCS after each research pass:

```
gs://{DEBUG_PROMPTS_BUCKET}/claude_deep_research_runner/{date}/{ts}_prompt.txt
gs://{DEBUG_PROMPTS_BUCKET}/claude_deep_research_runner/{date}/{ts}_response.txt
```

Two pairs per execution (first pass + second-pass critic), saved at `end_turn` or `max_tokens`.

```bash
# List today's debug files
gsutil ls gs://$(DEBUG_PROMPTS_BUCKET)/claude_deep_research_runner/$(date +%Y-%m-%d)/

# Read a response
gsutil cat gs://$(DEBUG_PROMPTS_BUCKET)/claude_deep_research_runner/$(date +%Y-%m-%d)/{ts}_response.txt
```

---

## Code Reference

| File | Role |
|------|------|
| `src/utils/logger.py` | Logger setup — detects Cloud Run, chooses handler |
| `src/utils/logging_context.py` | Per-request context vars (user_id, session_id, event_id) |
| `src/utils/telemetry.py` | OpenTelemetry + Cloud Trace integration |
| `src/utils/debug_logger.py` | Local prompt/response file logger (`DEBUG_PROMPTS=true`) |

---

**Last Updated:** 2026-02-18
