# RFC: Task Complexity Classification & Dynamic Execution Settings

**Status**: Accepted
**Owner**: Dmytro
**Date**: 2026-04-14

---

## 1. Motivation

The orchestrators currently have two problems sharing a single root:

1. **The Router underdelivers**. The `complexity 1–10 → Quick/Smart` classification is too coarse and does not use the potential of LLM triage. We pay for the router on every message, but all we get from it is a binary switch.
2. **Quick has degenerated into a copy of Smart**. After the parity refactoring, the only differences are the absence of re-evaluation after tool results and disabled intent_remap. A separate agent is not justified.

**The idea**: the router classifies the kind of task (semantically), while the execution configuration (tier, thinking_effort, [optionally] provider) is resolved through a table that has a code-level default and a user-level override. Smart becomes the single orchestrator, executing with a dynamically selected configuration. Quick is deprecated and gets removed in a separate PR.

Key separation of responsibilities:

- **Router** — understands the _kind of task_, knows nothing about infrastructure.
- **Complexity settings table** — maps kind → execution parameters.
- **Smart** — consumes resolved settings per-call.

## 2. Current state: entry points into Smart

This is critical for the design, because the router currently covers only one of the paths. Other entry points either set parameters explicitly or run on defaults.

| Entry point              | Source                                                                                                 | Goes through Router now?        | How execution context is set now                                                  |
| ------------------------ | ------------------------------------------------------------------------------------------------------ | ------------------------------- | --------------------------------------------------------------------------------- |
| User message (chat)      | Slack / Telegram adapter → ConversationHandler                                                         | **Yes**                         | Router outputs `target_agent` (quick/smart)                                       |
| Reminder fire            | Cloud Scheduler → WorkerHandler.fire_due_reminders → RemindersService → UserNotificationService.notify | **No**                          | Hardcoded to Smart, `thinking_effort` not set                                     |
| Daily email review       | Cloud Scheduler → WorkerHandler.\_handle_daily_email_review → notify                                   | **No**                          | Hardcoded to Smart + `thinking_effort="medium"`                                   |
| Deep research result     | Cloud Run Job → webhook/polling → notify                                                               | **No**                          | Hardcoded to Smart                                                                |
| Async doc/PDF delivery   | AgentWorkerHandler → notify                                                                            | **No**                          | Hardcoded to Smart                                                                |
| Agent → Agent delegation | DocPlanner → DocGenerator (coordinator)                                                                | **No** (no router in the chain) | Target agent fixed in coordinator; each has its own tier from AgentContextBuilder |

**Conclusion**: the router covers only the user-chat path. Any uniform solution must either (a) thread the router through all entry points, (b) provide an alternative mechanism for setting complexity for non-user triggers, or (c) combine both — router for user messages, explicit hint for system triggers, propagation for agent-to-agent.

## 3. Proposed direction (high-level)

### 3.1 Domain primitives

```python
# src/domain/task_complexity.py
class TaskComplexity(str, Enum):
    SMALL_TALK       = "small_talk"
    INFO_SEARCH      = "info_search"
    SIMPLE_ANALYTICS = "simple_analytics"
    DEEP_REASONING   = "deep_reasoning"

# src/domain/complexity_settings.py
class ComplexitySettings(BaseModel):
    tier: PerformanceTier
    thinking_effort: Optional[str] = None
    intent_remap: Dict[str, str] = {}
    provider_override: Optional[str] = None   # rare edge case
```

Semantic names (not numeric levels) — it is easier for the LLM router to classify by the meaning of the task; the code-level table transparently maps them to execution parameters.

### 3.2 Settings table

The default table lives in `src/infrastructure/agent_config.py`, user override via `UserBotConfig.complexity_settings_overrides`. Provider is **absent** from the defaults — it comes from agent-level settings (`user_config.get_provider_for_agent("smart")` or the STRATEGIES default). A per-complexity provider override is a vestige for edge cases.

Example default:

```
small_talk       → ECO
info_search      → BALANCED
simple_analytics → BALANCED + thinking=low
deep_reasoning   → PERFORMANCE + thinking=high
```

