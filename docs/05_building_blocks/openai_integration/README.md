<!-- living-doc
covers:
  src/adapters/openai_adapter.py
-->
# OpenAI Integration

## Purpose

`OpenAIAdapter` makes OpenAI an LLM provider behind the same port as the Gemini, Claude and Grok adapters
(`src/ports/llm_port.py::LLMPort`). An agent never knows it is talking to OpenAI: the provider is chosen per agent by
`AgentProviderStrategy` (`src/services/agent_context_builder.py`) and the user's `UserBotConfig`, and the adapter
translates the domain request into one call of the Responses API and the answer back into an `LLMResponse`.

What it is not: not the realtime voice adapter, not the transcription adapter, and not deep research (Claude is the only
deep-research provider; `docs/10_rfcs/DEEP_RESEARCH_RFC.md`, `decisions/openai_deep_research_adapter_deferred.md`).
OpenAI is the default provider of some agents (`default_provider` entries in `AgentProviderStrategy.STRATEGIES`, e.g.
`web_search`, `notes`, `tutor`) and an allowed override or failover target for most others.

## Owned data

None stored. The adapter keeps no state between calls. Its configuration:

- `OPENAI_API_KEY` is a required-configuration key in `src/config/settings.py::load_settings` (from `.env`, else Secret
  Manager). `ServiceContainer._init_openai` (`src/composition/service_container.py`) builds the adapter only when the
  key is set and registers it in the `ProviderRegistry` under `"openai"`; without a key OpenAI is simply absent and the
  bot runs on the other providers.
- `OPENAI_TIER_OVERRIDES` (`tier=model,tier=model`) is an optional knob read with `os.getenv` in
  `OpenAIAdapter.__init__`, so it needs no `load_settings` entry. It remaps `OpenAIAdapter.MODEL_TIERS` without a
  redeploy; a malformed pair is logged and skipped (`_parse_tier_overrides`), never fatal. `make openai-model`
  shows it, `make openai-rollback` sets the previous model map, `make openai-forward` removes it.
- The tier to model map is the class constant `OpenAIAdapter.MODEL_TIERS`; why the models are what they are:
  `decisions/model_refresh_2026_10.md`, `docs/10_rfcs/GPT_5_6_MIGRATION_RFC.md`. Prices live in
  `src/domain/billing.py`, not here (`decisions/openai_gpt56_price_cut.md`).

## Flows

**One request** (`OpenAIAdapter.generate_content`):

1. The `PROMPT_CACHE_BOUNDARY` marker is removed from the system instruction. The text before it is kept as the seed of
   the cache key (step 8).
2. `USER_TURN_SYSTEM_ANCHOR` is searched backwards through all messages, found in the newest user message that holds it
   and cut out of that message. It moves into a leading `developer` item together with the persona anchor
   (`build_persona_anchor`), because the developer role outweighs user text. Searching only the last message misses it
   from the second delegation turn on, when the last message is a tool result
   (`decisions/openai_personality_grounding.md`).
3. Messages become Responses API input items (`_convert_input`); the system instruction goes to `instructions`, not
   into the items. A model message with `raw_content` (the earlier `response.output` list) is replayed as is; a tool
   result becomes `function_call_output` whose `call_id` is found from the preceding call (`_find_call_id`). The
   `call_id` is stored in `ToolCall.thought_signature`.
4. Tools become internally tagged function tools (`_convert_tools`), `strict` always `False`, and `tool_choice` is
   `required` for `force_tool_use`, else `auto`. With `use_grounding` the native `{"type": "web_search"}` tool is
   prepended.
5. Output format (`text.format`): grounding sends none; a dict `response_schema` sends `json_schema` with
   `strict=False` (`_to_openai_json_schema` lower-cases Gemini-style `OBJECT`/`STRING` types); `application/json`
   alone sends `json_object` plus a `developer` item `Respond in JSON.`, since the API demands the word "json" in the
   input.
6. Reasoning effort (see Invariants) and `reasoning.summary="concise"` for the PERFORMANCE and ULTRA models only.
7. `store` is `request.allow_provider_storage`; callers such as the drive agents set it to `False`.
8. Caching: with `cache_config.enabled`, models in `_DEPRECATED_RETENTION_PREFIXES` get `prompt_cache_key`
   (`_prompt_cache_key`, a hash of the static prefix, sent via `extra_body`); others get `prompt_cache_retention="24h"`.
9. The call runs under the request timeout twice: as the SDK `timeout` and as an outer `asyncio.wait_for` that bounds
   total wall time including SDK retries. SDK errors map to the `LLM*Error` types (`src/domain/exceptions.py`) that
   `BaseAgent._call_llm` uses to decide on failover: 429 rate limit, 503 unavailable, other 5xx server error, connection
   error network error, 4xx client error. A 4xx whose code is in `_ACCOUNT_ERROR_CODES` (quota, billing) is
   `LLMAccountError`, an account problem and not a failover trigger.

