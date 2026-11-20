<!-- living-doc
covers:
  relay_main.py
  src/handlers/media_stream_handler.py
  src/handlers/sfu_stream_handler.py
  src/handlers/call_control_handler.py
  src/services/call_control_hub.py
  src/domain/voice_control_protocol.py
  src/ports/call_control_plane_port.py
  src/adapters/http_call_control_plane_adapter.py
  src/web/voice_control_plane_app.py
  src/services/voice_call_setup_service.py
  src/web/voice_webhook_app.py
  src/web/twilio_signature_verifier.py
  src/ports/telephony_port.py
  src/adapters/twilio_telephony_adapter.py
  src/web/voice_web_call_app.py
  src/web/voice_web_call_media.py
  src/ports/media_room_port.py
  src/adapters/cloudflare_sfu_adapter.py
  src/domain/media_room_*.py
  src/domain/sfu_packet.py
  src/domain/pcm_*.py
  src/domain/paced_audio_outlet.py
  src/domain/inbound_gap_tracker.py
  src/domain/voice_audio_*.py
  src/domain/voice_playback_tracker.py
  src/domain/voice_relay_call_options.py
  src/domain/voice_ticket_keys.py
  src/domain/voice_callback_ref.py
  src/domain/voice_auth_decision.py
  src/domain/voice_amd.py
  src/web/static/call*
  src/web/static/interpreter-icon-*.png
  src/web/static/tutor-icon-*.png
  scripts/voice/cloudflare_sfu_poc/
  scripts/voice/test_mulaw_relay_poc.py
-->
# Voice Call Transport

## Purpose

