# Decision: Remove Logfire — Cloud Trace is the sole tracing backend (in-house)

**Status:** Decided (remove Logfire). **Execution deferred ~a few days** for final hands-on evaluation of the Logfire UI; then revert to `TRACING_BACKEND=cloud_trace` and strip the Logfire branch. Until then `both` stays active (cost negligible).
**Date:** 2026-05-31
**Context:** Observability split (`project_observability_split`). Logfire was added as an OTel-native tracing backend alongside the pre-existing Cloud Trace exporter, partly to evaluate/learn it.

## Decision

Logfire is judged **redundant** for this project and will be removed. **Cloud Trace** (OpenTelemetry exporter, in-house GCP) becomes the sole tracing backend. The OTel instrumentation, the BigQuery content store, and the `DEBUG_PROMPTS` capture switch all stay — they are backend-agnostic.

## Why remove it

- **Redundant over the existing in-house stack.** The three observability pillars are already covered inside the GCP perimeter: **Cloud Logging** (events, correlated by trace_id/span_id), **Cloud Trace** (the distributed-trace waterfall), **BigQuery** (queryable LLM content + token/cost/latency). Logfire is a nicer UI over capabilities already present.
- **Its main differentiator is empty by design.** Logfire's value-add is the GenAI/LLM + Agents dashboards. Those key off OTel GenAI semantic conventions AND typically display prompt/response **content** — but content is deliberately kept out of spans (PII split → content lives only in BigQuery). So the LLM panels stay empty, and the metadata analytics they would show (tokens/cost/latency by model) are already a SQL query away in BigQuery.
- **Learning goal satisfied** (~1h was enough; nothing more to learn once the LLM dashboards don't populate).
- **Portfolio framing.** A half-integrated, abandoned external vendor is a negative signal, not a plus. The strong story is "vendor-neutral observability on OTel, in-house Cloud Trace by default, backend swappable in one line" — which the Logfire experiment validated. Removal *completes* that story rather than weakening it.

## Removal checklist (when executed)

1. `src/utils/telemetry.py` — drop `_init_logfire` + the `logfire`/`both` branches; keep `cloud_trace` | `none`.
2. `requirements.txt` — remove `logfire`; revert opentelemetry-* to unpinned (the pins existed only for Logfire compatibility).
3. `cloudbuild-dev.yaml` — `TRACING_BACKEND=both` → `cloud_trace`; remove the `LOGFIRE_TOKEN` secret ref.
4. `tests/unit/utils/test_telemetry_backend.py` — rewrite for cloud_trace/none only.
5. Secret Manager — delete the `LOGFIRE_TOKEN` secret (infra step, owner).
6. Update `project_observability_split` memory.

## Rejected alternatives

- **Keep Logfire (`both` or `logfire`-only).** No operational value over the in-house stack, and a standing external-vendor dependency + portfolio negative.
- **Hand-roll GenAI semantic conventions to light up the dashboards.** Would still show no message content (PII), so only metadata panels — and that data is already in BigQuery. Not worth the work for a backend being removed.
