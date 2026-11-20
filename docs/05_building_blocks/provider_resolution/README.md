<!-- living-doc
covers:
  src/services/provider_registry.py
  src/services/agent_context_builder.py
  src/services/alerting_llm_proxy.py
  src/services/provider_breaker_alerter.py
  src/adapters/in_memory_provider_resilience.py
  src/ports/provider_resilience_port.py
  src/domain/native_format.py
  src/domain/llm.py
  src/ports/llm_port.py
  src/adapters/claude_adapter.py
  src/adapters/gemini_adapter.py
  src/utils/genai_sdk_compat.py
-->
# Provider Resolution

## Purpose

This block keeps agent code independent of LLM vendors. An agent asks for a **performance tier**
(`src/domain/user.py::PerformanceTier`); the block turns the request, the user's configuration and the agent type into
a concrete provider, model and failover plan, delivered as one `AgentExecutionContext`
(`src/ports/llm_port.py::AgentExecutionContext`). It also owns what happens when a provider fails (circuit breaker,
fallback, rotation), the per-model capability gates inside the adapters, and the declaration of which file formats a
model reads natively.

It is not the model catalogue (the tier maps live in each adapter), not the price list (`src/domain/billing.py`), and
not the prompt cache policy ([prompt_cache_strategy](../prompt_cache_strategy/README.md)). Per-request tier and provider
overrides chosen by the router are described in [hybrid_router](../hybrid_router/README.md).

## Owned data

Nothing is stored. All state is in process memory:

- **Registry** — `ProviderRegistry` maps a lowercase name to an adapter. There are separate instances for the text LLMs
  (`ServiceContainer`: `gemini` and `claude` always, `grok` and `openai` when their keys are set), for deep research,
  image and video (`src/composition/application_graph.py`), and for the drive indexer service
  (`src/composition/drive_model_setup.py::build_drive_model_registry`, Claude, OpenAI and Gemini only).
  `ProviderRegistry.get` raises `ValueError` for an unregistered name.
- **Strategy table** — `AgentProviderStrategy.STRATEGIES`, one entry per agent type: `default_provider`,
  `allowed_providers`, `required_capabilities`, `fallback` (a provider name or `None`) and an optional `fallback_tier`.
  An unknown agent type gets the `quick` entry. This table is the only place a provider is pinned or locked: an agent
  that must run on one provider has a single-element `allowed_providers`, either because its output format only that
  provider honours (`postprocessing`, whose `response_schema` is Gemini-only) or by a measured-quality choice
  (`email_card`, `fact_query_slots`, the memory-search slots call).
- **Breaker state** — `InMemoryProviderResilience`, per provider name and per process, shared by all agents: a rolling
  window of failure times and an `opened_at` mark. It is lost on restart and not shared between Cloud Run instances.
- **Tier maps** — `MODEL_TIERS` on each adapter (`ClaudeAdapter.MODEL_TIERS`, `GeminiAdapter.MODEL_TIERS`,
  `OpenAIAdapter.MODEL_TIERS`, `GrokAdapter.MODEL_TIERS`); `ClaudeAdapter.__init__` and `OpenAIAdapter.__init__` let
  environment variables (`CLAUDE_PERFORMANCE_MODEL`, `CLAUDE_SMALL_MODEL`, `OPENAI_TIER_OVERRIDES`) repoint tiers
  without a redeploy.

## Flows

### Resolving a context

`AgentContextBuilder.build(agent_type, config)` (`src/services/agent_context_builder.py`):

1. **Provider name** — `AgentContextBuilder.resolve_provider_name`: the user's per-agent choice
   (`UserBotConfig.agent_providers`), else the global `provider_preference`, else the strategy default. A value that is
   not in the strategy's `allowed_providers` is **silently skipped**, not an error; names are lowercased first.
2. **Tier** — `UserBotConfig.get_tier_for_agent`: the stored per-agent tier, else `_DEFAULT_AGENT_TIERS`
   (`src/domain/user.py`), else `default_tier`. An agent type with no `_DEFAULT_AGENT_TIERS` entry gets the user's
   `default_tier`, so a new agent that needs a specific tier needs its entry there.
