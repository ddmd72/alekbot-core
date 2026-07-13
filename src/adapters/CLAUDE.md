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
  parameter the agent sends. Concrete trap: `BALANCED` on Claude → `claude-haiku-4-5-20251001`,
  which rejects `output_config.effort` (HTTP 400). ConsolidationAgent default is therefore
  `PERFORMANCE` → **`claude-sonnet-5`** in `ClaudeAdapter.MODEL_TIERS` (was `claude-sonnet-4-6`
  until 2026-07; env-overridable via `CLAUDE_PERFORMANCE_MODEL` for instant rollback). See
  `docs/05_building_blocks/provider_resolution/README.md` §2.1, §2.4 and the Sonnet 5 gates below.
- **Sonnet 5 sampling / thinking gates** — Sonnet 5 (and Opus 4.7/4.8, Fable 5) **400 on a
  non-default `temperature`/`top_p`/`top_k`**, so `ClaudeAdapter._NO_SAMPLING_MODELS` omits the
  sampling param entirely for them (Sonnet 4.6 / Opus 4.6 / Haiku keep it). Sonnet 5 also runs
  **adaptive thinking by default** when `thinking` is omitted (unlike 4.6) — `_ADAPTIVE_DEFAULT_ON_MODELS`
  sends `thinking:{type:"disabled"}` when no effort is requested, to honour the caller's intent.
  New tokenizer (~30% more tokens) — keep `max_tokens` headroom. Transient-error rollback:
  `_MODEL_FALLBACK` retries `claude-sonnet-5`→`claude-sonnet-4-6` once on 529/503/5xx (not 4xx).
  Decision: `docs/04_solution_strategy/decisions/claude_sonnet_5_adoption.md`.
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
    Surfaced via the Smart provider-rotation landing ULTRA on OpenAI. Sampling params gated separately
    on `_REASONING_PREFIXES = (gpt-5, o1, o3)`.
- **GeminiEmbeddingAdapter** — `gemini-embedding-2`, dim 768 (Matryoshka from native 3072; migrated
  from `-001` 2026-05-29). Legacy `task_type` → inline instruction prefix inside the adapter
  (`RETRIEVAL_DOCUMENT`→`"title: | text: …"`, `RETRIEVAL_QUERY`→`"task: search result | query: …"`,
  `SEMANTIC_SIMILARITY`→passthrough; unknown→`ValueError`). No true batch — `get_embeddings_batch`
  fans out N parallel single-content calls via `asyncio.gather`. Throttle: process-local
  `asyncio.Semaphore` (`GEMINI_EMBED_CONCURRENCY=20`). Transient 429/503 mapped to typed `LLMError`
  and retried via the shared `retry_async` executor (see `decisions/typed_retry_policy.md`).
  See `docs/05_building_blocks/embedding_system/README.md`.
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
  OpenAI/Grok: mapped to `response_format: {"type": "json_object"}`.
  Claude: **no equivalent in API — silently ignored**. Claude has no native json_object mode.

  **`response_schema`** — JSON Schema for the output. Describe **every** field: a provider with
  native constrained decoding returns `{}` for an under-specified `{"type":"object"}` and `[]`
  for a bare array (Gemini drops widgets/links this way, worst on Flash) — see `_RESPONSE_SCHEMA`.
  Gemini: natively enforced. Dict schemas route to `responseJsonSchema` (not the stricter
  `responseSchema`), which accepts deep nesting. **Known issue:** schema + Groovy DSL prompt →
  Flash Lite returns empty responses (session 7, confirmed by 22+ tests). This is why MemorySearch
  uses `response_mime_type` without `response_schema`.
  OpenAI: forwarded as `text.format={"type":"json_schema","strict":false}` — the schema IS sent
  and natively enforced (`OpenAIAdapter._to_openai_json_schema` lowercases Gemini-style uppercase
  types; suppressed when `use_grounding` is set — Web Search + JSON mode → 400). Grok: still
  `json_object` mode (schema not forwarded; structure from the OUTPUT_FORMAT token + examples).
  Claude: `response_schema` is forwarded natively via **`output_config.format`**
  (`{"type":"json_schema","schema":…}`) — the model emits schema-valid JSON as **text**, mirroring
  Gemini's `response_json_schema`. There is **no** synthesized `respond` tool. The schema is shaped
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

  > **Note — `deliver_response` is NOT this mechanism.** Smart passes
  > `terminal_tool="deliver_response"` to the DelegationEngine, but that terminal-tool branch is
  > vestigial (no adapter declares such a tool). Smart's structured output arrives via
  > `output_config.format` → JSON-text (Claude) or native JSON (Gemini/OpenAI). See
  > IMPLEMENTATION_ROADMAP.md TD-3.

  **OUTPUT_FORMAT token** — prompt-level instruction in Firestore blueprint. The authoritative
  source of truth for output structure. All JSON agents must have one. `response_schema` and
  `response_mime_type` are provider hints to enforce the format at API level; the token
  defines the actual schema the LLM follows.

  **What agents should pass:**
  - JSON agents WITHOUT tools: `response_mime_type` + `response_schema` (both).
    Gemini uses both natively. OpenAI enforces `response_schema` via json_schema (strict:false);
    Grok reacts via json_object. Claude: `response_mime_type` is silently ignored;
    `response_schema` is forwarded via `output_config.format` (see above).
  - JSON agents WITH tools: `response_schema` only (no `response_mime_type`).
    Gemini cannot combine mime_type + tools. Schema works with tools on all providers.
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
  `array<object{anchor,title,url}>`. Structure is enforced from code (Gemini responseJsonSchema,
  Claude output_config.format, OpenAI json_schema); the OUTPUT_FORMAT_JSON token no longer
  duplicates the schema.

- **`rich_content.data.rows` format: `[{"cells": [...]}, ...]`.** Each table row is an object with
  a `cells` key (array of strings). Never use `[[...], [...]]` (Gemini hangs on
  `array<array<string>>` in `response_schema`) or duplicate `rows` keys (JSON parse drops all but
  last). The Slack adapter normalizes all row variants: `{cells}` objects, plain arrays, flat lists.
