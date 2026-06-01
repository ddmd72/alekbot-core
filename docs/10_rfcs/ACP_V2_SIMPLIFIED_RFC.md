# RFC: Agent Communication Protocol v2 - Simplified (Registry Pattern)

**Status:** Implemented (2026-02-21)  
**Date:** 2026-02-12  
**Milestone:** Phase 2 - Multi-Agent Evolution  
**Supersedes:** ACP v1 (src/domain/agent.py) and the complex ACP v2 variant (since removed)

---

## 1. Executive Summary

Current Agent Communication Protocol (ACP v1) is **synchronous-only**, blocking Gmail indexing and other long-running tasks. The original ACP v2 RFC proposed a **complex multi-agent orchestration system** with 4 verbs, inbox pattern, and loop prevention.

**This simplified RFC** focuses on **scalability without complexity**:

- ✅ **Agent Registry Pattern**: Dynamic agent discovery prevents SmartAgent from becoming a tool monster
- ✅ **2 Execution Modes**: Simple sync/async (no complex verb semantics)
- ✅ **3 Generic Tools**: SmartAgent delegates to specialists without knowing implementation details
- ✅ **Easy Integration**: New agents added with 3 lines of code, zero SmartAgent changes
- ✅ **Proactive Memory First**: Architecture supports unique features (biographical context, semantic lens)

**Result:** 2-week implementation (vs 5 weeks complex version), scales to 50+ integrations without prompt bloat.

---

## 2. Problem Statement

### 2.1 Current Limitations (ACP v1)

```python
# Synchronous blocking
message = AgentMessage.create(
    sender="smart_agent",
    recipient="gmail_agent",
    intent=AgentIntent.QUERY,
    payload={"query": "index all emails"}
)

response = await coordinator.route_message(message)  # ⏳ BLOCKS for 90 seconds
```

**Problems:**

1. **No async execution**: Gmail indexing blocks user for 90 seconds
2. **No background tasks**: Consolidation cannot run in background

---

### 2.2 Future Challenge: SmartAgent Monster

**Scenario:** Add 10 integrations (Gmail, Jira, Calendar, GitHub, Slack, Notion, etc.)

**Bad Approach (Tool Monster):**

```python
# SmartAgent prompt explodes
TOOLS = [
    "search_memory",          # Memory
    "search_web",             # Web
    "index_gmail",            # Gmail
    "search_gmail",           # Gmail
    "create_jira_ticket",     # Jira
    "search_jira",            # Jira
    "check_calendar",         # Calendar
    "add_calendar_event",     # Calendar
    "search_github_issues",   # GitHub
    "create_github_pr",       # GitHub
    "search_slack",           # Slack
    "send_slack_message",     # Slack
    "search_notion",          # Notion
    "create_notion_page",     # Notion
    # ... 50+ tools total
]

# Prompt: 5000+ lines
# LLM: confused which tool to use
# Maintenance: nightmare (every new agent = change SmartAgent)
```

**Consequences:**

- ❌ Prompt bloat (5000+ lines)
- ❌ LLM confusion (50+ tools = poor selection accuracy)
- ❌ Tight coupling (new agent = modify SmartAgent)
- ❌ Testing nightmare (mock 50+ tools)

---

## 3. Solution: Agent Registry + Router Pattern

### 3.1 Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  USER REQUEST                       │
│         "find my medical tests" / "index Gmail"    │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│            HybridRouter (existing)                  │
│  - Quick intent classification                      │
│  - Routes to: QuickAgent / SmartAgent               │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│          SmartAgent (Clean Orchestrator)            │
│                                                     │
│  Tools (only 3, never grows):                      │
│    1. delegate_to_specialist(intent, query)        │
│    2. respond_directly(text)                       │
│    3. ask_clarification(question)                  │
│                                                     │
│  Prompt: 200 lines (fixed forever)                 │
│  Knows: intents (abstract), not implementations    │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼ delegate_to_specialist("search_email", "medical tests")
┌─────────────────────────────────────────────────────┐
│            AgentRegistry (NEW)                      │
│  - Dynamic agent discovery                          │
│  - Intent → Agent mapping                           │
│  - Execution mode routing (sync/async)              │
│                                                     │
│  Returns: GmailAgent (execution_mode=sync)          │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────┐
│         GmailAgent (Specialist)                     │
│  - search_email(query) → semantic search            │
│  - index_gmail() → async background indexing        │
│  Returns: [7 email results]                         │
└───────────────────┬─────────────────────────────────┘
                    │
                    ▼ SmartAgent formats response
