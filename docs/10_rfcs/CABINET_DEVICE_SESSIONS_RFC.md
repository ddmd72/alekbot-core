# RFC: Cabinet device sessions, a facts PIN and the account lock

**Status:** Accepted (2026-11-22).
**Supersedes on implementation:** `decisions/cabinet_session_24h_cap.md`. The whitelist gate
(`decisions/cabinet_oauth_whitelist_gate.md`) stays as it is.
The decisions are in §2 and §13. Every example below is invented (`cabinet.example.com`, the user `alex@example.com`,
the labels "iPhone — Home Screen (call)" and "Mac — Chrome"); a PIN or a duress code is always written `$<digits>`.

## 1. Problem

The Cabinet (facts, directives, mail index) and the voice call pages (Lelik, interpreter, tutor) log the user out
too often. In practice each entry point needs a full Google OAuth login every 24h. Each iPhone Home Screen app has
its own cookie jar, so each one logs in on its own. Two needs follow:
- starting Lelik should be easy;
- a lost or stolen device must be shut out quickly.

Today the second is impossible. Tokens are stateless JWTs, and nothing can revoke a single device.

## 2. Decisions (2026-11-22)

- **Industry baseline, not paranoid mode.** The target is OWASP ASVS 5.0 Level 2 session management. NIST SP
  800-63B-4 is a reference, not a target. Coercion is not in the threat model.
- **Remember the device.** One Google sign-in per browser or Home Screen app. The session slides for up to 30 days
  and ends after 14 days of inactivity.
- **Passkeys are the fast way back in, not a mandatory login.** Whenever a session is needed, Face ID signs in
  instead of Google. Google stays as the fallback and the enrolment path.
- **A devices list** in the Cabinet, for every user: log out one device or all of them. `$devices off` does the same
  from chat.
- **A chat notice on every new sign-in**, for every user.
- **An opt-in 4-digit PIN guards the facts tab only.** A user who sets no PIN sees the facts tab as today, and
  nothing can lock that user through the PIN. The device lock protects the rest of the Cabinet and Lelik.
- **A duress code, set together with the PIN, and five wrong PIN entries in a row, lock the whole account.** Every
  entry point and every background job of the user stops, and every session is revoked. Only an operator unlocks it,
  flipping the flag by hand in Firestore. Typos can therefore lock the account until an operator acts: an accepted
  risk.
- **No concealment.** After the lock fires, the bot and the Cabinet say plainly that the account is locked, and the
  notice goes to every chat channel.
- **Accepted exposure.** A stolen phone whose passcode is known keeps whatever the phone's live sessions give until
  the account holder acts (§8).

## 3. Threat model

| Attacker has | Today | After this RFC |
|---|---|---|
| A phone, unlocked or with a known passcode, with live sessions | Up to 48h of Cabinet and calls; no revocation | The Cabinet and Lelik until `$devices off`, which takes effect in ≤30 s. With a facts PIN set, the facts tab needs the PIN, and five wrong guesses lock the account |
| The same phone, with the account holder's Google account signed in on it | A new Google login at will | Every new session is announced in chat. With a PIN set, the account holder answers with the duress code in chat: a full lock in seconds (§7.8) |
| A stolen session cookie (copied, not the device) | Valid until its JWT expires | Rotation detects reuse and revokes that session (§7.1) |
| An unlocked laptop left in a public place | 24h | The same as the first row, plus the 14-day idle end |

Out of scope:
- coercion of the account holder;
- an attacker with Firestore or Secret Manager access;
- Telegram and Slack sessions: the bot cannot log out a chat app;
- the SIM card used to place phone calls.

## 4. Current state (verified in code, 2026-11-22)

**Sessions.**
- `src/config/auth.py:92-93`: `ACCESS_TOKEN_TTL` and `REFRESH_TOKEN_TTL` are both 86400. MCP has its own TTLs (`:103-104`).
- `SessionService` (`src/services/session_service.py:18-242`) signs both tokens as HS256 JWTs. Nothing is stored server-side.
  The refresh token is not rotated. Its age is capped by the current TTL (`:228-232`).
- `/auth/callback` (`src/web/oauth_app.py:169-267`) sets the httponly cookies `access_token` and `refresh_token`
  (`:234-251`).
- `/auth/refresh` (`:272-341`) calls `AuthenticationService.resume_session` (`authentication_service.py:256-281`).
- `/auth/logout` (`:346-363`) only deletes the cookies.
- `make_auth_required` (`src/web/cabinet_auth.py:8-33`) is the JWT gate of the Cabinet blueprints, used by
  `user_cabinet_app.py:175` and `voice_web_call_app.py:81`.

**The gate is not the only reader of the cookie.** Eight places read the `access_token` cookie and call
`SessionService.verify_access_token` themselves:

