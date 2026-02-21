# Multi-Agent System (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the Actor Model-based multi-agent architecture, agent coordination patterns, and resilience mechanisms.

### When to Read

- **For AI Agents:** Before changing agent coordination, routing logic, or ACP semantics.
- **For Developers:** When adding new agents, modifying message flow, or tuning circuit breakers/retries.

### When to Update

This document MUST be updated when:

- [ ] `AgentCoordinator` routing logic changes.
- [ ] `AgentMessage` or `AgentResponse` schemas are modified.
- [ ] New core or infrastructure agents are introduced.
- [ ] Resilience rules (circuit breaker, retry) are adjusted.
- [ ] Agent lifecycle management in `UserAgentFactory` changes.

### Cross-References

- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Hybrid Router:** [../hybrid_router/README.md](../hybrid_router/README.md)
- **Agent Registry (ACP v2):** [../agent_registry/README.md](../agent_registry/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)
- **Sliding Window Consolidation:** [../sliding_window_consolidation/README.md](../sliding_window_consolidation/README.md)

---

## 1. Overview

Alek-Core uses a **Multi-Agent System (MAS)** based on the **Actor Model** to handle complex reasoning and specialized tasks. Instead of a monolithic orchestrator, the system consists of independent, specialized agents that communicate via a unified protocol.

**Core Principle:** Every agent is a specialist. Coordination is handled by a central hub, and instances are isolated per user.

### 1.1 Key Benefits

- **Specialization:** Each agent (Memory, Web, Consolidation) focuses on one domain.
- **Resilience:** Failures in one agent (e.g., Web Search timeout) don't crash the entire system.
- **Scalability:** Agents can execute in parallel via `asyncio.gather`.
- **Extensibility:** New capabilities can be added by registering new agents without modifying core logic.

---

## 2. Agent Communication Protocol (ACP)

The ACP standardizes all inter-agent traffic, ensuring platform-agnostic communication.

### 2.1 AgentMessage (Request)

- `task_id`: Unique identifier for tracking.
- `sender` / `recipient`: Routing addresses.
- `intent`: `QUERY`, `DELEGATE`, `INFORM`, `REQUEST_FEEDBACK`.
- `payload`: Task-specific data (e.g., search query).
- `context`: Metadata (user_id, session_id, account_id).
- `priority`: 0-10 scale for scheduling.

### 2.2 AgentResponse (Result)

- `status`: `SUCCESS`, `PARTIAL`, `FAILED`, `TIMEOUT`, `CANNOT_HANDLE`.
- `result`: The actual data or `SmartResponse`.
- `confidence`: 0.0-1.0 score.
- `metadata`: Usage stats (tokens), latency, reasoning traces. SmartAgent additionally carries:
  - `response_summary` — pre-computed compact history entry (if available synchronously).
  - `response_summary_task` — `asyncio.Task` for deferred summary generation (fire-and-forget postprocessing).

---

## 3. Coordination & Routing

### 3.1 AgentCoordinator

The central hub for all agent interactions.

- **Registry:** Maintains a map of active agent instances.
- **Explicit Routing:** Routes messages to a specific `agent_id`.
- **Broadcast Routing:** Finds capable agents based on intent and capabilities.
- **Parallel Execution:** Executes multiple tasks simultaneously and aggregates results.

### 3.2 Execution Flow

1. **Ingress:** `ConversationHandler` creates an `AgentMessage` and routes it to the `RouterAgent`.
2. **Triage:** `RouterAgent` classifies the intent and delegates to `Quick` or `Smart` agents.
3. **Specialization:** `SmartResponseAgent` calls `delegate_to_specialist(intent, query)` → coordinator resolves via `AgentRegistry` → routes to the appropriate specialist.
4. **Aggregation:** Results are synthesized and returned to the user.

### 3.3 ACP v2: Agent Registry Pattern

ACP v1 had SmartAgent hardcoding tool schemas per specialist (tight coupling). ACP v2 replaces this with a dynamic registry:

- **AgentRegistry** maps intents → AgentManifest (agent_id + execution mode per intent).
- **SmartAgent** has 1 fixed tool: `delegate_to_specialist(intent, query, context)`. Never grows.
- **AgentCoordinator** adds `handle_delegation()` — translates the generic tool call into a concrete AgentMessage routed to the right specialist.
- **ExecutionMode:** SYNC (search queries, inline result) or ASYNC (long tasks, Cloud Tasks + callback).

