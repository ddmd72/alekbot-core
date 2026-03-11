# Multi-Agent System: Business Logic

**Status:** 📘 Concept Documentation
**Created:** 21.01.2026
**Updated:** 30.01.2026 (v6.0 revisions)
**Version:** 2.0

## 📖 HowTo: Using This Document

### Purpose
Describes the business logic of the multi-agent system: how agents work together to process requests, learn, and synthesize knowledge.

### When to Read
- **For AI Agents:** When understanding agent workflows or delegation patterns.
- **For Developers:** When designing new agents or debugging agent interactions.

### When to Update
This document MUST be updated when:
- [ ] New agents are added to the system.
- [ ] Agent communication patterns change.
- [ ] Business rules for agent selection evolve.

### Cross-References
- **Multi-Agent System:** [../05_building_blocks/multi_agent_system/README.md](../05_building_blocks/multi_agent_system/README.md)
- **Hybrid Router:** [../05_building_blocks/hybrid_router/README.md](../05_building_blocks/hybrid_router/README.md)
- **Sliding Window Consolidation:** [../05_building_blocks/sliding_window_consolidation/README.md](../05_building_blocks/sliding_window_consolidation/README.md)
- **Runtime View:** [../06_runtime/README.md](../06_runtime/README.md)

---

## 🏗️ System Architecture (v6.0)

### High-Level Flow

```
User Message
    ↓
ConversationHandler (Platform-Agnostic Orchestrator)
    ↓
AgentCoordinator (Central Router)
    ↓
RouterAgent (Triage & Classification)
    ├─ Simple Query → QuickResponseAgent
    │       ├─ (optional) Delegates to MemorySearchAgent
    │       └─ (optional) Delegates to WebSearchLightAgent  ← lightweight grounding
    └─ Complex Query → SmartResponseAgent
            ├─ Delegates to MemorySearchAgent
            ├─ Delegates to WebSearchAgent
            ├─ Delegates to EmailSearchAgent
            ├─ Delegates to MapsSearchAgent
            └─ Delegates to DeepResearchAgent  ← SYNC ACK, long-running op via adapter (Gemini/OpenAI)
    ↓
Response Synthesis
    ↓
User receives answer
```

### Shared Session Context (v6.0)

Session context is stored centrally in `SessionStore` (Firestore). Agents load context on demand via `session_id`.

**Key Benefits:**
- Eliminates repeated serialization of long histories
- Ensures all agents see the same truth
- Keeps response agents lightweight while providing full context

**Flow:**
```
ConversationHandler → AgentCoordinator → Agent
Agent → SessionStore.load(session_id)
Agent → Reasoning + Delegation → Final answer
ConversationHandler → Relay result + persist session
```

**Code References:**
- `src/handlers/conversation_handler.py` - Orchestration
- `src/infrastructure/agent_coordinator.py` - Routing
- `src/adapters/firestore_session_store.py` - Session persistence

---

## 🤖 Agent Roles & Responsibilities

### 1. RouterAgent - "The Dispatcher"

**Business Purpose:** Triage and classify user queries for optimal routing

**When Activated:** Every user message (first in chain)

**Process Flow:**
```
1. Receive user query from ConversationHandler
2. LLM Triage: Analyze complexity (1-10) and tone
3. Rule-Based Fallback: Pattern matching if LLM fails
4. Routing Decision:
   - Complexity ≤5 → quick_response_agent
   - Complexity >5 → smart_response_agent
5. Return routing target + metadata
```

**Example:**
```
User: "Hi"
  ↓
RouterAgent: complexity=1, tone=casual
  ↓
Routes to QuickResponseAgent (fast, cheap)
```

**Why Separate Agent:**
- Optimizes cost (70% of queries use Flash model)
- Reduces latency for simple queries
- Hybrid LLM + rule-based approach

**Code:** `src/agents/core/router_agent.py`

See: [Hybrid Router](../05_building_blocks/hybrid_router/README.md)

---

### 2. QuickResponseAgent - "The Fast Responder"

**Business Purpose:** Handle simple queries with minimal cost, with optional lightweight delegation for memory or web lookup.

**When Activated:** Router classifies query as simple (complexity ≤5)