The exact values are a matter of tuning after rollout.

### 3.3 Resolution

`TaskExecutionResolver` (a new service) reads `message.context["task_complexity"]`, resolves settings via the table + user override, and builds `TaskExecutionOverride(llm, model_name, thinking_effort)` through the extended `AgentContextBuilder.resolve_for_task(agent_type, user_config, settings)`.

At the start of `process()`, Smart calls the resolver and applies the override to all turns inside the delegation loop (one provider for the entire scope of message processing).

### 3.4 Who sets `task_complexity` in `message.context`

This is where the open questions of §4 live. Candidates:

- **Router** — for user messages, as an extension of the current intent classification.
- **Worker handlers** — for system triggers, an explicit hint (`task_complexity="deep_reasoning"` when creating the AgentMessage).
- **AgentNote** — an optional pinned field, if the reminder author (LLM or user) wants to set it explicitly.
- **Inheritance** — for agent-to-agent: Smart delegates to sub-specialists via the coordinator, where complexity is not carried over (sub-specialists have their own fixed tier anyway); but if Smart calls itself through the coordinator (no such case yet), complexity must carry over.

### 3.5 Dispatch simplification

The Quick agent stays registered in the registry, but ConversationHandler does not dispatch to it — all user messages go to Smart with resolved complexity. Removing Quick is a separate PR (see §6).

## 4. Open architectural questions

These questions block design finalization. While they are unanswered — no implementation.

### Q1. Reminders: route through router or pinned complexity?

**Options**:

- **A**. Run the reminder alert text through the router in `RemindersService.fire_due_reminders` before `notify()`. Pros: one path for all messages. Cons: +1 LLM call per fire, +latency, +cost; the router gets called from a service that used to be purely infrastructural.
- **B**. Pinned complexity on `AgentNote` (a new field `complexity: Optional[TaskComplexity]`). NotesAgent LLM / Cabinet UI set it when creating the reminder. If empty — default (e.g. `simple_analytics`). Pros: 0 extra LLM calls; the reminder author knows the desired depth. Cons: a new layer of responsibility for NotesAgent (tool schema + prompt context), the Cabinet UI reminder form gets yet another field.
- **C**. A per-entry-source default in code. Reminders always get `simple_analytics` (or `deep_reasoning`), no classification. Pros: simplest. Cons: cannot distinguish "remind me to water the plants" from "do a morning briefing on my inbox" — both go through the same tier.
- **D**. Hybrid B+C: per-entry-source default, but AgentNote can override via pinned.

**Leaning towards D** — it is both cheap and gives per-reminder control.

### Q2. Daily email review and other worker tasks: explicit hint or router?

Same as Q1, but for worker-level triggers:

- Daily email review already passes `thinking_effort="medium"`. It is logical to keep the same style: worker-task code explicitly sets `task_complexity="deep_reasoning"` in the `notify(...)` kwargs.
- The router is definitely not needed here — the worker-task developer knows the nature of the payload better than the router.

**Preliminary decision**: **explicit hint in worker handlers**. No need to remove `thinking_effort="medium"` — it wins over profile.thinking_effort (context has priority).

### Q3. Agent → Agent delegation: propagation or fixed tier?

Currently sub-specialists (EmailSearch, WebSearch, Maps, Compute) have their own tier, hardwired in AgentContextBuilder by `agent_type`. Smart delegates to them via the coordinator — its complexity does not apply to them.

However: **after tool results Smart re-evaluates and may delegate further**. All within one user message → one Smart turn → one complexity. Nothing needs to change here.

A problem would arise if we make **Smart delegate to another Smart-like orchestrator** (e.g. an internal "reasoning" agent). No such case exists yet.

**Preliminary decision**: complexity is a parameter of one specific Smart run, not propagated downward. Sub-specialists use their own fixed tiers.

**But** — the invariant must be recorded: if a nested Smart appears in the future, complexity must either propagate or be re-classified at the new level. To be decided when it becomes necessary.

### Q4. Safety net for router uncertainty

CLAUDE.md currently says: "low confidence always falls back to Smart". With finer granularity we need to decide:

