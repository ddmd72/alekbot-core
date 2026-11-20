<!-- living-doc
covers:
  src/utils/logger.py
  src/utils/logging_context.py
  src/utils/telemetry.py
  src/utils/logfire_redacting_exporter.py
  src/adapters/bigquery_prompt_content_adapter.py
  src/ports/prompt_content_store.py
  src/domain/observability.py
  src/domain/log_redaction.py
  src/adapters/firestore_dedup_store.py
  src/ports/dedup_store.py
-->
# Observability

## Purpose

This block is what the system records about itself while it runs, and why each record exists. Four streams, each
answering a different question:

| Stream | Answers | Where it lands |
|---|---|---|
| Logs | what happened, in order, for one request or user | stdout; Cloud Logging on Cloud Run |
| Spans | how long, who called whom, which provider | Logfire and/or Cloud Trace (OpenTelemetry) |
| Prompt content | what exactly was sent to and returned by the model | BigQuery `prompt_content`, expiring by partition |
| Event marks | has this event already been taken | Firestore (not an observability stream; it lives here because it guards Telegram retries and the daily review) |

The streams are joined by `trace_id`: logs and `prompt_content` rows carry it as `tr_` plus 32 hex characters, the
span's own id is the bare hex, so strip the prefix to join to Logfire. **How to read them** (queries, filters, `make fetch-logs`, the Logfire MCP) is
[`LOGGING.md`](../../07_deployment/LOGGING.md); this file is what is recorded and the rules behind it. Memory
telemetry (`alek_memory_dev`) is a separate stream: [`memory_telemetry`](../memory_telemetry/README.md).

Not here: alert rules ([`ALERTING.md`](../../07_deployment/ALERTING.md)), billing (`ledger_scope`, see the billing
decisions), per-agent business logging.

## Owned data

**`prompt_content`** (BigQuery; dataset `BIGQUERY_PROMPT_DATASET`, table `BIGQUERY_PROMPT_TABLE`, default
`prompt_content`). One row per successful LLM call, written by `BigQueryPromptContentAdapter`; the schema is created on
first use (`_ensure_table_sync`) as DAY-partitioned on `timestamp` with the expiry in code
(`_PARTITION_EXPIRATION_MS`), so there is no cleanup job. The row is `domain/observability.py::PromptContentRecord`:
linkage (`trace_id`, `span_id`), who (`user_id`, `account_id`), which agent and model, `turn`, the content
(`request_text`: system instruction plus rendered messages; `response_text`; `thought_text`, the reasoning summary kept
apart from the answer; `tool_calls` as JSON), token counts including cache legs, and `latency_ms`. The `error` column
exists but nothing sets it: a failed call writes no row. Two kinds of row are written outside `_call_llm`: deep-research results
(`record_dr_result`, `agent_type="deep_research"`, the pass number carried in `turn`) and realtime-voice turns
(`record_turn` from `src/web/voice_control_plane_app.py`).

**Spans** are not stored by us: Logfire and Cloud Trace hold them.

**Event marks** (`FirestoreDedupStore`, port `DedupStore`): one document per key, `created_at` plus `expires_at` (the
Firestore TTL field). Two live users: Telegram inline updates (`event_dedup` collection, the store's default `ttl_seconds`; key
`telegram::{update_id}`, `src/adapters/telegram/webhook_adapter.py`) and the daily email review (the
`worker_task_dedup` collection shared with the task claims, `ttl_seconds` set in `application_graph.py`; key `daily_review:{user}:{date}`,
`src/services/start_daily_email_review_task.py`). Slack and Cloud Tasks deliveries are not deduped here (see Flows).

## Flows

### Logs

