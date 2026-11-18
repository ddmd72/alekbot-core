# Web — traps

- **The main Quart app has no static folder:** `/static/*` 404s; a new asset gets an explicit route (see `voice_web_call_app.py`).
- **Every new route is pinned twice:** in `tests/unit/main_boots_offline_url_map.txt` (sorted; the offline-boot and
  Telegram-failure tests read it) and, if anonymous by design, in `PUBLIC` of `test_every_route_refuses_anonymous.py`.
  The boot tests are `slow`: a local run skips them, so a missing pin first shows up red in CI.
- **Cabinet session slides** (owner rule): a login gives 24h tokens and `/auth/refresh` mints a full-TTL access token;
  an expired refresh token, or one older than the current TTL, is rejected (`decisions/cabinet_session_24h_cap.md`).
- **Sandbox hard stop:** remove the caller's `roles/run.invoker` binding on the render service; never empty
  `SANDBOX_RENDER_CALLERS_DEV` (an empty caller list refuses the new revision, the old one keeps serving).
