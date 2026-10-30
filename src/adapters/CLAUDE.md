# Adapters

Port implementations for every external boundary: LLM providers (Gemini/Claude/Grok/OpenAI),
Firestore repos, Slack/Telegram, Gmail, MicrosoftToDo (GoogleTasks frozen), Node runners
(DOCX/Puppeteer), Unsplash, embeddings, MCP repo.

## Import Rules

`adapters/ → domain/, ports/, config/` only. **No cross-subpackage adapter imports** (REQ-ARCH-23).
See the root `CLAUDE.md` (Import Rules, Layer Semantics) for the full boundary map.

## Adding or Modifying an LLM Adapter

Mandatory protocol: [`docs/how_to/ADAPTER_WIRE_TESTING.md`](../../docs/how_to/ADAPTER_WIRE_TESTING.md).
Every new/modified adapter needs wire tests (**mock at the SDK boundary, not the port** — port-level
mocks cannot detect translation regressions) + contract validators in
`tests/contracts/adapter_contracts.py`.

## Provider Resolution & Capability Gates

- **PerformanceTier** (ECO/BALANCED/PERFORMANCE) — abstraction between agents and concrete models.
  When picking a default tier for a new agent, verify the resolved model accepts every
  parameter the agent sends. Concrete trap (historical): `BALANCED` on Claude was `claude-haiku-4-5-20251001`,
  which rejects `output_config.effort` (HTTP 400). `ClaudeAdapter.MODEL_TIERS` since 2026-10-15:
  ECO and TIER1–3 → `claude-haiku-5-5` (accepts effort; `CLAUDE_SMALL_MODEL` env repoints those, **not**
  BALANCED any more), BALANCED → `claude-sonnet-5-5`, PERFORMANCE → **`claude-opus-5-5`**, ULTRA →
  `claude-fable-5-1`. ConsolidationAgent's default `PERFORMANCE` therefore resolves to Opus 5.5 unless
  `CLAUDE_PERFORMANCE_MODEL` pins it (instant rollback lever — `make claude-rollback` → Sonnet 5;
  `cloudbuild-dev.yaml` carries no pin since 2026-10-15, so a deploy returns to the Opus default). See
  `docs/05_building_blocks/provider_resolution/README.md` §2.1, §2.4 and the gates below.
- **Claude sampling / thinking / tool-choice gates — substring traps.** Model ids nest
  (`claude-sonnet-5` is a prefix of `claude-sonnet-5-5`), so every gate is either a deliberate
  family prefix or an exact id; read each one before adding a model.
  - **No sampling params are sent, by any adapter** (2026-10-08, `decisions/temperature_removed.md`).
    Sonnet 5+, Opus 4.7+/5+, Fable and Haiku 5.5 **400 on a non-default `temperature`/`top_p`/`top_k`**;
    Gemini and the OpenAI reasoning line deprecate or reject them too. `LLMRequest` has no
    `temperature` field; the old `_NO_SAMPLING_MODELS` gate is gone from `ClaudeAdapter` (the Deep
    Research runner keeps its own copy because the same tuple also gates its `max_tokens`).
  - `_THINKING_OFF` (longest prefix wins) — what to send when the caller asks for **no** thinking on
    a model that thinks by default: Sonnet 5 → `disabled`; **Sonnet 5.5 → `between_tools`**
    (`disabled` is a 400 there). Opus 5.5 / Fable cannot turn thinking off — the field is omitted
    and they run adaptive at their default effort (Opus 5.5 default = `medium`).
  - `_NO_FORCED_TOOL_MODELS` — Sonnet 5.5 / Opus 5.5 / Fable 5.1 **400 on `tool_choice` any/tool**;
    a forced request is sent as `auto`.
  - All three are computed in `_model_dependent_params`, which runs again for the same-provider
    fallback model (`_MODEL_FALLBACK`: 5.5 → 5 → 4.6 on 529/503/5xx, never 4xx) — Sonnet 5 does not
    know `between_tools`. Each trap was reproduced as a live 400 before it was fixed (2026-10-03).
  - **Refusals** — the 5.5 / Fable generation runs safety classifiers; a decline is HTTP 200 with
    `stop_reason:"refusal"` and an empty or partial body. `_raise_on_refusal` turns it into
    `LLMClientError` (content-policy class: no failover, Slack-alerted) on both the plain and the
    grounded path; the DR runner raises instead of returning a "partial report".
  - **Account-level 4xx** — "credit balance too low" is HTTP 400 `invalid_request_error`, same as a
    content refusal; `_is_account_error` reads the error body's message (and `billing_error` by type)
    and raises `LLMAccountError` (an `LLMClientError`: no failover, alerted) on both paths. The drive
    indexer reads it as an outage, not a refusal of the file. OpenAI: a billing/quota `code` on a
    non-429 4xx (`_ACCOUNT_ERROR_CODES`).
  - `_NO_XHIGH_MODELS` — the 4.6 generation's effort scale stops at `high`; `xhigh` is clamped.
  New tokenizer (~30% more tokens) — keep `max_tokens` headroom.
  Decisions: `decisions/claude_sonnet_5_adoption.md`, `decisions/model_refresh_2026_10.md`.