This block is how one call's audio and control reach the realtime session that is Lelik: who may start a call and how
many at once (the **ticket** and the **one-call marker**), the two ways audio arrives (a Twilio phone call, and the
Cabinet web call over Cloudflare's Realtime SFU), the **relay** service that terminates both and bridges them to the
provider session, the page-to-relay **control channel**, and the relay-to-main **control plane** (config in; transcript
and delegations out). The conversation itself (persona, barge-in, silence, the summary, provider sessions, resumption)
is `VoiceSessionService` and [Voice Companion](../voice_companion/README.md); this block hands that service a ticket, an inbound audio stream and a way to
send audio back. `MediaStreamHandler` and `SfuStreamHandler` both drive `handle_call` through the same callback bundle;
making it a port was deferred until a third transport (decision `voice_web_transport_cloudflare_sfu.md`).

The relay (`relay_main.py`) is its own Cloud Run service: an open WebSocket is one long request, and the relay holds no
state but the call in flight. The main service holds all stored state.

## Owned data

Everything stored is in the voice tickets collection (`EnvironmentConfig.voice_tickets_collection`, through
`FirestoreEphemeralStore`): a value with an `expires_at` timestamp, under a Firestore TTL policy
(`docs/07_deployment/README.md`). Deletion is lazy, so reads also check expiry. Key builders:
`src/domain/voice_ticket_keys.py`.

- **Ticket record** `voice_ticket:<digest>`: the pending call. The session config the relay redeems plus `user_id`,
  `account_id`, `call_kind` (`phone`/`web`), `mode`, and for web calls `control_token_sha256`. It is **redeemed** once, by
  `/voice/session-config` (`get_and_delete`); before that `/voice/answer`, `/voice/status` and `WebCallMedia.close` read
  it. Short TTL (`_TICKET_TTL_S`).
- **Call kind record** `voice_call_kind:<digest>`: `{call_kind, mode}`, readable after the ticket is consumed (report and
  delegation read the mode there).
- **One-call marker** `voice_one_call:{user_id}`: `{in_flight, call_id?, owner}`. One live call per user, phone or web.
  `call_id` is set only for web calls (how a second start or a dial recognises a web holder); `owner` is
  `one_call_owner(digest_key, ticket)`, a keyed digest naming the holding call. Short TTL at claim; the full call TTL
  only once the relay redeems the ticket (web) or the inbound dial ends (phone).
- **Pending callback** `voice_pending_callback:{CallSid}`: `{ticket_ct, user_id, account_id, to_number}`.
- **Web call record** `voice_web_call:{call_id}`: `{user_id, account_id, ticket_ct, session_id, adapter_ids, control_ct}`,
  the browser's only handle on a call; every route checks its `user_id` against the Cabinet token.
- **Other keys:** `voice_amd:<digest>` (async answering-machine verdict), `voice_button_end:<digest>` (hang-up button),
  and the late-answer pair `voice_delegation_result:` / `voice_delegation_abandoned:` (see the control plane).

`ticket_ct` and `control_ct` are `CipherPort` ciphertext, each bound to its own AAD (`voice/<record key>`, and
`voice/voice_web_call:{call_id}/control` for the token), so neither can stand in for the other.

Relay memory only (lost on restart, which ends the calls): `SfuStreamHandler`'s per-ticket registry and
`CallControlHub`'s digest-to-link map. On the SFU: one Cloudflare session per web call and two WebSocket adapters.

## Flows

### Admission: ticket and one-call marker

Every call is a **ticket**: a random UUID, a bearer credential for one redemption. The main service mints it; the relay
learns it from the Twilio `start` event (phone) or the SFU's WebSocket URLs (web) and trades it at the control plane for
the session config. On the media sockets the relay authenticates nothing but that trade: an unknown or redeemed ticket
fails `fetch_session_config` and the call ends before any provider session opens.

The web start claims the marker with the store's atomic `set_if_absent` (`VoiceCallSetupService.claim`); `/voice/auth`
claims inline the same way. `VoiceCallSetupService.prepare` then assembles the persona (`LelikAgent.session_config`)
and writes the ticket and call-kind records. On failure it releases ticket and marker and alerts (an unsupported mode
releases without an alert), so a failed start never locks the user out.

A new call replaces a running one in two directions, never the third:
- a web start meets the user's own **web** call: ends it, waits for its marker (bounded), claims
  (`src/web/voice_web_call_app.py::create_voice_web_call_blueprint`, nested `_replace_running_call`);
- a phone dial meets a **web** call: ends its media, waits a shorter bound (Twilio's webhook deadline), and then takes the
  marker over, unless it now names another call (a second dial got there), which is rejected
  (`src/web/voice_webhook_app.py::create_voice_webhook_blueprint`, nested `_replace_web_call`);
- nothing replaces a **phone** call: a web start gets 409, a second dial `<Reject/>`.

Ending a web call is closing its SFU adapters (`src/web/voice_web_call_media.py::WebCallMedia.close`): the relay sees the
egress gone and runs its normal end path, whose report releases the marker. A call whose ticket the relay never redeemed
is released by `WebCallMedia.close` directly.

### Phone call (Twilio): we always call back

Caller ID is a claim and the callback is the test: the line's real subscriber is the one rung. So the inbound dial is
never the call.

1. `/voice/auth`: verifies the Twilio signature, resolves the number to a user (unbound number: reject and alert),
   requires a `CallSid`, claims or replaces the marker, mints the ticket, parks the pending callback, says "Calling you
   back" and hangs up.
2. `/voice/inbound-status`: on the inbound dial's terminal status claims the parked callback with `get_and_delete`
   (duplicate deliveries find nothing), rewrites the marker at the full TTL, and `LelikAgent.execute` originates the
   callback through `TelephonyPort` (`TwilioTelephonyAdapter.originate_call`, async answering-machine detection). The
   callback goes out only after the inbound leg ended: from inside `/voice/auth` it raced the caller's own line.
3. `/voice/answer?ref=`: decrypts the callback reference (below), `prepare`s the persona and answers TwiML `<Connect><Stream>` to the relay with the ticket as a
   stream parameter. A persona failure releases and speaks a plain apology.
4. `/voice/status?ref=`: records the AMD verdict (weighed at call end so a voicemail greeting is never summarised) and
   releases on any terminal status. It is the only release for a callback that rings out and never reaches `/voice/answer`.
5. Relay: `src/handlers/media_stream_handler.py::MediaStreamHandler.handle_connection` reads the ticket from `start` and
   runs `handle_call`. Audio is base64 mu-law passed through untouched; what the caller heard comes from Twilio's echoed
   `mark` events; barge-in sends Twilio `clear`.

### Web call (Cabinet, over Cloudflare's SFU)

Why: a phone call costs PSTN minutes and is narrowband mu-law; the same model over wideband PCM was judged far better, at
negligible transport cost (decision `voice_web_transport_cloudflare_sfu.md`). The page must stay on screen (RFC §3: a
browser cannot hold a screen-off call), so the phone path stays as the hands-free fallback.

1. `POST /api/voice/web-call` validates, mints `call_id`, the ticket and the **control token**, claims or replaces the
   marker, `prepare`s, opens the caller leg on the SFU and writes the web call record. **The ticket never reaches the
   browser.** The token is minted here, not at connect: the SFU dials the relay during `attach_agent` and the relay
   redeems the ticket at once, so its digest must already be on the ticket record.
2. `POST .../connect`: `MediaRoomPort.attach_agent` makes the two adapters (endpoints `<relay>/sfu/ingest?ticket=` and
   `<relay>/sfu/egress?ticket=`) and subscribes the caller to Lelik's track, then re-reads the record: a hangup that
   landed during the attach closes the adapters it just made instead of resurrecting the call. Hands out
   `control: {url, token}` when the channel knob is on.
3. The **SFU dials the relay** (no UDP reaches Cloud Run; the relay stays in the media path).
   `src/handlers/sfu_stream_handler.py::SfuStreamHandler.handle_connection` keeps one record per ticket; the second socket
   completes the pair and starts `handle_call`. Egress carries the microphone, ingest Lelik's voice, as protobuf packets
   of 20 ms PCM (`src/domain/sfu_packet.py`). The handler resamples both ways (`PcmDownsampler`, `PcmUpsampler`).
4. Outbound audio is paced: provider audio arrives faster than real time, so `_pace` sends one frame per tick through
   `PacedAudioOutlet`. "Played" means handed to the SFU, reported to `PlaybackTracker`, so barge-in logic equals the
   phone's.
5. End: the page's `/hangup` (trigger `button` first writes the button marker), or the relay (silence, provider error,
   dead peer). The page polls `.../status`: live while the marker names this call. `/voice/submit-transcript` releases it.

Web calls need Twilio configured too: `LelikAgent` exists only with the telephony adapter and from-number
(`src/composition/user_agent_factory.py::UserAgentFactory._build_lelik`), and `prepare` fails closed without it.

### Relay to main: the control plane

`CallControlPlanePort` is the relay's only edge to the main service (`HttpCallControlPlaneAdapter`, a Google OIDC token of
the relay's own identity). Routes in
`src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`:

- `/voice/session-config`: redeems the ticket. 404 when the marker names another owner (the call was replaced before the
  relay arrived); mints the voice MCP tokens and reads the resume handle (at redemption, so a stored ticket holds
  neither); extends the marker last.
- `/voice/submit-transcript`: usage, AMD and mode lookups, resume handle, summary, turn recording, then the marker
  release in a `finally` that must never be skipped.
- `/voice/delegate`: runs one specialist delegation shielded from the handler (Quart cancels a handler whose client
  disconnects, and the relay disconnects exactly when it stops waiting). If the relay stopped waiting, the answer goes to
  chat: delegate and `/voice/delegate/abandon` each write their own marker first and only an atomic `get_and_delete` of
  the result posts, so at most one side does. A failure is kept as a flag, never as text. Interpreter calls never post
  late answers, with the `VOICE_MCP_TOOLS` knob on or off (the call mode is read for every delegation and stored on the
  kept result).
- `/voice/relay-warmup`: see the keep-alive trap.

### Control channel (page to relay)

`/ctl` is a third relay path: the page's own socket to the process running its call. It carries the Ask and position
buttons, live call text and `listening`. Frames, versions, features and limits: `src/domain/voice_control_protocol.py`.

1. The token's SHA-256 goes into the ticket record, the token (encrypted) into the web call record; `/connect` hands it
   out when `_control_channel_on`.
2. The page dials `/ctl`. `src/handlers/call_control_handler.py::CallControlHandler.handle_connection` waits for
   `hello {v, token}` (never in the URL), hashes it and asks `CallControlHub.attach` for the live call with that digest,
   holding a hello that arrives before the call registered. Unknown: `bye{auth}`.
3. `handle_call` opens the link in the hub with the features the call's mode offers; teardown closes it. A newer socket with
   the same token replaces the old; `welcome` carries feature state so a reconnect re-syncs.
4. `listening`: the page has subscribed to Lelik's track. The web greeting waits for it (capped), because `/connect` takes
   seconds and an earlier greeting would be over before the browser listened. A web call without a link waits the whole
   cap; a phone call never waits.

## Invariants and traps

- **The relay must stay at one instance** (`cloudbuild-dev.yaml`, relay `--max-instances`): the SFU's two sockets and the
  page's `/ctl` socket must reach one process, because pairing and the hub are in-process. Raising it breaks calls
  silently; scaling means shards of one instance each, one base URL per call for all three paths. No test pins it.
- **The raw ticket travels in URLs that Cloud Run logs, on the web path.** The Twilio callbacks carry an encrypted
  reference instead (`src/domain/voice_callback_ref.py`: `CipherPort` ciphertext of the ticket, URL-safe, `ref=` query
  of `/voice/answer` and `/voice/status`; `LelikAgent.execute` builds the URLs). Only a request with a valid Twilio
  signature reaches the code that decrypts it, and the relay never accepts it, so a log reader learns nothing usable;
  the old `ticket=` query is refused (`tests/unit/web/test_voice_callback_ref_urls.py`). The raw ticket still goes to
  Twilio in the TwiML stream parameter (a response body, not logged). The SFU adapter endpoints
  (`.../sfu/*?ticket=`) still carry it: that URL is the media-leg capability itself. A ticket read from a log opens
  nothing after the call (redeemed once and atomically, `get_and_delete`;
  `tests/unit/handlers/test_relay_refuses_unredeemable_ticket.py`; useless without the OIDC-gated
  `/voice/session-config`), but during a live call, after a mid-call egress drop and reattach logs the URL, a reader
  could speak into the call as the owner without hearing the replies. Pending the owner's choice (refuse a second
  egress, restrict `/sfu/*` at the network, or accept). Elsewhere the ticket is never raw: store keys are keyed digests,
  hand-out records ciphertext, app logs `fingerprint(ticket)` (`tests/unit/test_no_raw_ticket_in_logs.py`,
  `tests/unit/web/test_voice_ticket_digest.py`). The control token is a separate secret exactly because it must stay out
  of URLs: first frame only, never logged.
