# RFC: ComputeAgent — Precise Calculations via Python Code Execution

**Status:** IMPLEMENTED
**Date:** 2026-03-05
**Owner:** AI Engineering
**Milestone:** Specialist Agents — Phase 3

**Related:** agent_manifest.py (multi-intent pattern from EmailSearch)

---

## 1. Problem Statement

Numeric questions ("how many days until New Year", "BMI at 80kg/180cm", "square root of 289",
"mortgage at 4.5% for 20 years") are currently answered by Quick/Smart from general knowledge.
LLMs are unreliable at arithmetic and have no reliable way to execute precise calculations.

**Failure modes today:**
- **Math errors:** LLMs miscalculate multi-step arithmetic, percentages, compound interest
- **Date drift:** "How many days until X" requires knowing today's date and computing precisely
- **No delegation path:** No specialist agent exists for computation — orchestrator answers in-head
- **No verification:** LLM "thinks" it computed correctly but has no way to verify

**Desired outcome:** Smart/Quick delegates computation tasks to `ComputeAgent` via one of four
typed intents. The agent uses Gemini code_execution — LLM writes Python code, executes it
in a sandbox, and returns the verified result. The agent is a pure calculator: it only computes
what it's told, has no access to external data (no internet, no APIs, no live rates). If a
task requires data the agent doesn't have, it honestly reports that the task cannot be completed.

---

## 2. Architecture

### 2.1 Core Philosophy

ComputeAgent is a **calculator in an agent network**. It does not know about intents, routing,
or other agents. It receives a computation task, writes Python code, executes it, and returns
the result. Its unique advantage is **Python code execution** — deterministic, verifiable,
precise.

**What it CAN do:**
- Arithmetic, algebra, equations, unit conversions (all via Python math/sympy)
- Date/time calculations (Python datetime — knows current time via context injection)
- Financial formulas (compound interest, amortization — pure math with provided numbers)
- Statistics, BMI, any numeric analysis computable with Python

**What it CANNOT do (and will honestly say so):**
- Fetch live exchange rates (no internet access)
- Look up stock prices or market data
- Access external calendars, holiday databases
- Anything requiring network/API calls

### 2.2 Intent Design — Multi-Intent Single Agent (EmailSearch Pattern)

Four intents route to one `ComputeAgent`, mirroring `EmailSearchAgent` (3 intents, 1 agent).
The orchestrator sees four distinct tools with precise descriptions — clear signals for when
to delegate. The agent itself does NOT branch on intent — it just receives the query and
computes.

| Intent | Orchestrator signal | Example |
|--------|---------------------|---------|
| `compute_math` | Arithmetic, algebra, unit conversions | "square root of 289", "150 km in miles" |
| `compute_datetime` | Date/time arithmetic, countdowns, age | "days until New Year", "what day was 2024-02-29" |
| `compute_finance` | Financial formulas (numbers provided by user) | "mortgage at 4.5% for 20 years on 300k" |
| `compute` | General numeric analysis (fallback) | "BMI at 80kg/180cm", "average of 12, 45, 78" |

**Critical orchestrator instruction:** Compute agent ONLY computes with data provided in the
query. For live data (exchange rates, stock prices, current weather) — use `search_web`.

### 2.3 Full Flow

```
User: "how many days until New Year?"
  |
  v Smart/Quick -> compute_datetime intent -> ComputeAgent
      |    payload: {"query": "how many days until New Year?"}
      |
      +-- Build system prompt via PromptBuilderPort (agent_type="compute")
      |
      +-- Single LLM call (use_code_execution=True):
      |     LLMRequest(use_code_execution=True, ...)
      |     # GeminiAdapter injects code_execution tool internally — agent stays provider-agnostic
      |     system_instruction: cognitive process from PromptBuilder
      |     message: "current_datetime: Thursday, 05 March 2026, 14:30 UTC\n\nTASK: how many days until New Year?"
      |
      +-- Gemini generates Python code:
      |     from datetime import datetime
      |     today = datetime(2026, 3, 5)
      |     new_year = datetime(2027, 1, 1)
      |     delta = (new_year - today).days
      |     print(f"{delta} days until New Year 2027")
      |
      +-- Sandbox executes -> stdout: "302 days until New Year 2027"
      |
      v-- Gemini returns natural language answer incorporating code output

  -> AgentResponse.success(result="302 days until New Year 2027 (from March 5, 2026).")

  v Smart/Quick formats response for user
```