1. `src/utils/logger.py::setup_logger` runs on import. On Cloud Run (a service's `K_SERVICE` or a job's `CLOUD_RUN_JOB`, `src/utils/cloud_run.py::on_cloud_run`) it installs a
   `StructuredLogHandler` (JSON to stdout) with `_CloudContextFilter`, which attaches `user_id`, `session_id`,
   `event_id`, `trace_id` as Cloud Logging labels (`labels.user_id=...` is how a request is found). Locally it writes a
   console line and `alek_debug.log` (DEBUG, overwritten each start).
2. Only the local console formatter `AlekFormatter` honours `LOG_TRACE_CONTEXT` (`full`, the default, appends the ids;
   `clean` omits them). On Cloud Run the labels are always there.
3. `trace_id` comes from the live OpenTelemetry span, else telemetry's own variable (`telemetry.get_trace_ids`), else
   `logging_context`; `session_id`, `event_id` and `user_id` come from the variables set by
   `logging_context.set_log_context` (Slack intake, `ConversationHandler`). `telemetry.set_request_context` is a second,
   separate set that feeds the prompt-content row's `user_id` (and `trace_id` for logs, as above).
4. `quiet_third_party_loggers` (called by the main service, indexer, job and relay; not the sandbox) holds the HTTP libraries at WARNING: their INFO lines carry full
   URLs, which for Telegram contain the bot token.
5. Lifecycle lines come from `BaseAgent` (`_on_agent_start`, `_on_agent_success`, `_on_agent_error`,
   `_on_delegation`): an agent calls them instead of writing its own start/done/error lines, and does not log an error
   before re-raising (the hook logs it). `_on_agent_error` omits the traceback for provider unavailability and rate
   limits. `logger.*` inside `execute()` is for context the hooks do not carry.

### Spans

1. `main.py::main` calls `telemetry.init_telemetry` first, before the application graph; `job_main.py` calls it too,
   but its deploy sets no `TRACING_BACKEND` (backend `none`), so it exports no spans. The indexer, relay and sandbox
   entry points do not call it.
2. `TRACING_BACKEND` (`cloud_trace`, `logfire`, `both`, `none`) picks the backend (`_resolve_backend`). Unset: Cloud
   Trace when `APP_ENV=production`, otherwise none. `logfire.configure()` swaps the global OpenTelemetry provider, so
   `start_span` call sites do not know the backend; there is no tracing port on purpose. For `both`, Cloud Trace is an
   extra processor on the Logfire provider. If Logfire is requested but unusable (no `LOGFIRE_TOKEN`, not installed),
   `init_telemetry` falls back to Cloud Trace on its own provider, and content capture (below) is then off.
3. Trace context across Cloud Tasks: the Slack adapter calls `inject_trace_headers` when it enqueues and the worker
   calls `extract_context` (`src/adapters/slack/http_adapter.py`). By code reading, the injection runs after the
   `slack.event.received` block has closed and no server span is instrumented, so no `traceparent` is injected and
   `worker.process_event` likely starts a new trace; only the custom `x-trace-id` (`build_trace_id`) crosses. Not
   confirmed in Logfire: check there before relying on one trace spanning intake and worker.
4. Our own spans are created with `telemetry.start_span` (grep it for the current set: Slack intake and worker,
   conversation, delegation, voice, Drive MCP). The one every LLM call adds is `llm.call` (one leaf per LLM call from `BaseAgent._emit_llm_span`: model, agent type, provider,
   turn, tokens, latency; never content).

### Prompt content (two destinations, one switch)

LLM calls go through `BaseAgent._call_llm` (a convention, not enforced by a test: a caller that uses the provider
directly, as `src/services/history_summary_service.py` does, leaves no row and no `llm.call` span). After a successful call it emits the `llm.call` span and then
`PromptContentStore.record_turn`.

