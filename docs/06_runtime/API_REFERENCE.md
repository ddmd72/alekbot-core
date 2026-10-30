# API Reference

HTTP routes of the main Quart service (`main.py` registers the blueprints in `src/web/` and the platform adapters).
None of them is a public API: they serve the Cabinet, the chat platforms, Google Cloud (Cloud Tasks / Scheduler),
Twilio, OpenAI webhooks and the claude.ai connector. No version prefix; breaking changes ship with the deploy.

**Auth legend**
- **JWT** — Cabinet session: `access_token` HttpOnly cookie (web UI) or `Authorization: Bearer <token>`
  (`src/web/cabinet_auth.py`). Invalid or expired → `401`. The session is capped at 24 h with no silent refresh
  (`decisions/cabinet_session_24h_cap.md`).
- **owner** — JWT plus the account-owner check.
- **OIDC** — Google-signed ID token of an allowed service account (`decisions/worker_oidc_and_docx_sandbox.md`).
- **none** — public, or protected by its own signature/state as noted.

## Sign-in and connections (`/auth/*`, `src/web/oauth_app.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/auth/login` | none | Redirect to Google sign-in |
| `GET` | `/auth/callback` | none (OAuth state) | Sign-in callback; whitelist gate; sets the session cookies |
| `POST` | `/auth/refresh` | cookie | New access token within the 24 h cap |
| `POST` | `/auth/logout` | cookie | Revoke the refresh token, clear cookies |
| `GET` | `/auth/me` | JWT | Current user and account |
| `POST` | `/auth/link-oauth` | JWT | Link another OAuth identity to the user |
| `GET` | `/auth/connect/<kind>` | Cabinet session | Start an integration connect; `kind` is `gmail`, `google_tasks`, `microsoft_todo` or `microsoft_onedrive`. `?credential=<id>` reconnects that stored credential. Unknown kind 404; a realm with no OAuth client on this deployment 501 |
| `GET` | `/auth/connect/callback` | Cabinet session / server-side flow state | The one callback of every integration (Google and Microsoft). The flow is stored server-side and consumed once; the user returns to the Cabinet with `?connected=<kind>` or `?connect_error=<status>`. The To Do activation enqueues `setup_microsoft_todo`. Google Tasks is frozen: no connect button, stored grants keep working |

## Cabinet API (`src/web/user_cabinet_app.py`, JWT on every route)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/user/platforms` | Linked Slack / Telegram identities |
| `POST`, `DELETE` | `/api/user/link-platform` | Link / unlink Slack (`?platform=slack` on delete) |
| `POST` | `/api/user/link-telegram` | Link Telegram |
| `POST` | `/api/user/request-phone-otp`, `/api/user/verify-phone-otp` | Prove a phone number (Twilio Verify) for voice calls |
| `GET` | `/api/user/facts` | Latest facts |
| `GET` | `/api/user/facts/browse` | Cursor-paginated facts (`limit`, `cursor`, `domain`) |
| `POST` | `/api/user/facts/search` | Semantic search over facts |
| `POST` | `/api/user/facts/<fact_id>/invalidate` | Invalidate a fact (no LLM; the baseline cache is rebuilt) |
| `GET`, `POST` | `/api/user/invite-codes` | List codes / create one (**owner**) |
| `POST` | `/api/user/join-team` | Join an account with an invite code |
| `GET`, `PUT` | `/api/user/timezone` | User timezone |
| `GET`, `PUT` | `/api/user/location` | User location (`knowledge_base { user_location }`) |
| `GET`, `POST` | `/api/user/language` | UI / response language |
| `GET`, `PUT` | `/api/user/voice-languages` | Spoken languages for voice-message transcription |
| `GET`, `PUT` | `/api/user/deep-research` | Deep research settings |
| `GET`, `POST` | `/api/user/reminders` | List / create reminders |
| `PUT`, `DELETE` | `/api/user/reminders/<note_id>` | Edit / delete a reminder |
| `GET` | `/api/connectors` | The connectors the Cabinet offers a connect button for: `kind`, `integration`, `label` (a kind whose realm has no OAuth client here is left out) |
| `GET` | `/api/gmail/status` | Gmail connection, `needs_reconnect`, indexing cursor |
| `POST` | `/api/gmail/index` | Start an indexing job (`incremental` / `backfill` / `reindex`) |
| `GET` | `/api/gmail/jobs/<job_id>` | Job status |
| `POST` | `/api/gmail/jobs/<job_id>/cancel` | Cancel a running job |
| `GET`, `PUT` | `/api/gmail/auto-index` | Scheduled indexing settings |
| `GET`, `PUT` | `/api/gmail/daily-review` | Daily email review settings |
| `DELETE` | `/api/gmail/disconnect` | Revoke Gmail and remove credentials |
| `DELETE` | `/api/gmail/data` | Delete all indexed email data |
| `GET` | `/api/tasks/status` | Task-list connection state |
| `DELETE` | `/api/tasks/disconnect` | Revoke task-list access |
| `GET` | `/api/tasks/microsoft/status` | To Do connection and change subscriptions |
| `POST` | `/api/tasks/microsoft/reindex` | Re-index all task lists (Cloud Tasks) |
| `GET` | `/api/tasks/microsoft/lists` | The user's To Do lists (proxied from Graph) |
| `DELETE` | `/api/tasks/microsoft/disconnect` | Drop subscriptions, revoke tokens, clear the search index |
| `GET` | `/api/drive/status` | Drive connection and index state |
| `GET`, `PUT` | `/api/drive/indexing` | Drive indexing opt-in |
| `GET`, `PUT` | `/api/drive/deep-research-copy` | Copy deep research reports to the drive (opt-in) |
| `DELETE` | `/api/drive/disconnect` | Disconnect the drive |

