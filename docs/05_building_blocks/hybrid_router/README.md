<!-- living-doc
covers:
  src/agents/core/
  src/infrastructure/delegation_engine.py
  src/infrastructure/task_execution_resolver.py
  src/domain/task_complexity.py
  src/domain/complexity_settings.py
  src/domain/tone.py
  src/agents/web_search_light_agent.py
-->
# Hybrid Router

## Purpose

This block is the front door of a chat turn. `RouterAgent` reads the turn, classifies what kind of task it is
(`TaskComplexity`) and the user's tone, starts the memory prefetch, loads the active reminders and hands everything to
`SmartResponseAgent`. The Smart agent then runs the delegation loop (`DelegationEngine`) with an execution context
(provider, model, tier, thinking effort) that `TaskExecutionResolver` derives from the classification and from the
user's own overrides. The router decides *what kind of task*; the resolver decides *how to run it*; neither knows the
other's internals.

Quick is the other orchestrator in the block. It no longer receives routed traffic: `RouterAgent._apply_routing_rules`
always returns the Smart agent. Quick runs only as the emergency fallback when Smart fails and as the formatter of
background notifications (see Flows). `WebSearchLightAgent` is a one-call grounded lookup used by the voice companion,
not by Quick or Smart.

What it is not: it does not choose specialists (the orchestrating LLM does, through the registry, see
[agent_registry](../agent_registry/README.md)), it does not own the specialists' behaviour
and it does not own provider and model tables
([provider_resolution](../provider_resolution/README.md)).

## Owned data

The block stores nothing. It reads these `UserBotConfig` fields and produces these message-context keys.

**Read from the user's config** (`src/domain/user.py::UserBotConfig`): `complexity_settings_overrides` (per
`TaskComplexity`, a `ComplexitySettings`), `agent_tiers` (default tier per agent type, `_DEFAULT_AGENT_TIERS`),
`agent_providers` and `provider_preference` (provider choice), `model_overrides` (a model pin per agent type),
`agent_thinking` (thinking effort per agent type). Only `tier` of a `ComplexitySettings` is required. Every
`TaskComplexity` has an entry in `DEFAULT_COMPLEXITY_SETTINGS`.

These are read from the `UserBotConfig` snapshot the user's agents took when they were built
(`UserAgentFactory`). A Firestore edit of any of them reaches Smart only after the user's agents are rebuilt: run
`$admin_cache_reset` as a command (see Invariants). A blank `thinking_effort` or `provider_override` is coerced to
`None` and a non-dict `intent_remap` to `{}` (`ComplexitySettings` validators), so a blank console field inherits the
default.

**Written by the router into the routed message's context** (`RouterAgent.execute`): `classification` (the raw triage
dict), `routing` (`RoutingMetadata.to_dict()`), `task_complexity` (the enum's string value, so it survives a Cloud
Tasks hop), `enriched_context` (the prefetched facts, as a dict), `agent_notes` (active reminders, one dict per note,
`RouterAgent._note_context`), `routed_by`.

`RoutingMetadata` (`src/domain/agent.py`) is built from the triage dict by `src/domain/tone.py::build_routing_metadata`.

## Flows

### 1. Routed chat turn

1. `ConversationHandler` sends the message to `router_agent_{user_id}` (a bound channel skips the router and delegates
   straight to the bound intent).
2. `RouterAgent.execute` loads the last `RouterAgentConfig.context_window` messages and starts the memory prefetch
   (`RouterAgent._start_prefetch`): the slots agent (`FactQuerySlotsAgent.query_for_messages`, `path="router"`) reads
   the same messages as triage, in parallel with it (`FACT_PROFILE_RFC` §5.4).
3. Triage (`RouterAgent._classify_with_llm`): one JSON call on the router's own context (tier from
   `_DEFAULT_AGENT_TIERS["router"]`, provider from `AgentProviderStrategy.STRATEGIES["router"]`), prompt from `PromptBuilderPort.build_for_agent(agent_type="router")`, schema
   `RouterAgent.TRIAGE_RESPONSE_SCHEMA`, text parts only (an image would make the model answer instead of classify).
   The triage dict also carries `search_intent`, read in step 6.
4. `build_routing_metadata` turns the dict into `RoutingMetadata`. An unknown or empty complexity becomes
   `simple_analytics` (`src/domain/tone.py::_safe_complexity`, mirrored by `src/domain/agent.py::_coerce_task_complexity`).
5. Vision override: a native binary image in the current message forces `deep_reasoning`. A `file_data` entry that
   has only a storage `ref` and a non-image type is a file label, not vision.
