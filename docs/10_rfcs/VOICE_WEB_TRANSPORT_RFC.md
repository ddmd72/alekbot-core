# RFC: Voice Web Transport — call Lelik from the Cabinet over WebRTC

**Status:** Implemented, pending UAT
**Date:** 2026-09-27
**Owner:** Dmytro
**Milestone:** Voice — second transport

**Related:** `VOICE_COMPANION_RFC.md` (the call itself: persona, delegation, barge-in, silence,
summary — all unchanged here; this RFC answers its §5.1), POC
`scripts/voice/cloudflare_sfu_poc/poc.py` + `page.html` (**authoritative** for the SFU protocol:
API sequence, adapter shapes, protobuf framing, PCM format, pacing).

---

## 1. Problem

The Twilio path works but has two costs the owner no longer accepts:

- **Money.** Identity is proven by a callback, so every call is an outbound PSTN call to a Spanish
  mobile: $0.0486–0.18/min + Media Streams $0.0044/min + AMD $0.0075/call (Twilio ES price list,
  2026-09-27) — roughly $0.05–0.19 per minute before the realtime model.
- **Audio.** G.711 μ-law at 8 kHz both ways. The model *speaks* narrowband and *hears* narrowband.
  On the spike (§6) the same model over wideband PCM was judged by the owner "an order of magnitude
  better — the voice and intonation we could not get over Twilio".

## 2. Goals

1. A one-button call page in the Cabinet; the conversation is the same Lelik (same
   `VoiceSessionService`, persona, delegation, summary).
2. Wideband audio end to end (browser Opus ⇄ SFU ⇄ PCM 24 kHz to the provider).
3. Transport cost negligible next to the model (measured §6).
4. The media path stays **inside our perimeter** (VOICE_COMPANION §5: relay-in-the-path is what makes
   every failure visible in our logs) — the relay still carries the audio.
5. The transport a future native app will use (calls now, own chat later) — no server rework then.
6. Fix the hexagonal leaks the second transport exposes (§5), not work around them.

## 3. Non-goals

- **Screen-locked / backgrounded calls from the browser.** Impossible on iOS Safari (VOICE_COMPANION
  §5.1 stands). The page keeps the screen on (Wake Lock). WhatsApp-like background calling is a
  **native app** concern (CallKit + PushKit / ConnectionService) and reuses this server side as is.
- **Retiring Twilio.** It stays as the hands-free / pocket / car path.
- **Own chat.** Chat is not a WebRTC concern (history, offline delivery, push) — it will be a new
  platform in `ConversationHandler`. This RFC only avoids foreclosing it.
- **Prompt tuning** (reasoning effort, `caller_opening`, `SPOKEN_DELIVERY`). Out of scope; the web
  path gets its own env knobs (§5.6) so UAT can tune it without code.
- **Direct browser→provider WebRTC.** Rejected (§7).

## 4. Answer to VOICE_COMPANION §5.1

§5.1 rejected a browser transport because it cannot work screen-off. That is still true, and it is
now an accepted limit rather than a blocker: the owner uses the page screen-on, telephony keeps the
screen-off case, and background calling moves to the native app — which needs exactly the transport
built here. §5's other argument (the relay must stay in the media path for observability, barge-in
and silence) is **preserved**: Cloudflare delivers the media *to the relay*, like Twilio does.

## 5. Design

### 5.1 Topology

```
browser ──WebRTC (Opus)──► Cloudflare Realtime SFU ──WS "egress" adapter: user mic, PCM 48k stereo──► relay /sfu/egress
browser ◄─WebRTC (Opus)─── Cloudflare Realtime SFU ◄─WS "ingest" adapter: Lelik, PCM 48k stereo────── relay /sfu/ingest
                                                                                        relay ⇄ OpenAI Realtime (PCM 24k mono)
main service (Cabinet API) ──HTTPS, App Secret──► SFU API (sessions, tracks, adapters)
```

