<!-- living-doc
covers:
  src/infrastructure/agent_registry.py
  src/infrastructure/agent_manifest.py
  src/infrastructure/agent_coordinator.py
  src/infrastructure/agent_config.py
  src/agents/base_agent.py
  src/agents/agent_runtime.py
  src/domain/agent.py
  src/domain/delegation_timestamp.py
  src/domain/retry_policy.py
  src/handlers/agent_worker_handler.py
  src/composition/user_agent_factory.py
  src/ports/agent_factory_port.py
  src/services/agent_fallback_service.py
  src/utils/circuit_breaker.py
  src/utils/retry.py
-->
# Agent Registry and the Multi-Agent Runtime

## Purpose

This block is how one chat turn becomes work done by specialist agents. Smart, Quick and the bound-channel agents
(Lelik, the tutor, the domain researcher) each hold one LLM tool, `delegate_to_specialist(intent, query, context)`. They
know intent names, never implementations. The registry maps an intent to the agent that owns it; the coordinator
dispatches the call, inline or through Cloud Tasks; the per-user factory builds the agent on first use. A new specialist
is one descriptor in `src/infrastructure/agent_manifest.py` plus a factory builder: no orchestrator changes.

It also owns what every agent shares: the message and response contract (`AgentMessage`, `AgentResponse`,
`DeliveryItem`), the execution wrapper (circuit breaker, typed retry, timeout), the worker that runs ASYNC intents, and
the fallback chain that guarantees a displayable answer.

Not here: the multi-turn tool loop itself (`DelegationEngine`, see [hybrid_router](../hybrid_router/README.md) and
[hybrid_router](../hybrid_router/README.md)), per-agent behaviour (each specialist's own block or
`src/agents/CLAUDE.md`), provider failover (see [provider_resolution](../provider_resolution/README.md)), and the
step list for adding an agent ([`NEW_AGENT_PLAYBOOK.md`](../../how_to/NEW_AGENT_PLAYBOOK.md)).

## Owned data

Nothing is stored. State is in memory, per process:

- **Registry**: `AgentRegistry._agents` (agent id to descriptor) and `_intent_to_agent`. Filled once at startup from
  `src/infrastructure/agent_manifest.py::ALL_DESCRIPTORS` by `src/composition/application_graph.py::_build_coordinator`.
- **Coordinator**: `AgentCoordinator.agents`, the live per-user instances, keyed `{base_agent_id}_{user_id}`.
- **Factory cache**: per user, a profile plus the built agents, a TTL swept periodically (`UserAgentFactory._cache_ttl`, `_evict_expired_cache`), plus a per-user
  generation counter that eviction bumps (`UserAgentFactory._evict_user`).
- **Circuit breaker**: failure counts per agent id (`src/agents/base_agent.py::CircuitBreaker`), one instance per agent.

`AgentDescriptor` (`src/infrastructure/agent_registry.py`) has two halves. Part A is what an agent offers (its
intents and their execution modes, descriptions, typed `context_schemas`, `internal`, `eager`, the ASYNC dispatch
deadline, the file prefetch opt-out). Part B is what an orchestrator may call (`allowed_intents`, `intent_remap`,
`intent_fanout`: secondary intents dispatched in parallel with the primary). The two orchestrator
descriptors (`QUICK_RESPONSE`, `SMART_RESPONSE`) are class attributes of their agents and are not in `ALL_DESCRIPTORS`.

The wire contract is in `src/domain/agent.py`: `AgentMessage` (its `AgentIntent`), `AgentResponse` (its
`AgentStatus`, `delivery_items`, `history_context`) and `AgentConfig` (timeout, breaker threshold and recovery).

## Flows

### 1. Which intents an LLM sees

1. `src/infrastructure/agent_coordinator.py::AgentCoordinator.get_available_intents_for` delegates to
   `AgentRegistry.get_available_intents_for(descriptor)`.
2. `allowed_intents is None` gives every non-internal intent. A frozenset gives exactly the registered intents it names,
   internal ones included. `internal=True` means "not offered by default", not "unreachable"
   (`decisions/allowlist_names_internal_intent.md`).