┌─────────────────────────────────────────────────────┐
│                  USER RESPONSE                      │
│           "Found 7 records with medical tests..."  │
└─────────────────────────────────────────────────────┘
```

### 3.2 Key Principles

1. **SmartAgent = Generic Orchestrator**: Knows intents (what), not implementations (how)
2. **AgentRegistry = Discovery Service**: Maps intents to specialist agents
3. **Specialists = Domain Experts**: Gmail, Jira, Calendar, etc. (self-contained)
4. **2 Execution Modes**: Sync (immediate) or Async (background + callback)

---

## 4. Component Design

### 4.1 SmartAgent: Minimal Tool Set

```python
# src/agents/smart_agent.py

class SmartAgent(BaseAgent):
    """
    Clean orchestrator with fixed 3 tools.
    Never grows with new integrations.
    """

    def get_tools(self) -> List[ToolDefinition]:
        """Tools exposed to LLM."""
        return [
            ToolDefinition(
                name="delegate_to_specialist",
                description="""
Delegate task to specialist agent.

Use when user needs specialized action (search, indexing, integration).

Parameters:
- intent: What to do (e.g., "search_email", "create_ticket")
- query: User's question or command
- context: Optional parameters

Available intents (auto-updated):
{{#each available_intents}}
- {{name}}: {{description}}
{{/each}}
""",
                parameters={
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "Action to perform (from available intents)"
                        },
                        "query": {
                            "type": "string",
                            "description": "User's question or command"
                        },
                        "context": {
                            "type": "object",
                            "description": "Optional parameters"
                        }
                    },
                    "required": ["intent", "query"]
                }
            ),
            ToolDefinition(
                name="respond_directly",
                description="Answer directly without delegation (simple questions)",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"}
                    },
                    "required": ["text"]
                }
            ),
            ToolDefinition(
                name="ask_clarification",
                description="Ask user for clarification when request is ambiguous",
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"}
                    },
                    "required": ["question"]
                }
            )
        ]
```

**Prompt Structure (200 lines, fixed):**

```groovy
## Role

You are a smart orchestrator that delegates tasks to specialist agents.

## Available Tools

1. delegate_to_specialist - Delegate to specialist
2. respond_directly - Answer simple questions
3. ask_clarification - Request clarification

## Available Intents

