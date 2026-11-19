# RFC: Geo marks — the owner shares where he is, on purpose

**Status:** Implemented v1 (2026-11-19); landmarks around the position added the same day (§2, §4.1–§4.4, §4.7).
Product decisions are the owner's (2026-11-18) and final; this RFC turns them into a
design. Open: §8 and the Telegram picked-point probe (§9). Slices, file anchors and tests: the private delivery plan.

## 1. Problem

The bot knows one place: the static home location (`UserBotConfig.location`, injected as `user_location` into every
prompt, `src/services/prompt_builder.py:210`). When the owner is elsewhere and asks "is the gym still open?", the bot
answers for home. The owner wants two things:

1. Send where he is right now, to ask a question in that context.
2. Keep the shared point in the conversation, so the bot can relate to it later ("if you are still there").

Sending must be easy and work the same from a phone and from a laptop.

## 2. Owner decisions (2026-11-18, binding)

- **Home is untouched.** The home location and everything built on it stay as they are: no rewording of the
  `user_location` line, no new global "where now" block in any prompt.
- **A geo mark is an event in the conversation**, sent only by the owner: where and when he shared it. A mark with
  no text says that the owner intentionally shared his current position at that time; an earlier mark is not
  evidence that he is still there.
- **No text → just the mark in the chat** (where and when), no bot commentary, no LLM turn.
- **Follow-up without a new mark:** the bot may use the earlier mark, stating its age; beyond that it judges itself.
- **The bot never asks for a mark.** No recent mark → it works from home as today.
- **A mark carries:** coordinates, the nearest street address (reverse geocoding, never an LLM guess), the accuracy of
  the fix in metres, the time it was shared. Coordinates reach the model: the owner shares them on purpose.
- **Landmarks around the position (owner, 2026-11-19; replaces "a venue name is not added").** At a large
  place (a park, a stadium) the nearest address names a small place at its edge or a building across the road, the
  large place itself is absent, and the model cannot tell which place the owner means; a name in the owner's UI
  language can also come back transliterated past recognition. A mark now also carries the landmarks and areas
  Google's reverse geocoding returns for the position (§4.3), each with its relation and distance, worded as reference
  points, never as "the user is at X". Names come in English (`language=en`): a local name stays readable and is never
  transliterated.
- **No movement journal.** Marks live in the session history of their channel (the sliding window); losing old marks
  is expected. Memory may extract patterns from them. Each channel has its own session: a Slack mark is not visible
  to Lelik and the reverse.
- **One command, no separate check-in.** Text after it carries the intent.
- **Entry points v1:** Slack, a `geo` button on Lelik's call page, a Telegram location pin. Not in v1: Telegram live
  location (a stream is not an intentional share), Maps links (a place is not "I am here").
- **The Slack link is one-time and bot-issued**; it needs no Cabinet login (Cabinet pages do not refresh the 24 h
  session). Slack's iOS in-app browser returns browser geolocation (about 2 m) while its Permissions API reports
  "denied": the page calls `getCurrentPosition` directly and never pre-checks permissions.

## 3. Current state (verified in code, 2026-11-18)

- **Slack `$` commands** are dispatched in the queued worker path: `src/adapters/slack/http_adapter.py:497-509` builds a
  command `MessageContext` and calls `ConversationHandler.handle_command`
  (`src/handlers/conversation_handler.py:1469`). The command string is lower-cased (`http_adapter.py:500`); the
  original text stays in `context.text`. Commands are not written to session history.
- **Telegram** drops `message.location`: `_process_message` reads `message.text or message.caption`
  (`src/adapters/telegram/webhook_adapter.py:186`) and media only (`:261`). Every `edited_message` update is dropped at
  intake (`:140`, only `update.message` is read).
- **Telegram Bot API** (read 2026-11-18): `Location` has `horizontal_accuracy` (optional, 0–1500 m) and `live_period`
  (optional, "for active live locations only"). `Message.venue` is set for a venue, and then `location` is set too.
  `caption` exists only for animation, audio, document, live photo, paid media, photo, video, voice and widget; and
  `sendLocation` has no caption: **a location pin never carries text.** Nothing in the API says whether a point was
  the sender's position or picked on the map.
