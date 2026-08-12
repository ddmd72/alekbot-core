# Gemini thought parts are split from the answer, not dropped

**Date:** 2026-08-12
**Status:** Accepted
**Related to:** `logfire_prompt_content_capture.md` (the observability split this feeds)

## Context

`215788d` — a commit titled "fix: OpenAI cache_write_tokens extraction" — also enabled
`include_thoughts=True` on Gemini's `ThinkingConfig`. Gemini then returns an extra part whose
`.text` is the reasoning summary; `part.thought` is a **bool flag**, not the text.

The extractor in `GeminiAdapter._parse_response` joined `.text` across every part:

```python
text = "".join([p.text for p in candidate.content.parts if p.text])
```

Because the thought part comes first, the reasoning was prepended to the answer.

## Damage (both observed in production, 2026-08-12)

- A user-facing HTML briefing was delivered with **4244 bytes of reasoning before `<!DOCTYPE`** —
  11.5% of the file, opening with "**My Approach to Crafting a Ukrainian HTML Newspaper Edition**".
- `EmailClassificationAgent` fed the reasoning transcript to `json.loads`, which failed at char 0 on
  both retries → `classify_batch: parse_error`.

Logs date it precisely: `parts=1` with `thoughts=2823` before the change, `parts=2` after. Thinking
always ran and was always billed — the flag only controls whether the summary is returned.

**Scope note:** `invalid JSON on turn 2` also occurred on 2026-08-09, before the regression (model
emitted prose instead of JSON; the retry recovered). The regression did not invent that error — it
turned an occasional flake into a deterministic failure for every JSON-parsing Gemini agent. The
HTML leak is attributable to the regression without qualification.

## Decision

**Split the two channels; keep both.**

- `text` takes non-thought parts only — the answer, and nothing else.
- The reasoning goes to a new `LLMResponse.thought_text`, and onward to
  `PromptContentRecord.thought_text` (a nullable `STRING` column added to the live
  `prompt_content` table). `record_turn` already receives the whole `LLMResponse`, so no port
  signature changed.
- `include_thoughts=True` **stays on.** Thinking is billed whether or not the summary comes back, so
  discarding it buys nothing and loses queryable insight into what the model actually did.
- `raw_content` stays **unfiltered**. Gemini requires thought blocks to be resent exactly as
  received for reasoning continuity ("You MUST always resend all thought blocks… You should NOT
  remove or modify thought blocks from the history"), so the replayed model turn must keep them.
  An earlier draft of this fix proposed stripping them — that would have been wrong.
- The dead thought-extraction block from `215788d` is deleted: it tested
  `isinstance(p.thought, str)` against a bool, so it never fired. The advertised observability never
  worked; only the leak did.

## Alternatives rejected

- **Turn `include_thoughts` off.** Restores known-good behaviour with the least surface, and was the
  first recommendation. Rejected by the owner: the reasoning is already paid for, belongs in
  BigQuery/logs, and is the input to a planned feature that streams live "thinking" text into chat
  instead of a static "думаю" placeholder. Suppressing a signal to avoid mishandling it is the wrong
  trade when the mishandling is a one-line filter.
- **Strip thought parts from `raw_content` too.** Contradicts Gemini's documented requirement above.
- **Filter at each call site.** Every consumer of `text` would have to know about thought parts;
  the adapter is the one place that knows the provider's part taxonomy.

## Consequences

- Any consumer wanting reasoning reads `thought_text`; `text` is the answer by construction, so a
  future `include_thoughts` flip on another provider cannot re-introduce this class of corruption.
- Historical `prompt_content` rows have `thought_text = NULL`; the column is additive and the 3095
  existing rows were untouched.
- **Deploy ordering matters and is now satisfied:** the column had to exist before the writer
  shipped, because `insert_rows_json` rejects the *entire row* on an unknown field and the adapter
  only logs insert errors — the failure mode would have been a silent telemetry blackout, not a
  crash.

## Verification

- New tests build **real `google.genai.Part` objects**, not `MagicMock`: a mocked `part.thought` is
  truthy regardless of what the code does, so a mock-based test passes against the broken extractor.
  Confirmed red before the fix (`text` came back as reasoning+answer concatenated), green after.
  Covers: thought excluded from `text`; thought-only response yields empty `text` rather than
  promoting reasoning; `json.loads(text)` succeeds with a thought part present; `thought_text`
  captured; `thought_text` is `None` without a thought part; `raw_content` retains both parts;
  `include_thoughts` stays enabled.
- `make test-unit` 4550 passed, `ruff check src/` clean.
- Row/schema parity checked against the live table before shipping: 21 record fields vs 21 columns,
  no extra field that would reject a row.