3. **Model** — `UserBotConfig.model_overrides[agent_type]`, else `provider.get_model_for_tier(tier)`.
4. **Wrapping** — `AgentContextBuilder._build` wraps the provider in `CachingLLMProxy` when the cache strategy returns a
   config for the agent type, then in `AlertingLLMProxy` (outermost, so it sees the real provider's errors).
5. **Fallback** — the strategy's `fallback`, when it differs from the primary: resolved at build time to a provider and
   model, at `fallback_tier` if the strategy pins one, otherwise at the primary's tier. It gets the alerting proxy but not
   the caching proxy (a cache is useless after a provider switch). A fallback that cannot be resolved is logged
   (`llm_fallback_provider_unavailable`) and the context is built without one.

`resolve_for_task` is the same build with the tier taken from `ComplexitySettings.tier` instead of config; if
`ComplexitySettings.provider_override` is set it replaces the provider name and `agent_providers` and `provider_preference`
are not consulted. Smart applies it per request; the per-request override pattern is in
[hybrid_router](../hybrid_router/README.md).
Agents whose work is not an `LLMPort` call use the same name resolution against another registry:
`resolve_async_context` (deep research; a per-agent override outside `allowed_providers` raises
`UnsupportedProviderError` instead of falling back), `resolve_image_generation_context`,
`resolve_video_generation_context`.

### Runtime failover

`BaseAgent._call_llm` (`src/agents/base_agent.py`) is the one place a provider failure is handled. Adapters translate SDK
errors into domain types (`LLMRateLimitError`, `LLMUnavailableError`, ...), and only the set
`src/domain/exceptions.py::FAILOVER_TRIGGER_TYPES` triggers any of what follows; a 4xx (`LLMClientError`) never does and
is alerted by `AlertingLLMProxy`. The governing invariant is **one transcript = one provider**
(`decisions/transcript_integrity_one_provider.md`): a multi-turn transcript carries provider-specific state (tool ids,
thinking blocks, cache), so it is never re-served on another provider.

1. **Breaker pre-check.** If `ProviderResiliencePort.is_provider_open(primary)`, the call is not made and
   `ProviderBreakerOpenError` takes the failure path (it is not counted as a new failure, which would feed back on
   itself). Success calls `record_success`, except when the breaker tripped during the call.
2. **Locked transcript** (any tool part or `raw_content` in `request.messages`): retry the same provider up to
   `BaseAgent._SAME_PROVIDER_RETRY_ATTEMPTS` times, then raise `TranscriptLockedError`. Never switches provider.
3. **Unlocked request**: fail over once to `ctx.fallback_provider`. No fallback configured, or its breaker open, raises
   `BothProvidersUnavailableError`; so does a failing fallback. A fallback success deliberately does **not** call
   `record_success(fallback)`, because the reset would erase its accumulated failures after one lucky call
   (`BaseAgent._call_llm` docstring).
4. **Smart rotation.** `SmartResponseAgent.execute` (`src/agents/core/smart_response_agent.py`) catches
   `TranscriptLockedError` and asks `AgentContextBuilder.resolve_next_provider` (via
   `TaskExecutionResolver.next_provider_override`) for the next entry of `allowed_providers` that was not tried and whose
   breaker is closed, at the same tier, then re-runs the whole delegation from scratch. A turn past the long-turn mark
   (`clock.marked`) does not rotate. Details: `decisions/cross_provider_execution_retry.md`.
5. **Quick fallback.** A failed Smart response goes to Quick, then to a synthetic apology
   (`src/services/agent_fallback_service.py::AgentFallbackService.try_quick_fallback`).

`InMemoryProviderResilience.record_failure` opens the breaker on the threshold-th failure inside the rolling window;
after the cooldown the next `is_provider_open` call reports closed again (half-open) and clears the window. On each
CLOSED to OPEN transition `ProviderBreakerAlerter.on_open` posts a throttled Slack alert: failover keeps users served, so
a chronically dead provider would otherwise stay invisible.

### Native-format declaration

What a model reads as file bytes instead of converted text (`docs/10_rfcs/EXTERNAL_FILES_RFC.md` §4.3).

1. `LLMPort.native_formats(model_name)` (`src/ports/llm_port.py`) returns a tuple of `NativeFormat`; the default is
   `()` (text only). Claude, Gemini, OpenAI and Grok override it by model-name prefix or id list; any other name gets
   `()`. The caching and alerting proxies forward the call. The Claude, Gemini and OpenAI constants cite the provider's documentation; a
   cap that the documentation does not state takes the smallest documented value of the others.
2. Each engine runner puts `to_plain(provider.native_formats(model))` under `context["native_formats"]`
   (`src/domain/native_format.py::NATIVE_FORMATS_KEY`) in a **copy** of its context, per attempt, so a fallback attempt
   on another provider declares its own formats. The runners are the agents that call an LLM and then
   delegate file work (grep `NATIVE_FORMATS_KEY` under `src/agents/`).
3. `src/infrastructure/agent_coordinator.py::_specialist_context` drops the key for every intent outside
   `NATIVE_OPENING_INTENTS` (`src/infrastructure/agent_manifest.py`): the declaration is the caller's provider's, not a
   specialist's.
4. `src/infrastructure/drive_operations.py::_declared_format` looks up the declaration by mime; its caller passes a file
   natively when the mime is declared, its size is within `max_bytes` and a PDF's page count within `max_pages`;
   otherwise the file goes to the converter as text. No key means text. `NativeFormat` refuses at construction a cap
   above `document_limits.native_ceiling`.

## Invariants and traps

- **A tier is not a model, and models differ in what they accept.** Changing a default tier (`_DEFAULT_AGENT_TIERS`) or a
  `MODEL_TIERS` entry changes the model, which may reject a parameter the agent sends (effort, thinking type,
  `response_schema`). Verify the resolved model against every parameter the agent sends. The Claude tier map and the env
  levers are in `src/adapters/CLAUDE.md`.
- **Silent parameter dropping, not 400s.** Each adapter gates parameters per model before serializing. No adapter sends
  sampling parameters at all (`decisions/temperature_removed.md`). Gates are prefix or substring matches, so a model id
  that is a prefix of another matches both: read every gate before adding a model.
  - `ClaudeAdapter._THINKING_MODELS`: adaptive thinking and `output_config.effort` go together; a model outside the tuple
    (Haiku 4.5) gets neither, because sending effort there is a 400. Public form: `ClaudeAdapter.takes_adaptive_thinking`;
    the deep-research runner takes the same gate through it (injected, never copied).
  - `ClaudeAdapter._DYNAMIC_SEARCH_MODELS`: other models get the legacy web-search tool.
  - `ClaudeAdapter._THINKING_OFF`, `ClaudeAdapter._NO_FORCED_TOOL_MODELS`, `ClaudeAdapter._MODEL_FALLBACK`: no-thinking
    value, forced-tool degradation and same-provider model fallback. `ClaudeAdapter._model_dependent_params` recomputes
    thinking, effort and `tool_choice` for the primary model and again for the fallback model; the fallback itself is
    chosen in the request path (traps in `src/adapters/CLAUDE.md`).
  - `OpenAIAdapter._MIN_MEDIUM_EFFORT_PREFIXES`, `_MIN_LOW_EFFORT_PREFIXES`, `_THINKING_OFF_EFFORT`: effort is clamped up
    to the model's floor after it is resolved, so the clamp covers both an explicit value and the
    grounding-forced `low`.
- **`required_capabilities` is declarative.** `_build` reads provider capabilities only to pass them on; it does not
  check them against the strategy. A provider in `allowed_providers` is assumed able to serve the agent.
- **Fallback is per strategy, rotation is not.** `fallback` governs unlocked failover only; rotation walks the whole
  `allowed_providers` list. Memory-quality agents have `fallback: None` by an owner ruling recorded in the
  comments of `AgentProviderStrategy.STRATEGIES`, so their outage is a failure, not a cheaper answer.
- **Fallback tier is part of the strategy.** An entry pins `fallback_tier` when the fallback must not run at the primary's
  tier: the drive indexer agents run at ECO, which on Gemini is flash-lite, so their Gemini fallback is pinned to BALANCED;
  the `drive` specialist runs at BALANCED and pins PERFORMANCE for its OpenAI fallback, whose BALANCED is a smaller model.
- **The breaker is per process.** With more than one Cloud Run instance each keeps its own view; the port is the seam for
  a shared implementation. The drive indexer service has its own builder and breaker.
- **Names are lowercased in two places** (`resolve_provider_name` and `_build`) because the registry keys are lowercase
  and a mixed-case user value reaches the registry as an unknown name.

## Links

- [prompt_cache_strategy](../prompt_cache_strategy/README.md), [hybrid_router](../hybrid_router/README.md),
  [openai_integration](../openai_integration/README.md), [grok_integration](../grok_integration/README.md)
- Decisions in `docs/04_solution_strategy/decisions/`: `transcript_integrity_one_provider.md`,
  `cross_provider_execution_retry.md`, `provider_resilience_port.md`, `temperature_removed.md`, `model_refresh_2026_10.md`
- `docs/10_rfcs/EXTERNAL_FILES_RFC.md` (native formats); `src/adapters/CLAUDE.md` (tier maps and gates);
  `docs/how_to/ADAPTER_WIRE_TESTING.md`

## Where to start reading

- `src/services/agent_context_builder.py::AgentProviderStrategy` and `AgentContextBuilder.build`
- `src/agents/base_agent.py::BaseAgent._call_llm`
- `src/agents/core/smart_response_agent.py::SmartResponseAgent.execute`
- `src/adapters/in_memory_provider_resilience.py::InMemoryProviderResilience`
- `src/adapters/claude_adapter.py::ClaudeAdapter._model_dependent_params`
- `src/ports/llm_port.py::LLMPort.native_formats`