### 2.4 Code Execution Tool

`types.Tool(code_execution=types.ToolCodeExecution())` — Gemini-native sandbox.
- Gemini generates Python code based on the query
- Code runs in an isolated sandbox (no network, no filesystem)
- stdout/result returned to Gemini, which formulates the final answer
- Standard library available (math, datetime, statistics, decimal, fractions)
- No pip packages — but stdlib covers 99% of computation needs

---

## 3. New Components

### 3.1 Intent Constants

**File:** `src/infrastructure/agent_manifest.py`

```python
class Intent:
    # ... existing intents ...
    COMPUTE_MATH     = "compute_math"
    COMPUTE_DATETIME = "compute_datetime"
    COMPUTE_FINANCE  = "compute_finance"
    COMPUTE          = "compute"
```

### 3.2 Agent Descriptor

**File:** `src/infrastructure/agent_manifest.py`

Key design: every capability_description explicitly states "ONLY computes — does NOT search
or fetch external data" — this is the primary routing signal for the orchestrator.

```python
COMPUTE = AgentDescriptor(
    agent_id="compute_agent",
    agent_type="compute",
    capabilities={
        Intent.COMPUTE_MATH:     ExecutionMode.SYNC,
        Intent.COMPUTE_DATETIME: ExecutionMode.SYNC,
        Intent.COMPUTE_FINANCE:  ExecutionMode.SYNC,
        Intent.COMPUTE:          ExecutionMode.SYNC,
    },
    description="Precise computation via Python code execution",
    capability_descriptions={
        Intent.COMPUTE_MATH: (
            "Precise arithmetic, algebra, equations, unit conversions ... "
            "ONLY computes what you tell it. Does NOT search or fetch external data."
        ),
        Intent.COMPUTE_DATETIME: (
            "Date and time calculations ... "
            "ONLY computes — does NOT look up holidays, events, or external calendars."
        ),
        Intent.COMPUTE_FINANCE: (
            "Financial calculations: loan/mortgage payments, compound interest ... "
            "ONLY computes with numbers YOU provide. Has NO access to live rates."
        ),
        Intent.COMPUTE: (
            "General-purpose computation ... Executes Python code in sandbox. "
            "ONLY computes — does NOT search, fetch, or access external data."
        ),
    },
)
```

### 3.3 Agent Config

**File:** `src/infrastructure/agent_config.py`

```python
@dataclass
class ComputeAgentConfig:
    temperature: float = 0.0     # deterministic computation
    timeout_ms: int = 30_000     # single code_execution call
```

### 3.4 Provider Strategy

**File:** `src/services/agent_context_builder.py`

```python
# code_execution is Gemini-only (sandbox Python execution).
"compute": {
    "default_provider": "gemini",
    "allowed_providers": ["gemini"],
    "required_capabilities": ["native_tools"],
    "fallback": None
},
```

### 3.5 ComputeAgent Class

**File:** `src/agents/compute_agent.py`

The agent does NOT know about intents. It receives a query, builds a prompt with current
datetime context, sets `use_code_execution=True` in `LLMRequest`, and returns plain text result.
Provider-agnostic: `GeminiAdapter` injects `types.Tool(code_execution=...)` internally when
it sees the flag; other adapters ignore it.

