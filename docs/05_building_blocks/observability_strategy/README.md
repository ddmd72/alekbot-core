# Observability Strategy (Building Block)

## 1. Overview
Observability is built on three layers:
1. Human-readable logs (primary for operators).
2. Structured fields (for Cloud Logging filters).
3. OpenTelemetry traces (for end-to-end latency analysis).

## 2. Human-Readable Logs
- Full mode includes `trace_id`, `session_id`, `span_id`, `event_id`, `user_id`.
- Clean mode omits metadata for local readability.
- Mode is controlled by `LOG_TRACE_CONTEXT`.

## 3. Structured Fields
Structured JSON fields allow filtering:
- `component`, `latency_ms`, `model`, `trace_id`, etc.
- Used in Cloud Logging queries.

## 4. Tracing (OpenTelemetry)
- Traces are initialized via `init_telemetry("alek-core")` in `main.py`.
- Spans include: `slack.event.received`, `conversation.handle_message`, `llm.generate.*`, `tool.execute`.
- Trace IDs are injected into logs for correlation.

## 5. Slack Event Deduplication
- `FirestoreEventDedupStore` prevents duplicate Slack events.
- TTL is 1 hour; protects against Slack retry storms.

## 6. Troubleshooting Tips
- Ensure Cloud Trace API + service account roles enabled.
- Confirm `set_log_context()` is invoked in handlers/adapters.
- Verify Cloud Tasks headers propagate trace context.

## 7. Code References
- `src/utils/logger.py`
- `src/utils/telemetry.py`
- `src/utils/logging_context.py`
- `src/utils/performance_logger.py`
- `src/adapters/firestore_dedup_store.py`
- `src/adapters/slack/http_adapter.py`

## 8. LLM Observability — sensitivity split (2026-05-30 rework, LIVE)

The §4 OTel model was extended into a **split-by-sensitivity** design (supersedes the older framing
where they overlap). Two streams, joined by `trace_id`:

- **Tracing (non-sensitive: spans / latency / tokens) → Logfire** (or Cloud Trace). Backend chosen
  by `TRACING_BACKEND` (`cloud_trace | logfire | both | none`); `both` attaches Cloud Trace as an
  in-house fallback processor on the Logfire provider. **No `TracingPort`** — OTel is already the
  vendor-neutral abstraction; `logfire.configure()` swaps the global provider, so existing
  `start_span`/`get_tracer` route through it with zero call-site changes.
- **Content (sensitive: prompt/response text + tokens) → BigQuery**, in-perimeter, 30-day TTL.
  Behind the `PromptContentStore` port (`src/ports/`), impl `BigQueryPromptContentAdapter`
  (lazy client, DAY-partitioned table, TTL-in-code via `expiration_ms`, all errors swallowed —
  capture must never break the LLM path). Single capture point: `BaseAgent._call_llm` →
  `record_turn(...)`. Table `alek_observability_dev.prompt_content`; `request_text` is rendered by
  `_render_messages` (text + tool_call/tool_response only — **never `file_data`/image bytes**) and
  is populated even for failed (e.g. 400) calls.

**Rationale:** a Logfire breach leaks only metadata (harmless); sensitive payload never leaves the
GCP perimeter. **Gating:** the BigQuery store is wired iff `DEBUG_PROMPTS=true` AND
`BIGQUERY_PROMPT_DATASET` is set — `DEBUG_PROMPTS` is a global write on/off switch, not an adapter
selector. **Legacy:** `PromptDebugLogger` (the old GCS `…-debug-prompts/` dump) is fully superseded
and no longer called from `_call_llm` — removal backlogged as TD-1 in the roadmap.

See: `decisions/tracing_backend_both_interim.md`, `decisions/llm_observability_pending.md`; how to
**read** the data in `CLAUDE.md` → "Debugging Cloud Run". Code: `src/utils/telemetry.py`,
`src/adapters/bigquery_prompt_content_adapter.py`, `src/agents/base_agent.py::_call_llm`.

## 9. Status
**Production Ready** — OpenTelemetry + human logs + dedup, plus the LLM sensitivity split
(Logfire/Cloud Trace tracing + BigQuery content store) live since 2026-05-30.