Available intents are injected into the `delegate_to_specialist` tool description dynamically from the registry. Adding a new agent = register in `main.py` + add entry to `PROTOCOL_SMART_AGENT_SELECTION` Firestore token. Zero SmartAgent code changes.

See: [Agent Registry Building Block](../agent_registry/README.md) for full details.

---

## 4. Agent Lifecycle (UserAgentFactory)

Agents are instantiated and managed per user to ensure strict data isolation and personalized configuration.

### 4.1 Per-User Isolation

- **Factory:** `UserAgentFactory` builds a complete set of agents for each user.
- **Caching:** Agent instances are cached for **1 hour (TTL)** to optimize "warm starts".
- **Configuration:** 3-level inheritance (USER > ACCOUNT > SYSTEM) is resolved during instantiation. Resolved values include `semantic_search_limit`, `biographical_cache_limit`, `principles_cache_limit`, and `history_recent_full_turns` (how many recent model turns receive full context vs. compressed summary).

### 4.2 Prompt Integration

- Agents use `UserPromptBuilder` to assemble their system instructions.
- **v3 Integration:** Supports token-based prompt assembly with security validation.
- **Preloading:** Prompts are pre-assembled and cached during agent initialization.

---

## 5. Agent Categories

### 5.1 Core Agents (Reasoning)

- **Router Agent:** Intent classification and triage.
- **Quick Agent:** Fast, low-cost responses (Gemini Flash). Outputs JSON (`full_response` + `response_summary`) parsed by `llm_response_parser`.
- **Smart Agent:** Deep reasoning and specialist delegation (Gemini Pro / Claude). Uses 1 generic `delegate_to_specialist(intent, query)` tool — intents resolved via AgentRegistry. After generating a response, fires async `response_summary_task` (via `HistorySummaryService`) so history compression never blocks Slack delivery. Timeout: `240s`, `max_retries=0`.

### 5.2 Specialist Agents (Tools)

- **Memory Search Agent:** Two-phase: (1) LLM key formulation via `COGNITIVE_PROCESS_MEMORY_SEARCH` Firestore token — Gemini Flash extracts `keywords`, `primary_query`, `alternative_query`, `domains` from the delegation query; (2) multi-vector RRF search via `SearchEnrichmentService`. Schema-enforced: 3–5 keywords, 2 domains max, 50-char query limit.
- **Web Search Agent:** Real-time information retrieval via Google Search grounding.
- **Consolidation Agent:** Background synthesis of conversation history into facts.

### 5.3 Infrastructure Agents

- **Billing Agent:** Quota enforcement and usage tracking.
- **Logger Agent:** Centralized structured logging and trace correlation.

---

## 6. Resilience Mechanisms

### 6.1 Circuit Breaker

Every agent is protected by a `CircuitBreaker` to prevent cascading failures.

- **Threshold:** 3 consecutive failures.
- **State:** Opens for **5 minutes** (recovery timeout).
- **Action:** Requests to an "Open" agent are immediately rejected with `FAILED` status.

### 6.2 Retry Logic

- **Strategy:** Exponential backoff (1s, 2s, 4s...).
- **Limit:** 2 retries by default (3 total attempts). **Exception: SmartAgent uses `max_retries=0`** — retrying a thinking model that timed out doubles wall-time to 480s+ and provides no UX benefit.
- **Timeout:** Message-level timeout overrides agent-level configuration. SmartAgent: `timeout_ms=240000` (4 min) to cover worst-case 3-turn reasoning cycles on large sessions. Previous value (150s) was too close to the boundary: Claude API required 149.7s under load.

---

## 7. SmartAgent: Fire-and-Forget Postprocessing

Thinking models (gemini-pro-preview) are expensive to call twice. History compression runs as an async background task so the user receives a response immediately:

```
SmartAgent.execute()
  │
  ├─ await LLM multi-turn reasoning  (~13–90s depending on context size)
  │
  ├─ asyncio.create_task(_generate_history_summary(response_text))
  │      └─ delegates to HistorySummaryService.summarize_model_response()
  │         → Gemini Flash (BALANCED tier), response_schema JSON, ≤300 chars
  │         runs CONCURRENTLY with Slack delivery below
  │
  └─ return AgentResponse(metadata={"response_summary_task": task})

ConversationHandler
  │
  ├─ await response_channel.send(text)   ← user sees this immediately
  │
  └─ summary = await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
       └─ history_text = summary if ENABLE_HISTORY_OPTIMIZATION else full_text
```

**Key invariants:**
- `asyncio.shield()` prevents cancellation if ConversationHandler times out before the task finishes.
- Fallback: if the summary task fails or exceeds 10s, full response text is saved to session history (no data loss). Failure is logged as `WARNING`.
- `HistorySummaryService` is always Gemini — provider locked at composition time, immune to `provider_preference: "claude"/"grok"`. This is required because `response_schema` (structured JSON output) is a Gemini-only feature; other adapters silently ignore it, causing JSON parse failures.
- Fail-fast: one attempt, no retry. Under `asyncio.shield` + 10s window, a retry only wastes time with no UX benefit.
- The `response_summary` key is the single standard across both agents: SmartAgent postprocessing output, QuickAgent JSON field, and `parse_llm_response` parser key are all named `response_summary`.
- `HistorySummaryService` is injected into `SmartResponseAgent` via constructor. Designed to be reused by other agents (e.g., `QuickResponseAgent`) without code duplication.

**Environment flag:** `ENABLE_HISTORY_OPTIMIZATION=true` (default: `false`). When disabled, full response text is always stored — safe fallback for debugging.

---

## 8. ConversationHandler: Graceful Degradation on SmartAgent Failure

When SmartAgent returns `TIMEOUT` or `FAILED`, `ConversationHandler` does not expose raw error
text to the user. Instead it falls back to QuickAgent with an injected system context note:

```
ConversationHandler.handle_message()
  │
  ├─ await coordinator.route_message(message → router_agent)
  │      └─ SmartAgent: TIMEOUT or FAILED
  │
  ├─ Fallback: build fallback_message
  │      recipient = quick_response_agent_{user_id}   ← direct, bypasses Router
  │      current_message_parts += MessagePart(text="[System: ...apologize, no technical details...]")
  │
  ├─ await coordinator.route_message(fallback_message)
  │      └─ QuickAgent: SUCCESS → response replaces original
  │         (existing text extraction handles SmartResponse normally)
  │
  └─ If QuickAgent also fails: send_status(ERROR) only — no raw error text
```

**Key design decisions:**
- Fallback is inline (no new method) — stays inside `async with RequestContext`.
- `[System: ...]` note replicates the file-failure graceful degradation pattern from `FileConversionService`.
- Raw `f"Error: {response.error}"` to user is permanently removed.
- If both agents fail, user sees only the ERROR emoji status — never a stack trace.

**Tests:** `tests/unit/handlers/test_conversation_handler_fallback.py` (7 tests).

---

## 9. Code References

- `src/domain/agent.py`: ACP definitions (Message, Response, Config).
- `src/infrastructure/agent_coordinator.py`: Routing, parallel execution, and `handle_delegation()` (ACP v2).
- `src/infrastructure/agent_registry.py`: AgentRegistry, AgentManifest, ExecutionMode (ACP v2).
- `src/agents/base_agent.py`: Base class with resilience primitives.
- `src/agents/core/smart_response_agent.py`: `delegate_to_specialist` tool + memory-first parallel scheduling.
- `src/agents/memory_search_agent.py`: LLM key formulation + `MEMORY_SEARCH_RESPONSE_SCHEMA`.
- `src/handlers/agent_worker_handler.py`: ASYNC task execution from Cloud Tasks.
- `src/services/user_agent_factory.py`: Lifecycle and DI management.
- `src/services/history_summary_service.py`: LLM-based response compression (Gemini-locked, fail-fast).
- `src/utils/llm_response_parser.py`: Unified JSON parser for `full_response` + `response_summary`. Guards against mistaking embedded JSON examples for response envelopes.
- `src/handlers/conversation_handler.py`: Fire-and-forget summary task + graceful degradation fallback.

---

## 10. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-02-21

---
