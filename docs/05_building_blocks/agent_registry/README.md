# Agent Registry (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the dynamic agent discovery system (ACP v2) that decouples SmartAgent from specialist
implementations. The foundation of scalable multi-agent delegation.

### When to Read

- **For AI Agents:** Before modifying how SmartAgent calls specialists, changing intent routing, or
  adding a new specialist agent.
- **For Developers:** When registering a new agent, extending the worker handler, or changing
  execution modes.

### When to Update

This document MUST be updated when:

- [ ] A new specialist agent is registered in `main.py`.
- [ ] `AgentManifest` or `AgentRegistry` interface changes.
- [ ] `coordinator.handle_delegation()` routing logic is modified.
- [ ] The Firestore token `PROTOCOL_SMART_AGENT_SELECTION` changes.
- [ ] An intent's execution mode changes (SYNC ↔ ASYNC).
- [ ] MemorySearchAgent key formulation schema (`MEMORY_SEARCH_RESPONSE_SCHEMA`) changes.

### Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)
- **RFC:** [../../10_rfcs/ACP_V2_SIMPLIFIED_RFC.md](../../10_rfcs/ACP_V2_SIMPLIFIED_RFC.md)

---

## 1. Overview

The **Agent Registry** is the dynamic discovery system at the core of ACP v2. It maps abstract
*intents* to specialist agent implementations, so SmartAgent can delegate tasks without knowing
which agent handles them or how.

**Problem solved:** In ACP v1, every new integration required adding a new hardcoded tool to
SmartAgent — prompt bloat, tight coupling, poor LLM accuracy. The Registry absorbs all growth:
SmartAgent has 1 fixed delegation tool forever; the registry grows, SmartAgent never changes.

**Core Principle:** SmartAgent knows *what* (intent names), not *how* (implementations). The
registry translates intent → agent manifest → execution path.

---

## 2. Architecture

```
SmartAgent (LLM)
  │  calls: delegate_to_specialist(intent="search_memory", query="...")
  ▼
AgentCoordinator.handle_delegation()
  │  registry.get_agent_for_intent("search_memory")
  │  → AgentManifest(agent_id="memory_search_agent", intents={"search_memory": SYNC}, ...)
  │
  ├─ SYNC path ──────────────────────────────────────────────────────────
  │    _execute_sync()
  │      Resolves per-user agent_id: "memory_search_agent_{user_id}"
  │      Creates AgentMessage(intent=QUERY, payload={query, **context.params})
  │      await route_message(message) → AgentResponse
  │      Returns result inline to SmartAgent
  │
  └─ ASYNC path (future: Gmail indexing) ────────────────────────────────
       _execute_async()
         await task_queue.enqueue_agent_task(agent_id, intent, query, context) → task_id
         Returns AgentResponse(result={"status": "started", "task_id": task_id})
         [Cloud Tasks → /worker → AgentWorkerHandler → execute → notify user]
```

---

## 3. AgentRegistry

Located at `src/infrastructure/agent_registry.py`.

### 3.1 AgentManifest

```python
@dataclass
class AgentManifest:
    agent_id: str                      # "memory_search_agent"
    intents: Dict[str, ExecutionMode]  # {"search_memory": ExecutionMode.SYNC}
    description: str                   # Injected into SmartAgent prompt dynamically
    requires_auth: bool = False        # OAuth requirement flag (future use)
```

### 3.2 ExecutionMode

```python
class ExecutionMode(str, Enum):
    SYNC  = "sync"   # Immediate — returns result inline (search queries, <5s)
    ASYNC = "async"  # Background — Cloud Tasks + user notification (long-running tasks)
```

### 3.3 Current Registry (as of 2026-02-21)

| Agent ID | Intent | Mode | Description |
|----------|--------|------|-------------|
| `memory_search_agent` | `search_memory` | SYNC | Semantic search through biographical facts and personal knowledge |
| `web_search_agent` | `search_web` | SYNC | Real-time web search for current information |

Both registered in `main.py` at startup. GcpTaskQueue is only instantiated in HTTP mode when
`GOOGLE_CLOUD_PROJECT` is present; otherwise coordinator operates SYNC-only.

---

## 4. SmartAgent as Generic Orchestrator

