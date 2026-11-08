# RFC: Voice call control channel — page ↔ relay, and its first three features

**Status:** Accepted (owner decisions recorded 2026-11-08, §13). Slice 3 (channel skeleton) built: the knob
`VOICE_CONTROL_CHANNEL` defaults to **off** until slice 4 is live-checked (§9 below says "default on" for the finished
feature). Slice 4 ("Ask Lelik") built: adapter `item_id`, `AskLatch` (`src/domain/voice_ask_latch.py`), the relay's
control task and note (`_ASK_NOTE`), the button on the interpreter page, the P1/P2 probe
(`scripts/voice/ask_note_probe.py`), and the edited `VOICE_PERSONA_INTERPRETER` in `prompts_snapshot/` (not uploaded:
§5.6, §12 — after P1 passes and the relay is live). Readings of §5.3 the latch fixes: a command that changes nothing
echoes the current state with its `seq`; speech over Lelik's audible answer is not marked again; the 20 s reply timeout
applies only until the reply starts (the button releases when the answer ends, however long), with a 90 s runaway cap
from the reply's start. Slice 5 (resumption) built; §6 follows the live probe P3.
**Date:** 2026-11-08
**Owner:** Dmytro
**Milestone:** Voice — web call controls

**Related:** `VOICE_WEB_TRANSPORT_RFC.md` §5.4, `decisions/voice_web_transport_cloudflare_sfu.md` (relay
`--max-instances=1` pairing), `decisions/lelik_voice_modes.md`, `VOICE_MULTI_PROVIDER_RFC.md` §4.3, `decisions/voice_xai_protocol_probe.md`.

---

## 1. Problem

The call page and the relay that runs the call share nothing but audio. The page reaches the relay only through the SFU
(`src/web/voice_web_call_app.py:150-154` hands the SFU the ticket-carrying `/sfu/*` URLs) and learns that a call ended by
polling the main service every 2 s (`src/web/static/call.html:810-828`, `voice_web_call_app.py:197-205`). Three owner
needs (2026-11-08) cannot be met that way:

- **A. "Ask Lelik".** In interpreter mode the caller says "Lelik, …" to talk to Lelik instead of the other person
  (`prompts_snapshot/tokens/user/VOICE_PERSONA_INTERPRETER.groovy:24`). Live, the name trigger failed on the first
  utterance and whenever the name was not the first word. A button is reliable, but the relay must learn about the press.
- **B. Session resumption.** A dropped call loses everything Lelik knew (in interpreter mode: who is who, which
  languages). xAI can resume a conversation; nothing carries the handle from one call to the next.
- **C. Hangup on page hide.** Live calls were ended by a browser `/hangup`; the relay side was clean. The page hangs
  up on every `pagehide` (`call.html:1066-1075`). Owner, live: the call dropped when the app went to the background (app
  switch); screen lock not yet checked. The hangup request carries no reason, so the logs cannot tell.

## 2. Goals

1. A **control channel**: a small websocket from the page to the relay process that runs the call, authenticated by a
   per-call token, carrying typed messages both ways.
2. **A** on it: a big "Ask Lelik" button on `/cabinet/call/interpreter` only.
3. **B**: a re-dialled call within the window continues the previous conversation (xAI only).
4. **C**: going to the background never ends the call, on every page; a hangup comes only from the button and the
   existing server-ended and connect-failure paths, and names its trigger in the log.
5. The channel can later carry **on-screen text** (live transcript lines) and a **mid-call mode switch**
   (`lelik_voice_modes.md` "Not in v1"). Named here as future users; not designed.

## 3. Non-goals

- Any change to the `/cabinet/call` UI (owner); C changes its behaviour, not its UI (Q7). Replacing the 2 s status poll.
- Phone calls (no page); the OpenAI path for A/B (modes are xAI-only). Mid-call xAI socket reconnect (§6.4).

## 4. The channel

### 4.1 Topology and auth

