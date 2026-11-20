<!-- living-doc
covers:
  src/services/prompt_cache_strategy.py
  src/services/caching_llm_proxy.py
  src/ports/prompt_cache_strategy_port.py
-->
# Prompt Cache Strategy

## Purpose

This block decides, per agent, whether a request asks its LLM provider for prompt caching, and attaches that request
without the agent knowing. An agent declares only its type (`consolidation`, `smart`, ...); the strategy turns the type
and the provider's capabilities into a `PromptCacheConfig`, and a proxy around the provider writes that config into
every request. The provider adapter then does the provider-specific work (Anthropic breakpoints, OpenAI cache
parameters).

It is the *decision* and the *injection*. It is not the cut between static and dynamic prompt text (the assembler
places `PROMPT_CACHE_BOUNDARY`, the Claude adapter splits on it; see Flows) and it does not measure hits: billing
reads cached tokens from each adapter's usage parsing.

## Owned data

None. The block is stateless: no collection, no cache of its own. `PromptCacheConfig` (`src/domain/llm.py`) is the one
value it passes around: an on switch plus `cache_last_message` (also mark the messages of a multi-turn loop, not only
the system prompt). It has no TTL or scope field. The config is per request and never stored.

## Flows

### Resolving and wrapping (once per agent context)

1. `AgentContextBuilder._build` (`src/services/agent_context_builder.py`) resolves the provider for the agent type, then
   calls `PromptCacheStrategy.resolve(agent_type, capabilities)` with the provider's `ProviderCapabilities`.
2. `resolve` returns `None` when `capabilities.context_caching` is false or the type is not in
   `PromptCacheStrategy.CACHEABLE_AGENTS`; otherwise `PromptCacheConfig(enabled=True, cache_last_message=...)`, with
   `cache_last_message` true only for `PromptCacheStrategy.MULTI_TURN_AGENTS`. The reason a type is in or out is the
   class docstring: a multi-turn loop amortises the write surcharge, a single-turn call does not.
3. If a config came back, the builder wraps the provider in `CachingLLMProxy`, then wraps that in the alerting proxy
   (outermost). The fallback provider (`AgentProviderStrategy.STRATEGIES[...]["fallback"]`) is wrapped by the alerting
   proxy only: a cache is useless after switching provider.
4. The wiring is `ServiceContainer` in `src/composition/service_container.py`: it builds one `PromptCacheStrategy` and
   passes it to `AgentContextBuilder` together with `caching_proxy_factory=CachingLLMProxy`. The builder wraps only when
   it holds both.

### Injecting into a request (every call)

`CachingLLMProxy.generate_content` copies the request with the proxy's config when `request.cache_config` is empty and
forwards it. A request that already carries a `cache_config`, even `enabled=False`, is never overridden: that is how
one call opts out. Every other `LLMPort` method is delegated to the inner provider.

### What each adapter does with an enabled config

- **Claude** (`ClaudeAdapter.generate_content`): the system instruction is split at `PROMPT_CACHE_BOUNDARY`
  (`src/domain/llm.py`). The static prefix becomes a system block with `cache_control: ephemeral`; the text after the
  boundary becomes a second block with no mark, and is omitted when empty. With no boundary the whole instruction is one
  marked block. An empty instruction is never marked (Anthropic rejects empty text). With `cache_last_message`, `ClaudeAdapter._convert_messages`
  also marks the last content block of the last user message and of the previous user message (a rolling pair, so the
  chain survives a turn with more than 20 tool blocks). At most three marks are placed.
- **OpenAI** (`OpenAIAdapter.generate_content`): no markers. For models whose prefix is in
  `OpenAIAdapter._DEPRECATED_RETENTION_PREFIXES` it sends a `prompt_cache_key` hashed from the static prefix
  (`OpenAIAdapter._prompt_cache_key`); for older models `prompt_cache_retention`.
- **Gemini and Grok** declare `context_caching=False`, so the strategy never returns a config for them. They still strip
  the boundary marker from the instruction. Grok ignores an enabled config with a log line; **Gemini raises
  `ValueError`** for one.

### Building the prompt so the cut lands in the right place

