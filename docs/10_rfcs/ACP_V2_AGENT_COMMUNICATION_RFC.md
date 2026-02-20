# RFC: Agent Communication Protocol v2 (ACP v2)

**Status:** Proposed  
**Date:** 2026-02-11  
**Owner:** AI Engineering (Cline)  
**Milestone:** Phase 2 - Multi-Agent Evolution

**Related Building Blocks:** Multi-Agent System, Hybrid Router  
**Related ADRs:** TBD  
**Supersedes:** ACP v1 (src/domain/agent.py)

---

## 1. Executive Summary

Current Agent Communication Protocol (ACP v1) supports only **synchronous request-response** patterns. This blocks implementation of long-running tasks like Gmail indexing, background consolidation, and proactive agent behaviors.

**ACP v2** introduces **verb-based communication model** with native LLM tool support, enabling:

- ✅ Fire-and-forget delegation (INSTRUCT)
- ✅ Synchronous queries (ASK)
- ✅ Asynchronous notifications (INFORM)
- ✅ Collaborative workflows (COLLABORATE)
- ✅ Provider-agnostic implementation (Hexagonal Architecture)

---

## 2. Problem Statement

### 2.1 Current Limitations (ACP v1)

```python
# SmartAgent delegates to MemoryAgent
message = AgentMessage.create(
    sender="smart_agent",
    recipient="memory_search_agent",
    intent=AgentIntent.QUERY,
    payload={"query": "find medical tests"}
)

response = await coordinator.route_message(message)  # ⏳ BLOCKS
# Must wait for response before continuing
```

**Problems:**

1. **No async execution:** Cannot start long task without blocking
2. **No callback mechanism:** Cannot notify when task completes
3. **No progress tracking:** User has no visibility into long operations
4. **LLM confusion:** Agent doesn't know which pattern to use

### 2.2 Motivating Use Case: Gmail Indexing

**Scenario:** User asks "index my Gmail"

**Required flow:**

1. SmartAgent delegates indexing to GmailAgent (90 seconds)
2. SmartAgent immediately responds to user "Started, will notify"
3. GmailAgent indexes in background (Cloud Tasks)
4. GmailAgent notifies SmartAgent when complete
5. SmartAgent informs user "Done, 8432 emails indexed"

**Current ACP v1:** ❌ Cannot implement (blocks for 90 seconds)  
**ACP v2:** ✅ Natural workflow with INSTRUCT verb

### 2.3 Additional Use Cases

| Use Case                 | Required Pattern           | ACP v1 | ACP v2            |
| ------------------------ | -------------------------- | ------ | ----------------- |
| Search facts             | Request-response           | ✅ ASK | ✅ ASK            |
| Index Gmail              | Fire-and-forget + callback | ❌     | ✅ INSTRUCT       |
| Background consolidation | Fire-and-forget            | ❌     | ✅ INSTRUCT       |
| Progress updates         | Streaming notifications    | ❌     | ✅ INFORM (multi) |
| Proactive suggestions    | Agent-initiated            | ❌     | ✅ INFORM         |
| Batch processing         | Collaborative parallel     | ❌     | ✅ COLLABORATE    |

---

## 3. Proposed Solution: Verb-Based Protocol

### 3.1 Core Concept

**Natural language verbs** define communication patterns, understandable to both LLMs and humans.

### 3.2 Communication Verbs

```python
class AgentVerb(str, Enum):
    """
    Natural language verbs for agent communication.
    Each verb implies execution semantics without explicit configuration.
    """

    ASK = "ask"
    """
    Request immediate answer (synchronous).

    Semantics:
    - Sender waits for response
    - Recipient executes and returns result
    - Typical latency: <5 seconds

    Examples:
    - "Find my medical tests from 2025"
    - "Search emails about flights"
    - "Translate this text to Ukrainian"
    """

    INSTRUCT = "instruct"
    """
    Assign long-running task (asynchronous with callback).

    Semantics:
    - Sender receives acknowledgment immediately
    - Recipient executes in background (Cloud Tasks)
    - Recipient sends INFORM when complete
    - Typical latency: 30s - 5min

    Examples:
    - "Index all Gmail emails from 2020"
    - "Consolidate last 100 messages"
    - "Generate monthly report"
    """

    INFORM = "inform"
    """
    Notify without expecting response (fire-and-forget).

    Semantics:
    - No response expected
    - Recipient may process or ignore
    - Unidirectional communication

    Examples:
    - "Indexing completed, 8432 emails"
    - "User preferences updated"
    - "Circuit breaker opened for WebSearchAgent"
    """

    COLLABORATE = "collaborate"
    """
    Work together on complex task (bidirectional).

    Semantics:
    - Multiple message exchanges
    - Both agents contribute to solution
    - Coordinated execution

    Examples:
    - "Help classify these 50 emails" (batch processing)
    - "Review and improve this draft" (iterative refinement)
    - "Validate this extraction" (quality control)
    """
```

### 3.3 Semantic Guarantees

| Verb            | Execution     | Response | Callback     | Timeout  | Use When             |
| --------------- | ------------- | -------- | ------------ | -------- | -------------------- |
| **ASK**         | Sync          | Required | No           | 5-30s    | Need answer now      |
| **INSTRUCT**    | Async         | Ack only | Yes (INFORM) | 5min     | Long task            |
| **INFORM**      | Fire-forget   | None     | No           | N/A      | One-way notification |
| **COLLABORATE** | Bidirectional | Multiple | Contextual   | Variable | Complex workflow     |

---

## 4. Architecture: Hexagonal Design

### 4.1 Layer Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      DOMAIN LAYER                           │
│  (LLM-agnostic, Provider-agnostic)                         │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│  │ AgentMessage │    │ AgentVerb    │    │ToolDefinition│ │
│  │              │    │              │    │              │ │
│  │ - verb       │    │ - ASK        │    │ - name       │ │
│  │ - sender     │    │ - INSTRUCT   │    │ - parameters │ │
│  │ - recipient  │    │ - INFORM     │    │ - description│ │
│  │ - payload    │    │ - COLLABORATE│    │              │ │
│  └──────────────┘    └──────────────┘    └──────────────┘ │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐  │
│  │          MessageContext (extended)                   │  │
│  │                                                      │  │
│  │  - user_id: str                                     │  │
│  │  - text: str                                        │  │
│  │  - source_type: MessageSource (USER|AGENT|SYSTEM)  │  │
│  │  - source_metadata: Dict (sender_agent, task_id)   │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ Uses
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                       PORT LAYER                            │
│  (Interfaces)                                               │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  LLMService (Port)                                   │  │
│  │                                                       │  │
│  │  + generate_with_tools(                              │  │
│  │      prompt: str,                                    │  │
│  │      tools: List[ToolDefinition],  ← Abstract        │  │
│  │      context: Dict                                   │  │
│  │    ) -> LLMResponse                                  │  │
│  │                                                       │  │
│  │  LLMResponse:                                        │  │
│  │    - text: Optional[str]                             │  │
│  │    - tool_calls: List[ToolCall]  ← Abstract          │  │
│  │    - metadata: Dict                                  │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  AgentCoordinator (Port)                             │  │
│  │                                                       │  │
│  │  + handle_tool_call(tool_call: ToolCall)             │  │
│  │  + deliver_agent_message(message: AgentMessage)      │  │
│  │  + route_verb(verb, from, to, payload)               │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ Implements
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                     ADAPTER LAYER                           │
│  (Provider-specific implementations)                        │
│                                                             │
│  ┌────────────────────┐      ┌────────────────────┐       │
│  │ GeminiAdapter      │      │ ClaudeAdapter      │       │
│  │                    │      │                    │       │
│  │ implements         │      │ implements         │       │
│  │ LLMService         │      │ LLMService         │       │
│  │                    │      │                    │       │
│  │ Converts:          │      │ Converts:          │       │
│  │ ToolDefinition     │      │ ToolDefinition     │       │
│  │   → Gemini format  │      │   → Claude format  │       │
│  │                    │      │                    │       │
│  │ ToolCall           │      │ ToolCall           │       │
│  │   ← Gemini response│      │   ← Claude response│       │
│  └────────────────────┘      └────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Domain Models (Provider-Agnostic)

