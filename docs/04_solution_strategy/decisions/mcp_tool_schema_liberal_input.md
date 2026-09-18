# The MCP tool advertises a constraint-free schema and normalizes input server-side

**Date:** 2026-09-18
**Status:** Live
**Scope:** `get_user_context` — the only tool on the remote MCP server exposed to claude.ai
Custom Connectors.

## Problem

External MCP clients sent malformed arguments on the **first** call after connecting — the
call that matters most, because there is no prior turn to learn from.

## Two causes, both in the emitted schema

The handler declared `alternate_phrasing: Optional[str] = None` and
`keywords: Optional[List[str]] = None`. FastMCP derives the wire schema from the annotations
via pydantic, so those rendered as:

```json
"keywords": { "anyOf": [ { "items": {"type": "string"}, "type": "array" }, { "type": "null" } ], "default": null }
```

1. **`anyOf` with `null`.** Clients collapse the union to an untyped value; the parameter's
   type is effectively lost and the model guesses the shape.
2. **No per-parameter `description` — the larger cause.** The schema carried only
   auto-generated titles (`"Alternate Phrasing"`, `"Keywords"`). Every instruction about the
   arguments lived in the prose tool description, which is not read reliably at
   argument-construction time.

Both `docs/05_building_blocks/remote_mcp_server/README.md` and
`docs/10_rfcs/REMOTE_MCP_SERVER_RFC.md` documented a clean schema that the code never
produced. The drift was never caught because `src/composition/mcp_setup.py` had **no test at
all** (deferred as F16.3 in `r18_2_polish_tier_deferred.md`).

## Decision

**Accept liberally, advertise strictly.**

- Optionality is expressed as `default: "" / []`, never `Optional[T]`.
- Every parameter carries a `description` containing a concrete example value.
- **No validation constraint is advertised** — no `minLength`, `maxLength`, `maxItems`,
  `pattern`, `additionalProperties: false`.
- `pydantic.BeforeValidator` hooks run `normalize_phrase` / `normalize_keywords`
  (`src/domain/mcp.py`, pure functions, no I/O) before validation. They do not appear in the
  emitted schema, so the advertised contract stays clean while explicit nulls, delimited
  strings, JSON-encoded arrays, casing, embedded spaces and excess tags are all absorbed.

Both invariants are asserted in `tests/unit/composition/test_mcp_setup.py` against the real
`list_tools()` output, and the coercion table in `tests/unit/domain/test_mcp_tool_input.py`.

## Why no constraints in the schema

This was the one contested point, and it is the crux of the decision.

FastMCP calls `call_tool(validate_input=False)` on the low-level server and validates
arguments with **pydantic** instead. A constraint violation therefore does not produce a
polite schema warning — it raises `ToolError`, which the SDK converts into a `CallToolResult`
with `isError: true` carrying the raw text `Error executing tool get_user_context: 1
validation error …`. The model must then recover and call again.

**Every advertised constraint is a potential extra round-trip.** So a constraint is only worth
advertising if rejecting the call beats interpreting it — and for this tool it never does:

| Proposed constraint | What it would reject | What the server does instead |
|---|---|---|
| `maxItems: 5` | a 6th tag | keeps the first 5, drops the rest silently |
| `pattern: "^\\S+$"` | `["mcp schema"]` | splits it into two tags |
| `additionalProperties: false` | an unknown extra argument | ignores it |
| `minLength` / `maxLength` | a short or long phrase | trims and searches |

Bounds are still *stated* — in the parameter descriptions, where they steer generation
without gating execution.

## Error contract

`isError` is reserved for genuine failures: authentication, a search backend fault, and a call
carrying no search terms at all. That last one is the **only** branch permitted to instruct
the model, because it is the only input the server cannot resolve on its own. A blank `query`
accompanied by usable `keywords` still searches.

**Zero results is a success**, rendered as `No records matched.` Not an error, and explicitly
not a suggestion to rephrase and retry: any string the server was about to write in the form
"do it this way and call again" should have been *done* rather than written. The server has no
better phrasing available than the caller already produced.

Note the prior behaviour this replaces: the two authentication branches returned their message
as a **successful** tool result, so a client could not distinguish an auth failure from
retrieved content. They now raise `ToolError`.

## Description softened

`ALWAYS call this tool before answering any question from the user` was removed. It competed
with the user's own client-side preferences, and the observed effect was a model oscillating
between over-calling and improvising arguments. The replacement states when to call
("whenever personal context could change the content or tone of the answer, including when
unsure"), when to skip, what each retrieval vector does, and that an empty result is valid.

This is the only **behavioural** change in the set — the rest are mechanical. If call
frequency drops, revert this string alone; the schema work is independent.

## Deferred, deliberately

- **Server-side translation of a non-English `query`.** Would add an LLM call to a path whose
  entire value is that it bypasses the agent stack: latency, cost and a new failure mode on
  every call. Per-parameter descriptions are the cheap lever; revisit only if telemetry shows
  clients still sending non-English `query` values.
- **`search_phrase_2=alternate_phrasing or query`** (`mcp_setup.py`). When
  `alternate_phrasing` is empty, phrase 1 == phrase 2, so RRF sees the same facts at the same
  ranks in two result lists and roughly doubles their score relative to tag/metadata hits.
  Passing `""` instead would drop the `phrase2_metadata` channel entirely, so this is a
  retrieval-tuning question, not a schema one. Left unchanged rather than altered blind.

## Verification

Unit tests cover the schema shape and the coercion table. The acceptance criterion that
cannot be unit-tested — *N consecutive real calls with no corrective round-trip* — is a live
observation: check Logfire for `get_user_context` spans after deploy.

**Client-side caching caveat:** claude.ai caches `tools/list` at registration time. To pick up
the new schema the connector must be **removed and re-added** — see README § 10.