The page dials `wss://<relay>/ctl`: the same relay service the SFU dials, so the same single process that holds the call
(`src/handlers/sfu_stream_handler.py:10-11`). No new pairing problem: the channel is matched to the call by a token.

- **Mint.** `POST /api/voice/web-call` (`voice_web_call_app.py:111`) mints `control_token` (32 random bytes, urlsafe)
  next to `call_id` and `ticket`. Its SHA-256 goes into the ticket record through `VoiceCallSetupService.prepare`
  (`src/services/voice_call_setup_service.py:59-73`), so the relay gets it with the session config it already fetches
  once per call (`src/web/voice_control_plane_app.py:151`). The raw token is stored in the `voice_web_call:{call_id}`
  record encrypted, the same way as `ticket_ct` (`voice_web_call_app.py:128`).
- **Hand-out.** `/connect` (the route that builds the SFU URLs, `voice_web_call_app.py:142-167`) returns
  `control: {url, token}` next to `sdp`. Minting at start, not at connect, is forced: the SFU dials the relay during
  `attach_agent`, and the relay redeems the ticket at once, so the digest must already be in the ticket record.
- **Present.** The token goes in the first message, never in the URL: Cloud Run's request log records upgrade URLs.
- **Verify.** The relay holds `sha256 → call` in memory for the call's life; a plain hash suffices for a 256-bit random
  token. No new secret on the relay, no new main-service route.
- **Scope.** One call. The token dies with the call (relay forgets it at teardown); the page never sees the ticket
  (`voice_web_call_app.py:4-6` stays true).

### 4.2 Protocol v1

JSON text frames, each `{"type": …}`; unknown types are ignored (forward-compatible). Max frame 1 KB.

| Direction | Message | Meaning |
|---|---|---|
| page → relay | `hello {v: 1, token}` | first frame, within 5 s of open, or the relay closes |
| relay → page | `welcome {v: 1, features: ["ask"], ask: "idle"\|"armed"\|"active"}` | features this call offers; current state |
| relay → page | `bye {reason: "version"\|"auth"\|"call_ended"\|"replaced"}` | then close |
| page → relay | `ask_arm {seq}` / `ask_disarm {seq}` | press / press again before speaking |
| relay → page | `ask_state {state, seq?, reason?}` | every transition; `reason`: `answered`, `cancelled`, `timeout`, `call_ended` |
| both | `ping` / `pong` | every 20 s from the page; keeps proxies and Cloud Run from idling the socket |

- **Versioning.** `hello.v` is the page's version; the relay answers `welcome` with the version it speaks or
  `bye{version}`. New capability = a new `features` entry, never a changed meaning of an old message.
- **Features per call.** `ask` is offered only when the session config's `mode` is `interpreter`. Future: `transcript`
  (relay → page `line {who, text, final}`), `mode` (page → relay `switch_mode`).
- **Reconnect.** The page re-dials with the same token (backoff 1, 2, 4 … 10 s) while the call is live. A new socket
  replaces the old one (`bye{replaced}` to the old). `welcome` carries the current `ask` state, so the button re-syncs.
- **Relay is authoritative.** The page shows "pressed" on tap (optimistic) and rolls back if no `ask_state` arrives in 2 s.

### 4.3 Relay components

- `src/services/call_control_hub.py` — `CallControlHub`: digest → `CallControlLink` (inbound command queue, `send`,
  attach/replace/detach socket). One per relay process, injected into the **web** `VoiceSessionService` only
  (`relay_main.py:172-185`); the phone instance gets none.
- `src/handlers/call_control_handler.py` — the `/ctl` socket: `hello` deadline, frame cap, rate cap (10 msg/s), hands
  commands to the link. Caps unauthenticated sockets like `_MAX_UNPAIRED_CALLS` (`sfu_stream_handler.py:38-40`).
- `relay_main.py:187-192` routes `/ctl` next to `/sfu/`.
- `VoiceSessionService.handle_call` (`src/services/voice_session_service.py:182-200`) opens the link from
  `config["control_token_sha256"]` when present, runs a `control_task` beside the forward and consume loops, and closes
  the link (sending `bye{call_ended}`) in the teardown `finally` (`:262-287`).