| Place | What it decides |
|---|---|
| `src/web/landing_app.py:25` | `/`: a signed-in visitor goes to the Cabinet |
| `src/web/mcp_consent_app.py:58` | The MCP connector consent page: which user grants the connector |
| `src/web/file_access_app.py:79` | A gated `/f/<token>` file link: the cookie's user must match the link's |
| `src/web/oauth_app.py:88` | `_session`: the user of the `/auth/connect/<kind>` routes (`:533`, `:575`) |
| `src/web/oauth_app.py:381` | `/auth/me` |
| `src/web/oauth_app.py:448` | `/auth/link-oauth`: linking a provider identity to the signed-in user |
| `src/web/user_cabinet_app.py:193` | The decorator of the internal docs pages |
| `src/web/user_cabinet_app.py:243` | `/join`: the team invite link |

A revocation that only the gate honours leaves all eight open. The worst case is the consent page granting the MCP
connector from a revoked device (§7.1 closes it).

**The frontend never refreshes.** Nothing in `src/web/static/` calls `/auth/refresh`. `call.html:1219-1236`,
`call_modes.html:250-252` and `cabinet.html:3550-3580` send a 401 straight to `/auth/login`.

**The "inactive" flag exists but is checked in two places only.** `UserProfile.is_active` (`src/domain/user.py:470`) is
read by `resume_session` (`authentication_service.py:270`), which blocks a refresh, and by
`UserAgentFactory._load_user_profile` (`src/composition/user_agent_factory.py:751`), which raises `PermissionError`
when a user's agents are built. The factory keeps the agents cached per user for an hour after their last use
(`:250-251`), so that check does not stop a user an instance already serves.
- Sign-in does not check it: `handle_oauth_callback`, `:103-254`.
- Chat does not check it: `IAMService.authorize` Branch 1 (`iam_service.py:169-203`) checks only the whitelist.
- Neither do the worker, the voice routes or MCP: `_allow_reason` (`mcp_authorization_service.py:500`) checks only
  existence, email and whitelist.
- Nothing in `src/` ever sets the flag to false.
- `BillingAccount.is_active` (`src/domain/billing.py:166`) is read by `check_quota` (`firestore_account_repo.py:229`,
  no callers) and `list_all_accounts` (`:217`).

So the flag is reusable, but every entry point class needs its check (§7.8).

**`$` commands never reach session history or a model.**
- Telegram: the `$` branch (`src/adapters/telegram/webhook_adapter.py:263-281`) calls
  `ConversationHandler.handle_command` and returns before `handle_message`.
- Slack: `_dispatch_command` (`src/adapters/slack/http_adapter.py:651-683`, called at `:529` and `:739`) does the same.
- Slack builds history from the channel. `SlackChannelHistorySource` filters `$` messages out
  (`src/adapters/slack/channel_history.py:41`, `:82`).
- `handle_command` (`conversation_handler.py:1475-1631`) writes no session messages, with one exception: `$geo` with
  text queues a turn by design.
- In both adapters `IAMService.authorize` runs before the dispatch (Telegram `:206`, Slack `:469`, `:589`, `:629`,
  `:702`; the queued `geo_mark_turn` task re-authorizes at `:370`).
- Not verified: whether either adapter logs the raw text of a command before dispatch. §7.9 makes that a rule with a
  test.

**Keyed digest.** `keyed_digest(key, purpose, value)` (`src/domain/secret_digest.py:13`) computes HMAC-SHA256 under
`SECRET_DIGEST_KEY`. The key is required at boot (`decisions/secrets_at_rest_cipher_and_digest.md`), and the graph
already carries it (`application_graph.py:139`, `:181`).

**Notices.** `UserNotificationService.notify_ui_notice` (`user_notification_service.py:237-246`) posts a locale string
literally. Every proactive delivery resolves its target through `resolve_channel` (`:146`).

**No WebAuthn code** exists in `src/`. The runtime is Python 3.11 (`Dockerfile:4`).

## 5. Goals and non-goals

**Goals:**
1. A revocable server-side session per browser, with silent refresh and lifetimes documented as ASVS V7.1.1 requires.
2. Passkey sign-in as the fast path.
3. A devices list and a kill switch, for every user.
4. A new-sign-in notice, for every user.
5. An opt-in facts PIN.
6. A full account lock that every entry point honours, triggered by the duress code or by five wrong PIN entries, so
   only for a user who set a PIN.

**Non-goals:**
- forcing biometrics;
- native apps;
- coercion resistance;
- an unlock path inside the product;
- logging out chat apps.

## 6. Platform facts for passkeys (checked 2026-11-22)

