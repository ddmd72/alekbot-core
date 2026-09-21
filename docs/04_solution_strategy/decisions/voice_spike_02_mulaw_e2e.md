# Spike 0.2 — μ-law end to end: clean, once four unrelated infra bugs were fixed

**Date:** 2026-09-20/21
**Status:** Live (spike complete, real call data; RFC not yet edited per plan's rule)

## Question

RFC §5 claims the relay is "a base64 decode and a byte forward, no resampling, no DSP" because
both Twilio and both realtime providers speak `audio/x-mulaw` / `audio/pcmu` 8kHz natively. This
spike tests that claim end to end on a real call: Twilio → relay (`scripts/voice/test_mulaw_relay_poc.py`)
→ OpenAI's Realtime API → back, checking for garbling, resampling artifacts, or a silent format
reversion (the specific risk named in RFC §7's spike table).

## Method

Real phone call from the owner's personal number to the provisioned Twilio number
(the Spanish local number in `TWILIO_PHONE_NUMBER`), Twilio's Voice webhook pointed at the relay
script (exposed via `ngrok http 8765`),
`PROVIDER=openai`. xAI was not reached this session (see Revisit-if).

## Result: audio was clean

The owner's own words: "связь отличная" (connection quality is excellent). No garbling, no
resampling artifacts, no dropped audio reported once the session actually connected and
exchanged a full turn. This confirms RFC §5's core claim for the OpenAI leg: the `audio/pcmu`
format is accepted end to end with no transcoding needed.

## What it took to get there — four unrelated bugs, none about audio format itself

None of these bugs were about μ-law/audio format correctness — every one was infrastructure the
call had to pass through before audio format even became testable. Recorded because Slice 1's
real webhook handler and `VoiceSessionService` will hit the same class of issues if not designed
around them from the start:

1. **TwiML Bins are US1-only.** The provisioned number's voice processing region is Ireland
   (IE1) — TwiML Bins return `401 Not Authorized` for any number routed outside US1 (confirmed:
   Twilio error 11200, `Got HTTP 401 response to handler.twilio.com/twiml/...`). Not previously
   known to this RFC. Fixed by serving TwiML directly from the relay process instead of a Bin
   (commit `22b3fe7`) — which also sidesteps the region question entirely for Slice 1's real
   webhook handler, since a plain HTTP webhook has no such region restriction.
2. **`websockets==15.0.1`'s HTTP parser only accepts GET.** Twilio's default `voice_method` is
   POST; the library's `Request.parse()` hard-codes an expected-GET check and raises before any
   application-level `process_request` hook runs. Fixed by setting the number's `VoiceMethod` to
   `GET` (commit `a1538d8` documents this in the script). **This is a constraint of using the
   `websockets` library as a combined HTTP+WS server** — Slice 1's real answer webhook (item 4)
   should not inherit this constraint if it's a proper HTTP framework (Quart/aiohttp) fronting a
   separate WS handler, but it's worth confirming explicitly when that's built, not assumed away.
3. **A stale session-config schema, not backported from Task 1's own finding.** This script's
   `session_update_event()` shipped with the pre-GA flat `"modalities": [...]` field — the exact
   shape Task 1's spike (committed earlier the same session) had already found rejected by
   OpenAI's GA API (`missing_required_parameter session.type`). That fix was never propagated
   into this script because it was drafted from the plan's original text, not cross-checked
   against a sibling task's mid-flight discovery. Symptom was silent: OpenAI's rejection came
   back as an `error` event the script didn't print, so the call connected with zero audio and
   zero visible error. Fixed to `{"type": "realtime", "output_modalities": ["audio"], ...}`
   (commit `4d4775d`), which also added error-event logging so this class of failure is never
   silent again.
4. **Charles Proxy MITM breaks outbound TLS** (owner's machine runs a debugging proxy) —
   `ssl.SSLCertVerificationError: self-signed certificate in certificate chain` connecting to
   `wss://api.openai.com`. Already a known, documented issue in this repo's memory
   (`reference_charles_proxy_python_ssl.md`) and in Task 1's own report; recurred here because
   it's a per-invocation environment condition (`NO_PROXY='*'`), not something fixable in the
   script itself.

**Process lesson, not a code fix:** bug 3 above is a real gap in how this plan's tasks were
sequenced — a fact Task 1 discovered mid-flight never got checked against Task 3's already-drafted
brief before Task 3 ran. Worth a standing habit for the rest of this plan (Tasks 4-6, and any
future multi-task spike sequence): before dispatching a task that shares code/assumptions with an
earlier one, re-check the earlier task's actual findings, not just the original plan text.

## Additional, non-rigorous finding: barge-in

Beyond this task's original scope, a minimal interruption handler was added live (commit
`2869844`: on `input_audio_buffer.speech_started` mid-response, send Twilio a `clear` event and
OpenAI a `response.cancel`) to get a first read on whether barge-in is viable at all. Result:
**mechanically works** (confirmed on the real call — the model's speech did eventually stop when
interrupted) **but felt slow** to the owner, and the model's default persona was verbose (no
`instructions` were set in this bare-bones session, so it used its default chatty behavior).
**This is not a rigorous latency measurement** — no `turn_detection` tuning was attempted, and the
owner's own read (§7's own framing of this kind of finding) was "let's test this properly under
Task 4 instead of tuning by feel mid-call." Recorded as informative context for Task 4, not as
this spike's answer to anything.

## Verdict

**RFC §5's core claim holds for OpenAI**: `audio/pcmu` end to end, no resampling, no DSP, clean
audio on a real call. **xAI not verified this session** (see Revisit-if). The four infrastructure
bugs above are not a mark against the RFC's design — none of them are about the audio path itself
— but bugs 1 and 2 are concrete, previously-unknown constraints that Slice 1's real webhook
handler (item 4) and deployment (item 3) should account for explicitly rather than rediscover:
region-independent webhook delivery (not a TwiML Bin), and GET vs. POST if `websockets` (or a
similar minimal library) is reused as the answer-webhook's transport.

## Revisit if

- **xAI leg of this spike was never run** — `PROVIDER=xai` against the same real-call setup is
  still open. Given `grok-voice-think-fast-2.0`'s session-config differences already surfaced in
  Task 1 (older `"modalities"` key, no genuine text-mode output), the xAI audio leg may have its
  own surprises worth a short follow-up call before Slice 1 commits to a provider. **Note:**
  `test_mulaw_relay_poc.py`'s `session_update_event()` currently raises `NotImplementedError` for
  `PROVIDER=xai` (it only implements OpenAI's GA session-config shape) — that needs Task 1's
  xAI session-config fix ported in before this follow-up run can happen.
- **Barge-in latency**, properly measured with `turn_detection` tuned and a real `instructions`
  prompt set — this is Task 4's job (0.3, relay latency), not a redo of this ad hoc test.
