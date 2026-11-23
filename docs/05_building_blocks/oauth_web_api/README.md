<!-- living-doc
covers:
  src/web/oauth_app.py
  src/services/session_service.py
  src/services/auth_provider_registry.py
  src/config/auth.py
  src/adapters/firebase_auth_adapter.py
  src/ports/auth_port.py
  src/domain/auth.py
  src/domain/safe_redirect.py
  src/domain/device_label.py
-->
# OAuth Web API

## Purpose

This block is how a person signs in to the web side of the system (the Cabinet, the voice call page, the MCP consent
page) and how that sign-in is kept. It does four things: it runs the Google sign-in through the `/auth/*` routes, it
turns a successful sign-in into a revocable device session and its two cookies, it renews and revokes that session, and it starts and
finishes the connect of integrations (mail, tasks, drive) under the signed-in session.

It is the web counterpart of the Slack and Telegram adapters: those identify a person by platform id, this one by a
Google identity, and both land on the same `UserProfile` and `BillingAccount`. A Google identity that matches no existing user (by identity or verified email) gets a new account with the person as
owner ("Master Account First": the account is created before the user). Who may sign in at all is
decided by the access whitelist, not by Google.

What it is not: it does not store the session record itself (`DeviceSessionService` over
`{prefix}auth_device_sessions_v1` does, CABINET_DEVICE_SESSIONS_RFC §7.1), it does not guard the Cabinet routes
(`src/web/cabinet_auth.py::make_auth_required` does, through the one verifier this block also uses), and the
credential store and token refresh of the connected integrations belong to the integration blocks. The multi-tenant
data model behind the user and account is in [oauth_multi_tenant](../oauth_multi_tenant/README.md).

## Owned data

The block owns no Firestore collection of its own. What it writes and holds:

- **Session cookies**, set by `/auth/callback` and `/auth/refresh` (`src/web/cabinet_auth.py::set_session_cookies`):
  `__Host-access` (the access JWT) and `__Host-refresh` (the session's current refresh secret, 32 random bytes; the
  record stores only its SHA-256). Both Secure, HttpOnly, SameSite=Lax, Path=/, and both live until the session's
  absolute end: the JWT inside the access cookie expires after 15 minutes, but the cookie must outlive it, because
  refresh and logout take the `sid` from that expired JWT. The lifetimes are `DeviceSessionPolicy`'s, not
  configuration (`decisions/cabinet_device_session_lifetimes.md`).
- **Sign-in cookies**, set by `/auth/login`, short-lived: `oauth_state` (the CSRF value), `auth_next` (where to return)
  and `invite_code` (a team invite to consume after sign-in). The callback deletes only `oauth_state`; the other two
  expire by themselves.
- **Token claims** (`SessionService.create_device_access_token`): identity, `role` (from the account's `iam_policy`,
  `viewer` if absent), `tier`, `type: "access"` and the session's `sid`. HS256, signed with
  `AuthConfig.oauth_session_secret` (`OAUTH_SESSION_SECRET`). A token without `sid` is refused.
- **The session's label and platform hint** (`src/domain/device_label.py::describe_device`): words picked from a fixed
  vocabulary by the User-Agent ("iPhone — Home Screen (call)", "Mac — Chrome"); nothing of the User-Agent is copied.
- **Written on the user and account** through `AuthenticationService`: a sign-in of a known identity rewrites only `auth_metadata`;
  the by-email attach and `link_oauth_identity` rewrite only `external_user_id` and `auth_metadata` (the whole map). Both
  are `update_user_fields`, so a concurrent change of another field survives; a first sign-in
  creates a `BillingAccount` (tier FREE, the person as `owner` in `iam_policy`) and then the `UserProfile`;
  `external_user_id` is `"{provider}|{sub}"`. `AuthenticationService.resume_session` writes nothing.
- **The connect flow record** (`oauth_flow:<digest>`, TTL `src/services/oauth_connect_service.py::_FLOW_TTL_S`) is written by
  `src/services/oauth_connect_service.py::OAuthConnectService.start` and consumed by `complete`; the credential it ends
  in is described in [`credential_store_cutover.md`](../../04_solution_strategy/decisions/credential_store_cutover.md).

## Flows

### Sign in

1. `GET /auth/login` (`src/web/oauth_app.py::create_oauth_blueprint`, route `oauth_login`) takes `provider`, `next`
   and `code`. `next` goes through `src/domain/safe_redirect.py::safe_return_path` (same-site relative path only, else
   `/cabinet`). It generates the state, builds Google's consent URL with
   `AuthPort.get_authorization_url` (`FirebaseAuthAdapter`), sets the three cookies and
   redirects. An unknown provider or any failure is a 500.
