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

This block is how one call's audio and control get between the caller and the realtime session that is Lelik. It
covers who may start a call and how many at once (the **ticket** and the **one-call marker**), the two ways audio
arrives (a Twilio phone call, and the Cabinet web call over Cloudflare's Realtime SFU), the **relay** service that
terminates both and bridges them to the provider session, the page-to-relay **control channel**, and the relay-to-main
**control plane** that carries the config in, and the transcript and delegations out.

What it is not for: the conversation itself (persona, barge-in, silence handling, delegation policy, the end-of-call
summary, provider sessions, resumption) is `VoiceSessionService` and Lelik's own docs; this block only hands that
service a ticket, an inbound audio stream and a way to send audio back. The two transports are deliberately thin and
interchangeable at that seam: `VoiceSessionService.handle_call` takes a bundle of callbacks, and `MediaStreamHandler`
and `SfuStreamHandler` both drive it the same way (making the bundle a port was deferred until a third transport).

The relay is its own Cloud Run service (`relay_main.py`), apart from the main service: an open WebSocket is a long
request, the relay needs its own timeout, and it holds no state but the call in flight. The main service holds all
stored state.

## Owned data

Everything stored is in the voice tickets collection (`EnvironmentConfig.voice_tickets_collection`, written through
`FirestoreEphemeralStore`): a value with an `expires_at` timestamp. The collection has a Firestore TTL policy on it
(`docs/07_deployment/README.md`); deletion is lazy, so every read also checks expiry. Key builders are in
`src/domain/voice_ticket_keys.py`.

- **Ticket record** `voice_ticket:<digest>`: the pending call. Holds the session config the relay will redeem (persona
  instructions, tools, voice spec) plus `user_id`, `account_id`, `call_kind` (`phone` or `web`), `mode`, and for web
  calls `control_token_sha256`. Written by `VoiceCallSetupService.prepare` (on the phone path `/voice/auth` first writes
  an identity-only record, `/voice/answer` then overwrites it via `prepare`). Read **once**, by
  `/voice/session-config`, with an atomic `get_and_delete`. Short TTL (`_TICKET_TTL_S`).
- **Call kind record** `voice_call_kind:<digest>`: `{call_kind, mode}`, so the end-of-call report and delegations can
  read the call's mode without the (consumed) ticket. Written by `prepare`; read by `/voice/submit-transcript` and
  `/voice/delegate`.
- **One-call marker** `voice_one_call:{user_id}`: `{in_flight, call_id?, owner}`. Exactly one live call per user, phone
  or web. `call_id` is present only for web calls (it is how a phone dial or a second web start recognises a web
  holder). `owner` is `one_call_owner(digest_key, ticket)`, a keyed digest naming the call that holds it. TTL is short
  at claim and extended to the full call TTL only when the relay redeems the ticket (web) or when the inbound dial ends
  (phone). Deleted by `release_one_call_marker` only while it still names the releasing call.
- **Pending callback** `voice_pending_callback:{CallSid}`: `{ticket_ct, user_id, account_id, to_number}`, parked by
  `/voice/auth` for the callback; claimed once by `/voice/inbound-status`.
- **Web call record** `voice_web_call:{call_id}`: `{user_id, account_id, ticket_ct, session_id, adapter_ids,
  control_ct}`; the SFU session and adapter ids, and the browser's only handle on the call. Every route checks its
  `user_id` against the Cabinet token.
- **Other keys:** `voice_amd:<digest>` (the async answering-machine verdict, written by `/voice/status`, read by
  `/voice/submit-transcript`), `voice_button_end:<digest>` (written by `/hangup` with trigger `button`, read when the
  report decides on resumption), and the late-answer pair `voice_delegation_result:` and `voice_delegation_abandoned:`
  (the delegation hand-off in the control plane below).

`ticket_ct` and `control_ct` are `CipherPort` ciphertext with the record key in the AAD (`voice/<record key>`), so the
raw ticket and control token are recoverable only from their own record, for its life.

In memory on the relay only, lost on restart (which ends the calls): the per-ticket call registry in
`SfuStreamHandler`, and `CallControlHub`'s digest-to-link map. On the SFU side: one Cloudflare session per web call and
two WebSocket adapters (ids kept in the web call record).

## Flows

### Admission: ticket and one-call marker

Every call starts as a **ticket**: a random UUID, opaque, a bearer credential for exactly one redemption. No store key
holds it raw; every key is a keyed digest under its own purpose. The main service mints it; the relay learns it from
the Twilio `start` event (phone) or from the SFU's WebSocket URLs (web), and trades it at the control plane for the
session config. The relay authenticates nothing else itself: an unknown or already redeemed ticket fails
`fetch_session_config` and the call ends before any provider session opens.

The marker is claimed before the ticket is prepared, with the store's atomic `set_if_absent`
(`VoiceCallSetupService.claim`). `VoiceCallSetupService.prepare` then assembles Lelik's persona through
`LelikAgent.session_config` and writes the ticket and call kind records; on any failure it releases both and alerts, so
a failed start never locks the user out.

A new call replaces a running one of the same user in two directions, never the third:
- a web start meets the user's own **web** call: ends it, waits for its marker to clear (bounded), claims
  (`voice_web_call_app.py::create_voice_web_call_blueprint`, inner `_replace_running_call`);
- a phone dial meets a **web** call: ends its media, waits a shorter bound (Twilio's webhook deadline), then takes the
  marker over regardless (`voice_webhook_app.py::create_voice_webhook_blueprint`, inner `_replace_web_call`);
- nothing replaces a **phone** call: a web start gets 409, a second dial gets `<Reject/>`.

Ending a web call means closing its SFU adapters (`WebCallMedia.close`); the relay sees the egress gone, runs its normal
end path, and the end-of-call report releases the marker. A call whose ticket the relay never redeemed is released
directly by `WebCallMedia.close`.

### Phone call (Twilio): we always call back

The caller dials the Twilio number; we never accept the inbound leg as the call. Caller ID is a claim, the callback is
the test (the line's real subscriber is the one who is rung).

1. `/voice/auth` (`voice_webhook_app.py::create_voice_webhook_blueprint`): verifies the Twilio signature, resolves the
   number to a user (`get_user_by_platform_id("phone", ...)`; unbound number: `<Reject/>` and an alert), requires a
   `CallSid`, claims or replaces the marker, mints the ticket, parks the pending callback, answers "Calling you back" and
   hangs up.
2. `/voice/inbound-status`: on the inbound dial's terminal status, claims the parked callback with `get_and_delete`
   (a duplicate delivery finds nothing). Not `completed`: releases ticket and marker. `completed`: rewrites the marker at
   the full TTL, and `LelikAgent.execute` originates the callback through `TelephonyPort`
   (`TwilioTelephonyAdapter.originate_call`, answering-machine detection on, asynchronous). The callback is placed only
   after the inbound leg ended: placed from inside `/voice/auth` it raced the caller's own line.
3. `/voice/answer?ticket=`: the callback was picked up. Resolves the ticket, `prepare`s the persona, answers TwiML
   `<Connect><Stream>` to the relay with the ticket as a stream parameter. A persona failure releases and speaks a
   plain apology.
4. `/voice/status?ticket=`: async answering-machine verdict (stored as `voice_amd:`, weighed at call end so a voicemail
   greeting is never summarised) and call-leg lifecycle; any terminal status releases ticket and marker. It is the only
   release for a callback that rings out and never reaches `/voice/answer`.
5. Relay: `relay_main.py::route` sends any non-`/sfu/`, non-`/ctl` upgrade to
   `MediaStreamHandler.handle_connection`, which reads the ticket from the `start` event and runs
   `VoiceSessionService.handle_call` until the stream stops. Audio is base64 mu-law 8 kHz carried through untouched;
   played-audio knowledge comes from Twilio's echoed `mark` events (`PlaybackTracker`); barge-in clears Twilio's queue
   with a `clear` event.

### Web call (Cabinet, over Cloudflare's SFU)

Why: a phone call costs PSTN minutes and is narrowband mu-law; the same model over wideband PCM sounded far better and
costs almost nothing in transport. The cost of the choice: the page must stay on screen (an iOS browser cannot keep
audio alive screen-off), so the phone path stays as the hands-free fallback.

1. `POST /api/voice/web-call` (`voice_web_call_app.py::create_voice_web_call_blueprint`, Cabinet session): validates the
   body, mints `call_id`, the ticket and a **control token** (minted here because the SFU dials the relay during
   `attach_agent` and the relay redeems the ticket at once, so its digest must already be on the ticket record), claims
   or replaces the marker, `prepare`s, then `MediaRoomPort.open_caller` (browser offer in, SFU answer out) and writes the
   web call record. Returns `call_id` and the SDP answer; **the ticket never reaches the browser**.
2. `POST .../connect`: `MediaRoomPort.attach_agent` creates the two WebSocket adapters, whose endpoints are
   `<relay>/sfu/ingest?ticket=` and `<relay>/sfu/egress?ticket=`, and has the caller's session subscribe to Lelik's
   track. Then re-reads the record: a hangup that landed during the attach closes the adapters it just made instead of
   resurrecting the call. Hands out `control: {url, token}` when the channel knob is on.
3. `POST .../renegotiate`: the browser's answer to the subscription offer.
4. The **SFU dials the relay**, so no UDP reaches Cloud Run and the relay stays in the media path. Egress carries the
   caller's microphone, ingest takes Lelik's voice. `SfuStreamHandler.handle_connection` keeps one record per ticket; the
   second socket completes the pair and `_run` starts `handle_call`.
5. Audio (all in `SfuStreamHandler`): each message is a protobuf `Packet` (`sfu_packet.py`) of 48 kHz stereo 16-bit PCM,
   20 ms. Inbound goes through `PcmDownsampler` to 24 kHz mono for the provider session. Outbound goes through
   `PacedAudioOutlet` (upsampled by `PcmUpsampler`): provider audio arrives faster than real time, so the ingest writer
   (`_pace`) sends exactly one frame per tick, zero-pads a response's tail and sends silence when idle. "Played" means
   handed to the SFU, and the pacer reports it to `PlaybackTracker`, so barge-in logic is the same as on the phone.
6. Ending: the page's `/hangup` (closes the adapters; trigger `button` also writes the button marker first), or the relay
   ends it (silence, provider error, dead peer). The page polls `.../status`: live while the marker names this call.
   `/voice/submit-transcript` releases the marker.

Web calls need Twilio configured too: `LelikAgent` exists only when the telephony adapter and the from-number are
configured (`UserAgentFactory._build_lelik`), and `prepare` fails closed without it.

### Relay to main: the control plane

`CallControlPlanePort` is the relay's only edge to the main service (`HttpCallControlPlaneAdapter`, a Google OIDC token
from the relay's own runtime identity; the routes accept the invoker identity, the relay's identity and an optional
transition set). Routes in `voice_control_plane_app.py::create_voice_control_plane_blueprint`:

- `/voice/session-config`: redeems the ticket (single use). Refuses with 404 when the marker names another owner (the
  call was replaced before the relay arrived), mints the call's voice MCP tokens and reads the resume handle (both at
  redemption, never at `prepare`, so a stored ticket holds neither), and only then extends the marker to the full TTL.
- `/voice/submit-transcript`: end-of-call usage, transcript and turns, then the summary, then the marker release. The
  release sits in a `finally` and must never be skipped, or the user is locked out of calling for the marker's TTL.
- `/voice/delegate`: runs one specialist delegation for the live call under the call's user context, shielded from the
  handler (Quart cancels a handler whose client disconnects, and the relay disconnects exactly when it stops waiting).
  Its answer is kept so that if the relay stopped waiting it still reaches the chat: `/voice/delegate/abandon` and the
  delegation each write their own marker first, and only an atomic `get_and_delete` of the result posts it, so at most
  one side posts. A failed delegation is kept as a flag, never as error text; interpreter-mode calls never post late
  answers.
- `/voice/relay-warmup`: Cloud Scheduler calls this, and it sends the relay a plain GET and waits (see traps).

### Control channel (page to relay)

A third relay path, `/ctl`, is the page's own socket to the process that runs its call: it carries the "Ask Lelik" button,
the position (`geo`) button, live call text, and the `listening` signal. The protocol (frames, versions, features,
limits) is `src/domain/voice_control_protocol.py`, spec `VOICE_CALL_CONTROL_CHANNEL_RFC.md`.

1. Start: the token is minted (above); only its SHA-256 goes into the ticket record, the token itself, encrypted, into
   the web call record. `/connect` hands it to the page when `_control_channel_on`.
2. The page dials `/ctl`. `CallControlHandler.handle_connection` waits for a first `hello {v, token}` (the token is never
   in the URL: Cloud Run logs upgrade URLs), hashes it, and asks `CallControlHub.attach` for the live call with that
   digest, holding a hello that arrives before the call registered. Unknown token: `bye{auth}`.
3. The call registers its link in the hub when `VoiceSessionService.handle_call` starts (`CallControlHub.open`), with the
   features this call offers (which depend on its mode); teardown closes it (`bye{call_ended}`). A new socket with the
   same token replaces the old one; `welcome` carries current feature state so a reconnect re-syncs.
4. `listening`: the page says it has subscribed to Lelik's track (after `/renegotiate`); the web greeting waits for it,
   capped, because `/connect` takes seconds and a greeting sent earlier was over before the browser listened. A web
   call with no link waits the whole cap; a phone call never waits.

### Dead peer

`SfuStreamHandler._watch_gaps` runs from pairing. If no egress packet arrives for the dead-peer timeout
(`DEAD_PEER_TIMEOUT_S`, knob `VOICE_DEAD_PEER_TIMEOUT_S`, read in `relay_main._dead_peer_timeout_s`), it ends the call
through the normal end path, so the transcript is submitted, the marker released, and the call stays resumable. The
timeout is far above the pauses seen when iOS backgrounds the web app, because the page no longer hangs up when hidden;
the watchdog is for pages that are really gone. It keys on packets, not on what they carry.

## Invariants and traps

- **The relay must stay at one instance** (`cloudbuild-dev.yaml`, relay `--max-instances`). The SFU's two sockets of a
  call, and the page's `/ctl` socket, must reach the same process: pairing and the control hub are in-process dicts.
  Raising it breaks calls silently; scaling means relay shards, each at one instance, with the main service choosing one
  base URL per call for all three paths. No test pins the flag, only the yaml comment.
- **No raw ticket anywhere.** Store keys are keyed digests, hand-out records carry ciphertext, logs carry
  `fingerprint(ticket)`. Enforced by `tests/unit/test_no_raw_ticket_in_logs.py` and
  `tests/unit/web/test_voice_ticket_digest.py`. The control token is likewise never logged and never in a URL.
- **A ticket is redeemed once and atomically** (`get_and_delete`): a captured ticket cannot read back the persona twice.
  `tests/unit/handlers/test_relay_refuses_unredeemable_ticket.py`.
- **Twilio webhooks verify the signature over the external URL including the query string** (the ticket rides in it).
  The verifier is injected and opens with no auth token configured (local dev only). `_external_url` rebuilds the URL
  from forwarded headers because Cloud Run terminates TLS. `tests/unit/web/test_twilio_signature_verifier.py`.
- **The marker must always be released, and only by its owner.** A stuck marker locks the user out; a replaced web call's
  late report must not delete the phone call's marker (`release_one_call_marker`). The store has no compare-and-delete,
  so a takeover between the read and the delete is still lost; `/voice/inbound-status` rewrites the marker whole, which
  usually repairs it. `tests/unit/web/test_voice_phone_replaces_web_call.py`.
- **A replaced call that the relay redeems late is refused** (`/voice/session-config` 404 on an owner mismatch), so it
  never starts and never extends the other call's marker.
- **The marker's full TTL is earned at redemption**, not at start: extending earlier holds it for an hour when the relay
  never arrives. `tests/unit/web/test_voice_phone_replace_budget_and_session_config.py`.
- **Ingest closing ends the call; egress has a short re-attach window.** The SFU retries a dropped egress, and a new
  egress socket for a live ticket re-attaches; a registry removal is by identity, so a stale unpaired call timing out
  cannot evict a newer call under the same ticket. `tests/unit/handlers/test_sfu_stream_handler_hardening.py`.
- **`/sfu/*` and `/ctl` are public sockets.** The handlers cap unpaired and unauthenticated entries, refuse tickets that
  are not UUID-shaped, bound the first frame's time and the page's frame size and rate. Tests in
  `tests/unit/handlers/test_sfu_stream_handler_hardening.py` and `test_call_control_handler.py`.
- **A silent SFU track dies.** Cloudflare garbage-collects a track with no packets, which is why `PacedAudioOutlet` sends
  silence frames when idle. `tests/unit/domain/test_paced_audio_outlet.py`.
- **`attach_agent` is all-or-nothing.** Cloudflare reports per-track failures inside a 2xx body; an adapter without an
  id or with an error code is a `MediaRoomError` and the adapters already made are closed.
  `tests/unit/adapters/test_cloudflare_sfu_adapter_attach_cleanup.py`.
- **The relay needs a keep-alive.** Its cold start is longer than Cloudflare's WebSocket handshake, so the first web call
  after idle failed. Cloud Scheduler cannot hit the relay itself (its request carries a `Content-Length`, which the
  relay's `websockets` parser rejects before any handler), so it calls the main service's `/voice/relay-warmup`, which
  GETs the relay (`relay_main.py` answers plain HTTP with 200). `docs/07_deployment/SCHEDULERS.md`.
- **Formats are domain values** (`voice_audio_format.py`): mu-law 8 kHz for the phone leg and its provider session, PCM
  24 kHz mono for the web provider session, PCM 48 kHz stereo for the SFU wire. The relay builds two
  `VoiceSessionService` instances for this; only the web one gets the control hub, so phone calls have no control channel
  and no dead-peer watchdog.
- **The control channel's kill switch is off in code and on in the deploy.** `_control_channel_on` reads
  `VOICE_CONTROL_CHANNEL` (default off); `cloudbuild-dev.yaml` sets it on. Off: `/connect` hands out nothing, the page
  opens no socket, and every web greeting waits the full `listening` cap.
- **The web call exists only when configured.** `main.py` registers the blueprint, and builds the SFU adapter the phone
  dial also uses, only when the Cloudflare app id, app secret and `VOICE_RELAY_STREAM_URL` are all present; otherwise it
  logs a warning and boots without it. The relay base URL for the SFU and `/ctl` is the origin of that same value.
- **Cabinet pages revalidate** (`Cache-Control: no-cache` on the call pages): a stale cached page after a deploy once
  looked like a ringback bug. An expired Cabinet session sends the call page to login and back
  (`call.html::goToLogin`).
- **The phone number check is a user lookup, not a secret.** A dial from an unbound number is rejected with an alert and
  produces no ticket, marker or callback.

## Links

- [`VOICE_WEB_TRANSPORT_RFC.md`](../../10_rfcs/VOICE_WEB_TRANSPORT_RFC.md) and
  [`voice_web_transport_cloudflare_sfu.md`](../../04_solution_strategy/decisions/voice_web_transport_cloudflare_sfu.md):
  why the SFU, the topology, the pairing constraint, the alternatives rejected.
- [`VOICE_CALL_CONTROL_CHANNEL_RFC.md`](../../10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md): the channel, the dead-peer
  watchdog, replacing a running call.
- [`VOICE_COMPANION_RFC.md`](../../10_rfcs/VOICE_COMPANION_RFC.md): the call itself; §4.5, §4.6 (callback identity),
  §4.13 and §4.14 (relay as its own service) are this block's origin.
- [`SECRETS_AT_REST_RFC.md`](../../10_rfcs/SECRETS_AT_REST_RFC.md) §2.5: tickets as keyed digests, hand-out records as
  ciphertext.
- [`voice_spike_02_mulaw_e2e.md`](../../04_solution_strategy/decisions/voice_spike_02_mulaw_e2e.md),
  [`voice_spike_03_latency.md`](../../04_solution_strategy/decisions/voice_spike_03_latency.md): the phone path's audio
  and latency evidence (POC `scripts/voice/test_mulaw_relay_poc.py`; the SFU POC is `scripts/voice/cloudflare_sfu_poc/`).
- [`runtime_service_accounts.md`](../../04_solution_strategy/decisions/runtime_service_accounts.md): the relay's own
  identity and the control-plane verifier set.
  [`cabinet_session_24h_cap.md`](../../04_solution_strategy/decisions/cabinet_session_24h_cap.md): the session the call
  page depends on.
- Deployment: [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md) (keep-alive),
  [README.md](../../07_deployment/README.md) (relay knobs, TTL policy, the channel switch).

## Where to start reading

1. `relay_main.py::main`: the two session services, the three routes.
2. `src/handlers/sfu_stream_handler.py::SfuStreamHandler`: pairing, pacing, re-attach, dead peer.
3. `src/web/voice_web_call_app.py::create_voice_web_call_blueprint`: the web call's start, connect, hangup, replacement.
4. `src/web/voice_webhook_app.py::create_voice_webhook_blueprint`: the phone path from dial to stream.
5. `src/services/voice_call_setup_service.py::VoiceCallSetupService.prepare`: the ticket, the marker, the failure release.
6. `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`: what the relay asks of the main service.
7. `src/services/call_control_hub.py::CallControlHub` with
   `src/handlers/call_control_handler.py::CallControlHandler`: the control channel.
