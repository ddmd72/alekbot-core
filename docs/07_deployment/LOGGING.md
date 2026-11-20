<!-- living-doc
covers: none
-->

# Logging Guide

How to read what the system recorded. Four sources, each answering a different question; pick by the question, not by
habit. The short version for agents is in the root `CLAUDE.md` § Debugging Cloud Run; this file holds the detail.
Project ID, service and user IDs come from `.env` / memory, never from this file.

| Question | Source |
|---|---|
| What happened, errors, tracebacks, control flow | Cloud Logging (this file, below) |
| The actual prompt and response of an LLM call, tokens, history over many turns | BigQuery `prompt_content` |
| Who delegated to whom, what each call got and returned, failed calls | Logfire |
| Memory over time per process | BigQuery `alek_memory_dev` (`docs/05_building_blocks/memory_telemetry/README.md`) |

The content stores (BigQuery, Logfire) and their sensitivity rules are described in
[`observability_strategy`](../05_building_blocks/observability_strategy/README.md).

## How a log line is produced

`src/utils/logger.py::setup_logger` picks the handler by `K_SERVICE` (set by Cloud Run, absent locally):

- **Cloud Run:** `StructuredLogHandler` at INFO writes one JSON object per line to stdout. `_CloudContextFilter` adds
  the labels `user_id`, `session_id`, `event_id`, `trace_id` (from `src/utils/logging_context.py` and the active span),
  so they are filterable as `labels.user_id=...` in Cloud Logging. Only INFO and above reach it.
- **Local:** INFO to the console through `AlekFormatter`, DEBUG to `alek_debug.log` (truncated on every start).

Traps:
- `LOG_TRACE_CONTEXT` (`make logs-mode-clean` / `logs-mode-full`) is read only by the local `AlekFormatter`. On Cloud
  Run no formatter reads it, so those two targets change nothing in the logs.
- `make fetch-logs` and the other `fetch-logs*` targets write the `textPayload` of each entry. Once fetched, grep the
  local file instead of looping `gcloud logging read`.

## Cloud Logging

The `Makefile` targets use `SERVICE_NAME`, `RESEARCH_JOB`, `VOICE_RELAY`, `INDEXER` (the `*-dev` names; there is one
live environment) and `K` (default 300). All run against `<PROJECT_ID>` from `.env`.

| Workload | View | Fetch to a local file | Live |
|---|---|---|---|
| Main service | `make logs` | `make fetch-logs` -> `alek_debug.log` | `make logs-tail` (`logs-perf`: timing lines only) |
| Deep-research job | `make logs-job` | `make fetch-logs-job` -> `alek_debug_job.log` | none |
| Voice relay | `make logs-relay` | `make fetch-logs-relay` -> `alek_debug_relay.log` | none |
| Drive indexer | `make logs-indexer` | `make fetch-logs-indexer` -> `alek_debug_indexer.log` | none |

A single job execution: `make list-jobs`, then `make logs-execution EXECUTION=<name>`. Job logs have resource type
`cloud_run_job`, so `logs-tail` (which filters `cloud_run_revision`) never shows them.

Filters for the Log Explorer or `gcloud logging read '<filter>' --project=<PROJECT_ID> --limit=N --format=json`:

```
resource.type="cloud_run_revision" AND resource.labels.service_name="<SERVICE_NAME>" AND severity>=ERROR
labels.user_id="<USER_ID>"
labels.session_id="<SESSION_ID>"
resource.type="cloud_run_job" AND resource.labels.job_name="<RESEARCH_JOB>"
```

`session_id` is `f"{user_id}:{channel_id}"` (`docs/08_concepts/DATABASE_SCHEMA.md`). Add `--freshness=2h` to bound a
user or session query.

### Provider failover and breaker lines

`BaseAgent` and the breaker log these as message text, with the agent and the `<from>-><to>` providers inside it:
`llm_fallback`, `llm_fallback_ok`, `llm_fallback_failed`, `llm_fallback_refused`, `llm_no_fallback`, `llm_both_open`
(`src/agents/base_agent.py`), `provider_breaker_open provider=... agent=...` (the agent whose failure tripped it) and
`provider_breaker_closed` (`src/adapters/in_memory_provider_resilience.py`).

```bash
grep 'llm_fallback\|llm_no_fallback\|llm_both_open\|provider_breaker_' alek_debug.log   # add: | grep agent=<type>
```

### Document service and reader lines

- `[DocumentService] read <status>: kind=... store=... caller=... detail=... request=...` is the one failure line of the
  document service (`src/services/document_service.py::DocumentService._done`): ERROR for the systemic statuses
  (`_ERROR_STATUSES`), WARNING otherwise.
- A failed provider call of the document reader leaves no span content; it leaves one WARNING
  `llm_call_failed provider=<p> error_type=<t> window=reader` (`src/agents/drive_document_reader_agent.py`).

## LLM content: BigQuery `prompt_content`

One row per successful LLM call through `LLMPort`, written by `BaseAgent._call_llm`, day-partitioned with a 30-day
expiry (`src/adapters/bigquery_prompt_content_adapter.py`). A failed call has no row; look for it in Logfire.

- Capture is on only when `DEBUG_PROMPTS=true` and `BIGQUERY_PROMPT_DATASET` are both set
  (`src/composition/service_container.py::ServiceContainer._init_prompt_content_store`). `DEBUG_PROMPTS` defaults to
  `false` (`src/config/settings.py`); `cloudbuild-dev.yaml` sets both for the service and the job. Locally set both in
  `.env` to capture.
- Read it with `bq query --project_id=<PROJECT_ID> --use_legacy_sql=false --format=json`. Use JSON: `request_text` is
  multi-line and breaks CSV. Columns and the recording rules are in the observability block.
- `trace_id` carries a `tr_` prefix (`src/utils/telemetry.py`); strip it before joining to Logfire. The stored
  `span_id` is the enclosing span, and there is no `parent_span_id`, so BigQuery cannot answer relationship questions.
- The deep-research runner calls the Claude SDK directly, bypasses `LLMPort` and writes no row. It logs a
  summary-only INFO line per pass (`src/agents/base_agent.py::BaseAgent._debug_raw_turn`), found in the job logs.

## Logfire: the cascade

Logfire holds the same content plus the call tree. Use it for anything about relationships between calls and for failed
calls. It is on when `TRACING_BACKEND` is `logfire` or `both` and `LOGFIRE_CAPTURE_CONTENT=true`
(`src/utils/telemetry.py`; the capture flag is a kill switch that drops back to metadata-only without a deploy). Design
and threat model: `docs/04_solution_strategy/decisions/logfire_prompt_content_capture.md`.

Traps:
- Gemini SDK spans are off while google-genai is 3.x or newer
  (`src/utils/telemetry.py::google_genai_instrumentation_decision`). Gemini turns carry only our own `llm.call` spans;
  their content is in BigQuery.
- File parts of an LLM call (images, PDFs sent natively) are replaced by `{"type": "file_omitted", ...}` in the exported
  `gen_ai.*` attributes (`src/utils/logfire_redacting_exporter.py`). The document reader's provider calls run with SDK
  instrumentation suppressed (`src/utils/telemetry.py::sdk_spans_suppressed`), so a reader call shows only our
  `llm.call` span.

Logfire exposes an MCP server, so an agent session can run SQL over the telemetry. That is local tooling, not a
project dependency: nothing is added to `requirements.txt` or the container.