## 5. A — "Ask Lelik"

### 5.1 Behaviour (owner, 2026-11-08)

Press → button visibly pressed (`armed`). The caller's **next** utterance only is marked with a system note. The button
releases when Lelik's answer ends. Pressing again before speaking cancels: nothing is sent to the model.

### 5.2 Where the note goes (probe facts, xAI grok-voice-3.0)

- `input_audio_buffer.speech_started {item_id, audio_start_ms, previous_item_id}` arrives ~0.2 s after speech starts;
  `speech_stopped` 1.1–1.3 s after it ends; `response.created` ~0.2 s after `speech_stopped`; the user item is added only
  at commit, 0.3–1.7 s after `speech_stopped`.
- A `conversation.item.create` (role `system`, `input_text`) sent after `speech_started` lands **directly before** that
  user item: the committed item's `previous_item_id` is the note.
- `conversation.item.delete` works (`conversation.item.deleted`). `response.done` ends a reply and names no user item.
- **Not proven:** that the model honours the note. Synthetic English questions were answered even without one. P1 (§10)
  must test non-English speech (Portuguese in the probe) before the prompt line is relied on.

### 5.3 The latch

A pure state machine, `src/domain/voice_ask_latch.py` (`AskLatch`, no I/O; mypy strict). Methods return actions; the
service performs them. All transitions are synchronous, so the consume loop and the control task cannot interleave
inside one (single event loop; the note is sent after the transition).

| State | Event | Action → next state |
|---|---|---|
| idle | `arm` | → armed |
| armed | `disarm` | → idle, `ask_state{idle, cancelled}` (nothing to delete) |
| armed | `speech_started(item)` | insert note, remember `item` → active |
| armed | 60 s with no speech | → idle, `timeout` (owner, Q1) |
| active | `speech_stopped(item)` | → awaiting_reply |
| active | `speech_started` (a pause split the question, no reply audio yet) | insert the note again |
| awaiting_reply | `response_created` | mark reply started |
| awaiting_reply | `response_done` after reply started | → idle, `answered` |
| awaiting_reply | 20 s from `speech_stopped` and no reply started | → idle, `timeout` |
| awaiting_reply | 90 s from the reply's `response_created` with no `response_done` (a runaway reply) | → idle, `timeout` |
| any | call ends | `call_ended` |

Why "`response_created` after `speech_stopped`": if Lelik was mid-translation when the caller spoke, xAI stops that reply
and its `response.done` arrives before `speech_stopped`; it must not release the button. `disarm` while active/awaiting
is ignored (the words are already marked); the page shows the button as busy.

Note text (owner, Q2; a constant next to `_WEB_PICKUP_NOTE`, `voice_session_service.py:32`; English, bracketed):
`[The caller's next words are addressed to you, not for translation: a question or a remark for you, such as where they
are or which language to use. Answer the caller in their language; do not translate these words.]` It is a system-role
item, the same channel as the pickup and waiting notes; it never enters a user message. It is not deleted afterwards: it sits before the item it
marks, and a delete of a missing item is a call-ending provider error (`voice_session_service.py:480-484`).

**Fallback** if P2 shows `speech_started` is unreliable: insert at press, delete on cancel. It needs the note's item id
(a client-set `item.id`, or a surfaced `conversation.item.added`), so it is built only if P2 asks for it.

**Known limits.** The note marks the next speech, whoever speaks: if the other person talks first, their words get it.
The caller controls the button, so this is accepted. A false VAD start that never commits releases on the 20 s timeout.

### 5.4 Adapter and port changes

- `speech_started` / `speech_stopped` carry `item_id`; both adapters drop it today (`src/adapters/xai_realtime_adapter.py:153-156`,
  `src/adapters/openai_realtime_adapter.py:260-263`; OpenAI's events carry it too, changed for port honesty under
  `docs/how_to/ADAPTER_WIRE_TESTING.md`). The `receive_events` docstring (`src/ports/realtime_session_port.py:41-53`)
  lists the keys. No new port method (`submit_message` exists).

