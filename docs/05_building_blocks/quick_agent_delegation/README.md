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

- [ ] `QUICK_INTENTS` constant in `quick_response_agent.py` changes.
- [ ] `MAX_DELEGATION_TURNS` changes.
- [ ] `_execute_quick_delegation_loop` or `_execute_quick_parallel` logic changes.
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
for two specific intents:

- `search_memory` — retrieve biographical facts from the user's memory base.
- `search_web_light` — run a single-pass Google Search grounding lookup.

**Key constraint:** The Quick path must stay fast. Delegation is bounded to `MAX_DELEGATION_TURNS=2`
and the intent set is intentionally narrow. Complex multi-step research stays in SmartAgent.

**Two separate delegation trees exist in the system:**

```
RouterAgent
  ├─ Simple Query → QuickResponseAgent
  │       ├─ search_memory  → MemorySearchAgent   (shared)
  │       └─ search_web_light → WebSearchLightAgent (Quick-only)
  └─ Complex Query → SmartResponseAgent
          ├─ search_memory  → MemorySearchAgent   (shared)
          └─ search_web     → WebSearchAgent       (Smart-only)
```

SmartAgent delegates via `AgentRegistry` + `PROTOCOL_SMART_AGENT_SELECTION`. QuickAgent uses a
simpler mechanism: `QUICK_INTENTS` constant + `PROTOCOL_QUICK_AGENT_SELECTION` Firestore token.
No `AgentRegistry` object is involved in the Quick path.

---

## 2. Delegation Loop

### 2.1 Entry Point: `_execute_quick_delegation_loop()`

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
  │
  └─ _execute_quick_delegation_loop() [loop up to MAX_DELEGATION_TURNS=2]
         Turn N:
           a. Build delegate_to_specialist tool with QUICK_INTENTS
           b. LLM call (BALANCED tier provider)
           c. No tool_calls in response?
              → parse_llm_response(response.text) → (full_response, summary, rich)
              → return AgentResponse immediately
           d. Has tool_calls?
              → _execute_quick_parallel(tool_calls)
              → Append tool results to history
              → Next turn
         If MAX_DELEGATION_TURNS exhausted without final response → return partial
```

### 2.2 Parallel Execution: `_execute_quick_parallel()`

Tool calls within a single turn are executed with memory-first ordering:

```
_execute_quick_parallel(tool_calls)
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

## 3. Available Intents (QUICK_INTENTS)

`QUICK_INTENTS = {"search_memory", "search_web_light"}`

These are injected into the `delegate_to_specialist` tool description via
`_get_quick_tool_declarations()`. The LLM only sees these two intents — it cannot accidentally
call Smart-path intents like `search_web`.

### 3.1 search_memory

Routes to `MemorySearchAgent` (shared with Smart path). Same two-phase execution: LLM key
formulation → multi-vector RRF search via `SearchEnrichmentService`.

**When to call (per `PROTOCOL_QUICK_AGENT_SELECTION`):**
- User references personal data not covered by the biographical context already in the prompt.
- Query explicitly asks about stored facts ("What did I say about...", "Where's my X").

**Anti-patterns:** Calling for general knowledge questions; passing raw user message with
unresolved references ("it", "that project", "the one we discussed").

### 3.2 search_web_light → WebSearchLightAgent

Routes to `WebSearchLightAgent`. Single Gemini + Google Search grounding call.

**When to call (per `PROTOCOL_QUICK_AGENT_SELECTION`):**
- Current date/time, current prices, today's weather, single-fact external lookup.
- Query has a short, precise, standalone answer.

**Anti-patterns:** Multi-part research queries; comparative analysis; anything that would benefit
from synthesis of multiple sources (send those to Smart instead).

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
    "type":     "html_card",
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
| Max turns | 2 | 5 |
| Intent set | `search_memory`, `search_web_light` | `search_memory`, `search_web` |
| Registry | None (`QUICK_INTENTS` constant) | `AgentRegistry` (ACP v2) |
| Prompt token | `PROTOCOL_QUICK_AGENT_SELECTION` | `PROTOCOL_SMART_AGENT_SELECTION` |
| Web specialist | WebSearchLightAgent (ECO, single pass) | WebSearchAgent (BALANCED, full synthesis) |
| History cleaning | Yes (`_clean_history_for_quick`) | No |
| Output format | JSON envelope always | `deliver_response` tool |
| History summary | From JSON field, HistorySummaryService as fallback | `HistorySummaryService` fire-and-forget |
| Model tier | BALANCED | PERFORMANCE |

---

## 7. Code References

- `src/agents/core/quick_response_agent.py` — `QUICK_INTENTS`, `MAX_DELEGATION_TURNS`, `_execute_quick_delegation_loop`, `_execute_quick_parallel`, `_clean_history_for_quick`, `_get_quick_tool_declarations`, `parse_llm_response`
- `src/agents/web_search_light_agent.py` — WebSearchLightAgent implementation
- `src/services/agent_context_builder.py` — `AgentProviderStrategy` entry for `"web_search_light"`: ECO tier, Gemini only, `required_capabilities: ["native_tools"]`
- `src/domain/user.py` — `_DEFAULT_AGENT_TIERS`: `"web_search_light": PerformanceTier.ECO`
- `src/utils/llm_response_parser.py` — `parse_llm_response` for JSON envelope parsing
- Firestore token: `PROTOCOL_QUICK_AGENT_SELECTION` — when/how Quick delegates
- Firestore token: `OUTPUT_FORMAT_JSON` — JSON output schema for QuickAgent

---

## 8. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-02-26