```python
# src/domain/agent.py (extended)

@dataclass
class AgentMessage:
    """
    Universal agent-to-agent message.

    Platform-agnostic, LLM-agnostic, wire-format agnostic.
    """
    task_id: str
    verb: AgentVerb  # NEW: Replaces intent
    sender: str      # Agent ID (e.g., "smart_agent")
    recipient: str   # Agent ID or "broadcast"
    payload: Dict[str, Any]
    context: Dict[str, Any]  # user_id, session_id, account_id
    priority: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Execution tracking (for INSTRUCT)
    parent_task_id: Optional[str] = None  # If this is subtask
    callback_address: Optional[str] = None  # Where to send INFORM


# src/domain/llm_tools.py (new)

@dataclass
class ToolDefinition:
    """
    Platform-agnostic tool definition.

    Converted to provider-specific format by adapters:
    - Gemini: tools parameter
    - Claude: tools parameter (different schema)
    - OpenAI: functions parameter
    """
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema format

@dataclass
class ToolCall:
    """
    Platform-agnostic tool invocation request from LLM.

    Parsed from provider-specific response:
    - Gemini: function_call part
    - Claude: tool_use content block
    - OpenAI: function_call object
    """
    tool_name: str
    arguments: Dict[str, Any]
    call_id: Optional[str] = None  # Provider-specific call ID

@dataclass
class ToolResult:
    """
    Platform-agnostic tool execution result.

    Sent back to LLM in next turn.
    """
    tool_name: str
    call_id: Optional[str]
    result: Any
    error: Optional[str] = None


# src/domain/messaging.py (extended)

class MessageSource(str, Enum):
    """Source type for incoming messages."""
    USER = "user"      # From user via platform (Slack/Telegram)
    AGENT = "agent"    # From another agent (async callback)
    SYSTEM = "system"  # From system (cron, webhook, internal event)

@dataclass
class MessageContext:
    """
    Platform-agnostic message context.

    Extended to distinguish user vs agent messages.
    """
    user_id: str
    text: str
    session_id: str
    thread_id: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)

    # NEW: Source identification
    source_type: MessageSource = MessageSource.USER
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    # For AGENT source:
    #   - sender_agent: str (who sent this)
    #   - task_id: str (what task this relates to)
    #   - verb: AgentVerb (INFORM usually)
```

### 4.3 Port Interfaces

```python
# src/ports/llm_service.py (extended)

class LLMService(ABC):
    """
    LLM provider interface with tool support.

    Implementations:
    - GeminiAdapter (Gemini API)
    - ClaudeAdapter (Anthropic API)
    - OpenAIAdapter (OpenAI API, future)
    """

    @abstractmethod
    async def generate_with_tools(
        self,
        prompt: str,
        tools: List[ToolDefinition],  # Provider-agnostic
        context: Dict[str, Any],
        model: Optional[str] = None
    ) -> LLMResponse:
        """
        Generate response with tool calling support.

        Args:
            prompt: System + user prompt (assembled)
            tools: Available tools (abstract format)
            context: Request context (trace_id, etc.)
            model: Override model (optional)

        Returns:
            LLMResponse with text and/or tool_calls
        """
        pass

@dataclass
class LLMResponse:
    """
    Provider-agnostic LLM response.
    """
    text: Optional[str]  # Generated text (if any)
    tool_calls: List[ToolCall]  # Tools LLM wants to call
    finish_reason: str  # "stop", "tool_calls", "length", etc.
    metadata: Dict[str, Any]  # tokens_used, latency_ms, model_used


# src/infrastructure/agent_coordinator.py (extended)

class AgentCoordinator:
    """
    Central routing hub for agent communication.

    Responsibilities:
    - Route AgentMessages between agents
    - Handle tool calls from LLM
    - Deliver async callbacks (INFORM)
    - Manage agent inbox (persistence)
    """

    async def route_verb(
        self,
        verb: AgentVerb,
        from_agent: str,
        to_agent: str,
        payload: Dict[str, Any],
        context: Dict[str, Any]
    ) -> AgentResponse:
        """
        Route message based on verb semantics.

        ASK: Synchronous execution, await response
        INSTRUCT: Enqueue to Cloud Tasks, return ack
        INFORM: Fire-and-forget delivery
        COLLABORATE: Initiate multi-turn exchange
        """
        message = AgentMessage(
            task_id=str(uuid4()),
            verb=verb,
            sender=from_agent,
            recipient=to_agent,
            payload=payload,
            context=context
        )

        if verb == AgentVerb.ASK:
            return await self._execute_sync(message)
        elif verb == AgentVerb.INSTRUCT:
            return await self._execute_async(message)
        elif verb == AgentVerb.INFORM:
            return await self._deliver_inform(message)
        elif verb == AgentVerb.COLLABORATE:
            return await self._initiate_collaboration(message)

    async def handle_tool_call(
        self,
        calling_agent_id: str,
        tool_call: ToolCall
    ) -> ToolResult:
        """
        Handle tool invocation from LLM.

        Currently supported tools:
        - delegate_to_agent: Inter-agent communication
        - (future: external APIs, database queries)
        """
        if tool_call.tool_name == "delegate_to_agent":
            args = tool_call.arguments

            response = await self.route_verb(
                verb=AgentVerb[args["verb"]],
                from_agent=calling_agent_id,
                to_agent=args["agent"],
                payload={
                    "task": args["task"],
                    "context": args.get("context", {})
                },
                context=args.get("context", {})
            )

            return ToolResult(
                tool_name=tool_call.tool_name,
                call_id=tool_call.call_id,
                result=response.result
            )

    async def deliver_agent_message(
        self,
        message: AgentMessage
    ) -> None:
        """
        Deliver async message to agent inbox.

        Used for INFORM callbacks when target agent is not active.
        Persists to Firestore, agent checks inbox on next activation.
        """
        await self.inbox_repo.save_message(
            agent_id=message.recipient,
            message=message
        )
```

