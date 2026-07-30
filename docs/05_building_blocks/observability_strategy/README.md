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

- **Tracing (spans / latency / tokens, **and since 2026-07-30 prompt/response content**) → Logfire**
  (or Cloud Trace). Backend chosen by `TRACING_BACKEND` (`cloud_trace | logfire | both | none`);
  `both` attaches Cloud Trace as an in-house fallback processor on the Logfire provider.
  **No `TracingPort`** — OTel is already the vendor-neutral abstraction; `logfire.configure()` swaps
  the global provider, so existing `start_span`/`get_tracer` route through it with zero call-site
  changes.
- **Content (sensitive: prompt/response text + tokens) → BigQuery**, in-perimeter, 30-day TTL.
  Behind the `PromptContentStore` port (`src/ports/`), impl `BigQueryPromptContentAdapter`
  (lazy client, DAY-partitioned table, TTL-in-code via `expiration_ms`, all errors swallowed —
  capture must never break the LLM path). Single capture point: `BaseAgent._call_llm` →
  `record_turn(...)`. Table `alek_observability_dev.prompt_content`; `request_text` is rendered by
  `_render_messages` (text + tool_call/tool_response only — **never `file_data`/image bytes**).
  **Correction 2026-07-30:** an earlier version of this section claimed the row "is populated even
  for failed (e.g. 400) calls". **It is not.** Both `_emit_llm_span` and `record_turn` sit on the
  success return path (`base_agent.py:1181,1185`); a raising call exits before them, so a failed LLM
  call has **neither span nor BigQuery row**. Failed calls are observable in Logfire only, via the
  SDK instrumentation added with content capture.

**Rationale (revised 2026-07-30).** The original rationale — "a Logfire breach leaks only metadata;
sensitive payload never leaves the GCP perimeter" — **no longer holds**: content is now sent to
Logfire as well. The split is now *durability vs analysis*, not sensitivity: BigQuery is the
in-perimeter durable store (incl. the retried `record_dr_result` path), Logfire is the cascade view
and query surface over the same turns. The perimeter argument was retired on evidence — Logfire
itself runs on GCP, and prompt content already reaches four LLM providers on every request. Full
threat model, economics and rejected alternatives: `decisions/logfire_prompt_content_capture.md`.

**Content capture:** `LOGFIRE_CAPTURE_CONTENT=true` (kill switch, default off) →
`_instrument_llm_sdks()` in `src/utils/telemetry.py` attaches Logfire's own
`instrument_anthropic`/`instrument_openai`/`instrument_google_genai` with `version='latest'`, which
emits the OTel GenAI semantic conventions (`gen_ai.input.messages` / `gen_ai.output.messages` /
`gen_ai.system_instructions`) that drive the LLM panels. Instruments SDK *classes*, so it is
independent of adapter construction order; `instrument_openai` covers Grok too. Captured payload is
the **provider wire format**, so it also reveals adapter translation and prompt-cache behaviour.
This produces two spans per call — the SDK's (content) and the custom `llm.call` (metadata:
`agent_type`, `turn`, `provider`, which semconv lacks).

**Gemini content is NOT captured** — `instrument_google_genai` needs
`opentelemetry-instrumentation-google-genai` (requires `opentelemetry-api~=1.43`), which cannot
co-install with `logfire==4.34.0` (`opentelemetry-sdk<1.42`). Gemini traffic (Router triage, Compute)
stays metadata-only in Logfire and content-complete in BigQuery; Anthropic and OpenAI/Grok — incl.
Smart's primary model — are unaffected.

**Gating:** the BigQuery store is wired iff `DEBUG_PROMPTS=true` AND
`BIGQUERY_PROMPT_DATASET` is set — `DEBUG_PROMPTS` is a global write on/off switch, not an adapter
selector. **Legacy:** `PromptDebugLogger` (the old GCS `…-debug-prompts/` dump) is fully superseded
and no longer called from `_call_llm` — removal backlogged as TD-1 in the roadmap.

See: `decisions/tracing_backend_both_interim.md`, `decisions/llm_observability_pending.md`; how to
**read** the data in `CLAUDE.md` → "Debugging Cloud Run". Code: `src/utils/telemetry.py`,
`src/adapters/bigquery_prompt_content_adapter.py`, `src/agents/base_agent.py::_call_llm`.

## 9. Status
**Production Ready** — OpenTelemetry + human logs + dedup, plus the LLM sensitivity split
(Logfire/Cloud Trace tracing + BigQuery content store) live since 2026-05-30.