- **Session history:** `SessionStore.append_messages_batch` (`src/ports/session_store.py:61`) appends atomically under
  the sliding window. Two consecutive user messages are a malformed transcript on providers that require alternating
  roles (`src/domain/llm.py:418-424`), so code-written history always comes as a user/model pair: `$memory save`
  (`src/services/memory_fact_service.py:59-66`), skill saved (`conversation_handler.py:1980-1988`).
- **Code text inside the owner's turn:** a voice transcript becomes the owner's text
  (`decisions/voice_message_transcription.md`); chat-upload alerts are extra parts of the owner's message, appended
  after the text (`conversation_handler.py:802-807`) and persisted with it (`_clean_user_parts`, `:1408`). Persisted
  history is never keyed and is read as a record (`SYSTEM_TURN_FINGERPRINT_RFC.md` §4.4); a record that claims no
  system voice has the shape `[Drive note: …]` (`src/domain/drive_note.py:744`).
- **Short-lived capabilities:** `EphemeralStore` (`src/ports/ephemeral_store.py`) has `get_and_delete`, an atomic
  single-use read; `OAuthConnectService` stores a flow under `keyed_digest(digest_key, purpose, state)`
  (`src/services/oauth_connect_service.py:69-76,89`; `src/domain/secret_digest.py:13`). Drive links are a 128-bit opaque
  id, never a JWT, stored under its keyed digest (`src/domain/drive_link.py:3-4`). Every new Ephemeral collection is an
  `EnvironmentConfig` property with a row in `config/ttl_registry.json` (`tests/support/ttl_registry.py:73`).
- **Static pages:** the main app has no static folder; a page is an explicit route over `src/web/static/`
  (`src/web/voice_web_call_app.py:135-143`). Every new route is pinned in `tests/unit/main_boots_offline_url_map.txt` and
  probed by `tests/unit/web/test_every_route_refuses_anonymous.py` (`src/web/CLAUDE.md`).
- **Posting to a channel from outside a turn:** `NotificationChannelFactoryPort.create(platform, channel_id)`
  (`src/ports/notification_channel_factory_port.py:14`) gives a `ResponseChannel`.
- **Queued turns:** Slack events already run as Cloud Tasks (`WorkerHandler`, `slack_worker`,
  `src/composition/application_graph.py:385`); generic task types go through `TaskQueue.enqueue_worker_task`
  (`src/ports/task_queue.py:134`) and the worker's `tasks` registry (`src/handlers/worker_handler.py:131-136`).
- **Google key:** the Maps MCP client authenticates with `GOOGLE_SEARCH_API_KEY` (`X-Goog-Api-Key`,
  `src/adapters/mcp/mcp_client.py:120`; wired at `src/composition/user_agent_factory.py:548-551`); the key is in
  `load_settings()` (`src/config/settings.py:71`).
- **Lelik:** the page ↔ relay control channel (`VOICE_CALL_CONTROL_CHANNEL_RFC.md`) carries page commands only for the
  features a call offers (`CallControlLink.accepts`, `src/services/call_control_hub.py:174`); today only an interpreter
  call has a command consumer (`_run_ask_control`, `src/services/voice_session_service.py:607`, started at `:410-418`).
  Relay notes enter the provider conversation as system-role items (`_system_note`, `:713`; `_ASK_NOTE`, `:58`).
  The channel's kill switch `VOICE_CONTROL_CHANNEL` defaults to off (`src/web/voice_web_call_app.py:69-72`).

## 4. Design

### 4.1 The model

`src/domain/geo_mark.py` (stdlib only):

- `GeoSource(str, Enum)`: `SLACK_LINK = "slack_geo"`, `TELEGRAM_PIN = "telegram_pin"`, `LELIK_BUTTON = "lelik_button"`.
- `GeoFix` (frozen dataclass): `latitude`, `longitude`, `accuracy_m: Optional[float]`, `shared_at` (aware UTC),
  `source`. `GeoFix.parse(...)` refuses a non-finite value, latitude outside ±90, longitude outside ±180, a negative
  accuracy; that is the only validation the entry points need.