---

## 5. Tool Definition: delegate_to_agent

### 5.1 Tool Schema (Abstract)

```python
DELEGATE_TOOL = ToolDefinition(
    name="delegate_to_agent",
    description="""
Delegate task to specialist agent.

Use this tool to:
- ASK another agent for information (synchronous, wait for answer)
- INSTRUCT another agent to perform long task (async, will notify you)
- INFORM another agent about event (fire-and-forget, no response)
- COLLABORATE with another agent on complex task (iterative exchange)

Available agents:
- memory_search: Search user's knowledge base (facts, principles)
- web_search: Search web for current information
- gmail_agent: Index and search Gmail emails
- consolidation: Extract facts from conversation history

Examples:
- ASK memory_search "find user's medical tests from 2025"
- INSTRUCT gmail_agent "index all emails from 2020"
- INFORM smart_agent "indexing completed, 8432 emails processed"
- COLLABORATE classification_agent "help classify these 50 emails"
""",
    parameters={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "enum": [
                    "memory_search",
                    "web_search",
                    "gmail_agent",
                    "consolidation",
                    "smart_agent"
                ],
                "description": "Target specialist agent"
            },
            "verb": {
                "type": "string",
                "enum": ["ASK", "INSTRUCT", "INFORM", "COLLABORATE"],
                "description": """
Communication verb:
- ASK: Need answer now (sync, <5s)
- INSTRUCT: Long task, notify when done (async, 30s-5min)
- INFORM: One-way notification (no response)
- COLLABORATE: Work together (multiple exchanges)
"""
            },
            "task": {
                "type": "string",
                "description": "Task description in natural language"
            },
            "context": {
                "type": "object",
                "description": """
Optional structured context for task execution.

Examples:
- {"action": "index_all", "date_from": "2020/01/01"}
- {"search_query": "medical tests", "limit": 10}
- {"batch_size": 50, "category": "travel"}

If not provided, agent will parse task from natural language.
""",
                "properties": {
                    "action": {"type": "string"},
                    "params": {"type": "object"}
                }
            }
        },
        "required": ["agent", "verb", "task"]
    }
)
```

### 5.2 Prompt Integration (SmartAgent)

```groovy
## Available Tools

You have access to specialist agents via the `delegate_to_agent` tool.

### When to Use

**ASK** - when you need information NOW:
- User asks question that requires search
- Need to verify information before answering
- Combine results from multiple sources
Example: User asks "find my medical tests" → ASK memory_search

**INSTRUCT** - when task takes >5 seconds:
- Long-running operations (indexing, processing)
- Background tasks user doesn't need to wait for
- Proactive operations (consolidation, cleanup)
Example: User says "index Gmail" → INSTRUCT gmail_agent → Tell user "Started, will notify"

**INFORM** - when notifying without expecting response:
- Report task completion to user
- Update other agents about state changes
- Log important events
Example: After indexing done → INFORM user "Done, 8432 emails"

**COLLABORATE** - when you need help with complex task:
- Batch processing (split work between agents)
- Iterative refinement (review and improve)
- Quality validation (second opinion)
Example: 200 emails to classify → COLLABORATE classification_agent

### Important Rules

1. **Check message source** before responding:
   - source_type=USER → answer user's question
   - source_type=AGENT → process agent notification

2. **For AGENT messages**, check source_metadata:
   - If sender_agent matches your INSTRUCT → inform user about completion
   - If unsolicited → process or ignore based on content

3. **Choose correct verb**:
   - Need answer to continue → ASK
   - Task >5s → INSTRUCT
   - Just notifying → INFORM
   - Need back-and-forth → COLLABORATE

### Current Message

Source: {{source_type}}
{{#if source_metadata.sender_agent}}
From agent: {{source_metadata.sender_agent}}
Verb: {{source_metadata.verb}}
Task: {{source_metadata.task_id}}
{{/if}}
```

---

## 6. Message Flow Examples

### 6.1 Example 1: ASK (Sync Search)

```
┌─────────┐
│  USER   │ "find my medical tests from 2025"
└────┬────┘
     │
     ▼
┌──────────────────┐
│  SmartAgent      │ LLM generates tool call:
│                  │ delegate_to_agent(
│                  │   agent="memory_search",
│                  │   verb="ASK",
│                  │   task="find medical tests from 2025"
│                  │ )
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  Coordinator     │ route_verb(ASK) → execute_sync()
│                  │ Creates AgentMessage(verb=ASK, recipient="memory_search")
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│ MemorySearchAgent│ execute() → search facts
│                  │ Returns: [7 medical test facts]
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  Coordinator     │ Returns ToolResult(result=[...])
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  SmartAgent      │ LLM receives tool result
│                  │ Generates text response
│                  │ "Found 7 records with medical tests..."
└────┬─────────────┘
     │
     ▼
┌─────────┐
│  USER   │ Receives formatted answer
└─────────┘

Total latency: ~3 seconds (sync)
```

### 6.2 Example 2: INSTRUCT (Async Indexing)

```
┌─────────┐
│  USER   │ "index my Gmail"
└────┬────┘
     │
     ▼
┌──────────────────┐
│  SmartAgent      │ LLM generates tool call:
│                  │ delegate_to_agent(
│                  │   agent="gmail_agent",
│                  │   verb="INSTRUCT",
│                  │   task="index all emails from 2020",
│                  │   context={"action": "index_all", "date_from": "2020/01/01"}
│                  │ )
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  Coordinator     │ route_verb(INSTRUCT) → execute_async()
│                  │ 1. Creates AgentMessage(verb=INSTRUCT)
│                  │ 2. Sets callback_address="smart_agent"
│                  │ 3. Enqueues to Cloud Tasks
│                  │ 4. Returns ack: {"status": "started", "task_id": "xyz"}
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  SmartAgent      │ LLM receives ToolResult(result={"status": "started"})
│                  │ Generates text:
│                  │ "✅ Started Gmail indexing. Will take 1-2 minutes, will notify when done."
└────┬─────────────┘
     │
     ▼
┌─────────┐
│  USER   │ Receives immediate response
└─────────┘

Latency: ~1 second (async acknowledgment)

        [90 seconds later, Cloud Tasks worker]

┌──────────────────┐
│  GmailAgent      │ execute() in background:
│                  │ 1. Fetch emails from Gmail API (10s)
│                  │ 2. Classify via LLM (30s)
│                  │ 3. Save to Firestore (20s)
│                  │ 4. Task complete! indexed_count=8432
│                  │
│                  │ Send callback:
│                  │ delegate_to_agent(
│                  │   agent="smart_agent",  ← callback_address
│                  │   verb="INFORM",
│                  │   task="indexing completed",
│                  │   context={"indexed_count": 8432, "task_id": "xyz"}
│                  │ )
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  Coordinator     │ route_verb(INFORM) → deliver_inform()
│                  │ SmartAgent may be sleeping → save to inbox
│                  │ inbox.save_message(agent_id="smart_agent:user_U123", message=...)
└────┬─────────────┘

        [Next user message, SmartAgent wakes up]

┌─────────┐
│  USER   │ "hey"
└────┬────┘
     │
     ▼
┌──────────────────┐
│  SmartAgent      │ On activation:
│                  │ 1. Check inbox: inbox.get_unread_messages()
│                  │ 2. Found INFORM from gmail_agent
│                  │ 3. Process: MessageContext(
│                  │      source_type=AGENT,
│                  │      source_metadata={"sender_agent": "gmail_agent", "verb": "INFORM"}
│                  │    )
│                  │ 4. LLM understands: "This is callback from my INSTRUCT task"
│                  │ 5. Generate notification for user
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│ ResponseChannel  │ send_message("✅ Gmail indexing complete! 8,432 emails.")
└────┬─────────────┘
     │
     ▼
┌─────────┐
│  USER   │ Receives notification
└─────────┘
```

