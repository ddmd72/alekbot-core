# Decision: specialist execution-context wiring + ASYNC generator timeout ceilings

**Status:** Adopted (2026-10-07).

## Incident

2026-10-07, 06:13-06:20 UTC: the single HTML-page generation delegated that day failed outright.
`GrokAdapter` sent one request (`grok-4.7`, `thinking=medium`) that did not return within
`HtmlPageGeneratorAgentConfig.request_timeout_s` (420s), and the agent died with:

```
llm_no_fallback →None cause=timeout http=None: request timeout after 420s
❌ html_page_generator_agent failed (no retry — non-transient):
   both providers unavailable: primary='' fallback=None primary_cause=LLMTimeoutError
```

`AgentProviderStrategy.STRATEGIES["html_page"]` declares `fallback: "gemini"`
(`src/services/agent_context_builder.py`), so this should have retried on Gemini instead of
failing the whole generation.

## Root cause

`BaseAgent._call_llm`'s cross-provider failover (`src/agents/base_agent.py`, the
`BothProvidersUnavailableError` branch) reads `ctx = self._agent_execution_context`. That
attribute defaults to `None` (`BaseAgent.__init__`) and is only ever set by
`self._set_execution_context(execution_context)` — called from the constructors of
`RouterAgent`, `SmartResponseAgent`, `QuickResponseAgent`, and `NotesAgent`.

`HtmlPageGeneratorAgent`, `PdfGeneratorAgent`, `DocPlannerAgent`, and `DocGeneratorAgent` — the
four ASYNC document/page generators — never called it. Each constructor only did
`self._llm = execution_context.provider`, so `self._agent_execution_context` stayed `None` for
the agent's entire lifetime. With `ctx is None`, `_call_llm` sets `primary_name = ""` and
`fallback_provider = None` unconditionally, so the failover branch raises
`BothProvidersUnavailableError` on the *first* FAILOVER-triggering error (timeout, 429, 503) —
the declared fallback is structurally unreachable, regardless of what the strategy table says.
This has been true since the `html_page` fallback was declared (2026-08-15); today's Grok timeout
is simply the first time it was exercised in production.

## Decision

1. **Wire the fallback.** Call `self._set_execution_context(execution_context)` in the
   constructors of all four affected agents, immediately after `super().__init__(config)` —
   same position NotesAgent already uses. The execution context is resolved once per user in
   `UserAgentFactory` and never changes for the agent's lifetime, so setting it once in
   `__init__` is sufficient; no per-message re-resolution is needed (the `fallback_ctx_override`
   escape hatch on `_call_llm` exists for agents that *do* resolve context per-message, which
   none of these four do).
2. **Raise the timeout ceiling for all four ASYNC generators** (owner decision, same day: these
   intents are ASYNC — nobody blocks on them — so a generous per-call ceiling costs nothing but
   wall-clock time, and the family shouldn't wait for each agent to individually time out in
   production before getting the same headroom):
   - `HtmlPageGeneratorAgentConfig`: `request_timeout_s` 420s → 900s, `timeout_ms` 600_000 →
     1_100_000. (Directly evidenced: grok-4.7, default since 2026-10-03, overran the old 420s
     ceiling calibrated against grok-4.6's 229s baseline.)
   - `PdfGeneratorAgentConfig`: added `request_timeout_s=900` (previously **unset** — the LLM
     call relied solely on the provider SDK's own default, e.g. OpenAI's 300s client ceiling,
     which risks the SDK silently retrying a slow call and paying for multiple generations before
     `timeout_ms` kills the agent anyway — the same waste HtmlPageGenerator's ceiling exists to
     avoid). `timeout_ms` 600_000 → 1_100_000.
   - `DocPlannerAgentConfig`: `request_timeout_s` 540s → 900s, `timeout_ms` 600_000 → 1_100_000.
   - `DocGeneratorAgentConfig`: `timeout_ms` 600_000 → 1_100_000 only. **No `request_timeout_s`
     added** — unlike the other three, this agent is a `MAX_TURNS=5` tool-calling loop, not a
     single LLM call; a flat 900s *per call* would allow up to 4500s across the loop, which
     doesn't match the "one generous ceiling" intent. Each turn still falls back to the provider
     SDK's own default (e.g. Claude's ~120s), bounded in aggregate by the raised `timeout_ms`.
   - `agent_manifest.py`: `dispatch_deadline_s` (Cloud Tasks) 720 → 1220 for all four descriptors
     (`DOC_PLANNER`, `DOC_GENERATOR`, `PDF_GENERATOR`, `HTML_PAGE_GENERATOR`) — new `timeout_ms`
     (1100s) + 2 min overhead. Cloud Tasks' own `dispatch_deadline` ceiling is 1800s, so 1220s
     leaves ample room.

## Rejected alternatives

- **Per-call `fallback_ctx_override`** instead of `_set_execution_context` in `__init__`: that
  parameter exists for agents resolving execution context per-message and must not mutate
  `self.llm`. All four affected agents resolve their context once, at construction — a one-time
  `_set_execution_context` call is the simpler, already-established pattern (NotesAgent).
- **A flat `request_timeout_s=900` on `DocGeneratorAgent`'s per-turn loop**: rejected — see above,
  the single-call budget doesn't transfer to a 5-turn tool loop without redesigning how much of
  the total budget each turn gets, which is out of scope here.
- **Leaving Pdf/DocPlanner/DocGenerator at their old ceilings** (original scoping of this fix,
  2026-10-07 same day): superseded by owner instruction to raise the ceiling for all four rather
  than wait for each to individually demonstrate the same failure.

## Verification

- `tests/unit/agents/test_{html_page_generator,pdf_generator,doc_planner,doc_generator}_agent.py`
  (175 tests) pass unchanged — including PdfGeneratorAgent's, confirming no test asserted the
  absence of `timeout` on its `LLMRequest`.
- `tests/unit/agents/core/test_base_agent_fallback.py`, `tests/unit/test_base_agent.py`,
  `tests/integration/test_provider_resilience_recovery_flow.py` pass unchanged.
- Full `make check` (6,461 tests) green, twice (once per round of changes).

## Triggers to revise

- If `DocGeneratorAgent`'s multi-turn loop starts timing out in a way that suggests its per-turn
  SDK-default budget is now the bottleneck, that's a separate, deliberate design question (what
  budget each of the 5 turns should get) — not a copy of this fix.
