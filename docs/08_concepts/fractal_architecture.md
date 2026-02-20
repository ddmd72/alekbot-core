# Fractal Architecture Pattern

**Also known as:** Recursive Hexagonal, Agent-as-Adapter Pattern

## 📖 HowTo: Using This Document

### Purpose
Explains the Fractal Architecture pattern: how adapters can themselves be agents, creating recursive hexagonal structures.

### When to Read
- **For AI Agents:** When designing new tools or specialist agents.
- **For Developers:** When deciding between simple, hybrid, or full hexagonal tool implementations.

### When to Update
This document MUST be updated when:
- [ ] New tool complexity levels are identified.
- [ ] Resilience patterns change (circuit breaker, retry logic).
- [ ] Self-recursion capabilities are added or modified.

### Cross-References
- **Multi-Agent System:** [../05_building_blocks/multi_agent_system/README.md](../05_building_blocks/multi_agent_system/README.md)
- **Target Architecture:** [../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md#8-fractal-architecture-pattern-recursive-hexagonal)
- **Agent Best Practices:** [./agent_best_practices.md](./agent_best_practices.md)

---

## 📖 Philosophy

Fractal Architecture is an extension of Hexagonal Architecture where Output Adapters are themselves agents, creating a recursive structure.

### Core Idea

**In classical hexagonal:**
```
Core → Port → Adapter → External Service
```

**In fractal hexagonal:**
```
Core → Port → Adapter-Agent → (Port → Adapter → Service)*
```

An Output Adapter can be a fully-fledged agent with its own ports and adapters, creating a **recursive structure**.

---

## 🎯 Advantages Over Linear Frameworks

### vs CrewAI

| Aspect | CrewAI | Fractal Hexagonal |
|--------|--------|-------------------|
| **Isolation** | Agents know about each other | Core does not know what is inside the adapter |
| **Testing** | Mocking the entire crew | Mocking a single port |
| **Model flexibility** | Usually one model | Cheap models in adapters |
| **Complexity** | Linear growth | Hierarchical (manageable) |

### vs LangChain Chains

| Aspect | LangChain | Fractal Hexagonal |
|--------|-----------|-------------------|
| **Structure** | Linear chains | Tree hierarchy |
| **Reuse** | Difficult | Adapter composition |
| **Debugging** | "Black box" | Clear boundaries |

---

## 🏗️ Implementation in Alek-Core

### Example 1: WebSearchAgent

**Current implementation (v6.0 - Agent-Based):**

```python
SmartResponseAgent (Core)
    ↓
AgentCoordinator (Router)
    ↓
WebSearchAgent (Specialist Agent)
    ↓ [creates its own Gemini client with Grounding]
Gemini API + GoogleSearch
```

**Key features:**
- ✅ SmartResponseAgent does not know that WebSearchAgent calls LLM itself
- ✅ Can be replaced with Serper.dev without changing Core
- ✅ Can use a cheap model (Flash) for search

**Code:**
```python
class WebSearchAgent(BaseAgent):
    def __init__(self, llm_service: LLMService, grounding_tool: types.Tool):
        # Agent creates its own LLM context
        self.llm_service = llm_service
        self.grounding_tool = grounding_tool

    async def process(self, message: AgentMessage) -> AgentResponse:
        # Call a separate LLM for search with Grounding
        response = await self.llm_service.generate(...)
        return AgentResponse.success(...)
```

**File:** `src/agents/web_search_agent.py`

### Example 2: Self-Delegation (Full Recursion)

**Concept:**
```python
SmartResponseAgent
    ↓
AgentCoordinator (Port)
    ↓
SelfDelegationAgent (Recursive Agent)
    ↓ [calls SmartResponseAgent again]
SmartResponseAgent (Sub-instance with clean context)
```

**Use Case:**
```
User: "Analyze my health for the year and give a financial plan"

SmartResponseAgent (parent):
  → delegate_to_fresh_mind("Analyze health data for 2025")
  → delegate_to_fresh_mind("Create financial plan based on: {health_summary}")
  → Synthesize results
```

**Guards:**
- Max recursion depth = 3
- Cost limiter per delegation
- Session isolation

---

## 📊 Agent Complexity Levels

### Level 1: Simple (Linear)

**When to use:**
- 0-1 external dependency
- Trivial logic
- Predictable result

**Example:**
```python
class TimezoneTool(BaseTool):
    async def execute(self, timezone: str) -> str:
        return datetime.now(pytz.timezone(timezone)).isoformat()
```

**Architecture:** Linear (hexagonal not needed)

---

### Level 2: Medium (Hybrid Hexagonal)

**When to use:**
- 1-2 external dependencies
- Simple business logic
- Dependencies can be swapped

**Example:** WebSearchAgent (current implementation v6.0)

**Architecture:** Hybrid
- Port exists (BaseAgent)
- Agent creates LLM contexts itself
- But inside the agent — linear logic

---

### Level 3: Complex (Full Hexagonal)

**When to use:**
- 3+ dependencies
- Complex orchestration
- Fallbacks and quality checks required
- Multiple potential implementations

**Example:**
```python
class MultiSearchAgent(BaseAgent):
    def __init__(
        self,
        primary_provider: SearchProvider,  # Port!
        fallback_provider: SearchProvider, # Port!
        cache_service: CacheService,       # Port!
        quality_checker: QualityChecker    # Port!
    ):
        # Full hexagonal structure inside the agent

    async def process(self, message: AgentMessage) -> AgentResponse:
        # 1. Check cache
        if cached := await self._cache.get(message.payload["query"]):
            return AgentResponse.success(result=cached, metadata={"cached": True})

        # 2. Try primary search
        result = await self._primary.search(message.payload["query"])

        # 3. Quality check
        if not await self._checker.is_good_enough(result):
            logger.warning("Primary search quality low, trying fallback")
            result = await self._fallback.search(message.payload["query"])

        # 4. Cache result
        await self._cache.set(message.payload["query"], result)

        return AgentResponse.success(result=result)
```

**Architecture:** Full Fractal Hexagonal
- Agent has its own ports
- Any dependency can be swapped
- Full testability

---

## 🛡️ Agent Resilience Patterns

### 1. Self-Correction (Built into BaseAgent)

**Code:** `src/agents/base_agent.py`

```python
class BaseAgent(ABC):
    MAX_RETRIES = 2

    async def execute_with_retry(self, message: AgentMessage) -> AgentResponse:
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await self.process(message)

                # Validate response
                if response.status == AgentStatus.SUCCESS:
                    return response

                logger.warning(f"Retry {attempt+1}: {response.status} from {self.agent_id}")

            except Exception as e:
                if attempt == self.MAX_RETRIES - 1:
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        error=f"MAX_RETRIES_EXCEEDED: {str(e)}"
                    )
                logger.warning(f"Retry {attempt+1}: {e}")

        return AgentResponse.failure(...)
```

### 2. Delegation Pattern (AgentCoordinator)

```python
# In SmartResponseAgent:
response = await self.coordinator.route_message(
    AgentMessage.create(
        sender=self.agent_id,
        recipient="memory_search_agent",
        intent=AgentIntent.DELEGATE,
        payload={"query": "user's preferences"}
    )
)

if response.status == AgentStatus.CANNOT_HANDLE:
    # Try alternative approach
    response = await self.coordinator.route_message(
        AgentMessage.create(
            sender=self.agent_id,
            recipient="web_search_agent",
            intent=AgentIntent.DELEGATE,
            payload={"query": "general information"}
        )
    )
```

**File:** `src/domain/agent.py`

### 3. Circuit Breaker (Built into BaseAgent)

**Code:** `src/agents/base_agent.py`

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=300):
        self._failures = {}
        self._threshold = failure_threshold
        self._timeout = recovery_timeout

    def is_open(self, agent_id: str) -> bool:
        if agent_id not in self._failures:
            return False

        failures, last_failure = self._failures[agent_id]

        # Auto-recovery after timeout
        if time.time() - last_failure > self._timeout:
            del self._failures[agent_id]
            return False

        return failures >= self._threshold

    def record_failure(self, agent_id: str):
        if agent_id in self._failures:
            count, _ = self._failures[agent_id]
            self._failures[agent_id] = (count + 1, time.time())
        else:
            self._failures[agent_id] = (1, time.time())