### 6.3 Example 3: COLLABORATE (Batch Processing)

```
┌──────────────────┐
│  GmailAgent      │ Has 200 emails to classify
│                  │ Too many for one LLM call (context limit)
│                  │
│                  │ Split into 4 batches of 50
│                  │
│                  │ Parallel delegation:
│                  │ delegate_to_agent(
│                  │   agent="classification_agent_1",
│                  │   verb="COLLABORATE",
│                  │   task="classify emails batch 1-50",
│                  │   context={"batch": emails[0:50]}
│                  │ )
│                  │ delegate_to_agent(
│                  │   agent="classification_agent_2",
│                  │   verb="COLLABORATE",
│                  │   task="classify emails batch 51-100",
│                  │   context={"batch": emails[50:100]}
│                  │ )
│                  │ ... (parallel execution)
└────┬─────────────┘
     │
     ▼
┌──────────────────┐
│  Coordinator     │ route_verb(COLLABORATE) → execute_sync() x4 in parallel
│                  │ Uses asyncio.gather() for concurrent execution
└────┬─────────────┘
     │
     ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ClassificationAgent│  │ClassificationAgent│  │ClassificationAgent│
│       1           │  │        2          │  │       3          │
│ Classify 50 emails│  │ Classify 50 emails│  │ Classify 50 emails│
└────┬──────────────┘  └────┬──────────────┘  └────┬──────────────┘
     │                      │                       │
     └──────────────────────┴───────────────────────┘
                            │
                            ▼
┌──────────────────┐
│  GmailAgent      │ Aggregates results from 4 agents
│                  │ Total: 200 classified emails
│                  │ Continues with indexing
└──────────────────┘
```

---

## 7. Implementation Details

### 7.1 Provider Adapters (Hexagonal Architecture)

#### Gemini Adapter

```python
# src/adapters/gemini_adapter.py

class GeminiAdapter(LLMService):
    """
    Gemini-specific implementation of LLMService.

    Converts between abstract ToolDefinition and Gemini's tool format.
    """

    async def generate_with_tools(
        self,
        prompt: str,
        tools: List[ToolDefinition],
        context: Dict[str, Any],
        model: Optional[str] = None
    ) -> LLMResponse:
        # Convert abstract tools → Gemini format
        gemini_tools = [
            {
                "function_declarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters
                    }
                    for tool in tools
                ]
            }
        ]

        # Generate with Gemini
        response = await self.client.generate_content_async(
            contents=prompt,
            tools=gemini_tools,
            generation_config=self._get_generation_config(model)
        )

        # Convert Gemini response → abstract format
        return self._convert_response(response)

    def _convert_response(self, gemini_response) -> LLMResponse:
        """Convert Gemini response to abstract LLMResponse."""
        tool_calls = []
        text = None

        for part in gemini_response.parts:
            if hasattr(part, 'function_call'):
                # Gemini function call
                tool_calls.append(ToolCall(
                    tool_name=part.function_call.name,
                    arguments=dict(part.function_call.args),
                    call_id=None  # Gemini doesn't use call IDs
                ))
            elif hasattr(part, 'text'):
                text = part.text

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason="stop" if not tool_calls else "tool_calls",
            metadata={
                "model": gemini_response.model,
                "tokens_used": self._count_tokens(gemini_response)
            }
        )
```

#### Claude Adapter

```python
# src/adapters/claude_adapter.py

class ClaudeAdapter(LLMService):
    """
    Claude-specific implementation of LLMService.

    Converts between abstract ToolDefinition and Claude's tool format.
    """

    async def generate_with_tools(
        self,
        prompt: str,
        tools: List[ToolDefinition],
        context: Dict[str, Any],
        model: Optional[str] = None
    ) -> LLMResponse:
        # Convert abstract tools → Claude format
        claude_tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters  # Claude uses input_schema
            }
            for tool in tools
        ]

        # Generate with Claude
        response = await self.client.messages.create(
            model=model or "claude-3-opus-20240229",
            messages=[{"role": "user", "content": prompt}],
            tools=claude_tools,
            max_tokens=4096
        )

        # Convert Claude response → abstract format
        return self._convert_response(response)

    def _convert_response(self, claude_response) -> LLMResponse:
        """Convert Claude response to abstract LLMResponse."""
        tool_calls = []
        text_parts = []

        for content_block in claude_response.content:
            if content_block.type == "tool_use":
                # Claude tool use
                tool_calls.append(ToolCall(
                    tool_name=content_block.name,
                    arguments=content_block.input,
                    call_id=content_block.id  # Claude uses call IDs
                ))
            elif content_block.type == "text":
                text_parts.append(content_block.text)

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            finish_reason=claude_response.stop_reason,
            metadata={
                "model": claude_response.model,
                "tokens_used": {
                    "input": claude_response.usage.input_tokens,
                    "output": claude_response.usage.output_tokens
                }
            }
        )
```

### 7.2 Agent Inbox (Persistence)

