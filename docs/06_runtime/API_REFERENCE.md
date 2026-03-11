# API Reference

HTTP routes of the main Quart service (`main.py` registers the blueprints in `src/web/` and the platform adapters).
None of them is a public API: they serve the Cabinet, the chat platforms, Google Cloud (Cloud Tasks / Scheduler),

**Auth legend**

---





| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/auth/login` | none | Redirect to Google sign-in |
| `GET` | `/auth/callback` | none (OAuth state) | Sign-in callback; whitelist gate; sets the session cookies |
| `POST` | `/auth/refresh` | cookie | New access token within the 24 h cap |
| `POST` | `/auth/logout` | cookie | Revoke the refresh token, clear cookies |
| `GET` | `/auth/me` | JWT | Current user and account |
| `POST` | `/auth/link-oauth` | JWT | Link another OAuth identity to the user |
| `GET` | `/auth/connect-gmail` (+ `/callback`) | JWT / OAuth state | Connect Gmail (read-only scope) |
| `GET` | `/auth/connect-microsoft-todo` (+ `/callback`) | JWT / OAuth state | Connect Microsoft To Do; the callback enqueues `setup_microsoft_todo` |

---







| Method | Path | Description |
|--------|------|-------------|





### Facts

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/user/platforms` | Linked Slack / Telegram identities |
| `GET` | `/api/user/facts/browse` | Cursor-paginated facts (`limit`, `cursor`, `domain`) |
| `POST` | `/api/user/facts/search` | Semantic search over facts |
| `POST` | `/api/user/facts/<fact_id>/invalidate` | Invalidate a fact (no LLM; the baseline cache is rebuilt) |

### Team Invites

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | none | Public landing; a signed-in owner is redirected to `/cabinet` (`src/web/landing_app.py`) |
| `GET` | `/og.png`, `/robots.txt`, `/favicon.ico` | none | Landing assets |
| `GET` | `/cabinet` | none (its API calls need JWT) | Cabinet SPA; on a touch device it opens `/cabinet/call` |

---

## Voice



| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/voice/auth`, `/voice/inbound-status`, `/voice/answer`, `/voice/status` | Twilio signature | Phone-call webhooks (`src/web/voice_webhook_app.py`) |
| `POST` | `/voice/session-config`, `/voice/submit-transcript`, `/voice/delegate`, `/voice/delegate/abandon`, `/voice/relay-warmup` | OIDC | Control plane for the voice relay (`src/web/voice_control_plane_app.py`) |
| `POST` | `/api/voice/web-call` | JWT | Start a web call (Cloudflare SFU) |
| `POST` | `/api/voice/web-call/<call_id>/connect`, `/renegotiate`, `/hangup` | JWT | Web call signalling |
| `GET` | `/api/voice/web-call/<call_id>/status` | JWT | Web call state |




---

## Web UI

| Method | Path | Auth | Description |
|--------|------|------|-------------|




---

## Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|


---

## Notes

- Fact writes: invalidation is the only direct API write; facts are created and corrected through consolidation








