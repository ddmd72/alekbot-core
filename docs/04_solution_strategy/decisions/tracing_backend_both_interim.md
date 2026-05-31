# Decision: Keep TRACING_BACKEND=both (Logfire + Cloud Trace) — interim

**Status:** Interim — both backends active; full switch to a single backend deferred pending an explicit decision
**Date:** 2026-05-31
**Context:** Observability split (`project_observability_split`). Logfire added as an OTel-native tracing backend alongside the pre-existing Cloud Trace exporter.

## Decision

Run `TRACING_BACKEND=both` for now (dev). Every span fans out to **both** Logfire and Cloud Trace: `logfire.configure()` owns the global OTel provider, and `BatchSpanProcessor(CloudTraceSpanExporter())` is attached as an `additional_span_processor` on the same provider.

This is deliberately **not** a final state. Collapsing to one backend is left as an open decision, not done by default.

## Why keep both for now

- **Cost is not a forcing function.** At solo-dev volume (~5–15 spans/request, well under Cloud Trace's 2.5M-spans/month free tier and Logfire's free tier) both backends are effectively $0. Export is async/batched (no request-path latency), spans carry metadata only (no content). So dual-write costs ~nothing — it does not pressure a quick switch.
- **The two have different value props** and the trade-off hasn't been resolved:
  - **Logfire** — superior UI, the backend being learned/evaluated; but an external vendor.
  - **Cloud Trace** — stays inside the GCP perimeter (in-house fallback), no third party; weaker UI.
- Switching to `logfire` alone means accepting an external vendor as the *sole* tracing path; switching to `cloud_trace` alone means dropping the Logfire evaluation. Either is a real decision (vendor-dependence vs in-house), not yet made.

## How to switch (when decided)

One value in `cloudbuild-dev.yaml`: `TRACING_BACKEND=logfire` (Logfire only) | `cloud_trace` (legacy, in-house only) | `none`. Redeploy. No code changes — OTel is the abstraction, the backend is the swap point.

## Rejected alternatives (for now)

- **Logfire-only immediately.** Premature — commits to a sole external tracing dependency before the vendor has been evaluated in real use.
- **Cloud-Trace-only (revert).** Throws away the Logfire evaluation that motivated this work.
- **Decide on cost grounds.** Dual-write is free at this scale; cost can't be the deciding factor. The decision is about vendor-dependence vs in-house UX, to be made later.
