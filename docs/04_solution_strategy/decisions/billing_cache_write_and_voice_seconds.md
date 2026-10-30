# Billing: OpenAI cache writes were billed twice, xAI voice seconds were summed as a running total

**Date:** 2026-10-13
**Status:** Accepted
**Related to:** `openai_cache_write_tokens_location.md`, `voice_xai_protocol_probe.md`,
`billing_per_model_pricing.md`, `docs/10_rfcs/VOICE_MULTI_PROVIDER_RFC.md`

## Context

The owner compared the daily Billing Summary with the provider dashboards and found it higher.
For 2026-10-12, one account showed `1,837,216 tok / $4.7557`. The suspicion was voice calls.
Reading the data showed two independent defects. That account's line had no voice in it at all.

## Defect 1: OpenAI cache-write tokens billed at 1× + 1.25×

`OpenAIAdapter` set `prompt_tokens = input_tokens − cached_tokens` and
`cache_creation_tokens = cache_write_tokens`. `calculate_cost` treats the three legs as
**non-overlapping**, the way Anthropic reports them. OpenAI's `cache_write_tokens` is a *subset* of
`input_tokens`, so every written token was billed once as prompt (1×) and again as a write (1.25×).

Evidence:
- **Live probe, 2026-10-13, `gpt-6-luna`, Responses API.** The same ~7k prompt was sent twice with
  one `prompt_cache_key`. Call 1 returned `input_tokens=7017, cached_tokens=0, cache_write_tokens=7014`.
  Call 2 returned `input_tokens=7017, cached_tokens=7014, cache_write_tokens=0`. The input total did
  not move, so the written tokens are inside it.
- **Production, 30 days of `prompt_content`.** ~2,800 OpenAI rows carry a write, and on every one
  `cache_creation_tokens = prompt_tokens − 3`. `cache_creation_tokens > prompt_tokens` never occurs.
  The first post-fix call in `openai_cache_write_tokens_location.md` (prompt 11977, write 11974)
  already showed the overlap; that record was about *where* the field lives, not what it overlaps.

Repriced at our own rates, 30 days of OpenAI text calls come to **$47.25 as recorded vs $35.86
actual (−24%)**. On 2026-10-12 the account above drops from $4.76 to $3.27. The token counter was
inflated the same way, because the written tokens were counted twice.

**Fix:** `prompt_tokens = input − cached − cache_write`. The adapter logs a WARNING when
`cached + cache_write > input`. That shape appears in a third-party usage object quoted in
`openai_cache_write_tokens_location.md` but never in our production data. If it occurs, the legs
overlap and cost is over-reported, so it must not be hidden by the `max(…, 0)` clamp.

## Defect 2: xAI `billable_audio_seconds` is a running total, and we summed it

`XaiRealtimeAdapter` passed `billable_audio_seconds` from each `response.done` through unchanged,
and `VoiceCallBuffer.add_usage` sums every response. xAI reports that field as the **session's
running total**. The token legs on the same event are per response.

Evidence:
- **Live probe, 2026-10-13** (`scripts/voice/test_xai_realtime_protocol_poc.py usage`). Three
  replies arrived at 6.9 s, 14.5 s and 23.7 s of session time and reported 7, 14 and 23 billable
  seconds. Output tokens per reply were 224, 205 and 273, which is per response, not cumulative.
  Summing the seconds gives 44 for a 23-second session.
- **Production, call `15ab5540…`, 2026-10-12.** The relay log puts it at 18:49:35–18:53:43, about
  4 minutes. It was recorded as 1861 s, so $2.48 instead of about $0.33. Long calls were hit hardest
  because the error grows with the square of the number of replies. Recorded xAI voice cost from
  2026-09-29 to 2026-10-12 is about $19.3; the real figure is a fraction of that.

**Fix:** the adapter keeps the highest running total it has seen and emits only the increment. One
adapter is built per call (`relay_main._ADAPTERS`), so this needs no cross-call state.
`VoiceCallBuffer` stays provider-agnostic and keeps summing. Each `response.done` now logs the
running total and the delta.

Also fixed: `/voice/submit-transcript` added the seconds into the account's token counter.
`domain.billing.realtime_token_count` sums only the `*_tokens` legs.

## Realtime usage legs

`domain.billing.calculate_realtime_cost` prices the flat legs `OpenAIRealtimeAdapter._flatten_usage`
produces. `reasoning_tokens` are a subset of the output text leg, never additive.

## Known remaining gaps (not fixed here)

- **Seconds after the last reply.** xAI bills the session until hang-up, but the last
  `response.done` reports the running total only up to that reply. A tail of silence before the
  hang-up is not counted. This is a small under-report.
- **xAI text input is priced per event, not per token.** docs.x.ai (checked 2026-10-13) lists
  `$0.004 / text input`, meaning per `conversation.item.create`: anchors, injected answers and notes.
  `_REALTIME_PRICING_PER_MILLION_TOKENS` prices it at $0.004 per **million tokens**, which is
  effectively zero. Fixing it means counting the events in the relay. That is a few cents per call,
  so it is left as a known under-report. The audio rate, $0.08/min, was re-verified the same day.

## Consequences

- **Historical cost is over-reported,** a fifth such defect after the inflated pre-2026-07-28 ledger,
  pre-2026-07-30 model misattribution, missing pre-2026-08-11 cache-write extraction and Sonnet's
  intro pricing.
  - OpenAI: from 2026-08-11 (when write extraction started) to this deploy.
    Reprice from BigQuery with `prompt_tokens − cache_creation_tokens` for `gpt-*` models.
  - xAI voice: from 2026-09-29 to this deploy. Real duration comes from the relay log, not
    from the recorded seconds.
  - Account documents are not rewritten; same stance as the earlier records.
- Claude and Gemini are unaffected. Claude reports non-overlapping legs, and Gemini has no
  write leg.