3. `BaseAgent._build_delegate_tool_declaration` renders them into the one tool, merging every `context_schemas` field.

### 2. A SYNC delegation

1. The orchestrator calls `AgentCoordinator.handle_delegation(intent, query, context, calling_agent_id)`.
2. The registry resolves the intent to a descriptor. An unknown intent returns a `FAILED` response ("No agent
   registered for intent") with the available intent names as suggestions.
3. The cycle guard (`_refuse_if_looping`) refuses a target already in the call chain (`CALL_CHAIN_KEY` in the context)
   or a chain deeper than `MAX_DELEGATION_DEPTH`, logs ERROR and posts to the alert sink
   (`decisions/delegation_cycle_guard.md`).
4. A non-eager agent is built first (flow 4).
5. The query gets a UTC timestamp prefix (weekday, date, year, time) so specialists resolve relative dates against the right
   year. `src/domain/delegation_timestamp.py::strip_delegation_timestamp` removes it where the raw text is needed.
6. `_execute_sync` prefetches a `file_ref` into `file_content` or `file_note` (`prefetch_file_ref`, unless the descriptor
   opts out), then routes an `AgentMessage(QUERY)` with payload `{query, intent, **params}` to
   `{agent_id}_{user_id}` through `route_message` and `BaseAgent.process` (flow 5).

The execution mode is the manifest's. The caller cannot choose it (`decisions/delegate_mode_parameter_removed.md`,
`tests/unit/infrastructure/test_delegation_mode_is_the_manifests.py`).

### 3. An ASYNC delegation

