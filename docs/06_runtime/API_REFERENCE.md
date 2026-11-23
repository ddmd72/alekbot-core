<!-- living-doc
covers:
  src/web/main_service_routes.py
  src/web/landing_app.py
  src/web/static/landing.html
  src/web/static/alek-logo.png
  src/web/static/alek.jpg
  src/web/static/og.png
-->
# API Reference

The HTTP surface of the services, by route family: which module serves it, who may call it, what is public. None of it
is a public API: callers are the Cabinet, the chat platforms, Google Cloud (Cloud Tasks, Scheduler), Twilio, Microsoft
Graph and the claude.ai connectors. There is no version prefix; a breaking change ships with the deploy.

**Where the exact list lives.** The route list of the main service's Quart app is the sorted file
`tests/unit/main_boots_offline_url_map.txt`; the MCP ASGI paths served before Quart are pinned in `MCP_DISPATCHED` of the probe below. `tests/unit/web/test_every_route_refuses_anonymous.py` builds every
blueprint with its real gate, sends each route without credentials and asserts a refusal and that nothing behind the
gate was touched; a route that is anonymous by design is listed in its `PUBLIC` set. A new route is pinned in both
(`src/web/CLAUDE.md`). This page groups routes by family and module; the route module holds exact paths, bodies and status codes.

## Auth kinds

| Kind | What it is | Refusal |
|---|---|---|
| JWT | Cabinet device session: `__Host-access` HttpOnly cookie (web UI) or `Authorization: Bearer`, checked by the one verifier (`src/web/cabinet_auth.py::CabinetSessionVerifier`: the JWT with its `sid`, then the session record) through `make_auth_required`, shared by every Cabinet blueprint | 401 |
| account owner | JWT whose `role` claim is `owner` (invite codes) | 403 |
| docs owner | The one user id configured as `DOCS_OWNER_USER_ID` (`src/web/user_cabinet_app.py`, `owner_only`) | 404 on every failure, so the page looks absent |
| OIDC | Google-signed ID token of an allowed service account (`src/web/worker_oidc_verifier.py::build_oidc_gate`; `decisions/worker_oidc_and_docx_sandbox.md`) | 401 |
| signature | Slack (`src/adapters/slack/signature.py`), Telegram secret-token header, Twilio (`src/web/twilio_signature_verifier.py`) | 401 / 403 |
| capability | The id or token in the URL is the credential | a refusal that reveals nothing more where the module says so (`/d`, `GET /geo`); `/f` and `/s` answer with different bodies, and `/f` also 403 for a gated link opened by another user |

A login starts a device session: a 15-minute access JWT and a rotating refresh secret; `/auth/refresh` renews it until
14 days without use or 30 days after sign-in, and a revoked session is refused within 30 s
(`decisions/cabinet_device_session_lifetimes.md`).

## Main service (`main.py`)

| Family | Module | Auth |
|---|---|---|
| `/` landing, `/og.png`, `/alek.jpg`, `/robots.txt` | `src/web/landing_app.py` | public. `/` redirects a holder of a valid login cookie to `/cabinet`; `robots.txt` allows only the exact root |
| `/health`, `/favicon.ico`, `/worker` | `src/web/main_service_routes.py` | `/worker` OIDC; the others public |
| `/auth/*` sign-in and connect | `src/web/oauth_app.py` | see below |
| `/cabinet`, `/join`, `/cabinet/docs`, `/api/user/*`, `/api/gmail/*`, `/api/tasks/*`, `/api/drive/*`, `/api/connectors` | `src/web/user_cabinet_app.py` | JWT on every `/api/*` route; `/cabinet` and `/join` are not gated (the SPA calls the API with its cookie; `/join` consumes the invite when the cookie is valid, else redirects to sign-in); `/cabinet/docs` is docs owner |
| `/cabinet/call*` pages, `/api/voice/web-call*`, `/api/voice/ui-language`, `/api/voice/call-text/file` | `src/web/voice_web_call_app.py` | call pages and their manifests and icons are public static HTML; every `/api/voice/*` route is JWT |
| `/voice/auth`, `/voice/inbound-status`, `/voice/answer`, `/voice/status` | `src/web/voice_webhook_app.py` | Twilio signature (403) |
| `/voice/session-config`, `/voice/submit-transcript`, `/voice/delegate`, `/voice/delegate/abandon`, `/voice/relay-warmup` | `src/web/voice_control_plane_app.py` | OIDC: the voice relay's control plane |
| `/slack/events`, `/slack/interactivity`, `/slack/commands` (slash commands, queued, empty 200) | `src/adapters/slack/http_adapter.py` | Slack signature, checked on the raw capped body before it is parsed |
| `/telegram/webhook` | `src/adapters/telegram/webhook_adapter.py` | secret-token header |
| `/webhook/microsoft-tasks/<user_id>` | `src/web/microsoft_tasks_webhook.py` | `clientState` plus a subscription stored for that user |
| `/f/<token>`, `/f/<token>/raw` | `src/web/file_access_app.py` | capability token; a gated token also needs the Cabinet cookie of the file's owner |
| `/s/<code>` | `src/web/short_link_app.py` | capability; redirects to the target URL |
| `/d/<link_id>` | `src/web/drive_link_app.py` | capability; one-time drive download link |
| `/geo/<link_id>` | `src/web/geo_mark_app.py` | one-time link id (`GEO_MARKS_RFC`) |
| `/mcp/consent` | `src/web/mcp_consent_app.py` | Cabinet cookie; anonymous is sent to `/auth/login` |
| `/mcp`, `/drive-mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/oauth-*` | `src/web/mcp_dispatcher.py` to the `mcp` SDK apps | `/mcp` and `/drive-mcp` take a bearer token; `/authorize`, `/token`, `/register`, `/revoke` are the OAuth protocol endpoints; `/.well-known/oauth-*` is public by design (`MCP_PUBLIC` in the probe); see `docs/05_building_blocks/remote_mcp_server/README.md` |