The SFU **dials the relay** (WebSocket), exactly as Twilio Media Streams does today — no UDP on our
side, Cloud Run stays. Two adapters per call: `location: "remote"` (egress: the browser's `mic`
track delivered to us) and `location: "local"` (ingest: we publish track `lelik`, the browser pulls
it). Wire format (POC-verified): each WS binary message is protobuf
`Packet{1 sequenceNumber, 2 timestamp, 5 payload}`, payload PCM s16le 48 kHz **stereo** interleaved,
20 ms = 3840 B, timestamp in 48 kHz samples (step 960). Stereo only — no mono option.

### 5.2 Call flow

1. Page: `getUserMedia` → `RTCPeerConnection` (STUN `stun.cloudflare.com:3478`) → offer.
2. `POST /api/voice/web-call` (Cabinet JWT): claim the one-call marker, assemble the persona
   (`LelikAgent.session_config`), mint the ticket with `call_kind: "web"`, SFU `sessions/new` +
   `tracks/new` (offer → answer). Returns `{call_id, sdp}`. `call_id` is a separate random id; the
   **ticket never reaches the browser**.
3. Page sets the answer, waits for `connected`, `POST /api/voice/web-call/<id>/connect`: ingest
   adapter (endpoint `wss://<relay>/sfu/ingest?ticket=…`), egress adapter
   (`…/sfu/egress?ticket=…`), pull `lelik` into the browser's session → SFU offer.
4. Page answers, `POST …/renegotiate`.
5. Relay: pairs the two sockets by ticket, runs `VoiceSessionService.handle_call(ticket, …)` —
   unchanged — with a PCM-24k provider session.
6. End: page `POST …/hangup` (closes adapters) **or** relay ends the call (silence watchdog, provider
   error) and closes its sockets. The page polls `GET …/status`; the call is live while the marker is
   held by this `call_id`. `/voice/submit-transcript` releases the marker as today.

### 5.3 Hexagonal fixes (the second transport exposes these)

| # | Leak today | Fix | Layer |
|---|---|---|---|
| 1 | μ-law is a constant inside domain (`MULAW_8K_BYTES_PER_MS` in `PlaybackTracker`) | `AudioFormat` value object (`encoding`, `sample_rate_hz`, `channels`, `bytes_per_ms`) with `MULAW_8K`, `PCM16_24K`, `PCM16_48K_STEREO`; `PlaybackTracker.bytes_per_ms` defaults to μ-law | `domain/` |
| 2 | `audio/pcmu` hardcoded in `OpenAIRealtimeAdapter` | `audio_format: AudioFormat = MULAW_8K` ctor arg; session formats and outbound `AudioFrame`s derive from it | `adapters/` |
| 3 | A transport = one Twilio handler | `SfuStreamHandler` beside `MediaStreamHandler`, kept a **thin vendor adapter** (sockets, protocol, timing). Vendor-neutral logic is pure domain: `PacedAudioOutlet` (provider audio → fixed frames, tail padding, idle silence, played-marks), SFU packet codec, resampling 48k stereo ⇄ 24k mono (numpy precedent: `domain/vector_math.py`). No port in front of the handler: a driving adapter calls the core's primary port (`handle_call`), nothing calls it | `handlers/`, `domain/` |
| 4 | Call setup (marker → persona → ticket) is inline in Twilio webhooks | `VoiceCallSetupService`; the web entry point uses it; the Twilio blueprint builds it internally from its existing constructor args (§5.5) | `services/` |
| 5 | No boundary for an SFU | `MediaRoomPort` + `CloudflareSfuAdapter`. Justified: system boundary, real alternative (LiveKit / self-hosted SFU) | `ports/`, `adapters/` |
| 6 | "phone" is hardcoded in the pickup note and the summary header | `call_kind` (`phone`/`web`) on the ticket → relay picks the pickup note; `voice_call_kind:{ticket}` → summary header wording | `domain/` + service |

