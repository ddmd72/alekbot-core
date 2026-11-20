<!-- living-doc
covers:
  src/domain/geo_mark.py
  src/services/geo_mark_service.py
  src/composition/geo_mark_setup.py
  src/web/geo_mark_app.py
  src/web/static/geo.html
  src/ports/reverse_geocoding_port.py
  src/adapters/google_geocoding_adapter.py
-->
# Geo Marks

## Purpose

A **geo mark** is a position the owner shares on purpose, turned into one event of the conversation that the model
reads. Three doors lead to it: `$geo` in a Slack direct message (a one-time link to a browser page), a Telegram location
pin, and the share button on Lelik's call page. Each door gets a fix from the owner's device, the block reverse-geocodes
it through Google (nearest street address, plus the landmarks and areas around the position), and the result reaches
the model as text in the conversation.

What it is not: it does not track location. Nothing is recorded unless the owner presses the button, sends the pin or
taps the link; there is no movement journal and no collection of marks. It does not touch the static home location
(`UserBotConfig.location`, injected into prompts by `src/services/prompt_builder.py::PromptBuilder`, a separate and
deferred privacy question: `decisions/user_location_pii_tradeoff.md`). The bot never asks for a mark; with no recent mark
it works from home. A mark is not evidence of where the owner is later: the rendered text says so and the model draws
its own conclusion about age.

## Owned data

Almost nothing is stored; that is the design.

- **Link records** (the only stored data): `EnvironmentConfig.geo_mark_links_collection`, an ephemeral store written by
  `src/adapters/firestore_ephemeral_store.py::FirestoreEphemeralStore`. One document per pending Slack link, under
  `geo_link:` plus the keyed digest of the link id; the raw id is never stored or logged. The record is
  `src/domain/geo_mark.py::GeoLinkBinding`: whose mark, the channel and session it lands in, the Slack user the queued
  turn re-authorizes, the UI language, and the owner's text after `$geo` (possibly empty). Lifetime `LINK_TTL_S`;
  `config/ttl_registry.json` carries the sweep policy for links nobody opened. The store reads the expiry itself.
  Writer: `GeoMarkService.issue_link`. Reader and deleter: `peek_link` (read only) and `redeem_link` (take once).
- **The mark itself** is history text: a user-message part rendered by `src/domain/geo_mark.py::render_mark_text`,
  appended to the channel's session by `GeoMarkService`. It lives exactly as long as the session window keeps it, and
  memory consolidation may extract from it like from any user message. A Slack mark is not visible to Lelik or Telegram,
  and the reverse: each channel has its own session.
- **Values** (all in `src/domain/geo_mark.py`): `GeoFix` (a validated position: coordinates, optional accuracy, aware
  UTC time, `GeoSource`), `GeoMark` (a fix plus `GeoPlace` data: address, `GeoLandmark`, `GeoArea`), `GeoPlace.EMPTY`
  for a lookup that returned nothing. `GeoFix.parse` is the one validation every door uses.

## Flows

### Slack: `$geo [text]`

1. The command reaches `src/handlers/conversation_handler.py::ConversationHandler._handle_geo_command` through
   `handle_command`. Guards in `_geo_marks_for` and the handler: no service configured, a bound channel (marks live in
   the default session only), Telegram (answers with a pointer to the pin), and anything but a direct message each get
   one localized line and no link.
2. `src/services/geo_mark_service.py::GeoMarkService.issue_link` mints an opaque random id (`secrets.token_urlsafe`), stores the binding under its
   keyed digest, returns the URL. The handler posts it through the channel's document-link send.
3. `GET` of that URL, `src/web/geo_mark_app.py::create_geo_mark_blueprint` (`geo_page`), calls `peek_link`: it consumes
   nothing, so a link unfurler or a reload is harmless. It serves `src/web/static/geo.html` with the owner's strings in
   a data block.
4. The page asks the browser for a fresh high-accuracy position at once and `POST`s `{latitude, longitude, accuracy}`
   (`geo_fix`). All checks (origin, JSON content type, body cap, `GeoFix.parse`) run before the link is taken, so a
   refused request leaves the link usable for the page's retry.
5. `GeoMarkService.redeem_link` takes the record once (`get_and_delete`), runs `locate`, posts the one-line confirmation
   to the bound Slack channel, then branches:
   - **No text:** `_save_pair` appends the user/model pair (mark text, confirmation) to the session. No LLM turn.
   - **Text:** enqueues the `geo_mark_turn` worker task with the link digest as `dedup_key`. If the enqueue fails the
     pair is appended instead and the page says the question was not sent.
