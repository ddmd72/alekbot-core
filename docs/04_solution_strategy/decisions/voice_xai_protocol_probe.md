# xAI realtime protocol probe — what holds for Lelik on Grok

**Date:** 2026-09-29. **Script:** `scripts/voice/test_xai_realtime_protocol_poc.py` (live,
`grok-voice-think-fast-2.0`, synthesized speech over server_vad, no Twilio).

`VoiceSessionService` owns the turn cycle and barge-in on the assumption that the provider neither
replies nor cancels on its own (`RealtimeSessionPort.receive_events`: "the provider does not reply
on its own"). The probe checked that assumption and the rest of the session shape before writing
`XaiRealtimeAdapter`.

| Question | Result |
|---|---|
| `turn_detection.create_response: false` | **Accepted and echoed, NOT honoured.** `response.created` arrives ~200 ms after `speech_stopped`, before `input_audio_buffer.committed`, and generates audio. 5/5 speech turns. |
| `interrupt_response: false` | Accepted and echoed. Not exercised mid-generation: a long reply finished generating in ~6 s (faster than real time), so barge-in is playback-side anyway. |
| `response.create` while a response is active | No error. The active one ends `cancelled`, a new one starts (OpenAI errors here). |
| `response.cancel` with nothing active | `error` "Cancellation failed: no active response found" (same as OpenAI). |
| `conversation.item.truncate` | Works: `conversation.item.truncated` with the cut transcript. |
| `conversation.item.create` role `system` | Accepted (`conversation.item.added`). |
| Event names | OpenAI GA names: `input_audio_buffer.speech_started/stopped/committed`, `conversation.item.input_audio_transcription.completed`, `response.output_audio.delta`, `response.output_audio_transcript.done`. `conversation.item.added`, not `.created`. |
| `speech_started` timing | Prompt in 9/9 fresh-session runs (onset reported ~2 s into synthetic speech at default threshold 0.85). **Twice delivered late, batched with `speech_stopped`**, both when the model had just spoken. Watch in UAT: late onset = no mid-sentence barge-in. |
| Formats | `audio/pcmu` (phone) and `audio/pcm` rate 24000 (web) both work. |
| Usage | **Top-level `usage` on `response.done`** (not under `response`): OpenAI-shaped token details plus `output_audio_seconds` and `billable_audio_seconds`. A cancelled response still bills. |
| `reasoning.effort` | `high` or `none`. Time to first audio: none 0.79–0.82 s, high 1.05–1.45 s. |
| Model default VAD | Without `turn_detection` in session.update: `speech_started` only, no commit. |

Follow-up probes: `response.create` echoes `response.metadata` on created/done; `response.cancel`
honours `response_id` (a mismatch is an error, not a cancel of the active reply); a cancelled
reply's item is gone from the conversation (`conversation.item.delete` → "Item not found", and the
model reports it said nothing).

**Decision (owner, 2026-09-29): the adapter absorbs the auto-reply.** It tags its own requests,
cancels every untagged reply by id at `response.created`, and swallows that reply's events and the
cancel errors. Its usage rides on the next `response_done`. `VoiceSessionService` is unchanged.
A live adapter smoke confirmed it: one reply per turn, and the tool call round trip was clean.

Rejected:
- **Provider-owned turns.** The persona anchor moves into instructions, and the service gains a
  per-provider mode. That loses the blip filter (`barge_in_min_speech_s`) and needs the late-answer
  policy reworked. It also makes an A/B test compare two call behaviours, not two providers.
- **Manual turns (`turn_detection: null`).** It loses server VAD, which is barge-in's only signal.

Revisit if xAI starts honouring `create_response` (the flag is already sent). Also revisit if UAT
hears clipped starts: a reply cancelled after its first audio chunk left the provider.

**UAT outcome (owner, 2026-09-29, one web call): rejected, relay back on OpenAI.**
- Audio quality was better than `gpt-realtime-2.1`.
- The delivery had "zero emotion" (voice `rex`, effort `high`).
- Grok recites prompt text verbatim. Three times it spoke the English few-shot line "Sent the
  scouts out." from `FEW_SHOT_EXAMPLES_LELIK` as its lookup acknowledgement. gpt-realtime took the
  same few-shots as a pattern. Lelik's prompt is tuned to how gpt-realtime reads it, and
  that does not carry over.
- Grok is not viable without its own prompt profile for Lelik. The mechanics held: no provider
  errors, delegation worked, and the reply was not cut.

**Second test (owner, 2026-09-30): bare Grok.** Idea: the rules tuned for gpt-realtime may be
what hurts Grok, so the test removes them all. Setup:
- **Relay:** `VOICE_XAI_BARE=on`. Grok owns its turns: `create_response` and `interrupt_response`
  are on and nothing is cancelled. The relay sends no persona anchor, no caller opening and no
  silence or waiting notes. On barge-in it only drops audio it has already queued. It still
  starts the greeting (the pickup note) and delivers delegation answers.
- **Main service:** `LELIK_PROMPT_PROFILE=lelik_bare`. That profile carries character (archetype,
  vibe, Smart's shared Ranevskaya humor), language, memory, a role with no behaviour rules and a
  plain tool list. It drops SPOKEN_DELIVERY, the few-shots, the policies and the copilot
  register.
- **Voice:** `castor`, picked by ear from TTS samples.