**Process Flow:**
```
1. Load biographical context (get_biographical_context_cached)
2. Merge with Router semantic enrichment (facts, semantic_lens)
3. Build system prompt via PromptBuilder v3 (agent_type="quick")
   — includes PROTOCOL_QUICK_AGENT_SELECTION delegation token
4. Load conversation history (last 20 messages, tiered compression)
5. Clean history: _clean_history_for_quick()
   — strips all tool_call / tool_response turns (clean text only)
6. _execute_quick_delegation_loop() [MAX_DELEGATION_TURNS=5]
   Turn N:
     a. LLM call with delegate_to_specialist tool
        — intents from get_available_intents_for(descriptor) (same non-internal set as Smart)
     b. No tool_calls? → parse_llm_response(text) → return JSON response
     c. Has tool_calls? → apply _INTENT_REMAP → _execute_quick_parallel():
           - memory calls: sequential first
           - other calls: asyncio.gather (parallel)
     Append tool results → next turn
7. Output: JSON envelope { full_response, response_summary, rich_content }
   — response_summary used directly as history text (no HistorySummaryService)
   — HistorySummaryService fires only as fallback for plain-text (non-JSON) path
```

**Available intents (same as Smart, non-internal):**
- `search_memory` — semantic search through user's biographical facts
- `search_web` — LLM sees this; at dispatch time `_INTENT_REMAP` routes it to `WebSearchLightAgent`
  (ECO tier, single grounding call, `internal=True` in registry)
- `search_emails`, `get_email_details`, `get_email_attachment` — email archive specialist

**Output format:**
```json
{
  "full_response":    "complete Slack mrkdwn answer for the user",
  "response_summary": "≤300 chars for session history",
  "rich_content":    { "type": "widget", "data": {...}, "fallback": "..." } | null
}
```

**Example (no delegation):**
```
User: "Good morning!"
  ↓
QuickResponseAgent: Load history + bio context
  ↓
LLM: no tool call → parse JSON → return full_response
```

**Example (with web search):**
```
User: "What's today's weather in Valencia?"
  ↓
QuickResponseAgent: Turn 1 — LLM calls search_web_light("Valencia weather today")
  ↓
WebSearchLightAgent: Gemini grounding → "22°C, sunny" (plain mrkdwn)
  ↓
Turn 2: LLM synthesizes → JSON response → return
```

**Why Separate Agent:**
- Uses BALANCED tier Flash (10x cheaper than Thinking)
- Bounded delegation: max 5 turns, intent remap to ECO-tier web search
- Optimized for <3s latency even with one tool round-trip
- Clean history = no tool-call noise in context window

**Code:** `src/agents/core/quick_response_agent.py`

---

### 2a. WebSearchLightAgent - "The Quick Lookup"

**Business Purpose:** Provide fast, single-fact web answers for QuickResponseAgent without the latency overhead of the full WebSearchAgent.

**When Activated:** QuickResponseAgent delegates `search_web_light` intent.

**Process Flow:**
```
1. Build prompt via PromptBuilder v3 (agent_type="websearch_light")
   — or inline Groovy cognitive_process fallback if no PromptBuilder
2. Inject current_date + user_query into augmented query string
3. Single LLMRequest:
   - model: ECO tier (gemini-flash-lite-latest)
   - tools: [grounding_tool]
   - temperature: 0.5
4. Return response.text as plain Slack mrkdwn (no JSON, no markdown headers)
```

**Example:**
```
Delegation query: "What is the current EUR/USD exchange rate?"
  ↓
WebSearchLightAgent: grounding call
  ↓
Returns: "1 EUR = 1.08 USD (as of 12:00 UTC)" (plain mrkdwn, single pass)
```

**Why Separate Agent:**
- Cannot combine Google Search grounding + function calling in one Gemini request
  (Gemini API limitation — grounding requires separate request)
- ECO tier = cheapest possible model (Flash Lite)
- Single pass: no synthesis loop, no JSON envelope overhead
- Alternative fallback: `["memory_search_agent"]` if grounding unavailable

**Code:** `src/agents/web_search_light_agent.py`

---

### 3. SmartResponseAgent - "The Orchestrator"

**Business Purpose:** Handle complex queries with delegation to specialists

**When Activated:** Router classifies query as complex (complexity >5)