- **Per-turn anchors by provider.** `USER_TURN_SYSTEM_ANCHOR` (prepended to the latest user message by
  `BaseAgent`) and `build_persona_anchor` reach OpenAI/Grok in one `developer` item. Claude has no
  `developer` role: the turn anchor stays in the user message and `ClaudeAdapter._add_persona_anchor` puts the
  persona block right after it (same channel, 2026-10-18). Gemini still gets the turn anchor only.
- **ProviderRegistry** — runtime LLM provider selection (gemini/claude/grok).
- **Adapter capability gates** — each adapter silently drops/clamps parameters the resolved model
  doesn't accept instead of forwarding and crashing on 400. ClaudeAdapter gates `thinking`,
  `output_config.effort`, and `web_search_20260209` on `_THINKING_MODELS` / `_DYNAMIC_SEARCH_MODELS`
  substring checks; verify against `client.models.retrieve(<model>).capabilities` when adding
  a new gate. SDK pin: `anthropic >= 0.97.0`.
  - **OpenAIAdapter reasoning.effort floor** — `gpt-5.5-pro` (ULTRA) rejects `reasoning.effort="low"`
    (min `medium`; `400 Unsupported value: 'low'`). `_MIN_MEDIUM_EFFORT_PREFIXES = ("gpt-5.5-pro",)`
    clamps `low→medium` (prefix match, after effort is resolved → covers both explicit `thinking=low`
    and grounding-forced `low`). The 5.4 family (nano/mini/5.4) accepts `low` — **live-probed 2026-07-13,
    don't infer a new model's floor, probe it** (see memory `reference_openai_reasoning_effort_floor`).
    Surfaced via the Smart provider-rotation landing ULTRA on OpenAI. `_REASONING_PREFIXES` now only
    selects the `reasoning.effort` path; no sampling param is sent.
  - **GeminiAdapter sends no `temperature` / `top_p` / `top_k` / `thinking_budget`** (deprecated by
    Google 2026-10, hard 400 announced) — thinking is `thinking_level` only. See
    `decisions/gemini_sampling_params_removed.md`.
  - **GeminiAdapter file parts:** `uri` (already uploaded), `path` (uploaded via the Files API), `base64` (sent inline
    as `inline_data` bytes, `types.Part.from_bytes`; added 2026-10-13 for the drive agents' Gemini fallback — before,
    a base64 part was dropped with an "Unsupported file_data format" warning and the model answered blind), `ref`
    (label only). Gemini caps an inline request at 20 MB. Wire tests:
    `tests/unit/adapters/test_gemini_adapter_inline_bytes_wire.py`.
- **GeminiEmbeddingAdapter** — `gemini-embedding-2`, dim 768 (Matryoshka from native 3072; migrated
  from `-001` 2026-05-29). Legacy `task_type` → inline instruction prefix inside the adapter
  (`RETRIEVAL_DOCUMENT`→`"title: | text: …"`, `RETRIEVAL_QUERY`→`"task: search result | query: …"`,
  `SEMANTIC_SIMILARITY`→passthrough; unknown→`ValueError`). No true batch — `get_embeddings_batch`
  fans out N parallel single-content calls via `asyncio.gather`. Throttle: process-local
  `asyncio.Semaphore` (`GEMINI_EMBED_CONCURRENCY=20`). Transient 429/503 mapped to typed `LLMError`
  and retried via the shared `retry_async` executor (see `decisions/typed_retry_policy.md`); any other
  4xx → `LLMClientError(http_status=code)`, other 5xx → `LLMServerError`, neither retried. The client has a 30 s
  HTTP timeout (`HttpOptions(timeout=30_000)`, milliseconds); a timeout maps to `LLMUnavailableError`, like a 503.
  `EMBEDDING_SPACE` (`"gemini-embedding-2/768"`) is a class attribute composition injects into the drive index, which
  re-embeds cards when it changes (USER_DRIVE_INDEXING_RFC Δ1, Δ25).
  See `docs/05_building_blocks/embedding_system/README.md`.
