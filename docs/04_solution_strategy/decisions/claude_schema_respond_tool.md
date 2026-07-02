# Claude structured output: `output_config.format` (native), not a synthesized tool

**Date:** 2026-07-02
**Status:** Accepted — **supersedes** the 2026-07-01 `respond`-tool decision below (which itself
reverted the 2026-04-23 `output_config.format` migration `772beb8`). Net: back to native
`output_config.format`, now that the model that broke it (see History) is understood.

> Filename kept (`claude_schema_respond_tool.md`) because CLAUDE.md files link to it; the `respond`
> tool it was named for no longer exists.

## Decision

On the Claude provider, `LLMRequest.response_schema` is forwarded **natively** via
`output_config.format` (`{"type":"json_schema","schema": …}`) — the same shape as Gemini's
`response_json_schema`. The model emits schema-valid JSON as **text**; the adapter returns it
verbatim as `LLMResponse.text`. There is **no** synthesized `respond` tool, no tool-call
interception, no safety-net retry.

The schema is shaped for Anthropic's grammar compiler before forwarding (`claude_adapter.py`):

1. `_nullable_to_union` — `{"nullable": true}` → a JSON-Schema `["<type>", "null"]` union
   (Anthropic's grammar has no `nullable` keyword).
2. `_make_strict` — `additionalProperties: false` on every object; drop unsupported keywords
   (`maxLength`, numeric/array constraints). **No `required` injection** → optional/variant keys
   stay optional.

## Why (the journey)

The synthesized `respond` tool (the previous decision) failed on **`claude-sonnet-5`**: the model
leaked its internal `<parameter name="…">…</parameter>…</invoke>` tool-call serialization into the
output. Debugged exhaustively 2026-07-02 with raw request/response capture (`ANTHROPIC_LOG=debug`
+ per-call `tools`/`content` dumps):

- `tool_choice=auto` → the model bypassed `respond` into (clean) plain text, which tripped the
  safety-net `tool_choice=any` retry — and **that forced retry** produced the leak (~100% of
  bypasses on Sonnet 5; the doc's earlier "any 0/3" was measured on 4.x).
- `tool_choice=any` (primary) → no bypass, but the forced `respond` call **still** crammed the whole
  envelope as `<parameter>` XML into the first field (`input KEYS = ['full_response']`).
- `strict: true` + discriminated `anyOf` → structure enforced (all 4 keys present) but content
  **corrupted**: the real widget/summary/links bled into the strings as XML while grammar-forced
  fields got stubs (`{"html":"<div></div>"}`). Worse than before.

Conclusion: the `<parameter>` leak **is** the tool-call format. No `tool_choice`, prompt, or
schema tuning removes it — it is intrinsic to Sonnet 5 generating this multi-field tool call.
Removing the tool removes the format that leaks. `output_config.format` constrains the model to
emit JSON as **text**, where no tool-call serialization exists.

**Attribution — UNRESOLVED, do not over-credit `output_config.format`.** `output_config.format` was
reverted 2026-07-01 over [anthropic-sdk-python#1204](https://github.com/anthropics/anthropic-sdk-python/issues/1204)
(reasoning leak / empty-text-block on multi-turn + adaptive thinking), and it was still leaking two days
before this change. When we switched back to it (2026-07-02) we **simultaneously** re-added the full
formal `json_schema` to the `OUTPUT_FORMAT_JSON` prompt token (it had drifted / been de-duplicated
before). Both changed at once — **the variables were not isolated** (the prompt-only-without-format
revision was never tested), so "the format just works now / #1204 is outdated" is **not proven**.

**Working hypothesis (owner's observation, treated as baseline until disproven):** the key stabilizer
is the **full formal schema in the prompt token** — Anthropic relies on the prompt-level schema to get
complex outputs right; the grammar (`output_config.format`) constrains *structure*, the prompt schema
guides *content*. To prove it, use the still-live debug capture (`ANTHROPIC_LOG=debug`) and remove ONE
variable at a time (drop the token schema, or drop `output_config.format`) and watch for the leak.
Result observed 2026-07-02 with BOTH live on `claude-sonnet-5`: clean valid JSON, real widgets/tables,
no `<parameter>` leak.

## Grammar limit — schema must stay compilable

`output_config.format` uses the **same grammar compiler as strict tool use**, so a complex schema
returns `400 invalid_request_error: "Schema is too complex."` (each optional key ~doubles the
grammar state space). The first strict attempt 400'd on `rich_content.data`'s flat bag of ~8
optional variant keys (`2^8` states). Fix: model `rich_content` as a discriminated **`anyOf`**
(`null | widget | table | file`), each variant a closed object declaring only its own keys — 4
small branches instead of `2^N`. Same output shape (`{type, data, fallback}`); downstream
rendering unchanged. This lives in `SmartResponseAgent._RESPONSE_SCHEMA`.

## Consequences

- Gemini/OpenAI paths unchanged (native `response_schema`). The Claude path now matches them
  conceptually (schema forwarded natively) instead of diverging through a tool.
- Removed from the adapter: the `respond` tool injection, `_schema_tool_active`, `tool_choice=any`
  forcing, the plain-text-bypass safety net, `_retry_respond_any`, and the respond-call intercept.
- `_make_strict` / `_nullable_to_union` are **live** (they shape the `output_config.format` schema).
  `_strip_nullable` is now dead (superseded) but retained pending a separate test-cleanup.
- `_RESPONSE_SCHEMA.rich_content` is a discriminated `anyOf` (was a flat variant bag).
- Verified live on `claude-sonnet-5` (widget + table render, JSON valid, no leak) and via a
  provider smoke test that Gemini accepts the `anyOf` schema.

---

## History — the superseded `respond`-tool decision (2026-07-01)

Kept for the record. `772beb8` (2026-04-23) had migrated Claude structured output to the GA
`output_config.format` API; on 2026-07-01 that was reverted to a synthesized `respond` tool
(schema as `input_schema`, `nullable` stripped via `_strip_nullable`, `tool_choice=auto`, adapter
intercepts the call → JSON text) because `output_config.format` produced degenerate output on
multi-turn + adaptive-thinking loops (#1204: reasoning trapped in the first JSON string field on
Sonnet; empty text block alongside `tool_use` → `400` on replay on Opus). A `tool_choice=any`
retry recovered plain-text bypasses. That approach held on the 4.x models but broke on Sonnet 5 —
see **Why (the journey)** above.
