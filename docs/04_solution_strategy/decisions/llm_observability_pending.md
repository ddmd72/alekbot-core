# Decision: LLM observability — replace custom `PromptDebugLogger`, evaluation pending

**Status:** Pending — **architectural concern named, evaluation deferred**.
**Date:** 2026-05-01
**Trigger:** Integration test `tests/integration/test_smart_concurrent_per_user.py::test_concurrent_execute_runs_in_parallel_with_per_call_overrides` fails under `DEBUG_PROMPTS=true` (the de-facto state of dev environment, which is the maintainer's effective production). The failure surfaced a class-of-problem with the current custom debug-logging that goes beyond the test.

## Context

Today the project uses a custom in-house debug logger:

- **Code:** `src/utils/debug_logger.py::PromptDebugLogger`.
- **Mechanism:** every `BaseAgent._call_llm` writes an LLM request/response pair to either the local filesystem (no `DEBUG_PROMPTS_BUCKET`) or a GCS bucket (Cloud Run mode) via **synchronous `urllib3`** under `google.cloud.storage`.
- **Activation:** `DEBUG_PROMPTS=true` env var; in this project's dev environment that is the default ("dev = de-facto prod" per maintainer).

### Problems with the current shape

1. **Blocks the asyncio event loop.** `blob.upload_from_string(...)` is a sync call held inside an async hot path (`_call_llm`). With `DEBUG_PROMPTS=true`, two concurrent `process()` calls on the same agent serialize on these uploads — exactly what the new `test_concurrent_execute_runs_in_parallel_with_per_call_overrides` test detects (and intentionally fails on, see below). Real production impact: agent latency degrades anywhere this logger is enabled.
2. **No structured trace model.** Each LLM call writes one `_request.txt` + one `_response.txt` file. There is no notion of agent → delegation → sub-agent span hierarchy, no causality, no link between an originating user message and the resulting tool/specialist tree. Any "what happened during conversation X" inspection requires manual GCS file timestamp triangulation.
3. **No first-class observability primitives.** No latency histograms, no cost aggregation per user / per agent / per provider, no error-rate dashboards, no prompt-quality A/B comparison. Billing accumulator (`BaseAgent._billing_*`) provides cost; everything else is left to whoever is looking at the bucket.
4. **GCS-bucket-as-database antipattern.** The bucket is the only durable record. No retention policy, no UI, no search, no de-dup. Filenames carry timestamps to fake an index.
5. **Not a debugging *workflow* — it is a dump.** The maintainer cannot replay a session, fork a prompt, or compare two responses side-by-side without leaving the codebase. Custom UI on top of GCS is unrealistic.

### What "fully clean" would look like

A managed LLM-observability platform that replaces the dump-to-GCS pattern with structured spans, durable retention, and a UI. Three serious candidates for evaluation:

- **Langfuse** (open-source, self-hostable on GCP or fully managed). Native trace/span/generation primitives, supports the patterns in this project (multi-turn delegation, tool use, branching). Has Python SDK that wraps async cleanly. Cost: container + Postgres if self-hosted, or per-trace pricing if managed.
- **Helicone** (managed, observability-as-proxy). Inserts itself between the SDK and the provider; captures tokens/latency/cost without code changes. Cleaner integration but requires routing all LLM calls through their proxy — affects rate-limit and connection-pool behavior.
- **OpenTelemetry + LLM semantic conventions (vendor-neutral)** + a backend like Grafana Tempo or Honeycomb. Most flexible but most work; native to Cloud Run (already has OTel auto-instrumentation hooks). Probably underweight for an LLM-specific need.

The right answer is not pre-decided here — the eval IS the next step.

## Decision

**Defer.** Do not patch around the failing parallelism test. Do not migrate to a managed observability platform yet. Keep `PromptDebugLogger` as-is as a known-degraded workaround. The integration test stays in its current (failing under `DEBUG_PROMPTS=true`) state as a deliberate reminder that the problem is unresolved.

### Why deferred

- **Vendor selection requires evaluation.** Langfuse vs Helicone vs OTel is a non-trivial call. Each affects the agent code differently (SDK wrap vs proxy vs annotation), and the wrong choice locks in a migration cost. This is its own decision-record-worthy step, not a sub-bullet.
- **Coupling with `provider_resilience_port_pending.md`.** That decision proposes a `ProviderResiliencePort` that owns failure tracking per provider. The instrumentation hooks for "where did this latency come from?" / "which provider failed?" naturally belong to the resilience port too. Doing observability in isolation risks duplicating the future state surface.
- **Coupling with portfolio doc-shape pass (Bucket I).** Observability is one of the most portfolio-visible architectural decisions a project can make. Picking the tool now, before the doc-shape decision, risks picking a tool that doesn't tell the right story for the portfolio framing (e.g. Helicone-as-proxy is cheap engineering but invisible to a reviewer skimming the repo; Langfuse self-hosted is more work but produces UI screenshots that read well in a release narrative).
- **The current shape, while degraded, is not actively broken in production usage.** The maintainer reports the GCS dump is usable for daily debugging needs. The cost is the latency hit (real, named) and the inability to do anything more sophisticated (acknowledged trade-off).

## Why not partial closure

Per `feedback_clean_or_explain.md`: "every non-trivial change is binary — clean hexagonal implementation OR explicit deferral with rationale". Three smaller fixes were considered and rejected:

1. **Make `_gcs_upload` async (`asyncio.to_thread` wrap).** Closes the event-loop block. Does not address the lack of trace model, no UI, no retention, no cost aggregation. Locks in a pattern that the eventual migration will discard. Throwaway work.
2. **Add a fire-and-forget queue inside the logger.** Same shape — closes the latency hit, but commits more code to a logger that should be replaced. Worse than option 1 because it adds new state to a doomed component.
3. **Disable debug logging in the failing test only.** Hides the symptom in one test, leaves the prod concern in place, makes a future engineer assume the problem is gone. Actively misleading.

The integration test failing under `DEBUG_PROMPTS=true` is **the right shape of reminder** — it is a load-bearing visible signal, not a hidden TODO. Per the project's "loud failure over silent drop" discipline (see `feedback_clean_or_explain.md`), letting a known integration-test failure stay visible is preferable to muting it.

## Triggers to revisit

1. **A real product question that the current GCS dump cannot answer.** Most plausible: "why was this user's response slow?" / "did agent X regress on quality after a prompt change?" / "what is the cost-per-conversation distribution?" — any of these forces a real observability primitive.
2. **Pre-release-branch portfolio doc-shape pass (Bucket I).** Observability is a prime portfolio surface; the doc-shape decision likely names what observability shape best supports the portfolio narrative.
3. **`provider_resilience_port_pending.md` design pass.** When that subsystem is built, its instrumentation needs (record_failure / record_success / latency tracking) overlap heavily with observability — the right time to pick the platform.
4. **Cost growth.** When token usage moves from "manageable solo-dev bill" to "needs per-feature attribution to make spend decisions", custom in-line accumulation stops being enough.

## Consequences

**Positive:**
- The shape of the eventual fix (replace, not patch) is committed in writing. Future work does not waste cycles on `asyncio.to_thread` or fire-and-forget queue refactors that will be discarded.
- Three correlated architectural items (this, F4.5 provider resilience, Bucket I doc-shape) get designed together — likely as a single observability + resilience pass.
- The failing integration test is **purposefully** kept as a load-bearing reminder. Anyone running the suite in dev mode sees the symptom; the failure traceback now points to this decision record.

**Negative / cost:**
- Production agents continue to take the latency hit when `DEBUG_PROMPTS=true`. This is the de-facto dev-as-prod state — the maintainer accepts it consciously.
- One integration test remains in a failing state under dev settings. CI must be configured to either run with `DEBUG_PROMPTS=false` or to expect this one failure (TBD when CI is set up).
- The trace-model gap remains: complex multi-agent delegation flows (Smart → search_memory + search_web in parallel → terminal_tool) cannot be reconstructed from GCS files without manual triangulation.

## References

- `src/utils/debug_logger.py` — the current custom logger.
- `tests/integration/test_smart_concurrent_per_user.py::test_concurrent_execute_runs_in_parallel_with_per_call_overrides` — the failing test that surfaced the prod concern. **Intentionally not patched.**
- `src/agents/base_agent.py:1006` (call site of `_debug_llm_response`) and `:891` (the actual sync upload trigger).
- `docs/04_solution_strategy/decisions/provider_resilience_port_pending.md` — coupled decision (instrumentation + failure tracking + per-provider state).
- `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md` — Bucket I (pre-release-branch doc-shape pass).
- Project rule: `feedback_clean_or_explain.md` (clean implementation OR explicit deferral; partial fixes rejected).