6. The queued turn: `src/services/geo_mark_turn_task.py::GeoMarkTurnTask` checks the payload and hands it to
   `src/adapters/slack/http_adapter.py::HTTPModeAdapter.handle_geo_mark_turn`, which re-authorizes the Slack user, takes
   the session lock and runs the normal chat turn with `MessageContext.geo_mark_note` set.
   `ConversationHandler.handle_message` puts that note as a message part before the owner's text, and the turn persists
   both parts as one user message.

Everything in steps 2 to 5 completes before the HTTP response: Cloud Run throttles CPU once it is sent.

### Telegram: the pin

`src/adapters/telegram/webhook_adapter.py::TelegramWebhookAdapter._handle_location` runs before the text path. A venue or a
live location gets a one-line notice and no mark; a forwarded location is not taken as a mark; a plain pin becomes a `GeoFix` (time
from the message) and goes to `ConversationHandler.handle_geo_mark`, which calls `locate` and `record` on the chat's
session. A pin never carries text (the Bot API gives a location no caption, RFC §3), so there is never a turn: the owner's next
message is an ordinary turn that sees the pair in history. A point whose accuracy is absent is worded as "a point" that
may have been picked on the map, because the API has no flag for that.

### Lelik: the share button

1. `src/services/voice_session_service.py::VoiceSessionService` offers feature `FEATURE_GEO` (defined in
   `src/domain/voice_control_protocol.py`) on every web call that has a control link, interpreter or not. The control
   channel is behind the `VOICE_CONTROL_CHANNEL` knob; the call page shows the button only when the call offers it.
2. The page sends a `geo` frame; `_run_control` dispatches it to `_handle_geo`: parse (`parse_geo_command`), refuse
   with a reason if the provider session is not open or a fix came too soon after the last one, look up the address
   through the relay's own geocoder, insert one system note with `render_call_note` (`_mid_call_note`), answer with a
   `geo_state` frame.
3. The note exists only in the provider conversation of that call. It is not in the call transcript buffer, the post-call
   chat note or companion memory.

The relay composes its own `GoogleGeocodingAdapter` (`relay_main.py::_reverse_geocoder`); that deploy unit therefore
needs the same key as the main service.

### Reverse geocoding

`src/ports/reverse_geocoding_port.py::ReverseGeocodingPort.look_up` returns a `GeoPlace` and never raises.
`src/adapters/google_geocoding_adapter.py::GoogleGeocodingAdapter` asks Google's Geocoding API (English names, address
descriptors on), takes the first result's address, and reads landmarks and areas from the response-level descriptor,
sorted nearest first because Google ranks by prominence. Any failure, any non-OK status or a timeout is
`GeoPlace.EMPTY`, and a malformed descriptor drops the landmarks but keeps the address. Wiring:
`src/composition/geo_mark_setup.py::build_geo_mark_service` builds the adapter only when `GOOGLE_SEARCH_API_KEY` is set
(without it every mark has no address) and builds no service at all without a task queue (a local run), in which case
`$geo` and the pin answer that sharing is unavailable. The route is registered in `main.py` only when the service exists.

## Invariants and traps

- **The mark text is description, never imperative.** `render_mark_text` is persisted in a user message and the
  consolidator reads every user message as the owner's own words, so a rule written there would become a standing
  directive. Only `render_call_note` (a system item of a running call, never persisted) may state a rule. The header is
  `[Geo mark: ...]`, which claims no system voice and is not keyed. The shape of both renderers is pinned in
  `tests/unit/domain/test_geo_mark.py`.
- **Roles must alternate.** A mark with no turn is always a user/model pair (the model half is the confirmation line the
  bot really posted), never a lone user message: some providers reject two consecutive user messages. With text, the mark
  is a part of the owner's own user message instead, placed before the question.
- **The owner's text is never rendered into the mark.** It stays its own message part, verbatim; `$geo` lower-cases the
  command string, so the handler takes the text from the original `context.text`.
- **A geocoding failure never fails a mark.** No address is a valid mark; the text says the lookup returned none.
  `GeoMarkService.locate` also guards against an adapter that raises.