| # | Fact | Status | Source |
|---|---|---|---|
| F1 | `userVerification: "required"` cannot force biometrics. The UV flag does not say whether a PIN, a passcode or a biometric was used | Confirmed | github.com/w3c/webauthn/issues/1728; web.dev/articles/webauthn-user-verification |
| F2 | iCloud Keychain and Google Password Manager passkeys sync across the user's devices | Confirmed | support.apple.com/en-us/102195; developers.google.com/identity/passkeys/supported-environments |
| F3 | The BE flag marks a synced credential and is fixed at registration. `devicePubKey` was dropped from Level 3 | Confirmed | w3.org/TR/webauthn-3 §6.1; lists.w3.org/Archives/Public/public-webauthn/2024Aug/0058.html |
| F4 | iOS Home Screen web apps have cookies and storage separate from Safari | Confirmed | developer.apple.com/videos/play/wwdc2023/10120 |
| F5 | WebAuthn works in an iOS Home Screen web app and sees Safari's passkeys | Confirmed (2026-11-22, an iOS device check of a Home Screen web app on a public WebAuthn demo site; iOS shows the system passkey sheet, then user verification) | Real-device check. The check per entry point is part of the passkey PR (§11 step 2). Before it, only one unanswered forum report of double prompts (developer.apple.com/forums/thread/815784) |
| F6 | Safari dropped the user-gesture requirement for WebAuthn in iOS 17.4 / macOS 14.4 and added rate limiting instead | Confirmed (Apple engineer) | developer.apple.com/forums/thread/747036 |
| F7 | The RP ID must be the origin's effective domain or a registrable suffix of it. `*.run.app` is on the Public Suffix List | Confirmed | w3.org/TR/webauthn-3; publicsuffix.org/list |
| F8 | `webauthn` (py_webauthn, Duo Labs) 3.0.1 of 2026-09-25, BSD-3, Python 3.10+, built for relying parties; `fido2` (Yubico) 2.2.1 | Confirmed (PyPI) | pypi.org/project/webauthn; pypi.org/project/fido2 |

Two consequences:
- A synced passkey (F2) is not a device: one passkey signs on the iPhone, the iPad and the Mac. **The device is the
  session**, one per cookie jar. The devices list shows sessions, and "log out a device" revokes a session.
- A passkey proves that someone unlocked the user's keychain with Face ID or the passcode (F1). It is a convenience
  and a speed bump, not proof of the user.

## 7. Design

### 7.1 Device sessions

**The session record** (`DeviceSession`, collection `{prefix}auth_device_sessions_v1`, doc id a random `sid`):

| Field | Contents |
|---|---|
| `user_id`, `account_id` | The session's user and account |
| `label`, `platform_hint` | e.g. "iPhone — Home Screen (call)". From the User-Agent: untrusted, shown literally |
| `auth_kind`, `auth_at` | How and when the session was signed in (`google` or `passkey`) |
| `created_at`, `last_seen_at`, `expires_at` | `expires_at` = `created_at` + 30 days |
| `refresh_hash`, `prev_refresh_hash`, `rotated_at` | The rotating refresh secret |
| `passkey_at` | When a passkey assertion last proved the session (its passkey sign-in, or the step-up before a call, §7.3) |
| `facts_unlocked_until` | §7.7 |
| `revoked_at`, `revoke_reason` | Set by logout, the devices list, the kill switch or the account lock |
| `purge_after` | Firestore TTL field: `last_seen_at` + 30 days |

Every query is a single equality on `user_id`, so no composite index is needed. Every client passes `database=`.

**Tokens and lifetimes** (`DeviceSessionPolicy`, all in one domain file):

| Constant | Value | Meaning |
|---|---|---|
| `ACCESS_TTL` | 15 min | JWT access cookie, carrying the usual claims plus `sid`. Refreshed silently |
| `IDLE_TTL` | 14 days | No refresh for this long and the session ends (decided, §2) |
| `ABSOLUTE_TTL` | 30 days | The session ends whatever its activity |
| `ROTATION_GRACE` | 60 s | Two tabs refreshing at once (below) |
| `GATE_CACHE_TTL` | 30 s | The revocation latency bound |

`ACCESS_TOKEN_TTL` and `REFRESH_TOKEN_TTL` in `auth.py` stop governing the Cabinet and are deleted with the old
refresh path.

The cookies become `__Host-access` and `__Host-refresh`: `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/` (ASVS 4.0.3
V3.4.1–3.4.4). The refresh cookie's `max-age` runs to `expires_at`. The refresh secret is 32 random bytes, and the
record stores only its SHA-256.

**Refresh** (`POST /auth/refresh`, rewritten):
1. Find the session by the `sid` claim. An expired JWT is accepted for its `sid` only.
2. Require the session to be neither revoked nor past its idle or absolute end, and require `resume_session` to pass:
   active user, whitelisted email, existing account.
3. If the secret matches `refresh_hash`: mint a new access token, rotate the secret, and set `last_seen_at`.
4. If it matches `prev_refresh_hash` within `ROTATION_GRACE`: mint an access token only and set no new refresh
   cookie. Two tabs racing therefore end with the newer cookie.
5. If it matches `prev_refresh_hash` after the grace, or neither hash: someone replayed an old secret. Revoke the
   session (ASVS 4.0.3 V3.2.1 spirit).