- **Microsoft Graph (`src/adapters/microsoft/`)** — the token POST in `graph_auth.py` has its own 30 s
  `ClientTimeout` (aiohttp's default is 300 s); `invalid_grant`, `interaction_required` and `consent_required` raise
  `GraphReauthRequired` (a `ValueError` subclass, so To Do is unchanged; the drive maps it to `DriveReauthRequired`).
  `OneDriveAdapter.get_changes`: a 404 on a stored delta cursor is `DriveResyncRequired` like a 410; the cursor is
  bound to the drive and App Folder that served it. Every Graph call is bounded by `GRAPH_CALL_TIMEOUT_S` (60 s).
- **Firestore vector queries with a projection** — `select([...])` before `find_nearest(...)` silently drops the
  distance result field unless its name is in the select list (verified live 2026-10-12;
  `FirestoreDriveIndexRepository.find_nearest_cards`).
- **`GcpTaskQueue`** — `CloudTasksClient` is synchronous gRPC (20 s default timeout): every enqueue runs
  `create_task` in `asyncio.to_thread`, never on the event loop.
- **PromptCacheStrategy** — transparent prompt caching via proxy pattern. Agents declare their
  type; strategy resolves cache config; `CachingLLMProxy` wraps the provider. Agents never
  import or reference `PromptCacheConfig`. See `docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md`.

## Agent Output Format Standards

Every agent that produces structured LLM output MUST follow these rules — no exceptions. These
mechanisms are enforced HERE, in the adapters; the agent side (OUTPUT_FORMAT token, `_parse_response`,
retry) lives with each agent (see `src/agents/CLAUDE.md`).

- **OUTPUT_FORMAT token is mandatory.** Every agent with structured output must have a dedicated
  `OUTPUT_FORMAT_{AGENT}` token in its blueprint. Never embed format instructions inside
  `cognitive_process` or any other token.

- **No regex fallbacks.** `_parse_response()` calls `json.loads()` directly on the raw LLM output.
  On `JSONDecodeError` → raise `ValueError`. Never extract partial output via `re.search`.
  Exception: `EmailClassificationAgent._parse_response()` — markdown code block extraction
  allowed due to cost/latency trade-off in tool-calling mode. See inline comment for rationale.

- **Retry on invalid output, not silent degradation.** When `_parse_response()` raises `ValueError`:
  append the bad model response + a user correction message to history, then continue the loop.
  After `MAX_PARSE_RETRIES` exhausted → `_all_failed(..., "parse_error")` + log error.
  Never post-process malformed output in Python.

- **JSON output enforcement — three mechanisms, provider-specific:**

  **`response_mime_type="application/json"`** — forces model to return raw JSON (no markdown).
  Gemini: natively supported, but **cannot combine with function calling** (API error).
  OpenAI/Grok: mapped to `text.format={"type":"json_object"}` (Responses API).
  Claude: **no equivalent in API — silently ignored**. Claude has no native json_object mode.

  **`response_schema`** — JSON Schema for the output. Describe **every** field: a provider with
  native constrained decoding returns `{}` for an under-specified `{"type":"object"}` and `[]`
  for a bare array (Gemini drops widgets/links this way, worst on Flash) — see `_RESPONSE_SCHEMA`.
  Gemini: natively enforced. Dict schemas route to `responseJsonSchema` (not the stricter
  `responseSchema`), which accepts deep nesting. **Known issue:** schema + Groovy DSL prompt →
  Flash Lite returns empty responses (session 7, confirmed by 22+ tests). This is why MemorySearch
  uses `response_mime_type` without `response_schema`.
  **Function tools always carry `"strict": false` (OpenAI + Grok).** Omitted, OpenAI's Responses
  API defaults it to `true` and rewrites the schema (all-required, `additionalProperties:false`);
  a masked key then becomes a whitespace loop to `max_output_tokens`. See
  `decisions/delegate_tool_contract_and_strict.md`.
  OpenAI: forwarded as `text.format={"type":"json_schema","strict":false}` — the schema IS sent
  and natively enforced (`OpenAIAdapter._to_openai_json_schema` lowercases Gemini-style uppercase
  types; suppressed when `use_grounding` is set — Web Search + JSON mode → 400). Grok: same
  mechanism since 2026-08-14 — `text.format={"type":"json_schema","strict":false}` with
  `GrokAdapter._to_json_schema` (a deliberate duplicate of the OpenAI helper; REQ-ARCH-23
  forbids adapter→adapter imports), also suppressed under grounding.
  Claude has **two paths** (2026-10-23, `decisions/claude_deliver_response_string_tool.md`):
  - *Schema, no client tools* (and any grounded request): `response_schema` is forwarded natively via
    **`output_config.format`** (`{"type":"json_schema","schema":…}`) — the model emits schema-valid JSON as
    **text**, mirroring Gemini's `response_json_schema`.
  - *Schema + client tools, not under grounding* (Smart, Quick, Tutor): `output_config.format` is **not**
    sent; the adapter appends a synthesized `deliver_response` tool with one required string parameter
    `response`, returns `ToolCall("deliver_response", {"response": <string>})` unparsed, and
    `extract_structured_response` parses it with the shared `parse_llm_response`. Why: on Sonnet 5.x the
    grammar + adaptive thinking + tools triple (anthropic-sdk-python#1204) ended tool turns in filler calls
    instead of the answer; a legitimate tool to end the turn removed them (probes in the decision record).
    `_drop_document_citations` runs only when `output_config.format` is sent, so PDF citations are kept on
    this path. A same-provider fallback request keeps no format. Under `max_tokens` the SDK drops an
    unfinished tool-input string, so a cut `response` arrives as `{}` and the adapter drops every tool call
    (an orphan `tool_use` would 400 the next request); a complete string with broken JSON is not a
    truncation (parser `unparseable source=deliver`). Cloud Logging drops `extra=` fields: grep the message
    text `event=llm_response_parse source=deliver outcome=truncated` and `[LlmResponseParse] source=deliver`.

  The native path has no synthesized `respond` tool. Its schema is shaped
  for Anthropic's grammar compiler first: `_nullable_to_union` (`nullable:True` → `["<type>","null"]`
  union) then `_make_strict` (`additionalProperties:false` on every object; drop unsupported keywords
  like `maxLength`; NO `required` injection so optional/variant keys stay optional).
  **History (2026-07-02):** the earlier synthesized `respond` tool was removed because on Sonnet 5
  the model leaked its internal `<parameter>` tool-call serialization into the output — verified
  exhaustively: `tool_choice=auto` (bypass→leaky retry), `=any` (leaky respond), and `strict+anyOf`
  (structure OK but fields corrupted/stubbed) all failed. `output_config.format` produces JSON text,
  so no tool-call format exists to leak. The `output_config.format` path had itself been reverted
  2026-07-01 over anthropic-sdk-python#1204 (reasoning leak / empty-text-block on multi-turn+thinking);
  that bug is outdated on Sonnet 5 (re-verified live 2026-07-02). **Grammar limit:** `output_config.format`
  uses the same grammar compiler as strict tool use, so a complex schema 400s with "Schema is too
  complex" — `rich_content` is therefore a discriminated `anyOf` (null | widget | table | file), not a
  flat bag of optional keys. See `docs/04_solution_strategy/decisions/claude_schema_respond_tool.md`.

  > **Note — `deliver_response` IS this mechanism on Grok and on Claude (with tools).** Smart, Quick
  > and Tutor pass `terminal_tool="deliver_response"` to the DelegationEngine. On Gemini/OpenAI the
  > branch stays cold (native JSON). On **Grok** constrained JSON and function calling compete, so
  > `GrokAdapter` synthesizes `deliver_response` as a real function whose parameters are the
  > response schema — its args are the answer fields. On **Claude** with tools the schema is not in
  > the tool at all: the parameter is one `response` string and the schema reaches the model only
  > through the prompt. In both cases the terminal-tool branch is the reply path. The earlier
  > "vestigial" note is retracted and TD-3 is closed as invalid: **do not delete this machinery.**
  > A terminal tool may also arrive in the same batch as real work (`create_html_page` +
  > `deliver_response`); the engine dispatches the siblings before returning — see
  > `decisions/terminal_tool_co_emitted_calls.md`.

  **OUTPUT_FORMAT token** — prompt-level instruction in Firestore blueprint. The authoritative
  source of truth for output structure. All JSON agents must have one. `response_schema` and
  `response_mime_type` are provider hints to enforce the format at API level; the token
  defines the actual schema the LLM follows.

  **What agents should pass:**
  - JSON agents WITHOUT tools: `response_mime_type` + `response_schema` (both).
    Gemini uses both natively. OpenAI enforces `response_schema` via json_schema (strict:false);
    Grok enforces `response_schema` the same way. Claude: `response_mime_type` is silently ignored;
    `response_schema` is forwarded via `output_config.format` (see above).
  - JSON agents WITH tools: `response_schema` only (no `response_mime_type`).
    Gemini cannot combine mime_type + tools. On Claude with tools the schema is **not enforced on
    the wire** (it triggers the string `deliver_response` tool instead); the OUTPUT_FORMAT token is
    the schema's only carrier there.
  - Non-JSON agents: neither. OUTPUT_FORMAT token handles everything.

  **Guard:** agents requiring JSON output must be locked to providers that support it
  in `AgentProviderStrategy.STRATEGIES` (`allowed_providers`). If an agent uses
  `response_mime_type` without `response_schema`, it **must not** run on Claude
  (`response_mime_type` is still silently ignored by Claude — only `response_schema` is
  honoured, via `output_config.format`).

- **`_RESPONSE_SCHEMA` on Quick/Smart.** Both orchestrators pass
  `response_schema=_RESPONSE_SCHEMA` to `LLMRequest` even when tools are active. It now
  **fully describes every field.** `rich_content` is a discriminated `anyOf` (null | widget |
  table | file) — each typed variant declares its own `data` keys (widget: html/alt_text; table:
  title/headers/rows[{cells}]/footer; file: filename/title/content). `anyOf` (not a flat bag of
  optional keys) is mandatory on two fronts: a flat `{"type":"object"}` data field comes back as
  `{}` on Gemini Flash (widget dropped), AND on Claude the flat bag's `2^N` optional-key grammar
  400s with "Schema is too complex" under `output_config.format`. `link_list` is required
  `array<object{anchor,title,url}>`. Structure is enforced from code where the provider allows
  (Gemini responseJsonSchema, Claude output_config.format without tools, OpenAI json_schema, Grok's
  `deliver_response` parameters). The OUTPUT_FORMAT_JSON token also carries the schema
  (`prompts_snapshot/tokens/system/OUTPUT_FORMAT_JSON.groovy`), and on Claude with tools it is the only
  carrier (Smart/Quick: OUTPUT_FORMAT_JSON; Tutor: OUTPUT_FORMAT_TUTOR).

- **`rich_content.data.rows` format: `[{"cells": [...]}, ...]`.** Each table row is an object with
  a `cells` key (array of strings). Never use `[[...], [...]]` (Gemini hangs on
  `array<array<string>>` in `response_schema`) or duplicate `rows` keys (JSON parse drops all but
  last). The Slack adapter normalizes all row variants: `{cells}` objects, plain arrays, flat lists.
  It also makes every row the same width (the widest of header and rows — nothing is dropped) and
  never sends an empty cell (`SLACK_EMPTY_CELL`): Slack rejects uneven rows and empty `raw_text`
  with `invalid_blocks`. If Slack rejects the blocks anyway (row/column/10k-char limits), the table
  is re-sent as plain text via `send_long_text` — the reply is never lost to a block schema error.

## Traps from the root CLAUDE.md

- **Usage legs never overlap:** `prompt_tokens` is always uncached input; `calculate_cost` bills it, `cache_read_tokens` and
  `cache_creation_tokens` as disjoint legs. OpenAI/Gemini report cached inside the input total, so the adapter subtracts both cache legs.
- **xAI realtime `billable_audio_seconds`** on `response.done` is the session's running total: `XaiRealtimeAdapter` emits the
  increment (`decisions/billing_cache_write_and_voice_seconds.md`).
- **Workspace bucket (versioned):** `GcsWorkspaceAdapter` deletes by metadata patch + delete of the live object under
  `if_generation_match`, never a generation-addressed delete (that erases the version).
- **Slack voice memo** is detected by the file name `audio_message.*`, never by mimetype (seen as `audio/mp4` and `video/mp4`).