- **BigQuery.** The store is built by `ServiceContainer._init_prompt_content_store` only when `DEBUG_PROMPTS=true` AND
  `BIGQUERY_PROMPT_DATASET` is set; otherwise `_call_llm` skips capture entirely. `record_turn` builds the row and
  writes it in a background task: best-effort, never raises, never blocks the LLM path. `user_id` comes from the
  request context, set by the Slack and voice entry points and, for every other path, by `BaseAgent.process` through
  `telemetry.request_user_scope`. A model call made outside `process()` names its user itself with the same scope
  (`src/services/consolidation_service.py`, `src/composition/email_classifier_runner.py`,
  `src/agents/fact_query_slots_agent.py`); where no caller knows the user, it is NULL. `request_text` carries text and tool calls/responses only,
  never file or image bytes (`_render_messages`).
- **Logfire.** With `LOGFIRE_CAPTURE_CONTENT=true` (kill switch, default off) and a Logfire backend,
  `telemetry._instrument_llm_sdks` attaches the provider-SDK instrumentation, which puts the wire-format prompt and
  response on the SDK's own span (OpenTelemetry GenAI attributes). It is the provider wire format, so it also shows
  adapter translation and prompt-cache behaviour. So one call yields two spans: the SDK's (content) and
  our `llm.call` (metadata semconv lacks). Instrumentation is on SDK classes, so adapter construction order is
  irrelevant; `instrument_openai` covers Grok.
- The split is durability versus analysis, not sensitivity: both hold content. BigQuery is the durable, in-perimeter
  store; Logfire is the cascade view (who delegated to whom), which BigQuery cannot express (no parent span).
  Threat model and alternatives: [`logfire_prompt_content_capture.md`](../../04_solution_strategy/decisions/logfire_prompt_content_capture.md).
- Deep research writes outside `_call_llm`: `job_main.py` calls `record_dr_result`, which is awaited and retried so an
  expensive result survives a failed delivery (it writes before delivering). The deep-research runner drives the
  provider SDK directly and logs only a summary line (`BaseAgent._debug_raw_turn`): its prompts are in no
  `prompt_content` row, and appear only on Logfire SDK spans where instrumentation is active (the main service's
  `agent_execution` path, not the Cloud Run Job).
- Cloud Run throttles CPU after the response, so a request that ends right after capturing awaits
  `PromptContentStore.flush` (`src/services/call_text_file_service.py`, `src/web/voice_control_plane_app.py`).

### Event marks

`DedupStore.try_mark_processed` claims a key atomically (`create`; an expired, unpurged mark is rewritten under a
`last_update_time` precondition), so of concurrent claimers exactly one wins; `release` deletes the mark after a failed
attempt so a retry is processed. Slack intake instead names its Cloud Task after the event id (a same-name create is
`ALREADY_EXISTS`), and `WorkerHandler` claims every Cloud Tasks delivery by task name with `TaskClaimStore`; that
mechanism and its TTLs are [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md).

## Invariants and traps

- **A failed LLM call has no `prompt_content` row and no `llm.call` span.** Both sit after the success path of
  `_call_llm`. Failed calls are visible in Logfire (SDK spans) and in logs only.
- **Capture must never break the request.** `record_turn` and `_store` swallow and log every error; only
  `record_dr_result` retries (`_DURABLE_ATTEMPTS`), and it also returns instead of raising.
- **File contents never leave the process in a span.** `logfire_redacting_exporter.RedactingExporter` replaces every
  part whose type is not text, tool or reasoning with `{"type": "file_omitted", ...}` in the three `gen_ai.*` attributes
  at export time (a span processor cannot edit attributes). Logfire's own exporter is replaced by ours when capture is on
  (`_logfire_redacting_processor`); if ours cannot be built, capture is switched off, not sent unredacted. Cloud Trace
  gets the same wrapper when capture is on. An agent whose requests carry files also suppresses SDK spans for its
  provider calls (`_provider_call_scope` override, `telemetry.sdk_spans_suppressed`; the document reader), leaving only
  its `llm.call` span.