- **Twilio webhooks verify the signature over the external URL including the query string** (the callback reference rides in it, so
  a swapped reference fails). `_external_url` rebuilds it from forwarded headers because Cloud Run terminates TLS. The
  injected verifier opens when no auth token is configured (local dev only).
  `tests/unit/web/test_twilio_signature_verifier.py`.
- **Marker release has two rules.** Web start failures, the end-of-call report and `VoiceCallSetupService.release` go
  through `release_one_call_marker`, which deletes only a marker that still names the releasing call (a replaced web
  call's late report must not delete the phone call's marker). The phone path's own releases (`/voice/auth` park
  failure, `/voice/inbound-status`, the sync-AMD branch, `/voice/status`) are unconditional deletes. That is safe only
  because nothing replaces a phone call, so a phone marker is always its own. The store has no compare-and-delete, so a
  takeover between the owner read and the delete is still lost; `/voice/inbound-status` rewrites the marker whole, which
  usually repairs it. A stuck marker locks the user out, hence release also on every failure.
  `tests/unit/web/test_voice_phone_replaces_web_call.py`.
- **The full marker TTL is earned at redemption.** Extending earlier holds it an hour when the relay never arrives; a
  replaced call redeemed late is refused so it never extends the other call's marker.
  `tests/unit/web/test_voice_phone_replace_budget_and_session_config.py`.