### 5.5 Page

Only when `window.VOICE_MODE === "interpreter"` (`voice_web_call_app.py:53-60` injects it); `/cabinet/call` and the tutor
page get no button and open no control socket in v1. A large button under the call button, label "Ask Lelik" (with
the page's other locales). States: hidden (no call), disabled (channel down or no `ask` feature), idle, pressed
(armed), busy (active/awaiting: Lelik is answering), back to idle on `ask_state{idle}`. Vibrates on press if supported.

### 5.6 Prompt token

Owner (Q2): `VOICE_PERSONA_INTERPRETER.groovy:24` goes entirely: the name algorithm and the "other person speaks to you
directly" rule. Without the button Lelik **only** translates: everything either side says, even nonsense, even words that
sound meant for him. Replacement paragraph: "You translate everything either side says, even when it sounds like nonsense
or seems meant for you. The only exception is a system note saying the caller's next words are addressed to you: then
answer the caller in their language, do not translate those words, and go back to translating." Setup (where they are,
which language) also goes through the button; the note names it. Tokens ship by upload, not deploy: upload **after** the
relay with the latch is live, or the name trigger disappears before the button exists.

## 6. B — Session resumption (xAI)

### 6.1 Provider facts (docs.x.ai, voice-agent, read 2026-11-08; live probe P3, 2026-11-08, grok-voice-3.0)

The probe overrides the docs where they differ.

- **Opt-in:** `session.update` with `session.resumption = {"enabled": true}`, sent on every connection, resumes included.
- **Handle:** `conversation.created.conversation.id`, a plain UUID, sent right on connect, before any `session.update`.
  A resumed connection gets the same value back.
- **Resume:** the WebSocket URL carries `conversation_id=<id>` next to `model=...`.
- **Replay:** `conversation.item.added` events with `"replayed": true` (live items carry `replayed: false`), NOT
  `conversation.item.created`. They arrive BEFORE `session.updated`. Only assistant items were seen replayed for
  text-sent user turns; the model still had the user context. Not said: instructions, tools, system items.
- **A bogus or expired id gets no error.** The session opens fresh, `conversation.created` echoes the sent id, and nothing
  arrives with `replayed: true`. "Resumed" therefore means: at least one replayed item before `session.updated`.
- xAI lets an id be resumed repeatedly; "one resume per handle" (§6.2) is our own rule. History is dropped after 30 min
  of inactivity (docs).
- The replay's billing does not show in `usage` (no extra `billable_audio_seconds` on the first resumed reply).

### 6.2 Design (re-dial resumes)

1. **Adapter.** `open()` always sends `resumption.enabled`; it takes an optional `resume_handle` and adds it as
   `conversation_id` to the URL. It surfaces `conversation.created` as a new event `resume_handle {handle}` (OpenAI emits
   none). With a handle it reads on to `session.updated` (bounded wait) and sets `resumed` = a replayed item was seen
   before it; replayed items are history and produce no event. No replay: it logs `resume: not honoured` and the call is
   fresh.
2. **Relay → main.** The service keeps the handle from the event and sends it, with the provider, in
   `submit_transcript` (the teardown `finally`).
3. **Main (owner, Q3).** Only a call that did **not** end by the hang-up button resumes, within 25 min (under xAI's 30 min
   idle, counted from call end), in the same mode and on the same provider. `/hangup` with `trigger=button` (§7) writes a
   short-lived marker keyed by a digest of the ticket (`button_end_key` in `src/domain/voice_ticket_keys.py`);
   `/voice/submit-transcript` skips the handle when it finds the marker, else stores `voice_resume:{user_id}` =
   `{handle, mode, provider}` (no transcript content), through the ephemeral store with TTL 25 min (the store reads an
   expired value as absent). `/voice/session-config` reads it at ticket redemption (not at `prepare`, so the stored ticket
   never holds a handle and an unredeemed ticket does not burn it), requires user, mode and provider to match, puts
   `resume: {handle}` into the config and deletes the record (one resume per handle). A mismatching record is left to its TTL.
