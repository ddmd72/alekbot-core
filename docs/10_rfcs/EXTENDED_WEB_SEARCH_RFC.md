# RFC: Extended Web Search Agent

**Status:** ~~Proposed~~ **REJECTED — superseded by prompt-based approach**
**Date:** 2026-02-22
**Owner:** AI Engineering
**Milestone:** Phase 2 — Async Specialist Agents
**Depends on:** ACP_V2_SIMPLIFIED_RFC.md (ASYNC infrastructure)
**Parallel with:** GMAIL_EMAIL_INDEXING_RFC.md (both need `_notify_completion`)

> **Decision (2026-02-22):** RFC invalidated after POC testing. The proposed multi-turn agent loop
> is unnecessary. A single Gemini grounding call with a cognitive process prompt achieves equivalent
> or better research coverage at 15-30s latency. See §8 for details.

---

## 1. Executive Summary

Current `WebSearchAgent` executes a **single Gemini grounding call** — one query, one result.
For research tasks (event analysis, product comparison, controversy investigation) this is insufficient.

**This RFC introduces:**

- ✅ `ExtendedWebSearchAgent` — dedicated ASYNC agent with multi-angle search strategy
- ✅ `AgentWorkerHandler._notify_completion()` — first real implementation (unblocks Gmail too)
- ✅ Clear routing split: `search_web` (quick fact, SYNC) vs `search_web_extended` (research, ASYNC)
- ✅ SmartAgent passes enriched context; agent owns search strategy entirely

**No changes to SmartAgent code.** Registry + manifest descriptions drive routing.

---

## 2. Problem Statement

### 2.1 Single-Shot Search Limitation

```
User: "Розкажи про скандали навколо Фальяс 2026"

Current WebSearchAgent:
  → one Gemini grounding call: "скандали Фальяс 2026"
  → returns first-page results
  → SmartAgent formats and responds

Result: superficial. Misses context, depth, cross-references.
```

**Evidence from production logs (2026-02-22):**
Bot's first attempt answered from general knowledge (no web search at all).
When corrected, did enriched multi-angle search manually inside SmartAgent prompt.
This logic does NOT belong in SmartAgent — it belongs in a dedicated agent.

### 2.2 Current Architecture Gap

```
search_web (SYNC, WebSearchAgent):
  1 query → 1 Gemini grounding call → result

search_web_extended (missing):
  query + context → multi-step research strategy → synthesis → async notification
```

### 2.3 _notify_completion is Missing

`AgentWorkerHandler` has `# TODO: notify user via platform-agnostic ResponseChannel`
on lines 83 and 95. Without this, ASYNC execution mode is useless — tasks complete
silently and the user never sees the result.

This is the shared blocker for ALL async agents (ExtendedWebSearch and Gmail alike).

---

## 3. Solution Design

### 3.1 Two-Agent Split

| Aspect | `search_web` (existing) | `search_web_extended` (new) |
|---|---|---|
| Use case | Quick single fact | Research, analysis, comparison |
| Execution | SYNC | ASYNC |
| Strategy | 1 Gemini grounding call | Multi-angle LLM loop |
| Response | Inline in conversation | Slack DM/thread on completion |
| Latency | 2-5s | 30-120s |
| Initiator decision | SmartAgent via registry | SmartAgent via registry |

### 3.2 Routing Principle

SmartAgent does NOT contain routing logic. It reads `AgentManifest.description` from
the registry and lets the LLM decide. The manifest descriptions carry the discriminating criteria:

```python
# search_web
description="Quick web search for a single current fact (price, date, news headline). Use when a brief answer suffices."

# search_web_extended
description="Deep multi-angle research: events, reviews, controversies, comparisons. Use when the user needs analysis, not just a fact. ASYNC — result delivered separately."
```

### 3.3 Enriched Context from SmartAgent

SmartAgent does not pass just a raw query. The `delegate_to_specialist` tool schema
for `search_web_extended` has required structured fields. SmartAgent's LLM fills them
from conversation context.

