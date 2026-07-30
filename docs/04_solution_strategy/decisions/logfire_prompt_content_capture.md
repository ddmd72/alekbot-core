# Decision: send LLM prompt/response content to Logfire (all agents)

**Status:** Adopted 2026-07-30. Supersedes `tracing_backend_both_interim.md` (which resolved to
"remove Logfire" and is now closed as **superseded, not executed**).
**Date:** 2026-07-30
**Context:** The reopened question in `tracing_backend_both_interim.md` — do Logfire's GenAI panels
justify the instrumentation and the external vendor? — is answered here, and answered differently
than that record proposed, because the requirement changed from "metadata-only panels" to
"searchable cascade of actual prompts".

## Decision

Enable full prompt/response capture in Logfire for **all** agents, via Logfire's own SDK
instrumentation (`instrument_anthropic` / `instrument_openai` / `instrument_google_genai`,
`version='latest'`), gated by a `LOGFIRE_CAPTURE_CONTENT` kill switch. BigQuery `prompt_content`
stays as the in-perimeter durable store; Logfire becomes the analysis surface over the same data.

## Why the BigQuery-only status quo was insufficient

The cascade — "which agent received what and returned what" — is **structurally unreconstructable**
from `prompt_content`, not merely inconvenient:

- No `parent_span_id` column (`bigquery_prompt_content_adapter.py`); `turn` + `agent_type` are the
  only ordering signal.
- The stored `span_id` points at the **enclosing** span, not the LLM call. `llm.call` is created and
  immediately ended without being made current (`base_agent.py:1216,1232`), and `record_turn` runs
  after it (`:1185`), so `get_trace_ids()` returns `delegation.loop`. A row cannot be tied to its own
  call.
- `trace_id` is written with a `tr_` prefix (`telemetry.py:135-140`), so any Logfire join needs a
  `SUBSTR` first.

Fixing all three plus writing a tree viewer is building a UI that already exists.

## Risk analysis (the part that took the deliberation)

**The framing "keep it in my perimeter" does not survive contact with the facts.**

- **Logfire runs on GCP** — US region `us-east4`, EU `europe-west4`. The choice is not "my perimeter
  vs someone's cloud", it is "my GCP project vs Pydantic's GCP project". Same infrastructure trust
  layer; the difference is the access-control layer.
- **Prompt content already leaves the perimeter four times per request** — Anthropic, OpenAI, Google,
  xAI all receive the full assembled system prompt including memory facts, email bodies and
  biographical cache. Logfire is a fifth processor, not the first exit.
- **The genuine delta**, and the reason this needed a decision: an LLM API call is transient with
  ~30-day provider retention and no search UI. Logfire holds the same text **at rest, indexed,
  behind a web login**. The new risk is a second internet-facing login over a searchable index of
  the exocortex — not "data went outside".

**Vectors, ordered by probability:**

1. **Compromise of the owner's own Logfire account** (phishing, reused password, stolen session).
   Orders of magnitude likelier than a vendor breach, and fully in the owner's control. Dominant
   vector — mitigated by hardening that account, which is the actual security work here.
2. **Owner misconfiguration** — org invite, shared dashboard, public project.
3. **Integration/supply-chain** — the Salesloft Drift class (Aug 2025): stolen OAuth tokens from the
   vendor's infrastructure hit 700+ organisations that had merely *granted a token to a third party*.
   This is the class one enters by adding a vendor; vendor quality does not remove it.
4. **Vendor breach reaching customer telemetry** — the New Relic class (NR23-01, Nov 2023): stolen
   employee credentials + social engineering, three weeks of access to staging. Notably, **customer
   telemetry was not hosted on the compromised system** and there was no lateral movement to
   production. Incidents happen; segmentation generally holds.
5. **Subprocessors.** Logfire's published list includes **OpenAI ("AI model inference, non-EU
   users")** and Vertex AI for EU users. On the US region, Logfire's own AI features can feed
   captured content to another LLM.
6. **Jurisdiction.** US region → US legal process, while the owner is EU-resident.

**No public breach of Logfire was found.** Treated as a weak signal, not a strong one: young
product, small vendor, low researcher attention. What is verifiable: SOC 2 Type II with no
exceptions, HIPAA, and a DPA committing to breach notice within 72 hours.

**Honest comparison of blast radius — the counterintuitive part.** The BigQuery table is guarded by
a single Google account which, if compromised, exposes not 30 days of telemetry but *everything*:
Firestore facts, emails, history. A compromised Logfire account exposes telemetry within retention.
**The owner's own GCP account is the fatter target.** Additionally Pydantic has an audited process
and a notification SLA; this project has no access auditing or anomaly alerting on BigQuery reads.
The in-house perimeter wins on *vendor concentration*, not on process or blast radius.