```python
# src/adapters/firestore_agent_inbox.py

class FirestoreAgentInbox:
    """
    Persistent inbox for async agent messages.

    Collection: agent_inbox/{agent_id}/messages/{message_id}

    Use case: Store INFORM messages when target agent is not active.
    """

    async def save_message(
        self,
        agent_id: str,  # e.g., "smart_agent:user_U123"
        message: AgentMessage
    ) -> None:
        """Save message to agent's inbox."""
        doc_ref = self.db.collection("agent_inbox") \
            .document(agent_id) \
            .collection("messages") \
            .document(message.task_id)

        await doc_ref.set({
            "message_id": message.task_id,
            "sender": message.sender,
            "verb": message.verb.value,
            "payload": message.payload,
            "context": message.context,
            "received_at": firestore.SERVER_TIMESTAMP,
            "status": "unread",
            "priority": message.priority
        })

    async def get_unread_messages(
        self,
        agent_id: str,
        limit: int = 10
    ) -> List[AgentMessage]:
        """Retrieve unread messages from inbox."""
        query = self.db.collection("agent_inbox") \
            .document(agent_id) \
            .collection("messages") \
            .where("status", "==", "unread") \
            .order_by("received_at", direction=firestore.Query.ASCENDING) \
            .limit(limit)

        docs = await query.get()

        messages = []
        for doc in docs:
            data = doc.to_dict()
            messages.append(AgentMessage(
                task_id=data["message_id"],
                verb=AgentVerb(data["verb"]),
                sender=data["sender"],
                recipient=agent_id,
                payload=data["payload"],
                context=data["context"],
                priority=data.get("priority", 0)
            ))

        return messages

    async def mark_read(
        self,
        agent_id: str,
        message_id: str
    ) -> None:
        """Mark message as read."""
        doc_ref = self.db.collection("agent_inbox") \
            .document(agent_id) \
            .collection("messages") \
            .document(message_id)

        await doc_ref.update({"status": "read"})

    async def cleanup_old_messages(
        self,
        agent_id: str,
        older_than_hours: int = 24
    ) -> int:
        """Delete old read messages."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

        query = self.db.collection("agent_inbox") \
            .document(agent_id) \
            .collection("messages") \
            .where("status", "==", "read") \
            .where("received_at", "<", cutoff)

        docs = await query.get()

        for doc in docs:
            await doc.reference.delete()

        return len(docs)
```

### 7.3 ConversationHandler Integration

```python
# src/handlers/conversation_handler.py (extended)

class ConversationHandler:
    """
    Orchestrates user ↔ agent communication.

    Extended to handle both user messages and agent callbacks.
    """

    async def handle_message(
        self,
        message_context: MessageContext
    ) -> None:
        """
        Unified message handler for USER and AGENT sources.

        Routing:
        - source_type=USER → New user query
        - source_type=AGENT → Async callback from agent
        - source_type=SYSTEM → Internal event
        """

        # Check for pending agent messages first
        if message_context.source_type == MessageSource.USER:
            await self._check_agent_inbox(message_context.user_id)

        # Route to appropriate agent
        response = await self.smart_agent.execute(message_context)

        # Send response to user
        if response.text:
            await self.response_channel.send_message(response.text)

    async def _check_agent_inbox(self, user_id: str) -> None:
        """
        Check agent inbox for pending notifications.

        Called when user sends new message to ensure agent sees
        any INFORM callbacks that arrived while agent was sleeping.
        """
        agent_id = f"smart_agent:user_{user_id}"

        # Get unread messages
        messages = await self.inbox.get_unread_messages(agent_id)

        if not messages:
            return

        # Process each INFORM message
        for msg in messages:
            # Convert to MessageContext
            context = MessageContext(
                user_id=user_id,
                text=msg.payload.get("task", ""),
                session_id=msg.context.get("session_id", ""),
                source_type=MessageSource.AGENT,
                source_metadata={
                    "sender_agent": msg.sender,
                    "verb": msg.verb.value,
                    "task_id": msg.task_id,
                    "payload": msg.payload
                }
            )

            # Let agent process the notification
            response = await self.smart_agent.execute(context)

            # If agent wants to inform user
            if response.text:
                await self.response_channel.send_message(response.text)

            # Mark as read
            await self.inbox.mark_read(agent_id, msg.task_id)
```

---

## 8. Open Questions & Grey Zones

### 8.1 Question 1: Inbox Check Timing

**Problem:** When should agent check inbox?

**Options:**

A. **On every user message** (current proposal)

- ✅ Pro: Simple, guaranteed delivery
- ❌ Con: Extra Firestore read on every request

B. **Periodic background job** (cron every 5 min)

- ✅ Pro: No per-request overhead
- ❌ Con: Delayed delivery (up to 5 min)

C. **Push notification** (Cloud Tasks → /worker endpoint)

- ✅ Pro: Immediate delivery
- ❌ Con: More complex, requires endpoint

**Recommendation:** Start with A (every message), optimize to C later.

---

### 8.2 Question 2: Multi-Turn COLLABORATE

**Problem:** COLLABORATE may need multiple back-and-forth exchanges. How to track state?

**Options:**

A. **Stateless** (each exchange is independent)

- Agent stores conversation in payload
- No shared state needed

B. **Session-based** (create temporary collaboration session)

```python
collaboration_id = coordinator.start_collaboration(
    agent_1="gmail_agent",
    agent_2="classification_agent"
)
# Messages exchange via collaboration_id
coordinator.end_collaboration(collaboration_id)
```

C. **Thread-based** (use existing session_id)

- COLLABORATE messages within same session
- Session store tracks multi-agent conversations

**Recommendation:** Start with A (stateless), add B if needed.

---

### 8.3 Question 3: Error Handling in INSTRUCT

**Problem:** What if background task fails? How to notify user?

**Options:**

A. **Always INFORM sender** (even on failure)

```python
# On failure
delegate_to_agent(
    agent="smart_agent",
    verb="INFORM",
    task="indexing failed",
    context={"error": "Gmail API rate limit exceeded"}
)
```

B. **Store failure in inbox + user notification**

- Save error to inbox
- System sends direct notification to user

C. **Retry + eventual notification**

- Retry 3 times with exponential backoff
- Only notify if all retries fail

**Recommendation:** A + C (retry then INFORM on final failure)

---

### 8.4 Question 4: Tool Call Parsing

**Problem:** What if LLM generates malformed tool call?

**Example:**

```json
{
  "agent": "nonexistent_agent", // Wrong agent name
  "verb": "SEARCH", // Wrong verb (not in enum)
  "task": null // Missing required field
}
```

**Handling:**

1. **Validation layer** in Coordinator

   ```python
   try:
       agent_enum = AgentEnum[args["agent"]]
       verb_enum = AgentVerb[args["verb"]]
   except KeyError:
       return ToolResult(
           error="Invalid agent or verb. Available: ..."
       )
   ```

2. **LLM retry** with error context
   - Return error as tool_result
   - LLM sees error, retries with correct format

3. **Fallback** to natural language
   - If structured call fails, ask agent to describe intent
   - Human operator reviews

**Recommendation:** 1 + 2 (validate + LLM retry)

---

### 8.5 Question 5: Context Parsing (Structured vs NLP)

**Problem:** Should agents expect structured context or parse natural language?

**Hybrid approach (recommended):**

```python
# SmartAgent can provide structured context
delegate_to_agent(
    agent="gmail_agent",
    verb="INSTRUCT",
    task="index emails",
    context={
        "action": "index_all",       # For code path
        "date_from": "2020/01/01",
        "task_description": "index all emails from 2020"  # For LLM fallback
    }
)

# GmailAgent tries structured first
async def execute(self, message: AgentMessage):
    context = message.payload.get("context", {})

    # Try structured path
    if "action" in context:
        action = context["action"]
        if action == "index_all":
            return await self._index_all(
                date_from=context.get("date_from"),
                date_to=context.get("date_to")
            )

    # Fallback: parse via LLM
    task_desc = context.get("task_description") or message.payload["task"]
    parsed = await self.llm.parse_task(task_desc)
    return await self._execute_parsed(parsed)
```