```python
# Tool schema drives what SmartAgent must provide:
"context": {
    "research_focus": "what specifically to look for",
    "output_intent":  "analysis | comparison | timeline | recommendations",
}
```

The tool description (injected from manifest + prompt token) tells SmartAgent *what* to fill.
The agent itself decides *how* to search — SmartAgent does not prescribe the search strategy.

---

## 4. Component Design

### 4.1 ExtendedWebSearchAgent

```python
# src/agents/extended_web_search_agent.py

class ExtendedWebSearchAgent(BaseAgent):
    """
    Multi-angle research agent using iterative Gemini grounding.

    Owns its search strategy entirely.
    SmartAgent passes enriched context; this agent decides angles, queries, synthesis.

    Execution: ASYNC only (registered with ExecutionMode.ASYNC).
    Result delivered via AgentWorkerHandler._notify_completion().
    """

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        grounding_tool: object,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._grounding_tool = grounding_tool
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        return (
            message.intent == AgentIntent.DELEGATE
            and bool(message.payload.get("query"))
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query   = message.payload.get("query", "")
        context = message.payload.get("context", {})

        # Build system prompt from Firestore (agent_type="extended_websearch")
        system_prompt = await self._build_prompt(query, context)

        # Multi-angle search loop (strategy defined in prompt, not here)
        results = await self._run_search_loop(system_prompt, query, context)

        # Synthesize
        synthesis = await self._synthesize(results, query, context)

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=synthesis,
            metadata={"angles_searched": len(results), "model": self.model_name},
        )
```

**Search loop sketch (prompt-driven, not hardcoded angles):**

```python
async def _run_search_loop(self, system_prompt, query, context):
    """
    LLM decides search angles. Hard cap: MAX_ITERATIONS = 6.

    Turn 1: LLM given query + context → returns search plan (list of queries)
    Turns 2-N: execute each grounding query, feed results back to LLM
    Final turn: LLM synthesizes all results into research report
    """
    MAX_ITERATIONS = 6
    history = []
    results = []

    for _ in range(MAX_ITERATIONS):
        response = await self._llm.generate_content(
            LLMRequest(
                system_instruction=system_prompt,
                messages=history,
                tools=[self._grounding_tool],
                ...
            )
        )

        if response.is_final:   # LLM signals done (no more search queries)
            break

        search_query = response.next_search_query
        grounding_result = await self._execute_grounding(search_query)
        results.append(grounding_result)
        history.append(...)     # feed result back

    return results
```

**Prompt (Firestore token `extended_websearch`):**

- Defines what "angles" mean: factual, background, recent, social/reviews, controversies
- Instructs LLM when to stop (enough data vs diminishing returns)
- Defines output format (Slack mrkdwn, structured sections)
- Instructs LLM to note what was NOT found

### 4.2 AgentWorkerHandler: _notify_completion()

Current state: `coordinator` injected, no response channel.
Required change: inject `app_client` (Slack) for MVP notification.

```python
# src/handlers/agent_worker_handler.py

class AgentWorkerHandler:
    """
    Background task executor for async agent intents.
    Now includes Slack notification on completion (MVP: always Slack).
    """

    def __init__(
        self,
        coordinator: AgentCoordinator,
        slack_app_client,           # Slack WebClient — MVP only
    ) -> None:
        self._coordinator = coordinator
        self._slack_client = slack_app_client

    async def handle_task(self, payload):
        ...
        try:
            response = await self._coordinator.route_message(message)

            if response.status == AgentStatus.SUCCESS:
                await self._notify_completion(
                    user_id=context.get("user_id"),
                    channel_id=context.get("channel_id"),   # original channel
                    thread_ts=context.get("thread_ts"),      # reply in thread if available
                    result=response.result,
                )
            else:
                await self._notify_failure(
                    user_id=context.get("user_id"),
                    channel_id=context.get("channel_id"),
                    thread_ts=context.get("thread_ts"),
                    error=response.error,
                )

        except Exception as e:
            ...

    async def _notify_completion(self, user_id, channel_id, thread_ts, result):
        """
        Post result to original channel/thread.
        Fallback to DM if channel_id not available.
        """
        target = channel_id or user_id  # DM fallback
        text = str(result) if isinstance(result, str) else result.get("text", str(result))

        await self._slack_client.chat_postMessage(
            channel=target,
            text=text,
            thread_ts=thread_ts,
            mrkdwn=True,
        )

    async def _notify_failure(self, user_id, channel_id, thread_ts, error):
        target = channel_id or user_id
        await self._slack_client.chat_postMessage(
            channel=target,
            text=f"❌ Дослідження не вдалося: {error}",
            thread_ts=thread_ts,
            mrkdwn=True,
        )
```

