# RFC: Voice Web Transport — call Lelik from the Cabinet over WebRTC

**Status:** Proposed — spike done, implementation not started
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
| 4 | Call setup (marker → persona → ticket) is inline in Twilio webhooks | `VoiceCallSetupService`; the web entry point uses it; the Twilio blueprint builds it internally from its existing constructor args (see §5.5 for the stop rule) | `services/` |
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
  `claim_extend(user_id, holder, ttl_s)` (call lifetime once the SFU answered), `holder(user_id)`,
  `prepare(ticket, user_id, account_id, call_kind) -> None` (persona + ticket +
  `voice_call_kind:{ticket}`; on failure releases ticket + marker, alerts, raises
  `VoiceCallSetupError`), `release(ticket, user_id)`.
- **Twilio blueprint migration, with a stop rule.** `create_voice_webhook_blueprint`'s signature is
  used by 8 test files; the blueprint constructs `VoiceCallSetupService` from the arguments it
  already receives. If any existing test fails, the migration is reverted and recorded as a
  deferral — tests are not edited without per-test approval.
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
- **Twilio webhook migration** if §5.5's stop rule trips.

## 9. Test plan

Unit (new files only; no existing test edited): `AudioFormat`; `PlaybackTracker` at 48 B/ms;
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
