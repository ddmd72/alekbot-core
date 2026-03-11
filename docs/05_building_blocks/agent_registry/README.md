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
- [ ] `AgentDescriptor` fields or `AgentRegistry` interface changes.
- [ ] `coordinator.handle_delegation()` routing logic is modified.
- [ ] The Firestore token `PROTOCOL_SMART_AGENT_SELECTION` or `PROTOCOL_QUICK_AGENT_SELECTION` changes.
- [ ] An intent's execution mode changes (SYNC ↔ ASYNC).
- [ ] MemorySearchAgent output format token (`OUTPUT_FORMAT_MEMORY_SEARCH`) or key formulation changes.
- [ ] `QuickResponseAgent` `MAX_DELEGATION_TURNS` or `intent_remap` changes.

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

**Important:** QuickAgent and SmartAgent share the same `AgentRegistry` for intent discovery. The
difference is in how they query it: Smart calls `get_available_intents()` (all non-internal intents),
Quick calls `get_available_intents_for(descriptor)` filtered by its `allowed_intents` (same set) and
applies `intent_remap` at dispatch time (`search_web` → `search_web_light`). `web_search_light_agent`
is registered with `internal=True` — invisible to LLMs but reachable via remap. Both trees share
`MemorySearchAgent`. See [Section 5.3](#53-quick-delegation-path-protocol_quick_agent_selection) and
[Quick Agent Delegation](../quick_agent_delegation/README.md).

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
  └─ ASYNC path ─────────────────────────────────────────────────────────
       _execute_async()  [not used by deep_research — uses SYNC ACK, delivery is adapter-internal]
         await task_queue.enqueue_agent_task(agent_id, intent, query, context) → task_id
         Returns AgentResponse(result={"status": "started", "task_id": task_id})
         [Cloud Tasks → /worker → AgentWorkerHandler → execute → notify user]

  Note: deep_research_agent uses SYNC mode — returns ACK immediately after create_interaction().
  Delivery mechanism is adapter-internal (not the agent's concern):
    GeminiDeepResearchAdapter: enqueues deep_research_polling Cloud Task on create_interaction().
    OpenAIDeepResearchAdapter (planned): registers webhook; OpenAI calls back on completion.
```

---

## 3. AgentRegistry

Located at `src/infrastructure/agent_registry.py`.

### 3.1 AgentDescriptor

```python
@dataclass
class AgentDescriptor:
    # Identity
    agent_id: str          # "memory_search_agent"
    agent_type: str        # "memory_search"

    # A: What this agent offers other agents
    capabilities: Dict[str, ExecutionMode]              # {"search_memory": SYNC}
    capability_descriptions: Dict[str, str] = field(default_factory=dict)
    internal: bool = False   # True = not shown in LLM tool list (e.g. web_search_light)

    # B: What this agent needs (to delegate)
    # None = all non-internal intents; frozenset = explicit allow-list
    allowed_intents: Optional[frozenset] = None
    intent_remap: Dict[str, str] = field(default_factory=dict)
    # e.g. {"search_web": "search_web_light"} — Quick uses cheaper variant

    description: str = ""
    requires_auth: bool = False

# Backward-compatible alias
AgentManifest = AgentDescriptor
```

### 3.2 ExecutionMode

```python
class ExecutionMode(str, Enum):
    SYNC  = "sync"   # Immediate — returns result inline (search queries, <5s)
    ASYNC = "async"  # Background — Cloud Tasks + user notification (long-running tasks)
```

### 3.3 Registry Methods

```python
def get_available_intents(self) -> List[Dict[str, str]]:
    """All non-internal intents — injected into SmartAgent tool description."""

def get_available_intents_for(self, descriptor: AgentDescriptor) -> List[Dict[str, str]]:
    """Intents available to a specific agent, filtered by its allowed_intents.
    None → all non-internal; frozenset → only those matching the set."""
```

### 3.4 Current Registry (as of 2026-03-05)

All agents registered via `main.py` at startup. `GcpTaskQueue` only instantiated in HTTP mode.

| Agent ID | Intent(s) | Mode | `internal` | Caller |
|----------|-----------|------|-----------|--------|
| `memory_search_agent` | `search_memory` | SYNC | False | Quick, Smart |
| `web_search_agent` | `search_web` | SYNC | False | Smart |
| `web_search_light_agent` | `search_web_light` | SYNC | **True** | Quick (via `intent_remap`) |
| `email_search_agent` | `search_emails`, `get_email_details`, `get_email_attachment` | SYNC | False | Quick, Smart |
| `maps_search_agent` | `maps_query` | SYNC | False | Quick, Smart |
| `compute_agent` | `compute_math`, `compute_datetime`, `compute_finance`, `compute` | SYNC | False | Quick, Smart |
| `deep_research_agent` | `deep_research` | SYNC | False | Smart |

`web_search_light_agent` is `internal=True` — it never appears in LLM tool lists. Quick reaches it
via `intent_remap: {"search_web": "search_web_light"}` at dispatch time.

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

Available intents are injected into the tool description at runtime:
- SmartAgent calls `coordinator.get_available_intents()` — all non-internal intents.
- QuickAgent calls `coordinator.get_available_intents_for(self._descriptor)` — same non-internal
  set, but filtered by its own `allowed_intents` (currently `None` → same result as Smart).

When a new agent is registered in `main.py` with `internal=False`, both agents automatically see
the new intent — no code change required in either agent.

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

### 5.3 compute_agent (`compute_math`, `compute_datetime`, `compute_finance`, `compute`)

- **When:** Any numeric task that can be solved with Python: arithmetic, algebra, unit conversions, date/time arithmetic, financial formulas (numbers provided by user), statistics.
- **How:** Pass the task verbatim as the `query`. The agent is self-contained.
- **Anti-patterns:** Using for tasks that require live data (exchange rates, stock prices) — use `search_web` instead. Compute agent has no internet access; it will honestly report the failure.
- **Four intents by type:** `compute_math` (arithmetic/algebra/units), `compute_datetime` (dates/countdowns/age), `compute_finance` (loan/mortgage/compound interest with provided numbers), `compute` (general numeric fallback — BMI, averages, custom formulas).

### 5.4 Quick Delegation Path: PROTOCOL_QUICK_AGENT_SELECTION

QuickAgent uses a **separate Firestore token** (`PROTOCOL_QUICK_AGENT_SELECTION`) to control when it calls `delegate_to_specialist`. The intent set is smaller and scoped to what Quick can meaningfully handle:

- `search_memory` — same rules as SmartAgent: self-contained query, no raw user message verbatim.
- `search_web_light` — **When:** Quick, factual external lookup (current date/time, price, weather, single fact). Simple single-answer queries. **How:** Pass a short, precise question. **Anti-patterns:** Complex multi-part research; queries that benefit from full synthesis (route those to Smart instead); personal data (use `search_memory`).

**Key behavioral constraints for Quick delegation:**
- `MAX_DELEGATION_TURNS = 5` — if the LLM hasn't responded after 5 tool rounds, stop.
- Memory calls execute first (sequential); other calls run in parallel (`asyncio.gather`).
- `_clean_history_for_quick` strips all `tool_call`/`tool_response` turns from session history before injecting it into the LLM — Quick's history context is always clean text, never polluted with prior tool scaffolding.

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

### 6.3 Output Format Enforcement (Prompt-Level)

Constraints are described in the `OUTPUT_FORMAT_MEMORY_SEARCH` Firestore token (category:
`output_format`), which is included in the `memorysearch` agent profile
(`universal_agent_v1_SYSTEM_memorysearch`). The token replaces the former `response_schema`
API-level constraint.

**Why prompt-level instead of `response_schema`:** Gemini Flash Lite returns empty responses
when `response_schema` (structured output) is combined with Groovy DSL system instructions.
`response_mime_type="application/json"` alone is sufficient — the model follows the JSON
structure described in the prompt without API-level enforcement. This was confirmed by
22+ diagnostic tests in `scripts/debug/test_gemini_json_schema.py`.

| Field | Constraint | Enforced by |
|-------|-----------|-------------|
| `keywords` | 3–5 short English terms (1–2 words) | Prompt |
| `domains` items | enum of 15 values | Prompt |
| `domains` | 1–2 values | Prompt |
| `primary_query` | max 50 chars | Prompt |
| `alternative_query` | max 50 chars, no overlap with primary | Prompt |

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

## 9. Adding a New Specialist Agent

Complete checklist. Touch files IN THIS ORDER.

### Step 1 — `src/infrastructure/agent_manifest.py`

Add `Intent` constant + `AgentDescriptor` + append to `ALL_DESCRIPTORS`:

```python
class Intent:
    FOO = "foo"                          # new

FOO = AgentDescriptor(
    agent_id="foo_agent",
    agent_type="foo",
    capabilities={Intent.FOO: ExecutionMode.SYNC},
    description="...",
    capability_descriptions={
        Intent.FOO: "What this agent does. payload: {\"query\": \"<task>\"}"
    },
    internal=False,
)

ALL_DESCRIPTORS = [..., FOO]
```

`main.py` registers `ALL_DESCRIPTORS` automatically. Both Quick and Smart see the new intent
immediately — no changes to either orchestrator.

### Step 2 — `src/infrastructure/agent_config.py`

Add a typed `@dataclass` config and singleton:

```python
@dataclass
class FooAgentConfig:
    temperature: float = 0.7
    timeout_ms: int = 60_000
    # any other agent-specific tunable params

FOO = FooAgentConfig()
```

### Step 3 — `src/services/agent_context_builder.py`

Add provider strategy for the new agent type:

```python
AgentProviderStrategy.STRATEGIES["foo"] = {
    "default_provider": "gemini",
    "allowed_providers": ["gemini"],
    "required_capabilities": ["native_tools"],
    "fallback": None,
}
```

### Step 4 — `src/agents/foo_agent.py`

Implement the agent class. **Follow this exact structure — no deviations:**

```python
class FooAgent(BaseAgent):
    TEMPERATURE = FOO.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        # domain-specific deps (e.g. grounding_tool, service port)...
        prompt_builder: Optional[PromptBuilderPort] = None,  # REQUIRED — see §9 note
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error="No query provided in payload")
        return await self._call_foo(message, query)

    async def _call_foo(self, message: AgentMessage, query: str) -> AgentResponse:
        self._on_agent_start(query)
        system = await self.prompt_builder.build_for_agent("foo", self.user_id) \
                 if self.prompt_builder else ""
        try:
            self._debug_prompt(system, query, model=self.model_name)
            response = await self._llm.generate_content(LLMRequest(...))
            self._debug_response(response.text or "")
            self._on_agent_success(len(response.text or ""), output_text=response.text or "")
            return AgentResponse.success(...)
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(...)

    def _get_alternative_agents(self) -> list[str]:
        return ["web_search_agent", "memory_search_agent"]
```

> **Note on `prompt_builder`:** All specialist agents receive `PromptBuilderPort` — this is
> mandatory. System instructions come through the port, not hardcoded in the class. The only
> exception is when no prompt profile exists yet — mark it as technical debt with a comment
> (see `MapsSearchAgent.SYSTEM_INSTRUCTION` for the anti-pattern and the required TODO).

> **Note on provider-specific tools (e.g. code_execution, grounding):** Do NOT inject Gemini-specific
> `types.Tool(...)` objects from the factory or the agent class. Use `LLMRequest` flags instead:
> `use_code_execution=True` (sandbox Python execution, Gemini only) or `use_grounding=True`
> (Google Search grounding, Gemini only). `GeminiAdapter` reads the flag and injects the tool
> internally. Other adapters silently ignore the flag. See `ComputeAgent` as the reference
> implementation.

> **Note on extended thinking:** Use `LLMRequest.thinking: Optional[str]` — the unified
> provider-agnostic thinking field. Values: `"low"` | `"medium"` | `"high"` | `None` (disabled).
> Each adapter maps this to its own API: `GeminiAdapter` → `ThinkingConfig(thinking_level=LOW/MEDIUM/HIGH)`;
> `ClaudeAdapter` (Sonnet/Opus) → `thinking={"type": "adaptive"}, effort=...`. Do NOT put
> Gemini `ThinkingLevel` or Claude `budget_tokens` in the agent class — those are adapter internals.
> `ConsolidationAgent` (Claude Sonnet, BALANCED) uses `thinking="high"` (complex multi-turn reasoning).
> `EmailClassificationAgent` (Gemini) uses `thinking="medium"`. When `thinking=None`, Claude Sonnet
> defaults to adaptive with `effort="medium"` at the adapter level.

### Step 5 — `src/composition/user_agent_factory.py`

Wire the agent into the factory. Four touch points:

```python
# 1. Import at top
from ..infrastructure.agent_config import FOO as FOO_CFG
from ..agents.foo_agent import FooAgent

# 2. Inside _create_and_cache_agents():
foo_context = self.context_builder.build("foo", user_profile.config)
foo_agent = FooAgent(
    config=AgentConfig(
        agent_id=f"foo_agent_{user_id}",
        agent_type="foo",
        timeout_ms=FOO_CFG.timeout_ms,
        capabilities=["..."],
    ),
    execution_context=foo_context,
    prompt_builder=prompt_builder,
    user_id=user_id,
)

# 3. Register
self._register_agents([..., foo_agent])

# 4. Cache dict + eviction key
cached = {..., "foo_agent": foo_agent}
# In _evict_expired_cache: add "foo_agent" to the key tuple
```

### Step 6 — `tests/unit/agents/test_foo_agent.py`

Minimum required coverage:

- `can_handle`: correct intent + valid query → True
- `can_handle`: wrong intent → False; missing/empty query → False
- `execute` happy path: LLM called, result structure matches AgentResponse.success
- `execute` error path: empty query → AgentResponse.failure; LLM exception → AgentResponse.failure

### Step 7 — Prompt work (via PromptBuilderPort)

Create upload files and upload in this order (token → blueprint → profile):

1. **Token** (`firestore_utils/uploads/COGNITIVE_PROCESS_FOO.json`):
   `token_id`, `category: "cognitive_process"`, `class: "cognitive_process"`, `content`, `metadata`.
   Upload: `python firestore_utils/upload.py <collection> COGNITIVE_PROCESS_FOO --format json`

2. **Blueprint** (`firestore_utils/uploads/foo_agent_v1.json`):
   `blueprint_id: "foo_agent_v1"`, `outer_class: "FooAgent extends Agent"`, `class_order: ["cognitive_process"]`.
   Upload: `python firestore_utils/upload.py <collection> foo_agent_v1 --format json`

3. **Profile** (`firestore_utils/uploads/foo.json`):
   `agent_id: "foo"`, `blueprint_id: "foo_agent_v1"`, `tokens: {"COGNITIVE_PROCESS_FOO": {"order": 10, "non_overridable": true}}`.
   Document ID = `agent_type` string passed to `build_for_agent()`. Upload: `python firestore_utils/upload.py <collection> foo --format json`

4. Update `PROTOCOL_SMART_AGENT_SELECTION` token: add `when` / `how` / `anti_patterns`
   for the new intent
5. If Quick should also call it: update `PROTOCOL_QUICK_AGENT_SELECTION` token

### Verification

```bash
make test-unit      # all unit tests green
make test-e2e-all   # Quick and Smart delegate correctly to the new agent
```

**SmartAgent and QuickAgent require zero code changes. The registry handles everything.**

---

## 10. Code References

- `src/infrastructure/agent_manifest.py` — **Single source of truth** for all agent declarations: `Intent` constants, `AgentDescriptor` instances for every agent (specialists + orchestrators), `ALL_DESCRIPTORS` list. Start here when adding or understanding any agent.
- `src/infrastructure/agent_config.py` — Central config registry: typed `@dataclass` per agent (`QUICK`, `SMART`, `ROUTER`, `MEMORY_SEARCH`, `WEB_SEARCH`, `WEB_SEARCH_LIGHT`, `CONSOLIDATION`, `EMAIL_SEARCH`, `EMAIL_CLASSIFICATION`, `MAPS_SEARCH`, `COMPUTE`). Holds all tunable behavior params: delegation turns, timeouts, temperatures, and thinking config. `MapsSearchAgentConfig.model_name` is pinned to `gemini-2.5-flash` (Maps grounding unsupported on Gemini 3.x). `ComputeAgentConfig.temperature=0.0` (deterministic computation). `ConsolidationAgentConfig.thinking_effort="high"` + `max_tokens=32_000` (complex multi-turn reasoning; Claude Sonnet 4.6).
- `src/infrastructure/agent_registry.py` — `AgentDescriptor` dataclass (alias: `AgentManifest`), `AgentRegistry` mechanics, `ExecutionMode`, `get_available_intents()`, `get_available_intents_for(descriptor)`. Descriptor instances live in `agent_manifest.py`.
- `src/infrastructure/agent_coordinator.py` — handle_delegation(), _execute_sync(), _execute_async(), get_available_intents(), get_available_intents_for()
- `src/agents/base_agent.py` — lifecycle hooks, `_debug_prompt`, `_debug_response`
- `src/agents/core/smart_response_agent.py` — delegate_to_specialist tool, memory-first parallel scheduling. Class-level `_descriptor = SMART_RESPONSE`.
- `src/agents/core/quick_response_agent.py` — `MAX_DELEGATION_TURNS=5`, `_execute_quick_delegation_loop`, `_execute_quick_parallel`, `_clean_history_for_quick`. Class-level `_descriptor = QUICK_RESPONSE` (intent_remap lives in descriptor).
- `src/agents/memory_search_agent.py` — LLM key formulation (`response_mime_type` only, no `response_schema`)
- `src/agents/web_search_light_agent.py` — Lightweight grounding specialist (`internal=True`, Quick path via remap)
- `src/handlers/agent_worker_handler.py` — ASYNC task execution handler
- `src/ports/task_queue.py` — enqueue_agent_task() protocol method
- `src/adapters/gcp_task_queue.py` — Cloud Tasks enqueuing implementation
- `src/adapters/claude_adapter.py` — Claude adapter. Model tiers: ECO=`claude-haiku-4-5-20251001`, BALANCED/PERFORMANCE=`claude-sonnet-4-6`. Adaptive thinking enabled by default for all Sonnet/Opus calls (`effort="medium"` adapter default; overridden by `LLMRequest.thinking`). `temperature` auto-set to 1.0 when thinking is active. `max_tokens` reads from `LLMRequest.max_tokens` (default 16,000).
- `src/adapters/gemini_adapter.py` — Gemini adapter. Maps `LLMRequest.thinking` → `ThinkingConfig(thinking_level=LOW/MEDIUM/HIGH)`. `use_code_execution=True` injects `types.Tool(code_execution=...)` internally.
- `main.py` — registers `ALL_DESCRIPTORS` into `AgentRegistry` at startup (1 loop, no inline declarations)
- Firestore token: `PROTOCOL_SMART_AGENT_SELECTION` — delegation rules for SmartAgent
- Firestore token: `PROTOCOL_QUICK_AGENT_SELECTION` — delegation rules for QuickAgent
- Firestore token: `OUTPUT_FORMAT_MEMORY_SEARCH` — key formulation output constraints (prompt-level)
- Firestore token: `COMPUTE_COGNITIVE_PROCESS` — ComputeAgent identity, capability, rules, failure protocol (`domain_prompt_tokens_v3_system`)
- Firestore profile: `universal_agent_v1_SYSTEM_memorysearch` (collection: `development_domain_prompt_profiles_v3`)
- Firestore blueprint: `compute_agent_v1` (collection: `domain_prompt_blueprints_v3`)
- Firestore profile: `compute` (collection: `domain_prompt_profiles_v3`) — document ID = `agent_type`
- Building Block: [Quick Agent Delegation](../quick_agent_delegation/README.md)

---

## 11. Status

**Status:** ✅ Production Ready (SYNC path)
**ASYNC path:** Infrastructure complete; activated with the first ASYNC agent (Gmail indexing).
**Last Updated:** 2026-03-07
**Implemented via:** [ACP v2 Simplified RFC](../../10_rfcs/ACP_V2_SIMPLIFIED_RFC.md)
