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

## 8. Status
**Production Ready** (OpenTelemetry + human logs + dedup active).
