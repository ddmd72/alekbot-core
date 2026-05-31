# Decision: Remove Logfire — Cloud Trace is the sole tracing backend (in-house)

**Status:** **REOPENED 2026-05-31** — a load-bearing reason for removal was factually wrong (see correction). `both` stays active while the call is reconsidered; the user is evaluating hands-on for a few days.

> **Correction (primary source).** An earlier version of this record claimed Logfire's LLM dashboards are "empty by design because content must be sent (PII)." **That is false.** Logfire's GenAI instrumentation does NOT capture prompt/completion content by default — without `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, spans show `<elided>` and carry **metadata only** (model, tokens, latency). Source: https://pydantic.dev/docs/logfire/integrations/llms/google-genai/ . So metadata-only LLM/agent dashboards ARE achievable, **PII-safe**. The dashboards are empty today only because the GenAI instrumentation (`logfire.instrument_anthropic/openai/google_genai` + the `opentelemetry-instrumentation-*` packages) is not wired — not because of any PII blocker.
**Date:** 2026-05-31
**Context:** Observability split (`project_observability_split`). Logfire was added as an OTel-native tracing backend alongside the pre-existing Cloud Trace exporter, partly to evaluate/learn it.

## Decision

Logfire is judged **redundant** for this project and will be removed. **Cloud Trace** (OpenTelemetry exporter, in-house GCP) becomes the sole tracing backend. The OTel instrumentation, the BigQuery content store, and the `DEBUG_PROMPTS` capture switch all stay — they are backend-agnostic.

## Why remove it

- **Redundant over the existing in-house stack.** The three observability pillars are already covered inside the GCP perimeter: **Cloud Logging** (events, correlated by trace_id/span_id), **Cloud Trace** (the distributed-trace waterfall), **BigQuery** (queryable LLM content + token/cost/latency). Logfire is a nicer UI over capabilities already present.
- **Its main differentiator (GenAI/LLM + Agents dashboards) is achievable but unwired.** ~~Empty by design due to PII~~ — corrected above: metadata-only dashboards are PII-safe and achievable. The real trade is: those pre-built panels require adding `opentelemetry-instrumentation-{anthropic,openai,google-genai}` (deps + version pinning) + `logfire.instrument_*` (SDK patching), to get analytics (tokens/cost/latency/model) that are **also derivable from BigQuery via SQL**. So the value is "pre-built GenAI panels" vs "SQL over BQ" — a convenience delta, weighed against +deps and an external vendor.
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
- **Hand-roll GenAI semantic conventions.** Unnecessary — Logfire's own `instrument_*` emits the conventions reliably (and metadata-only by default), so hand-rolling would only add version-matching fragility.

## Open question (the reopened decision)

With the PII blocker debunked, the call is now a genuine convenience-vs-cost trade, to be made after hands-on: do Logfire's pre-built GenAI/agent panels (metadata-only, PII-safe) justify +3 instrumentation deps + an external vendor, given BigQuery already holds the same numbers? Recommended next step: wire the metadata-only instrumentation during the evaluation window, then decide on evidence.