1. `handle_delegation` steps 1 to 5 as above; then `_execute_async` enqueues a Cloud Task through `TaskQueue`
   (`enqueue_agent_task`, with the descriptor's deadline) and returns `{"status": "started", "task_name": ...}`. The
   caller tells the user the result arrives separately.
2. `/worker` hands `task_type="agent_execution"` to `src/handlers/agent_worker_handler.py::AgentWorkerHandler.handle_task`.
   It opens the context in a `RequestContext` (a task carries none), prefetches the `file_ref` just before the
   specialist runs (the task carries the ref, never file text), and routes an `AgentMessage(DELEGATE)` to
   `{agent_id}_{user_id}`.
3. Delivery is a branch on the intent inside `handle_task`: the deep-research runner, DOCX, and PDF, HTML page and image
   `document` items each have their own deliverer; a failure notice exists only for those and for `send_to_chat`.

### 4. A lazy agent, and eviction

1. `eager=False` descriptors are registered at startup (their intents are visible at once) but built on first use. The
   coordinator calls `AgentFactoryPort.create_agent_on_demand(agent_type, user_id)` from `handle_delegation`, and from
   `route_message` through `_try_lazy_load` for a recipient that is not registered (a Cloud Task callback).
2. `UserAgentFactory.create_agent_on_demand` looks up `_LAZY_BUILDERS[agent_type]`, then under the per-user lock builds, binds the
   runtime (`BaseAgent.bind_runtime` with `AgentRuntime`) before registering the agent, and records its id in
   `_lazy_agent_ids` for eviction.
3. Eviction (`$admin_cache_reset`, the TTL sweep) takes no lock, so it bumps the user's generation. The eager build
   (`_build_unless_overtaken`) is redone when an eviction overtook it, at most `_MAX_BUILD_ATTEMPTS` times, keeping the last.
   The lazy build re-reads the cache entry under the lock instead and returns `False` after `_MAX_BUILD_ATTEMPTS` evictions.
   An eager build that is cancelled (or fails) after registering its agents and before writing the cache entry unregisters
   exactly the agents it registered and re-raises, so no orphan registration blocks the next build.
4. A turn that was delegating during an eviction finds its eager agents unregistered. `_try_lazy_load` sees an eager
   descriptor and calls `restore_agents_for_user`, which rebuilds the set, instead of answering "Unknown recipient".

### 5. One execution

`src/agents/base_agent.py::BaseAgent.process` wraps every `execute()`:

1. Breaker check: `AgentConfig.circuit_breaker_threshold` failures open it for `circuit_breaker_recovery_ms`; an open
   breaker answers `FAILED` without running the agent.
2. `can_handle()`; a false answer is `CANNOT_HANDLE` with `_get_alternative_agents()` as suggestions.
3. `_run_retry_loop` runs `execute()` under `timeout_ms` (the message's overrides the config's). Only
   `LLMRateLimitError` and `LLMUnavailableError` are retried, with `RetryPolicy` (`src/domain/retry_policy.py`) through
   `src/utils/retry.py::retry_async`. A timeout is never retried (`TIMEOUT` response), nor is any other exception.
   `CancelledError` propagates and does not count as a failure.
4. Outcome: any `AgentResponse` that `execute()` returns, `FAILED` included, resets the breaker; only an exception or
   timeout raised out of `execute()` records a failure. An agent that turns its own errors into `AgentResponse.failure`
   never opens its breaker.

Retry is switched off for an execution under an outer retry (`context["suppress_transient_retry"]`, set by Cloud Task
callers that return 5xx) and for the orchestrator of a clocked long turn (`BaseAgent._effective_retry_policy`).
Agents whose retry would redo billed or long work set `RETRY_POLICY = NO_RETRY_POLICY` (the generators, the router, the
deep-research runner). Decision: `decisions/typed_retry_policy.md`.

### 6. Failure of the main agent

`ConversationHandler._route_with_fallback` calls `AgentFallbackService.try_quick_fallback` after the router answer
(only before the turn's long-turn mark, `decisions/long_running_turns.md`). Any non-`SUCCESS` status goes to Quick directly
(bypassing the router) with `context["fallback_note"]`, which `QuickResponseAgent` appends in memory only, wrapped with `wrap_system` and the turn's key, so it is never persisted and speaks as a keyed system note (`src/agents/core/quick_response_agent.py::QuickResponseAgent`); an ops alert is posted when a sink is wired. If Quick also fails or raises, the service
returns a synthetic `SUCCESS` carrying `AgentFallbackService._APOLOGY_TEXT`, so the user never sees an error status.
The service is built in `build_application_graph`; without it the turn returns the raw failure.

### 7. What a specialist hands back

- **`history_context`**: a specialist that wants its result visible to the LLM on later turns sets
  `AgentResponse.history_context = {"<name>_context": value}`. The delegation engine accumulates it generically and
  `ConversationHandler._prepare_history_texts` writes every metadata key ending in `_context` (via
  `src/domain/history_context_block.py::history_context_block`) into the model message's `full_text`, never into the
  summary text. Whether the LLM sees it depends on history tiering: `BaseAgent._apply_history_tier` keeps full text for
  recent model turns only.
- **`delivery_items`**: typed artifacts delivered after the text. The orchestrator extends them without looking inside.
  The synchronous consumer is `ConversationHandler._deliver_item` (one branch per `type`); ASYNC results are delivered by
  `AgentWorkerHandler`. A new type is one branch in the consumer that will see it.
- **History summary**: Smart returns `metadata["response_summary_task"]`; `_prepare_history_texts` awaits it under
  `asyncio.shield` with a short timeout after the reply is sent, and falls back to the full text.

## Invariants and traps

- **Orchestrators never change for a new specialist.** Both read intents from the registry; the descriptor tests
  (`tests/unit/infrastructure/test_agent_manifest.py`, `test_registry_internal_allowlist.py`) check the descriptor fields
  and allowlist filtering, not the orchestrators.
- **`ALL_DESCRIPTORS` is the whole registry.** An agent built by the factory but missing from it has no intent, so no
  delegation reaches it. The consolidation agent and the deep-research runner are such agents: they are addressed by
  agent id (`/worker` payload, `route_message`), not by intent.
- **An intent belongs to one agent.** `AgentRegistry.register` logs a WARNING and the later registration wins.
- **`context_schemas` fields are merged by name and the first intent wins.** Two intents declaring the same field must
  describe it identically (`FILE_REF_DESCRIPTION`), or one description is silently lost.
- **The intent to `agent_type` link is by string.** `_LAZY_BUILDERS` and `_LAZY_AGENT_IDS` are keyed by the descriptor's
  `agent_type`; a mismatch makes `create_agent_on_demand` log "No lazy builder" and return `False`, and the delegation
  then fails at routing.
- **A new eager agent must be added to `UserAgentFactory._EAGER_AGENT_KEYS`.** Eviction unregisters only the keys listed
  there; a missing one stays registered and later registrations are refused as duplicates (logged ERROR), so a reset
  keeps serving the old config.
- **`bind_runtime` before `register_agent`.** An agent routable without its coordinator, content store and quota service
  runs unbilled and uncaptured (`tests/unit/composition/test_agent_registration_window.py`).
- **An ASYNC agent that returns `DeliveryItem`s needs a branch in `AgentWorkerHandler.handle_task`.** Without one the
  task succeeds and nothing is delivered; the worker, not the agent, sends them. An agent that posts to the chat itself
  (`send_to_chat`, through `AlekGatewayAgent`) needs only the failure notice.
- **The native-format declaration travels only to file-opening intents.** `_specialist_context` strips
  `NATIVE_FORMATS_KEY` for every intent outside `NATIVE_OPENING_INTENTS`, in message and in task body
  (`tests/unit/infrastructure/test_coordinator_native_formats.py`).
- **A gathered child's failure is that child's.** In the engine a specialist's exception or its own `CancelledError`
  becomes a failed tool result; only a cancel of the turn itself propagates.
- **Two classes are named `CircuitBreaker`.** The agent breaker is `src/agents/base_agent.py::CircuitBreaker`
  (per agent id, thresholds from `AgentConfig`). `src/utils/circuit_breaker.py::CircuitBreaker` is a different, async,
  call-wrapping breaker used by the Slack and Telegram adapters; it never guards an agent.
- **`BaseAgentConfig.history_full_turns` is dead.** Nothing reads it; the live value is the literal
  `BaseAgent.HISTORY_FULL_TURNS`, and the two disagree. Editing `src/infrastructure/agent_config.py` does not change
  history tiering.
- **Bot-written text is never the owner's words.** Only user-role text, including the legacy
  `MessagePart.consolidation_text`, counts as the owner's; the serializer
  (`src/domain/consolidation_serialization.py`) reads model text (the summary, or `full_text` with the `*_context` blocks
  in full mode) as the bot's. Facts from a document go through the owner-confirmed path (`decisions/memory_facts_owner_confirmed.md`).

## Links

- [`ACP_V2_SIMPLIFIED_RFC.md`](../../10_rfcs/ACP_V2_SIMPLIFIED_RFC.md): the registry design (history).
- Decisions: `decisions/allowlist_names_internal_intent.md`, `delegation_cycle_guard.md`, `delegate_mode_parameter_removed.md`,
  `delegate_tool_contract_and_strict.md`, `typed_retry_policy.md`, `transcript_integrity_one_provider.md`,
  `long_running_turns.md` (all under `docs/04_solution_strategy/decisions/`).
- [hybrid_router](../hybrid_router/README.md),
  [observability_strategy](../observability_strategy/README.md), [document_generation](../document_generation/README.md).
- Adding an agent: [`NEW_AGENT_PLAYBOOK.md`](../../how_to/NEW_AGENT_PLAYBOOK.md); the roster and per-agent traps:
  `src/agents/CLAUDE.md`.

## Where to start reading

- `src/infrastructure/agent_manifest.py::ALL_DESCRIPTORS`: every agent and intent, with `Intent`.
- `src/infrastructure/agent_registry.py::AgentRegistry`: intent lookup and the `internal` rule.
- `src/infrastructure/agent_coordinator.py::AgentCoordinator.handle_delegation`: the dispatch path.
- `src/composition/user_agent_factory.py::UserAgentFactory.create_agent_on_demand`: lazy build and eviction.
- `src/agents/base_agent.py::BaseAgent.process`: breaker, retry, timeout.
- `src/handlers/agent_worker_handler.py::AgentWorkerHandler.handle_task`: ASYNC execution and delivery.
- `src/services/agent_fallback_service.py::AgentFallbackService.try_quick_fallback`: the degradation chain.
