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
- **Map `finish_reason` for Claude and OpenAI too.** Deferred — their analogues (`stop_reason=refusal`,
  `incomplete_details.reason`) were not verified against the live APIs, and this repo requires wire
  tests per adapter. They return `None`, which preserves today's behaviour exactly.
- **Localize the new error text.** Consistent with the existing hardcoded-English error path
  (`ERROR_APOLOGY` tech debt); localizing one string in isolation buys nothing.

## Triggers to revise

- A second provider starts reporting refusals → do the Claude/OpenAI mapping rather than widening
  the Gemini one.
- Recitation blocks stop being rare (say, more than weekly) → the fix belongs in the prompt that
  builds these requests, not in a retry.
- The retry starts firing twice for the same request regularly → the directive wording is not
  landing; tune it before adding a third attempt.
