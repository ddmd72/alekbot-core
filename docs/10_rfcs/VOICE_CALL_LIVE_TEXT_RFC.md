# RFC: Live call text on the web call screen

**Status:** Accepted (owner decisions recorded 2026-11-08, §8). Not built.
**Date:** 2026-11-08
**Owner:** Dmytro
**Milestone:** Voice — modes, next

**Related:** `VOICE_WEB_TRANSPORT_RFC.md` (the page, the SFU, the relay), `VOICE_MULTI_PROVIDER_RFC.md` (turn ownership),
`decisions/lelik_voice_modes.md` ("on-screen text" is its listed next step), `decisions/voice_xai_protocol_probe.md`.
The control-channel RFC (branch `docs/voice-control-channel-rfc`, being written in parallel) owns the page↔relay socket;
this RFC only names what rides on it. No POC exists: the production code is the spec, plus the xAI probe of
2026-11-08 (12 runs, the full event stream of `grok-voice-3.0`, two of them quoted in §2).

---

## 1. Problem

During a web call the screen shows only "Live" and a timer. In the two modes that is the weak point:

- **Interpreter.** The person across the table hears a translation once. Text they can read (and re-read) is what
  a printed phrasebook gives and a voice does not.
- **Spanish tutor.** A learner keeps up far better reading the sentence they hear, and seeing what the recogniser
  understood of their own Spanish is pronunciation feedback.

Today nothing reaches the page during a call. The adapters map only whole utterances — the caller's
`conversation.item.input_audio_transcription.completed` → `user_transcript`, Lelik's
`response.output_audio_transcript.done` → `model_transcript` (`src/adapters/xai_realtime_adapter.py:147-150`,
`src/adapters/openai_realtime_adapter.py:254-257`). The relay concatenates them per turn
(`src/services/voice_session_service.py:406-424`) into `VoiceCallBuffer` (`src/domain/voice_call_buffer.py:28-30`) and
sends the whole transcript to the main service once, at call end (`voice_session_service.py:282-287`).

## 2. What the provider actually gives (xAI probe, 2026-11-08)

| Fact | Evidence (probe runs `q1_both`, `q4_note_run2`) |
|---|---|
| Lelik's text streams: `response.output_audio_transcript.delta`, one per phrase of ~3–4 s of speech | 9 deltas over a 32 s reply; 5 over a 14 s reply |
| Each delta carries `start_time` (seconds into the reply's audio) and a `rid` shared with its audio chunks | `start_time` 0, 2.24, 5.44, 8.96 … lands 70–110 ms after the summed audio before it (2.17, 5.35, 8.87 …) |
| Generation runs 11–15× faster than playback | 32.3 s of audio arrived in 2.1 s; 14.1 s in 1.25 s |
| A delta arrives with or before its audio, never after | `q4_note_run2`: the 2nd phrase (`start_time` 1.92) arrived at 7.951, its audio at 8.135 |
| The caller's text has **no deltas**: only `…transcription.completed`, at the commit | 0.5–1.9 s after `speech_stopped`, in the same millisecond as the reply's first item, 0.3–0.4 s before its first audio |
| The recogniser is separate and errs where the model did not | heard "latest news about **Sad** James Webb"; the reply was right |
| Built-in search (if ever enabled) shows up only as `function_call` items **after** the spoken reply | the delay is visible only as a later first audio (2.19 s vs 0.70 s) |

Not covered by the probe: what xAI sends when the caller interrupts (§9, P1).

**Consequence.** "Text ahead of the audio" is not a few hundred milliseconds: the whole reply's text is on the relay
seconds before the caller hears its second sentence. Showing text on arrival would print 30 s of speech at once and
print words the caller may never hear (barge-in drops the queued audio, `voice_session_service.py:346-349`).
**Text must be released by the playback clock, not by the provider.**

## 3. Goals and non-goals

Goals: (1) the caller's words and Lelik's words on the mode pages, live, in step with the voice; (2) never show as
said what was not played; (3) the interpreter can show a translation to the other person; (4) nothing new persisted.

Non-goals: the control channel itself; changing the main page `/cabinet/call` (owner decision, §8 Q2); a stored
or shared transcript (the record stays `/voice/submit-transcript`); phone calls (no screen); editing recognised text.

## 4. Design

### 4.1 What text, per mode

| Page | Lelik's lines | Caller's lines | Extra |
|---|---|---|---|
| Interpreter | yes, main text | yes, secondary, "as heard" | "Show" view: the latest Lelik line large, flippable 180° (§4.8) |
| Tutor | yes, main text | yes, secondary, "as heard" | none in v1; marked corrections need a model tool (§8 Q6) |
| Main `/cabinet/call` | unchanged in v1 | — | opt-in later only if the owner wants it (§8 Q2) |

Modes are xAI-only (`decisions/lelik_voice_modes.md:28-30`), so v1 needs only the xAI deltas. OpenAI matters only if
the main page gets text.

### 4.2 Timing: the relay paces text by what was played

The relay already owns the playback clock. On a web call `_pace` sends one 20 ms frame to the SFU per tick and records
it as played (`src/handlers/sfu_stream_handler.py:237-261`, `PlaybackTracker.record_played`,
`src/domain/voice_playback_tracker.py:33-40`); `VoiceSessionService` notes where each reply item starts in that byte
stream (`voice_session_service.py:393-396`). Text is timed against the same counter:

- New pure domain class **`LiveTextPacer`** (one per call). Each Lelik delta is stored with an **anchor**: the byte
  offset at which its audio starts = `item_start_bytes + start_time × bytes_per_ms` when the provider gives an offset
  (xAI), else `playback.sent_bytes` at the delta's arrival (OpenAI, any provider without offsets: the text then leads
  by at most the audio still queued for that one phrase).
