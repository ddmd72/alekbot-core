# Decision: LLM observability — migrate to Pydantic Logfire

**Status:** Adopted (platform selected) — **implementation pending**.
**Date:** 2026-05-01 (initial deferral) → 2026-05-01 (platform chosen: Logfire).
**Trigger:** Integration test `tests/integration/test_smart_concurrent_per_user.py::test_concurrent_execute_runs_in_parallel_with_per_call_overrides` fails under `DEBUG_PROMPTS=true` (the de-facto state of dev environment, which is the maintainer's effective production). The failure surfaced a class-of-problem with the current custom debug-logging that goes beyond the test.

## Selection rationale (added 2026-05-01)

After discussion of three candidate platforms (Langfuse self-hosted, Helicone proxy, Logfire managed) plus OpenLLMetry-as-vendor-neutral-SDK, **Pydantic Logfire** was chosen for alekbot. Reasons:

- **Pydantic-native structural logging.** alekbot is heavily Pydantic — domain entities, agent messages, LLM requests are all `BaseModel`. Logfire (built by the Pydantic team) converts that structure into searchable trace fields. Other tools serialize as JSON strings and lose field-level search.
- **Auto-instrumentation for Anthropic / OpenAI / google-genai SDKs.** Zero adapter changes — span per LLM call captured at the SDK boundary with model, tokens, cost, latency, finish_reason, full payload.
- **Async-native OTel context propagation.** Trace ID flows through `asyncio.gather` and `create_task` automatically — multi-agent parallel delegation (Smart → search_memory + search_web in parallel → tool_use) reconstructs as a single tree without manual instrumentation. Cloud Tasks causality propagates via OTLP headers.
- **Free-tier sufficient for solo-dev traffic.** Logfire's free allotment is 2-3 orders of magnitude above alekbot's expected load.
- **Hexagonal-port-friendly.** Integration via decorator on `BaseAgent._call_llm` + per-delegation span on `AgentCoordinator.handle_delegation`. **The `LLMPort` boundary stays intact**; observability wraps the boundary, does not pierce it.
- **Pydantic-managed = not a moving target.** The Pydantic team has 8+ years of Python ecosystem stewardship; Logfire is funded as their commercial offering. Lower vendor-stability risk than newer LLM-observability startups.

**Why not the alternatives:**
- **Langfuse self-hosted** — would bundle prompt-management with observability, but alekbot's prompt system is class-based composition with 4-level overrides, not generic CRUD. Langfuse prompt-management is a partial overlap, not replacement. Self-host adds Postgres + ClickHouse maintenance to a solo dev — not worth it for the observability-only need.
- **Helicone proxy** — fights hexagonal `LLMPort` design (proxy becomes single point of failure for 4 providers, complicates per-provider rate-limit/connection-pool tuning). Bad for portfolio narrative — codebase looks unobserved because nothing visible in repo.
- **OpenLLMetry SDK + neutral backend** — vendor-neutrality not load-bearing for solo project; backend choice (Grafana Tempo / Honeycomb) still adds infra. Logfire-as-backend with OpenLLMetry-as-SDK is overkill for current traffic.

## Context

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

**Adopt Pydantic Logfire.** Implementation deferred to pre-release-branch phase. Until Logfire lands, keep `PromptDebugLogger` as-is as a known-degraded workaround. The integration test stays in its current (failing under `DEBUG_PROMPTS=true`) state as a deliberate reminder until Logfire's non-blocking OTLP exporter replaces sync GCS uploads.

### Implementation plan

- **Port:** new `ObservabilityPort` in `src/ports/` to keep the adapter swap-friendly. Methods cover the actual operations alekbot performs: span creation around `_call_llm`, around `handle_delegation`, attribute attachment for token/cost accounting, exception recording.
- **Adapter:** `LogfireObservabilityAdapter` in `src/adapters/logfire_observability.py`. Wraps `logfire.instrument()` decorators and span context managers per the port.
- **Composition:** Logfire SDK initialized once in `main.py` with project token + service name; adapter constructed and threaded into `BaseAgent` via DI.
- **Instrumentation hooks (all behind the port):**
  - `BaseAgent._call_llm` — span per LLM call. Logfire auto-instrumentation also captures the underlying SDK call (anthropic/openai/google-genai) — both layers visible.
  - `AgentCoordinator.handle_delegation` — span per delegation. Parent-child tree of multi-agent flows reconstructs automatically.
  - Cloud Tasks payload propagation — OTLP trace headers serialized into task payload, deserialized by `WorkerHandler`. Causality across reminder fire / daily email / async DR / async doc generation preserved.
- **Sunset `PromptDebugLogger`:** retain GCS bucket per current retention policy until daily-debugging workflow validated against Logfire UI. Once validated: delete `src/utils/debug_logger.py`, remove `DEBUG_PROMPTS` env var, drop the GCS bucket.
- **Failing integration test resolution:** `test_concurrent_execute_runs_in_parallel_with_per_call_overrides` will pass without modification once Logfire's non-blocking OTLP exporter replaces sync GCS uploads inside `_debug_llm_response`. No test changes required.

### Why implementation is deferred (not done now)

- **Coupling with `provider_resilience_port_pending.md`.** That decision proposes a `ProviderResiliencePort` whose `record_failure` / `record_success` hooks naturally become Logfire span attributes. Building observability before resilience risks designing instrumentation that conflicts with the resilience port's state model.
- **Pre-release-branch doc-shape pass (Bucket I).** Logfire UI screenshots are a portfolio-visible asset. Bundle Logfire integration with the doc-shape decision so the screenshots inform the narrative shape.
- **Current usage is degraded but functional.** GCS dump remains usable for daily debugging; the latency hit is real but the maintainer accepts it consciously until Logfire lands.

## Why not partial closure

Per `feedback_clean_or_explain.md`: "every non-trivial change is binary — clean hexagonal implementation OR explicit deferral with rationale". Three smaller fixes were considered and rejected:

1. **Make `_gcs_upload` async (`asyncio.to_thread` wrap).** Closes the event-loop block. Does not address the lack of trace model, no UI, no retention, no cost aggregation. Locks in a pattern that the eventual migration will discard. Throwaway work.
2. **Add a fire-and-forget queue inside the logger.** Same shape — closes the latency hit, but commits more code to a logger that should be replaced. Worse than option 1 because it adds new state to a doomed component.
3. **Disable debug logging in the failing test only.** Hides the symptom in one test, leaves the prod concern in place, makes a future engineer assume the problem is gone. Actively misleading.

The integration test failing under `DEBUG_PROMPTS=true` is **the right shape of reminder** — it is a load-bearing visible signal, not a hidden TODO. Per the project's "loud failure over silent drop" discipline (see `feedback_clean_or_explain.md`), letting a known integration-test failure stay visible is preferable to muting it.

## Triggers to start implementation

1. **Pre-release-branch doc-shape pass (Bucket I) starts.** Logfire screenshots inform the doc/portfolio narrative shape — bundle.
2. **`provider_resilience_port_pending.md` implementation starts.** Bundle observability + resilience as a single architectural pass to avoid double-redesigning the instrumentation surface.
3. **A daily-debugging workflow gap.** Concretely: a "why was this user's response slow?" or "did agent X regress on quality after I tuned the prompt token?" question that the current GCS dump cannot answer in <5 minutes. That signal forces immediate Logfire integration regardless of bucket order.

## Re-evaluate platform choice if

- Logfire pricing model materially changes (free tier shrinks below alekbot's traffic + 10× headroom).
- Logfire is acquired/sunset and roadmap stops moving (cf. small-vendor risk).
- F5.6 blueprint revision concludes that prompt-management UI is now load-bearing — at that point Langfuse self-hosted re-enters consideration as a bundle.

## Consequences

**Positive (post-decision, before implementation):**
- Platform is chosen — no vendor-evaluation cycles when implementation starts.
- The `ObservabilityPort` design keeps swap optionality if Logfire ever needs to be replaced.
- Three correlated architectural items (this, F4.5 provider resilience, Bucket I doc-shape) get implemented together as a single bundled architectural pass.
- The failing integration test is **purposefully** kept as a load-bearing reminder. Anyone running the suite in dev mode sees the symptom; the failure traceback now points to this decision record.

**Positive (post-implementation, expected):**
- Multi-agent flow visibility (Smart → search_memory + search_web in parallel → terminal_tool reconstructs as a single trace tree).
- Cost-per-user / per-agent / per-provider pivot from a single query (currently requires manual GCS inspection + accumulator math).
- Pydantic-aware searchable trace fields — `LLMRequest` / `AgentMessage` / `MessageContext` field-level search in UI.
- `_call_llm` no longer blocks the event loop on debug logging — failing integration test passes without modification.

**Negative / cost (during deferral):**
- Production agents continue to take the latency hit when `DEBUG_PROMPTS=true`. This is the de-facto dev-as-prod state — the maintainer accepts it consciously.
- One integration test remains in a failing state under dev settings. CI must be configured to either run with `DEBUG_PROMPTS=false` or to expect this one failure (TBD when CI is set up).
- The trace-model gap remains: complex multi-agent delegation flows cannot be reconstructed from GCS files without manual triangulation.

**Negative / cost (post-implementation):**
- Vendor dependency on Logfire (managed-only, no self-host).
- Per-span cost beyond free tier — non-issue at current traffic but a watch-point if the bot scales beyond solo-use.
- Pydantic-team stewardship risk — assessed low (8+ year ecosystem stewardship track record, Logfire is their flagship commercial product).

## References

- `src/utils/debug_logger.py` — the current custom logger.
- `tests/integration/test_smart_concurrent_per_user.py::test_concurrent_execute_runs_in_parallel_with_per_call_overrides` — the failing test that surfaced the prod concern. **Intentionally not patched.**
- `src/agents/base_agent.py:1006` (call site of `_debug_llm_response`) and `:891` (the actual sync upload trigger).
- `docs/04_solution_strategy/decisions/provider_resilience_port_pending.md` — coupled decision (instrumentation + failure tracking + per-provider state).
- `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md` — Bucket I (pre-release-branch doc-shape pass).
- Project rule: `feedback_clean_or_explain.md` (clean implementation OR explicit deferral; partial fixes rejected).
