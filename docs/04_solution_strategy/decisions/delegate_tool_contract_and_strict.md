# Function tools are sent with `strict: false`; every prose-named field is a declared schema field

**Date:** 2026-10-09
**Status:** Live
**Scope:** `delegate_to_specialist` (and every function tool) in `OpenAIAdapter` / `GrokAdapter`;
`capability_descriptions` vs `context_schemas` in `src/infrastructure/agent_manifest.py`.

## Problem

Two Smart turns on `gpt-6.1-sol` went silent for ~13 minutes and emitted 64 000 output tokens,
almost all whitespace, inside a `delegate_to_specialist` call:

- 2026-10-07 21:19 — `create_pdf`, 806 s. The turn still delivered.
- 2026-10-09 16:33 — `deep_research`, 817 s. The args were truncated JSON, nothing was dispatched,
  the next call ran out of the turn clock, and the turn failed. The research never started.

The flood was not inside a string. In both cases it began at a **key position inside
`"context":{`** — whitespace between JSON tokens, i.e. inside the tool-call grammar.

## Cause

1. `OpenAIAdapter._convert_tools` sent no `strict`. The Responses API then defaults function tools
   to `strict: true` and rewrites the schema: every property required,
   `additionalProperties: false`, keys emitted in schema order. Probed 2026-10-09: a tool with an
   optional `note` came back with `required: [color, note]`. xAI defaults to `strict: false`.
2. `context` is generated only from `context_schemas` (`BaseAgent._build_delegate_tool_declaration`),
   but the `deep_research` description told the model to send
   `payload: {"query", "language", "brief"}`, and no schema declared those two keys.
3. At `"context":{` the model wanted to write `language`. The grammar masked that key and left
   whitespace as the only legal token, so the model looped until `max_output_tokens`.

Replay of the stored 2026-10-09 request (`scripts/validation/replay_tool_arg_runaway.py`,
gpt-6.1-sol, same instructions, input, tools and effort, `max_output_tokens=8000`):

| Arm | Runaways | `context` |
|---|---|---|
| A — production (strict, keys undeclared) | **4 / 45**, all at `"context":{` | always empty |
| B — `language` + `brief` declared | **0 / 45** | both filled in 30/30 logged runs |
| C — `strict: false` | **0 / 35** | JSON valid in 35/35 |

The 2026-10-07 `create_pdf` request did not reproduce in 5 runs. Its stall position matches the same
mechanism, but the masked key behind it was not identified.

## Decision

1. **Make every prose-named field a declared schema field.**
   - `DEEP_RESEARCH` declares `language` and `brief`. `DeepResearchAgent` appends
     "Please write the entire response in {language}." again, as `DEEP_RESEARCH_RFC.md` §3.3
     specifies. That line was lost in `0078947`, and its read in `33fb214`, as an unused variable.
   - `MANAGE_USER_TASKS` used to say `"context": "<optional background>"`. A string `context` is
     normalized to `{"reasoning": …}`, so the agent's `payload["context"]` read never got it. It now
     declares `background`, and `TasksAgent` reads that.
   - `tests/unit/infrastructure/test_manifest_prose_matches_tool_schema.py` fails CI when any field
     named in a `payload: {…}` / `context={…}` block is missing from the **generated** tool schema.
2. **Send `strict: false` explicitly on every function tool** (OpenAI and Grok, including Grok's
   synthesized `deliver_response`). `context` is one object shared by ~15 intents, documented as
   "fields for intents that require them". Strict grammar contradicts that: it forces every intent's
   field on every call. It also turns any future prompt/schema mismatch, including ones in
   Firestore `PROTOCOL_*` tokens that no repo test can see, into a 13-minute hang instead of a
   tolerated extra key. Relying on a provider default is how this went unseen.

## Alternatives rejected

- **Streaming guard that aborts on a whitespace run, or an idle timeout.** Both treat the symptom,
  and the owner rejected this approach. An idle timeout would not even fire, because tokens keep
  flowing.
- **Keep strict and only fix the manifest.** Fixes the known cases. The class stays live for any
  mismatch the test cannot see (prompt tokens live in Firestore).
- **Lower Smart's 64k `max_tokens`.** Shortens the hang and cuts reasoning headroom; see
  `model_refresh_2026_10`.

## Cost of the choice

Without strict, a model may send an undeclared key, or omit a declared one. Specialists already read
their fields with `.get(…)` defaults, and the replay produced valid JSON in 35/35 runs. Arm C also
showed that an undeclared field is simply not sent. That is why declaring fields (decision 1) still
matters with strict off.