`PromptAssemblyService._inject_runtime_context` (`src/services/prompt_v3/prompt_assembly_service.py`) appends
everything that changes per request after `PROMPT_CACHE_BOUNDARY`: query-specific facts (facts tagged `semantic_lens`
are partitioned out of the biography in `src/services/prompt_builder.py`), reminders, the current date and time (only
when `include_datetime`), the tail block and, for the orchestrators, the turn's `session_key` (last). Everything before
it (template, biography, knowledge base, skills catalog, standing directives) must be stable between requests. Smart
and Quick pass a `session_key`, so their dynamic block is never empty; the key is constant inside a turn, so the
rolling message marks keep hitting (`SYSTEM_TURN_FINGERPRINT_RFC.md` section 4.9).

## Invariants and traps

- **Both conditions are needed, and the agent-type string is the key.** An unknown type gets no caching. The name must
  match what the builder is called with: the builder is called with `web_search`, while `CACHEABLE_AGENTS` lists
  `websearch` (the name `WebSearchAgent` gives the prompt builder), so web search is **not** cached today. Pinned by
  `tests/unit/services/test_prompt_cache_strategy.py`.
- **`context_caching` is not Claude-only.** `OpenAIAdapter.CAPABILITIES` sets it, so Smart or Quick moved to OpenAI
  by user config receive an enabled config. Grok sets it false on purpose: its caching is automatic with nothing to
  control (comment at `GrokAdapter.CAPABILITIES`).
- **The default provider decides what is cached out of the box**, not the strategy: each type's `default_provider` in
  `AgentProviderStrategy.STRATEGIES` decides it, so a type whose default provider cannot cache (Smart today) is cached
  only for a user who moves it to Claude or OpenAI. `tests/unit/services/test_agent_context_builder.py` pins the wrapping
  for a caching provider and its absence otherwise.
- **Multi-turn marks only for loops that will read them.** A single-turn agent with `cache_last_message` would pay the
  write surcharge with nothing to read; that is why `quick` is cacheable but not in `MULTI_TURN_AGENTS`. Pinned by
  `tests/unit/services/test_prompt_cache_strategy.py`.
- **Dynamic text before the boundary silently kills the cache.** A date, a per-query fact or a per-attempt key placed in
  the static part changes the prefix on every request: the call becomes a write each time and never a read. A new
  per-request block belongs after `PROMPT_CACHE_BOUNDARY`. An agent that builds its own prompt must place the boundary
  itself (`DriveAgent` appends it when absent).
- **An agent never sets `cache_config` except to opt out.** An explicit config bypasses the proxy; on Gemini any
  enabled config raises.
- **A prompt shorter than the provider's minimum cacheable length is silently not cached**, whatever the config says.
- **No cache across a provider switch.** The fallback gets the raw provider (see Flows), and a cache written on one
  provider is not read on another.
- **`ttl` and scope are not controllable here.** `PromptCacheConfig` has no such field; the provider's own default
  applies. Do not document or build on per-agent TTL.
- **No hit metrics are recorded by this block.** `LLMResponse.cache_metadata` exists but no adapter fills it; cached
  tokens reach billing through each adapter's usage parsing.

Adding a cacheable agent type: put it in `CACHEABLE_AGENTS` (and `MULTI_TURN_AGENTS` if its loop has at least two turns
within the provider's cache lifetime), make sure its default provider has `context_caching`, and give the builder the
exact same type string. Adding a provider: set `context_caching=True` only if its adapter acts on an enabled
`cache_config`; the strategy needs no change.

## Links

- [`HEXAGONAL_PROMPT_CACHING_RFC.md`](../../10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md): the design record, section 13 for
  the cache boundary.
- [`SYSTEM_TURN_FINGERPRINT_RFC.md`](../../10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md): section 4.9, the session key and the
  cache.
- [provider_resolution](../provider_resolution/README.md): how the provider the strategy looks at is chosen.
- [prompt_design_system_v3](../prompt_design_system_v3/README.md): the assembler that places the boundary.

## Where to start reading

1. `src/services/prompt_cache_strategy.py::PromptCacheStrategy.resolve`: the whole decision.
2. `src/services/caching_llm_proxy.py::CachingLLMProxy.generate_content`: the injection.
3. `src/services/agent_context_builder.py::AgentContextBuilder._build`: where wrapping happens.
4. `src/adapters/claude_adapter.py::ClaudeAdapter.generate_content`: the boundary split and system marks.
5. `src/adapters/claude_adapter.py::ClaudeAdapter._convert_messages`: the rolling message marks.
6. `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService._inject_runtime_context`: what sits after the boundary.