- **Code-level fallback**: the router returned invalid / unknown / low-confidence → which default? Not `small_talk` (degrades quality), probably `simple_analytics`. But: `simple_analytics` on an utterly trivial message is an overpayment. Is the cost/quality trade-off in favor of quality consensually OK?
- **Prompt-level rule**: an explicit rule "when in doubt, pick nothing lower than `simple_analytics`". This is the same thing, just in the prompt. Both levels are needed (prompt + code), because the LLM can make mistakes.

**Open question**: What exactly counts as "low confidence"? The router currently emits a confidence score in its output, but the threshold (e.g. `< 0.7` → fallback) must be calibrated on real data.

### Q5. Provider override per complexity — needed in v1?

The user said "a provider override is a very rare case — by default an agent has one provider". Options:

- **A**. Keep the `provider_override` field in `ComplexitySettings` but do not show it in the v1 Cabinet UI (configurable only via API/JSON). Ready for extension.
- **B**. Cut it entirely until a real use case appears (YAGNI). Add later.

Not a critical decision, but it affects the `ComplexitySettings` signature and the volume of tests.

### Q6. Quick deprecation: synchronously or as a follow-up?

- **A**. In the same PR: remove Quick, WebSearchLight, intent_remap, the corresponding tests. Pros: a clean final picture. Cons: a large PR, regression risk on the traffic that currently goes to Quick.
- **B**. In this PR only disable dispatch to Quick (ConversationHandler always goes to Smart). Quick lives on as dead code. Removal is a follow-up. Pros: easier to roll back. Cons: a transition period with dead code.

**Leaning towards B** — less risk packed into a single PR.

### Q7. `message.context` as a channel — is it sufficient?

Currently `message.context` is a dict that:

- is initialized in the handler / adapter,
- is propagated via DelegationEngine context passthrough,
- is read in agents.

The problem: `task_complexity` is **metadata about execution**, not message content. Mixing it with `origin_channel_id`, `session_id` and the rest is semantically a bit dirty.

Alternative: add a separate field `AgentMessage.execution_hints: Optional[ExecutionHints]` as a value object.

Pros of a separate field: a typed channel, an explicit contract, fewer "magic strings" in the context dict.
Cons: refactoring `AgentMessage` and all call sites; `thinking_effort` and others already live in the context dict — the split would create inconsistency.

**Open question**: do the `AgentMessage.execution_hints` refactoring within this RFC, or keep everything in the context dict as it is now? A purely architectural choice.

### Q8. Router responsibility: stays an intent classifier or expands into a dispatch controller?

Currently the router:

- Classifies complexity 1–10 (crude)
- Extracts semantic lens + search intent
- Triggers memory/web enrichment **before** routing

If we also ask the router to return `task_complexity`, its output schema expands. That is fine. But if reminders/email_review later also start going through the router (Q1 opt A), the router turns from "understands user intent" into "a universal dispatcher". That is a **change of responsibility** that should be explicitly recorded or rejected.

**Leaning towards**: the router stays an intent classifier for user messages. System triggers set complexity explicitly. This preserves unity of responsibility and does not smear the router across different call paths.

### Q9. How many complexity levels in v1?

The user gave 4 as an example. Possible alternatives:

- **3** levels (`light`, `standard`, `deep`) — less decision fatigue for the LLM router, but coarser granularity.
- **4** levels (the ones listed in §3.1) — a balance.
- **5** levels (add `research` above `deep_reasoning`) — needed only if deep research triage will be classified separately.

To be decided at RFC finalization. Architecturally, adding/removing a level is easy (an enum entry + a table row).

### Q10. Cabinet UI — what does the user see and change?

- **Minimum**: a 4-row table, for each complexity — a tier dropdown + a thinking dropdown. Provider override hidden (see Q5).
- **Plus**: default values shown in gray, an `is_overridden` flag, a "reset to default" per row.
- **Plus**: example hints ("small_talk: greetings, yes/no…") so the user understands what they are tuning.

Not blocking, but must be decided before the UI implementation.

### Q11. Logging and debuggability