- **Ingest closing ends the call; egress has a short re-attach window** (the SFU redials a dropped egress, per the
  code's comment; a new egress for a live ticket re-attaches). Registry removal is by identity, so a stale unpaired call
  cannot evict a newer one under the same ticket. The dead-peer watchdog (`DEAD_PEER_TIMEOUT_S`, knob
  `VOICE_DEAD_PEER_TIMEOUT_S`, read by `relay_main._dead_peer_timeout_s`) ends a paired call with no inbound packet
  through the normal end path (transcript submitted, marker released, resumable); it counts packets, not what they carry.
  `tests/unit/handlers/test_sfu_stream_handler_hardening.py`, `test_sfu_stream_handler_dead_peer.py`.
- **`/sfu/*` and `/ctl` are public sockets.** The handlers cap unpaired and unauthenticated entries, refuse tickets that
  are not UUID-shaped, bound the hello's time and the page's frame size and rate. The relay's `/ctl` sockets do
  authenticate, by the hello token against the hub.
- **Idle SFU tracks need silence.** `PacedAudioOutlet` sends silence frames when nothing is queued, because the SFU drops a
  track that gets no packets (decision record). `tests/unit/domain/test_paced_audio_outlet.py`.
- **`attach_agent` is all-or-nothing.** Cloudflare reports per-track failures inside a 2xx body; such a response is a
  `MediaRoomError` and the adapters already made are closed.
  `tests/unit/adapters/test_cloudflare_sfu_adapter_attach_cleanup.py`.
- **The relay needs a keep-alive.** Its cold start outlasts Cloudflare's WebSocket handshake (measured, `VOICE_COMPANION_RFC.md`
  relay keep-alive note), and Scheduler cannot call the relay itself (its request's `Content-Length` is
  rejected by the `websockets` parser). Scheduler calls the main service's `/voice/relay-warmup`, which GETs the relay;
  `relay_main.py` answers plain HTTP with 200. `docs/07_deployment/SCHEDULERS.md`.