**Response** (`_parse_response`): text is `output_text`; `url_citation` annotations are appended once each as a
`*Sources:*` block so downstream agents see the URLs; function calls become `ToolCall`s, and unparsable (truncated)
arguments become `{"_parse_error": "truncated_json", ...}` rather than an exception. `finish_reason` comes from
`status` and `incomplete_details` (`_map_finish_reason`). `raw_content` is `response.output`, which step 3 replays.

**Files** (`_file_input_part`): an image becomes inline `input_image`, any other file inline `input_file` with a
base64 data URL; there is no Files API upload (`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §24.2). A part that cannot be
read or has no content raises `LLMClientError`, because a request silently missing its file answers about nothing. A
file part holding only a `ref` is skipped. What the models read natively (images, PDF) is `native_formats`.

## Invariants and traps

- **No sampling parameter is ever sent** (`decisions/temperature_removed.md`). `_REASONING_PREFIXES` now gates only
  `reasoning.effort` (`_is_reasoning_model`); other models get no `reasoning` block at all.
- **Effort rules are per model and verified by live probes, not by documentation.** `thinking` values in `_EFFORTS`
  pass through, anything else becomes `medium`. Grounding without `thinking` forces `low`, so agentic web search stays
  on; with both, `thinking` wins. A model that reasons when `reasoning` is omitted is given an explicit effort when the
  caller asks for none (`_THINKING_OFF_EFFORT`, exact model ids). Two floors clamp upward before the call:
  `_MIN_LOW_EFFORT_PREFIXES` (`none` becomes `low`) and `_MIN_MEDIUM_EFFORT_PREFIXES` (`none` and `low` become
  `medium`); without them the API answers 400. The floors matter on the provider-rotation path, where a Smart run
  that lost its primary provider is rebuilt on OpenAI at the same tier (`decisions/cross_provider_execution_retry.md`).
  Tests: `tests/unit/adapters/test_openai_adapter.py`. A new model: probe its accepted efforts first and extend the
  constants, which carry the probe dates.
- **Tools are sent with `strict=False` explicitly.** Omitted, the API defaults to strict and rewrites the schema (every
  property required); with the delegation tool's shared `context` object the model then streamed whitespace for
  minutes (`decisions/delegate_tool_contract_and_strict.md`).
- **Cache legs must not overlap.** OpenAI's `input_tokens` already includes `cached_tokens` and `cache_write_tokens`,
  and both sit in `input_tokens_details`. `_parse_response` subtracts both so `prompt_tokens`, `cache_read_tokens` and
  `cache_creation_tokens` are disjoint; subtracting only the read leg billed written tokens twice
  (`decisions/openai_cache_write_tokens_location.md`, `decisions/billing_cache_write_and_voice_seconds.md`).
- **`CAPABILITIES.context_caching` is `True` while `supports_caching()` returns `False`.** The first says the provider
  caches prefixes implicitly; the second says the adapter has no explicit cache object to manage, so `CachingLLMProxy`
  does nothing for it. Do not "fix" one to match the other.
- **Grounding and JSON mode exclude each other** (the API answers 400), hence step 5; a grounded call that needs
  structured output has to parse text.
- **`call_id` carries the tool-call identity.** `_find_call_id` raises `ValueError` when no earlier call matches a tool
  result; a history that lost its model turn cannot be replayed on OpenAI.
- The model is a prefix match everywhere (`startswith`), so an unknown future model with a known prefix inherits that
  family's rules. Check the gates when a new family ships.

## Links

- `docs/10_rfcs/GPT_5_6_MIGRATION_RFC.md` (probe results, caching decision), `decisions/model_refresh_2026_10.md` (current
  tier map), `decisions/openai_ultra_tier_to_gpt_5_5_pro.md` (earlier ULTRA choice, kept for the effort-floor origin).
- `decisions/openai_personality_grounding.md`, `decisions/delegate_tool_contract_and_strict.md`,
  `decisions/temperature_removed.md`, `decisions/openai_cache_write_tokens_location.md`,
  `decisions/openai_gpt56_price_cut.md`, `decisions/cross_provider_execution_retry.md`.
- `docs/10_rfcs/EXTERNAL_FILES_RFC.md` (native formats), `docs/how_to/ADAPTER_WIRE_TESTING.md` (mandatory protocol for
  changing this adapter), `src/adapters/CLAUDE.md`, `docs/05_building_blocks/provider_resolution/README.md`.
- Decision records are in `docs/04_solution_strategy/decisions/`.

## Where to start reading

- `src/adapters/openai_adapter.py::OpenAIAdapter.generate_content` - the whole request path.
- `src/adapters/openai_adapter.py::OpenAIAdapter._convert_input` - history and tool-result replay.
- `src/adapters/openai_adapter.py::OpenAIAdapter._parse_response` - citations, tool calls, usage legs.
- `src/adapters/openai_adapter.py::OpenAIAdapter.MODEL_TIERS` - tier map and the effort-gate constants beside it.
- `src/services/agent_context_builder.py::AgentProviderStrategy` - which agents may use OpenAI.
- `src/composition/service_container.py::ServiceContainer._init_openai` - wiring.