### 5.4 Relay-side SFU transport

- **Pairing.** Both halves carry `?ticket=` in the URL. The handler keeps a per-ticket record; the
  call starts when both are present (15 s timeout for the second). The relay is
  `--max-instances=1` (cloudbuild) — **required**: both sockets must land on one instance.
  One instance is not one user: it serves many concurrent calls (asyncio; per-call load is two
  50 fps resampling streams). Capacity per instance is measured during UAT (CPU per call).
- **Scaling path (not built — YAGNI).** The main service chooses the URLs the SFU dials, so calls
  can be pinned to **relay shards**: N relay services, each `--max-instances=1`, the main service
  picks one per call (hash of user or least-loaded). Pairing stays local to a shard; idle shards
  cost nothing at `min-instances=0`. The Twilio path shards the same way (`/voice/answer` chooses
  the stream URL).
- **Egress reconnect.** Cloudflare retries the egress endpoint for 5 s after a drop (stream mode).
  A new egress socket for a live ticket re-attaches to the running call. Ingest does not reconnect:
  ingest closing ends the call.
- **Pacing.** Provider audio arrives faster than real time; `PacedAudioOutlet` upsamples it to 48k
  stereo and the ingest writer sends one 20 ms frame per tick (a response's partial tail is
  zero-padded and sent at once). With nothing to say it sends a silence frame every tick
  (the SFU garbage-collects a track with no packets for 30 s). Cost of continuous silence measured
  (§6): negligible.