**Note on hexagonal purity:**
Injecting `slack_app_client` directly is a pragmatic MVP choice.
Phase 2: extract `NotificationPort(ABC)` with `send_dm(user_id, text)` method.
Justified by 2 implementations: Slack + Telegram (both have `ResponseChannel` adapters).

### 4.3 AgentRegistry: Updated Manifest Descriptions

```python
# main.py

agent_registry.register(AgentManifest(
    agent_id="web_search_agent",
    intents={"search_web": ExecutionMode.SYNC},
    description=(
        "Quick web search for a single current fact (price, date, news headline, "
        "brief answer). Use when the user needs one concrete piece of information."
    ),
))

agent_registry.register(AgentManifest(
    agent_id="extended_web_search_agent",
    intents={"search_web_extended": ExecutionMode.ASYNC},
    description=(
        "Deep multi-angle research: events, controversies, reviews, comparisons, "
        "analysis. Use when the user needs depth, not just a fact. "
        "ASYNC — result delivered separately, not inline."
    ),
))
```

### 4.4 Async Payload: channel_id and thread_ts

`AgentCoordinator._execute_async()` must include `channel_id` and `thread_ts`
in the context passed to Cloud Tasks so `_notify_completion()` can reply in thread:

```python
async def _execute_async(self, manifest, query, context, delegation_context):
    payload = {
        "agent_id": manifest.agent_id,
        "intent":   intent,
        "query":    query,
        "context": {
            **context,
            "channel_id": delegation_context.get("channel_id"),  # add if not already there
            "thread_ts":  delegation_context.get("thread_ts"),
        }
    }
    task_name = await self._task_queue.enqueue(...)
    ...
```

### 4.5 UserAgentFactory: ExtendedWebSearchAgent instantiation

```python
# src/services/user_agent_factory.py

def _create_extended_web_search_agent(self, user_id: str) -> ExtendedWebSearchAgent:
    """Per-user instance. Shares grounding_tool with WebSearchAgent."""
    ec = self._provider_registry.get_execution_context(
        agent_type="extended_websearch",
        tier=PerformanceTier.PERFORMANCE,
    )
    return ExtendedWebSearchAgent(
        config=AgentConfig(
            agent_id=f"extended_web_search_agent_{user_id}",
            agent_type="extended_websearch",
        ),
        execution_context=ec,
        grounding_tool=self._grounding_tool,
        prompt_builder=self._prompt_builder,
        user_id=user_id,
    )
```

---

## 5. ASYNC Flow: End-to-End

```
User: "Досліди всі скандали та проблеми навколо Фальяс 2026"

1. SmartAgent LLM:
   delegate_to_specialist(
     intent="search_web_extended",
     query="скандали та проблеми Фальяс 2026",
     context={
       "research_focus": "скандали, проблеми, критика, інциденти",
       "output_intent": "analysis"
     }
   )

2. AgentCoordinator:
   manifest = registry.get_agent_for_intent("search_web_extended")
   # → extended_web_search_agent, ExecutionMode.ASYNC
   → _execute_async(manifest, query, context)
   → enqueues to Cloud Tasks (agent-tasks-prod)
   → returns {status: "started", message: "..."}

3. SmartAgent formats ACK to user:
   "🔍 Починаю глибоке дослідження Фальяс 2026.
    Це займе хвилину-дві — надішлю результат окремо."

4. [~60-90 seconds later, Cloud Tasks worker]

5. AgentWorkerHandler.handle_task():
   → ExtendedWebSearchAgent.execute()
   → Multi-angle search loop (factual, history, news, controversies, social)
   → Synthesis: structured research report

6. AgentWorkerHandler._notify_completion():
   → slack_client.chat_postMessage(
       channel=original_channel_id,
       thread_ts=original_thread_ts,
       text=<research report>
     )

User sees research report in the original thread.
```