{{#each available_intents}}
- **{{name}}**: {{description}}
{{/each}}

## Decision Rules

1. **Delegate** when task requires specialist knowledge:
   - Search queries → delegate_to_specialist("search_memory" or "search_email")
   - Indexing → delegate_to_specialist("index_gmail")
   - Integration tasks → delegate_to_specialist("create_ticket", "add_event", etc.)

2. **Respond directly** for simple questions:
   - "hey", "how are you" → respond_directly
   - General knowledge questions → respond_directly

3. **Ask clarification** when ambiguous:
   - "find my documents" → which type? (email, notion, slack?)

## Current Request

User: {{user_query}}
Context: {{biographical_context}}
```

**Key Point:** Prompt NEVER changes when adding new agents. Only `available_intents` list updates dynamically.

---

### 4.2 AgentRegistry: Dynamic Discovery

```python
# src/infrastructure/agent_registry.py

@dataclass
class AgentManifest:
    """Agent capability declaration."""
    agent_id: str                    # "gmail_agent"
    intents: List[str]               # ["search_email", "index_gmail"]
    description: str                 # "Gmail integration specialist"
    execution_mode: ExecutionMode    # SYNC or ASYNC
    requires_auth: bool = False      # OAuth required?

class ExecutionMode(str, Enum):
    SYNC = "sync"      # Immediate response (search queries)
    ASYNC = "async"    # Background task + callback (indexing)

class AgentRegistry:
    """
    Central registry for agent discovery.
    Enables adding new agents without modifying SmartAgent.
    """

    def __init__(self):
        self._agents: Dict[str, AgentManifest] = {}
        self._intent_to_agent: Dict[str, str] = {}

    def register(self, manifest: AgentManifest):
        """
        Register new agent.

        Example:
            registry.register(AgentManifest(
                agent_id="jira_agent",
                intents=["search_jira", "create_ticket"],
                description="Jira integration for teams",
                execution_mode=ExecutionMode.SYNC
            ))
        """
        self._agents[manifest.agent_id] = manifest

        for intent in manifest.intents:
            if intent in self._intent_to_agent:
                logger.warning(f"Intent '{intent}' already registered, overwriting")
            self._intent_to_agent[intent] = manifest.agent_id

    def get_agent_for_intent(self, intent: str) -> Optional[AgentManifest]:
        """Get agent that handles given intent."""
        agent_id = self._intent_to_agent.get(intent)
        if agent_id:
            return self._agents[agent_id]
        return None

    def get_available_intents(self) -> List[Dict[str, str]]:
        """
        Get all intents for SmartAgent prompt.
        Auto-updates when new agents registered.
        """
        intents = []
        for agent_id, manifest in self._agents.items():
            for intent in manifest.intents:
                intents.append({
                    "name": intent,
                    "description": manifest.description
                })
        return intents

    def list_agents(self) -> List[AgentManifest]:
        """List all registered agents."""
        return list(self._agents.values())
```

---

### 4.3 AgentCoordinator: Simplified Routing

```python
# src/infrastructure/agent_coordinator.py (simplified)

class AgentCoordinator:
    """
    Routes agent communication based on execution mode.
    Simplified: only sync/async, no complex verbs.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        cloud_tasks: CloudTasksService,
        response_channel: ResponseChannel
    ):
        self.registry = registry
        self.cloud_tasks = cloud_tasks
        self.response_channel = response_channel

    async def handle_delegation(
        self,
        intent: str,
        query: str,
        context: Dict[str, Any],
        calling_agent_id: str
    ) -> AgentResponse:
        """
        Handle delegate_to_specialist tool call.

        Routes based on execution mode:
        - SYNC: Execute immediately, return result
        - ASYNC: Enqueue to Cloud Tasks, return ack
        """
        # Discover agent
        manifest = self.registry.get_agent_for_intent(intent)
        if not manifest:
            return AgentResponse.failure(
                error="UNKNOWN_INTENT",
                message=f"No agent registered for intent: {intent}"
            )

        # Route based on execution mode
        if manifest.execution_mode == ExecutionMode.SYNC:
            return await self._execute_sync(manifest, query, context)
        elif manifest.execution_mode == ExecutionMode.ASYNC:
            return await self._execute_async(manifest, query, context, calling_agent_id)

    async def _execute_sync(
        self,
        manifest: AgentManifest,
        query: str,
        context: Dict[str, Any]
    ) -> AgentResponse:
        """Execute synchronously (search queries)."""
        agent = self._get_agent_instance(manifest.agent_id)
        result = await agent.execute(query, context)
        return result

    async def _execute_async(
        self,
        manifest: AgentManifest,
        query: str,
        context: Dict[str, Any],
        calling_agent_id: str
    ) -> AgentResponse:
        """
        Execute asynchronously (long tasks).

        Flow:
        1. Enqueue to Cloud Tasks
        2. Return acknowledgment immediately
        3. Worker executes task in background
        4. Notify user via Slack when done
        """
        task_id = await self.cloud_tasks.enqueue(
            queue_name="agent-tasks",
            handler="/worker/agent-execution",
            payload={
                "agent_id": manifest.agent_id,
                "query": query,
                "context": context,
                "callback": {
                    "user_id": context["user_id"],
                    "channel": "slack"
                }
            }
        )

        return AgentResponse.success(
            result={
                "status": "started",
                "task_id": task_id,
                "message": f"Task started. Will notify when complete."
            }
        )
```

---

### 4.4 Background Worker: Async Execution

```python
# src/handlers/worker_handler.py (NEW)

class AgentWorkerHandler:
    """
    Handles async agent execution from Cloud Tasks.
    Notifies user when task completes.
    """

    async def handle_task(self, request: Dict[str, Any]):
        """Execute agent task in background."""
        agent_id = request["agent_id"]
        query = request["query"]
        context = request["context"]
        callback = request["callback"]

        try:
            # Execute agent
            agent = self._get_agent(agent_id)
            result = await agent.execute(query, context)

            # Notify user (via Slack)
            await self._notify_completion(
                user_id=callback["user_id"],
                channel=callback["channel"],
                result=result
            )

        except Exception as e:
            # Notify failure
            await self._notify_failure(
                user_id=callback["user_id"],
                channel=callback["channel"],
                error=str(e)
            )

    async def _notify_completion(
        self,
        user_id: str,
        channel: str,
        result: AgentResponse
    ):
        """Send completion notification to user."""
        if channel == "slack":
            await self.slack_client.send_message(
                user_id=user_id,
                text=f"✅ Task completed!\n\n{result.message}"
            )
```

---

## 5. Usage Examples

### 5.1 Example 1: Synchronous Search

```
User: "find my medical tests from 2025"

SmartAgent (LLM generates):
  delegate_to_specialist(
    intent="search_email",
    query="medical tests from 2025"
  )

Coordinator:
  manifest = registry.get_agent_for_intent("search_email")
  # → GmailAgent, execution_mode=SYNC

  result = await gmail_agent.execute(query)
  # Returns: [7 email results]

SmartAgent formats response:
  "Found 7 emails with medical tests from 2025..."

Total latency: ~3 seconds (sync)
```

---

### 5.2 Example 2: Async Indexing

```
User: "index my Gmail"

SmartAgent (LLM generates):
  delegate_to_specialist(
    intent="index_gmail",
    query="index all emails from 2020"
  )

Coordinator:
  manifest = registry.get_agent_for_intent("index_gmail")
  # → GmailAgent, execution_mode=ASYNC

  task_id = await cloud_tasks.enqueue(...)
  return {"status": "started", "task_id": "xyz"}

SmartAgent responds:
  "✅ Started Gmail indexing. Will take 1-2 minutes, will notify when done."

[90 seconds later, background worker]

Worker:
  result = await gmail_agent.execute("index all emails")
  await slack.send_message(
    user_id="U123",
    text="✅ Indexing complete! 8,432 emails."
  )

User receives Slack notification:
  "✅ Indexing complete! 8,432 emails."
```

---

## 6. Adding New Agent: 5 Minutes, Zero SmartAgent Changes

### Step 1: Create Agent (new file)

```python
# src/agents/jira_agent.py

class JiraAgent(BaseAgent):
    """Jira integration specialist."""

    async def execute(self, query: str, context: Dict) -> AgentResponse:
        """Handle Jira operations."""

        # Parse intent from query
        if "create ticket" in query.lower():
            return await self._create_ticket(query, context)
        elif "search" in query.lower():
            return await self._search_issues(query, context)
        else:
            return AgentResponse.failure("Unknown Jira operation")

    async def _create_ticket(self, query: str, context: Dict) -> AgentResponse:
        # Extract ticket details from query (LLM or regex)
        # Call Jira API
        # Return success
        pass

    async def _search_issues(self, query: str, context: Dict) -> AgentResponse:
        # Parse search query
        # Call Jira API
        # Return results
        pass
```

---

### Step 2: Register Agent (3 lines in main.py)

```python
# main.py

# Existing agents
registry.register(AgentManifest(
    agent_id="memory_search",
    intents=["search_memory"],
    description="Search biographical facts",
    execution_mode=ExecutionMode.SYNC
))

registry.register(AgentManifest(
    agent_id="gmail_agent",
    intents=["search_email", "index_gmail"],
    description="Gmail integration",
    execution_mode=ExecutionMode.SYNC  # search is sync, index is async (handled internally)
))

# NEW: Jira agent (3 lines)
registry.register(AgentManifest(
    agent_id="jira_agent",
    intents=["search_jira", "create_ticket"],
    description="Jira integration for teams",
    execution_mode=ExecutionMode.SYNC
))
```

---

### Step 3: Done! SmartAgent Auto-Updates

```
User: "create a Jira ticket: Bug in login"

SmartAgent (sees new intent in prompt):
  Available intents:
  - search_memory: Search biographical facts
  - search_email: Gmail integration
  - index_gmail: Gmail integration
  - search_jira: Jira integration for teams  ← NEW
  - create_ticket: Jira integration for teams  ← NEW

SmartAgent (LLM generates):
  delegate_to_specialist(
    intent="create_ticket",
    query="Bug in login"
  )

Coordinator:
  manifest = registry.get_agent_for_intent("create_ticket")
  # → JiraAgent, execution_mode=SYNC

  result = await jira_agent.execute(query)

SmartAgent responds:
  "✅ Created ticket PROJ-1234: Bug in login"
```

**Zero changes to SmartAgent code or prompt template!**

---

## 7. Scalability: From 3 Agents to 50+ Integrations

### MVP (Week 1-2)

```python
registry.register(MemorySearchAgent)    # search_memory
registry.register(WebSearchAgent)       # search_web
registry.register(GmailAgent)           # search_email, index_gmail

# SmartAgent prompt: 200 lines
# Intents: 4
```

---

### Phase 2: Team Integrations (1 week per agent)

```python
registry.register(JiraAgent)            # search_jira, create_ticket
registry.register(CalendarAgent)        # check_calendar, add_event
registry.register(GitHubAgent)          # search_github, create_pr
registry.register(SlackAgent)           # search_slack, send_message

# SmartAgent prompt: 200 lines (unchanged!)
# Intents: 12
```

---

### Phase 3: Enterprise (1 week per agent)

```python
registry.register(NotionAgent)          # search_notion, create_page
registry.register(ConfluenceAgent)      # search_confluence
registry.register(LinearAgent)          # search_linear, create_issue
registry.register(FigmaAgent)           # search_figma
registry.register(AsanaAgent)           # search_asana, create_task
# ... 20 more agents

# SmartAgent prompt: 200 lines (still unchanged!)
# Intents: 50+
```

**Key Point:** SmartAgent code and prompt NEVER change. Only registry grows.

---

## 8. Comparison: Registry Pattern vs Tool Monster

### Approach A: Tool Monster (BAD - don't do this)

```python
# SmartAgent grows with every integration
class SmartAgent:
    def get_tools(self):
        return [
            search_memory,
            search_web,
            index_gmail,
            search_gmail,
            create_jira_ticket,
            search_jira,
            check_calendar,
            add_event,
            search_github,
            create_pr,
            # ... 50 tools
        ]

# Prompt: 5000+ lines (50 tools × 100 lines each)
# LLM confusion: High (50+ options)
# Maintenance: Nightmare
# Test complexity: Mock 50+ tools
```

**Problems:**

- ❌ Every new agent = modify SmartAgent
- ❌ Prompt bloat (5000+ lines)
- ❌ LLM accuracy drops (too many choices)
- ❌ Tight coupling (hard to test, hard to maintain)

---

### Approach B: Registry Pattern (GOOD - this RFC)

```python
# SmartAgent fixed (3 tools, never changes)
class SmartAgent:
    def get_tools(self):
        return [
            delegate_to_specialist,  # Generic delegation
            respond_directly,
            ask_clarification
        ]

# Registry grows (but SmartAgent doesn't)
registry.register(GmailAgent)
registry.register(JiraAgent)
registry.register(CalendarAgent)
# ... 50 agents

# Prompt: 200 lines (fixed)
# LLM sees: intents only (abstract), not implementations
# Maintenance: Easy (each agent self-contained)
# Test complexity: Test agents independently
```

**Advantages:**

- ✅ SmartAgent never changes (3 tools forever)
- ✅ Prompt stays small (200 lines)
- ✅ LLM accuracy stays high (intents vs tools)
- ✅ Loose coupling (agents independent, easy to test)

---

## 9. Implementation Plan

### Phase 1: Core Infrastructure (Week 1)

**Day 1-2: AgentRegistry**

- [ ] Create AgentManifest dataclass
- [ ] Create ExecutionMode enum
- [ ] Implement AgentRegistry.register()
- [ ] Implement AgentRegistry.get_agent_for_intent()
- [ ] Implement AgentRegistry.get_available_intents()
- [ ] Unit tests (10 tests)

**Day 3: AgentCoordinator Refactoring**

- [ ] Add AgentRegistry dependency
- [ ] Implement handle_delegation()
- [ ] Implement \_execute_sync()
- [ ] Implement \_execute_async()
- [ ] Integration tests (5 tests)

**Day 4: Worker Handler**

- [ ] Create AgentWorkerHandler
- [ ] Implement handle_task()
- [ ] Implement \_notify_completion() (Slack)
- [ ] Implement \_notify_failure()
- [ ] E2E test (async execution)

**Day 5: SmartAgent Tool Update**

- [ ] Remove old tool definitions
- [ ] Add delegate_to_specialist tool
- [ ] Add respond_directly tool
- [ ] Add ask_clarification tool
- [ ] Update prompt template with available_intents
- [ ] E2E tests (3 scenarios)

---

### Phase 2: Agent Migration (Week 2)

**Day 1: Register Existing Agents**

- [ ] Create manifests for MemorySearchAgent
- [ ] Create manifests for WebSearchAgent
- [ ] Create manifests for ConsolidationAgent
- [ ] Register in main.py
- [ ] Test delegation flows

**Day 2: GmailAgent Enhancement**

- [ ] Split search_email (sync) and index_gmail (async)
- [ ] Register GmailAgent with both intents
- [ ] Test sync search flow
- [ ] Test async indexing flow

**Day 3-4: Documentation**

- [ ] Update Multi-Agent System building block
- [ ] Create Agent Registry guide
- [ ] Update SmartAgent documentation
- [ ] Create "Adding New Agent" tutorial

**Day 5: Testing & Polish**

- [ ] End-to-end test suite (10 scenarios)
- [ ] Performance benchmarks
- [ ] Documentation review
- [ ] Code review

---

## 10. Migration from ACP v1

### Backward Compatibility

```python
# Old code (ACP v1) still works
message = AgentMessage.create(
    intent=AgentIntent.QUERY,
    recipient="memory_search_agent",
    payload={"query": "find tests"}
)
response = await coordinator.route_message(message)

# New code (ACP v2) for SmartAgent
# SmartAgent uses delegate_to_specialist tool
# Coordinator translates to AgentMessage internally
```

**Strategy:**

1. **Phase 1**: Registry + new SmartAgent tools (SmartAgent uses new system)
2. **Phase 2**: Migrate specialist agents to registry (one by one)
3. **Phase 3**: Deprecate old AgentMessage.create() API (6 months notice)

---

## 11. Open Questions & Decisions

### Q1: Intent Granularity

**Question:** Should intents be atomic or composite?

**Options:**

A. Atomic (separate intents)

```python
intents=["search_email", "index_gmail"]
```

B. Composite (agent handles routing)

```python
intents=["gmail"]  # Agent internally routes to search vs index
```

**Decision:** **A (Atomic)** - More explicit, better for LLM tool selection

---

### Q2: Execution Mode Per Intent or Per Agent?

**Question:** Can agent have mixed execution modes?

**Example:** GmailAgent has search (sync) and index (async)

**Options:**

A. Per Agent (current)

```python
AgentManifest(
    agent_id="gmail_agent",
    intents=["search_email", "index_gmail"],
    execution_mode=ExecutionMode.SYNC  # But index is async?
)
```

B. Per Intent

```python
AgentManifest(
    agent_id="gmail_agent",
    intents={
        "search_email": ExecutionMode.SYNC,
        "index_gmail": ExecutionMode.ASYNC
    }
)
```

**Decision:** **B (Per Intent)** - More flexible, handles mixed scenarios

**Update AgentManifest:**

```python
@dataclass
class AgentManifest:
    agent_id: str
    intents: Dict[str, ExecutionMode]  # intent → mode mapping
    description: str
```

---

### Q3: Callback Channel

**Question:** How to notify user when async task completes?

**Options:**

A. Always Slack (hardcoded)  
B. Configurable per user (Slack, Telegram, Email)  
C. Multi-channel (all platforms user is active on)

**Decision:** **A for MVP**, B for Phase 2

---

## 12. Success Criteria

### Performance

- **Delegation latency**: <500ms (registry lookup + routing)
- **Sync execution**: <5s (search queries)
- **Async ack**: <1s (enqueue to Cloud Tasks)
- **Callback delivery**: <10s (after task completion)

### Scalability

- **Agent growth**: Add 10 agents without SmartAgent changes ✅
- **Prompt size**: Stay under 300 lines (200 base + 100 intents) ✅
- **LLM accuracy**: >90% correct intent selection with 20+ intents ✅

### Developer Experience

- **New agent time**: <1 hour (code + register + test)
- **Lines of code**: <5 lines in main.py (registration)
- **Zero coupling**: No changes to existing agents when adding new ones

---

## 13. Future Enhancements (Phase 3+)

### 1. Intent Parameters Schema

```python
AgentManifest(
    agent_id="jira_agent",
    intents={
        "create_ticket": IntentDef(
            execution_mode=ExecutionMode.SYNC,
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "high"]}
                }
            }
        )
    }
)
```

---

### 2. Agent Versioning

```python
AgentManifest(
    agent_id="gmail_agent",
    version="2.0",
    intents=["search_email", "index_gmail"]
)

# Support multiple versions simultaneously
registry.get_agent_for_intent("search_email", version="2.0")
```

---

### 3. Agent Marketplace

- Discover community-contributed agents
- Install with `alek install jira-agent`
- Auto-register in AgentRegistry

---

### 4. Intent Aliases

```python
IntentDef(
    canonical="search_email",
    aliases=["find_emails", "search_gmail", "find emails"]
)
# LLM can use any alias, maps to canonical intent
```

---

## 14. References

**Related RFCs:**

- [GMAIL_EMAIL_INDEXING_RFC.md](./GMAIL_EMAIL_INDEXING_RFC.md) - Motivating use case

**Building Blocks:**

- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md)
- [Hybrid Router](../05_building_blocks/hybrid_router/README.md)

**Industry References:**

- LangChain AgentExecutor (complex tool orchestration)
- Microsoft Semantic Kernel (planner pattern)
- AutoGPT (task decomposition)

**Design Patterns:**

- Registry Pattern (Martin Fowler)
- Strategy Pattern (execution mode selection)
- Facade Pattern (SmartAgent as facade to specialists)

---

## Changelog

### 2026-02-12

- Initial simplified RFC created
- Agent Registry Pattern designed
- 3-tool SmartAgent architecture
- 2 execution modes (sync/async) instead of 4 verbs
- 2-week implementation plan (vs 5 weeks complex version)
- Focus on scalability without complexity
- Per-intent execution mode granularity

---

**Last Updated:** 2026-02-21
**Status:** ✅ Implemented (SYNC path production-ready; ASYNC infrastructure in place)
**Implementation:** Commit `54e250f` (2026-02-21).
**Next Steps:** Gmail Agent (first ASYNC agent + user notification callback)