- **Playback.** "Played" = handed to the SFU by the pacer (the browser's jitter buffer adds
  ~50 ms, measured). The pacer calls `PlaybackTracker.record_played` with provider-format byte
  totals, so `VoiceSessionService` barge-in / silence logic is unchanged. `clear` drops the queue and
  marks everything sent as played (same end state as Twilio's echoed marks after `clear`).
- **Thinking cue** is μ-law-only and off in production; the SFU transport passes no
  `send_cue_audio`. Cue pacing constants stay μ-law (explicit deferral, §8).

### 5.5 Main-service side

- `MediaRoomPort` (neutral verbs): `open_caller(offer_sdp, mid) -> CallerLeg`,
  `attach_agent(caller_session_id, ingest_url, egress_url) -> AgentLeg`,
  `complete_negotiation(caller_session_id, answer_sdp)`, `close(adapter_ids)`.
- `VoiceCallSetupService`: `claim(user_id, holder, ttl_s) -> bool` (short setup TTL),
  `holder(user_id)`, `prepare(ticket, user_id, account_id, call_kind) -> None` (persona + ticket +
  `voice_call_kind:{ticket}`; on any failure, including the ticket writes, releases ticket +
  marker, alerts, raises `VoiceCallSetupError`), `release(ticket, user_id)`. There is no
  `claim_extend`: the web marker is extended to the full call TTL only when the relay redeems the
  ticket in `/voice/session-config` (`voice_control_plane_app.py`, review Ruling 4). Extending it
  when the SFU answered would hold the marker for an hour even if the relay never arrived.
- **Twilio blueprint migration (shipped).** `create_voice_webhook_blueprint` keeps its signature and
  constructs `VoiceCallSetupService` from the arguments it already receives; `/voice/answer` uses
  `prepare()`. `/voice/auth`, `/voice/inbound-status` and the AMD path still manage the marker and
  ticket inline. Three pre-existing tests read "the last `store.set` call", which `prepare()`'s
  extra `voice_call_kind` write changed. They were updated by reviewer ruling to look the ticket
  write up by key (commit `558cb2b`): `test_answer_webhook_assembles_persona_and_streams_on_human_pickup`,
  `test_answer_stores_instructions_and_tools_on_the_ticket`,
  `test_answer_ticket_identity_wins_over_session_keys`.
- `voice_web_call_app.py` blueprint (Cabinet JWT via a shared `cabinet_auth` helper), web call
  record `voice_web_call:{call_id}` = `{user_id, account_id, ticket, session_id, adapter_ids}`;
  every route checks the record's `user_id` against the token.
- Marker value for web calls: `{"in_flight": True, "call_id": <id>}` — status is "live" iff the
  marker exists and names this call.
- Hangup when the relay never started (ticket still unconsumed): release ticket + marker directly.

### 5.6 Configuration

| Key | Where | Kind |
|---|---|---|
| `CLOUDFLARE_SFU_APP_ID`, `CLOUDFLARE_SFU_APP_SECRET` | main service | secrets → `load_settings()` + Secret Manager |
| `VOICE_RELAY_STREAM_URL` | main service | existing; its origin is the base for `/sfu/*` endpoints |
| `VOICE_WEB_REASONING_EFFORT` (default `medium`), `VOICE_WEB_CALLER_OPENING` (`on`/`off`) | relay | optional knobs, `os.getenv` in `relay_main.py` (bootstrap) |

`reasoning_effort` is `medium` on both voice paths (owner, 2026-09-27; the phone path was `high`
since 2026-09-23).

## 6. Spike results (2026-09-27, laptop, cloudflared tunnel, POC)

| Measure | Value |
|---|---|
| Browser ⇄ SFU | RTT 21–48 ms, jitter 2–4 ms, 0 loss, POP Madrid |
| SFU → us | continuous 50 msg/s even in silence, 3840 B frames → **0.68 GB/h** |
| us → SFU (continuous silence policy) | **0.64 GiB/h** |
| First audio after end of speech (OpenAI, provider VAD) | 646–1479 ms, median ~900 (Twilio relay 2026-09-25: 1062–1939, median ~1350, plus PSTN) |
| Adapter setup + pull | ~2 s |
| Owner verdict | audio "ideal", voice/intonation far above Twilio |

**Cost** (verified rates): Cloudflare Realtime 1000 GB/mo free then $0.05/GB (card on file, charged
only above the free tier); GCP Premium egress $0.12/GiB after 1 GiB/mo. Worst case ≈ **$0.077/h**
of GCP egress, Cloudflare free — vs Twilio $3–11/h. Open: whether Cloudflare bills the WS PCM bytes
or the Opus bytes (`bytesProcessed` on close looked Opus-sized) — irrelevant under the free tier.

## 7. Alternatives rejected

- **Direct browser WebRTC → OpenAI + sideband control.** Zero transport cost, but provider lock-in,
  audio leaves our perimeter, and barge-in/playback logic would have to be re-expressed over provider
  events (a split of `VoiceSessionService`).
- **LiveKit Cloud.** Free tier then a $50/mo step; the relay counts as a participant (minutes ×2);
  outbound WebRTC from Cloud Run unproven.
- **Self-hosted SFU on a VM.** VM, TLS/TURN and ops for a solo project; Cloud Run cannot take UDP.
- **Moving hosting to Cloudflare.** Unrelated to this feature; separate decision.

## 8. Explicit deferrals

- **Splitting `VoiceSessionService` into policy vs audio plumbing** — needed only if audio ever
  bypasses the relay (§7 first bullet).
- **`handle_call`'s callback bundle → an ABC transport port** — the bundle is already the port
  structurally; converting it rewrites ~100 lines across 15 test files for no behaviour change.
- **Thinking cue for non-μ-law transports** — the cue is off; revisit with the cue itself.

## 9. Test plan

Unit (new files only; existing tests are read-only for the implementer — a task reviewer rules
per test, intended change vs. code wrong, and specifies the exact edit applied in a fix round; see
§12 for round 1's rulings): `AudioFormat`; `PlaybackTracker` at 48 B/ms;
OpenAI adapter wire test for `audio/pcm` 24k; SFU packet codec round trip + malformed input;
resampler tone preservation (1 kHz survives both ways) + chunk-boundary continuity;
`PacedAudioOutlet` framing / tail padding / mark release / clear;
`SfuStreamHandler` pairing / pairing timeout / egress re-attach / ingest close ends call / pacing
emits silence / clear marks played; pickup note per `call_kind`; summary header per kind;
`CloudflareSfuAdapter` request shapes (mocked httpx); `VoiceCallSetupService` claim/prepare
failure/release; web call blueprint auth, ownership, busy (409), SFU failure releases the marker,
hangup before relay start releases ticket + marker, status. Architecture suite (`make check`) must
stay green. Live: §10.

## 10. Rollout / verification

1. Secrets in Secret Manager + cloudbuild `--set-secrets`; deploy (main + relay).
2. Laptop call: greeting, conversation, barge-in (1 s), delegation (`search_web`), silence
   watchdog note + hang-up, page shows "ended", summary lands in the primary channel, second call
   succeeds (marker released).
3. iPhone Safari call (screen on) — same checklist.
4. Twilio call still works end to end.

**Rollback:** remove the Cabinet link and the `/sfu/*` relay routes; Twilio path is untouched.

## 11. Open questions

1. Which bytes Cloudflare bills for WS adapters (dashboard after UAT).
2. `SILENCE=heartbeat` (1 frame/s) — does it keep the ingest track alive? Would halve idle egress.
3. RealtimeKit mobile SDKs vs bare libwebrtc for the future native app — can RealtimeKit meetings
   use WS adapters?
4. A forgotten open tab with background noise keeps VAD busy; the relay's 60-min `--timeout` is the
   only hard cap today. A per-call max duration may be needed.

## 12. UAT round 1 (2026-09-28)

The owner's first live web calls (2026-09-27) surfaced five gaps. Plan:
`docs/superpowers/plans/2026-09-28-voice-web-uat-round1.md`.

**Bluetooth HFP.** A headset paired over Bluetooth exposes its microphone through the HFP
profile, which forces the *entire* Bluetooth link — including the speaker leg — down to
narrowband (typically 16 kHz-equivalent, mono, compressed). Selecting the built-in microphone
instead keeps the Bluetooth output on A2DP (wideband stereo), which is the leg the owner
actually judges quality on. `call.html` now exposes a `<select>` of `audioinput` devices,
persisted per browser in `localStorage`, with the choice enforced via
`getUserMedia({deviceId:{exact:…}})` and a fallback to the default device on
`OverconstrainedError`/`NotFoundError` (e.g. a saved device unplugged since the last call). Hint
text nudges the owner toward the built-in mic when on Bluetooth.

**Ringback.** The call page now plays a Spanish-style ringback tone (425 Hz, 1.5 s on / 3.0 s
off) from the moment the Call button is tapped until the first sustained (≥150 ms) audible
sound from Lelik, detected by an `AnalyserNode` on a separate `MediaStreamSource` tap of the
remote track — the `<audio>` element stays the sole playback path, so a suspended
`AudioContext` (iOS lock screen, Siri) can never silence Lelik. Reaching `connected` on the
RTCPeerConnection does not stop it — only actual voice does, since the WebRTC connection can be
live for a second or more before Lelik's audio starts. Two safety nets: if no analyser could be
created, ringback stops at Live; otherwise it is capped at 8 s past Live
(`RINGBACK_LIVE_CAP_MS`) even if detection never fires.

**Dispatch filler.** Live logs showed 12 s of dead air between a delegation being dispatched and
the watchdog's own `_WAITING_NOTE` (which only fires `_silence_timeout_s` later). A new
`_DISPATCH_NOTE` fires immediately after the turn that dispatched the tool call — one line asking
Lelik to keep the caller company, gated on nothing already owning the reply slot (no active
response, caller not speaking, no answer already flushed, not barged into). **Trade-off:** a
delegation that resolves in under a second now still waits behind the filler's own sentence
before the real answer can be spoken, since only one response can be active at a time
(`response.create` while one is active is call-ending) and the filler's `response_done` is what
flushes the queued answer.

**300 s relay timeout, 600 s `ask_alek` ceiling, late answers reach chat.** The relay's
`VoiceSessionService._fetch_answer` timeout is 300 s (was 90 s), configured per instance in
`relay_main.py`. The `ask_alek` path (Lelik → `AlekGatewayAgent` → Router → Smart) gets a
600 s ceiling — `ASK_ALEK_TIMEOUT_MS` on both the gateway's `AgentConfig` and the explicit
per-message `timeout_ms`, which `RouterAgent`/`BaseAgent._execute_with_timeout` already prefer
over any agent-level default — chosen to outlast the relay's 300 s while staying under the
abandon marker's 900 s TTL and Cloud Run's 1800 s request timeout.

An answer that outlives the relay's wait (timeout, or the call ending with the delegation still
in flight) is posted to the caller's chat instead of being discarded. Key protocol, both sides
best-effort and idempotent:
- `/voice/delegate` runs the dispatch inside a **shielded**, module-tracked task
  (`asyncio.shield` + a strong reference held until a done-callback releases it), so a Cloud Run
  disconnect on the relay's side does not cancel the in-flight delegation.
- On success it stores `voice_delegation_result:{ticket}:{call_id} = {output, request, user_id,
  account_id}` (TTL 900 s), then checks for an existing `voice_delegation_abandoned:{ticket}:
  {call_id}` marker; if present, it claims the result with an atomic `get_and_delete` and posts.
- The relay's timeout path, and call teardown for any still-pending delegation, call
  `POST /voice/delegate/abandon`, which writes the abandoned marker and independently attempts
  the same `get_and_delete` claim.
- Whichever side runs second always sees the other's marker (Firestore writes are strongly
  consistent) and `get_and_delete` is the single atomic claim point, so **at most one side posts,
  and a repeat abandon (timeout, then again at call end) finds nothing to claim** — exactly-once
  delivery, not at-least-once.