2. Google returns to `GET /auth/callback` (`oauth_callback`). A missing `code` is 400; a state that does not equal the
   `oauth_state` cookie is 400.
3. `AuthenticationService.handle_oauth_callback` exchanges the code, verifies the ID token
   (`FirebaseAuthAdapter.verify_token`: the Firebase Admin check first, then Google's OIDC keys with the OAuth client as
   audience), and runs the whitelist gate `_require_allowed_email` before any user is read or written. It then finds the
   user by `external_user_id`, else by email (the existing user gets the Google identity attached), else registers a new
   user and account (`register_new_user`).
4. A pending `invite_code` cookie is consumed with `InviteCodeService.consume_team_invite`; a failure is logged and does
   not stop the sign-in.
5. The route starts a device session (`DeviceSessionService.start`, which also sends the new-sign-in notice), sets the
   two cookies, deletes `oauth_state` and **redirects** to `auth_next` (validated again with `safe_return_path`). The
   response is a redirect, not JSON.
6. A rejected identity (`AccessDeniedError`) returns a fixed 403 HTML page and sets no session cookie. Any other failure
   is a 500 with a generic body; the detail goes to the log only.

### Renew

`POST /auth/refresh` (`refresh_token_endpoint`) takes the user and `sid` from the access cookie, its JWT verified for
signature and type but accepted expired (`CabinetSessionVerifier.presented_session`), and the secret from the refresh
cookie; either missing is 401 with both cookies cleared. `AuthenticationService.resume_session` then loads the stored
user and account and checks the whitelist again (absent or inactive user, a missing or de-listed email, an absent
account: 401 `Session no longer valid`, cookies cleared; a store error: 500, no cookie change). Then
`DeviceSessionService.refresh` decides on the record: ROTATE sets both cookies, GRACE (the previous secret within 60 s
of a rotation: a second tab) sets the access cookie only, REPLAY (an older secret: the session is revoked) and DEAD
(revoked, idle 14 days or 30 days old) are 401 with both cookies cleared. The new access token is built from the stored
user and account, never from the old token's claims. The body is `{"success": true, "expires_at": <exp>}`; the token
is never in it.

### Identity and sign-out

- `GET /auth/me` returns user and account fields of the live session's token (cookie or Bearer) and `expires_at`, the
  access token's expiry in Unix seconds, so a page can refresh early. `POST /auth/logout` revokes the session named by
  the access cookie (expired or not), then clears both cookies; a failed revocation still clears them and answers 500.
- `POST /auth/link-oauth` (`link_oauth_to_existing_user`) attaches a Google identity to the signed-in user: it needs a
  live session, a JSON `code` and a `state` equal to the `oauth_state` cookie, then calls
  `AuthenticationService.link_oauth_identity`, which runs the same whitelist gate on the identity being linked. Errors:
  401 without a token, or (after the code and state checks, so a bad state with a bad token is 400) with a token that
  does not verify or a session that is not live, 403 for a rejected identity, 400 for a state or code problem and an
  identity already linked to another user (`ValueError`), 500 otherwise.

### Connect an integration

1. `GET /auth/connect/<kind>` (`connect_start`) needs the Cabinet session cookie (none: redirect to
   `/auth/login?next=/cabinet`). An unknown or not offered kind is 404; a kind whose realm has no OAuth client on this
   deployment is 501; the service not wired is 501. `?credential=<id>` starts a reconnect, `?intent=add` an added
   account. `OAuthConnectService.start` stores the flow (only a keyed digest of the state, the PKCE verifier sealed) and
   the route redirects to the provider. A reconnect of a credential the user does not hold goes back to the Cabinet with
   `connect_error=state`.
2. `GET /auth/connect/callback` (`connect_callback`) is the one callback of every integration and realm
   (`AuthConfig.oauth_connect_redirect_uri`). A provider refusal, a missing state or a missing code return to the Cabinet
   with `connect_error` set to `denied`, `state` or `no_code`. No session, or one without an `account_id`, goes to the
   sign-in. `OAuthConnectService.complete` consumes the flow, exchanges the code, checks scopes and the account, stores
   the credential and runs the integration's activation. The user returns to the flow's return path with
   `?connected=<kind>` or `?connect_error=<status>&kind=<kind>`.

## Invariants and traps

