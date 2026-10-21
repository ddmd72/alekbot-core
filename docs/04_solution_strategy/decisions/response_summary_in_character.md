# Smart's response_summary is written in Alek's character

**Date:** 2026-10-21
**Status:** Accepted (owner decision).

## Decision

`response_summary` (Smart's per-turn summary stored in history) is written in Alek's own character:
his humour, always with emojis, keeping the facts, numbers and names. Budget: up to 60 words, hard
cap 600 characters (`maxLength` in `_RESPONSE_SCHEMA`; was 300). `HistorySummaryService`, the
fallback when the model omits it, follows the same rule. The `OUTPUT_FORMAT_JSON` Firestore token's
field guideline and examples are updated alongside.

## Why

- Older turns reach Smart, consolidation and Lelik only through this summary. Dry summaries teach
  the next reader a dry register; Lelik's opening history slice made him terse.
- For an LLM reader, emojis carry tone in few tokens.
- A word budget, because a model follows "60 words" better than a character count; the 600-char cap
  is the backstop.

## Costs

- Longer history in Smart's prompt: about twice the summary part of each older turn.
- `HistorySummaryService` is shared, so Quick and Tutor summaries take the same style.
- Lelik's history-entry cap (`LelikPersonaService.MAX_ENTRY_CHARS`) rose 500 → 700 so a 600-char
  summary is not cut; user turns in his slice may be 200 chars longer too.

## Rejected

- `full_text` for Lelik: tables, service tails, tool noise.
- A separate restyling LLM pass: latency and cost on every turn.

## Revisit if

- Smart's prompt cost from history grows noticeably.
- Summaries drift into performance (jokes replacing facts) and consolidation loses content.

## Call notes too (2026-10-21)

Lelik's end-of-call note follows the same principle: `LelikSummarizerAgent` writes it as Lelik
himself retelling the call (first person, his `VOICE_*_LELIK` tokens instead of Smart's slots,
matching his `response_text`, emojis, up to 60 words). Soft wording did not hold on the ECO model:
one of three notes slipped back to "користувач…", so the role token states the voice rule first,
names the voice sections, forbids the third-person words literally and gives one wrong/right pair in
English. Token sources: `firestore_utils/uploads/COGNITIVE_PROCESS_LELIK_SUMMARIZER.*`,
`lelik_summarizer.json`. Accepted residual: in character it occasionally embellishes a detail.
