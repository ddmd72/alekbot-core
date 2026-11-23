# Web — traps

- **The main Quart app has no static folder:** `/static/*` 404s; a new asset gets an explicit route (see `voice_web_call_app.py`).
- **Every new route is pinned twice:** in `tests/unit/main_boots_offline_url_map.txt` (sorted; the offline-boot and
  Telegram-failure tests read it) and, if anonymous by design, in `PUBLIC` of `test_every_route_refuses_anonymous.py`.
  The boot tests are `slow`: a local run skips them, so a missing pin first shows up red in CI.
- **Who is signed in is asked of `CabinetSessionVerifier` only** (`cabinet_auth.py`): it checks the JWT's `sid` against
  the device-session record, so a route that reads the `__Host-access` cookie (or a Bearer token) and verifies it itself
  lets a revoked device in. `test_access_cookie_one_reader.py` fails the build on such a read. The access cookie outlives
  its 15-minute JWT on purpose: refresh and logout take the sid from the expired token
  (`decisions/cabinet_device_session_lifetimes.md`).
- **The passkey RP ID is the host of `OAUTH_REDIRECT_URI`** (`identity_setup.passkey_relying_party`): a passkey is
  bound to it, so moving the sign-in to another domain kills every passkey (users sign in with Google and enrol again).
- **A passkey ceremony route behind the gate never answers 401 to a refused assertion** (`device_auth_app.py`):
  `authFetch` answers a 401 by refreshing and re-sending, and the re-sent nonce is already spent. Those refusals are 400.
- **Sandbox hard stop:** remove the caller's `roles/run.invoker` binding on the render service; never empty
  `SANDBOX_RENDER_CALLERS_DEV` (an empty caller list refuses the new revision, the old one keeps serving).