---

## 6. Implementation Plan

### Step 1: AgentWorkerHandler._notify_completion() [prerequisite for all async agents]

- [ ] Add `slack_app_client` param to `AgentWorkerHandler.__init__()`
- [ ] Implement `_notify_completion()` with channel+thread reply
- [ ] Implement `_notify_failure()`
- [ ] Update `main.py`: pass `slack_app_client` to `AgentWorkerHandler`
- [ ] Verify `channel_id` / `thread_ts` are available in context (check coordinator payload)
- [ ] Unit test: mock `slack_app_client.chat_postMessage`, verify called with correct args

### Step 2: ExtendedWebSearchAgent class

- [ ] Create `src/agents/extended_web_search_agent.py`
- [ ] Implement `can_handle()`, `execute()`, `_run_search_loop()`, `_synthesize()`
- [ ] Prompt: create Firestore token `extended_websearch` (agent_type)
- [ ] Add to `src/agents/__init__.py`
- [ ] Unit tests: mock LLM, verify multi-turn loop, verify synthesis called

### Step 3: Registration

- [ ] `UserAgentFactory._create_extended_web_search_agent()`
- [ ] `UserAgentFactory` registers agent instance in coordinator
- [ ] `main.py`: `AgentManifest` for `extended_web_search_agent`
- [ ] Update `web_search_agent` manifest description (more precise discrimination)

### Step 4: AgentCoordinator payload

- [ ] Verify `channel_id` and `thread_ts` pass through to Cloud Tasks payload
- [ ] If missing: add to `_execute_async()` from `delegation_context`

### Step 5: Prompt token in Firestore

Owner task (manual):
- Create `PROTOCOL_SMART_AGENT_SELECTION` token update: when/how examples for `search_web_extended`
- Create `extended_websearch` agent prompt token with search strategy instructions

---

## 7. Open Questions & Decisions

### Q1: Search Angles — LLM-Driven vs Hardcoded

**Question:** Does the agent prompt define fixed search angles, or does the LLM derive them?

**Options:**

A. Fixed angles in prompt (factual, history, news, social, controversies)
B. LLM derives angles from query + context each time

**Discussion:**
A is predictable and testable. B is adaptive but harder to control.
Hybrid: prompt provides angle taxonomy as examples, LLM selects relevant subset.

**Decision:** TBD (leaning A for MVP, B for v2)

---

### Q2: Max Iterations Cap

**Question:** Hard cap on search iterations?

**Options:**

A. Hard cap: 6 iterations (regardless of LLM request)
B. LLM signals DONE (prompt-driven stop condition)
C. Both: LLM signals, but hard cap as safety net

**Decision:** C — LLM signals done, hard cap = 6 as fallback

---

### Q3: Notification Target — Thread vs DM

**Question:** Where does the result appear?

**Options:**

A. Always reply in original thread (where request was made)
B. Always DM to user
C. Thread if `thread_ts` available, DM fallback

**Discussion:**
A/C is the better UX — keeps research result contextually near the request.
Requires `channel_id` and `thread_ts` in async payload.

**Decision:** C for MVP

---

### Q4: Timeout Handling

**Question:** What if search takes >120s (Cloud Tasks timeout)?

**Options:**

A. Cap search loop at 4 angles max (fits in ~90s)
B. Split into two tasks (search + synthesize)
C. Optimistic: current search takes 15-20s/angle, 6 angles = ~90s, fits

**Decision:** C for MVP. Monitor in production. Revisit if timeout occurs.

---

### Q5: NotificationPort for Phase 2

**Current:** MVP injects `slack_app_client` directly into `AgentWorkerHandler`.

