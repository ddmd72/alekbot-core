# ADR-004: Agent Handoff Pattern for Web Search

**Status:** Accepted (2026-01-23) — still in force.

## Context

Grounded web search is a provider-native tool (Gemini: Google Search; OpenAI: `web_search`; Claude: `web_search` +
`web_fetch`). When this was decided, the Gemini API refused a request that combined its Google Search grounding tool with
custom function declarations (`400 INVALID_ARGUMENT`). The orchestrator needs its custom tools (`delegate_to_specialist`,
local tools) on every call, so it could not also carry the grounding tool.

## Decision

Web search is a **specialist agent** the orchestrator delegates to, not a tool on the orchestrator's own call.
`WebSearchAgent` (`src/agents/web_search_agent.py`, intents `search_web` and `fetch_url`) sends one request with
`use_grounding=True` and no custom tools; each LLM adapter injects its own native search tool. Smart reaches it through
`delegate_to_specialist` → `DelegationEngine` → `AgentCoordinator`, like any other specialist, and synthesises the answer.

## Rejected alternatives

- **Google Custom Search Engine as a custom tool** — snippets too thin, poor relevance without tuning, no synthesis.
- **Intent pre-routing** (classify first, run a grounded call, inject the result) — adds a sequential step to every
  search turn and takes the decision away from the orchestrator.
- **Grounding on the orchestrator's call** — blocked by the provider constraint above.

## Consequences

- One extra LLM call per search, paid for clean separation: the specialist has its own model, tier and prompt
  (`fetch_url` runs at ECO, see `decisions/websearch_per_intent_tier.md`).
- Smart can fan out a search and a memory lookup in one tool batch.
- The same pattern carries `WebSearchLightAgent`, Lelik's one-call grounded lookup.

## Revisit when

Every configured orchestrator provider accepts native search alongside function declarations **and** the extra
specialist call shows up as a cost or latency problem.