**The gate.** One async cookie verifier checks the JWT and then `DeviceSessionService.is_live(sid)`. `auth_required`
and all eight direct readers of §4 call it; nothing else reads the access cookie, and a test fails on a read of it
anywhere else in `src/`. `is_live` answers from a per-instance cache of at most 30 s, and otherwise reads the record.
A token without `sid` is refused, so old tokens die at deploy. The cache is filled after an awaited read and cleared
by a revocation on the same instance, so it carries the version counter that `src/services/CLAUDE.md` prescribes. The
cost is one read per session per 30 s per instance.

**Logout** revokes the record, then clears the cookies.

### 7.2 Lifetimes against ASVS and NIST (the V7.1.1 record)

ASVS 5.0 V7.1.1, V7.3.1 and V7.3.2 (L2) require the idle and absolute lifetimes to be documented and justified
against NIST (github.com/OWASP/ASVS, `5.0/en/0x16-V7-Session-Management.md`). NIST 800-63B-4 at AAL2 says: an overall
timeout SHALL exist and SHOULD be no more than 24h, and the inactivity timeout SHOULD be no more than 1h
(pages.nist.gov/800-63-4/sp800-63b/aal). ASVS 4.0.3 V3.3.2 named 12h or 30 min for L2.

Our 30 days and 14 days deviate on purpose. The justification:
- Each account is one person, on their own devices, each behind a device lock.
- A session is revocable server-side in ≤30 s.
- Every new session is announced.
- Where the user sets one, the facts sit behind a second factor (the facts PIN), with a limit of five wrong
  entries.
- For such a user, a full lock is one chat message away.
- The cost of the NIST numbers is the defect this RFC fixes: a Google login per entry point per day.

ASVS 5.0 V7.5.2 asks for re-authentication before sessions are terminated. We deviate here too: logging out is
reduce-only, so the worst abuse is a denial of service. V7.4.3 (offer to terminate other sessions after a factor
change) is met by an offer after a PIN change or reset.

### 7.3 Passkey fast sign-in

**Library.** `webauthn` (py_webauthn, F8) sits behind `SignerVerifierPort`. The port has a second implementation in
the optional native approver (§12), and it is the boundary to a third-party crypto library. Writing CBOR and COSE
verification in-house was rejected: it is security code nobody should own for one user.

**RP ID and origin** come from the host of `OAUTH_REDIRECT_URI` (for `https://cabinet.example.com/auth/callback`, the
RP ID is `cabinet.example.com`). That host is already deploy-correct and localhost-guarded (`auth.py:169-183`), so no
new deploy key is needed. Options: `residentKey: "required"`, `userVerification: "required"`, `attestation: "none"`,
and `user.id` = the user_id bytes.

**The passkey record** (`Passkey`, collection `{prefix}auth_passkeys_v1`, doc id = the SHA-256 of the credential id):

| Field | Contents |
|---|---|
| `user_id`, `account_id` | The passkey's user and account |
| `kind` | `webauthn` |
| `public_key`, `algorithm`, `sign_count` | The verifier's input |
| `label`, `platform_hint` | Shown in the devices list |
| `backup_eligible` | The BE flag (F3) |
| Times | `created_at`, `last_used_at`, `revoked_at` |

Challenges live in `EphemeralStore` (`set` with a 300 s TTL, then `get_and_delete`), keyed by a nonce held in a
short-lived httponly cookie.

**Flows.**
- **Sign-in page.** `/auth/login?next=…` serves a page with "Sign in with Face ID / passkey" first and "Sign in with
  Google" second. A successful passkey assertion runs `resume_session`, creates a session (`auth_kind = passkey`) and
  redirects to `next`. A browser with no passkey falls through to Google.
- **Enrolment.** Any live session can add a passkey: "Add Face ID for faster sign-in" in the devices list, also
  offered once after a Google sign-in. There is no hold: the paranoid mode is out of scope (§2). Adding one sends a
  chat notice.