```python
class ComputeAgent(BaseAgent):
    TEMPERATURE = COMPUTE.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None: ...

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        return await self._call_compute(message, query)

    async def _call_compute(self, message, query):
        # system_prompt from PromptBuilder
        # user_text = f"current_datetime: {now}\n\nTASK: {query}"
        # LLMRequest(use_code_execution=True, ...)
        # Single LLM call -> plain text result
```

### 3.6 Factory Wiring

**File:** `src/composition/user_agent_factory.py`

No provider-specific tool construction — the adapter handles it via `use_code_execution` flag.

```python
compute_agent = ComputeAgent(
    config=AgentConfig(...),
    execution_context=compute_context,
    prompt_builder=prompt_builder,
    user_id=user_id,
)
```

---

## 4. Files Changed

### New files
| File | Purpose |
|------|---------|
| `src/agents/compute_agent.py` | ComputeAgent implementation |
| `tests/unit/agents/test_compute_agent.py` | Unit tests |
| `docs/10_rfcs/COMPUTE_AGENT_RFC.md` | This RFC |
| `firestore_utils/uploads/COMPUTE_COGNITIVE_PROCESS.groovy` | Human-readable source for the token |
| `firestore_utils/uploads/COMPUTE_COGNITIVE_PROCESS.json` | Token upload file for Firestore |
| `firestore_utils/uploads/compute_agent_v1.json` | Blueprint upload file |
| `firestore_utils/uploads/compute.json` | Profile upload file |

### Modified files
| File | Change |
|------|--------|
| `src/infrastructure/agent_manifest.py` | Add 4 Intent constants + COMPUTE descriptor + ALL_DESCRIPTORS |
| `src/infrastructure/agent_config.py` | Add ComputeAgentConfig dataclass + COMPUTE instance |
| `src/services/agent_context_builder.py` | Add "compute" entry in STRATEGIES |
| `src/composition/user_agent_factory.py` | Import, instantiate, register, cache ComputeAgent |
| `src/ports/llm_port.py` | Add `use_code_execution: bool = False` to `LLMRequest` |
| `src/adapters/gemini_adapter.py` | Handle `use_code_execution` flag — injects `types.Tool(code_execution=...)` internally |

### NOT modified (by design)
| File | Why |
|------|-----|
| `src/agents/core/quick_response_agent.py` | Discovers compute intents via AgentRegistry automatically |
| `src/agents/core/smart_response_agent.py` | Same — registry-driven discovery |
| `src/services/` | No ComputeService needed — agent is self-contained |
| `src/ports/` | No new ports — uses existing LLMPort + PromptBuilderPort |

---

## 5. Design Decisions

### 5.1 Code Execution, Not Grounding

Previous design used Google Search grounding (passive web search). Changed to Gemini
code_execution because:
- **Deterministic:** Python math is exact. LLM "thinking" about sqrt(289) may hallucinate.
- **Verifiable:** Code output is the ground truth — not LLM interpretation.
- **Clear boundary:** Agent is a calculator. If it can't compute → honest failure.
  No ambiguity about whether it "searched enough" or "found the right rate."
- **No data dependency:** Agent never needs external data. Orchestrator handles that.

### 5.2 No Service Layer

Unlike EmailSearch (which needs EmailSearchService for vector search, Gmail API, attachment
parsing), Compute is a single LLM call with code_execution. The agent IS the service.

### 5.3 Plain Text Output

The agent returns plain text. The orchestrator uses the result as context, not structured data.

### 5.4 Agent Doesn't Know About Intents

The agent receives `payload.query` and computes. It does not branch on intent type.
The four intents exist solely for the orchestrator's routing convenience — they map to
different capability_descriptions that help the orchestrator decide when to delegate.

### 5.5 Honest Failure for Missing Data

If the query requires live data (exchange rates, stock prices), the agent will:
1. Attempt to write code
2. Realize it has no data source
3. Return a clear message: "Cannot compute: requires live exchange rate data not available
   in this environment."