**Phase 2:** Extract `NotificationPort(ABC)` with:
```python
@abstractmethod
async def send_notification(self, user_id: str, channel_id: str, thread_ts: Optional[str], text: str) -> None:
    ...
```

Implementations: `SlackNotificationAdapter`, `TelegramNotificationAdapter`.

**Decision:** Phase 2. Not blocking MVP.

---

## 8. Success Criteria

### Functional

- SmartAgent routes `search_web_extended` intent to `ExtendedWebSearchAgent` ✅
- SmartAgent responds with ACK within 2s ✅
- ExtendedWebSearchAgent executes 3-6 grounding queries ✅
- User receives research report in original thread within 120s ✅
- Failure results in error notification (not silent) ✅

### Quality

- Research covers ≥ 3 distinct angles ✅
- Result includes source attribution (Gemini grounding provides this) ✅
- Format: Slack mrkdwn, sections, not a wall of text ✅

### Infrastructure

- `_notify_completion()` implemented and tested ✅
- Gmail ASYNC agent can use same `_notify_completion()` without changes ✅
- No changes to SmartAgent code or prompt template ✅

---

## 9. References

**Depends on:**
- [ACP_V2_SIMPLIFIED_RFC.md](./ACP_V2_SIMPLIFIED_RFC.md) — ASYNC infrastructure (already implemented)
- [WEBSEARCH_STRUCTURED_OUTPUT_RFC.md](./WEBSEARCH_STRUCTURED_OUTPUT_RFC.md) — existing WebSearchAgent design

**Unlocks:**
- [GMAIL_EMAIL_INDEXING_RFC.md](./GMAIL_EMAIL_INDEXING_RFC.md) — shares `_notify_completion()`

**Building Blocks:**
- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md)

---

## Changelog

### 2026-02-22

- Initial RFC drafted
- Problem statement grounded in production log evidence (2026-02-22 cloud logs)
- Architecture aligned with ACP v2 registry pattern (no SmartAgent changes)
- `_notify_completion()` explicitly scoped as shared blocker for Gmail + ExtendedWebSearch
- Open questions documented: angles strategy, notification target, Phase 2 NotificationPort

---

## 8. Decision: RFC Rejected

**Date:** 2026-02-22 (same session as drafting)

### What was tested

POC (`scripts/prompt/test_extended_websearch_poc.py`) validated an alternative approach:
single Gemini grounding call with a cognitive process prompt instructing 5-vector orthogonal decomposition.

### Results

| Query type | Latency | Search queries | Coverage |
|---|---|---|---|
| News (Kyiv events) | 35s | 6 queries / 15 sources | 5 topic sections |
| Weather (Valencia week) | 16-31s | 6-15 queries | Daily table 22-28.02 |
| Local search (wine near Example City) | 18-23s | 6-18 queries | Tiered by shop type + hours |

### Why the RFC architecture is not needed

1. **Grounding API is prompt-driven** — Gemini internally decides search queries based on instructions.
   There is no `search(query=X)` API to call from an agent loop.
2. **`response.is_final` / `response.next_search_query`** — these fields do not exist in the Gemini API.
   The proposed loop control protocol has no implementation path.
3. **Single-call coverage is sufficient** — 5 orthogonal vectors in one call produces 15-20 sources
   across independent dimensions. Multi-turn adds latency with marginal quality gain for the use case.
4. **ASYNC complexity is unnecessary** — 15-30s is acceptable inline latency for research queries.
   `_notify_completion()` remains unblocked as a dependency only for Gmail RFC.

### What was implemented instead

Prompt token `COGNITIVE_PROCESS_WEBSEARCH` (in Firestore, not tracked in git) applied to the
existing `WebSearchAgent`:
- 5 orthogonal vectors — internal search strategy, not exposed in output
- Output grouped by topic (LLM decides structure)
- Series rule: enumerate each element individually (days, prices, events) — render as table where possible
- All findings include inline source links

**Status:** Rejected — no implementation required
**`_notify_completion()` dependency:** remains a blocker for Gmail RFC only
