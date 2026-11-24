<!-- living-doc
covers:
  src/web/user_cabinet_app.py
  src/web/cabinet_auth.py
  src/web/facts_pin_app.py
  src/web/static/cabinet.html
  src/web/static/cabinet.css
  src/web/static/cabinet-strings.js
  src/web/static/cabinet-shell.js
  src/web/static/cabinet-sections.js
  src/web/static/cabinet-settings.js
  src/web/static/cabinet-home.js
  src/web/static/cabinet-memory.js
  src/web/static/cabinet-reminders.js
-->
# User Cabinet

## Purpose

The Cabinet is the owner's web page for what chat is a poor place for: seeing and pruning what the bot remembers,
connecting and disconnecting integrations (Gmail, drives, Microsoft To Do), and setting personal preferences (language,
timezone, schedules, reminders, team invites). It is one page served at `/cabinet`: a shell
(`src/web/static/cabinet.html`: header, menu, section markup) and its assets (`cabinet.css`, `cabinet-strings.js`,
`cabinet-shell.js`, `cabinet-sections.js`, `cabinet-settings.js`, `cabinet-home.js`, `cabinet-memory.js`,
`cabinet-reminders.js`: one
stylesheet and plain scripts sharing one global scope, loaded in that order, no build), with five sections: Home,
Memory, Drives, Reminders, Settings
([CABINET_REDESIGN_RFC](../../10_rfcs/CABINET_REDESIGN_RFC.md)). Settings (`cabinet-settings.js`, RFC §5.1) is groups of
collapsed rows (Account, Profile, Connections, Security, Voice, More), one row open at a time; a switch or a select
saves at once, City alone has a Save button.
Reminders is its own section of rows (label, next run in the user's timezone, repeat); a row opens to Edit and Delete, and
"+ New" and Edit open the one form (`#reminder-form`), which `cabinet-reminders.js` moves into the row's body or above the
list, so one form is open at a time. The preset rules are named in words, any other rule is shown raw.
Behind it is one Quart blueprint of JSON routes behind the JWT session that the OAuth sign-in set.

What it is not: it is not a second way to write memory (a correction is a message the user sends in chat, see Flows),
its routes call services and repositories through ports, and the rules that live in the route closure are listed under Invariants and traps; it it does not own
the voice call page (`/cabinet/call` is `src/web/voice_web_call_app.py`, which reuses only the session gate).

## Owned data

The Cabinet owns no collection. Each route reads or writes data that another block owns, through the port named here.

| What the route changes | Owner of the data |
|---|---|
| Platform ids (`platform_identities`: Slack, Telegram, phone) | `UserRepository`; users and accounts: [oauth_multi_tenant](../oauth_multi_tenant/README.md) |
| Timezone, location, spoken languages, Gmail schedules, deep-research switches, `preferred_language`, `agent_mirror` | `UserBotConfig` of the user, through `UserRepository` |
| Invite codes and team membership | `InviteCodeService` |
| Fact state `invalidated`; answers to review questions | `CabinetFactService`, `FactAnswerService`; model in [memory](../memory/README.md) |
| Reminders | `AgentNotePort` (agent notes) |
| Gmail jobs, indexed mail, credentials | [gmail_email_indexing](../gmail_email_indexing/README.md); credentials through `OAuthCredentialsPort` and `CredentialRevocationService` |
| Drive connections, scope, notes, indexing switch | `DriveConnectionService`, `DriveDisconnectService`, `DriveNoteService`; [user_drive](../user_drive/README.md) |
| Microsoft To Do link and index | `TaskSetupService`; [tasks_integration](../tasks_integration/README.md) |

## Flows

### Page load and session

1. `src/web/user_cabinet_app.py::create_user_cabinet_blueprint` serves `/cabinet` with `Cache-Control: no-cache` (a
   cached page outlived deploys), and the page's assets at `/cabinet/assets/<name>`, only the names of `CABINET_ASSETS`
   (anything else is `404`; the main app has no static folder), with the same `no-cache`. Neither needs a session;
   every `/api/...` call does. The open section is kept in `?tab=` (old values `integrations`, `devices`, `team` are
   aliases; a connect return opens the place of its connector). Home is the default section, on a phone and a computer
   alike (no or an unknown `tab`); a bare `/cabinet` no longer redirects a phone to the call page (the Home call
   button is the way there).
   Home's tiles each show one status line (`cabinet-home.js`), read in parallel from routes the page already uses:
   Memory from `GET /api/user/facts-pin` (and `facts/review-queue` only when the facts are open, never a fact),
   Drives from `GET /api/drive/connections`, Reminders from `GET /api/user/reminders` and `/api/user/timezone`,
   Settings from `GET /api/gmail/status` and `/api/tasks/microsoft/status`. A failed read leaves a neutral line.
2. Every API route is wrapped by `src/web/cabinet_auth.py::make_auth_required`: a Bearer header or the `__Host-access`
   cookie goes to `CabinetSessionVerifier.identify` (the JWT with its `sid`, then `DeviceSessionService.is_live`);
   `g.user_id`, `g.account_id`, `g.role` and `g.session_id` come only from a verified token of a live session. A bad,
   missing or revoked one is `401`. The docs pages (`owner_only`) and `/join` ask the same verifier. Lifetimes and
   refresh: `decisions/cabinet_device_session_lifetimes.md`.
3. Sign-in itself, including the email whitelist that refuses unlisted users and the passkey sign-in page, is the
   OAuth block (`decisions/cabinet_oauth_whitelist_gate.md`); the Cabinet only consumes the session.

### Browse and search facts

1. `GET /api/user/facts/browse` (`browse_facts`) returns one page of the account's facts, newest first, with an opaque
   cursor. `state` is `current` (default) or `stale`, parsed by `src/domain/fact_review_overview.py::listable_state`;
   `domain` is one key of `src/domain/fact_domains.py` (any other value is `400`); `limit` is capped by the route.
   Query: `FirestoreFactRepository.get_paginated_facts` in `src/adapters/firestore_repo.py`.
2. `GET /api/user/fact-domains` gives the area labels in the page's UI language. A fact that has no
   profile yet has `primary_domain: null` and is in no domain filter.
3. `GET /api/user/facts/domain-counts?state=current|stale` (`fact_domain_counts`) gives `{total, counts}`: the number of
   facts of that state in all and per registry key, areas with none left out; `state` is parsed as browse parses it
   (`400` otherwise). `FactRepository.count_facts_by_domain` runs one count aggregation for the total and one per key,
   concurrently, over `get_paginated_facts`' own query (ordering kept), so each number is the length of the list the
   same filter browses and the listing's indexes serve it. A fact in two areas counts in both; area counts need not add
   up to the total.
4. `POST /api/user/facts/search` embeds the query (`EmbeddingService`, inside `billed_scope`, so the cost is billed to the
   account) and runs `FactRepository.search_facts` with a fixed result limit, for the JWT's account (`account_id` is the filter).
5. `GET /api/user/facts/recently-closed` lists facts archived or invalidated within `SET_ASIDE_WINDOW`
   (`fact_review_overview.py`), each with the actor of its last lifecycle entry.

The Memory section (`cabinet-memory.js`, CABINET_REDESIGN_RFC §5.2) draws these as: the review questions collapsed into
one row at the top (its count; it opens to the question UI of "Memory-review questions"), the search field with a dark
Search button, the state as a segmented control (current / stale / closed), and the life area as one select with counts
("All areas · total"): its list holds only the areas with facts in that state, the fullest first, with a filter field, a
line saying area counts overlap and a footer with the number of empty areas hidden; it is hidden for "closed" and over
search results. Facts are rows (text, area tag, date) on every list; a row opens to Edit (the correction message below)
and Delete; closed facts are read-only rows. Under the list: "Showing n of total" and "Show more" while browse has a
next page.

### Delete a fact

1. The page calls `POST /api/user/facts/<id>/invalidate` (`invalidate_fact`), which calls
   `src/services/cabinet_fact_service.py::CabinetFactService.invalidate` with the JWT's `account_id`.
2. The service refuses a fact of another account (`PermissionError`, `403`), follows a superseded id to its current
   version (`CABINET_ACCEPTS` in `src/domain/fact_lifecycle.py`), and refuses to delete a version whose text differs from
   what the page showed (`cabinet_invalidation`, `FactResolutionError`, `409`). The page reloads the list on `409`.
3. It writes `state = invalidated` in place with a lifecycle entry (actor `cabinet`), then refreshes the biographical
   context cache of the account, so the fact leaves the prompt at once.

### Correct a fact (no write API)

The page builds an English message ("current text / correct text / please update it") and copies it to the clipboard
(`copyCorrectMessage` in `cabinet-memory.js`); the user pastes it into chat. The correction then goes through the normal
consolidation path, which owns re-embedding and lineage. There is deliberately no Cabinet route that rewrites a fact.

### Memory-review questions

`GET /api/user/facts/review-queue` lists the open questions of this user (`FactAnswerService.review_queue`) with the
answer options each allows. `POST /api/user/facts/review-answers` takes 1 to `MAX_BULK_ANSWERS` answers and applies each
through `FactAnswerService.apply` (the same path as a chat or button answer, source `cabinet`). The response is
per question; one failure shows as `"error"` for that question and does not stop the others. Rules of the review:
`decisions/fact_review_and_owner_questions.md`.

### Integrations

Gmail, Microsoft To Do and the Telegram/Slack/phone links are rows of Settings → Connections; the drive card is the
Drives section, which the Drives row opens.

The Drives section draws a loading line while its first read runs (`cabinet-sections.js::loadDriveStatus`; the
connections, deep-research-copy and connectors reads run in parallel) and, when that read fails, an error line with a
Retry button. Once the card is drawn, a re-open or the 2-minute poll keeps it as it is, whatever the read does. The
call pages (`call.html`, `call_modes.html`) link back to `/cabinet` (Home), not to a section.

The External workers card links to a plain-language help page, `GET /cabinet/help/workers`
(`src/web/static/workers_help.html`, served like `/cabinet`: public static HTML, no-cache, holding nothing of the user's).
It is a separate page in the four UI languages with its own dictionary; being static it cannot read the Cabinet's
language, so the card's link carries it as `?lang=` (else the phone's language, else English).

In a Settings row the title keeps at least 40% of the row and wraps only between words; the value takes at most 60%, on
one line with an ellipsis (the opened row shows it whole), so a long value never squeezes the title on a phone.

1. `GET /api/connectors` tells the page which connect buttons to draw; the connect link is `/auth/connect/<kind>`
   (OAuth block). The Cabinet holds no list of kinds.
2. Gmail: `/api/gmail/status` reports the connection, the index frontiers and the running job. `/api/gmail/index`
   persists the job, then enqueues it (persist first, or the worker can find no job). A sync fallback runs when no task
   queue is wired. `mode` is `incremental` or `backfill`; `reindex` is refused with `400` because it would erase the
   cards. Schedules (`/api/gmail/auto-index`, `/api/gmail/daily-review`) write `UserBotConfig` only; the hourly jobs read it.
3. Disconnect (`DELETE /api/gmail/disconnect`) first marks "disconnected by user" through `IntegrationReconnectService`,
   then removes the credentials; indexed mail stays. `DELETE /api/gmail/data` deletes the indexed mail.
   Reconnect notices: `decisions/reconnect_notice_r1.md`.
4. Microsoft To Do: the `/api/tasks/microsoft/...` routes call `TaskSetupService` and the To Do
   provider. Google Tasks has `/api/tasks/status` and `/api/tasks/disconnect`.
5. Drive card: the `/api/drive/...` routes. Each route resolves the connection through the
   caller's own user id first, so another user's id answers `404`. Scope and indexing changes enqueue a `drive_sync` task
   so the indexer acts at once; with no drive queue the change is saved, a warning is logged and `queued` is false.
   A disconnect that leaves no primary drive turns the deep-research drive copy off.
   `GET /api/drive/connections` reads its rows and each row's parts concurrently (`asyncio.gather`), in stored order.

### Settings

- Language (`/api/user/language`): the Interface language and Reply language rows both send the pair
  (`preferred_language`, `agent_mirror`). `LanguagePreferenceService.set_preference` writes the config, swaps the user's
  `LANG_*` prompt-override token, invalidates the prompt cache and notifies the bot, so the change is seen in chat.
  A code that is not a supported language (or not a string) is refused with 400 and nothing is stored.
  Details: [localization_system](../localization_system/README.md). A POST without `agent_mirror` means mirror.
- Timezone, location, voice languages, the Gmail schedules, the deep-research switches: `UserBotConfig` writes of only
  the fields the route changes (`UserRepository.update_user_fields`), so a concurrent change of another field (a language
  switch in chat, another Cabinet save) survives. Timezone is validated as an IANA name.
- Reminders (`/api/user/reminders`): a `due` without an offset is a wall time in the user's timezone
  (`src/domain/local_time.py::wall_time_instant`) and is stored as UTC; the list answers in the user's timezone. A
  recurrence rule is normalized and phrased by `RecurrencePort`, so the page never parses a rule. An explicit empty
  `recurrence` on update stops the repeat; an absent one leaves it unchanged.

### Devices and passkeys

Settings → Security (`src/web/device_auth_app.py`, CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5): the Devices row lists the
live sessions with "Log out", "Log out all other devices" and "Log out all devices"; the Face ID and passkeys row lists
the user's live passkeys (`GET /api/user/passkeys`), each with "Remove"; "If a phone is lost" holds the steps. "Add Face ID for faster sign-in" runs the enrolment
(`/api/user/passkeys/register/options`, then `/register/verify`) from this session, through `session.js`
`addPasskey`; the user is loaded with `resume_session`, so an inactive or de-listed user cannot add one. After a Google
sign-in (`offer` in the list answer) the page shows a banner offering the same, once: a dismissal or an attempt is
remembered in this browser's `localStorage` (`alek.passkeyOffer`), and blocked storage only means the banner comes back.
When the user already has a passkey, adding or removing one needs a fresh proof on this session: a passkey assertion or
a Google sign-in under 5 minutes old. A browser that holds a local passkey marker (`alek.hasPasskey`, set on a passkey registration, sign-in or step-up)
runs the step-up and retries once; any other browser, or a failed step-up, goes straight to
`/auth/login/google?next=/cabinet?tab=devices&resume=add_passkey` (`remove_passkey&id=` for a removal), so the empty
iOS passkey sheet never opens. The return opens the passkeys row; the Cabinet consumes the flag and starts the change
itself, showing the Add button with a hint in that row if the browser refuses WebAuthn without a tap.
Labels and platform hints are User-Agent words, escaped in the page.

### The facts PIN

Opt-in per user (CABINET_DEVICE_SESSIONS_RFC §7.7; the rules are `FactsPinService`). A user without a PIN sees the Memory
tab as always. With one, the nine facts routes (`/api/user/facts`, `/fact-domains`, `/facts/browse`,
`/facts/domain-counts`, `/facts/recently-closed`, `/facts/review-queue`, `/facts/review-answers`, `/facts/search`, `/facts/<id>/invalidate`)
carry `facts_unlock_required` and answer 403 `facts_locked` until this session is unlocked: the tab asks for the PIN
(`POST /api/user/facts-pin/unlock`), which opens it for 15 minutes on that session only. Leaving the tab or closing the
page relocks it (`sendBeacon` to `/facts-pin/relock`; the 15 minutes are the guarantee). The routes live in
`src/web/facts_pin_app.py` behind the Cabinet gate: status, `set` (PIN and duress code, each typed twice), `change` and
`remove` (both need the current PIN), `reset` and `reset/cancel` (a forgotten PIN: 24 hours, a Google sign-in under five
minutes old). A wrong code is 400 `wrong_code` with `attempts_left`; the duress code and the fifth wrong code in a row
are both 423 `account_locked`. Never a 401 (`authFetch` would re-send), never a code in an answer or a log. The Memory PIN
row of Settings → Security (CABINET_REDESIGN_RFC §5.3) shows one state at a time: set in two steps (PIN, then the duress
code), change in three (current PIN, new PIN, duress code), remove, forgot → pending → cancel. A wrong current PIN goes
back to the first step with the attempts left and the typed codes cleared; the page checks only the codes' shape (four
digits, typed alike, duress unlike the PIN) before a request, the server keeps every rule. It offers "Log out all other
devices" after a change or a reset request. A new
facts route without the decorator fails `test_facts_pin_app.py`.