**Process Flow:**
```
1. Load session history (last 60 messages) with tiered context:
   - Last N model turns (default 5) → full response text
   - Older model turns → compressed summary (history_summary)
   - User messages → always full text
2. Build comprehensive prompt (kernel + context + tool declarations)
3. force_tool_use=True ensures LLM always calls a tool (never outputs plain text)
4. Agent delegation loop (max 5 turns):
   - Needs memory? → Delegate to MemorySearchAgent (always sequential, first)
   - Needs web info? → Delegate to WebSearchAgent
   - Ready to respond? → Call deliver_response(full_response, history_summary)
5. deliver_response is the ONLY output channel:
   - full_response: complete Slack mrkdwn answer for the user
   - history_summary: ≤300 chars compressed summary stored in session history
   - Loop terminates immediately on deliver_response call
```

**Session History Storage (per turn):**
```
MessagePart.text      → history_summary (compressed, ≤300 chars)
MessagePart.full_text → full response (always stored, used for recent N turns)
```

**Example:**
```
User: "What's my car and current gas prices?"
  ↓
SmartResponseAgent: Load history with tiered context
  ↓
Turn 1: LLM calls search_memory("my car")
  → MemorySearchAgent returns: "Honda Civic 2019"
Turn 2: LLM calls ask_web_search_agent("gas prices Valencia")
  → WebSearchAgent returns: "€1.45/liter"
Turn 3: LLM calls deliver_response(
    full_response="You have a Honda Civic 2019...",
    history_summary="Q: car+gas. A: Civic 2019, €1.45/l. 🚗⛽"
  )
  → Loop terminates, response delivered
```

**Why Separate Agent:**
- Uses full Pro model (complex reasoning, large context)
- Can delegate to multiple specialists in parallel
- Structured output via deliver_response guarantees history compression

**Code:** `src/agents/core/smart_response_agent.py`

---

### 4. MemorySearchAgent - "The Archivist"

**Business Purpose:** Retrieve relevant personal knowledge from user's memory

**When Activated:**
- User asks about their past ("What did I say about...?")
- User references personal information ("Where's my car?")
- SmartResponseAgent delegates memory search

**Process Flow:**
```
1. Receive search query from SmartResponseAgent
2. Generate semantic embedding of query
3. Perform vector search in Firestore (user's facts)
4. Return top N relevant facts with scores
5. SmartResponseAgent uses facts to ground response
```

**Example:**
```
User: "What's the model of my car?"
  ↓
MemorySearchAgent: Vector search("car model")
  ↓
Finds: "User owns a Honda Civic 2019" (score: 0.92)
  ↓
Returns to SmartResponseAgent for synthesis
```

**Why Separate Agent:**
- No LLM needed (pure vector search)
- Can be cached/optimized independently
- Isolated from web search logic

**Code:** `src/agents/memory_search_agent.py`

---

### 5. WebSearchAgent - "The Explorer"

**Business Purpose:** Search the internet for current/external information

**When Activated:**
- User asks about current events ("What's the weather?")
- User needs external information ("Flights to Krakow?")
- SmartResponseAgent delegates web search

**Process Flow:**
```
1. Receive search query from SmartResponseAgent
2. Load session context for query formulation
3. Call Gemini with Google Search grounding tool
4. LLM performs search + synthesizes answer
5. Return formatted answer with citations
```

**Example:**
```
User: "What's the weather in Valencia today?"
  ↓
WebSearchAgent: Gemini Grounding search("Valencia weather")
  ↓
Finds: Current weather data
  ↓
Returns: "Valencia: 22°C, sunny, light breeze"
```

**Why Separate Agent:**
- Uses Gemini Grounding (native search integration)
- Dedicated Flash instance for cost optimization
- Isolated from personal data search

**Code:** `src/agents/web_search_agent.py`

---

### 6. ConsolidationAgent - "The Life Chronicler"

**Business Purpose:** Extract and maintain structured long-term knowledge from conversations
and emails via a 3-stage pipeline.

**When Activated:**
- Session overflow (sliding window threshold: 100 messages, batch: 50)
- Manual trigger (`$consolidate` command)

**3-Stage Pipeline:**

```
Stage 1 — Conversation consolidation
  Batch of conversation messages
  → 8-step deliberate process (EXTRACT → CLASSIFY → SEARCH → ANALYZE → SIZE GATE → DECIDE → EXECUTE → REPORT)
  → Multi-turn LLM loop (tool calls: search_facts, create_fact, update_fact, merge_facts)
  → Facts written to Firestore (SCD2, 3-vector embeddings)

Stage 2 — Inline cluster review  (inline_cluster_review=True)
  For each fact written in Stage 1 → semantic search → cluster of related facts
  → LLM reviews cluster: merge duplicates, decompose compounds, supersede stale
  → Skipped if Stage 1 wrote 0 facts

Stage 3 — Email triage
  Unconsolidated IndexedEmail records
  → Same LLM loop as Stage 1 but with email content
  → Marks emails as consolidated
```