**Benefits:**

- Fast path: Structured context → direct execution
- Flexible path: Natural language → LLM parsing
- No breaking changes if schema evolves

---

### 8.6 Question 6: Security & Authorization

**Problem:** Should agents verify they're authorized to communicate?

**Concerns:**

1. **Impersonation:** Can malicious agent send INFORM pretending to be gmail_agent?
2. **Privilege escalation:** Can low-privilege agent invoke high-privilege operations?
3. **Data access:** Should agents enforce account_id boundaries?

**Solutions:**

A. **Trust model** (current)

- Agents trust Coordinator
- Coordinator verifies sender identity
- No direct agent-to-agent communication

B. **Capability-based** (future)

- Each agent has capabilities list
- Coordinator checks if sender has capability to invoke recipient

C. **Cryptographic** (overkill for MVP)

- Sign messages with agent keys
- Verify signatures on receipt

**Recommendation:** A for MVP, evaluate B for Phase 2

---

### 8.7 Question 7: Progress Reporting

**Problem:** For long INSTRUCT tasks, how to report progress?

**Options:**

A. **Multiple INFORM messages**

```python
# GmailAgent during indexing
await coordinator.route_verb(
    verb=AgentVerb.INFORM,
    from_agent="gmail_agent",
    to_agent="smart_agent",
    payload={"progress": 0.3, "stage": "classifying"}
)
```

B. **Streaming updates** (WebSocket)

- Open WebSocket channel
- Stream progress events

C. **Polling** (status endpoint)

- Agent stores progress in Firestore
- SmartAgent polls task_id status

**Recommendation:** A (multiple INFORM) for MVP

**Implementation:**

```python
# GmailAgent
async def execute(self, message: AgentMessage):
    total_emails = 10000

    for i, batch in enumerate(batches):
        # Process batch
        await self._classify_batch(batch)

        # Report progress every 10%
        if (i % (total_emails // 10)) == 0:
            await self.coordinator.route_verb(
                verb=AgentVerb.INFORM,
                from_agent=self.agent_id,
                to_agent=message.sender,  # callback to sender
                payload={
                    "type": "progress",
                    "progress": i / total_emails,
                    "stage": "classifying",
                    "processed": i
                }
            )
```

---

## 9. Migration Path (ACP v1 → v2)

### 9.1 Backward Compatibility

**Goal:** Existing agents work without changes.

**Strategy:**

1. **Keep AgentIntent as alias** to AgentVerb

   ```python
   # Old code still works
   AgentIntent.QUERY → maps to → AgentVerb.ASK
   AgentIntent.DELEGATE → maps to → AgentVerb.INSTRUCT
   ```

2. **Coordinator auto-detects** old vs new format

   ```python
   if message.intent:  # Old format
       verb = self._intent_to_verb(message.intent)
   else:  # New format
       verb = message.verb
   ```

3. **Gradual migration**
   - Phase 1: Add new verbs, keep old working
   - Phase 2: Migrate agents one-by-one
   - Phase 3: Deprecate old AgentIntent

### 9.2 Migration Checklist

**Per Agent:**

- [ ] Update to use `verb` instead of `intent`
- [ ] Add `delegate_to_agent` tool to LLM
- [ ] Update prompt with verb guidelines
- [ ] Implement inbox checking (if receives INFORM)
- [ ] Add structured context support (optional)
- [ ] Test ASK, INSTRUCT, INFORM flows
- [ ] Update Building Block documentation

**Infrastructure:**

- [ ] Implement LLMPort with tool support
- [ ] Create GeminiAdapter + ClaudeAdapter
- [ ] Implement FirestoreAgentInbox
- [ ] Extend AgentCoordinator with verb routing
- [ ] Add MessageSource to MessageContext
- [ ] Update ConversationHandler for agent messages

---

## 10. Testing Strategy

### 10.1 Unit Tests

**Domain Models:**

```python
def test_agent_message_with_verb():
    msg = AgentMessage(
        task_id="test",
        verb=AgentVerb.ASK,
        sender="smart_agent",
        recipient="memory_search",
        payload={"task": "find tests"},
        context={}
    )
    assert msg.verb == AgentVerb.ASK

def test_tool_definition_serialization():
    tool = ToolDefinition(
        name="delegate_to_agent",
        description="Delegate task",
        parameters={"type": "object", "properties": {...}}
    )
    json_str = json.dumps(tool.__dict__)
    assert "delegate_to_agent" in json_str
```

**LLM Adapters:**

```python
@pytest.mark.asyncio
async def test_gemini_adapter_converts_tools():
    adapter = GeminiAdapter(api_key="test")

    tools = [ToolDefinition(name="test_tool", ...)]

    # Mock Gemini response with function call
    mock_response = Mock()
    mock_response.parts = [
        Mock(function_call=Mock(name="test_tool", args={"x": 1}))
    ]

    llm_response = adapter._convert_response(mock_response)

    assert len(llm_response.tool_calls) == 1
    assert llm_response.tool_calls[0].tool_name == "test_tool"

@pytest.mark.asyncio
async def test_claude_adapter_converts_tools():
    # Similar test for Claude format
    pass
```

### 10.2 Integration Tests

**Verb Routing:**

```python
@pytest.mark.asyncio
async def test_ask_verb_executes_sync():
    coordinator = AgentCoordinator()
    memory_agent = MockMemoryAgent()
    coordinator.register_agent(memory_agent)

    response = await coordinator.route_verb(
        verb=AgentVerb.ASK,
        from_agent="smart_agent",
        to_agent="memory_search",
        payload={"task": "find tests"},
        context={"user_id": "U123"}
    )

    assert response.status == AgentStatus.SUCCESS
    assert memory_agent.execute_called

@pytest.mark.asyncio
async def test_instruct_verb_enqueues_async():
    coordinator = AgentCoordinator()
    task_queue = MockTaskQueue()

    response = await coordinator.route_verb(
        verb=AgentVerb.INSTRUCT,
        from_agent="smart_agent",
        to_agent="gmail_agent",
        payload={"task": "index emails"},
        context={}
    )

    assert response.result["status"] == "started"
    assert task_queue.enqueue_called
```

**Inbox Persistence:**

```python
@pytest.mark.asyncio
async def test_inform_saves_to_inbox():
    inbox = FirestoreAgentInbox(firestore_client)

    message = AgentMessage(
        task_id="test",
        verb=AgentVerb.INFORM,
        sender="gmail_agent",
        recipient="smart_agent:user_U123",
        payload={"task": "indexing done"},
        context={}
    )

    await inbox.save_message("smart_agent:user_U123", message)

    messages = await inbox.get_unread_messages("smart_agent:user_U123")

    assert len(messages) == 1
    assert messages[0].verb == AgentVerb.INFORM
```

