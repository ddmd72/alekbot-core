# ADR-004: Agent Handoff Pattern for Web Search

## Status

**Accepted** | **Date:** 2026-01-16 (RFC) | **Implemented:** 2026-01-23

---

## Context

### Problem Statement

Gemini API (google-genai SDK v1.x) has a technical limitation: **cannot use both Native Google Search (Grounding) and Custom Function Calling Tools in the same LLM request**. This creates a conflict:

- **Native Google Search** provides high-quality, summarization-based results ideal for current events
- **Custom Tools** (e.g., `search_memory`) are required for RAG and internal operations

Attempting both results in `400 INVALID_ARGUMENT` error.

### Alternative Considered: Google Custom Search Engine (CSE)

Using CSE API as a custom tool was tested but rejected due to:
- Limited context in search snippets
- Poor relevancy without complex query tuning
- No built-in summarization

### Available Options

**Option A: Intent Router (Pre-computation)**
- Classify query first (SEARCH_WEB vs INTERNAL)
- If SEARCH_WEB: dedicated LLM call with grounding → inject context → main LLM
- Sequential flow: increases latency

**Option B: Agent Handoff (Delegation)**
- Main agent (`SmartResponseAgent`) has access to `web_search_agent` via delegation
- When web info needed, delegate to WebSearchAgent
- WebSearchAgent uses dedicated Gemini instance with grounding only
- Returns result to main agent for synthesis

---

## Decision

**We chose Option B: Agent Handoff Pattern**

### Rationale

1. **Architectural Purity:** Main agent retains full control over delegation decision
2. **Flexibility:** Supports parallel execution of multiple agents (memory + web simultaneously)
3. **Scalability:** Pattern extends to other specialized agents (image analysis, code execution)
4. **Clean Separation:** Each agent has single responsibility

### Trade-offs Accepted

- Slightly more complex implementation (agent-within-delegation)
- Requires robust AgentCoordinator infrastructure
- Increased initial development time

---

## Implementation

### Architecture

```
SmartResponseAgent (Main Agent)
    ├─> has access to AgentCoordinator
    ├─> can delegate to WebSearchAgent
    └─> synthesizes results from multiple sources

WebSearchAgent (Specialist Agent)
    ├─> dedicated Gemini instance
    ├─> configured with Google Search grounding tool ONLY
    ├─> receives AgentMessage(intent=QUERY, payload={"query": "..."})
    └─> returns AgentResponse with search results
```

### Key Components

**WebSearchAgent** (`src/agents/web_search_agent.py`)
- **Capabilities:** Web search using Gemini Grounding
- **Model:** Uses tier-based model from AgentExecutionContext
- **Tools:** Google Search grounding tool only (no custom tools)
- **Prompt:** Augmented with "SearchAgent" persona for better result quality

**SmartResponseAgent** (`src/agents/core/smart_response_agent.py`)
- **Delegation:** Can invoke WebSearchAgent via AgentCoordinator
- **Synthesis:** Combines web search results with memory search, reasoning
- **Parallel Execution:** Can call multiple specialist agents simultaneously

**AgentCoordinator** (`src/infrastructure/agent_coordinator.py`)
- **Routing:** Routes AgentMessage to correct specialist agent
- **Isolation:** Each agent has independent LLM instance and configuration

### Message Flow

```
1. User: "What's the weather in Barcelona tomorrow?"
2. SmartResponseAgent: Determines need for web search
3. Delegates: AgentMessage(intent=QUERY, query="weather Barcelona tomorrow")
4. WebSearchAgent: Calls Gemini with grounding tool
5. Returns: AgentResponse with weather summary
6. SmartResponseAgent: Synthesizes final answer
```

---

## Consequences

### Positive

- ✅ **Best of Both Worlds:** High-quality grounding + full custom tool access
- ✅ **Extensible:** Pattern reusable for image, code, translation agents
- ✅ **Testable:** WebSearchAgent can be tested in isolation
- ✅ **Configurable:** Each agent has independent model/tier configuration

### Negative

- ⚠️ **Latency:** Two LLM calls instead of one (delegation overhead ~200-500ms)
- ⚠️ **Complexity:** Requires AgentCoordinator, AgentMessage protocol
- ⚠️ **Token Cost:** Main agent must decide to delegate (consumes tokens)

### Neutral

- 🔄 **Agent Proliferation:** More agents = more coordination overhead
- 🔄 **Debugging:** Multi-agent flows harder to trace (requires good observability)

---

## Compliance

### How This Aligns with Alek-Core Principles

- **Hexagonal Architecture:** WebSearchAgent is a domain service, Google Search is infrastructure adapter
- **Actor Model:** Each agent is an isolated actor with message-based communication
- **Clean Architecture:** Decision logic (SmartResponseAgent) separated from execution (WebSearchAgent)
- **Provider Agnostic:** Pattern works with any LLM provider that supports grounding

---

## Lessons Learned

### What Worked Well

1. **Groovy Persona Injection:** Augmenting query with "SearchAgent" persona improved result quality
2. **Confidence Scoring:** Length-based confidence (len/500) correlates well with result quality
3. **Parallel Delegation:** SmartResponseAgent can call memory + web simultaneously

### What Didn't Work

- **Tool-Based Approach:** Initial attempt to wrap Google Search as a custom tool failed due to API limitation
- **CSE Fallback:** Google Custom Search Engine API quality too low for production

### Future Improvements

- [ ] **Caching:** Cache web search results for identical queries (1-hour TTL)
- [ ] **Refinement Loop:** If WebSearchAgent returns low confidence, allow retry with refined query
- [ ] **Source Attribution:** Extract and return source URLs from grounding metadata

---

## References

- **RFC:** `docs/architecture/rfcs/SEARCH_STRATEGY_RFC.md` (archived)
- **Implementation:** `src/agents/web_search_agent.py`
- **Tests:** `tests/integration/test_web_search_agent.py`
- **Related ADRs:**
  - ADR-001: Actor Model
  - ADR-003: Sliding Window Consolidation

---

## Status History

| Date | Status | Reason |
|------|--------|--------|
| 2026-01-16 | Proposed (RFC) | Initial proposal |
| 2026-01-23 | Accepted & Implemented | WebSearchAgent deployed to production |
| 2026-01-30 | Documented (ADR) | RFC converted to ADR during migration |