**Intent API (internal=True):**

| Intent | Trigger |
|--------|---------|
| `consolidate_full` | Overflow, `$consolidate` — runs all 3 stages |
| `consolidate` | Stage 1 only |
| `consolidate_cluster` | Stage 2 only (cluster review) |
| `consolidate_email` | Stage 3 only (email triage) |

**Key mechanism — SIZE GATE:**
- When existing fact has `word_count > 40` AND planned op is UPDATE:
  - LLM runs co-location test
  - If facts span multiple concepts → decompose into atomic facts + SUPERSEDE original
  - 40-word hard limit is enforced explicitly in Stage 2 cluster review

**Deduplication strategy:**
- Semantic search before every write (multi-vector RRF)
- LLM decides: CREATE / UPDATE / MERGE / DISCARD
- SCD Type 2 history (`valid_from`, `valid_to`, `is_current`)

**Configuration:** `ConsolidationAgentConfig` in `src/infrastructure/agent_config.py`
- `max_turns=15` per stage (raised from 10 after Stage 2 on 25-fact cluster hit the limit)
- `timeout_ms=900_000` (15 min), Cloud Tasks `dispatch_deadline=1800s` (30 min)
- `thinking_effort="medium"` (Claude extended thinking)

**Code:** `src/agents/consolidation_agent.py`

See: [Sliding Window Consolidation](../05_building_blocks/sliding_window_consolidation/README.md)

---

## 🔄 Agent Communication Flow

### Synchronous Flow (Query)

```
User: "What's my car and current gas prices?"
    ↓
ConversationHandler → AgentCoordinator
    ↓
RouterAgent: complexity=8, routes to smart_response
    ↓
SmartResponseAgent: Analyzes query, decides to delegate
    ↓
┌──────────────────────┐  ┌───────────────────────┐
│ MemorySearchAgent    │  │ WebSearchAgent        │
│ searches "my car"    │  │ searches "gas prices" │
└──────────────────────┘  └───────────────────────┘
    ↓                          ↓
"Honda Civic 2019"       "€1.45/liter in Valencia"
    ↓──────────────┬────────────↓
                   ↓
           SmartResponseAgent synthesizes
                   ↓
           ConversationHandler persists session
                   ↓
User: "You have a Honda Civic 2019. Current gas prices
       in Valencia are around €1.45 per liter."
```

### Asynchronous Flow (Learning - v6.0)

```
User: "I moved to Valencia last month"
    ↓
SmartResponseAgent: Responds immediately
    ↓
SessionStore: Appends message to session
    ↓
[Session reaches 200 messages threshold]
    ↓
SessionStore: Creates batch in ConsolidationQueue
    ↓
[Background] ConsolidationHandler:
    ↓
Fetches batch → Triggers ConsolidationAgent
    ↓
ConsolidationAgent:
    Extracts: "User lives in Valencia" (high conf)
    Extracts: "User moved recently" (medium conf)
    ↓
Creates Fact: "User resides in Berlin, Germany"
Updates old fact: "User lived in Warsaw" → is_current=False
    ↓
Batch deleted from queue
```


---

## 🎯 Agent Selection Logic

### How RouterAgent Chooses Agents

**1. LLM Triage (Primary):**
```python
triage_result = await llm.generate(
    prompt="""Analyze this query:
    - Complexity (1-10)
    - Tone (casual/professional/technical/urgent)
    - Type (simple/personal/external)

    Query: "{query}"
    """
)
# complexity ≤5 → quick_response
# complexity >5 → smart_response
```

**2. Rule-Based Fallback:**
```python
if len(query.split()) <= 3 and "?" not in query:
    return "quick_response_agent"  # Greeting

if any(word in query for word in ["analyze", "compare", "explain"]):
    return "smart_response_agent"  # Complex reasoning
```