### Platforms, invites, team

- `POST /api/user/link-platform` binds Slack only (`_LINK_PLATFORM_ALLOWED`); Telegram has its own numeric-id route; a
  phone is linked only after a Twilio Verify code (`request-phone-otp`, `verify-phone-otp`, E.164 by `_E164_PATTERN`).
  Without a verify client these answer `501`.
- Invites: only a token with role `owner` may create a code (`403` otherwise). The code is returned once; later lists
  carry the hint only. `POST /api/user/join-team` and the `/join?code=` deep link both end in
  `InviteCodeService.consume_team_invite`; `/join` sends an unauthenticated visitor to login and back.

## Invariants and traps

- **No route skips the gate.** `tests/unit/web/test_every_route_refuses_anonymous.py` probes every route; only `/health`
  and the `/cabinet` shell are listed public. A new route is also pinned in `tests/unit/main_boots_offline_url_map.txt`
  (`src/web/CLAUDE.md`).
- **Tenancy comes from the token, never from the request.** Fact routes filter by the JWT's `account_id`; reminders,
  Gmail jobs, platforms, invite lists and drive connections by its `user_id` (`_own_connection` for drives).
  `tests/unit/web/test_cabinet_owner_and_tenant_refusals.py` shows the refusals for docs, Gmail jobs, reminders and
  facts on stored data; drives have no such case there.