The orchestrator then falls back to `web_search_agent`.

### 5.6 Fallback to web_search_agent

`_get_alternative_agents()` returns `["web_search_agent"]`. If compute fails (circuit breaker,
timeout, or "can't compute"), the coordinator can retry via web search.

---

## 6. Prompt Work

Prompts loaded via `PromptBuilderPort`. Upload token → blueprint → profile in this order.

### Token

| File | Collection | Token ID | Purpose |
|------|-----------|----------|---------|
| `firestore_utils/uploads/COMPUTE_COGNITIVE_PROCESS.json` | `domain_prompt_tokens_v3_system` | `COMPUTE_COGNITIVE_PROCESS` | Agent identity, capability, rules, failure protocol |

### Blueprint

| File | Collection | Document ID |
|------|-----------|-------------|
| `firestore_utils/uploads/compute_agent_v1.json` | `domain_prompt_blueprints_v3` | `compute_agent_v1` |

`outer_class: "ComputeAgent extends Agent"`, `class_order: ["cognitive_process"]`.
Blueprint class `cognitive_process` absorbs the `COMPUTE_COGNITIVE_PROCESS` token (matched by `class` field).

### Profile

| File | Collection | Document ID |
|------|-----------|-------------|
| `firestore_utils/uploads/compute.json` | `domain_prompt_profiles_v3` | `compute` |

Profile document ID = `agent_type="compute"` — resolved by `profile_repo.get_agent_profile("compute")`.
Profile maps `COMPUTE_COGNITIVE_PROCESS` token with `order: 10, non_overridable: true`.

### Upload Order

```bash
# Development
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COMPUTE_COGNITIVE_PROCESS --format json
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 compute_agent_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 compute --format json

# Production (after validation in dev)
python firestore_utils/upload.py domain_prompt_tokens_v3_system COMPUTE_COGNITIVE_PROCESS --format json
python firestore_utils/upload.py domain_prompt_blueprints_v3 compute_agent_v1 --format json
python firestore_utils/upload.py domain_prompt_profiles_v3 compute --format json
```

### Also update (manually in Firestore)

- `PROTOCOL_SMART_AGENT_SELECTION` — add WHEN/HOW/ANTI_PATTERNS for all four compute intents
- `PROTOCOL_QUICK_AGENT_SELECTION` — same (Quick also delegates to compute)

---

## 7. Cost Impact

| Metric | Value |
|--------|-------|
| LLM calls per request | 1 (Gemini Flash with code_execution) |
| Code execution cost | Included in Gemini API cost (no separate billing) |
| Comparison | Cheaper than grounding (no per-prompt fee) |
| Expected traffic | ~10-15% of requests (dates, math, formulas) |

---

## 8. Test Strategy

### Unit tests (`tests/unit/agents/test_compute_agent.py`)

1. `can_handle` — correct intent + payload, wrong intent, empty payload
2. `execute` happy path — verify LLM called with code_execution tool, result structure
3. `execute` — natural language and formula queries both work
4. `execute` empty query — failure response
5. `execute` LLM exception — failure response
6. Prompt builder failure — proceeds with empty prompt (non-fatal)
7. No prompt builder — still works
8. `use_code_execution=True` set on `LLMRequest` (provider-agnostic flag); no `tools` injected by agent
9. Current datetime injected in user message

---

## 9. Implementation Order

1. `src/infrastructure/agent_manifest.py` — Intent constants + COMPUTE descriptor
2. `src/infrastructure/agent_config.py` — ComputeAgentConfig
3. `src/services/agent_context_builder.py` — "compute" strategy
4. `src/agents/compute_agent.py` — Agent class
5. `src/composition/user_agent_factory.py` — Wiring
6. `tests/unit/agents/test_compute_agent.py` — Unit tests
7. Prompt tokens (manual upload to Firestore)
8. Agent selection protocol tokens update (manual)
