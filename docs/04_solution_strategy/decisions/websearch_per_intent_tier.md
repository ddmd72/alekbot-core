# Decision: per-intent performance tier for WebSearchAgent

**Status:** Adopted
**Date:** 2026-07-29

## Context

WebSearchAgent serves two intents that look alike and are not. `search_web` is multi-angle
research — plan searches, weigh sources, reconcile contradictions. `fetch_url` opens one
known page and extracts the items named in the request. Both ran on the agent's single
tier (BALANCED → `gpt-5.6-luna` on OpenAI).

The morning briefing made the cost split visible: of its 40 web_search calls, **31 are
`fetch_url` and ~73% of the intent's cost**. Replaying a real briefing run through the
production adapter (`scripts/websearch/ab_nano_vs_luna.py`) showed ECO (`gpt-5.4-nano`)
costs 4.7x less across all 40 calls.

But a whole-agent downgrade fails. Measured on 7 real recorded user queries
(`scripts/websearch/ab_user_queries.py`), ECO on `search_web`:

- returned 6.0 findings on average vs 7.9 (on a Spanish legal query, 3 vs 8 — it stated
  the legal opening and omitted the procedure: renunciation of temporary protection, TIE
  documentation, salary threshold);
- dropped the OUTPUT_FORMAT JSON shape on 1 of 7, answering in prose;
- ran 38.7s average vs 18.2s, with a **79s outlier against `timeout_ms = 90_000`**.

On `fetch_url` the same model matched BALANCED on extracted-item count — but only after
the prompt contradiction was fixed (see TD-6): the shipped wording demanded "the complete
page text without omissions" while the per-call message asked for specific items. Luna
ignored the conflict; nano obeyed it and returned navigation menus.

## Decision

The tier is per intent. `search_web` uses the agent's resolved tier; `fetch_url` uses
`WebSearchAgentConfig.fetch_url_tier` (default `PerformanceTier.ECO`, `None` to disable).

`WebSearchAgent._fetch_model_name()` passes that **tier** to
`LLMPort.get_model_for_tier()` and lets the provider name the model — OpenAI answers
`gpt-5.4-nano`, Gemini flash-lite, Claude haiku. `_call_grounded_llm` takes `model_name`
as a parameter; each intent supplies its own.

Three properties this shape buys:

- **The agent never selects a model.** It declares task difficulty with a domain enum;
  model choice stays behind the port, honouring the CLAUDE.md rule "the agent does not
  select the model itself". No provider-name branching, no tier→model table in the agent.
- **Nothing per-call is stored on `self`** — `decisions/per_call_execution_context.md`.
  The resolved model is a local, so concurrent `fetch_url` and `search_web` executions on
  the same singleton instance cannot race.
- **It degrades safely.** A provider that does not publish the tier raises `ValueError`;
  the agent logs and falls back to its own model, which is exactly the pre-split behaviour.

## Alternatives rejected

- **Whole-agent ECO.** Simplest, and briefly live (2026-07-29, Firestore
  `agent_tiers.web_search`). Reverted: it degrades user-facing search for a saving that
  lives almost entirely in `fetch_url`. The latency outlier against a 90s agent timeout
  made it an availability risk, not just a quality one.
- **Provider-scoped map** (`{"openai": ECO}`) keyed on
  `AgentExecutionContext.provider_name`. Matches the measured path exactly and touches
  nothing else, but moves provider names into agent config and repurposes a field
  documented as "for structured logging" into a behavioural switch. Rejected as the
  dirtier of two working options; revisit if an adapter's ECO turns out unfit for fetching.
- **Reviving `websearch_light` as a separate agent_type.** Firestore already holds that
  profile, blueprint and tokens with zero code references. It would give the fetch path
  its own tier *and* its own Firestore prompt (closing TD-6 too), but it means a second
  agent in the manifest, registry and factory for one intent. Rejected by the owner —
  not worth resurrecting an agent just for fetching.
- **Per-intent tier on `AgentDescriptor` + `ExecutionOverride`.** The most reusable and
  architecturally honest option, and it reuses the blessed per-call override mechanism.
  Rejected as YAGNI today: `AgentCoordinator` does not build execution contexts for
  specialists at all, so it would need `UserBotConfig` at dispatch time and new machinery
  for a single intent. This is the shape to adopt if a second agent ever needs per-intent
  tiers.

## Consequences

- Only OpenAI was measured. Gemini and Claude are reached on the WebSearch failover path;
  their ECO models are unverified for fetching. The `None` switch is the escape hatch.
- **Failover asymmetry, deliberate:** if `BaseAgent._call_llm` falls over to the fallback
  provider it uses `AgentExecutionContext.fallback_model_name` — the fallback's BALANCED
  model, not this tier. On the failover path completing the request outranks its price.
- The saving is not yet realised in production: it requires the TD-6 prompt fix to ship
  together with this change, since ECO on the old wording is worse than BALANCED on it.
- `search_web` cost is untouched. The briefing's remaining lever is structural — 31
  sources x fetch + a widen round + a verify round — which is separate work.

## Verification

`tests/unit/agents/test_web_search_fetch_prompt.py` — tier resolution goes through the
port with the declared tier, the resolved model reaches `LLMRequest.model_name`, the two
intents do not share a model, `None` disables the downgrade, and an unsupported tier
degrades to the agent's model instead of raising.