Sign-in (`src/web/oauth_app.py`): `/auth/login` and `/auth/callback` are public (the callback is protected by its OAuth
state cookie and applies the whitelist gate, `decisions/cabinet_oauth_whitelist_gate.md`); `/auth/refresh` takes the
sid from the access cookie (expired accepted), re-checks the user and the whitelist and presents the `__Host-refresh`
secret (rotate / grace / replay → revoked); `/auth/logout` revokes the session; `/auth/me` (with `expires_at`) and
`/auth/link-oauth` need a live session;
`/auth/connect/<kind>` starts an integration connect (the kinds are the offered entries of `src/composition/connector_registry.py`; an unknown or not offered kind 404, a kind with no
OAuth client on this deployment 501) and `/auth/connect/callback` is the one callback of every integration, its flow
stored server-side (`docs/10_rfcs/MULTI_DRIVE_RFC.md` §5.2).

## Other services

| Service | Entry | Routes | Auth |
|---|---|---|---|
| Drive indexer | `indexer_main.py`, `src/web/indexer_worker_app.py` | `POST /worker` (`drive_sync` only, a payload without `connection_id` is refused), `GET /health` | OIDC with its own audiences (`indexer_audiences`); the service also runs with `--ingress=internal` (comment in `src/web/indexer_worker_app.py`) |
| Sandbox render service | `sandbox_main.py`, `src/web/sandbox_app.py` | `POST /v1/<op>`, `GET /health` | OIDC; `src/web/sandbox_caller_gate.py::SandboxCallerGate` lets each caller email use only the op prefixes it is configured for (403 otherwise) |
| Voice relay | `relay_main.py` | WebSocket: Twilio media streams, `/sfu/*` legs, `/ctl` control socket; plain HTTP gets 200 for health | not Quart; a phone call's ticket is minted by `/voice/auth`, a web call's by `/api/voice/web-call`; `/voice/session-config` only redeems them |

## Traps

- **`/worker` is not reachable unauthenticated.** A plain `curl` against a live service returns 401; manual triggers are
  in `CLAUDE.md` (Manual Triggers). Locally, with no OIDC identity and `K_SERVICE` unset, the gate is open; on Cloud
  Run an empty identity set refuses every call.
- **A task for a locked user is acknowledged, never run.** Both `/worker` routes answer 200 `{"skipped": "user_inactive"}`
  for a payload naming (`user_id`, or `context.user_id`) a user whose stored profile is inactive
  (`CABINET_DEVICE_SESSIONS_RFC.md` §7.8); so do `/voice/session-config` and `/voice/delegate` with 403.
- **The main Quart app has no static folder.** `/static/*` returns 404; an asset gets an explicit route (the call pages
  and the landing do this).
- **`/health` is registered twice** (the Cabinet's and `main_service_routes`); the Cabinet's shadows the other
  (`tests/unit/main_boots_offline_url_map.txt`, first comment line).
- **Logout revokes nothing.** `POST /auth/logout` only clears the two cookies and needs no credential; an issued token
  stays valid until it expires (`src/web/oauth_app.py::create_oauth_blueprint`).
- **The landing page is one self-contained file** (`src/web/static/landing.html`): no external requests, numbers
  maintained by hand,.
- **A hub-path trap in the dispatcher:** drive MCP paths are matched before the facts MCP's prefix match
  (`src/web/mcp_dispatcher.py::mcp_owner`); with the drive connector not built, `/drive-mcp` falls through to the Quart
  app and answers 404.
- **Facts are written over HTTP only from the Cabinet:** invalidation and `POST /api/user/facts/review-answers` (`src/services/fact_answer_service.py`); everything else is created and corrected through
  consolidation and `$memory save` (`docs/05_building_blocks/memory/README.md`).
