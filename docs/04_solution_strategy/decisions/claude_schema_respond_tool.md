# Claude structured output: `respond` tool, not `output_config.format`

**Date:** 2026-07-01
**Status:** Accepted (reverts the 2026-04-23 `output_config.format` migration, `772beb8`)

## Decision

On the Claude provider, `LLMRequest.response_schema` is enforced by injecting a synthesized
`respond` tool (the schema becomes its `input_schema`, `nullable` stripped recursively via
`_strip_nullable`) with `tool_choice=auto`. The adapter intercepts the `respond` tool call and
returns its input as JSON text — callers see a normal `LLMResponse.text`. We do **not** use
Anthropic's GA `output_config.format` (constrained-decode) for `response_schema`.

## Context

`772beb8` (2026-04-23) migrated Claude structured output from a `respond`-tool hack to the GA
`output_config.format` API. On multi-turn delegation loops with adaptive thinking, that path
produces degenerate output — two symptoms, both **[anthropic-sdk-python#1204](https://github.com/anthropics/anthropic-sdk-python/issues/1204)**:

- **Reasoning leak (Bug 2, observed on Sonnet):** the model's planning narration is trapped in
  the first JSON string field (`full_response` starts with "...keeping full JSON structure in
  mind...}. Let me format this now.}" + stray `}`). Structurally valid, semantically polluted.
- **Empty text block (Bug 1, observed on Opus):** the model emits `{"type":"text","text":""}`
  alongside its `tool_use` blocks; stored in `raw_content`, replayed next turn, and the API then
  rejects it (`400 messages: text content blocks must be non-empty`) → Smart fails → Quick.

Root cause: `output_config.format` forces every text output to match the schema, which fights a
multi-turn tool loop (intermediate turns must delegate, not emit the final schema) and traps
in-band reasoning in the constrained text block. The `respond`-tool path keeps the answer in the
tool_use **input** and reasoning in the separate `thinking` block — no constrained-text failure mode.

## Alternatives considered

- **Keep `output_config.format`, wait for Anthropic (#1204 open).** Rejected: the degeneracy is
  user-facing (hard 400s and silent empty answers) and the fix is fully ours to make.
- **Filter empty text blocks on `raw_content` replay.** Rejected as masking a symptom, not the cause.
- **Rely on the OUTPUT_FORMAT prompt token + parse-retry only.** Rejected: unenforced structure —
  Anthropic breaks it periodically (this is the pre-schema failure mode we already lived through).
- **Force `tool_choice=any` (legacy).** Rejected: verified 2026-07-01 that `any` makes Sonnet
  spuriously call `delegate_to_specialist` on a terminal turn (extra loop iteration).

## `tool_choice=auto` + enriched declaration + non-leaky safety net

- **Primary: `tool_choice=auto`.** Empirically (2026-07-01, opus/sonnet/haiku 4.x) the model calls
  `respond` on the terminal turn; `any` as the primary spuriously delegates on Sonnet.
- **Enriched declaration.** A bare `respond` tool makes the model bypass (write plain text) or leak
  Claude's internal `<parameter>` tool-call format into the first field on complex prompts. So the
  adapter gives `respond` an explicit generic description ("call this tool; don't emit plain text or
  XML tags") — it cannot reference an agent's OUTPUT_FORMAT, which it doesn't know exists — and each
  agent adds per-field `description`s to its own schema (Smart's `_RESPONSE_SCHEMA`).
- **Safety net: `tool_choice=any` retry.** On a plain-text bypass the adapter re-issues once with
  `any`, which forces respond (structured) or a real delegation call (engine continues) — never plain
  text. The adapter is universal and must not hand plain text to a schema agent that `json.loads` the
  result directly. `any` does NOT leak `<parameter>` the way forcing the *specific* respond tool does
  (verified: specific-tool ~1/4 leak on long output, `any` 0/3). On retry failure it degrades to the
  primary result rather than raising.

## Known residual: stochastic model degeneracy (not fully solved)

On **complex, long** structured outputs opus intermittently dumps the `<parameter>` XML tool-call
format into the first string field even via the tool path. The rate is **stochastic and batch-
dependent** (same config observed at 0/3 and 4/5 across batches), so prompt/description tuning only
moves it in the noise — it is an Anthropic-side degeneracy that affects both `output_config.format`
(reasoning leak / empty text block) and the respond-tool (`<parameter>` leak). The tool path is kept
because it avoids the **hard 400** (empty text block → Smart→Quick) that `output_config.format`
caused; the residual leak is cosmetic (valid JSON, polluted field). Decision 2026-07-02: **leave as
is, do not chase with more prompt tuning** — track incidence; a targeted strip of the `<parameter>`
artifact (the real answer precedes the marker) is a documented fallback if incidence rises.

## Consequences

- Gemini/OpenAI paths unchanged (native `response_schema`). Only the Claude adapter diverges.
- `_make_schema_strict` (additionalProperties/required injection for the GA path) is removed —
  the `respond` tool uses the schema as-is minus `nullable` (via `_strip_nullable`), so
  mutually-exclusive variant keys (`rich_content.data`) stay optional.
- Verified end-to-end live across opus/sonnet/haiku with the real `_RESPONSE_SCHEMA` (nested
  `nullable` in `rich_content` strips cleanly, no 400).
