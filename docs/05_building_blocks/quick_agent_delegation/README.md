# Quick Agent Delegation (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the delegation mechanism inside `QuickResponseAgent`: how it optionally calls specialist
agents to retrieve memory or run lightweight web searches, while preserving its low-latency
contract.

### When to Read

- **For AI Agents:** Before changing QuickAgent prompt behavior, adding intents, or modifying
  `WebSearchLightAgent`.
- **For Developers:** When debugging Quick delegation flow, tuning `MAX_DELEGATION_TURNS`, or
  adding a new intent to the Quick path.

### When to Update

This document MUST be updated when:

- [ ] `AgentDescriptor.allowed_intents` or `intent_remap` for QuickAgent changes in `agent_manifest.py`.
- [ ] `MAX_DELEGATION_TURNS` changes.
- [ ] `DelegationEngine` loop mechanics, dispatch, or parallel execution changes.
- [ ] The Firestore token `PROTOCOL_QUICK_AGENT_SELECTION` is updated.
- [ ] `WebSearchLightAgent` model tier or output format changes.
- [ ] `_clean_history_for_quick` behavior changes.

### Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Agent Registry:** [../agent_registry/README.md](../agent_registry/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

`QuickResponseAgent` handles simple queries (complexity ≤5 as classified by RouterAgent). While
the majority of Quick responses require no tool use, the agent supports a bounded delegation loop
using the same non-internal intents as SmartAgent (discovered via `AgentDescriptor.allowed_intents=None`):

- `search_memory` — retrieve biographical facts from the user's memory base.
- `search_web` — web search + automatic maps fan-out (see below).
- `search_emails` — semantic search in indexed email archive.
- `get_email_details` — fetch full body of a specific email by ID.
- `get_email_attachment` — parse an email attachment as text.

Note: `maps_query` is `internal=True` — not shown in LLM tool declarations. Triggered automatically
via `intent_fanout` when the orchestrator dispatches `search_web` (see § 3.5).

**Key constraint:** The Quick path must stay fast. Delegation is bounded to `MAX_DELEGATION_TURNS=5`
and the web search is limited to the ECO-tier specialist. Complex multi-step research stays in SmartAgent.

**Delegation trees:**

```
RouterAgent
  ├─ Simple Query → QuickResponseAgent
  │       ├─ search_memory        → MemorySearchAgent    (shared)
  │       ├─ search_web           → WebSearchAgent       (shared)
  │       │       └─ [fan-out]    → MapsSearchAgent      (parallel, internal)
  │       ├─ search_emails        → EmailSearchAgent     (shared)
  │       ├─ get_email_details    → EmailSearchAgent     (shared)
  │       └─ get_email_attachment → EmailSearchAgent     (shared)
  └─ Complex Query → SmartResponseAgent
          ├─ search_memory        → MemorySearchAgent    (shared)
          ├─ search_web           → WebSearchAgent       (shared)
          │       └─ [fan-out]    → MapsSearchAgent      (parallel, internal)
          ├─ search_emails        → EmailSearchAgent     (shared)
          ├─ get_email_details    → EmailSearchAgent     (shared)
          └─ get_email_attachment → EmailSearchAgent     (shared)
```

Both agents use the same `AgentRegistry`. Quick calls `get_available_intents_for(descriptor)` which
returns the same non-internal intents as Smart (including `search_web`). `intent_remap` is currently
disabled on both orchestrators (`{}`). `intent_fanout` is configured on both:
`{search_web: FanoutSpec(intents=[maps_query], hint="...")}` — when the LLM dispatches
`search_web`, the DelegationEngine also dispatches `maps_query` in parallel and merges results
into a single labeled tool response. Controlled by `PROTOCOL_QUICK_AGENT_SELECTION` Firestore token.

---

## 2. Delegation Loop

### 2.1 Entry Point: `DelegationEngine.execute()`

Quick (and Smart) agents delegate loop mechanics to the shared `DelegationEngine`
(`src/infrastructure/delegation_engine.py`). The agent builds the `LLMRequest` (model, temperature,
schema, tools) and passes it to the engine. The engine owns the iteration, tool dispatch, history
management, and parallel execution.

