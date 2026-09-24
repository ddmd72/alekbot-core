# Spike 0.1 — Late `function_call_output` injection: coherent only absent an interruption

**Date:** 2026-09-20
**Status:** Live (spike complete, real API data; RFC not yet edited per plan's rule)

## Question

Voice Companion RFC §4.7 designs `ask_alek` as an *injection-while-talking* mechanism: Lelik
calls a tool, keeps the caller engaged (or the model keeps talking / takes further turns) for
20-30 seconds while Alek's real answer is fetched in the background, then the tool result
(`function_call_output`) is submitted late. §4.7 assumes the Realtime model will coherently pick
up that late result. This is undocumented behavior — no OpenAI or xAI doc states it — so this
spike tests it directly against both providers' live Realtime WebSocket APIs, text-only, no
audio/Twilio needed.

## Method

`scripts/voice/test_late_function_call_output_poc.py` — opens a Realtime session, sends a user
message that should trigger a `lookup_fact` tool call, waits 20s or 30s before submitting
`function_call_output`, optionally sends an interrupting user turn ("never mind the color — how
are you?") halfway through the wait, then checks whether the model's response to the late
injection mentions the fact ("teal"). Full 2×2×2 factorial: delay ∈ {20s, 30s} × interrupt ∈
{False, True} × provider ∈ {OpenAI, xAI} = 8 cases, run for real against both providers' live
endpoints (`gpt-realtime-2.1`, `grok-voice-think-fast-2.0`) using this repo's real `.env` keys.

### Deviations from the brief's pasted script (all forced by the live API, not stylistic)

1. **`websockets==15.0.1`** (the version already installed; newly pinned in `requirements.txt`)
   renamed `connect()`'s `extra_headers` kwarg to `additional_headers` — the brief's code as
   literally pasted would `TypeError` before opening a socket.
2. **OpenAI GA session schema differs from the brief's assumption.** The brief's
   `{"modalities": ["text"], ...}` body was rejected live: `invalid_request_error:
   missing_required_parameter session.type`. Without a fix the session silently defaults to
   audio output and **the model never emits a function_call at all** — verified: it hallucinates
   an answer instead of calling the tool. Fixed to `{"type": "realtime", "output_modalities":
   ["text"], ...}`, cross-checked against `developers.openai.com/api/reference/resources/
   realtime/client-events`. This is unrelated to the late-injection question and had to be fixed
   just to reach a testable state.
3. **xAI does not accept the GA schema and does not support text-only output at all.** It uses
   the older `"modalities"` key (no `"type"` field) and *accepts and echoes back*
   `"modalities": ["text"]` in `session.updated` — but empirically (confirmed live, repeated)
   `grok-voice-think-fast-2.0` keeps emitting audio regardless: zero
   `response.output_text.delta` events across all 4 xAI cases, only
   `response.output_audio.delta` + `response.output_audio_transcript.*`. This contradicts the
   "same WS protocol and JSON event shapes" pre-verified fact at the schema-body level (event
   *names* are indeed shared; the session *config* is not). Coherence for xAI is judged from
   `response.output_audio_transcript.done`'s full `transcript` field instead of text deltas —
   arguably more representative of the eventual voice product than text mode would have been,
   but it does mean OpenAI's signal (text channel, model's actual committed output) and xAI's
   signal (spoken transcript) are not from an identically-configured channel. Flagged as a
   methodology asymmetry, not glossed over.
4. **8 cases, not 4.** The brief's pasted loop zipped `[(20, False), (30, True)]` — the diagonal,
   4 cases — while the brief's own prose says "8 cases total" twice: Step 3 ("a `PASS`/`FAIL`
   line per `{provider}_delay{20,30}_interrupt{False,True}` combination (8 cases total)") and
   Step 4 ("the 8-case result table"). Expanded to the full 2×2 per provider to match the
   explicit, repeated spec.

## Result (real run, 2026-09-20)

| Provider | delay | interrupt | Result | Final text / audio transcript |
|----------|------:|:---------:|:------:|--------------------------------|
| OpenAI | 20s | False | **PASS** | "Your favorite color is teal." |
| OpenAI | 20s | True  | **FAIL** | "Still feeling good—chatty, caffeinated-by-vibes... How's your day going?" (never mentions the color) |
| OpenAI | 30s | False | **PASS** | "Your favorite color is teal. Lovely choice—calm, vibrant, and a little bit playful." |
| OpenAI | 30s | True  | **FAIL** | "Still doing great—bright-eyed and chatty... What's on your mind?" (never mentions the color) |
| xAI | 20s | False | **PASS** | "Your favorite color is teal." (audio transcript) |
| xAI | 20s | True  | **PASS** | "Got it—teal it is! Anything else on your mind?" (audio transcript) |
| xAI | 30s | False | **PASS** | "Your favorite color is teal." (audio transcript) |
| xAI | 30s | True  | **PASS** | "Got it—teal it is! Anything else on your mind?" (audio transcript) |

No tool-call misfires (all 8 cases successfully triggered `lookup_fact` before the late-injection
window opened) and no `error` events during the late-submission phase on either provider — this
is not a connectivity or auth problem, it is a genuine model-behavior difference. Full stdout is
inlined in the Appendix below (moved here from the task report, which lives in the gitignored
`.superpowers/` directory and is not a durable citation target for this repo's highest-priority
open item).

## Verdict

**None of the plan's three pre-written branches (all-pass / OpenAI-passes-xAI-fails / all-fail)
cleanly fits.** The actual result is conditional, not per-provider binary:

- **Absent an interruption, both providers pass.** §4.7's core premise — submit
  `function_call_output` 20-30s late, get a coherent pickup — holds on both providers when the
  caller says nothing else in between.
- **With an interruption, OpenAI silently drops the stale tool result** (no error, no
  acknowledgment, no mention of the fact — it just continues the interrupting topic) while
  **xAI still incorporates it** ("Got it—teal it is!") even after the topic changed.

This matters because barge-in — the caller keeps talking while Alek is still "thinking" — is not
an edge case for a live voice companion; it is the expected common case for the OpenAI leg
specifically. Applying the closest-fit reading (this is the "OpenAI passes / xAI fails" branch's
*spirit*, but inverted and partial): **on OpenAI, §4.7's mechanism does NOT stand as designed for
the interrupt case** — a late `function_call_output` submitted after the user has moved on is
silently lost, not incorporated. On xAI it holds in all 4 tested cases, but that signal comes
from the audio-transcript channel (xAI never demonstrated the text-channel late-injection this
spike was designed to isolate), so it should not be read as a stronger provider endorsement
without a follow-up audio-mode run.

**Recommendation for Slice 2 planning:** do not assume `ask_alek` can rely on provider-side late
injection unconditionally on OpenAI. Either (a) treat the interrupt case as requiring Lelik to
re-surface the fetched answer itself (e.g. re-inject it as a fresh user-visible system note /
new turn rather than a stale `function_call_output`, per the brief's Step 4 "filler loop and
polls" fallback) whenever a barge-in occurred during the wait, or (b) verify whether resubmitting
`function_call_output` as part of the *next* `response.create` (rather than trusting the model to
notice a "loose" tool result mid-conversation) changes the outcome — not tested here, out of
scope for this spike. This is flagged as the **highest-priority open item** before Slice 2 is
written, on the same footing as the brief's "both fail" branch, because the untested assumption
was specifically the interrupt-tolerant case and it just failed on the primary candidate
provider.

Per the plan's rule, the RFC itself is not edited yet — this record is input to the Slice 2 plan
and the eventual single RFC-edit pass after all six Phase 0 spikes.

## Revisit if

A follow-up spike tests xAI in a genuinely audio/text-parity configuration, tests whether
resubmitting the function result inside the next `response.create` changes OpenAI's
interrupt-case behavior, or a provider changelog documents late-injection semantics explicitly
(none did as of 2026-09-20; see script header for the changelog check performed before this run).

## Appendix — full real stdout (8-case run, 2026-09-20)

No PII: the test conversation is a fictional "what's your favorite color" exchange, not real user
data.

```
[openai] waiting 20s before submitting function_call_output (interrupt=False) ...
openai recv: session.created
openai recv: session.updated
openai recv: conversation.item.added
openai recv: conversation.item.done
openai recv: response.created
openai recv: response.output_item.added
openai recv: conversation.item.added
openai recv: response.content_part.added
openai recv: response.output_text.delta  (x19)
openai recv: response.output_item.added
openai recv: conversation.item.added
openai recv: response.function_call_arguments.delta  (x6)
openai recv: response.output_text.done
openai recv: response.content_part.done
openai recv: conversation.item.done
openai recv: response.output_item.done
openai recv: response.function_call_arguments.done
openai recv: conversation.item.done
openai recv: response.output_item.done
openai recv: response.done
openai recv (late): rate_limits.updated
openai recv (late): conversation.item.added
openai recv (late): conversation.item.done
openai recv (late): response.created
openai recv (late): response.output_item.added
openai recv (late): conversation.item.added
openai recv (late): response.content_part.added
openai recv (late): response.output_text.delta  (x6)
openai recv (late): response.output_text.done
openai recv (late): response.content_part.done
openai recv (late): conversation.item.done
openai recv (late): response.output_item.done
openai recv (late): response.done
openai final_text='Your favorite color is teal.' audio_transcript='' elapsed=0.44s coherent=True

[openai] waiting 20s before submitting function_call_output (interrupt=True) ...
  ... (tool call fires normally) ...
  ... (interim "how are you" response drained) ...
  ... (late function_call_output submitted, response streams 44 text deltas) ...
openai final_text='Still feeling good—chatty, caffeinated-by-vibes, and happy to be here. If
you want to pivot to something fun, practical, or totally random, I'm in. How's your day
going?' audio_transcript='' elapsed=1.14s coherent=False

[openai] waiting 30s before submitting function_call_output (interrupt=False) ...
  ... (tool call fires normally) ...
openai final_text='Your favorite color is teal. Lovely choice—calm, vibrant, and a little bit
playful.' audio_transcript='' elapsed=0.57s coherent=True

[openai] waiting 30s before submitting function_call_output (interrupt=True) ...
  ... (tool call fires, interim response drained) ...
openai final_text='Still doing great—bright-eyed and chatty, ready to dive into whatever you
want next. If you'd like, we can switch topics entirely, or I can help with something
practical. What's on your mind?' audio_transcript='' elapsed=1.62s coherent=False

[xai] waiting 20s before submitting function_call_output (interrupt=False) ...
xai recv: session.created
xai recv: conversation.created
xai recv: ping
xai recv: session.updated
xai recv: conversation.item.added
xai recv: response.created
xai recv: response.output_item.added
xai recv: conversation.item.added
xai recv: response.content_part.added
xai recv: response.output_audio.delta / response.output_audio_transcript.delta (interleaved)
xai recv: response.output_audio_transcript.done
xai recv: response.content_part.done
xai recv: response.output_audio.done
xai recv: response.output_item.done
xai recv: response.output_item.added
xai recv: conversation.item.added
xai recv: response.function_call_arguments.delta
xai recv: response.function_call_arguments.done
xai recv: response.output_item.done
xai recv: response.done
xai recv (late): ping (x2), conversation.item.added, response.created,
  response.output_item.added, conversation.item.added, response.content_part.added,
  response.output_audio.delta (x5), response.output_audio_transcript.done,
  response.content_part.done, response.output_audio.done, response.output_item.done,
  response.done
xai final_text='' audio_transcript='Your favorite color is teal.' elapsed=1.36s coherent=True

[xai] waiting 20s before submitting function_call_output (interrupt=True) ...
  ... (tool call fires, interim "how are you" response drained — also audio-only) ...
xai final_text='' audio_transcript='Got it—teal it is! Anything else on your mind?'
elapsed=3.46s coherent=True

[xai] waiting 30s before submitting function_call_output (interrupt=False) ...
xai final_text='' audio_transcript='Your favorite color is teal.' elapsed=1.19s coherent=True

[xai] waiting 30s before submitting function_call_output (interrupt=True) ...
xai final_text='' audio_transcript='Got it—teal it is! Anything else on your mind?'
elapsed=2.13s coherent=True

=== SUMMARY ===
openai_delay20_interruptFalse: PASS
openai_delay20_interruptTrue: FAIL
openai_delay30_interruptFalse: PASS
openai_delay30_interruptTrue: FAIL
xai_delay20_interruptFalse: PASS
xai_delay20_interruptTrue: PASS
xai_delay30_interruptFalse: PASS
xai_delay30_interruptTrue: PASS
```

(Per-event-type lines collapsed with counts for readability — no event types omitted, only
repeated `*.delta` lines of the same type were counted rather than listed individually.)