## Economics

Metering is **5 KB per span, averaged**; $20/mo of free credits ≈ 10M records; $2 per additional
million. A Smart call carrying a 20k-token system prompt is ~80 KB ≈ 16–20 record-equivalents; at a
few hundred LLM calls/day that lands comfortably inside the free allowance, and the many small spans
(`slack.event.received`, `delegation`) average the cost down. **Personal/Team retention is 30 days —
parity with the BigQuery TTL**, so nothing is lost. Computed from published pricing, *not measured*:
the first 24h of real ingest is the check, and `LOGFIRE_CAPTURE_CONTENT=false` is the revert.

## Why SDK instrumentation rather than hand-rolled semantic conventions

LLM adapters are **container singletons** (`service_container.py:87-88,367,382`) — one client per
provider shared by every agent. Per-agent content selection is therefore impossible at the SDK
level; it would require building semconv attributes by hand in `_emit_llm_span`, where
`self.agent_type` is in scope. The owner chose all-or-nothing content, which removes that
constraint and makes the ~10-line SDK path viable.

That path is also strictly better on fidelity: the captured payload is the **actual provider wire
format** after PromptBuilder assembly and the `PROMPT_CACHE_BOUNDARY` split, making adapter
translation bugs and cache behaviour observable — the subject of a dedicated testing protocol
(`docs/how_to/ADAPTER_WIRE_TESTING.md`). A hand-rolled `LLMRequest` projection would not show it.

`instrument_openai` covers Grok as well: `GrokAdapter` drives the same `AsyncOpenAI` client.

## Known gap: Gemini content is not captured

`instrument_anthropic` and `instrument_openai` are built into logfire and work against the installed
SDKs (verified by smoke test, not just mocks). `instrument_google_genai` is **blocked by a hard
dependency conflict**:

- `opentelemetry-instrumentation-google-genai` (1.0b1) requires `opentelemetry-api~=1.43`
- `logfire==4.34.0` requires `opentelemetry-sdk<1.42.0,>=1.39.0`

Installing it would drag the deliberately pinned OTel set (1.41.1 / 0.62b1) into a mismatched state —
the exact fragility `requirements.txt` documents at the pin block. **Not adding the package.**

Consequence: Gemini traffic (Router triage, Compute) is metadata-only in Logfire. It remains fully
captured in BigQuery, so nothing is lost relative to the previous state. The instrumentation call is
kept in place and fails with one explanatory startup line, so it self-enables when upstream relaxes
the constraint. The stated priority — Smart, which runs `gpt-5.4-mini` — is on the OpenAI path and
is fully covered.

## Consequences

- The claim "sensitive payload never leaves the GCP perimeter" in
  `05_building_blocks/observability_strategy/README.md` §8 is **no longer true** and was rewritten.
- Scrubbing needs no configuration: Logfire lists the `gen_ai.*` content keys in its scrubber's
  `SAFE_KEYS`, so the default `password|secret|auth|session|…` patterns do not corrupt prompt text.
  The corollary is that scrubbing offers no protection there either — content is stored verbatim.
- Two spans now exist per LLM call: the SDK's (content, wire format) and the custom `llm.call`
  (metadata). Deliberate — the custom one carries `agent_type`, `turn` and `provider`, which semconv
  lacks and which dashboards group by.
- The deep-research Cloud Run Job needs `TRACING_BACKEND` + `LOGFIRE_TOKEN` in its own env to be
  visible; `init_telemetry()` was added to `job_main.py` but the job's environment is provisioned
  separately from `cloudbuild-dev.yaml`.
- Retained known gap: failed LLM calls still produce no BigQuery row (the exception exits before
  `base_agent.py:1181`); the SDK instrumentation does capture them on the Logfire side.

## Rejected alternatives

- **Per-agent content capture** (sensitive agents metadata-only, others full). Rejected by the owner
  as all-or-nothing; would also have forced the hand-rolled semconv path — more code, lower fidelity.
- **Keeping metadata-only and improving BigQuery** (add `parent_span_id`, make `llm.call` current,
  build a viewer). Rejected: real cost, and the end state is a worse version of an existing product.
- **Migrating to the EU region** for jurisdiction. Rejected: region is fixed at signup and Pydantic
  does not support migration; the existing project is US.
- **Removing Logfire** (the prior record's decision). Rejected: it was argued when the requirement
  was metadata-only panels duplicating BigQuery numbers. With content, Logfire provides the cascade
  view and MCP-driven SQL access from Claude Code — capabilities the in-house stack does not have.