- A **failed** delegation is never posted verbatim (raw errors can carry URLs, and Lelik has
  already told the caller a real answer is coming): it is stored as `{failed: True}` with no
  text, and the sink posts a short, localized neutral line ("The request did not go through.")
  instead.
- Teardown order in `_run_call`'s `finally`: cancel event loop tasks (forward/consume/
  watchdog/cue/barge-in) → bounded concurrent abandons (one `wait_for(…, 5s)` per pending
  delegation, run together so N pending delegations cost one 5 s bound, not N×5 s) → cancel and
  gather the delegation tasks themselves → `session.close()` → `submit_transcript`.

**Live-speech gate.** `build_persona_anchor`'s spoken paragraph now ends with: "Before you speak,
check the wording and the meaning: would a real person say exactly this, in these words, in a
live conversation? If not, rephrase until they would." — gated on `spoken_delivery` being present,
so text-surface prompts are unaffected. (Kept when the rest of that paragraph was rewritten as a
register description on 2026-09-28 — see `decisions/lelik_delivery_single_source.md`.)

**Known gaps (deferred, from the round's ledger; not blocking):**
- An answer that is **mid-injection** at call end (already taken off the pending/queued state,
  not yet spoken — the few ms inside `submit_message`/`submit_tool_result`) is not abandoned.
- A chat-post failure **after** `get_and_delete` has already claimed the result loses that one
  answer (logged at error, no retry, no alert).
- `notify_raw` (used by the late-answer sink) writes no session history, so Alek has no record of
  a late answer that already reached the caller's chat.
- A link-bearing answer can reach chat twice: once as the gateway's own bare-anchor chat copy
  (existing behaviour, §5.2), once as the late-answer post if the relay had already stopped
  waiting.
- Cloud Run CPU throttling of the shielded delegation task after the relay's HTTP connection
  closes — resolved as accepted, see §13.

**§9 test-plan correction.** §9 originally said "no existing test edited." That held for the
2026-09-27 transport work through its first ten tasks, but two later, reviewer-ruled edits (both
rounds strictly read-only for the implementer; the task reviewer names the exact edit) changed
that:
- 2026-09-27 plan, Task 12 (webhook now reads the call's key, not the last store write):
  `tests/unit/web/test_voice_webhook_app.py::test_answer_webhook_assembles_persona_and_streams_on_human_pickup`,
  `tests/unit/web/test_voice_webhook_session_tools.py::test_answer_stores_instructions_and_tools_on_the_ticket`,
  `tests/unit/web/test_voice_webhook_session_tools.py::test_answer_ticket_identity_wins_over_session_keys`.
- 2026-09-28 plan (this round), Task 1 (the dispatch filler's own turn extends the fixture's event
  stream so a queued answer gets flushed):
  `tests/unit/services/test_voice_session_delegation.py::test_tool_call_is_forwarded_with_call_context_and_answered_as_function_output`,
  `tests/unit/services/test_voice_session_cancelled_tool_call.py::test_tool_call_without_a_prior_cancel_is_dispatched_as_before`.
- 2026-09-28 plan, Task 2 (reworded timeout copy + the new `delegate`/`call_id`/`ticket`/`request`
  body fields + `delegate_outcome` replacing `delegate` at the endpoint):
  `tests/unit/adapters/test_http_call_control_plane_delegate.py::test_delegate_posts_the_tool_call_and_returns_output`,
  `tests/unit/services/test_voice_session_delegation.py::test_timeout_is_spoken_not_silent`,
  `tests/unit/services/test_voice_session_late_timeout_note.py::test_late_timeout_reads_as_no_answer_not_as_one_that_arrived`,
  and, in `tests/unit/web/test_voice_delegate_endpoint.py`:
  `test_delegate_runs_lelik_dispatch_inside_the_callers_request_context`,
  `test_delegate_flushes_prompt_content_before_returning`, `test_delegate_opens_span_with_intent_attribute`,
  `test_delegate_flush_failure_does_not_change_the_response`, `test_delegate_500_when_dispatch_raises`.

Every edit above was applied by the implementer only after an explicit per-test reviewer ruling
("intended change", never "code wrong"); see the two plans' progress ledgers
(`.superpowers/sdd/2026-09-27-voice-web-transport/progress.md`,
`.superpowers/sdd/2026-09-28-voice-web-uat-round1/progress.md`) for the full rulings.

## 13. Pre-merge closeout (2026-09-28)

**Cabinet HTML revalidates.** `/cabinet` and `/cabinet/call` are served with
`Cache-Control: no-cache` (ETag revalidation, 304 when unchanged). Quart's `send_file` default
(`public, max-age=43200`) served the owner the previous day's page after a deploy — the actual
cause of "no ringback" in UAT round 2, not the ringback code.

**iOS ringer switch.** Until mic capture starts, iOS plays WebAudio through a session the ringer
switch mutes, so ringback was silent on a phone in silent mode. The Call tap sets
`navigator.audioSession.type = "play-and-record"` (Safari 17+, feature-detected) before starting
ringback; `endCall` restores `"auto"`.

**Ephemeral records expire server-side.** `FirestoreEphemeralStore` writes `expires_at` as a
Firestore Timestamp, and the voice tickets collection has a TTL policy on that field
(`docs/07_deployment/README.md`). Before this, `expires_at` was a float checked only on read, so
anything never read back — abandoned-delegation markers, unclaimed results, call-kind keys —
stayed forever (18 such documents at the switch, purged once; no dual-format reader). Reads keep
their own expiry check because TTL deletion is lazy (up to ~24 h).

**CPU throttling accepted.** The main service keeps Cloud Run's default CPU allocation (CPU only
during requests). Two production observations of the shielded late-answer task running after the
relay's HTTP connection closed: a 105.96 s delegation span completed (2026-09-27), and an answer
posted to chat 15 s after disconnect (2026-09-28). Neither showed throttling. Revisit only if a
late answer is lost with the delegation span cut short — the fix would be always-on CPU for the
main service, at its idle cost.

**Test edits (reviewer-ruled, intended change — float → Timestamp `expires_at`):** in
`tests/unit/adapters/test_firestore_ephemeral_store.py`: `test_get_returns_none_when_expired`,
`test_get_returns_value_when_not_expired`, `test_set_writes_value_and_expires_at` (now also
asserts a tz-aware datetime), `test_get_and_delete_returns_value_and_deletes_inside_the_transaction`,
`test_get_and_delete_returns_none_when_expired_but_still_consumes`,
`test_two_concurrent_get_and_delete_calls_yield_exactly_one_winner`. Fixture values changed type
only; the expired/live semantics of each test are unchanged.

**Whole-branch review, fixed:** `SfuStreamHandler` removes a call from its registry by identity,
so a stale unpaired call timing out can no longer evict a newer call the SFU's retries opened
under the same ticket; `CloudflareSfuAdapter.attach_agent` treats a 2xx body whose track carries
an `errorCode` (or no `adapterId`) as a `MediaRoomError`, so the already-created ingest adapter is
closed instead of leaking behind a bare 500.

**Whole-branch review, deferred (trigger to revisit):**
- SFU adapters are closed only by the page's `/hangup`. A killed tab leaves them redialling the
  relay with a spent ticket (each attempt: session-config 404, logged). Closing server-side on
  `submit_transcript` is the fix — revisit on the first such log loop.
- An ingest drop ends the call (tested behaviour); egress alone has a reattach window, because
  only egress retries were observed. Revisit if a live call ends on an ingest blip.
- The late-answer record is written on the spoken-answer path (a Firestore set + get before the
  relay gets the answer). Revisit if delegation spans show it as a measurable share.

## 14. Mobile entry (2026-09-28)

The call page is the Cabinet's front door on a phone. The goal is to go from pocket to talking
with one tap.

- **Redirect.** A bare `/cabinet` on a touch device narrower than 768px `location.replace`s to
  `/cabinet/call`. Any query string (`?tab=…`, OAuth `*_connected` / `*_error`, `joined`) keeps
  the Cabinet. The call page's back link is `/cabinet?tab=integrations`, so the redirect cannot
  loop.
- **Call screen.** The layout is full-screen and one-handed. Identity and state sit at the top;
  a 120px round button sits in the bottom third, green for Call and red for Hang up, with a call
  timer while live. The mic picker is folded under `⚙`. Safe-area insets are respected
  (`viewport-fit=cover`). The call logic (ringback, `callToken`, pagehide, wake lock) is
  unchanged; only markup, CSS and `renderButton` changed.
- **Cabinet tab bar.** The mobile nav is a full-width grid with an icon and a short label per
  tab, so it always fits. The old `max-content` pill ran off both screen edges in Ukrainian.
- **Home Screen.** `/cabinet/call/manifest.webmanifest` (start_url `/cabinet/call`, standalone)
  and `/cabinet/call/icon-{180,192,512}.png` are explicit blueprint routes, public by design.
  The main Quart app has no static folder, so `/static/*` 404s; the Cabinet header logo now uses
  the same icon route.
- **Siri.** There is no code for this. In the Shortcuts app, create a shortcut named e.g.
  "Call Alek" with the single action *Open URL* → `<service URL>/cabinet/call`. "Hey Siri, Call
  Alek" opens Safari on the call button. It does not autostart, by owner decision: iOS allows
  the AudioContext (ringback) only inside a user gesture, and an accidental open must not place
  a call.
- **Session.** It is capped at 24h with no silent refresh, and an expired session goes to login
  and back to the call page. See `decisions/cabinet_session_24h_cap.md`.