- **The whitelist gates every sign-in, link and renewal.** `AuthenticationService._require_allowed_email` takes the email
  from the verified ID token (never userinfo), requires `email_verified is True`, and runs before any user lookup.
  `resume_session` re-checks the last signed-in email (`auth_metadata["email"]`), so removing an address stops renewal;
  the access token already issued lives out its TTL. The rejection log line text is matched by an alert policy; keep it.
  Tests: `tests/unit/web/test_oauth_app_access_denied.py`, `test_oauth_app_refresh_whitelist.py`. Why:
  [`cabinet_oauth_whitelist_gate.md`](../../04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md).
- **Redirect targets are always checked.** `safe_return_path` is applied at `/auth/login`, at the callback and on the
  connect return path; the callback does not trust the `auth_next` cookie
  (`tests/unit/web/test_oauth_app_redirect_target.py`).
- **One reader of the access cookie.** Every route that asks who is signed in calls
  `src/web/cabinet_auth.py::CabinetSessionVerifier` (here: `_session`, `/auth/me`, `/auth/link-oauth`, refresh and
  logout); `tests/unit/web/test_access_cookie_one_reader.py` fails on a read anywhere else, and
  `test_revoked_session_refused_everywhere.py` shows each reader refusing a revoked session.
- **Role and tier come from the stored account**, at sign-in and at refresh; the old token only names the user and
  the session (`tests/unit/web/test_oauth_app_device_sessions.py`).
- **Revocation is server-side and bounded.** Logout, a replayed secret (and later the devices list and the kill switch)
  revoke the record; the gate's per-instance cache answers for at most 30 s. A whitelist removal stops the next refresh,
  so an issued access token lives at most its 15 minutes.
- **The CSRF check is the `oauth_state` cookie equal to the `state` value** (query on the callback, JSON body on link-oauth), nothing stored server-side; the connect flow, by contrast, is
  keyed and single use.
- **Startup refuses unsafe configuration.** `AuthConfig.validate` (called from `src/composition/boot_setup.py`) rejects a
  too-short session secret, the public dev placeholder when `K_SERVICE` is set, a localhost `OAUTH_REDIRECT_URI` on
  Cloud Run, and (with Firebase as provider) a missing Firebase project, web API key or Google OAuth client id/secret.  The deploy-side key for the redirect URI: `docs/07_deployment/README.md`.
- **Both callbacks must be registered at the provider.** `OAUTH_REDIRECT_URI` (sign-in) and
  `OAUTH_CONNECT_REDIRECT_URI` (connect) are separate callbacks and both must match the Google OAuth client (the
  connect one also the Microsoft app registration); a mismatch fails at the provider, not here.
- **`/auth/connect/*` reads only the cookie**, not a Bearer header. The callback also needs an `account_id` in it: a
  credential is never saved under a guessed tenant.
- **This blueprint sets no CORS headers**; nothing in `src/` adds them for these routes.
- **Every route is probed anonymously**: `tests/unit/web/test_every_route_refuses_anonymous.py` walks the URL map and
  allows an anonymous answer only for the routes it lists with a reason (`/auth/login`, `/auth/logout`).

## Links

- [oauth_multi_tenant](../oauth_multi_tenant/README.md): user, account and IAM model; [user_cabinet](../user_cabinet/README.md): the portal behind the session.
- [`cabinet_oauth_whitelist_gate.md`](../../04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md),
  [`cabinet_device_session_lifetimes.md`](../../04_solution_strategy/decisions/cabinet_device_session_lifetimes.md)
  (CABINET_DEVICE_SESSIONS_RFC).
- [`MULTI_DRIVE_RFC.md`](../../10_rfcs/MULTI_DRIVE_RFC.md): the connect flow and its one callback.

## Where to start reading

- `src/web/oauth_app.py::create_oauth_blueprint`: all routes.
- `src/services/authentication_service.py::AuthenticationService.handle_oauth_callback`, `resume_session`.
- `src/web/cabinet_auth.py::CabinetSessionVerifier`: the one session check and the cookies.
- `src/services/session_service.py::SessionService`: token claims and verification.
- `src/services/device_session_service.py::DeviceSessionService`: start, refresh, revoke, `is_live`.
- `src/adapters/firebase_auth_adapter.py::FirebaseAuthAdapter`: Google code exchange and ID-token verification.
- `src/services/oauth_connect_service.py::OAuthConnectService`: the connect flow.
- `src/config/auth.py::AuthConfig`: the session secret, redirect URIs, MCP TTLs and the startup checks.
