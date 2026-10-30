# Web — traps

- **The main Quart app has no static folder:** `/static/*` 404s; a new asset gets an explicit route (see `voice_web_call_app.py`).
- **Cabinet session is capped at 24h with no silent refresh** (owner rule); refresh tokens older than the current TTL are
  rejected (`decisions/cabinet_session_24h_cap.md`).
- **Sandbox hard stop:** remove the caller's `roles/run.invoker` binding on the render service; never empty
  `SANDBOX_RENDER_CALLERS_DEV` (an empty caller list refuses the new revision, the old one keeps serving).