- `GeoMark` (frozen dataclass): a `GeoFix` plus the lookup's result: `address: Optional[str]` (the reverse-geocoded
  nearest address), `landmarks: tuple[GeoLandmark, ...]` and `areas: tuple[GeoArea, ...]` (§4.3; empty when the
  lookup returned none). `GeoLandmark` (frozen): `name`, `kinds` (Google place types, minus the generic
  `establishment` / `point_of_interest`, underscores as spaces), `relation: Optional[str]` (Google's
  `spatial_relationship`), `distance_m: int` (straight line, rounded). `GeoArea` (frozen): `name`, `containment`
  (`WITHIN` / `OUTSKIRTS` / `NEAR`). `to_wire()` / `from_wire()` carry all of them.
- Pure renderers (§4.2) and the link id pattern (§4.5).

No Firestore record of marks: a mark exists only as history text (owner: no journal). No schema change, no index.

### 4.2 The text the model reads

**Where it lives.** A mark is a part of a **user** message, because it is the owner's act; its position, time and
accuracy are the owner's own data, and the address is a deterministic lookup, not model text. It is never a model
message (the model would read it as something it said) and never a notify frame.

- No text (Slack link, Telegram pin): one user/model pair is appended — user: the mark text; model: the short
  confirmation the bot posted in the channel (§4.4). The pair keeps roles alternating.
- With text (Slack link only): the chat turn's user message is `[mark text part, owner text part]`; the turn persists it
  with its answer like any message (`_clean_user_parts`). The mark goes first: the place is the context of the question.

**Voice.** The header is `[Geo mark: …]`, the shape of `[Drive note: …]`: it claims no system voice
(`[System: …]` is never used), and it is not keyed (persisted text never is).

**Descriptions, never imperatives.** The text is persisted in a user message, and the consolidator reads every user
message as the owner's words: an imperative there ("do not ask for a position") would become a standing directive.
So every line states what the mark is and what it is evidence of; the model draws the conduct from that. This also
covers "explains, then rules": what it is for, how later turns use it, which gap it bridges (home stays the
reference), all as statements.

**Text (English, built in `render_mark_text(mark, zone)`; fields in braces):**

```
[Geo mark: the user shared {position_phrase}, on purpose, {source_phrase}]
Shared at: {local time, YYYY-MM-DD HH:MM} ({UTC offset})
Position: {lat:.5f}, {lon:.5f}{accuracy_clause}
Nearest street address: {address_clause}
{landmarks_block}
{areas_line}
This records the position the user shared at that moment, for what they ask with it or soon after it. It is not
evidence of where they are at any later time; later, it shows only where they were at its 'Shared at' time. The user
shares a position only when they choose to; with no recent mark in the conversation, the home location stays the
reference.
```