```

---

## 🔄 Self-Recursion Capabilities

### Pattern: Task Decomposition

```python
class SelfDelegationAgent(BaseAgent):
    def __init__(self, coordinator: AgentCoordinator):
        self.coordinator = coordinator
        self._max_depth = 3
        self._recursion_depth = 0

    async def process(self, message: AgentMessage) -> AgentResponse:
        if self._recursion_depth >= self._max_depth:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Maximum recursion depth reached"
            )

        self._recursion_depth += 1
        try:
            sub_session_id = f"delegation_{uuid.uuid4()}"

            response = await self.coordinator.route_message(
                AgentMessage.create(
                    sender=self.agent_id,
                    recipient="smart_response_agent",
                    intent=AgentIntent.DELEGATE,
                    payload=message.payload,
                    context={"session_id": sub_session_id}
                )
            )

            return response
        finally:
            self._recursion_depth -= 1
```

### Pattern: Domain Specialists

```python
class HierarchicalAgent(BaseAgent):
    specialist_prompts = {
        "medical": "You are a medical specialist with deep knowledge...",
        "financial": "You are a financial analyst expert...",
        "legal": "You are a legal consultant..."
    }

    async def process(self, message: AgentMessage) -> AgentResponse:
        domain = message.payload.get("domain")
        if domain not in self.specialist_prompts:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"UNKNOWN_DOMAIN: {domain}"
            )

        # Delegate to specialist agent with domain-specific prompt
        session_id = f"specialist_{domain}_{uuid.uuid4()}"
        prompt = f"{self.specialist_prompts[domain]}\n\nQuery: {message.payload['query']}"

        response = await self.coordinator.route_message(
            AgentMessage.create(
                sender=self.agent_id,
                recipient="smart_response_agent",
                intent=AgentIntent.DELEGATE,
                payload={"text": prompt},
                context={"session_id": session_id}
            )
        )

        return response
