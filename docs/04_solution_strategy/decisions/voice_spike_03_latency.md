# Spike 0.3 — Relay latency: p50 681ms / p95 910ms on OpenAI, good enough for a POC

**Date:** 2026-09-21
**Status:** Live (spike complete, real call data; RFC not yet edited per plan's rule)

## Question

RFC §7 names relay latency as "the number that decides whether the feature is pleasant" — the
gap between the caller finishing a sentence and hearing the model's reply start. This spike
measures it on a real phone call, on the same relay already proven clean for audio format in
spike 0.2 (`docs/04_solution_strategy/decisions/voice_spike_02_mulaw_e2e.md`).

## Method

`scripts/voice/test_mulaw_relay_poc.py` (extended, commits `899988b`/`24a1d08`) times each
exchange from the provider's `input_audio_buffer.speech_stopped` event (the caller finished
talking) to the first `response.output_audio.delta` received afterward (the reply starts
arriving) — not from `speech_started`, which would incorrectly fold in however long the caller
was still mid-sentence. Real call, `PROVIDER=openai`, owner's personal phone → the provisioned
Twilio number → relay (local machine, exposed via `ngrok http 8765`) → `gpt-realtime-2.1`.

**Path measured:** phone → PSTN → Twilio → ngrok tunnel → owner's laptop (Valencia, Spain) →
OpenAI's Realtime API → back. This is **not** the eventual production path — Slice 1 deploys the
relay to Cloud Run (`us-central1` per RFC §5.2's "pragmatic v1: all three on the US path"), not a
developer's laptop behind a home/office connection and an ngrok tunnel. Numbers here are a
lower-confidence proxy for "is the mechanism viable at all," not a production latency forecast.

## Result

12 exchanges, one real call:

```
631, 681, 905, 910, 491, 812, 497, 582, 524, 740, 800, 601  (ms)
n=12  p50=681ms  p95=910ms
```

xAI was not tested this session (owner's call: no value in a second local-laptop run when the
eventual deployment path is Cloud Run, where absolute numbers will differ regardless of provider —
see Revisit-if).

## Verdict

**681ms p50 / 910ms p95 sits inside the working conversational-latency bar this plan set going in
(~800-1200ms p50, per Task 4's own note in the plan document, itself not an RFC-given number but a
standard turn-taking voice UX range) — with margin, even measured over a home connection + ngrok
rather than the eventual Cloud Run deployment.** Good enough to say the mechanism is not
disqualified by latency, on a laptop, on OpenAI. **Not** good enough to set a production
latency budget or to compare providers — that needs the same measurement run from `us-central1`
against both providers, which is real Slice-1-adjacent work, not this spike's job.

**Owner's explicit call, recorded verbatim in intent:** "нет смысла делать сейчас, в облаке будут
немного другие цифры, для ПОК результат хороший" — no value in refining this further at POC
stage; cloud numbers will differ; the POC result is good enough to move on. This spike is
considered closed on that basis, not because every angle was exhausted.

## Revisit if

- **Before Slice 1 ships**, re-run this same measurement with the relay actually deployed to
  Cloud Run (`us-central1`) against both providers — the laptop+ngrok path here is not
  representative of production network topology, and RFC §5.2's region-alignment discussion
  (Twilio media region, provider endpoint, relay region all needing to agree) is untested by this
  spike.
- **xAI's latency was never measured** — if xAI (`grok-voice-think-fast-2.0`) is still a live
  provider candidate at Slice 1 time (see spike 0.1's finding that xAI never demonstrated
  text-mode output and its late-injection signal came from the audio-transcript channel only),
  its latency profile is a real unknown, not assumed equal to OpenAI's.
- **Reasoning effort was not explicitly set in this spike's relay script** —
  `test_mulaw_relay_poc.py`'s `session_update_event()` sends no `reasoning` field at all. No spike
  in this plan determined what OpenAI's realtime API actually defaults to when the field is
  omitted, so this 681ms p50 figure cannot be attributed to any specific effort level (`minimal` or
  otherwise) — that would be an assumption, not a measured fact. This matters more now that the
  owner has set `medium` as the actual shipping default (Task 5 / 0.4's addendum): if the true
  server-side default already sits at `medium` or higher, this number is not a floor. Before
  treating 681ms as any kind of production latency baseline, re-measure with `reasoning.effort`
  explicitly set to `medium`.