| Placeholder | Value | What the model reads |
|---|---|---|
| `position_phrase` | every fix but the one below (browser, Telegram current position) | "their position at that moment" |
| | Telegram pin without `horizontal_accuracy` | "a point" (the source phrase names Telegram; `accuracy_clause` explains) |
| `source_phrase` | `slack_geo` / `telegram_pin` / `lelik_button` | "from the location link in Slack" / "as a Telegram location" / "from the voice call page" |
| `accuracy_clause` | ≤ 100 m | ", accurate to about {n} m" |
| | > 100 m, no landmarks and no areas | ", approximate: within about {n} m (or {x.x} km), so the address names the area, not the exact spot" |
| | > 100 m, landmarks or areas present | ", approximate: within about {n} m (or {x.x} km), so the address and the places around it name the area, not the exact spot" |
| | absent, Telegram | " — no accuracy came with it, so this may be a point picked on the map rather than where the user was" |
| | absent, Slack or Lelik | " — no accuracy came with it, so how close it is to the spot is unknown" |
| `address_clause` | present | "{address} (looked up from the position, not stated by the user)" |
| | absent (no geocoder, lookup failed, no result) | "not available (the lookup returned no address)" |
| `landmarks_block` | landmarks present | a header line "Landmarks around the position (from Google Maps, nearest first, each with its distance from the position; reference points for where the position is, not stated by the user):", then one line per landmark in the lookup's order: "- {name} ({kinds}), {n} m away: the position is {relation_phrase}" |
| | a landmark without kinds / without a known relation | the "({kinds})" / ": the position is …" part is left out |
| | no landmarks | no line at all (the feature is Experimental outside India; its absence says nothing) |
| `relation_phrase` | `NEAR` / `WITHIN` / `BESIDE` / `BEHIND` | "near it" / "within it" / "beside it" / "behind it" |
| | `ACROSS_THE_ROAD` / `DOWN_THE_ROAD` / `AROUND_THE_CORNER` | "across the road from it" / "down the road from it" / "around the corner from it" |
| `areas_line` | areas present | "Areas (from Google Maps): the position is {containment} {name}; {containment} {name}" — `WITHIN` "within", `OUTSKIRTS` "on the outskirts of", `NEAR` "near"; an area with another containment is left out |
| | no areas | no line at all |
| local time | `UserBotConfig.timezone`, UTC when unset or unreadable | the owner's clock, with its offset, so the age can be computed against the prompt's current time |

The owner's text is never rendered into the mark: it is its own part, verbatim.