```
execute()
  │
  ├─ 0. Load biographical context (get_biographical_context_cached)
  ├─ 1. Merge Router semantic enrichment
  ├─ 2. Build system prompt via PromptBuilder v3 (agent_type="quick")
  │       └─ Includes PROTOCOL_QUICK_AGENT_SELECTION token
  ├─ 3. Load conversation history (last 20 messages, tiered compression)
  ├─ 4. _clean_history_for_quick()
  │       └─ Removes all tool_call / tool_response turns from history
  ├─ 5. Build LLMRequest (model, system_prompt, messages, tools, response_schema)
  │
  └─ DelegationEngine.execute(call_llm, base_request, context, ...) [up to MAX_DELEGATION_TURNS=5]
         Turn N:
           a. LLM call via agent's _call_llm (billing + debug handled by BaseAgent)
           b. No tool_calls in response? → return DelegationResult(text=...)
           c. Has tool_calls? → memory-first parallel dispatch → append to history → next turn
         If MAX_DELEGATION_TURNS exhausted → return DelegationResult(failed=True)
  │
  Post-processing (in QuickAgent):
    parse_llm_response(result.text) → (full_response, summary, rich)
    → return AgentResponse
```

### 2.2 Parallel Execution (DelegationEngine)

Tool calls within a single turn are executed with memory-first ordering inside the engine:

```
DelegationEngine._execute_tool_calls(tool_calls)
  │
  ├─ Separate: memory_calls = [c for c if intent="search_memory"]
  ├─ Separate: other_calls  = [c for c if intent!="search_memory"]
  │
  ├─ 1. Execute memory_calls sequentially (await one by one)
  │        → Ensures memory facts are available when the LLM composes the next turn
  │
  └─ 2. Execute other_calls in parallel (asyncio.gather)
           → Currently: web_search_light calls run concurrently
```

**Rationale for memory-first:** If the LLM simultaneously calls `search_memory` and
`search_web_light`, the memory result may change how the LLM interprets the web result. Running
memory first avoids a wasted extra turn.

### 2.3 History Cleaning: `_clean_history_for_quick()`

Before each LLM call, the conversation history is cleaned to remove all tool scaffolding:

```
Raw session history:
  user:  "What's the weather?"
  tool_call: search_web_light(...)
  tool_response: "22°C sunny"
  model: "The weather is 22°C and sunny."
  user:  "And tomorrow?"

Cleaned history (injected into Quick's LLM call):
  user:  "What's the weather?"
  model: "The weather is 22°C and sunny."
  user:  "And tomorrow?"
```

**Why clean:** Quick's context window is smaller than Smart's. Tool call/response pairs from prior
turns add noise and tokens. The cleaned history reads as natural conversation — no LLM confusion
about whether to call tools again for the same question.

---

## 3. Available Intents

Intents are discovered at runtime from `coordinator.get_available_intents_for(self._descriptor)`.
The descriptor has `allowed_intents=None` (all non-internal) and `intent_remap={"search_web":
"search_web_light"}`. The LLM sees all non-internal intents (same as Smart, including `search_web`).
At dispatch time, `_INTENT_REMAP` silently routes `search_web` to `WebSearchLightAgent`. No hardcoded
intent set in agent code.

### 3.1 search_memory

Routes to `MemorySearchAgent` (shared with Smart path). Same two-phase execution: LLM key
formulation → multi-vector RRF search via `SearchEnrichmentService`.

**When to call (per `PROTOCOL_QUICK_AGENT_SELECTION`):**
- User references personal data not covered by the biographical context already in the prompt.
- Query explicitly asks about stored facts ("What did I say about...", "Where's my X").

**Anti-patterns:** Calling for general knowledge questions; passing raw user message with
unresolved references ("it", "that project", "the one we discussed").

### 3.2 search_web → (remapped) → WebSearchLightAgent

The LLM calls `search_web` (same intent name as Smart). At dispatch time `_INTENT_REMAP` substitutes
it to `search_web_light`, routing to `WebSearchLightAgent`. Single Gemini + Google Search grounding call.

**When to call (per `PROTOCOL_QUICK_AGENT_SELECTION`):**
- Current date/time, current prices, today's weather, single-fact external lookup.
- Query has a short, precise, standalone answer.

**Anti-patterns:** Multi-part research queries; comparative analysis; anything that would benefit
from synthesis of multiple sources (send those to Smart instead).

### 3.3 search_emails / get_email_details / get_email_attachment → EmailSearchAgent

Routes to `EmailSearchAgent` (shared with Smart path). Same three intents and behavior as on the
Smart path — no remap applied.

- `search_emails` — semantic 4-vector RRF search in the indexed email archive. Pass `query` as-is.
- `get_email_details` — fetch full email body from Gmail API. Requires `email_id` in `context`.
- `get_email_attachment` — parse attachment as text via markitdown. Requires `email_id` + `filename` in `context`.