4. **Greeting (owner, Q4).** Lelik speaks first, in his own words, and makes clear he knows where the conversation
   stopped. A resume note replaces the pickup note (and the caller opening, already in the provider's context): a bracketed
   silent system item like the pickup note, describing the situation (the call dropped, the caller dialled back, the
   conversation above is the same one) with no lines to say. Instructions are re-sent fresh. The note is sent only when
   the session resumed.
5. **Not honoured** (unknown or expired id; no error from xAI, see §6.1): the service logs `resume: not honoured` and
   behaves as a fresh call (the ordinary pickup note, caller opening included).

### 6.3 Limits

- A drop kills the call: the SFU legs belong to one relay call (`sfu_stream_handler.py:121-143`), egress gone for 5 s
  ends it (`:190-198`), and the ticket is single-use. Resumption restores **context**, not the call; the user re-dials.
- A relay crash or deploy loses the handle unless teardown reaches `submit_transcript` (likely on SIGTERM; unverified).
  A resumed companion call is summarised on its own, without the first part; modes have no summary.

### 6.4 Deferred: mid-call provider reconnect

The port says "no retry, no reconnect" (`realtime_session_port.py:83-84`). Reconnecting a dropped xAI socket with the
same handle fits this design; no log shows such drops yet. Reopen when one does.

## 7. C — Hangup on page hide

1. **Trigger in the request.** Every `/hangup` call sends `{trigger}`: `button` (`call.html:1050-1056`), `status_ended`
   (`:821`), `start_failed` (`:1045`), `superseded` (`:985-989`, a call cancelled while starting). The route
   (`voice_web_call_app.py:182-195`) logs it from a closed set (anything else → `unknown`). Enums only, no PII.
2. **No hangup on hide, on any page (owner, Q7).** The `pagehide` handler (`:1066-1075`) keeps only `stopRingback()`
   and sends no hangup; `visibilitychange` keeps only re-taking the wake lock (`:776-780`). `/cabinet/call` shares
   `call.html`, so it changes too (behaviour only). When a backgrounded page comes back, the 2 s status poll resumes.
3. **The cost, honestly.** A page that is really closed (app swiped away) can no longer end its call. Today's code ends
   it only when an SFU leg closes: egress gone and not re-attached within 5 s (`sfu_stream_handler.py:185-198`), or
   ingest closed (`:222-245`). Whether Cloudflare closes the legs when the browser's peer disappears is not in our code;
   the comment at `call.html:819-820` says Cloudflare keeps retrying egress until we close the adapters, which suggests it
   does not. No silence watchdog runs on xAI calls (PROVIDER ownership, `voice_session_service.py:255-256`). The hard cap
   is the relay's Cloud Run request timeout, 3600 s (`cloudbuild-dev.yaml:266`). So the worst case is **up to an hour**
   per closed page, billed at $0.08 per audio minute if xAI bills the idle minutes (≤ $4.80; unknown, P4). Meanwhile the
   one-call marker refuses a re-dial with 409 "a call is already in progress" (`voice_web_call_app.py:112-113`), for up to
   that hour: after a swipe-away the user may be unable to call again, and resumption cannot help until the old call ends.
   Both gaps are closed by §7.4 (dead-peer watchdog) and §7.5 (a new call replaces the running one).

### 7.4 Dead-peer watchdog in the relay (owner, Q8)

1. **Rule.** While the call is open, if no inbound audio from the browser arrives for **N seconds**, the relay ends the
   call itself, through the normal end path: `call.end()` (`sfu_stream_handler.py:74-77`) puts `None` on the inbound
   queue, the forwarding loop returns, `asyncio.wait(..., FIRST_COMPLETED)` (`voice_session_service.py:261`) releases and
   the teardown `finally` runs: loops cancelled, session closed, `submit_transcript` (`:282-287`), which releases the
   one-call marker (`voice_control_plane_app.py:305`). The summary policy is unchanged. The call is resumable (§6.2.3: not
   a button hangup).
2. **Where inbound audio enters.** The browser's microphone reaches the relay as the SFU egress socket: each binary
   packet is decoded and queued as an inbound frame (`sfu_stream_handler.py:168-184`, `call.inbound.put_nowait` at `:181`),
   and `_forward_inbound` hands the frames to the provider (`voice_session_service.py:588-590`). The watchdog stamps
   the monotonic time at `:181` and runs as one more task of the call, in the SFU handler (it owns the egress loop and
   `call.end()`), not in the session service. It starts when the call is paired (`_run`, `:121-135`).
3. **Not the existing silence watchdog.** `_watch_silence` (`voice_session_service.py:506`) measures the *caller being
   quiet* (and only on relay-owned turns); it cannot tell a quiet caller from a dead one. This gap is about *no packets*.
4. **N is not fixed yet.** It is chosen from the measurement in slice 2 (§11) and §10 P5. Until then the watchdog is not
   built. The trade-off, plainly: if iOS stops sending audio when the web app goes to the background, the watchdog ends a
   backgrounded call after N s, and resumption on return (§6) is what keeps the conversation; if iOS keeps sending,
   background calls just continue and the watchdog only catches dead pages.
5. **Caveat the data must answer.** The relay sees what the SFU forwards, not what the browser sends. If the SFU or the
   browser emits silence or comfort-noise packets while the microphone is stalled, "no packets" never fires and the
   watchdog is blind; the measurement shows which it is.

### 7.5 A new call replaces the running one (owner, Q9)

1. **Where the lock is.** `POST /api/voice/web-call` calls `call_setup.claim` (`voice_web_call_app.py:112`), which
   refuses when `voice_one_call:{user_id}` exists (`voice_call_setup_service.py:49-52`) and the route answers 409
   "a call is already in progress" (`:113`). The marker holds `{call_id}` (`:112`), readable by `holder`
   (`voice_call_setup_service.py:56`). The phone dial-in path has its own check on the same key
   (`voice_webhook_app.py:123-126`, reject).
2. **New behaviour.** When the claim is refused and the holder is a web call of the same user, the start route ends that
   call instead of answering 409: it loads `voice_web_call:{call_id}` and closes its SFU adapters
   (`media_room.close(record["adapter_ids"])`, as `/hangup` does at `:188`). The relay sees the egress gone, ends the
   call after the 5 s re-attach window (`sfu_stream_handler.py:192-196`) and runs the normal end path of §7.4.1, which
   releases the marker. The start waits for the marker to clear, bounded (a few seconds beyond the re-attach window),
   then claims and goes on; if the marker is still held at the bound, 409 as today.
3. **Resumption.** The replaced call is not a button hangup, so it stores a resume handle like any dropped call, and the
   new call is the re-dial that consumes it (§6.2.3). Order matters: `prepare` reads `voice_resume` only after the old
   call's `submit-transcript` has stored it, which is why the start waits for the marker release (the release is the last
   step of that route). A button-ended call stores nothing, so a new call after the button starts fresh, as decided.