- **Formats are domain values** (`src/domain/voice_audio_format.py`): mu-law 8 kHz on the phone leg, PCM 24 kHz mono for
  the web provider session, PCM 48 kHz stereo on the SFU wire. The relay builds two `VoiceSessionService` instances;
  only the web one gets the hub, so phone calls have no control channel and no dead-peer watchdog.
- **The channel switch is off in code and on in the deploy** (`_control_channel_on`, `VOICE_CONTROL_CHANNEL`; the deploy
  sets it on). Off: `/connect` hands out nothing and every web greeting waits the full `listening` cap.
- **The web call exists only when configured.** `main.py` builds the SFU adapter (the phone dial uses it too) and registers
  the blueprint only with the Cloudflare app id, secret and `VOICE_RELAY_STREAM_URL`; the relay base for SFU and `/ctl`
  is that value's origin.
- **Cabinet call pages are served `no-cache`** and an expired session sends the page to login and back.

## Links

- `docs/10_rfcs/VOICE_WEB_TRANSPORT_RFC.md` and decision `voice_web_transport_cloudflare_sfu.md`: why the SFU, the
  pairing constraint, alternatives rejected.
- `docs/10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md`: the channel, dead-peer watchdog, replacing a running call.
- `docs/10_rfcs/VOICE_COMPANION_RFC.md` (callback identity, relay as its own service);
  `docs/10_rfcs/SECRETS_AT_REST_RFC.md` §2.5 (tickets as digests and ciphertext).
- Decisions `voice_spike_02_mulaw_e2e.md`, `voice_spike_03_latency.md` (phone audio and latency evidence),
  `runtime_service_accounts.md` (relay identity, verifier set), `cabinet_session_24h_cap.md`.
- Deployment: `docs/07_deployment/SCHEDULERS.md` (keep-alive), `docs/07_deployment/README.md` (relay knobs, TTL policy).

## Where to start reading

1. `src/handlers/sfu_stream_handler.py::SfuStreamHandler`: pairing, pacing, re-attach, dead peer.
2. `src/web/voice_web_call_app.py::create_voice_web_call_blueprint`: web start, connect, hangup, replacement.
3. `src/web/voice_webhook_app.py::create_voice_webhook_blueprint`: the phone path from dial to stream.
4. `src/services/voice_call_setup_service.py::VoiceCallSetupService.prepare`: ticket, marker, failure release.
5. `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`: what the relay asks of the main service.