## Web pages

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | none | Public landing, one self-contained file (`src/web/static/landing.html`, no external requests, hand-maintained numbers), the GitHub repo's homepage; a signed-in owner is redirected to `/cabinet` (`src/web/landing_app.py`) |
| `GET` | `/og.png`, `/robots.txt`, `/favicon.ico` | none | Landing assets: the share image, and a `robots.txt` that allows only `/$` and closes everything else |
| `GET` | `/cabinet` | none (its API calls need JWT) | Cabinet SPA; on a touch device it opens `/cabinet/call` |
| `GET` | `/cabinet/call` (+ `manifest.webmanifest`, `icon-<size>.png`) | none (its API calls need JWT) | Voice call page, installable |
| `GET` | `/join?code=…` | cookie / redirect | Team invite deep link |
| `GET` | `/cabinet/docs[/<path>]` | owner | The `docs/` tree, rendered |
| `GET` | `/f/<token>`, `/f/<token>/raw` | capability token | Private file access (`src/web/file_access_app.py`) |
| `GET` | `/s/<code>` | none | Short link to a `/f/<token>` URL |

## Voice

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/voice/auth`, `/voice/inbound-status`, `/voice/answer`, `/voice/status` | Twilio signature | Phone-call webhooks (`src/web/voice_webhook_app.py`) |
| `POST` | `/voice/session-config`, `/voice/submit-transcript`, `/voice/delegate`, `/voice/delegate/abandon`, `/voice/relay-warmup` | OIDC | Control plane for the voice relay (`src/web/voice_control_plane_app.py`) |
| `POST` | `/api/voice/web-call` | JWT | Start a web call (Cloudflare SFU) |
| `POST` | `/api/voice/web-call/<call_id>/connect`, `/renegotiate`, `/hangup` | JWT | Web call signalling |
| `GET` | `/api/voice/web-call/<call_id>/status` | JWT | Web call state |

## Platforms, workers and webhooks

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/slack/events` | Slack signature | Slack Events API; enqueues a named Cloud Task |
| `POST` | `/telegram/webhook` | Telegram secret token | Telegram updates; enqueues `telegram_update` |
| `POST` | `/worker` | OIDC | Cloud Tasks / Scheduler entry; `WorkerHandler` dispatches on `task_type` (`docs/07_deployment/SCHEDULERS.md`) |
| `POST` | `/webhook/microsoft-tasks/<user_id>` | `clientState` | Graph change notifications (`?validationToken=` is echoed); re-indexes the task, renews the subscription |
| `POST` | `/webhooks/openai/deep-research` | webhook signature | OpenAI deep research events; enqueues `deep_research_webhook_event` |
| `GET` | `/health` | none | Liveness probe |

## Remote MCP server (claude.ai connector)

Routed by the ASGI dispatcher in `main.py` to the `mcp` SDK app: `/mcp` (streamable HTTP, bearer token),
`/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource…`; the consent page is `GET`/`POST /mcp/consent`
(`src/web/mcp_consent_app.py`). See `docs/05_building_blocks/remote_mcp_server/`.

## Other services

- **Drive indexer** (`indexer_main.py`): `POST /worker` (`drive_sync` only, OIDC, internal ingress), `GET /health`.
- **Sandbox render service** (`sandbox_main.py`): `POST /v1/<op>` (OIDC, per-op callers), `GET /health`.
- **Voice relay** (`relay_main.py`): WebSocket server for Twilio media streams and the SFU `/sfu/*` legs; not Quart.

## Notes

- Fact writes: invalidation is the only direct API write; facts are created and corrected through consolidation
  and `$memory save`, never over HTTP.