4. **Scope.** Web start replaces a web call. A phone dial-in meeting a running web call, and the reverse, are not
   designed here: the phone path keeps rejecting (`voice_webhook_app.py:124-126`).
5. **Not a security change.** The marker is per user and `/web-call` is authenticated; the caller can only end their
   own call.

## 8. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Control socket cannot connect / drops | button unusable | button disabled, reconnect with backoff; the call itself unaffected |
| `ask_arm` lost | button stays pressed | no `ask_state` in 2 s → page rolls back to idle |
| Note never sent (no speech) | button armed forever | released after 60 s |
| Reply never starts / never ends | button stuck busy | no reply 20 s after `speech_stopped`, or no `response_done` 90 s after the reply started → `timeout` release; a long answer in progress is never cut |
| Model ignores the note | Lelik translates the question | P1 gates the prompt change; the button still releases on `response_done` |
| Relay restart | call ends (SFU legs die) | page sees `status: ended`; re-dial resumes (B) |
| Resume handle unknown or expired | fresh session (xAI raises no error) | no replayed item before `session.updated` → `resume: not honoured` logged, ordinary pickup note |
| Page closed (app swiped away) | call runs on until N s without inbound audio | dead-peer watchdog ends it (§7.4); a re-dial replaces it sooner (§7.5); 3600 s remains the hard cap |