- **Lelik call start.** Every call start requires a fresh passkey assertion (about 1 s; it guards an unlocked phone
  in another person's hands; the passcode is accepted as fallback, F1). A 5-minute grace after the last assertion
  means a redial after a dropped call does not ask again.

### 7.4 Frontend: refresh before login

One shared `session.js` is served by an explicit route, since the main app has no static folder
(`src/web/CLAUDE.md`). `cabinet.html`, `call.html` and `call_modes.html` all use it.

`authFetch()` keeps one refresh in flight per page. On a 401 it calls `/auth/refresh` and retries once. Only when that
fails does it go to `/auth/login?next=<this page>`, and there the passkey path comes first.

The call pages run the same check on load and on `visibilitychange`, so a Home Screen app brought back from the
background refreshes before the user taps Call. `/auth/me` adds `expires_at` so the page can refresh early.

### 7.5 The devices list and the new-sign-in notice

**Cabinet → Devices** lists each session:
- label, platform hint, sign-in kind, created, last used, and "this device";
- "Log out" per row and "Log out all other devices";
- below the sessions, the passkeys, each with "Remove".

For `alex@example.com`, signed in on a phone and a laptop, the list shows two rows: "iPhone — Home Screen (call)"
(passkey, this device) and "Mac — Chrome" (Google).

`$devices` in chat lists the same sessions, and `$devices off` is the kill switch (§7.6). This meets ASVS 5.0 V7.5.2
(4.0.3 V3.3.4) apart from the re-authentication deviation in §7.2.

**Notice on every new session.** It goes through `notify_ui_notice`: "New sign-in: {platform_hint}, {time}, by
{Google|passkey}. Not you? `$devices off`." It is sent literally, because the hint is untrusted text. It is
code-written UI text and never claims the system's voice. Adding or removing a passkey sends a notice of its own.

### 7.6 Kill switch: `$devices off` and recovery

`revoke_all(user_id)` revokes, in one Firestore transaction, every session of the user, every passkey and any pending
PIN reset. Each sign-in and refresh reads its own record inside a transaction, so a race with the switch either sees
it or retries after it. The kill switch then revokes every MCP refresh token of the user, the same step as the
account lock's step 3 (§7.8), so an MCP client on the lost device loses its connector grant too.

| Path | Who can use it | Latency |
|---|---|---|
| `$devices off` in Slack or Telegram | The user's chat identity | ≤30 s (gate cache); same instance at once |
| Cabinet → Devices → "Log out all devices" | Any live session | ≤30 s |
| "Log out all devices" on the page after a Google sign-in | The account holder with their Google login and no device left | ≤30 s |

After the switch, the next access needs Google and then passkey re-enrolment. Google is the root of trust, so a
thief who also holds the account holder's Google account can sign in again. The new-sign-in notice shows it, and the
answer is the account lock (§7.8), through the duress code for a user who set a PIN.

The switch does not end a voice call in progress (≤ `call_ttl_s`, `voice_web_call_app.py:76`), and an MCP access
token already issued lives to its expiry (the MCP access TTL, `auth.py:103`).

**Lost-phone checklist**, shown in Devices and in the `$devices off` reply:
1. `$devices off`.
2. Remove the phone from the Apple or Google account, which stops synced passkeys (F2).
3. End the phone's Telegram and Slack sessions in those apps.
4. If the phone is also signed in to the Google account and a facts PIN is set, send the duress code in chat.

### 7.7 The facts PIN

The PIN is opt-in per user. Without a PIN record, the facts tab works as today, the facts routes are not guarded,
and nothing can lock that user through the PIN. Everything below applies to a user who set one.

**Record** (`FactsPin`, collection `{prefix}facts_pin_v1`, doc id = `user_id`):

| Field | Contents |
|---|---|
| `pin_digest` | `keyed_digest(key, "facts_pin", f"{user_id}:{code}")` |
| `duress_digest` | `keyed_digest(key, "duress_pin", f"{user_id}:{code}")` |
| `wrong_in_row` | The attempt counter |
| `reset_requested_at`, `reset_due_at` | A pending reset |
| `updated_at` | Last change |

Comparison uses `hmac.compare_digest`. A keyed digest is the right control for a 4-digit code:
- With only Firestore read, the 10⁴ codes cannot be tested offline without `SECRET_DIGEST_KEY`, which lives in
  Secret Manager.
- With the key too, any hash falls in seconds to minutes, because a slow KDF over 10⁴ inputs still finishes in
  minutes.

So the real defence is the server-side attempt limit, not hash cost.

**Rules** (`FactsPinPolicy`, domain):
- **Codes.** Exactly 4 digits. Refused as trivial:
  - all one digit;
  - ascending or descending runs, including runs that wrap from 9 to 0;
  - a repeated pair;
  - a short denylist of the most common codes.
  The duress code obeys the same rules and must differ from the PIN.
- **Setting and changing.** Done in the Cabinet settings. The PIN and the duress code are set together, each entered
  twice. A change needs the current PIN.
- **Unlocking.** The facts tab asks for the PIN when it opens. A correct PIN sets `facts_unlocked_until = now +
  UNLOCK_TTL` (15 min) on the session record. Leaving the tab or closing the page relocks it (`sendBeacon` to
  `/api/user/facts-pin/relock`, best effort). The server TTL is the guarantee.
- **Which routes.** A `facts_unlock_required` decorator guards the eight facts routes of `user_cabinet_app.py:895-1098`
  (`/api/user/facts`, `/fact-domains`, `/facts/browse`, `/facts/recently-closed`, `/facts/review-queue`,
  `/facts/review-answers`, `/facts/search`, `/facts/<id>/invalidate`). For a user without a PIN it passes.

**Attempts** (one counter per user, across the Cabinet and chat). `FactsPinService.check(user_id, code, surface)` runs
one Firestore transaction:
- **The duress code** fires the account lock (§7.8).
- **The real PIN** sets `wrong_in_row = 0` and returns OK.
- **Anything else** increments `wrong_in_row`. At 5 it fires the account lock (`reason = pin_attempts`).

Because the read, the decision and the increment share one transaction, parallel requests cannot pass 5: a
concurrent attempt conflicts and retries against the new count. A transactional increment, not a client-side check,
is required. The UI shows "Wrong code" and the attempts left.

Typos can lock the account, and only an operator can unlock it: an accepted risk.

**Reset when forgotten.**
1. A Google sign-in less than 5 minutes old (`auth_kind = google`, `auth_at`) asks "Forgot PIN".
2. A reset is pending for 24h, with a chat notice: "Facts PIN reset requested on {platform_hint}. It takes effect at
   {time}. Not you? `$pin cancel`".
3. After that time, the next facts tab visit asks for a new PIN and a new duress code. The old digests and the counter
   are cleared, and a second notice follows.

The kill switch and the account lock cancel a pending reset. The reset is the weak point: a thief who has the account
holder's Google account on the phone can start one, and only the 24h window and the notice stand against it. That is
why the duress code exists in chat.

### 7.8 The account lock

**Trigger:** the duress code (Cabinet or chat), or the fifth wrong PIN in a row. Both exist only for a user who set a
PIN.

**Flag:** the existing `UserProfile.is_active = false` (§4), plus one new field, `inactive_reason` (`duress` or
`pin_attempts`). The reason is for the operator reading Firestore. The flag is written in the same transaction as
the PIN record. Reusing the flag means `resume_session` already refuses refresh. The checks below close the other
entry points. Facts and the account document are untouched, and no data is deleted.

**`AccountLockService.lock(user_id, reason)`**, in order:
1. The transaction above.
2. `revoke_all` (§7.6): sessions, passkeys and the pending reset.
3. Revoke every MCP refresh token of the user, with a new repository method over `mcp_refresh_tokens`.
4. Post the lock notice to **every** stored channel (primary and last active, Slack and Telegram): "Your account is
   locked ({reason}). Only the administrator can unlock it." This is the one notice that bypasses the inactive check
   in `resolve_channel`.

**Unlock:** an operator flips the flag by hand in Firestore (`is_active = true`, clearing `inactive_reason` and
`wrong_in_row`). No route, command or script in the product unlocks.

**Entry points** (one check per entry point class; a test lists them):

| Entry point | Where the check goes | Effect when locked | Latency |
|---|---|---|---|
| Google sign-in | `AuthenticationService.handle_oauth_callback` after the user is resolved (`authentication_service.py:174-233`) | The "account locked" page, no session | At once |
| Passkey sign-in, refresh | `resume_session` (`:270`, exists) | 401; the page shows "account locked" | At once |
| Cabinet and call pages, and the eight direct cookie readers (the gate) | Sessions revoked by step 2; the one cookie verifier (§7.1, `cabinet_auth.py:8-33` today) | 401, then "account locked" | ≤30 s |
| Chat (Slack, Telegram; every message, queued Slack events and `$` commands) | `IAMService.authorize` Branch 1 (`iam_service.py:169-203`), beside the whitelist check | A reply "Your account is locked…", no turn, nothing stored | At once (fresh user read per message) |
| `/worker` tasks (reminders, email review, consolidation, agent executions, every task type) | `WorkerHandler._dispatch` (`worker_handler.py:287-299`). It reads `user_id` or `context.user_id` and answers 200 `skipped: user_inactive`, so Cloud Tasks does not retry | Task dropped | At once |
| Proactive delivery from any background path, including a fan-out task or a running deep-research job | `UserNotificationService.resolve_channel` (`user_notification_service.py:146`) returns None for an inactive user | Nothing posted | At once |
| Drive indexer | `src/web/indexer_worker_app.py` `/worker` (payloads carry `user_id`, `:15`) | `drive_sync` skipped | At once |
| Voice calls, web and phone | `/voice/session-config` (`voice_control_plane_app.py:211`), after the ticket resolves the user (`:229-237`); both call kinds pass here | Call refused before any token is minted | At once |
| Lelik memory delegation mid-call | `/voice/delegate` (`voice_control_plane_app.py:509`, user at `:515`) | Delegation refused; the call itself runs to its end | At once |
| MCP refresh and drive-link grants | `MCPAuthorizationService._allow_reason` (`mcp_authorization_service.py:500`), used by rotation (`:370`) and `grant_allowed` (`:478`) | `invalid_grant` | At once |
| MCP tool calls on an access token already issued | The authenticated-user step of each tool (`mcp_setup.py:290-303` for `get_user_context`; the drive connector tools' equivalent) | Tool error "account locked" | At once (otherwise ≤1h, the MCP access TTL) |

Every check reads the stored user fresh. None may use the agent factory's cached profile
(`UserAgentFactory._cache`, `user_agent_factory.py:250`).

A fan-out task with no user in its payload (for example a scheduler tick that finds due reminders) is not refused as
a whole. Its per-user work is caught either by the per-user task it enqueues or by `resolve_channel`.

Not covered, by decision: a voice call already connected; an LLM call already in flight; and the Cloud Tasks bodies
already queued, which are dropped when they arrive.

### 7.9 The duress code in chat

The code is a `$` command: `$<digits>`. `handle_command` gets a new branch, first in order: a command made of digits
only goes to `FactsPinService.check(user_id, code, surface="chat")`.
- **The duress code** locks the account, and the reply is the lock notice.
- **The real PIN** resets the counter. The reply says that the PIN does nothing in chat, that it is now in this
  chat's history, and that it should be changed in the Cabinet.
- **Any other digits** count as a wrong attempt. The reply is "Wrong code (n left)"; at 5 the account locks.
- **A user without a PIN** gets a fixed reply that no facts PIN is set. Nothing is counted.

It needs no interception of its own: `$` commands never reach session history, the consolidator or a model (§4,
paths cited). Two rules come with it:
- **The digits are never logged** and never echoed back. Tests pin that the branch logs only the outcome, and that
  neither adapter logs the text of a `$` message.
- **The message stays visible in the chat app.** In Slack it also stays in the channel the history source reads, and
  that source filters it out (`channel_history.py:82`).

### 7.10 Placement (checked against `tests/unit/test_req_arch_01_hexagonal_isolation.py`)

| Layer | What goes there |
|---|---|
| `domain/` | `device_session.py` (`DeviceSession`), `device_session_policy.py` (`DeviceSessionPolicy`: TTLs, `is_live`, `access_expiry`), `passkey.py` (`Passkey`), `facts_pin.py` (`FactsPin`), `facts_pin_policy.py` (`FactsPinPolicy`: `is_trivial`, `UNLOCK_TTL`, `MAX_WRONG`, `RESET_DELAY`, `PinOutcome`). `UserProfile` gains `inactive_reason`. Stdlib and pydantic only |
| `ports/` | `device_session_repository.py` (sessions and passkeys, `revoke_all` in one transaction), `facts_pin_repository.py` (the PIN transaction, which also writes the user flag), `signer_verifier.py` (§7.3) |
| `adapters/` | `firestore_device_session_repo.py`, `firestore_facts_pin_repo.py` (each with a store kit, `tests/CLAUDE.md`), `webauthn_signer_verifier.py` (the only importer of `webauthn`). Collection names in `config/environment.py` |
| `services/` | `device_session_service.py`, `facts_pin_service.py`, `account_lock_service.py`. Cross-service dependencies are injected, with `TYPE_CHECKING` imports (REQ-ARCH-22). The checks inside `AuthenticationService`, `IAMService`, `MCPAuthorizationService` and `UserNotificationService` read `user.is_active` from objects they already load |
| `web/` | `oauth_app.py` (callback, refresh, logout, sign-in page), `cabinet_auth.py` (the one cookie verifier with the `sid` check, used by the gate and the eight readers of §4), `device_auth_app.py` (passkey and devices routes, PIN routes, `facts_unlock_required`), the voice and indexer checks |
| `handlers/` | `$devices`, `$pin cancel` and the digits branch in `ConversationHandler.handle_command`, through new `CommandServices` fields. The `WorkerHandler._dispatch` check |
| `composition/` | Wiring only |
| `locales/` | Every notice and page string, in four languages |

## 8. Residual risk

What a stolen device that is unlocked, or whose passcode is known, still reaches:
- **Until the kill switch (≤30 s after it):** the Cabinet and Lelik calls; the facts tab too when the user set no
  PIN. Lelik's context carries ordinary facts (the biographical cache), and Lelik can reach memory by delegation.
  Synced passkeys let the holder open new sessions as well, each announced in chat. A call already connected runs to
  its end, and an MCP access token already issued lives to its expiry (≤1h).
- **Until the chat app sessions on the device are ended in those apps, or the account is locked:** the bot in
  Telegram and Slack, with its full memory. Neither the facts PIN nor the kill switch covers chat.
- **The facts tab, with a PIN set:** five guesses. After the fifth, the whole account locks.
- **With the account holder's Google account on the device as well:** new Google sign-ins, each announced in chat,
  and a PIN reset after 24h unless it is cancelled. The answer is the duress code in chat. A user without a PIN has
  the kill switch, repeated after each announced sign-in, and the recovery of the Google account itself.

## 9. Alternatives rejected

- **A facts-only lock at the facts read choke point** (an earlier draft). It meant guarding about 45 read call sites
  in 15 modules (`FirestoreFactRepository` at `service_container.py:311`, the person-card store, and a maintenance
  set exempted by hand), and it still left chat and Lelik answering. The account lock was chosen instead.
- **Passkey-first (mandatory) sign-in with a 24h enrolment hold.** That is the paranoid design: it puts a daily cost
  on every user for a threat that is out of scope (§2).
- **Native apps now.** Maintenance cost on two platforms for a gain the browser covers at the baseline; see §12.
- **Cloudflare Access in front of the service.** It adds a second identity layer and a second session that knows
  nothing about our devices list or the lock, and the relay and webhooks would need bypass rules.
- **Long JWTs plus a revocation list.** It is still a per-request read. The session record is that read, and it also
  gives the devices list.
- **A per-user epoch as the kill switch.** It cannot revoke one device, and it needs a second read in the gate.
- **A slow KDF for the PIN.** It adds minutes against an attacker who already holds the key (§7.7).
- **A timed lockout after five wrong entries.** Rejected by decision (§2): a full lock with a manual unlock.

## 10. Failure modes

| Failure | Effect | Answer |
|---|---|---|
| A user with a PIN mistypes it five times | Account locked | An operator unlocks in Firestore (accepted risk) |
| WebAuthn fails in one entry point's Home Screen app despite F5 | No Face ID path there | Google still works; the per-entry-point check runs before the passkey page becomes the default (§11 step 2) |
| Firestore read fails in the gate or the PIN check | 401 / 503 | Fail closed. Auth files keep 100 % line coverage (`tests/CLAUDE.md`) |
| A new route reads the session cookie itself | A revoked device passes there | The cookie-reader test (§7.1) fails the build |
| A check is forgotten on a new entry point | A locked user reaches it | The entry-point table becomes a test: each class's check is asserted with a locked user in `prod_graph` |
| The lock notice finds no channel | The user learns from the Cabinet page | Logged |
| RP ID changes with a domain move | Every passkey dead (F7) | Re-enrol; stated in the deploy README |

## 11. Rollout

There is no data migration. Old tokens carry no `sid` and die at deploy, which costs one Google login per entry
point.

1. **Sessions.** Records, rotation, the gate and the one cookie verifier for the eight direct readers (with its
   test), logout, `session.js`, the devices list, `$devices` / `$devices off` and the new-sign-in notice.
2. **Passkeys.** `SignerVerifierPort` and its adapter, the passkey records (`revoke_all` covers them), enrolment, the
   passkey at Lelik call start with its 5-minute grace. F5 is confirmed on a real device; this PR also checks each
   entry point (iPhone Safari, each Home Screen app, Chrome on macOS and Android) and carries the results. Then the
   passkey-first sign-in page.
3. **The PIN and the account lock.** The opt-in facts PIN, the duress code, the five-strikes lock, every entry-point
   check (§7.8) and their test, the `$<digits>` chat branch, `$pin cancel`, and the MCP grant revocation in the kill
   switch.
4. **No rollback lever.** Google stays a working path at every step, so no lockout needs a switch.
5. **Docs in the same PRs:**
   - a decision record that supersedes `cabinet_session_24h_cap.md` and holds the §7.2 justification;
   - the `src/web/CLAUDE.md` "Cabinet session slides" trap replaced;
   - a root CLAUDE.md map row, and a repo trap line: "an inactive user is refused at every entry point; a new entry
     point adds the check";
   - the TTL policy on `purge_after` in `docs/07_deployment/README.md`, a one-time deploy step;
   - new routes pinned in `main_boots_offline_url_map.txt`, and the anonymous ones (sign-in page, passkey options and
     verify, `session.js`) in `PUBLIC` (`tests/unit/web/test_every_route_refuses_anonymous.py`) with reasons.

## 12. Later, optional

**Passkey step-up.** Any route can require a fresh assertion: a decorator that checks `auth_at` on the session is
under N minutes old and otherwise runs the passkey sheet from `session.js`. The facts tab could take it in place of
the PIN, or on top of it. The session record and `SignerVerifierPort` already carry everything it needs.

**Native approver app.** A Kotlin Multiplatform app on one phone holds a biometric-only key, bound to the current
biometric set. It is a second `SignerVerifierPort` implementation (ECDSA over the challenge and `sid`), and its key is
a `Passkey`-like record with `kind = approver_app`. It approves a web session by signing an `EphemeralStore`
challenge bound to that `sid`. The session, the gate, the devices list and the lock do not change.

## 13. Decisions

The questions this design left open are closed (2026-11-22):
1. **A passkey check at every Lelik call start**, with a 5-minute grace, so a redial after a dropped call does not
   ask again (§7.3). Built in step 2, after the per-entry-point check.
2. **PIN unlock window:** 15 minutes, or until the tab closes (§7.7).
3. **The real PIN sent in chat:** the counter resets and the reply warns that the PIN should be changed (§7.9).
4. **`$devices off` revokes the MCP connector grants** as well as the sessions and passkeys (§7.6). A phone's MCP
   client reaches the facts through the connector; the account lock revokes them in any case.
5. **The lock is per user** (`UserProfile.is_active`), not per billing account: other members of the same account
   keep working. This assumes no account member can read another member's facts.