6. Prefetch gate: `search_intent == "none"` cancels the prefetch; any other value (a missing one reads as `"topic"`)
   awaits it (`RouterAgent._finish_prefetch`: scored search of the slots query, minus the biographical baseline,
   capped at the user's semantic search limit). Any failure returns no facts and the turn goes on.
7. Active reminders come from `AgentNotePort.list_active_notes`; a failure is logged and the list is empty.
8. The routed message goes to the Smart agent through `AgentCoordinator.route_message`.

If the router has no LLM, or the triage call raises, `RouterAgent._classify_request_with_fallback` uses the keyword
classifier `RouterAgent._classify_request`: the dict has no `metadata`, so `build_routing_metadata` derives
`small_talk` (simple phrase) or `simple_analytics` (everything else) and the tone `friendly`.

### 2. Resolving the execution context

1. `SmartResponseAgent.execute` calls `_resolve_effective`, once per call, with no mutation of `self`
   (`decisions/per_call_execution_context.md`). Priority: an `ExecutionOverride` object already in
   `message.context["execution_override"]`, else `TaskExecutionResolver.resolve` on `context["task_complexity"]`, else
   the agent's own context.
2. `TaskExecutionResolver.resolve` parses the string (an invalid one logs `invalid_task_complexity` and returns
   `None`), takes `resolve_complexity_settings(complexity, config)`, and asks
   `AgentContextBuilder.resolve_for_task` to build the context for `settings.tier`, with
   `settings.provider_override` winning over the 3-level provider choice (`decisions/two_layer_provider_override.md`).
3. Thinking effort: `message.context["thinking_effort"]`, else the override's `thinking_effort` when it is not `None`,
   else `agent_thinking["smart"]`.
4. `SmartResponseAgent._run` runs `DelegationEngine.execute` with that context for every turn of the loop.

Background work reaches step 1 without the router: `UserNotificationService.notify` and the reminder and daily email
review tasks put `task_complexity` into the context themselves, and reminders address the
Smart agent directly (`src/services/execute_reminder_task.py`).

### 3. The delegation loop

`DelegationEngine.execute` (Quick and Smart share it; Lelik and bound agents use it or `dispatch`):

1. The orchestrating LLM sees one delegation tool, `delegate_to_specialist` (`DELEGATE_TOOL_NAME`), with an intent
   argument, the terminal tool `deliver_response`, and, for Smart, local tools (skills, workspace, memory proposals;
   `SmartResponseAgent._run`). The intents offered are `AgentRegistry.get_available_intents_for(descriptor)`:
   every non-internal intent when `allowed_intents is None` (Quick and Smart both). Smart adds one internal intent
   itself, the task specialist's `work_with_external_tasks`, only on the owner's own interactive turn of a user with
   the external task queue switched on (`docs/05_building_blocks/external_task_queue/README.md`).
2. All tool calls of one turn run in a single `asyncio.gather` (`DelegationEngine._execute_tool_calls`). There is no
   memory-first ordering; a specialist that needs another's result is called in a later turn.
3. `DelegationEngine.dispatch` applies `intent_remap`, then `intent_fanout`, then the coordinator. Both descriptors have
   an empty remap and the same fan-out: `search_web` also dispatches `maps_query` in parallel
   (`src/infrastructure/agent_manifest.py::SEARCH_WEB_MAPS_FANOUT`). The merged tool result labels each section and
   carries the reconciliation hint. The first intent is primary: its failure is shown to the LLM. A failed secondary is
   skipped silently.
4. A failed single delegation returns `Tool note: ...` to the LLM so it can correct its input; the loop is bounded by
   `QUICK.max_delegation_turns` / `SMART.max_delegation_turns` (`src/infrastructure/agent_config.py`); under a turn
   clock Smart is bounded by `clock.max_loop_turns` instead (`src/domain/turn_clock.py`). Exhausting the turns returns
   `DelegationResult(failed=True)`; both agents turn that into `TURN_CAP_ERROR`.
5. A turn that calls `deliver_response` ends the loop. Calls emitted beside it are still dispatched, their results are
   not read (`decisions/terminal_tool_co_emitted_calls.md`).

### 4. Quick: fallback and notifications

- Fallback: `ConversationHandler._route_with_fallback` calls `AgentFallbackService.try_quick_fallback` when the routed
  turn did not succeed (and a long turn has not passed its mark). Quick gets the user's text plus a `fallback_note`
  so it apologises in the user's language; if Quick fails too, the caller gets a static apology.
- Notifications: `UserNotificationService.notify` addresses `quick_response_agent_{user_id}` unless
  `agent_id_override` names another agent.
- Quick's context is the fixed one `AgentContextBuilder.build("quick", ...)` gives (tier from `_DEFAULT_AGENT_TIERS["quick"]`,
  provider from `AgentProviderStrategy.STRATEGIES["quick"]`), never the complexity-resolved one. Its prompt is `agent_type="quick"`.

### 5. Provider rotation (Smart)

A terminal `TranscriptLockedError` (a provider outage in the middle of a locked tool transcript, after the same-provider
retry) re-runs the whole turn on the next allowed provider at the same tier
(`TaskExecutionResolver.next_provider_override`, `AgentContextBuilder.resolve_next_provider`). When the list is
exhausted the failure goes to the Quick fallback. A long-running turn does not rotate: its budget is one clock
(`decisions/cross_provider_execution_retry.md`).

### 6. Web search light