## 9. Security and logging

- Token: random, call-scoped, hash-only on the relay, first frame not URL, never logged (extend
  `tests/unit/test_no_raw_ticket_in_logs.py` to `control_token`). Messages carry enums, `seq`, booleans; future
  `transcript` lines are content: never logged.
- Relay logs per call fingerprint: `control: connected|replaced|closed`, `ask: armed|cancelled|note inserted|released
  (reason)`. Main: `web call {call_id}: hangup trigger=…`.
- Kill switch: `VOICE_CONTROL_CHANNEL` (`os.getenv` on the main service, default on). Off: `/connect` returns no
  `control` and the page shows no button; the caller then has no way to address Lelik until the old token is re-uploaded.

## 10. Probes (all xAI, $0.08 per audio minute; total well under $1)

| # | Question | How | Cost |
|---|---|---|---|
| P1 | Does grok-voice-3.0 honour the note on **Portuguese** speech, and still translate without it? | Probe script (lands in `scripts/voice/` with slice 4) with the new interpreter line; recorded Portuguese question with note, same without; a Portuguese sentence for translation with no note; 3 runs each | ~$0.10 |
| P2 | Is `speech_started` reliable enough (every utterance, before commit)? | Same runs: count `speech_started` vs committed items; also with a short pause mid-question | in P1 |
| P3 (done 2026-11-08, results in §6.1) | Resumption: replay shape, rejected/expired id, replay billing | Two sessions with `resumption.enabled`, reconnect with the id; a bogus id; compare `usage` on the first resumed reply | ~$0.05 |
| P5 (measurement, picks N) | Does iOS keep the microphone and outgoing RTP flowing for the home-screen web app in the background and on screen lock? (Owner saw no drop on lock; the background drops were our own `pagehide` hangup.) | After slice 2: an iPhone (home-screen web app), one call each: background ~10 s, ~1 min, ~5 min, screen lock; read the relay's gap log (§11 slice 2) | $0.08 per audio minute, a few minutes |
| P4 (later) | After slices 1–2: does a call survive app switch and screen lock; how long a swiped-away page's call runs, and does xAI bill it | an iPhone (home-screen web app): switch away and back, lock and unlock; then swipe the app away and time the relay's "call closed" and the billed seconds | ≤ $1 (bounded by the 1 h cap) |

## 11. Slices, in order

