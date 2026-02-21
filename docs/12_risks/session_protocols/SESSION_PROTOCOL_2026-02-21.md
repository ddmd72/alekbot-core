# Session Protocol: ACP v2 Simplified — Agent Registry Pattern

**Date:** 2026-02-21
**Branch:** develop
**Status:** ✅ Complete
**RFC:** docs/10_rfcs/ACP_V2_SIMPLIFIED_RFC.md

---

## Goal

Implement ACP v2 Simplified: replace hardcoded FUNCTION_TO_AGENT_MAP in SmartResponseAgent
with a dynamic AgentRegistry. Unblock async execution for future long-running agents (Gmail indexing).

## What Was Done

### New Files

**`src/infrastructure/agent_registry.py`**
- `ExecutionMode` enum: SYNC | ASYNC
- `AgentManifest` dataclass: agent_id, intents (Dict[str, ExecutionMode]), description, requires_auth
- `AgentRegistry`: register(), get_agent_for_intent(), get_available_intents(), list_agents()

**`src/handlers/agent_worker_handler.py`**
- `AgentWorkerHandler`: handles Cloud Tasks payload with task_type="agent_execution"
- Executes agent via coordinator.route_message(), logs result
- Notification callback deferred (will be added with Gmail agent)

### Modified Files

**`src/ports/task_queue.py`**
- Added `enqueue_agent_task(agent_id, intent, query, context) -> str` to Protocol

**`src/adapters/gcp_task_queue.py`**
- Implemented `enqueue_agent_task()`: POST to /worker with task_type="agent_execution"
- Reuses existing OIDC auth pattern from enqueue_slack_event()

**`src/infrastructure/agent_coordinator.py`**
- Constructor now accepts optional `registry: AgentRegistry` and `task_queue: TaskQueue`
- Added `handle_delegation(intent, query, context, calling_agent_id)` — new ACP v2 entry point
- Added `_execute_sync()` — resolves per-user agent_id, creates AgentMessage, routes via route_message()
- Added `_execute_async()` — enqueues to Cloud Tasks via task_queue
- Added `get_available_intents()` — proxies to registry, returns [] if not configured
- All existing methods (`route_message`, `register_agent`, `parallel_execute`) unchanged

**`src/agents/core/smart_response_agent.py`**
- Removed `FUNCTION_TO_AGENT_MAP` (was `{"search_memory": ..., "ask_web_search_agent": ...}`)
- Removed `_resolve_agent_id()` — logic moved to coordinator._execute_sync()
- Replaced 2-tool schema (`search_memory`, `ask_web_search_agent`) with 1 generic tool: `delegate_to_specialist(intent, query, context)`
- Available intents injected dynamically from coordinator.get_available_intents() into tool description
- `_execute_agents_smart_parallel()`: updated memory-first detection from `tc.name == "search_memory"` to `tc.name == "delegate_to_specialist" and tc.args["intent"] == "search_memory"`
- `_delegate_to_agent_with_retry()`: replaced coordinator.route_message() with coordinator.handle_delegation()
- `deliver_response` terminal tool kept as-is (RFC's respond_directly not added — avoid overlap)
- Prompt text: **TODO for owner** — update delegate_to_specialist description in Firestore tokens

**`main.py`**
- Added imports: AgentRegistry, AgentManifest, ExecutionMode, GcpTaskQueue, AgentWorkerHandler
- Created AgentRegistry with manifests for memory_search_agent (SYNC) and web_search_agent (SYNC)
- GcpTaskQueue created only in HTTP mode + GOOGLE_CLOUD_PROJECT present; otherwise None (SYNC-only)
- Coordinator now receives registry + task_queue
- AgentWorkerHandler created after coordinator
- /worker route extended: routes by task_type field, agent_execution → AgentWorkerHandler

## Key Decisions Made During Session

| Decision | Choice | Reason |
|----------|--------|--------|
| ACP v2 Simplified vs Complex | Simplified | 2 weeks vs 5, solo dev, solo budget |
| deliver_response vs respond_directly | Keep deliver_response | Already works, avoid regression |
| Per-intent execution mode | Yes (Dict[str, ExecutionMode]) | RFC Q2 decision B, more flexible |
| Async fallback in dev | No (None task_queue) | Only HTTP mode has Cloud Tasks |
| User notification in worker | Deferred | Platform-agnostic solution needed first |

## Test Result

1124 unit tests passed, 1 xfailed. No regressions.

## Next Steps

1. **Prompt update** (owner): update `delegate_to_specialist` description in Firestore prompt tokens
   to include available intents guidance and rich params for search_memory in `context` field
2. **Gmail Agent** (next session): implement GmailAgent with search_email (SYNC) + index_gmail (ASYNC),
   register in main.py, add user notification via platform-agnostic ResponseChannel
3. **Worker notification**: implement callback after Gmail agent ships