When Smart executed with an override — in the logs and in `get_debug` / billing we must see:

- Which complexity arrived
- How it was resolved (default vs user override)
- Which final `(provider, model, thinking)` was applied
- Who set the complexity (router / explicit / fallback)

Where to write this? `AgentResponse.metadata`? a separate event? `debug_logger`? To be decided at implementation time, but the RFC should record that "the resolution trace is a mandatory artifact".

## 5. Non-goals / out of scope v1

- Account-level overrides (no `AccountConfig` currently)
- Firestore-backed default table (in-code is enough for now)
- Integrating complexity with consolidation / deep research jobs (they have their own tier via the job port)
- Dynamic tuning of the complexity table based on a feedback loop (ML-ops)
- Multi-entry-point roll-out in a single PR — it must be staged

## 6. Migration path (sketch)

1. **Phase 1**: infrastructure — domain types, default table, resolver, `resolve_for_task`, wiring. The router **does not change**, nobody sets `task_complexity`. Smart behaves as before (override always None, old path). Unit tests. Nothing breaks.
2. **Phase 2**: the router starts emitting `task_complexity` for user messages; ConversationHandler passes it through into context. Quick stays alive but is never selected. Canary on dev, metrics.
3. **Phase 3**: reminders/worker tasks get an explicit hint. A couple of iterations tuning the defaults + router criteria.
4. **Phase 4**: removal of Quick + related pieces (follow-up PR after a production bake).

## 7. Appendix: critical files for the future implementation

Left as a hint, not as a commitment:

- `src/domain/task_complexity.py`, `src/domain/complexity_settings.py` _(new)_
- `src/domain/user.py` — `complexity_settings_overrides`
- `src/infrastructure/agent_config.py` — `DEFAULT_COMPLEXITY_SETTINGS`, resolver
- `src/services/agent_context_builder.py` — `resolve_for_task`
- `src/services/task_execution_resolver.py` _(new)_
- `src/agents/router_agent.py` — output schema, Firestore prompt token
- `src/handlers/conversation_handler.py` — complexity in context, always Smart
- `src/agents/core/smart_response_agent.py` — override in the delegation loop
- `src/agents/core/base_agent.py` — `_call_llm(llm_override)`
- `src/composition/service_container.py`, `src/composition/user_agent_factory.py`
- `src/handlers/worker_handler.py`, `src/services/user_notification_service.py` — explicit hint for system triggers (after Q2)
- `src/services/reminders_service.py` — fire-path behavior (after Q1)
- `src/domain/agent_note.py`, `src/adapters/firestore_agent_note_adapter.py` — pinned complexity (after Q1 opt D)
- `src/web/` — `/api/user/complexity-settings` GET/PUT (after Q10)
- Cabinet UI templates

## 8. Decision log

- **Q4 — confidence (2026-04-21)**: the `confidence` field was removed from `RoutingMetadata` and
  from the TRIAGE schema. The safety net is implemented statically: any unknown/empty
  `task_complexity` from the router maps to `TaskComplexity.SIMPLE_ANALYTICS` in
  `build_routing_metadata` and `RoutingMetadata.from_dict`. Calibrating confidence
  thresholds was deemed unfeasible without realtime metrics; we chose a deterministic
  fallback by enum value. If it is needed later — we will reintroduce `confidence` as a
  separate field without rewriting the routing logic.
- **Q6 — Quick deprecation (2026-04-21)**: option B. The router always goes to Smart
  (`_apply_routing_rules` → `smart_agent_id`), Quick stays as live code.
  Removal — a follow-up PR after a production bake.
- **v1 implementation (2026-04-21)**: Phase 1 + Phase 2 of RFC §6 are done.
  `TaskExecutionResolver`, `DEFAULT_COMPLEXITY_SETTINGS`, `AgentContextBuilder.resolve_for_task`,
  `UserBotConfig.complexity_settings_overrides`, wiring of task_complexity from the router
  into Smart via `message.context` — done. Phase 3 (reminders / daily email review /
  async doc delivery explicit hint) and Phase 4 (Quick removal) — not covered.