- **Gemini SDK spans are off on google-genai 3.x.** The vendor instrumentor supports <3 only
  (`google_genai_instrumentation_decision`); with content capture on, `telemetry` logs `GENAI_SPANS_OFF_WARNING` once at start. Gemini turns keep
  `llm.call` spans and full BigQuery rows. Gemini goes through the vendor instrumentor on purpose, not Logfire's wrapper
  ([`gemini_logfire_instrumentation.md`](../../04_solution_strategy/decisions/gemini_logfire_instrumentation.md)); its
  content env var takes `SPAN_ONLY`, not a boolean, or content is silently empty.
- **Secrets stay out of log lines, and so do exception messages that may hold paths or URLs** (agent hooks do log the first 60 characters of the user text). `domain/log_redaction.py` (`redact_for_log`, `redact_exception`,
  `redact_traceback`) removes given secrets (and a token's payload/signature segments), JWT-shaped strings, `Bearer`
  and `authorization` values and every URL, and truncates; use it for text from a provider or a library.
  `src/utils/logger.py::exc_brief` logs an exception as type plus a digest of its message when the message may hold paths or URLs.
  Tool results and code-written text follow the voice rules in the root `CLAUDE.md`.
- **`DEBUG_PROMPTS` is a capture switch, not an adapter selector**, and `BIGQUERY_PROMPT_DATASET` is required beside it.
  Deploy sets `DEBUG_PROMPTS=true`, so in cloud every LLM call is captured; locally capture is off unless `.env` sets
  both. Both are listed in `load_settings()` (`src/config/settings.py`); `LOGFIRE_CAPTURE_CONTENT` and `TRACING_BACKEND`
  are optional knobs read with `os.getenv`.
- **The row's model and provider name what served the call**, not what was asked: after a cross-provider fallback
  `_call_llm` passes the served request and provider to `record_turn`.
- **Two context-variable sets.** `logging_context` feeds the log labels `session_id`, `event_id` and `user_id`;
  `telemetry.set_request_context` feeds the prompt-content row's `user_id` (and `trace_id` for both). Setting only one
  leaves the other stream without that id.
- **Dedup limitation.** `try_mark_processed` can return a false duplicate when the SDK retries a commit whose first write
  landed (documented on the port); `is_duplicate` has no caller in `src/`.
- **Anthropic wire debug** (`ANTHROPIC_LOG` set on Cloud Run) gives the `anthropic` logger its own DEBUG handler with
  `propagate=False`, so raw HTTP payloads reach Cloud Logging without lowering the app-wide INFO level; inert otherwise.

## Links

- [`logfire_prompt_content_capture.md`](../../04_solution_strategy/decisions/logfire_prompt_content_capture.md),
  [`tracing_backend_both_interim.md`](../../04_solution_strategy/decisions/tracing_backend_both_interim.md),
  [`llm_observability_pending.md`](../../04_solution_strategy/decisions/llm_observability_pending.md),
  [`gemini_logfire_instrumentation.md`](../../04_solution_strategy/decisions/gemini_logfire_instrumentation.md),
  [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md).
- Reading the data: [`LOGGING.md`](../../07_deployment/LOGGING.md), root `CLAUDE.md` "Debugging Cloud Run".
- [`memory_telemetry`](../memory_telemetry/README.md), [`ALERTING.md`](../../07_deployment/ALERTING.md).

## Where to start reading

- `src/utils/logger.py::setup_logger` and `src/utils/telemetry.py::init_telemetry`
- `src/utils/telemetry.py::_instrument_llm_sdks`, `src/utils/logfire_redacting_exporter.py::RedactingExporter`
- `src/agents/base_agent.py::BaseAgent._call_llm`
- `src/ports/prompt_content_store.py::PromptContentStore`,
  `src/adapters/bigquery_prompt_content_adapter.py::BigQueryPromptContentAdapter`
- `src/domain/log_redaction.py::redact_for_log`
- `src/adapters/firestore_dedup_store.py::FirestoreDedupStore`