**Example (invented):** Alex Doe sends `$geo is the gym still open?` near the Iron Temple gym. History holds a user
message with two parts: the mark ("…shared their position at that moment, on purpose, from the location link in Slack /
Shared at: 2026-11-18 18:40 (UTC+00:00) / Position: 41.10000, -8.60000, accurate to about 5 m / Nearest street address:
Main Street, Springfield (looked up …) / Landmarks around the position (…): / - Iron Temple gym (gym), 20 m away: the
position is beside it / …") and "is the gym still open?". Two days later Alex asks from Shelbyville,
without a mark, where to eat: the mark is two days old, and home (12 Oak Street, Springfield) is the reference.

**Consolidation.** The consolidator may extract "visited Main Street, Springfield on 2026-11-18" or, over several marks,
a pattern ("goes to the Iron Temple gym"). The owner accepts both; a single day's event has no value and
may be lost. The "at that moment / not evidence of later" wording is what keeps it from becoming a current-state fact.
If live consolidations show state facts ("Alex is at Main Street"), the fix is in the consolidation token, not here
(§8).

### 4.3 Reverse geocoding

- **Port** `src/ports/reverse_geocoding_port.py`: `ReverseGeocodingPort.look_up(latitude, longitude) -> GeoPlace`
  (`GeoPlace`: `address`, `landmarks`, `areas`, the §4.1 types; `GeoPlace.EMPTY` when nothing came back). A system
  boundary with a test double; justified by the port rule.
- **Adapter** `src/adapters/google_geocoding_adapter.py`: `GET https://maps.googleapis.com/maps/api/geocode/json` with
  `latlng`, `language=en`, `extra_computations=ADDRESS_DESCRIPTORS` and `key`, aiohttp, a 3 s total timeout.
  - `address`: the first result's `formatted_address`.
  - `landmarks` / `areas`: the response-level `address_descriptor` (up to 5 landmarks, 3 areas). A landmark's name is
    `display_name.text`, its distance `straight_line_distance_meters`; an area's `containment`. An entry without a
    name or a distance is skipped; an unknown relation becomes `None`.
  - Address descriptors are GA in India and Experimental everywhere else (Google, read 2026-11-19); they bill on the
    same Geocoding SKU. A missing or malformed `address_descriptor` gives no landmarks and never fails the address.
  - `ZERO_RESULTS`, any other status, an HTTP error or the timeout → `GeoPlace.EMPTY`, logged with the status only.
    It never raises to the caller.
- **Why `en`, not the owner's UI language:** Google translates names into the requested language when it has a
  translation and transliterates them otherwise, and a transliterated local name no longer says what the place is.
  English keeps a local Latin-script name as it is, and the place type stands next to
  it. A local-language name would need a second request after the country is known.
- **Key:** `GOOGLE_SEARCH_API_KEY`, already in `load_settings()` and already sent to `mapstools.googleapis.com`. No new
  secret; the Geocoding API must be enabled on the project and allowed by the key's API restrictions (§9). With the
  key absent the adapter is not built and every mark has no address.
- **Failure never fails the mark.** No address is a valid mark (§4.2 table).
- **Logging:** never coordinates, never the address or a landmark name. A line carries the source, the accuracy band
  (`precise`/`approx`/`none`), `address=yes|no`, the landmark and area counts and the user id prefix.

### 4.4 `GeoMarkService` (`src/services/geo_mark_service.py`)

Owns the whole flow; every entry point calls it. Ports: `EphemeralStore` (links), `ReverseGeocodingPort` (optional),
`SessionStore`, `NotificationChannelFactoryPort`, `TaskQueue`, `UserRepository` (time zone), `LocalizationPort` (the
confirmation line); plus `digest_key` and `base_url`.

- `locate(fix, language) -> GeoMark`: geocode (never raises).
- `record(mark, *, user_id, session_id, channel, language) -> str`: render, post the confirmation line to `channel`
  (`ResponseChannel`), append the user/model pair (`raise_on_error=True`), return the confirmation. A failed post is
  logged and the pair is still appended; a failed append raises (the caller tells the owner the mark was not saved).
- `issue_link(binding) -> str` / `peek_link(raw) -> Optional[GeoLinkBinding]` / `redeem_link(raw, fix) -> GeoRedeem`:
  §4.5.

The confirmation line (a `UIMessage`, four locales) is what the bot posts and what the model message of the pair
holds: "📍 Location saved: {address} (±{n} m, {HH:MM})", or the no-address variant. With an address and landmarks it
is "📍 Location saved: {address}; nearby: {names} (±{n} m, {HH:MM})": the two nearest landmark names that the address
does not already contain; none left → the plain line. It is code text in a model message, and it is true: the bot
did post it.

### 4.5 Slack: `$geo [text]`, the link and the page

**Command.** `ConversationHandler.handle_command` gains `command.split()[:1] == ["geo"]`. The owner's text is taken from
`unwrapped(context.text)` after the `$geo` word, in its original case (the command string is lower-cased). The handler:

- works only in the owner's Slack direct message (`channel_type == "im"`, carried in the command's metadata): in a shared channel any member could post forged coordinates into the owner's session first and the redeemed address would be posted publicly. A localized line says so.
- refuses in a bound channel (`ChannelBindingService.get`): marks live in the default session only; a bound channel
  reads platform history or a companion session (`_resolve_session_mode`, `conversation_handler.py:566-590`). A
  localized line says so.
- on Telegram answers with a localized line pointing to the pin (the Cloud Task turn of §4.5 is built for Slack only;
  Telegram has a native share).
- otherwise calls `issue_link` and posts the link with `send_document_link(url, label)` (the Slack adapter formats it,
  `src/adapters/slack/response_channel.py:596`), with the validity in the line.

**Link.** `https://<service>/geo/<id>`: `secrets.token_urlsafe(16)` (22 URL-safe characters, 128 bits), the drive-link
precedent. Stored in a new `EphemeralStore` collection `{prefix}geo_mark_links` under
`geo_link:{keyed_digest(SECRET_DIGEST_KEY, "geo_mark_link", id)}`; the raw id is never stored; application logs carry only its fingerprint (Cloud Run's request log records the request path; accepted: single use, 10 minutes, and the id is already in the owner's DM). Record
(`GeoLinkBinding`): `user_id`, `account_id`, `platform`, `channel_id`, `session_id`, `platform_user_id` (the Slack user,
re-authorized when the turn runs), `language`, `text` (the owner's text, may be empty). TTL **10 minutes** (the
oauth-flow precedent; the owner taps it at once). Single use: the POST takes the record with `get_and_delete`. A new
`$geo` mints a new link; older ones simply expire.

