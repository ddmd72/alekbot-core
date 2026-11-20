<!-- living-doc
covers:
  src/adapters/grok_adapter.py
-->
# xAI Grok Integration

## Purpose

This block is the text-LLM adapter for xAI Grok: `GrokAdapter` implements `LLMPort` against xAI's **Responses API**
(`/v1/responses`) through the same pinned `openai` SDK that `OpenAIAdapter` uses, with a different `base_url`. There is
no `xai-sdk` dependency. The Responses API is the only xAI surface that has server-side search (`web_search`,
`x_search`), so the adapter cannot go back to Chat Completions.

It is the boundary for one provider: it turns domain `Message`/tool/`LLMRequest` objects into Responses items, calls
xAI, and turns the output items back into an `LLMResponse` with usage. Which agent runs on Grok, what a tier costs and
how a failed call fails over are decided elsewhere (see Flows).

What it is not: it does not cover Grok image and video generation (`grok_image_adapter`, `grok_video_adapter`, their own
ports) or the voice models, and it does not offer file upload or controllable prompt caching.

## Owned data

None. The adapter keeps no state between calls. Two things leave the process:

- Every request is sent with `store=True`, so xAI keeps the response in its dashboard for debugging.
- The credential is `XAI_API_KEY` (Secret Manager in the deploy, `.env` locally), read by `load_settings()` and handed to
  `ServiceContainer._init_grok`, which returns `None` when the key is absent: the bot starts without Grok.

Prices are not here: `src/domain/billing.py::_PRICING_PER_MILLION_TOKENS` holds them, keyed by the model ids that
`GrokAdapter.MODEL_TIERS` returns.

## Flows

**Which requests reach Grok.** `src/services/agent_context_builder.py::AgentProviderStrategy.STRATEGIES` lists `grok` in
`allowed_providers` per agent type and names it `default_provider` for `html_page` only (the image and video entries are
Grok-only too, but on their own ports). Three ways override a default, resolved by
`AgentContextBuilder.resolve_provider_name` and `AgentContextBuilder.resolve_for_task`:

1. a per-agent override, `UserBotConfig.agent_providers[agent_type]`;
2. the global `UserBotConfig.provider_preference`;
3. a per-complexity `ComplexitySettings.provider_override` (`src/domain/complexity_settings.py`). It is set per task
   complexity, not per agent, so it can move a whole class of background work onto Grok without touching any agent.

An override naming a provider outside the agent's `allowed_providers` is ignored, not an error. Cross-provider rotation
(`AgentContextBuilder.resolve_next_provider`, on a terminal `TranscriptLockedError`) walks `allowed_providers` in
declared order, and `grok` is last for `smart`. Moving a default to Grok needs an eval first
(`decisions/html_page_grok_default.md` is the precedent).

**One call, `GrokAdapter.generate_content`:**

1. Strip `PROMPT_CACHE_BOUNDARY` from the system prompt and derive `prompt_cache_key` from the text before it
   (`GrokAdapter._prompt_cache_key`). An enabled `cache_config` is ignored with a debug line, never an error.
2. `GrokAdapter._extract_turn_anchor` lifts `USER_TURN_SYSTEM_ANCHOR` out of the most recent user message that carries
   it. The anchor and `build_persona_anchor(system_instruction)` (`src/domain/llm.py`) go into **one** `developer` input
   item placed first.
3. `GrokAdapter._convert_input` and `GrokAdapter._convert_tools` build the input items and tools. `use_grounding`
   prepends `{"type": "web_search"}`.
4. The output format is chosen by this order: grounding sends none; a dict schema together with tools becomes a
   synthesized `deliver_response` function whose parameters are the schema (`GrokAdapter.TERMINAL_TOOL_NAME`) and forces
   `tool_choice="required"`; a dict schema alone is `text.format` `json_schema` with `strict=False`; a bare
   `application/json` mime type is `json_object`.
5. `request.thinking` becomes `reasoning.effort` (see traps). An explicit `request.timeout` is forwarded to the SDK and
   also bounds total wall time with `asyncio.wait_for`.
6. SDK errors map to domain errors (timeout, rate limit, network, 503 unavailable, other 5xx server, 4xx client). A 4xx
   is deterministic and is not a failover trigger.
7. `GrokAdapter._parse_response` reads the output items: `function_call` becomes a `ToolCall` (the xAI `call_id` rides in
   `thought_signature`), `reasoning` summaries become `thought_text`, `url_citation` annotations are appended to the text
   as a `*Sources:*` block, `web_search_call` items are only logged. `raw_content` keeps the output items, and
   `_convert_input` replays them verbatim on the next turn so the reasoning and function-call sequence survives.

## Invariants and traps

- **The key is stripped, and that is load-bearing.** Secret Manager mounts the value verbatim. A trailing newline makes
  `httpx` refuse the Authorization header, the SDK reports `APIConnectionError`, and the adapter maps it to
  `LLMNetworkError`: it looks like "xAI blocks Google Cloud" while nothing leaves the container. `GrokAdapter.__init__`
  strips; create the secret with `printf %s`. Test: `test_api_key_whitespace_is_stripped`. History:
  `decisions/grok_revival_2026_08.md`.
- **A retired model id does not fail, it mis-bills.** xAI keeps answering HTTP 200 for a retired id and serves another
  model. `MODEL_TIERS` may hold only ids that `GET /v1/models` lists, and every id in it needs a row in
  `_PRICING_PER_MILLION_TOKENS`, because `calculate_cost` returns 0.0 for an unknown model without any error.