- **Most optional collaborators answer `501` when absent** (`503` for fact delete); status reads answer
  `{"connected": false}`, `/api/connectors` answers an empty list, and `POST /api/user/language` without its service
  writes nothing and still answers `200`. Only the invite, session, user, fact and embedding dependencies are required
  by `create_user_cabinet_blueprint`. A new feature route must pick its behaviour on purpose, and `main.py` must pass
  the new argument.
- **Rules that live in the route closure** (not in a service): only role `owner` creates an invite
  (`create_invite`); a drive disconnect that leaves no primary turns the deep-research drive copy off
  (`drive_connection_disconnect`); a recurring reminder's due time is moved to its first occurrence on create
  (`create_reminder`); a Gmail job is persisted before it is enqueued, with a sync fallback when no queue is wired
  (`gmail_index`); cancel writes the job status directly (`gmail_cancel_job`); a scope preview counts only folders the
  saved scope does not already cover (`_new_scope_roots`, nested in the blueprint factory).
- **Drive settings are changed only here.** The writers (`rename`, `set_scope`, `set_indexing`, `disconnect`, ...) are
  called only from `user_cabinet_app.py` and `drive_disconnect_service.py`, so the bot cannot change them in chat
  (`tests/unit/test_req_arch_34_drive_settings_cabinet_only.py`). Files the index could not read are likewise shown in
  the Cabinet only (`decisions/drive_unreadable_files_cabinet_only.md`).