**When to call `search_emails` (per `PROTOCOL_QUICK_AGENT_SELECTION`):**
- User asks about a specific email, sender, invoice, document, or email thread.
- Do not call if the needed email is already present in `email_search_context` in the conversation
  history — use its `id` directly with `get_email_details` instead.

**Email search context in history:** After any delegation turn that included email searches, the
model history entry contains a JSON block:
```json
{"email_search_context":[{"you_searched":"...","you_received":[{"id":"...","from":"...","date":"...","summary":"..."}]}]}
```
Use `id` from prior turns directly with `get_email_details` / `get_email_attachment` without
re-searching. See [Multi-Agent System § 9](../multi_agent_system/README.md#9-conversationhandler-email-search-context-persistence).

### 3.4 maps_query → MapsSearchAgent (via intent fan-out)

`maps_query` is `internal=True` — the LLM never sees it in its tool list.
Instead, it is triggered automatically by `intent_fanout` on `search_web`.

When the DelegationEngine dispatches `search_web`, it checks the orchestrator's `intent_fanout`
config: `{search_web: FanoutSpec(intents=[maps_query], hint="...")}`. Both `search_web` and
`maps_query` run in parallel via `asyncio.gather`. Results are merged into a single tool response:

```
SYSTEM: This query was automatically dispatched to multiple specialists in parallel.
<reconciliation hint from FanoutSpec>

[Primary specialist: Web Search]
<web search results>

[Additional specialist: Maps]
<maps results — places, routes, weather, Google Maps links>
```

The orchestrator LLM synthesizes both into a single response. The hint instructs it which source
to trust for which data type (geodata → Maps, reviews → Web).

MapsSearchAgent routes to `MapsSearchAgent` (shared with Smart path). Model pinned to
`gemini-2.5-flash` (Maps grounding not supported on Gemini 3.x). Multi-turn MCP tool loop
with cognitive process triage: FULL_MATCH (deep search), PARTIAL (enrichment), NO_MATCH
(responds "no relevant geographic data").

**Fan-out behavior:** If MapsSearchAgent fails or returns empty — secondary failure is silently
skipped, web search result returned alone. The orchestrator never sees an error from maps.

### 3.5 Intent Fan-out Mechanism

`intent_fanout` is a declarative field on `AgentDescriptor`, analogous to `intent_remap`.
Configured via `FanoutSpec(intents: List[str], hint: str)`. The engine applies it in
`_dispatch_single()` after intent remap, before coordinator dispatch.

Currently configured on both Quick and Smart:
```python
intent_fanout={Intent.SEARCH_WEB: FanoutSpec(
    intents=[Intent.MAPS_QUERY],
    hint="For places, distances, routes... trust Maps over Web..."
)}
```

Each orchestrator can have independent fan-out config (different secondary intents, different
hints). The `hint` field provides per-mapping conflict resolution instructions visible to the
LLM in the tool response.

---

## 4. WebSearchLightAgent

### 4.1 Architecture

```
execute(message)
  │
  ├─ Build prompt:
  │    IF prompt_builder → PromptBuilder.build_for_agent(agent_type="websearch_light")
  │    ELSE → inline Groovy cognitive_process fallback (no external dependency)
  │
  ├─ Augment query:
  │    "// Context Injection
  │     current_date = '...'
  │     user_query = '...'
  │     {prompt}
  │     // Execute
  │     WebSearchLightAgent.run(user_query)"
  │
  └─ Single LLMRequest:
       model_name: ECO tier provider (gemini-flash-lite-latest)
       tools: [grounding_tool]          ← Google Search grounding
       temperature: 0.5
       system_instruction: ""           ← All context in user message
       → Returns response.text as plain Slack mrkdwn
```

### 4.2 Key Properties

| Property | Value |
|----------|-------|
| Tier | ECO (gemini-flash-lite-latest) |
| Passes | Single (no multi-turn) |
| Output format | Plain Slack mrkdwn (no JSON) |
| Tools | Google Search grounding only |
| Temperature | 0.5 |
| Alternative fallback | `["memory_search_agent"]` |
| Prompt source | PromptBuilder v3 (`agent_type="websearch_light"`) with inline fallback |

### 4.3 Why Not the Full WebSearchAgent?

`WebSearchAgent` (Smart path) loads session history, builds a full system prompt, and may run
synthesis across multiple sources. For Quick delegation, that overhead is unacceptable. The light
variant:
- Uses the cheapest model tier (ECO vs BALANCED).
- Makes a single call with no preamble.
- Returns raw grounding text without post-processing synthesis.
- Cannot be combined with function calling in the same request (Gemini API limitation) — hence it
  is a separate dedicated agent.

---

## 5. Output Format

QuickAgent's LLM is instructed (via `OUTPUT_FORMAT_JSON` token) to return a JSON envelope:

```json
{
  "full_response":    "Complete answer in Slack mrkdwn — shown to the user",
  "response_summary": "≤300 chars compressed for session history",
  "rich_content":     {
    "type":     "widget",
    "data":     { "html": "...", "alt_text": "..." },
    "fallback": "plain text if rich rendering not supported"
  }
}
```

**rich_content** is optional (`null` for plain answers). The JSON envelope is wrapped in a
` ```json ``` ` code block by the LLM and parsed by `parse_llm_response`.

**History storage:** `response_summary` is used directly as the history text — no
`HistorySummaryService` call needed. `HistorySummaryService` fires only as fallback when the LLM
returns plain text instead of JSON (non-JSON path).

---

## 6. Comparison: Quick vs Smart Delegation

| Aspect | QuickAgent | SmartAgent |
|--------|-----------|------------|
| Max turns | **5** | 5 |
| LLM-visible intents | Same non-internal set as Smart (memory, web, email × 3, maps) | All non-internal (via AgentDescriptor) |
| Intent remap at dispatch | `search_web` → `search_web_light` (`_INTENT_REMAP`) | None |
| Registry | `AgentRegistry.get_available_intents_for(descriptor)` | `AgentRegistry.get_available_intents()` |
| Prompt token | `PROTOCOL_QUICK_AGENT_SELECTION` | `PROTOCOL_SMART_AGENT_SELECTION` |
| Web specialist | WebSearchLightAgent (ECO, single pass, `internal=True`) | WebSearchAgent (BALANCED, full synthesis) |
| History cleaning | Yes (`_clean_history_for_quick`) | Yes (`_sanitize_tool_history`) |
| Output format | JSON envelope always | `deliver_response` terminal tool |
| Delegation engine | `DelegationEngine` (shared) | `DelegationEngine` (shared, `terminal_tool="deliver_response"`) |
| History summary | From JSON field, HistorySummaryService as fallback | `HistorySummaryService` fire-and-forget |
| Model tier | BALANCED | PERFORMANCE |

---

## 7. Code References

- `src/agents/core/quick_response_agent.py` — `MAX_DELEGATION_TURNS=5`, `_clean_history_for_quick`, `_get_quick_tool_declarations`, `_sanitize_response`. Builds `LLMRequest` and delegates loop to `DelegationEngine`. Post-processes `DelegationResult.text` via `parse_llm_response`.
- `src/infrastructure/delegation_engine.py` — `DelegationEngine.execute()` — reusable multi-turn tool-calling loop. Owns loop iteration, memory-first parallel dispatch, history management. Shared by Quick, Smart, and bound channel agents.
- `src/agents/base_agent.py` — lifecycle hooks + `_call_llm` (billing + debug) + `_build_delegate_tool_declaration` (static, builds tool schema from registry)
- `src/infrastructure/agent_registry.py` — `AgentDescriptor`, `get_available_intents_for(descriptor)`
- `src/infrastructure/agent_config.py` — Central config registry for all agents. `QuickAgentConfig` (`QUICK` instance): `context_window`, `max_delegation_turns`, `intent_remap`, `timeout_ms`. All specialist agent timeouts also sourced from here via `user_agent_factory.py`.
- `src/agents/web_search_light_agent.py` — WebSearchLightAgent implementation
- `src/services/agent_context_builder.py` — `AgentProviderStrategy` entry for `"web_search_light"`: ECO tier, Gemini only, `required_capabilities: ["native_tools"]`
- `src/domain/user.py` — `_DEFAULT_AGENT_TIERS`: `"web_search_light": PerformanceTier.ECO`
- `src/utils/llm_response_parser.py` — `parse_llm_response` for JSON envelope parsing
- Firestore token: `PROTOCOL_QUICK_AGENT_SELECTION` — when/how Quick delegates
- Firestore token: `OUTPUT_FORMAT_JSON` — JSON output schema for QuickAgent

---

## 8. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-04-07 — DelegationEngine extraction (loop/dispatch/parallel moved from agent to infrastructure)