| # | Slice | Contents | Tests |
|---|---|---|---|
| 1 | Hangup trigger | §7.1: page sends trigger, route logs it | route logs trigger, unknown → `unknown`, body optional |
| 2 | No hangup on hide + gap log | §7.2, shipped with slice 1, all pages. Also the relay logs inbound-audio gaps per call (§7.4.2): gap start, gap end, duration, call fingerprint only, no content; a gap is logged above a small floor (about 2 s) so normal jitter stays out. The hangup trigger is already logged by slice 1. This is the measurement for P5 | gap detector: gap logged at start and end with duration, none below the floor, no content in the line; otherwise a live app-switch call |
| 6 | Dead-peer watchdog | §7.4, N chosen from P5 data | idle for N s ends the call through the end path (transcript submitted, marker released); a packet resets the timer; not armed before pairing |
| 7 | New call replaces the running one | §7.5 | claim refused + web holder → adapters closed, wait for release, claim; marker never released within the bound → 409; button-ended call stores no handle, replaced one does |
| 3 | Channel skeleton | §4: token mint + digest in ticket, `/connect` returns `control`, hub, `/ctl` handler, hello/welcome/ping/bye, page connects on the interpreter page | hub (attach, replace, unknown digest, teardown); handler (hello deadline, bad token, frame cap, rate cap); `/connect` shape; token never logged |
| 4 | Ask Lelik | §5: adapter `item_id`, `AskLatch`, service wiring, button, then token upload after P1 | latch table row by row; service: note sent on `speech_started` while armed and before the user item, no note on cancel, release on the right `response_done` (not the interrupted reply's); adapter wire tests for `item_id` both providers |
| 5 | Resumption | §6.2 | adapter URL and `resumption` field, `resume_handle` event; service passes handle to `submit_transcript`; main stores/reads/deletes `voice_resume`, mode and provider match, TTL; a button-ended call stores no handle; resume note; refused handle reopens fresh |

Order: 1–2 are a few lines and give the evidence P4 and P5 need; 3 before 4 (4 needs the channel); 5 is independent of
3–4. 6 waits for the P5 data (it fixes N); 7 needs 5 (it relies on resumption) and is otherwise independent.
**Existing tests:** `tests/unit/adapters/test_xai_realtime_adapter.py:198` asserts `speech_started` maps to `{}`; slice 4
changes that on purpose, so a reviewer decides that edit (reviewer rule). No other existing test is expected to change;
no main-service route is added (`/ctl` is on the relay; `/connect` only gains a field).

## 12. Rollout

- Relay first, with no call live (one instance; a new revision drops calls in flight), then the main service; each side
  ignores fields it does not know. For slice 4, then upload `VOICE_PERSONA_INTERPRETER` (after P1 passes).
- Live check per slice: one interpreter call on the owner's phone; logs show `control: connected`, `ask: note inserted`,
  `released (answered)`; for C, an app switch and back keeps the call, and the log names each hangup's trigger.
- Rollback: `VOICE_CONTROL_CHANNEL=off` + re-upload the old interpreter token; resumption: `prepare` stops reading it.
- Docs in the same PRs: `decisions/lelik_voice_modes.md` (interpreter behaviour), `VOICE_WEB_TRANSPORT_RFC.md` §5.4
  (a third relay path), `docs/07_deployment/README.md` (the knob), this RFC's status.

## 13. Owner decisions (2026-11-08) — all decided

1. Armed with no speech auto-releases after 60 s.
2. The interpreter prompt keeps no name rule and no "other person speaks to you" rule: without the button Lelik only
   translates. The note says the next words are addressed to Lelik (a question or a remark, setup included) (§5.3, §5.6).
3. Only a call that did not end by the hang-up button resumes, within 25 min, same mode (§6.2.3).
4. On a resumed call Lelik speaks first in his own words and shows he knows where it stopped; the note describes the
   situation, no fixed lines (§6.2.4).
6. No extra limit when the control channel is down; the media connection bounds the call.
5, 7. (7 replaces 5.) Going to the background never hangs up, on every page including `/cabinet/call`; the cost of a closed page is
   accepted (§7.3); the hangup-trigger log ships.
8. (2026-11-08, owner: "a dead-peer watchdog in the relay: definitely yes".) The relay ends a call itself when no inbound audio
   arrives for N seconds, through the normal end path (§7.4). N is not fixed: it is picked from the slice-2 gap log after
   the owner's background tests (~10 s, ~1 min, ~5 min, screen lock; P5). If iOS stops sending in the background, a
   backgrounded call ends after N s and resumption keeps the conversation; if it keeps sending, background calls continue.
9. (2026-11-08, owner: "a new call replacing the old one: long overdue".) A new call from the same user ends the running one instead
   of answering 409, and starts; the replaced call is resumable unless it ended by the button (§7.5).
   Replacement is web-start-replaces-web-call only (§7.5.4); extending it to the phone dial-in path is an open owner
   question, not decided.