`WebSearchLightAgent.execute` makes one `use_grounding` call on the `web_search_light` context (tier from `_DEFAULT_AGENT_TIERS`) with the
`websearch_light` prompt profile, without biography or standing directives (a search model should not see them). It
answers `search_web_light`, an `internal=True` intent that only an explicit `allowed_intents` names; Lelik is the one
that does (`src/infrastructure/agent_manifest.py::LELIK`).

## Invariants and traps

- **Router always routes to Smart.** `RouterAgent._apply_routing_rules` ignores the metadata. Reviving a cheap Quick
  path means model-tier rework, not a routing flip (`decisions/quick_agent_deferred_deletion.md`).
- **The router never fails the turn on enrichment.** Slots, search, baseline and reminder loads each catch and log;
  the turn goes on without that part. The router has `NO_RETRY_POLICY` on purpose: triage latency is paid on every
  message.
- **The triage prompt is cached on the `RouterAgent` instance** (`_cached_triage_prompt`). A changed router token
  reaches a user only after the agent is rebuilt: `$admin_cache_reset` as a command
  (`ConversationHandler.handle_command`) clears the prompt cache and evicts the user's agents. The text reaching
  `RouterAgent._handle_admin_cache_reset` clears the prompt cache only. Without a reset the cache expires by idle time
  (`UserAgentFactory._cache_ttl`, refreshed on every use), so a user who writes at least once per TTL never gets
  rebuilt agents: a changed router token or config never reaches them.
- **A complexity default always applies when `task_complexity` is present.** `agent_tiers["smart"]` is the tier only
  when the context carries no (valid) complexity. A `model_overrides` pin beats any tier in both cases
  (`AgentContextBuilder._build`).
- **Merge is per field, one function.** `resolve_complexity_settings` is the only merge of user overrides onto the
  defaults; the reminder SLA clock uses it too, so the clock and the work agree
  (`src/services/execute_reminder_task.py`; the split reading killed a reminder at the wrong budget). A partial
  override keeps the default `thinking_effort`; `thinking_effort` of `None` in an override inherits the default
  (`tests/unit/infrastructure/test_task_execution_resolver.py`).
- **An override entry without `tier` is dropped on load** (`src/adapters/firestore_user_repo.py::_sanitize_user_doc`),
  so one blank console edit cannot block the user's messages.
- **`provider_override` is a `ProviderRegistry` key, not a vendor name** (the Claude provider is `claude`), and
  `resolve_for_task` does not check it against the strategy's allowed providers; an unknown name raises `ValueError`
  from `ProviderRegistry.get`.
- **`ComplexitySettings.intent_remap` is dead.** The resolver copies it into `ExecutionOverride`, but
  `SmartResponseAgent` passes no remap to the engine, not even its descriptor's. Only Quick passes its descriptor's
  `intent_remap`; a remap added to `SMART_RESPONSE` would be dead too.
- **Thinking effort means different things per adapter, and some models skip it.** The mapping lives in the adapters
  (`src/adapters/CLAUDE.md`), not here.
- **Engine text to the model says `Tool note:`/`Tool error:`**, never `SYSTEM:` (the fan-out preamble, the
  unknown-tool result); current-turn system text goes through `wrap_system` with the turn key. Quick's own inputs do not
  all follow this yet: the notification frame in `UserNotificationService` is a bare `[System: ...]`.
- **`task_complexity` travels as a string** in `message.context`, never the enum, so it survives serialisation.
- **Quick's fallback role is why its config is fixed.** Do not make it depend on the complexity resolver: it is the last
  LLM-backed line before the static apology.

## Links

- RFC: [`TASK_COMPLEXITY_EXECUTION_SETTINGS_RFC.md`](../../10_rfcs/TASK_COMPLEXITY_EXECUTION_SETTINGS_RFC.md),
  `FACT_PROFILE_RFC.md` §5.4 (the prefetch), `VOICE_COMPANION_RFC.md` §4.15 (web search light).
- Decisions (`docs/04_solution_strategy/decisions/`): `per_call_execution_context.md`, `two_layer_provider_override.md`,
  `cross_provider_execution_retry.md`, `quick_agent_deferred_deletion.md`, `terminal_tool_co_emitted_calls.md`.
- Neighbours: [agent_registry](../agent_registry/README.md),
  [provider_resolution](../provider_resolution/README.md), [memory](../memory/README.md),
  [prompt_design_system_v3](../prompt_design_system_v3/README.md).

## Where to start reading

- `src/agents/core/router_agent.py::RouterAgent.execute` — the whole routed turn.
- `src/domain/tone.py::build_routing_metadata` — triage dict to `RoutingMetadata`.
- `src/domain/complexity_settings.py::resolve_complexity_settings` — defaults plus user overrides.
- `src/infrastructure/task_execution_resolver.py::TaskExecutionResolver` — complexity string to `ExecutionOverride`.
- `src/agents/core/smart_response_agent.py::SmartResponseAgent._resolve_effective` — the per-call context.
- `src/infrastructure/delegation_engine.py::DelegationEngine.execute` — the loop; `DelegationEngine.dispatch` for one call.
- `src/agents/core/quick_response_agent.py::QuickResponseAgent.execute` — the fallback and notification path.