### 10.3 End-to-End Tests

**Gmail Indexing Flow:**

```python
@pytest.mark.e2e
async def test_gmail_indexing_complete_flow():
    """
    Test complete async flow:
    1. User requests indexing
    2. SmartAgent delegates with INSTRUCT
    3. GmailAgent indexes in background
    4. GmailAgent sends INFORM callback
    5. SmartAgent notifies user
    """
    # Setup
    user_context = MessageContext(
        user_id="U123",
        text="index my Gmail",
        session_id="sess456",
        source_type=MessageSource.USER
    )

    # Step 1: User message
    handler = ConversationHandler(...)
    await handler.handle_message(user_context)

    # Verify: SmartAgent responded immediately
    assert mock_response_channel.last_message.startswith("Started indexing")

    # Step 2: Wait for background task
    await asyncio.sleep(5)  # Simulate indexing

    # Step 3: Trigger inbox check (next user message)
    follow_up_context = MessageContext(
        user_id="U123",
        text="hey",
        session_id="sess456",
        source_type=MessageSource.USER
    )

    await handler.handle_message(follow_up_context)

    # Verify: SmartAgent sent completion notification
    messages = mock_response_channel.get_all_messages()
    assert any("Indexing complete" in msg for msg in messages)
```

---

## 11. Performance Considerations

### 11.1 Latency Targets

| Operation      | Target | Measurement                           |
| -------------- | ------ | ------------------------------------- |
| ASK (sync)     | <5s    | Time from tool_call to tool_result    |
| INSTRUCT (ack) | <1s    | Time from tool_call to acknowledgment |
| INFORM (save)  | <500ms | Time to persist to inbox              |
| Inbox check    | <300ms | Time to query unread messages         |

### 11.2 Optimization Strategies

**Parallel Execution:**

```python
# Coordinator executes multiple ASKs in parallel
responses = await asyncio.gather(
    self.route_verb(verb=AgentVerb.ASK, to_agent="memory_search", ...),
    self.route_verb(verb=AgentVerb.ASK, to_agent="web_search", ...),
    return_exceptions=True
)
```

**Inbox Batching:**

```python
# Check inbox once per user session, not per message
session_started = False
if not session_started:
    await self._check_agent_inbox(user_id)
    session_started = True
```

**Caching:**

```python
# Cache tool definitions (don't recreate on every call)
@lru_cache(maxsize=10)
def get_delegate_tool() -> ToolDefinition:
    return ToolDefinition(...)
```

---

## 12. Security Considerations

### 12.1 Threat Model

**Threats:**

1. **Malicious tool calls:** LLM generates tool call to wrong agent
2. **Data leakage:** Agent A reads Agent B's private data
3. **DoS:** Infinite INSTRUCT loop
4. **Impersonation:** Fake INFORM from non-existent agent

**Mitigations:**

1. **Whitelist validation:**

   ```python
   ALLOWED_AGENTS = {"memory_search", "web_search", "gmail_agent"}
   if tool_call.arguments["agent"] not in ALLOWED_AGENTS:
       raise ValueError("Unknown agent")
   ```

2. **Account isolation:**

   ```python
   # All messages carry account_id
   # Agents validate they have access to account
   if message.context["account_id"] != self.account_id:
       return AgentResponse.failure("Unauthorized")
   ```

3. **Rate limiting:**

   ```python
   # Max 10 INSTRUCT per user per hour
   if self.instruct_count[user_id] > 10:
       return AgentResponse.failure("Rate limit exceeded")
   ```

4. **Sender verification:**
   ```python
   # Coordinator verifies sender exists
   if message.sender not in self.agents:
       logger.warning(f"Message from unknown agent: {message.sender}")
       # Drop message
   ```

---

## 13. Documentation Updates Required

### 13.1 Building Blocks

**Multi-Agent System:**

- [ ] Add ACP v2 verb semantics
- [ ] Update architecture diagram
- [ ] Add tool-based delegation section
- [ ] Document inbox pattern

**Hybrid Router:**

- [ ] Update triage flow to use ASK verb
- [ ] Document when to use which verb

### 13.2 Concepts

**New Guide:** `docs/08_concepts/agent_communication_guide.md`

- Verb selection guide
- Tool calling examples
- Error handling patterns
- Best practices

### 13.3 Code References

**Update:**

- `src/domain/agent.py` docstrings
- `src/ports/llm_service.py` interface docs
- `src/infrastructure/agent_coordinator.py` routing logic
- `src/handlers/conversation_handler.py` message handling

---

## 14. Success Metrics

### 14.1 Adoption Metrics

- **Week 1:** SmartAgent + MemorySearchAgent use ASK verb
- **Week 2:** GmailAgent implements INSTRUCT pattern
- **Week 3:** All agents migrated from AgentIntent to AgentVerb
- **Month 1:** 80% of agent communications use verbs

### 14.2 Quality Metrics

- **Tool call accuracy:** >95% (valid agent + verb combinations)
- **Inbox delivery rate:** 100% (no lost INFORM messages)
- **Latency compliance:** 90% of ASK calls <5s, INSTRUCT acks <1s

### 14.3 Developer Experience

- **Prompt complexity:** Verb guidelines fit in <50 lines
- **Code changes:** Minimal impact on existing agents (<20 lines per agent)
- **Test coverage:** >80% for new ACP v2 components

---

## 15. Alternatives Considered

### 15.1 Alternative 1: Event-Driven Architecture

**Approach:** Pub/Sub pattern with Cloud Pub/Sub

**Pros:**

