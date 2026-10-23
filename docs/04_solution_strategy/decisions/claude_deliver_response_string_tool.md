# Claude ends structured tool loops through a string `deliver_response` tool

**Date:** 2026-10-23
**Status:** Accepted, not built. **Supersedes part of** `sonnet55_adaptive_thinking_stall.md` (its "no
workaround in our adapter" stance, for structured tool loops only) and **part of**
`claude_schema_respond_tool.md` (`output_config.format` stays the path for schema requests without
client tools; requests with tools move to the tool below).

## Decision

When a Claude request carries a dict `response_schema` **and** at least one client (function) tool, and is
not a grounded / server-tool request (`use_grounding`), `ClaudeAdapter`:

1. does **not** send `output_config.format`;
2. appends a synthesized tool `deliver_response` with **one** required string parameter `response`
   ("your whole reply: the JSON object described in the response format, serialized as a string"). The
   answer schema is **not** in the tool parameters; it reaches the model through the agent's prompt
   (Smart: the `OUTPUT_FORMAT_JSON` token);
3. when the model calls it, returns `ToolCall("deliver_response", args={"response": <the string as the
   model gave it>})`. The adapter does **not** parse the answer. `extract_structured_response` runs
   `parse_llm_response` on a string `response`, so the text path and the deliver path share one parser
   and all its repairs (quote repair, `\n` normalisation, list-shaped `rich_content`). A dict `response`
   is taken as the answer object. Grok is unchanged: its args are the schema fields themselves.

**Smart, Quick and Tutor all take this path.** Quick moves in the same change: it defaults to Claude
(`src/services/agent_context_builder.py`, `STRATEGIES["quick"]`) and formats every `notify()` alert, and
today it calls `DelegationEngine.execute` without `terminal_tool`, so a `deliver_response` call would get
"unknown tool" and loop. Quick passes `terminal_tool="deliver_response"` and reads its answer through
`extract_structured_response`.

Applies to every Claude model. Out of scope:

- **Consolidation**: it has tools but no schema, so the trigger does not apply. Both paths were measured fine.
- **Router**: it has a schema but no tools, so the trigger does not apply, and it cannot run on Claude in
  production (`allowed_providers` are grok / gemini / openai). Its Gemini-style uppercase schema types
  (`"OBJECT"`) get a 400 from Claude's `output_config.format`; that matters only if Router ever moves to
  Claude. Not fixed here (owner).
- **Deep research**: own SDK path.

## Evidence (2026-10-23, synthetic probes plus one real-stack run; about $8 in all)

**Baseline.** Sonnet 5.5, adaptive thinking, effort medium, Smart's real response schema, 6 intents, the
weather scenario. Live loop: 7/8 answered, 14 repeated identical calls. Hand-built "all data gathered"
history (the model only has to answer): Sonnet 5 4/5 filler calls, Sonnet 5.5 4/5 a tool call instead of
the answer, Opus 5.5 1/5.

**What did not fix it.**

| Attempt | Result |
|---|---|
| `between_tools` thinking | Fixes the stall, but the owner measured worse results on important tasks. Rejected. |
| A stop rule in the prompt | Rejected: it pushes good turns to answer early. |
| Required `reason` / `motive` parameter on `delegate_to_specialist` | Removed the long repeat loops. Fillers remained, and the model labelled them itself ("placeholder", "no call", "nothing more needed"): it has decided to stop, but it answers through the wrong channel. |
| Typed `need` enum with a "none" outlet | Labels every filler (100% precision), but the model does not obey a tool_result telling it to answer: one run marked "none" 9 times. |
| Param-less `deliver_response`, answer as text, grammar kept | 16/16 called it with **no** text, so the answer never arrived. |
| Strict answer tool with the full schema | 400 "compiled grammar is too large". |
| July's non-strict `respond` with the schema as parameters | `<parameter>` leaks (`claude_schema_respond_tool.md`). |

**String `deliver_response`, synthetic** (8 live loops + 8 hand-built histories per model):

| Model | Usable answers | Fillers | Repeats | Markup leaks | Stray chars after a complete object |
|---|---|---|---|---|---|
| Sonnet 5.5 | 16/16 | 0 | 0 | 0 | 0 |
| Opus 5.5 | 16/16 | 0 | 0 | 0 | 2 |
| Haiku 5.5 | 16/16 | 0 | 0 | 0 | 4 |
| Fable 5.1 | 16/16 | 0 | 0 | 0 | 0 |

All 64 answers covered the whole question. Each of the 6 cases with characters after the closing brace
(a trailing `"}`, `}` or `, "link_list": []}`) was recovered by first-object parsing. Today's
`parse_llm_response` does not recover them.

**Real stack.** Real Smart prompt (it holds the formal JSON schema), real read-only specialists, writes
stubbed. The adapter change was emulated by a monkeypatch of `ClaudeAdapter.generate_content`.

| Case | Current path | `deliver_response` |
|---|---|---|
| Smart, Sonnet 5.5, table answer | 1/2 ok; **1/2 failed**: 15 turns, 12 fillers, no answer | 2/2 tables, 3 turns |
| Smart, Opus 5.5, table answer | 2/2, 4-5 turns | 2/2, 3 turns |
| Smart, Haiku 5.5, simple questions | 5/5, 1 turn | 4/4, 1 turn: without the grammar it simply writes JSON text |

Tables on the deliver path had the correct `[{cells:[…]}]` shape, and no run leaked markup. The live
`OUTPUT_FORMAT_JSON` token says "RAW JSON only, first char '{', no prose", but the deliver path still
delivered every table, so in practice the tool description wins.

**Why it works where July's `respond` leaked.** The tool has no answer fields, so there is no field
structure to leak into: one string. It also removes the trigger triple of the upstream structured-output
bug (grammar + thinking + tools in a multi-turn conversation, anthropic-sdk-python#1204).
*Interpretation, not proven:* the model wants to end a tool turn with a tool call; giving it a legitimate
one removes the filler.

## Alternatives considered

All attempts in the table above. Also: keep "no workaround" and leave Sonnet off Smart (the 2026-10-20
position). Rejected: Sonnet 5.5 is not the only model that makes filler calls (Sonnet 5 4/5 and Opus 5.5
1/5 on the hand-built history), and the string tool fixed the stall on every model tested, in the same
number of turns or fewer. Parsing the string in the adapter: rejected in favour of one parser (Decision 3).
That keeps the args a dict whatever the model sends, and the text and deliver paths cannot drift apart.

## Implementation requirements

1. **Trigger:** computed from the converted client tool list (after `_convert_tools`, before grounding
   tools are prepended).
2. **`parse_llm_response`:** `json.loads(strict=False)`; strip a `json` code fence; on failure
   `raw_decode(strict=False)` starting at the first `{`; log which strategy won. A plain-text JSON answer
   (Haiku's 1-turn case) is no longer grammar-constrained.
3. **Truncation:** under `stop_reason == "max_tokens"`, a `deliver_response` call whose `response` is
   missing, empty, or neither a string nor a dict is dropped by the adapter with an ERROR log. An agent
   that ends with empty user text and no rich content must not report success silently. The plan designs
   the exact handling, starting from what Smart, Quick and Tutor do with empty final text today.
4. **Unparseable string:** handled the same way as today's text path (the fallback inside
   `parse_llm_response`), plus an ERROR log. It is no worse than today.
5. **Text next to the terminal call:** the terminal result carries the response text, which is used when
   `full_response` is empty.
6. **Sync sibling** co-emitted with `deliver_response`: a named WARN log only, no behaviour change (as on
   Grok, `terminal_tool_co_emitted_calls.md`).
7. **One structured log line per deliver parse:** `parsed` / `first_object` / `repaired` /
   `unparseable` / `truncated`.
8. **Preserved thinking:** the tools list must not change mid-conversation. Anthropic's documentation
   says it validates that system, tools and earlier messages are unchanged since a thinking block was
   produced (this comes from Anthropic's docs, not our code). The engine's wrap-up turn copies
   `base_request` changing only messages and timeout. Confirm with a wire test.
9. **Prompts:** check that Quick's and Tutor's prompts carry the schema.
10. **Tests:** wire tests + contract validators per `docs/how_to/ADAPTER_WIRE_TESTING.md`.
11. **Docs:** root `CLAUDE.md` Patterns line ("live on Grok, cold elsewhere"); `src/agents/CLAUDE.md`
    (terminal-tool roster line and the `deliver_response` note); `src/adapters/CLAUDE.md` (structured
    output section, and the stale "the OUTPUT_FORMAT_JSON token no longer duplicates the schema" line);
    the stale `respond` debug-capture comment and the "native only" comments in `claude_adapter.py`.

## Consequences

- The 2026-10-20 "no workaround in our adapter" stance is superseded for structured tool loops. Its
  consolidation monitoring (turn-cap alert, temporary stats line) stays. Whether Sonnet returns to Smart
  is the owner's call after deploy.
- On Claude, a request with schema + tools no longer gets a grammar-constrained answer: structure comes
  from the prompt schema and the shared parser. `output_config.format` remains for schema requests
  without tools.
- Claude and Grok now end structured tool loops through the same synthesized `deliver_response`. They
  differ in where the schema lives (Grok: tool parameters; Claude: the prompt) and in what the args hold
  (Grok: the answer fields; Claude: one `response` string).
- **Telemetry moves.** On Claude, the final answer now appears in BigQuery `prompt_content.tool_calls`,
  not `response_text`, and Logfire counts one more tool call per turn. The observability skill needs a
  note (outside the repo, owner).
- `_drop_document_citations` runs only when `output_config.format` is sent, so on the deliver path PDF
  citations are kept.
- The answer is JSON escaped inside a JSON string, so it costs extra output tokens. Measure after deploy.
- Optional owner follow-up: reword `OUTPUT_FORMAT_JSON` ("RAW JSON only, first char '{'") for the tool
  path. It is not changed here, because prompt uploads are owner-run.