- A pacer task in `VoiceSessionService` (the `_play_cue` pattern, `voice_session_service.py:553-586`; 100 ms tick)
  releases every phrase whose anchor ≤ `playback.played_bytes`, coalesced into one message per tick.
- Granularity is the phrase (subtitle-like: a phrase appears when its first word is played). Word-by-word reveal is
  a page-side option later: the pacer can attach the phrase's duration (next anchor − this one).
- "Played" is "sent to the SFU". The ear hears it after the SFU and the browser's jitter buffer — unmeasured; probe
  P2. A `LIVE_TEXT_LAG_MS` knob (`os.getenv`, default 0) shifts the release if the text visibly leads the voice.

Port change: the adapters gain `model_transcript_delta` `{item_id, text, audio_offset_ms|None}`, and `user_transcript`
gains `item_id`. The `.done` events keep feeding `VoiceCallBuffer` unchanged.

### 4.3 Barge-in

The cut must be ours whatever the provider does: at 11–15× generation, the reply is usually fully generated before
the caller interrupts, so the provider cannot know which part was heard.

- On interruption the relay calls `pacer.cut(playback.played_bytes)` **before** `clear_outbound_audio()`. Order
  matters: the SFU clear marks everything as played (`sfu_stream_handler.py:268-272`), the same trap the RELAY path
  already avoids by reading `heard_ms` first (`voice_session_service.py:444-451`).
- Both clear sites get the cut: PROVIDER (xAI) at `voice_session_service.py:346-349`, RELAY (OpenAI) inside
  `_barge_in`. The cue-tail clears (`:358-359`, `:389-391`) carry no reply audio; web calls have no cue anyway
  (`SfuStreamHandler` passes no `send_cue_audio`, `sfu_stream_handler.py:128-134`).
- Unreleased phrases are dropped; the line ends with `status: "interrupted"` (the page adds "…"). A phrase already
  released stays whole, though its tail may not have played: at most ~3–4 s of text, acceptable for subtitles.
- OpenAI's `conversation.item.truncate` (`openai_realtime_adapter.py:291`) removes the unheard transcript in OpenAI's
  context but never returns the cut text; it is no help for the screen. xAI's behaviour is probe P1.

### 4.4 The caller's lines: "as heard"

- Order is fixed by the relay, not by arrival: the caller's line id is allocated at `turn_committed`, Lelik's at
  `response_created`; a line's text fills in when it comes. On OpenAI the transcription may finish after Lelik starts
  speaking; the caller's line still sits above his reply. An empty transcript removes the line.
- Shown in the secondary colour, smaller, with a one-time hint "as the recogniser heard it". No editing (the model
  heard the audio, not this text; an edit would change nothing).
