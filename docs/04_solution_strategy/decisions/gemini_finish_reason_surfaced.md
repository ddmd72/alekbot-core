# finish_reason reaches the caller, and a recitation block is retried once

**Date:** 2026-08-13
**Status:** Accepted
**Related to:** `gemini_thought_parts_split.md` (the previous incident on the same parse path)

## Context

`HtmlPageGeneratorAgent` was asked to render a morning-briefing page from ~15 KB of verbatim news
copy (Europa Press, 20minutos). Gemini answered **HTTP 200 with zero parts**:

```
⚠️ [GeminiAdapter] Empty content/parts
   (finish_reason=FinishReason.RECITATION finish_message=None thoughts=2665 safety=None)
❌ error in html_page_generation: LLM returned empty HTML
```

The recitation filter blocked the output for reproducing source material too closely. Two defects
followed, both in our code:

1. `_parse_response` returned a bare `LLMResponse(text="")` on the blocked path — the reason was
   logged and then discarded. Every caller saw a provider refusal and a stalled model as the same
   value, and told the user "LLM returned empty HTML", which is neither true nor actionable.
2. Nothing retried. The block is a property of *how* the output was phrased, not of the request
   being impossible — the same assignment with a paraphrase constraint normally passes. `RETRY_POLICY`
   could not help: it only sees exceptions, and this is a 200.

Prevalence: one occurrence in 30 days of logs. Rare, but silent and user-visible when it lands.

## Decision

- `LLMResponse.finish_reason: Optional[FinishReason]` — a domain enum (`STOP`, `MAX_TOKENS`,
  `SAFETY`, `RECITATION`, `OTHER`). `GeminiAdapter` maps every blocking reason it can return,
  including the `IMAGE_*` variants; unmapped values become `OTHER` and the raw value stays in the log.
- `BaseAgent._call_llm_recitation_aware()` — one retry with `RECITATION_RETRY_DIRECTIVE` appended to
  the last user message via `append_user_directive`. Single-shot generation agents only
  (`HtmlPageGenerator`, `PdfGenerator`); explicitly not for delegation loops.
- `describe_empty_output(finish_reason)` turns the reason into user-facing text, so a blocked page
  now reports what to change instead of "the model returned nothing".

## Rejected alternatives

- **Retry inside `RETRY_POLICY`.** It is exception-driven; a recitation block is a successful call.
- **Retry inside `GeminiAdapter`.** The adapter would have to invent the corrective wording and
  re-enter generation — policy in a translation layer, and invisible to billing per turn.
- **Append a second user message instead of extending the last one.** Two consecutive user turns
  with no model turn between them is a malformed transcript on role-alternating providers.
- **Raise a typed `LLMRecitationError`.** Turns a partial-but-usable response (the filter can also
  truncate rather than blank) into a hard failure, and every caller would need a handler.
- **Map `finish_reason` for Claude and OpenAI too.** Deferred at first, done 2026-10-10 — see
  *Extension to all LLM adapters* below.
- **Localize the new error text.** Consistent with the existing hardcoded-English error path
  (`ERROR_APOLOGY` tech debt); localizing one string in isolation buys nothing.

## Triggers to revise

- A second provider starts reporting refusals → do the Claude/OpenAI mapping rather than widening
  the Gemini one.
- Recitation blocks stop being rare (say, more than weekly) → the fix belongs in the prompt that
  builds these requests, not in a retry.
- The retry starts firing twice for the same request regularly → the directive wording is not
  landing; tune it before adding a third attempt.

## Extension to all LLM adapters (2026-10-10)

`FinishReason` is the domain contract; every LLM adapter now translates its provider's signal into
it, so a caller never depends on which provider ran.

| Adapter | Source | Mapping |
|---|---|---|
| Gemini | `candidate.finish_reason` | as above |
| Grok, OpenAI | Responses API `status` + `incomplete_details.reason` | `completed` → STOP; `incomplete`: `max_output_tokens` → MAX_TOKENS, `content_filter` → SAFETY, else OTHER |
| Claude | `stop_reason` | `end_turn`/`stop_sequence`/`tool_use` → STOP; `max_tokens`/`model_context_window_exceeded` → MAX_TOKENS; else OTHER (`refusal` is raised earlier by `_raise_on_refusal`) |

Not reported → `None`, as before (Claude's grounded `pause_turn` loop leaves it unset).

**Trigger:** VideoGenerationAgent on `grok-4.7`, 2026-10-10. A crafted `video_prompt` was cut
off at `max_tokens=500` mid-string; `json.loads` failed, and with `finish_reason=None` the user
was told "the model returned nothing". The agent now reports a truncation as "cut off at the
output length limit", and any other malformed JSON as "malformed JSON (N characters)". It no
longer reports non-empty output as empty, so its message stays accurate even if a provider leaves
`finish_reason` unset.

**Unverified live:** the Responses-API reason strings follow OpenAI's documented values. xAI
claims compatibility, but no truncated xAI response has been captured to confirm it. If live
truncations still arrive as `finish_reason=None`, probe xAI's `status` field before widening
the mapping.