```

---

## ✅ Best Practices

### 1. Guard Against Infinite Loops
```python
MAX_RECURSION_DEPTH = 3
MAX_TOKENS_PER_DELEGATION = 1000
```

### 2. Session Isolation
- Parent context is NOT passed to child
- Only summary or specific facts
- Prevents context window explosion

### 3. Cost Tracking
```python
class CostTracker:
    def track_delegation(self, depth: int, tokens: int, cost: float):
        logger.info(f"Delegation depth={depth}, tokens={tokens}, cost=${cost}")

        if cost > DAILY_LIMIT:
            raise BudgetExceededError()
```

### 4. Monitoring & Alerts
- Track average delegation depth
- Alert on unusual recursion patterns
- Monitor token usage per agent

---

## ⚠️ Anti-Patterns

### ❌ Deep Recursion Without Limits
```python
# BAD
async def process(self, message: AgentMessage):
    return await self.coordinator.route_message(...)  # No depth check!
```

### ❌ Context Leakage
```python
# BAD
async def process(self, message: AgentMessage):
    # Passing entire parent context to child
    return await self.coordinator.route_message(
        AgentMessage.create(
            ...,
            context=parent_context  # Too much context!
        )
    )
```

### ❌ No Fallback Strategy
```python
# BAD
async def process(self, message: AgentMessage):
    return await self._primary_provider.search(query)
    # No fallback if fails!
```

---

## 📊 Decision Matrix

```
Is agent simple (0-1 dependency)?
├─ Yes → Linear Architecture
│   └─ Example: TimezoneTool
│
├─ Maybe (1-2 dependencies) → Hybrid Hexagonal
│   └─ Example: WebSearchAgent
│
└─ No (3+ dependencies) → Full Fractal Hexagonal
    └─ Example: MultiSearchAgent
        ├─ SearchProvider Port
        ├─ CacheService Port
        ├─ QualityChecker Port
        └─ Multiple implementations
```

---

## 🎓 Comparison with Industry Standards

### Alek-Core (Fractal Hexagonal) vs CrewAI

**Similarities:**
- ✅ Agent specialization
- ✅ Task delegation
- ✅ Hierarchical structure

**Differences:**
- ✨ **Full hexagonal isolation** (CrewAI agents know each other)
- ✨ **Port-based testing** (no need to mock entire crew)
- ✨ **Model flexibility** (different models per adapter)

### When to Use What?

**Use CrewAI if:**
- You need pre-built agent templates
- You're OK with opinionated structure
- Rapid prototyping

**Use Fractal Hexagonal if:**
- Need full control over architecture
- Multiple LLM providers
- Enterprise-grade testability
- Long-term maintainability

---

## 🚀 Future Evolution

### Milestone 7: Advanced Patterns
- Multi-Agent Consensus (voting on answers)
- Agent-to-Agent Communication Protocol (✅ Done in v6.0)
- Shared Memory Pool
- Dynamic agent loading (plugins)

---

**Last Updated:** 2026-01-30
**Status:** ✅ Current (v6.0 agent architecture documented)