"Bot-signed" is realised as a bot-issued capability, not a signed self-contained token: single use needs server state
anyway, and the repo standard for a one-time URL is an opaque id under its keyed digest (drive links, OAuth flows). A
forged id finds no record.

**Routes** (`src/web/geo_mark_app.py`, anonymous by design, the id is the capability; every refusal is one 404 page):

- `GET /geo/<id>`: a wrong-shaped id → 404 with no store read. `peek_link` (a `get`, never a delete): unknown or
  expired → the "link is not valid" page. Else `src/web/static/geo.html` with the owner's UI strings injected (the
  `_mode_page` precedent), headers `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `X-Robots-Tag: noindex`, `Permissions-Policy: geolocation=(self)`, a CSP of `default-src 'none'`, `connect-src 'self'`
  and the inline script allowed by its hash. A GET consumes nothing, so Slack's link unfurler and a page reload are
  harmless.
- `POST /geo/<id>`: `Content-Type: application/json` required (a cross-site form cannot send it without a preflight),
  an `Origin` header, when present, must be this service; body capped at 1 KB:
  `{latitude, longitude, accuracy}`. `GeoFix.parse` → 400 on a bad value **before** the record is taken, so a page bug
  does not burn the link. Then `redeem_link`; the answer is `{status, address, accuracy_m}` or the refusal.

**Page.** On load it calls `navigator.geolocation.getCurrentPosition` at once with
`{enableHighAccuracy: true, timeout: 20000, maximumAge: 0}`; no Permissions API call. States:

- locating: a spinner and "Getting your position…".
- success: POST; then ✓ + the address (or "saved, no street address found") + accuracy.
- `PERMISSION_DENIED`: "Location access is off for this browser or app", how to turn it on (system settings → the app →
  Location), and **Try again** (calls `getCurrentPosition` again: the link is not consumed).
- `POSITION_UNAVAILABLE` / `TIMEOUT`: "Could not get a position" and **Try again**.
- server 404/410 (expired, used): "This link is no longer valid — send `$geo` again".
- server error after the record was taken: "The position was not saved — send `$geo` again".

A failure on the page sends nothing to the server and posts nothing in Slack.

**Redeem** (`redeem_link`, all before the response — Cloud Run throttles CPU once it is sent):

1. `get_and_delete` the record; none → `GeoRedeem.GONE`.
2. `locate` (≤ 3 s), then post the confirmation line to the bound Slack channel (`channel_factory.create("slack",
   channel_id)`).
3. No text: append the pair (§4.2) to the bound `session_id`. Done.
4. Text: enqueue `geo_mark_turn` (`enqueue_worker_task`, deadline 1800 s, `dedup_key` = the link digest) with
   `{platform, user_id, account_id, channel_id, session_id, platform_user_id, text, mark_text, language}`. If the
   enqueue fails, the mark is not lost: the pair is appended instead and the page says the question was not sent.

**Queued turn.** `geo_mark_turn` is served by the Slack adapter (`SlackHttpAdapter.handle_geo_mark_turn`, registered in
the worker's `tasks` next to `slack_worker`): it re-authorizes the Slack user (IAM), resolves the language, builds the
`SlackResponseChannel` and a `MessageContext` with `text` = the owner's text and the new field `geo_mark_note` = the
rendered mark, and runs `ConversationHandler.handle_message` under the adapter's session lock. It shares one private
turn helper with `_process_message_event` (`http_adapter.py:428-534`) rather than faking a Slack event. The service
stays platform-neutral (REQ-ARCH-20): it never builds a Slack event. `ConversationHandler` puts
`MessagePart(text=context.geo_mark_note)` before the owner's text part (`conversation_handler.py:802`); the Router keeps
reading `context.text`, the owner's words.

### 4.6 Telegram: the location pin

In `_process_message`, before the text path, when `message.location` is set and the message is not forwarded:

| Case | Detection | Result |
|---|---|---|
| Venue (a place picked from the list) | `message.venue` set | no mark; a localized line: a place is not recorded as a geo mark |
| Live location | `location.live_period` set | no mark; a localized line: live locations are not recorded. Its later `edited_message` updates stay dropped at intake |
| Current position | `horizontal_accuracy` present | a mark, read as the owner's current position |
| Point without accuracy | `horizontal_accuracy` absent | a mark, worded as "a point" from Telegram that may be picked on the map (§4.2) |

The Bot API states no flag for "picked on the map"; v1 relies on accuracy being absent for such a point (Telegram
clients attach it to a GPS fix). This is an assumption about clients, not a documented fact: the rollout probe (§9)
checks it per client. If a picked point arrives with accuracy on some client, it would read as "current position";
that result goes to the owner before U2 is called done.

A pin never carries text (§3), so it is always a mark without a turn: the adapter calls the new
`ConversationHandlerPort.handle_geo_mark(fix, context, response_channel)`; the handler checks the binding and calls
`GeoMarkService.locate` + `record` with the Telegram chat as the channel. `shared_at` is `message.date`. The owner's
follow-up message is an ordinary turn that sees the pair in history.

### 4.7 Lelik: the `geo` button

The page gets a position and sends it over the control channel; Lelik receives it as context in the running call.

- **Protocol** (additive, `VOICE_CALL_CONTROL_CHANNEL_RFC.md` §4.2 rules): feature `geo`, offered on every web call when
  the channel is on. Page → relay `geo {seq, latitude, longitude, accuracy}`; relay → page
  `geo_state {seq, state: "sent"|"failed", address?}`. At most one `geo` per 10 s per call.
- **Relay:** one control task for every call with a link, dispatching by type (today's `_run_ask_control` becomes its
  `ask` branch). On `geo`: `GeoFix.parse`, reverse geocoding (the relay composes the same Google adapter; the relay
  service gains the `GOOGLE_SEARCH_API_KEY` secret), then `_system_note` with `render_call_note(mark)`, then
  `geo_state`.
- **The note** (system role, in the provider conversation, never persisted by us, so a plain rule is fine there):
  "[The caller has just shared {position_phrase}, on purpose, {source_phrase}: {lat}, {lon}{accuracy_clause};
  nearest street address: {address_clause}{landmarks_clause}{areas_clause}. They shared it for what they ask with it.
  Do not read the coordinates aloud.]" (the §4.2 phrases, with "the caller" for "the user" in the no-accuracy and
  address clauses). `landmarks_clause`: "; landmarks around the position (from Google Maps, nearest first): {item};
  {item}", each item "{name} ({kinds}), {n} m away, the position is {relation_phrase}" with the §4.2 omissions;
  `areas_clause`: "; areas: the position is {containment} {name}, {containment} {name}". Each is empty when there are
  none.
- **History:** in-call only. It is not added to the call transcript buffer, the post-call chat note or companion
  memory: each channel keeps its own marks (owner), and the call is Lelik's session. A resumed xAI call keeps it (the
  provider conversation holds it).
- **Why a later slice.** It needs a refactor of the relay's control task (a consumer for non-interpreter calls), a
  secret on a second deploy unit, a relay-first deploy, a live probe that a system item inserted mid-call (possibly
  during a response) is honoured by both realtime providers, and the control channel switched on in production. None
  of it touches U1–U3 files; it can run in parallel once U1 lands, but the probe gates it.

### 4.8 Placement (checked against `tests/unit/test_req_arch_01_hexagonal_isolation.py`)

| Piece | Layer | Rule it respects |
|---|---|---|
| `GeoFix`, `GeoMark`, renderers | `domain/geo_mark.py` | REQ-ARCH-01 (stdlib), REQ-ARCH-32 (no *Service) |
| `ReverseGeocodingPort` | `ports/` | REQ-ARCH-04/06 |
| Google adapter | `adapters/` | REQ-ARCH-18 (HTTP only in adapters), REQ-ARCH-16 |
| `GeoMarkService` | `services/` | REQ-ARCH-17 (no `os.getenv`), -20 (no platform format), -21 (collection via `EnvironmentConfig`), -22 |
| `$geo`, `handle_geo_mark` | `handlers/conversation_handler.py` | REQ-ARCH-10/25: imports the service, not ports |
| Telegram pin | adapter → `ConversationHandlerPort` | REQ-ARCH-16: adapters never import services |
| `/geo/<id>` routes | `web/geo_mark_app.py` | REQ-ARCH-15: the service only |
| Wiring | `composition/geo_mark_setup.py` | the only layer crossing boundaries |

### 4.9 Privacy

The deferred decision `decisions/user_location_pii_tradeoff.md` concerns the home address in every prompt; it is not
touched. Marks are explicit shares: their coordinates and address reach the model in the turns that carry them, as the
owner decided. They are kept only where history is kept (the session window, then consolidation). Logs carry neither
coordinates nor addresses (§4.3) and, for a link, only its fingerprint (Cloud Run's request log keeps the path: accepted, see §4.5). `$geo` works in the owner's DM only. The link record holds the owner's text for at most 10 minutes. Examples in tests and
docs use the invented universe only.

## 5. Alternatives rejected

- **A signed self-contained link (JWT/HMAC)** — single use needs a store anyway; the repo's one-time links are opaque
  ids under a keyed digest.
- **Mark as a model message or a notify frame** — the model would own it, or it would claim the system's voice.
- **A protocol token explaining marks** — a new global block (owner: none); the mark explains itself.
- **A synthetic Slack event re-entering `handle_queued_event`** — the service would build a Slack-shaped payload
  (platform format in services); a neutral task served by the adapter is the same code path without it.
- **Posting the mark via `UserNotificationService.notify`** — it writes a `[System: …]` frame into history.
- **The offline place table as an address fallback** — a town name is not the nearest address; no address is honest.

## 6. Conflicts with the owner's concept

- **`/geo` vs `$geo`.** The concept names Slack `/geo`. A Slack slash command needs a registered command and a new
  signed endpoint; an unregistered `/geo` is rejected by Slack before it reaches the bot. This RFC uses `$geo`, the
  repo's command protocol (the brief's choice). Owner to confirm.
- **A Telegram pin cannot carry text** (Bot API, §3): the "with text → a turn" path exists only for Slack; on Telegram
  the question is the next message. This narrows nothing the concept decided.

## 7. Failure modes

| Failure | What the owner sees | Alert |
|---|---|---|
| Geocoding fails / API disabled | mark without address | warning log with the status |
| Slack post fails on redeem | page ✓; the pair is still in history | error log |
| Session append fails | page: "not saved, send `$geo` again" | error log |
| Turn enqueue fails | page: mark saved, question not sent | error log |
| Browser denies / times out | page with Try again; nothing sent | none (owner action) |
| Link expired or used | "link no longer valid" page | info log |

## 8. Open questions

1. **Google Maps Platform terms on storing geocoding results.** The address stays in session history (and may reach
   facts through consolidation). Accept at one-user scale, or ask for another source?
2. **Consolidation of marks.** Wait for live evidence; if state facts appear, change the consolidation token.

## 9. Rollout

1. Enable the Geocoding API on the GCP project; add it to `GOOGLE_SEARCH_API_KEY`'s API restrictions if the key is
   restricted.
2. TTL policy on `expires_at` for the `geo_mark_links` collection group (the generated runbook in
   `docs/07_deployment/README.md`). No composite index: links are read by document id.
3. Deploy U1–U3 (main service only). Probe: `$geo` from a phone (the Slack app) and from a laptop (desktop browser);
   with and without text; deny once and retry.
4. Telegram probe (U2): from each client the owner uses, send the current location, a point picked on the map, a
   venue and a live location; record whether `horizontal_accuracy` is present for each, and report it.
5. U4: the relay probe of §4.7, then the relay deploy with the secret, then the page.