SmartAgent exposes exactly one delegation tool to the LLM:

```python
delegate_to_specialist(
    intent: str,    # one of the available intents
    query:  str,    # self-contained query, resolvable without prior context
    context: dict   # optional extra parameters (e.g., search enrichment hints)
)
```

Available intents are injected into the tool description at runtime from
`coordinator.get_available_intents()`. When a new agent is registered in `main.py`, SmartAgent's
LLM automatically sees the new intent — no code change required.

### 4.1 Memory-First Parallel Optimization

When SmartAgent detects `intent == "search_memory"` among multiple concurrent delegations, it
schedules memory search first so retrieved facts are available when the LLM formulates the final
response. Implemented in `_execute_agents_smart_parallel()` in `smart_response_agent.py`.

---

## 5. Firestore Prompt: PROTOCOL_SMART_AGENT_SELECTION

The canonical rules for when and how SmartAgent uses `delegate_to_specialist` live in the Firestore
token `PROTOCOL_SMART_AGENT_SELECTION`. This token is the *behavior specification* for the
delegation protocol — the code tool definition is generic; the Firestore token makes it intelligent.

### 5.1 memory_search_agent (`search_memory`)

- **When:** User asks about personal data requiring KB retrieval beyond the biographical baseline.
- **How:** Formulate a **self-contained query** — resolve conversational references ("this", "tell
  me more", "the project I mentioned") using conversation history before delegating. The query must
  be understandable without prior context.
- **Anti-patterns:** Passing the raw user message verbatim when it contains unresolved anaphora;
  using for external/real-time information.

### 5.2 web_search_agent (`search_web`)

- **When:** External, current, or real-time information (news, prices, world facts, documentation).
- **How:** Pass the user's question naturally, preserving their language.
- **Anti-patterns:** Using for personal data questions; changing the query language unnecessarily.

---

## 6. MemorySearchAgent: LLM Key Formulation

Before calling `SearchEnrichmentService`, MemorySearchAgent runs a **key formulation step** via
Gemini Flash (ECO tier). This is the bridge between a natural language delegation query and the
multi-vector search system.

### 6.1 Why

`SearchEnrichmentService` requires 3 distinct, non-overlapping inputs for its multi-vector strategy:
`keywords` (tag matching), `primary_query` (direct semantic vector), `alternative_query` (diversity
vector). A raw natural language query cannot fill all 3 effectively — an LLM sub-call optimizes
each channel independently.

### 6.2 Firestore Prompt: COGNITIVE_PROCESS_MEMORY_SEARCH

Token class: `cognitive_process`. This prompt instructs Gemini to act as a "Memory Search Key
Extractor" and produce a structured JSON output:

```json
{
  "keywords":         ["3–5 terms", "1–2 words each", "English only"],
  "primary_query":    "max 50 chars — direct intent description",
  "alternative_query":"max 50 chars — different phrasing for diversity recall",
  "domains":          ["up to 2 values from 15-value enum"]
}
```

**Domain enum (15 values):** `biographical`, `possession`, `health`, `medical_records`, `location`,
`work`, `network`, `preference`, `skill`, `project`, `finance`, `education`, `legal`,
`entertainment`, `communication`.

### 6.3 Schema Enforcement (API-Level)

`MEMORY_SEARCH_RESPONSE_SCHEMA` in `memory_search_agent.py` enforces constraints at the Gemini API
level via `response_schema` (structured output):

| Field | Constraint | Level |
|-------|-----------|-------|
| `keywords` | minItems=3, maxItems=5 | API |
| `domains` items | enum of 15 values | API |
| `domains` | maxItems=2 | API |
| `primary_query` | maxLength=50 chars | API |
| `alternative_query` | maxLength=50 chars | API |

### 6.4 Key → SearchEnrichmentService Mapping

| LLM output field | `enrich_context()` parameter |
|-----------------|------------------------------|
| `keywords` | `keywords` |
| `primary_query` | `search_phrase_1` |
| `alternative_query` | `search_phrase_2` |
| `domains` | `relevant_domains` |

---

## 7. AgentCoordinator: handle_delegation()

Added to the existing coordinator without modifying `route_message()`, `register_agent()`, or
`parallel_execute()` — fully backward compatible with ACP v1.

### 7.1 SYNC Flow

```
handle_delegation(intent="search_memory", query="...", context={user_id, account_id, params})
  ├─ registry.get_agent_for_intent("search_memory") → AgentManifest
  ├─ _execute_sync(manifest, intent, query, context)
  │    ├─ agent_id = f"{manifest.agent_id}_{context['user_id']}"
  │    ├─ message = AgentMessage(sender="coordinator", recipient=agent_id,
  │    │                         intent=QUERY, payload={query, **context.params})
  │    └─ return await route_message(message)
  └─ Returns AgentResponse directly to SmartAgent
```

### 7.2 ASYNC Flow

```
handle_delegation(intent="index_gmail", query="...", context={...})
  ├─ registry.get_agent_for_intent("index_gmail") → AgentManifest (mode=ASYNC)
  ├─ _execute_async(manifest, intent, query, context)
  │    └─ task_id = await task_queue.enqueue_agent_task(agent_id, intent, query, context)
  └─ Returns AgentResponse(result={"status": "started", "task_id": task_id})
```

### 7.3 Unknown Intent

Returns `AgentResponse(status=FAILED, error="UNKNOWN_INTENT")`. SmartAgent surfaces an appropriate
user-facing message without exposing internal details.

---

## 8. AgentWorkerHandler

Handles `task_type="agent_execution"` payloads at the `/worker` Cloud Tasks endpoint:

```python
payload = {
    "task_type": "agent_execution",
    "agent_id":  "gmail_agent",
    "intent":    "index_gmail",
    "query":     "...",
    "context":   {"user_id": ..., "account_id": ...}
}
```

Routes via `coordinator.route_message()`. User notification deferred — a platform-agnostic callback
will be implemented alongside the first ASYNC agent (Gmail indexing).

---

## 9. Adding a New Agent

### Step 1: Implement the agent

```python
# src/agents/gmail_agent.py
class GmailAgent(BaseAgent):
    async def process(self, message: AgentMessage) -> AgentResponse:
        ...
```

### Step 2: Register in main.py (3 lines)

```python
agent_registry.register(AgentManifest(
    agent_id="gmail_agent",
    intents={"search_email": ExecutionMode.SYNC, "index_gmail": ExecutionMode.ASYNC},
    description="Gmail search and background indexing specialist",
))
```

### Step 3: Update PROTOCOL_SMART_AGENT_SELECTION in Firestore

Add an entry in the `agents_registry` block with `when` / `how` / `examples` / `anti_patterns`
for the new intent. Upload via `firestore_utils/upload.py` (human only — never automated).

### Step 4: Register agent instances in the coordinator

Per-user instances registered via `coordinator.register_agent()` from `UserAgentFactory`.

**SmartAgent prompt auto-updates. Zero code changes to SmartAgent.**

---

## 10. Code References

- `src/infrastructure/agent_registry.py` — AgentRegistry, AgentManifest, ExecutionMode
- `src/infrastructure/agent_coordinator.py` — handle_delegation(), _execute_sync(), _execute_async(), get_available_intents()
- `src/agents/core/smart_response_agent.py` — delegate_to_specialist tool, memory-first parallel scheduling
- `src/agents/memory_search_agent.py` — LLM key formulation, MEMORY_SEARCH_RESPONSE_SCHEMA
- `src/handlers/agent_worker_handler.py` — ASYNC task execution handler
- `src/ports/task_queue.py` — enqueue_agent_task() protocol method
- `src/adapters/gcp_task_queue.py` — Cloud Tasks enqueuing implementation
- `main.py` — registry instantiation, manifest registration, /worker route extension
- Firestore token: `PROTOCOL_SMART_AGENT_SELECTION` — delegation rules for SmartAgent
- Firestore token: `COGNITIVE_PROCESS_MEMORY_SEARCH` — key formulation prompt for MemorySearchAgent

---

## 11. Status

**Status:** ✅ Production Ready (SYNC path)
**ASYNC path:** Infrastructure complete; activated with the first ASYNC agent (Gmail indexing).
**Last Updated:** 2026-02-21
**Implemented via:** [ACP v2 Simplified RFC](../../10_rfcs/ACP_V2_SIMPLIFIED_RFC.md)
