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
  `PERFORMANCE` (`claude-sonnet-4-6`) in `_DEFAULT_AGENT_TIERS`. See
  `docs/05_building_blocks/provider_resolution/README.md` §2.1, §2.4.
- **ProviderRegistry** — runtime LLM provider selection (gemini/claude/grok).
- **Adapter capability gates** — each adapter silently drops parameters the resolved model
  doesn't accept instead of forwarding and crashing on 400. ClaudeAdapter gates `thinking`,
  `output_config.effort`, and `web_search_20260209` on `_THINKING_MODELS` / `_DYNAMIC_SEARCH_MODELS`
  substring checks; verify against `client.models.retrieve(<model>).capabilities` when adding
  a new gate. SDK pin: `anthropic >= 0.97.0`.
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
  Claude: `response_schema` injects a synthesized **`respond` tool** (schema as its
  `input_schema`, `nullable` stripped recursively via `_strip_nullable`) with
  `tool_choice=auto`; the adapter intercepts the `respond` call and returns its input as
  JSON text. Structure is carried by tool_use input, NOT constrained-decode text.
  The GA `output_config.format` path was reverted 2026-07-01: on multi-turn tool loops with
  adaptive thinking it made Claude emit degenerate output — reasoning leaking into the first
  JSON string field, or an empty text block alongside tool_use that the API then rejects on
  replay (anthropic-sdk-python#1204). The tool path keeps reasoning in the thinking block.
  The tool's own **description matters**: a bare declaration makes the model bypass respond
  (write plain text) or leak Claude's internal `<parameter>` tool-call format into the first
  field on complex prompts — so the adapter gives `respond` an explicit "call this tool, don't
  emit plain text or XML tags" description, and agents add per-field `description`s on their
  schema (e.g. Smart's `_RESPONSE_SCHEMA`). Safety net for the rare plain-text bypass (model
  ignores respond under `auto`): the adapter re-issues once with **`tool_choice=any`** — which
  forces respond (structured) or a real delegation call (engine continues), never plain text,
  and does NOT leak `<parameter>` the way forcing the *specific* respond tool does (~1/4 on
  long outputs). The adapter must never return plain text to a schema agent — some `json.loads`
  the result directly. See `docs/04_solution_strategy/decisions/claude_schema_respond_tool.md`.

  > **Note — `deliver_response` is NOT this mechanism.** Smart passes
  > `terminal_tool="deliver_response"` to the DelegationEngine, but that terminal-tool branch is
  > vestigial (no adapter declares such a tool). Smart's structured output arrives via the
  > `respond`-tool → JSON-text path above (Claude) or native JSON (Gemini/OpenAI). See
  > IMPLEMENTATION_ROADMAP.md TD-3.

  **OUTPUT_FORMAT token** — prompt-level instruction in Firestore blueprint. The authoritative
  source of truth for output structure. All JSON agents must have one. `response_schema` and
  `response_mime_type` are provider hints to enforce the format at API level; the token
  defines the actual schema the LLM follows.

  **What agents should pass:**
  - JSON agents WITHOUT tools: `response_mime_type` + `response_schema` (both).
    Gemini uses both natively. OpenAI enforces `response_schema` via json_schema (strict:false);
    Grok reacts via json_object. Claude: `response_mime_type` is silently ignored;
    `response_schema` injects a synthesized `respond` tool (see above).
  - JSON agents WITH tools: `response_schema` only (no `response_mime_type`).
    Gemini cannot combine mime_type + tools. Schema works with tools on all providers.
  - Non-JSON agents: neither. OUTPUT_FORMAT token handles everything.

  **Guard:** agents requiring JSON output must be locked to providers that support it
  in `AgentProviderStrategy.STRATEGIES` (`allowed_providers`). If an agent uses
  `response_mime_type` without `response_schema`, it **must not** run on Claude
  (`response_mime_type` is still silently ignored by Claude — only `response_schema` is
  honoured, via the synthesized `respond` tool).

- **`_RESPONSE_SCHEMA` on Quick/Smart.** Both orchestrators pass
  `response_schema=_RESPONSE_SCHEMA` to `LLMRequest` even when tools are active. It now
  **fully describes every field** — `rich_content.data` carries all variant keys (table:
  title/headers/rows[{cells}]/footer; widget: html/alt_text; file: filename/content) and
  `link_list` is required `array<object{anchor,title,url}>`. This is mandatory, not optional:
  a flat `{"type":"object"}` data field comes back as `{}` on Gemini Flash (widget dropped) and
  a bare `link_list` array as `[]` (dangling `[N]` citations with no URL). Dict schemas route to
  Gemini's `responseJsonSchema`, which accepts the nesting — the earlier "keep data flat for the
  nesting limit" note applied to the stricter `responseSchema` path and is obsolete. The
  OUTPUT_FORMAT_JSON token no longer duplicates this schema (it drifted); structure is enforced
  from code (Gemini responseJsonSchema, Claude synthesized `respond` tool, OpenAI json_schema).

- **`rich_content.data.rows` format: `[{"cells": [...]}, ...]`.** Each table row is an object with
  a `cells` key (array of strings). Never use `[[...], [...]]` (Gemini hangs on
  `array<array<string>>` in `response_schema`) or duplicate `rows` keys (JSON parse drops all but
  last). The Slack adapter normalizes all row variants: `{cells}` objects, plain arrays, flat lists.