- Shown immediately on arrival, not paced: the caller has already said it.
- Echo during the greeting (`voice_session_service.py:166-173`) can appear as a caller line; accepted.
- Later improvements (§7 S5): a language hint or key terms for the recogniser, if xAI's input transcription accepts
  them (unverified; OpenAI's transcription takes `language` and `prompt`).

### 4.5 Thinking, Alek, search

A `status` message, derived from state the relay already keeps (`_CallState`, `voice_session_service.py:81-121`):
`listening` (caller speaking), `thinking` (response active, no audio yet), `speaking`, `asking_alek` (a delegation in
`state.delegations`, no reply audible), `sent_to_chat` (an errand dispatched; a short-lived chip). The page shows a
one-line status under the title, replacing "Live". The delegation's query is **not** shown: it is model wording, and
the interpreter's screen is read by others. Built-in search (not enabled today) would only lengthen `thinking` (§2).

### 4.6 Delivery: the control channel, not a poll through the main service

**A — control channel (recommended).** The relay pushes JSON text frames on the page↔relay websocket of the
control-channel RFC (same relay process; ticket-bound short-lived token minted by main). The pacer, the barge-in cut
and the socket are in one process; nothing leaves the relay but what was played.

**B — relay → main → ephemeral store → page poll.** The relay POSTs each released batch to a new main route (OIDC,
like `/voice/submit-transcript`); main appends to `voice_web_call:{call_id}` in `FirestoreEphemeralStore`
(the record of `src/web/voice_web_call_app.py:127-130`); the page reads new lines through the 2 s status poll
(`src/web/static/call.html:810-828`, `voice_web_call_app.py:197-205`).

| | A | B |
|---|---|---|
| Text behind the voice | ~0.05 s + ear lag | 0.1–2.3 s (release + POST + write + poll) |
| New moving parts | one message type on a channel that is coming anyway | a route, a ticket→call index, a writer task in the relay's hot loop |
| Limits | none at this rate | Firestore sustains ~1 write/s per document: one call's lines are near it |
| Auth | the channel's token | existing Cabinet JWT + owner check (`voice_web_call_app.py:33-37`) |
| Page reload | snapshot on reconnect (§4.9) | free (lines are in the store) |
| Cost | none measurable | ~1 POST + 1 write per phrase, 1 read per 2 s: cents per call-hour |

B is viable only as "captions that trail the voice by up to 2 s"; it cannot be in step, and it moves call text through
Firestore for no gain. **Recommendation: A.** If the control channel is rejected or slips far, §8 Q1 asks whether to
build B as a stopgap.

### 4.7 Messages (relay → page, `v: 1`)

```json
{"v":1,"seq":41,"type":"line","line":"l7","who":"lelik","op":"append","text":"Esta pomada é para queimaduras"}
{"v":1,"seq":42,"type":"line","line":"l7","op":"end","status":"done"}          // or "interrupted"
{"v":1,"seq":43,"type":"line","line":"l8","who":"caller","op":"set","text":"How often should I use it?"}
{"v":1,"seq":44,"type":"status","state":"thinking"}
{"v":1,"seq":45,"type":"snapshot","lines":[{"line":"l1","who":"lelik","text":"…","status":"done"}],"state":"listening"}
```

- `seq` is monotonic per call; the page drops any `seq` ≤ the last applied. Line ids are relay-made (`l1`, `l2` …),
  never provider ids.
- Every (re)connect gets one `snapshot` (the last 50 lines, ≤ 20 KB) and then live messages. No replay protocol:
  the snapshot is idempotent, so dedup is "replace the view".
- Nothing goes page → relay for this feature.

### 4.8 The page

Built into `call.html` behind `window.VOICE_MODE`, the way the mode label already is (`call.html:400-410`), so the
main page stays byte-for-byte the same in behaviour.

- **Layout while live** (`.screen`, max-width 420 px, `call.html:105-116`): the identity block shrinks to one row
  (48 px avatar, title, status), the text panel takes the middle (`flex: 1`, scrolls), the call button stays in the
  thumb zone. Before and after a call the page looks as today.
- **Text:** Lelik's lines `--text-main`, 20 px / 1.4; caller's lines `--text-muted`, 17 px, a small "You" label.
  Sizes via `font: -apple-system-body` scaling so iOS Dynamic Type applies. Light and dark from the existing tokens
  (`call.html:24-62`). Readable at arm's length on a phone is the target; P2 includes a look at it.
- **Scroll:** sticks to the bottom; if the user scrolls up it stops following and shows a "Latest ↓" pill. At most 50
  lines in the DOM.
- **Interpreter "Show" view:** a button (and a tap on any Lelik line) opens a full-screen card with the latest Lelik
  line at ~34 px, high contrast, updating live; "Flip" rotates it 180° for the person opposite; tap to close.
- **Accessibility:** the panel is `role="log"` with `aria-live="off"`: a screen reader speaking every line would talk
  over Lelik. Status text stays as it is. `prefers-reduced-motion`: no reveal animations.
- **XSS:** text is model and recogniser output; it is set with `textContent` only, never `innerHTML`.

### 4.9 Lifecycle

- **Backgrounded / locked (iOS):** a backgrounded web call is out of scope (`VOICE_WEB_TRANSPORT_RFC.md` §3; Wake
  Lock keeps the screen on, `call.html:768-780`). If the socket drops while hidden, the page reconnects on
  `visibilitychange` and the snapshot restores the view.
- **Reconnect:** the channel's own; this feature needs only the snapshot.
- **Resumed provider session** (proposed in parallel): the pacer and the line list live in the relay's per-call
  state, not in the provider session, so a resumed session continues the same lines. A relay restart loses them.
- **Call end:** the text stays on screen until the next Call tap or `pagehide`, then is cleared. Never written to
  `localStorage`; the canonical record stays `/voice/submit-transcript`. Modes get no after-call summary
  (`decisions/lelik_voice_modes.md:31-34`), so a "Copy text" button at the end is the only way to keep a lesson
  (§8 Q5).

### 4.10 Security, tenancy, logging

The channel's token is bound to the call's ticket and its owner (control-channel RFC); a socket only ever receives
its own call's lines. Text is never logged: log lines carry counts and lengths with `fingerprint(ticket)`, as today.
No secrets or ids in messages beyond the relay's line ids. Rate: xAI ~0.3 messages/s per speaking side plus status;
the 100 ms tick caps any provider at 10/s.

## 5. Cost and load

No provider cost: both transcripts are already produced and billed (xAI bills audio seconds). Relay CPU: one 100 ms
task per call next to the 50 frames/s of resampling and pacing — negligible; under 1 KB/s per call on the socket.
Main service and Firestore: untouched under A.

## 6. Tests

Offline (unit): `LiveTextPacer` — anchors with and without offsets, release by `played_bytes`, coalescing, `cut`
before clear, snapshot bounds; adapter wire tests for `model_transcript_delta` built from the probe's real event
shapes (`start_time`, `rid`); `VoiceSessionService` with a fake session and tracker — line order when the caller's
transcript arrives late, cut ordering on both turn ownerships, nothing released past the cut. The page: manual UAT.

Live probes (each a few cents of xAI audio): **P1** barge-in on xAI — a long reply interrupted at ~3 s: is there a
cancel, what do `.done` and `response.done` carry, do deltas keep coming. **P2** ear lag — a UAT call watching text
against voice; set `LIVE_TEXT_LAG_MS` from it. **P3** (only with Q2 = text on the main page) OpenAI delta timing and
whether `gpt-transcribe` streams input deltas.

## 7. Slices

| # | Slice | Needs |
|---|---|---|
| S0 | Probes P1 (+ doc check for xAI recogniser hints) | nothing |
| S1 | Adapter deltas + `LiveTextPacer` + barge-in cut + `line`/`snapshot` messages; plain text panel on both mode pages | control channel slice 1 |
| S2 | Interpreter "Show" view with flip | S1 |
| S3 | `status` messages and the one-line status | S1 |
| S4 | Main page, if Q2 says so (OpenAI deltas, P3) | S1, owner |
| S5 | Later: copy at end, word-level reveal, recogniser hints, tutor corrections (Q6), the heard cut into `VoiceCallBuffer` (§9) | owner |

## 8. Owner decisions (2026-11-08) — all decided

Each of the seven is decided as the RFC proposes (the recommended option of each): accepted as proposed (owner delegated the call, 2026-11-08).

1. **Delivery.** A — the control channel (recommended), or B — poll through main (§4.6). If the channel is late:
   wait (recommended: B is throwaway) or build B as a trailing-captions stopgap.
2. **Main page.** Unchanged (recommended for v1) / a "Text" toggle, off by default / always on.
3. **Text after a barge-in.** Drop what was not played (recommended) / show it dimmed as "not said".
4. **Caller's lines.** Shown dimmed "as heard" in both modes (recommended) / Lelik's lines only.
5. **After the call.** Keep on screen until the next call + a "Copy text" button on mode pages (recommended) / clear
   at hang-up. Or should a tutor session go to chat or the drive instead (a summary-path change, separate)?
6. **Tutor corrections highlighted.** Needs a model tool (e.g. `show_on_screen(text)`) the tutor calls with a
   corrected sentence — a prompt and tool experiment with latency risk. Explore after S1 (recommended) / not at all.
7. **Interpreter "Show".** Latest Lelik line, large, flippable (recommended) / the whole log, larger.

Decided: 1–7 = the recommended option, accepted as proposed (owner delegated the call, 2026-11-08).

## 9. Side findings (outside this RFC)

- **xAI never truncates.** Under PROVIDER ownership the relay only drops queued audio (`voice_session_service.py:346-349`);
  with replies generated 11–15× faster than played, xAI's context holds the whole reply as said after every barge-in.
  The pacer's cut gives the heard point that a later change could send through the port's `truncate` (whether xAI
  honours it is untested).
- **The call record includes unheard text.** `VoiceCallBuffer` stores the full `.done` transcript
  (`voice_session_service.py:408-424`), which feeds the summary and Alek's `call_context`
  (`voice_call_buffer.py:32-40`). The same cut could store only what was heard.