- **The `/cabinet/docs` routes are owner-only and answer `404`, not `401`,** to anyone else, and are off when
  `DOCS_OWNER_USER_ID` is unset.
- **A delete never removes text the owner did not see** (the `409` above, `cabinet_invalidation`). The Cabinet never
  writes a fact's text.
- **Composite indexes before deploy.** Browse and the domain counts need the `account_id, state, created_at` index and
  the variant with `domain_keys` in `config/firestore.indexes.json`; a missing one fails at query time. The counts keep
  the browse query's ordering on purpose: dropping it would count undated documents browse never lists and ask for
  other indexes.
- **Cancelling a Gmail job writes its status directly** (`gmail_cancel_job`); the worker stops chaining on the next task,
  so a running page can finish one more step.
- **Fact and drive text is untrusted:** the page puts it through `escapeHtml` before rendering.

## Links

- [oauth_web_api](../oauth_web_api/README.md) (sign-in, JWT), [oauth_multi_tenant](../oauth_multi_tenant/README.md)
  (users, accounts, roles)
- `decisions/cabinet_oauth_whitelist_gate.md`, `decisions/cabinet_device_session_lifetimes.md`,
  `decisions/fact_review_and_owner_questions.md`, `decisions/reconnect_notice_r1.md`,
  `decisions/drive_unreadable_files_cabinet_only.md`, `decisions/deep_research_drive_copy.md`
- `docs/10_rfcs/FACT_LIFECYCLE_RFC.md`, `FACT_PROFILE_RFC.md`, `MULTI_DRIVE_RFC.md`, `DRIVE_MCP_CONNECTOR_RFC.md`
- [Database schema](../../08_concepts/DATABASE_SCHEMA.md) (facts), [embedding_system](../embedding_system/README.md)

## Where to start reading

- `src/web/user_cabinet_app.py::create_user_cabinet_blueprint` (all routes, one closure)
- `src/web/cabinet_auth.py::make_auth_required`
- `src/services/cabinet_fact_service.py::CabinetFactService.invalidate`
- `src/adapters/firestore_repo.py::FirestoreFactRepository.get_paginated_facts`
- `src/services/language_preference_service.py::LanguagePreferenceService.set_preference`
- `src/web/static/cabinet.html` (the `section-*` blocks); `src/web/static/cabinet-memory.js` (`invalidateFactDirect`,
  `copyCorrectMessage`, `renderAreaSelect`)