- **No coordinates, addresses, place names or raw link ids in logs**, and the adapter logs no URL (it holds the key).
  Logs carry source, an accuracy band (`accuracy_band`), `address=yes|no`, counts, a user-id prefix and the link
  fingerprint. A test over the service checks this.
- **Link is a capability, so every refusal is the same.** Unknown, expired and used links are
  indistinguishable to the caller (one plain 404 on GET, one 410 on POST). A wrong shape (`LINK_ID_PATTERN`) is a 404 on
  both and never reaches the store. The route is anonymous on purpose
  and is pinned in `tests/unit/main_boots_offline_url_map.txt` and probed by
  `tests/unit/web/test_every_route_refuses_anonymous.py`.
- **Direct message only.** In a shared channel any member could post forged coordinates into the owner's session and the
  redeemed address would be posted publicly. The page never sees which channel; the binding fixes it at issue time.
- **The page never calls the Permissions API.** Slack's iOS in-app browser reports "denied" while geolocation still
  works (owner's observation, RFC §2); the page calls `getCurrentPosition` directly. `tests/unit/web/test_geo_page_static.py` pins this, the retry
  without reload, and the CSP: the inline script is allowed by its hash (`script_csp`), so the hash follows the script
  and nothing else may run inline.
- **Producer and consumer of the task payload share one key list.** `GeoMarkService.redeem_link` builds the payload,
  `GeoMarkTurnTask` reads `PAYLOAD_KEYS`; a missing key is 400, a platform outside `TURN_PLATFORMS` is 400, no runner is
  501. Tests match both sides. The turn re-authorizes the Slack user and drops the turn if the authorized user is not the
  link's owner, because a link can outlive revoked access.
- **A `$`-text with a mark note is not a command.** In `HTTPModeAdapter._run_authorized_turn` the command branch is
  skipped when `geo_mark_note` is set, so an owner question starting with `$` is not re-dispatched as a command.
- **Landmark wording.** Relations are reference points ("the position is beside it"), never "the user is at X". For
  `WITHIN` the distance is left out, because Google measures to the landmark's point and a distance would contradict
  "within". Names are requested in English: a local name in the UI language can come back transliterated past
  recognition.
- **Geocoding must be enabled and allowed, or marks silently lose their address.** The Geocoding API has to be enabled
  on the project and permitted by the key's API restrictions (RFC §4.3 "Key", §9.1). Otherwise Google answers a non-OK
  status, the adapter returns `GeoPlace.EMPTY` (`src/adapters/google_geocoding_adapter.py::GoogleGeocodingAdapter.look_up`)
  and the only signal is a `[Geocoding] status=...` warning log.
- **Address descriptors are Experimental outside India** (Google, per RFC §4.3). An absent landmark list says nothing about the place.
- **Telegram pin vs map point is an assumption.** The Bot API has no flag; the code relies on clients attaching accuracy
  to a GPS fix. If a client sends a picked point with accuracy, it reads as a current position.
- **Open owner questions:** whether storing geocoding results in session history fits Google Maps Platform terms, and
  whether consolidation should treat marks specially (changing its token, not this block).

## Links

- RFC (design, now a point-in-time record): `docs/10_rfcs/GEO_MARKS_RFC.md`.
- Owner rulings are in RFC §2: home untouched, a mark is an event sent only by the owner, no journal, one command, link
  one-time and bot-issued.
- Home address in every prompt (separate, deferred): `docs/04_solution_strategy/decisions/user_location_pii_tradeoff.md`.
- The `geo_mark_turn` task: `docs/07_deployment/SCHEDULERS.md`. Control channel protocol:
  `docs/10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md`.
- Fingerprint of code text in user turns: `docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md`.

## Where to start reading

- `src/domain/geo_mark.py::GeoFix` and `render_mark_text`: what a mark is and what the model reads.
- `src/services/geo_mark_service.py::GeoMarkService.redeem_link`: the whole Slack flow in one method.
- `src/web/geo_mark_app.py::create_geo_mark_blueprint`: the page, its headers and the POST checks.
- `src/handlers/conversation_handler.py::ConversationHandler._handle_geo_command` and `handle_geo_mark`: the doors.
- `src/adapters/google_geocoding_adapter.py::GoogleGeocodingAdapter.look_up`: the lookup and its failure modes.
- `src/services/voice_session_service.py::VoiceSessionService._handle_geo`: the Lelik door.
