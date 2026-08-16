# Transcription cost is not billed — it is infrastructure overhead

**Date:** 2026-08-16
**Status:** Accepted (deliberate deferral)

## Decision

Speech-to-text spend is **not** recorded in `TokenLedger` or the account counters. It is
treated as infrastructure overhead, like Firestore reads or GCS storage, and is visible only
as a line on the OpenAI invoice.

This is a deferral written down on purpose, not an oversight: someone will later notice that
voice messages produce no `usage.*` entry and wonder whether it is a bug. It is not.

## Why

`gpt-transcribe` costs **$0.0045 per minute of audio**. Ten voice messages a day of about
half a minute is roughly **$0.68 a month**; ten users on the same habit is about **$7 a
month** — against a daily alert threshold of $5 that this could not move if it tried.

The ceiling is what makes it safe to ignore: transcription is bounded by how long a human
speaks into a phone. Token spend can spike an order of magnitude from one badly shaped
delegation loop; a voice note cannot.

**The rule this looks like it violates does not apply here.** "Audio is a different rate
class and must get its own billing legs" was written in `VOICE_COMPANION_RFC.md` §4.9 about
*realtime sessions* at $32/$64 per 1M audio tokens, where a single conversation costs more
than a month of transcription. Two orders of magnitude apart; the same words, a different
problem.

## What it would have cost to do properly

Not exploratory work — the shape is known, which is part of why deferring is safe:

- a `TranscriptionResult` value object (the port returns a bare `str` today),
- a **second change to `AudioTranscriptionPort.transcribe` in two days**, breaking the port
  contract test again,
- transcription rates and a pricing function in `domain/billing.py`,
- a `QuotaService` dependency injected into `ConversationHandler`, which has no billing
  dependency today, plus wiring through both platform factories.

A new dependency between the conversation entry point and billing, for a figure nobody can
see move.

## Rejected alternatives

- **Full ledger integration** — above. Real work for an invisible number.
- **Log the cost per call and leave billing alone** — the cheap middle. Rejected by the owner
  as still not worth the line: if the amount does not matter, neither does watching it.
- **Estimate from file size** — would have been necessary and unreliable, except it isn't:
  the API returns `usage` in the response (`UsageDuration.seconds`, or `UsageTokens` with
  `input_token_details.audio_tokens`), and `QuotaService.record_usage` already accepts `cost`
  directly. Both are the reason the deferred work stays small.

## Revisit if

- voice becomes a primary input channel rather than an occasional one (say, transcription
  minutes per user per day reach double digits),
- per-user quotas or chargeback are introduced, so every provider call must be attributable,
- the provider moves transcription to token pricing at a materially higher rate, or a
  streaming tier (~$0.017/min, ~4x) is adopted.

Related: [`voice_message_transcription.md`](voice_message_transcription.md).