- Scalable (millions of messages/sec)
- Decoupled (agents don't know about each other)

**Cons:**

- Complex infrastructure
- Hard to debug message flow
- Cost ($40/month vs $0 for Firestore inbox)

**Verdict:** ❌ Over-engineering for current scale

---

### 15.2 Alternative 2: Workflow Engine

**Approach:** Use Temporal or Prefect for orchestration

**Pros:**

- Built-in retries, timeouts, monitoring
- Visual workflow diagrams

**Cons:**

- Additional infrastructure
- LLM can't directly invoke workflows
- Vendor lock-in

**Verdict:** ❌ Not compatible with LLM tool calling

---

### 15.3 Alternative 3: Microservices with gRPC

**Approach:** Each agent as separate service, communicate via gRPC

**Pros:**

- True horizontal scaling
- Strong typing with Protobuf

**Cons:**

- Deployment complexity (10+ services)
- Cost (10x Cloud Run instances)
- LLM can't generate gRPC calls

**Verdict:** ❌ Not feasible with LLM-driven architecture

---

## 16. Implementation Plan

### Phase 1: Foundation (Week 1)

**Day 1-2: Domain Models**

- [ ] Add AgentVerb enum
- [ ] Add ToolDefinition / ToolCall / ToolResult
- [ ] Extend MessageContext with source_type
- [ ] Update AgentMessage with verb field
- [ ] Unit tests (15 tests)

**Day 3-4: Ports**

- [ ] Extend LLMService with generate_with_tools()
- [ ] Add LLMResponse dataclass
- [ ] Create FirestoreAgentInbox port
- [ ] Unit tests (10 tests)

**Day 5: Documentation**

- [ ] Update domain/ docstrings
- [ ] Update ports/ docstrings
- [ ] Create ACP v2 migration guide

### Phase 2: Adapters (Week 2)

**Day 1-2: Gemini Adapter**

- [ ] Implement generate_with_tools()
- [ ] Tool conversion (abstract → Gemini)
- [ ] Response parsing (Gemini → abstract)
- [ ] Unit tests (20 tests)

**Day 3-4: Claude Adapter**

- [ ] Implement generate_with_tools()
- [ ] Tool conversion (abstract → Claude)
- [ ] Response parsing (Claude → abstract)
- [ ] Unit tests (20 tests)

**Day 5: Inbox Implementation**

- [ ] FirestoreAgentInbox implementation
- [ ] save_message(), get_unread_messages()
- [ ] mark_read(), cleanup_old_messages()
- [ ] Integration tests (15 tests)

### Phase 3: Coordinator (Week 3)

**Day 1-2: Verb Routing**

- [ ] Implement route_verb()
- [ ] ASK → execute_sync()
- [ ] INSTRUCT → execute_async()
- [ ] INFORM → deliver_inform()
- [ ] Integration tests (25 tests)

**Day 3-4: Tool Handling**

- [ ] Implement handle_tool_call()
- [ ] delegate_to_agent logic
- [ ] Error handling + validation
- [ ] Integration tests (15 tests)

**Day 5: ConversationHandler**

- [ ] Extend handle_message() for agent sources
- [ ] Implement \_check_agent_inbox()
- [ ] Update message routing
- [ ] E2E tests (10 tests)

### Phase 4: Agent Migration (Week 4)

**Day 1: SmartAgent**

- [ ] Add delegate_to_agent tool
- [ ] Update prompt with verb guidelines
- [ ] Implement inbox checking
- [ ] E2E tests (5 tests)

**Day 2: MemorySearchAgent**

- [ ] Support ASK verb
- [ ] Update response format
- [ ] E2E tests (3 tests)

**Day 3: WebSearchAgent**

- [ ] Support ASK verb
- [ ] Update response format
- [ ] E2E tests (3 tests)

**Day 4-5: GmailAgent**

- [ ] Implement INSTRUCT pattern
- [ ] Background indexing via Cloud Tasks
- [ ] INFORM callback on completion
- [ ] E2E tests (8 tests)

### Phase 5: Documentation (Week 5)

**Day 1-2: Building Blocks**

- [ ] Update Multi-Agent System
- [ ] Update Hybrid Router
- [ ] Update all affected blocks

**Day 3: Concepts**

- [ ] Create Agent Communication Guide
- [ ] Add examples and best practices

**Day 4-5: Polish**

- [ ] Review all documentation
- [ ] Update IMPLEMENTATION_ROADMAP
- [ ] Create Session Context entry

**Total:** 5 weeks, ~120-150 hours

---

## 17. Risks & Mitigation

### Risk 1: LLM Tool Calling Reliability

**Risk:** LLM generates invalid tool calls (wrong format, missing fields)

**Impact:** High - breaks agent communication

**Likelihood:** Medium (10-20% of calls in testing)

**Mitigation:**

1. Strict JSON Schema validation
2. Clear examples in prompt
3. LLM retry with error feedback
4. Fallback to natural language parsing

### Risk 2: Inbox Message Loss

**Risk:** INFORM messages not delivered due to Firestore failure

**Impact:** High - user doesn't see completion notification

**Likelihood:** Low (<0.1% failure rate)

**Mitigation:**

1. Firestore transaction atomicity
2. Retry logic (3 attempts)
3. Dead letter queue for failed deliveries
4. Monitoring + alerts

### Risk 3: Inbox Growth

**Risk:** Inbox accumulates old messages, degrades performance

**Impact:** Medium - slow queries, storage cost

**Likelihood:** High (without cleanup)

**Mitigation:**

1. Automatic cleanup (delete read messages >24h old)
2. Pagination (limit 10 messages per check)
3. Firestore TTL policy (future)

### Risk 4: Provider API Changes

**Risk:** Gemini/Claude changes tool calling format

**Impact:** Medium - adapter breaks, need update

**Likelihood:** Low (stable APIs)

**Mitigation:**

1. Hexagonal architecture isolates changes
2. Version pinning (gemini-1.5-pro-002)
3. Adapter tests catch breaking changes
4. Fallback to previous API version

---

## 18. Future Enhancements

### Phase 2+ Features

1. **Streaming Progress** (INSTRUCT with real-time updates)
   - WebSocket channel for progress events
   - Client sees "Indexing... 30%... 60%... 100%"

2. **Multi-Agent Workflows** (COLLABORATE orchestration)
   - Define complex workflows (DAGs)
   - Parallel execution with synchronization
   - Error recovery and retry logic

3. **Agent Marketplace** (dynamic agent discovery)
   - Agents register capabilities
   - LLM queries marketplace: "Who can translate to Spanish?"
   - Dynamic tool list based on available agents

4. **Cross-User Collaboration** (shared agents)
   - Organization-level agents
   - Team knowledge bases
   - Shared task queues

5. **Agent Monitoring Dashboard**
   - Real-time agent status
   - Message flow visualization
   - Performance metrics

---

## 19. References

**Related RFCs:**

- [Gmail Email Indexing RFC](./GMAIL_EMAIL_INDEXING_RFC.md)
- [Multi-Tenant OAuth RFC](./MULTI_TENANT_OAUTH_RFC.md)

**Building Blocks:**

- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md)
- [Hybrid Router](../05_building_blocks/hybrid_router/README.md)

**External Resources:**

- [Gemini Function Calling Docs](https://ai.google.dev/gemini-api/docs/function-calling)
- [Claude Tool Use Docs](https://docs.anthropic.com/claude/docs/tool-use)
- [OpenAI Function Calling Docs](https://platform.openai.com/docs/guides/function-calling)

---

## Changelog

### 2026-02-11

- Initial RFC created
- Verb-based protocol designed (ASK, INSTRUCT, INFORM, COLLABORATE)
- Hexagonal architecture with LLMPort abstraction
- Tool-based delegation with delegate_to_agent
- Inbox pattern for async callbacks
- Provider-agnostic implementation (Gemini + Claude adapters)
- Comprehensive open questions documented
- Implementation plan outlined (5 weeks)

---

**Last Updated:** 2026-02-11  
**Status:** 🟡 Proposed (Awaiting Review)  
**Next Steps:** Review with stakeholders, address open questions, begin Phase 1 implementation