- **Long-context cost is under-counted.** xAI doubles input and output prices at long prompts; `billing.py` does not
  model it (comment above the Grok rows).
- **Role precedence is `developer` > `instructions` > `user`** (measured with both orderings and a non-degenerate signal;
  the same as OpenAI). That is why the turn anchor is lifted into `developer`. `_extract_turn_anchor` searches backwards
  through all messages: from delegation turn 2 the last message is a tool result with no text, so looking only at the
  last one finds nothing. Tests: `test_turn_anchor_lifted_into_developer_item`,
  `test_turn_anchor_and_personality_share_one_developer_item`.
- **Structured output and function calling compete.** With a JSON format and tools both set, the model often writes the
  promised action into the answer field and calls nothing (measured on the real orchestrator prompt). So the schema moves
  onto the synthesized `deliver_response` tool, which `DelegationEngine` already treats as terminal. The name is
  duplicated on purpose (adapters cannot import agents); it must equal the `terminal_tool` the delegating agents
  pass to `DelegationEngine`. A terminal call can arrive in one batch next to real tool calls; `DelegationEngine` dispatches those too.
  Tests: `test_schema_plus_tools_synthesizes_terminal_tool`, `test_grounding_does_not_synthesize_terminal_tool`.
- **Grounding is paid input.** Declaring `web_search` adds thousands of input tokens before any search, and a grounded
  answer costs a large multiple of a plain one; JSON mode is dropped while it is on. Do not ground by default.
- **Both models reason by default**, with no `reasoning` block. Effort values come from user config, so an unknown value
  is normalised to `medium` (a bad string must not become an HTTP 400 mid-conversation). `none` is accepted only for ids
  starting with `GrokAdapter._EFFORT_NONE_MODELS` and is clamped to `medium` elsewhere. Grounding forces no effort
  (unlike `OpenAIAdapter`). Tests: `test_effort_none_allowed_on_grok_43`, `test_effort_none_clamped_to_medium_on_grok_46`,
  `test_unknown_effort_normalised_to_medium`.
- **Caching is automatic and not controllable.** `CAPABILITIES.context_caching` is `False`, which means "no breakpoints
  to place", not "no caching": cache hits are read from `usage.input_tokens_details.cached_tokens` and priced by the
  `cache_read` multiplier. `_parse_response` follows the repo convention that `UsageMetadata.prompt_tokens` is **uncached**
  input only (`input - cached`), with `cache_read_tokens` carrying the rest. Reasoning tokens are billed as output, so
  `total_tokens` is not `prompt + completion`.
- **Latency is minutes, not seconds, on a long multi-turn run.** The client ceiling is 300 s and `max_retries=2`, so one
  call can take up to three times that when no `request.timeout` is given; an explicit one bounds the total. A background
  task needs a Cloud Tasks `dispatch_deadline` that can hold a Grok run: `docs/07_deployment/SCHEDULERS.md`. Tests:
  `test_client_timeout_matches_openai_adapter`, `test_request_timeout_is_forwarded_to_the_sdk`.
- **No files except images.** `upload_file` raises `NotImplementedError`; `_convert_input` drops non-image binaries with a
  warning and sends images as `input_image` data URLs. `native_formats` reports images only on `_GROK_VISION_MODELS` and
  never a PDF, so `FileConversionService` converts a PDF to text before it gets here.
- **Old history still loads.** A model message whose `raw_content` is a pre-migration Chat Completions object is converted
  by `_convert_input`; do not remove that branch while such sessions exist.
- **Deliberate duplicates.** `_prompt_cache_key` and `_to_json_schema` copy `OpenAIAdapter`'s, because adapters may not
  import each other (REQ-ARCH-23). `_to_json_schema` lowercases `type` values, since some callers declare Gemini-style
  uppercase types.
- **Function tools send `strict=False` explicitly** so a provider default change cannot turn on strict grammar
  (`decisions/delegate_tool_contract_and_strict.md`). No sampling parameter is sent (`decisions/temperature_removed.md`).

## Links

- [`decisions/grok_revival_2026_08.md`](../../04_solution_strategy/decisions/grok_revival_2026_08.md): why the integration
  was dead for months, why the transport moved to Responses.
- [`decisions/html_page_grok_default.md`](../../04_solution_strategy/decisions/html_page_grok_default.md): the one
  agent that defaults to Grok.
- [`decisions/delegate_tool_contract_and_strict.md`](../../04_solution_strategy/decisions/delegate_tool_contract_and_strict.md),
  [`decisions/temperature_removed.md`](../../04_solution_strategy/decisions/temperature_removed.md).
- [`ADAPTER_WIRE_TESTING.md`](../../how_to/ADAPTER_WIRE_TESTING.md): wire tests mock `adapter.client.responses.create`.
  Grok's are `tests/unit/adapters/test_grok_adapter.py`, the shared offline stub in
  `tests/integration/offline/adapters/`, and the `"grok"` validators in `tests/contracts/adapter_contracts.py`.
- [`SCHEDULERS.md`](../../07_deployment/SCHEDULERS.md): task deadlines for long Grok runs.

## Where to start reading

- `src/adapters/grok_adapter.py::GrokAdapter.generate_content`
- `src/adapters/grok_adapter.py::GrokAdapter._parse_response`
- `src/adapters/grok_adapter.py::GrokAdapter._extract_turn_anchor`
- `src/adapters/grok_adapter.py::GrokAdapter.MODEL_TIERS`
- `src/services/agent_context_builder.py::AgentContextBuilder.resolve_provider_name`
- `src/composition/service_container.py::ServiceContainer._init_grok`