**3. Agent Capability Check:**
```python
class MemorySearchAgent(BaseAgent):
    config = AgentConfig(
        capabilities=["memory_search", "personal_data"]
    )

    async def can_handle(self, message: AgentMessage) -> bool:
        # Check if intent and payload are valid
        return (
            message.intent == AgentIntent.DELEGATE
            and "query" in message.payload
        )
```

**Example Decision Tree:**
```
Query: "What's my car?"
  ↓
RouterAgent: complexity=4, type=personal
  ↓
Routes to: QuickResponseAgent
  ↓
QuickResponseAgent: Recognizes personal query
  ↓
Option 1: Has recent memory in context → Answer directly
Option 2: Context insufficient → Could delegate to MemorySearchAgent
  ↓
Response delivered
```

---

## 💰 Cost Optimization Strategy

### Model Selection by Task Complexity (v6.0)

| Task | Agent | Tier | Model | Cost/1K tokens | Justification |
|------|-------|------|-------|----------------|---------------|
| **Vector Search** | Memory | — | None | FREE | Pure math, no LLM |
| **Simple Responses** | Quick | BALANCED | Flash | $0.0001 | Fast queries, bounded delegation |
| **Quick Web Lookup** | WebSearchLight | ECO | Flash Lite | ~$0.00002 | Single-pass grounding, cheapest tier |
| **Complex Responses** | Smart | PERFORMANCE | Thinking | $0.001 | Multi-step reasoning |
| **Full Web Search** | Web | BALANCED | Flash | $0.0001 | Deep search + synthesis |
| **Consolidation** | Consolidation | PERFORMANCE | Thinking | $0.001 | Knowledge synthesis |

**Cost Savings Example (100 messages/day):**
- **Old System:** All messages → Pro model → $10/day
- **New System (v6.0):**
  - 70 simple → Flash → $0.70
  - 30 complex → Thinking → $3.00
  - 1 consolidation → Thinking → $0.10
  - **Total:** $3.80/day
- **Savings: 62%**

---

## 🛡️ Resilience Patterns

### Circuit Breaker (Built into BaseAgent)

**Purpose:** Prevent cascading failures

**How It Works:**
```
Normal Operation:
  Agent → Success → Circuit CLOSED ✓

Failure Scenario:
  Agent → Fail #1 → Circuit CLOSED (retry)
  Agent → Fail #2 → Circuit CLOSED (retry)
  Agent → Fail #3 → Circuit OPEN ⚠️

Recovery:
  Wait 5 minutes → Circuit HALF-OPEN
  Test request → Success → Circuit CLOSED ✓
```

**Business Impact:**
- Failed agent doesn't block entire system
- User gets partial results instead of complete failure
- System auto-recovers without manual intervention

**Code:** `src/agents/base_agent.py:CircuitBreaker`

### Retry with Exponential Backoff

**Purpose:** Handle transient failures

**Pattern:**
```
Attempt 1: Fail → Wait 1s
Attempt 2: Fail → Wait 2s
Max Retries: 2 (configurable per agent)
```

**Business Impact:**
- Temporary network issues don't cause failures
- Rate limiting is respected
- User experience is smoother

---

## 📊 Observability & Monitoring

### What We Track (v6.0)

**Per-Agent Metrics:**
- Success/failure rate
- Average latency (p50, p95, p99)
- Token usage (cost tracking)
- Circuit breaker state

**Per-Request Metrics:**
- Total duration (end-to-end)
- Agent execution breakdown
- Delegation depth
- Error types and codes

**Business Value:**
- Identify slow agents → Optimize
- Track cost per user → Budget planning
- Monitor errors → Proactive fixes

**Implementation:**
- OpenTelemetry spans: `agent.process`, `agent.delegate`
- Cloud Trace integration
- Structured logging with trace IDs

See: [Observability Strategy](../05_building_blocks/observability_strategy/README.md)

---

---

## 📖 Summary

**Key Business Benefits:**
1. **Cost Optimization:** ~62% savings by routing to the right model (Flash for simple, Pro for complex)
2. **Context Quality:** Tiered history — recent turns get full context, older turns get compressed summary
3. **Structured Output:** SmartAgent outputs exclusively via `deliver_response` tool — guarantees history_summary and prevents plain-text leakage
4. **Reliability:** Circuit breakers, retries, graceful degradation across all agents
5. **Scalability:** Easy to add new specialist agents via AgentCoordinator registry

---

**Last Updated:** 2026-03-03
**Version:** 2.4
