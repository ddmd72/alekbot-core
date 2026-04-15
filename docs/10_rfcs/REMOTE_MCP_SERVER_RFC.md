# RFC: Remote MCP Server — Expose alekbot Memory to claude.ai

**Status:** IMPLEMENTED
**Date:** 2026-04-15
**Owner:** Dmytro
**Milestone:** External Interop — Custom Connectors

**Related:** `MCP_INFRASTRUCTURE_RFC.md` (the **inverse** direction — alekbot as MCP *client* to Google Maps)

---

## 1. Problem Statement

Anthropic launched **Custom Connectors** on claude.ai: any user can point Claude at a remote MCP (Model Context Protocol) server, and Claude will treat the exposed tools as first-class callable actions during conversations. The claude.ai UI handles OAuth, tool discovery, and tool-call invocation automatically.

We already have a sophisticated user memory system — biographical facts, consolidation, RRF vector search across four channels. It's only accessible through the alekbot chat (Slack/Telegram/Cabinet). This RFC is about **lifting one capability — memory search — out of alekbot and into claude.ai**, so Claude-on-claude.ai can ground its responses in the user's exocortex before answering.

**User-visible value:** ask Claude on claude.ai "what did I tell you about my medical tests?" and get an answer grounded in alekbot's indexed email facts + consolidated biographical memory, not hallucinated.

**Why not just use alekbot chat?** claude.ai has better UX for long conversations, artifacts, file uploads, and the Sonnet/Opus class of models. Users want to keep that UX and add their memory as an external capability.

## 2. Goals

1. Expose one tool — `get_user_context(query, alternate_phrasing?, keywords?)` — returning RRF-ranked memory facts.
2. Full OAuth 2.1 authorization server compatible with claude.ai's Custom Connector discovery flow (RFC 8414 / RFC 9728 / RFC 7591 DCR / PKCE S256 / RFC 8707 resource indicator).
3. Per-user identity: the issued token MUST carry `user_id` + `account_id` so the tool handler scopes search to the right user's facts.
4. Latency ≤ 1.5s p50 per tool call (claude.ai is latency-sensitive for first-token).
5. Zero business-logic duplication: reuse `SearchEnrichmentService` directly, bypass the whole alekbot agent stack (Quick/Smart/MemorySearchAgent).

## 3. Non-Goals

- Multi-tool surface (`save_fact`, `list_emails`, etc.) — out of scope for MVP.
- Admin UI for managing registered MCP clients (DCR without Cabinet UI is fine for solo-dev).
- Rate limiting on `/register` — Cloud Run caps handle abuse risk at this scale.
- Prod deployment — dev only for experimentation.
- Backwards compatibility with the 2025-03-26 MCP OAuth profile — target 2025-06-18 / 2025-11-25.
- Replacing `MemorySearchAgent` in the main chat path. That agent still serves the Quick/Smart orchestrators unchanged.

## 4. Key Design Decisions

### 4.1 Bypass the alekbot agent stack

The obvious architecture is: claude.ai → MCP server → our `MemorySearchAgent` → RRF. We deliberately don't do this.

**Reasoning:** `MemorySearchAgent` exists because Quick/Smart orchestrators often pass raw user messages that need LLM reformulation into search keys before vector search. An ECO-tier Gemini Flash Lite call inside the agent extracts `(keywords, phrase_1, phrase_2)` from the raw query — adds ~1–2s latency per call.

On claude.ai the "orchestrator" is Claude Sonnet/Opus itself. It's already capable of formulating good search queries directly. Giving it three tool parameters (`query`, `alternate_phrasing`, `keywords`) lets it do the key formulation *at the call site* — no server-side LLM hop needed.

**Latency budget:**
- With `MemorySearchAgent`: embed ~200ms + LLM key formulation ~1–2s + 6 × `find_nearest` ~700–1200ms = **~2.5–3.5s**
- Direct call to `SearchEnrichmentService`: embed ~200ms + 6 × `find_nearest` ~700–1200ms = **~1.0–1.4s**

Saves ~1.5s of first-token latency on every call. Measured on first real call: `find_nearest` × 6 in parallel finished in 225–437ms, total handler round-trip ~1.0s.

### 4.2 Use the official `mcp` Python SDK (not a custom server)

We could hand-roll the MCP protocol and OAuth server — the spec is public. We don't.

- The `mcp` SDK (`mcp.server.fastmcp.FastMCP`) ships with Streamable HTTP transport, JSON-RPC protocol handlers, RFC 9728 / RFC 8414 metadata endpoints, `/authorize` / `/token` / `/register` handlers with PKCE S256 and RFC 8707 validation, and a `BearerAuthBackend` middleware. All of that would be ~700 LOC of custom code that ships bugs.
- The SDK's `OAuthAuthorizationServerProvider` Protocol is the clean extension point: we implement 9 methods, the SDK handles the rest.
- **Cost:** we adopt the SDK's API shape, which is API-unstable (`FastMCP` is pinned to `mcp==1.27.*` and may have breaking changes on minor bumps). Acceptable for an experimental feature.

### 4.3 OAuth server at server root, not under `/mcp`

The first attempt mounted FastMCP's Starlette sub-app under `/mcp` in a parent Starlette via `Mount("/mcp", ...)`. This broke:

- **Absolute-path routes inside the SDK**: `create_auth_routes` registers `Route("/authorize", ...)`, `Route("/token", ...)`, `Route("/.well-known/oauth-authorization-server", ...)`. They are not prefixed by any mount awareness — they are server-root absolute.
- Metadata URLs are built as `issuer_url + "/authorize"`. If issuer is `https://host/mcp` then metadata advertises `https://host/mcp/authorize`, but the SDK's physical route under `Mount("/mcp")` becomes `/mcp/authorize` externally only because `/authorize` internally + `/mcp` mount prefix happen to concatenate right. Fragile.
- `/.well-known/oauth-protected-resource/mcp` (RFC 9728 path-suffix) is at **server root** by spec — mounting the sub-app under `/mcp` moves it to `/mcp/.well-known/oauth-protected-resource/mcp`, which is non-compliant.
- `Mount("/mcp", ...)` in Starlette does **not match the exact path `/mcp`** without a trailing slash — only `/mcp/...`. claude.ai sends `POST /mcp` literally, got 404.

**Fix:** set `issuer_url = "https://host"` (server ROOT), `resource_server_url = "https://host/mcp"`. AS endpoints live at `/authorize`, `/token`, `/register`, `/.well-known/oauth-authorization-server` (server root). PRM lives at `/.well-known/oauth-protected-resource/mcp`. MCP protocol endpoint lives at `/mcp`. No mount prefix arithmetic — every SDK-registered path matches its external URL 1:1.

Then replace Starlette `Mount` with a plain ASGI dispatcher that forwards FastMCP-owned paths to the SDK sub-app and everything else to Quart. See § 6.3.

### 4.4 Allowlist the public hostname in `TransportSecuritySettings`

FastMCP's constructor auto-enables DNS rebinding protection when `host` is a loopback (default `127.0.0.1`), hardcoding `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`. This intercepts every `/mcp` request and rejects anything with a `Host:` header not in that list — including our real public hostname `dev.alekbot.app`. OAuth endpoints (`/authorize`, `/token`, `/register`) are regular Starlette routes and bypass this check, so DCR and consent flow appear to work, but the first `/mcp` protocol request after `/token` is silently dropped with `"Invalid Host header"` — claude.ai surfaces this as `"Authorization with the MCP server failed"`.

**Fix:** pass explicit `TransportSecuritySettings(allowed_hosts=[...])` with the public netloc from `mcp_resource_uri` plus localhost for local dev. DNS rebinding protection stays on — we just extend the allow-list.

### 4.5 SDK provider lives in `composition/`, not `adapters/`

Hexagonal isolation rule **REQ-ARCH-01** forbids `adapters/` → `services/` imports. Our SDK provider delegates to `MCPAuthorizationService` — a service. Two choices:

- **A.** Create a port `MCPAuthorizationPort` that the service implements, and let the adapter depend on the port.
- **B.** Move the provider shim to `composition/`, which is allowed to cross all layers (it's the wiring root).

(B) is the right call: the SDK provider has **zero business logic** — every method is a type translation plus a delegate call. Creating a port for something with one implementation is overkill. The composition layer is exactly where "glue between an external library and our internal services" belongs — same architectural role as `ClaudeAdapter` for the Anthropic SDK, except that ClaudeAdapter implements an internal port (`LLMPort`) so it fits in `adapters/`. There is no internal port here — the SDK defines the interface.

File: `src/composition/mcp_sdk_oauth_provider.py`.

### 4.6 Consent UI lives in Quart, not in the SDK

The MCP SDK handles the OAuth protocol entirely except for the `/authorize` → consent UX step: the SDK's `authorize()` provider method returns a redirect URL that the browser follows. The developer is expected to render the consent page themselves, validate the user, and issue the auth code.

We implement `/mcp/consent` as a small Quart blueprint (`src/web/mcp_consent_app.py`) because:

- The consent page needs to verify the user's existing **Cabinet session cookie** (`access_token` — JWT from Firebase OAuth login). That's Quart's existing territory.
- Quart already handles all our web templates and request lifecycle.
- No reason to introduce a second Starlette-based web layer just for one HTML form.

The binding between the two is mechanical: our `MCPAuthorizationService` mints a **consent-state JWT** (`aud=mcp-consent`, 10min TTL) containing the pending `AuthorizationParams` and includes it in the redirect URL (`/mcp/consent?req=<jwt>`). The consent blueprint verifies the JWT, shows Approve/Deny, and on approve calls back into the service to issue the real OAuth code. Stateless, no extra Firestore collection for pending consents.

### 4.7 `SearchEnrichmentService` as a dedicated singleton for MCP

`SearchEnrichmentService` is NOT a `ServiceContainer` singleton in the main codebase. `UserAgentFactory` builds one per-user with `total_limit` tied to the user's tier (`UserBotConfig.semantic_limit`). For MCP we need a shared instance — the tool handler is called on many user requests, not per-user construction.

**Decision:** build a dedicated MCP singleton at `main.py` wiring time with default `SearchConfig()` limits and a fixed `total_limit=30`. Uses the same `container.repository` + `container.embedding_service` as everyone else. Per-user facts are scoped via `RequestContext(user_id=..., account_id=...)` set inside the tool handler before the search call — same mechanism the Firestore repository already respects for all queries.

## 5. External Contract

### 5.1 Discovery flow (how claude.ai finds everything)

User pastes `https://dev.alekbot.app/mcp` into claude.ai → claude.ai sends `POST /mcp` with no Bearer token → 401 with `WWW-Authenticate: Bearer resource_metadata="https://dev.alekbot.app/.well-known/oauth-protected-resource/mcp"` → claude.ai fetches that URL → finds `authorization_servers: ["https://dev.alekbot.app/"]` → fetches `https://dev.alekbot.app/.well-known/oauth-authorization-server` → finds all OAuth endpoints + `registration_endpoint` → does DCR → proceeds with Authorization Code + PKCE flow.

### 5.2 Tool definition

```json
{
  "name": "get_user_context",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query":              {"type": "string"},
      "alternate_phrasing": {"type": "string"},
      "keywords":           {"type": "array", "items": {"type": "string"}}
    }
  }
}
```

**Description is load-bearing** (it's the only lever on claude.ai's tool-use decisions):

> ALWAYS call this tool before answering any question from the user. Retrieves the user's personal biographical facts, preferences, ongoing projects, opinions, and historical context from their exocortex (alekbot). Without this context you will miss critical information the user expects you to know. Pass the user's question as `query`; optionally add `alternate_phrasing` with synonyms for better recall and `keywords` with 2-5 topical tags. **All records are stored in English. Always formulate `query` and `keywords` in English for optimal retrieval. Use `alternate_phrasing` for the original language if the user's question was not in English.** Skip only for pure math/code questions with zero personal dimension.

The English-priming sentence is load-bearing too — our embedding space is English-aligned, and claude.ai's first call used a three-phrase Russian string in `query` which degrades recall.

## 6. Architecture

### 6.1 Hexagonal layering

```
domain/       mcp.py                                      MCPClient, MCPAuthCode, MCPRefreshToken (pydantic)
ports/        mcp_client_repository.py                    MCPClientRepository (ABC)
adapters/     firestore_mcp_client_repository.py          port impl
services/     mcp_authorization_service.py                OAuth 2.1 business logic (pure DI — no config/adapter imports)
composition/  mcp_sdk_oauth_provider.py                   SDK shim, implements OAuthAuthorizationServerProvider
composition/  mcp_setup.py                                factory: builds FastMCP + sdk provider + tool handler
web/          mcp_consent_app.py                          Quart blueprint: GET/POST /mcp/consent
main.py                                                   parent ASGI dispatcher + lifespan chain
```

**Why this split is hexagonal-clean:**
- Domain pure (pydantic only, no I/O)
- Port is justified: storage is a real boundary + testable via fake
- Service owns all OAuth business rules and JWT minting
- SDK provider = wiring shim between SDK protocol and service API
- Web blueprint is tiny: one route pair for consent UI

### 6.2 OAuth 2.1 end-to-end flow

```
┌──────────────┐                                       ┌────────────────┐
│  claude.ai   │                                       │  dev.alekbot   │
│   client     │                                       │      .app      │
└──────┬───────┘                                       └───────┬────────┘
       │                                                       │
       │  1. user pastes URL → POST /mcp (no token)             │
       │──────────────────────────────────────────────────────>│
       │                                                       │
       │     401 + WWW-Authenticate: resource_metadata="…"     │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  2. GET /.well-known/oauth-protected-resource/mcp     │
       │──────────────────────────────────────────────────────>│
       │     {resource, authorization_servers}                 │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  3. GET /.well-known/oauth-authorization-server        │
       │──────────────────────────────────────────────────────>│
       │     {authorize, token, register, S256}                │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  4. POST /register  {client_name, redirect_uris}      │
       │──────────────────────────────────────────────────────>│
       │     201 {client_id, client_secret, …}                 │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  5. browser: GET /authorize?client_id=…&code_challenge=…&resource=…
       │──────────────────────────────────────────────────────>│
       │                                                       │  provider.authorize()
       │                                                       │  mints consent-state JWT
       │     302 Location: /mcp/consent?req=<consent_jwt>      │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  6. browser: GET /mcp/consent?req=<consent_jwt>       │
       │──────────────────────────────────────────────────────>│
       │                                                       │  requires Cabinet cookie
       │                                                       │  → redirect to /auth/login if missing
       │     200 HTML Approve/Deny form                        │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  7. browser: POST /mcp/consent  action=approve        │
       │──────────────────────────────────────────────────────>│
       │                                                       │  service.issue_auth_code_for_consent(
       │                                                       │    consent_jwt, user_id, account_id)
       │                                                       │  mints MCPAuthCode, persists
       │     302 redirect_uri?code=…&state=…                   │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  8. POST /token  grant_type=authorization_code        │
       │                  code=…, code_verifier=…              │
       │──────────────────────────────────────────────────────>│
       │                                                       │  SDK ClientAuthenticator: client_secret ✓
       │                                                       │  SDK TokenHandler: PKCE S256 ✓ + expiry ✓
       │                                                       │  provider.exchange_authorization_code()
       │                                                       │  → service.finalize_auth_code(code)
       │                                                       │     consume + mint access JWT + refresh token
       │     200 {access_token, refresh_token, expires_in}     │
       │<──────────────────────────────────────────────────────│
       │                                                       │
       │  9. POST /mcp  Authorization: Bearer <access_jwt>     │
       │     {jsonrpc: …, method: "tools/call",               │
       │      params: {name: "get_user_context", …}}          │
       │──────────────────────────────────────────────────────>│
       │                                                       │  BearerAuthBackend → provider.load_access_token
       │                                                       │  → service.verify_access_token (jwt.decode aud)
       │                                                       │  → AlekAccessToken with user_id/account_id
       │                                                       │  tool handler:
       │                                                       │    with RequestContext(user_id, account_id):
       │                                                       │      SearchEnrichmentService.enrich_context(...)
       │     200 {content: [{type: "text", text: <facts>}]}   │
       │<──────────────────────────────────────────────────────│
```

### 6.3 ASGI routing at the top

main.py replaces Starlette `Mount` with a plain ASGI callable that dispatches by path:

```python
def is_mcp_path(path: str) -> bool:
    if path in ("/mcp", "/mcp/"): return True
    if path in ("/authorize", "/token", "/register", "/revoke"): return True
    if path == "/.well-known/oauth-authorization-server": return True
    if path.startswith("/.well-known/oauth-protected-resource"): return True
    return False

async def parent_asgi(scope, receive, send):
    if scope["type"] == "lifespan":
        async with fastmcp.session_manager.run():
            # forward startup/shutdown messages back to the ASGI layer
            ...
        return
    if is_mcp_path(scope["path"]):
        await mcp_asgi(scope, receive, send)           # FastMCP
    else:
        await main_app(scope, receive, send)           # Quart
```

**Why not Starlette:** three reasons, all about `Mount("/mcp", ...)`:
1. does not match exact `POST /mcp` (no trailing slash) — sends it through the fallthrough mount and 404s
2. strips the `/mcp` prefix from the path, so SDK's absolute-path routes `/authorize`, `/token`, ... (registered at server root inside the sub-app) are no longer reachable at their advertised URLs
3. breaks RFC 9728 PRM path (`/.well-known/oauth-protected-resource/mcp`), which the spec fixes at server root — mounting relocates it to `/mcp/.well-known/oauth-protected-resource/mcp`

A plain dispatcher sidesteps all three because there's no path rewriting.

**Why not inside Quart:** Quart has no native way to mount a foreign ASGI app as a sub-tree. We'd have to monkey-patch or use a third-party middleware. The parent-ASGI dispatcher is ~30 LOC and has no dependencies.

**Lifespan chain:** Quart has no `before_serving` / `after_serving` hooks in this project (grep confirmed), so the parent's lifespan only runs FastMCP's `session_manager.run()` context. If Quart ever adds startup hooks, extend the context manager to forward the lifespan scope to Quart too.

### 6.4 Token + session model

| Kind                 | Form              | Storage                       | TTL    | Purpose                                         |
|----------------------|-------------------|-------------------------------|--------|-------------------------------------------------|
| MCP client           | pydantic model    | `{env}_mcp_oauth_clients`     | ∞      | DCR-registered OAuth client                     |
| Authorization code   | random 32 bytes   | `{env}_mcp_auth_codes`        | 10 min | One-shot redeem for access token                |
| Refresh token        | random 48 bytes   | `{env}_mcp_refresh_tokens`    | 30 d   | Rotated on every `/token` refresh call          |
| Access token         | HS256 JWT         | **stateless** — not stored    | 1 h    | Carries `sub`, `account_id`, `client_id`, `aud` |
| Consent-state JWT    | HS256 JWT         | **stateless** — in URL        | 10 min | Pending authorize() state between /authorize and /mcp/consent POST |

All JWTs signed with `auth_config.oauth_session_secret` (reused from Cabinet JWT signing — same HS256 secret, distinguished by `aud` field: `aud=cabinet` vs `aud=<mcp_resource_uri>` vs `aud=mcp-consent`).

**Why plaintext client_secret (unhashed):**
The `mcp` SDK's `ClientAuthenticator` at `/token` calls `provider.get_client(client_id)` and does:
```python
hmac.compare_digest(client.client_secret.encode(), request_client_secret.encode())
```
It compares the returned `client_secret` directly to the presented secret. There is **no hashing hook**. If we store a hash, the compare fails. So we store plaintext. Mitigation: Firestore collection is service-account-scoped, treat it as sensitive, never expose it beyond the backend. Acceptable for an MVP; if we ever productize this we'll need to upstream a hashing interface to the SDK or fork it.

### 6.5 Why subclass the SDK's `AccessToken`

The SDK's `AccessToken` has fields `token`, `client_id`, `scopes`, `expires_at`, `resource`. **It does not carry user_id.** But our tool handler needs `user_id` + `account_id` to run RRF search scoped to the right user.

`OAuthAuthorizationServerProvider` is `Generic[AuthorizationCodeT, RefreshTokenT, AccessTokenT]` — the authors explicitly expect subclassing. We define three subclasses in `mcp_sdk_oauth_provider.py`:

```python
class AlekAuthorizationCode(SDKAuthorizationCode):
    user_id: str
    account_id: str

class AlekRefreshToken(SDKRefreshToken):
    user_id: str
    account_id: str

class AlekAccessToken(SDKAccessToken):
    user_id: str
    account_id: str
```

The SDK's `BearerAuthBackend` wraps our `AlekAccessToken` in `AuthenticatedUser(auth_info=access_token)` and sets it on `request.user`. The tool handler downcasts:

```python
access_token = request.user.access_token
if isinstance(access_token, AlekAccessToken):
    user_id = access_token.user_id
    account_id = access_token.account_id
```

No contextvars gymnastics, no side-channel storage — the SDK's type system carries the identity through auth middleware into the handler.

## 7. Implementation Notes

### 7.1 Package rename surprise

The research agent reported the SDK class was renamed from `FastMCP` to `MCPServer` in `mcp.server.mcpserver`. This turned out to be wrong in the actually-shipped `mcp==1.27.0` — `FastMCP` is still at `mcp.server.fastmcp`. Confirmed by live introspection. The rename may have been proposed in a PR or a later (unreleased) version. Lesson: always verify SDK APIs by importing, not by web research alone.

### 7.2 PKCE and redirect_uri validation are SDK-owned

Our `MCPAuthorizationService.finalize_auth_code(code)` does NOT re-verify PKCE, redirect_uri match, client_id match, or code expiry — because the SDK's `TokenHandler.handle()` does all of that before calling `provider.exchange_authorization_code()`. The service only:
1. consumes the code atomically (one-shot enforcement)
2. mints access JWT
3. mints refresh token (random 48 bytes, stored by sha256 hash)

Same story for refresh token exchange: the SDK pre-validates client match, expiry, and scope downscoping. `service.rotate_refresh_token(token_value, requested_scopes)` only:
1. loads the existing token by hash
2. revokes it
3. mints a new refresh token + new access JWT

Defensive redundancy would mean passing `code_verifier` through the adapter, but the SDK doesn't expose it to `exchange_authorization_code` — by design. Trust the SDK at the adapter boundary.

### 7.3 Redirect-URI host allowlist

DCR is an unauthenticated endpoint: anyone can POST to `/register` and get a `client_id`. To prevent someone registering a malicious client with a callback URL pointing to their own evil.com, the service validates redirect URIs against a hardcoded host allowlist:

```python
_ALLOWED_REDIRECT_HOSTS = (
    "claude.ai",          # claude.ai/api/mcp/auth_callback
    "claude.com",         # may migrate
    "localhost",          # local MCP Inspector testing
    "127.0.0.1",          # same
)
```

Match logic: `host == h or host.endswith("." + h)` — allows subdomains of claude.ai (in case Anthropic adds staging environments). `http://` is only allowed for `localhost`/`127.0.0.1`; everything else requires `https://`. Violation → `RegistrationError(error="invalid_redirect_uri")` from the adapter → SDK returns 400.

### 7.4 Tool description is the only knob on call rate

We cannot *force* claude.ai to call a tool. The tool description is the only lever, and it matters a lot — "helps with personal context" will never trigger, "ALWAYS call this tool before answering" triggers aggressively. The current wording is a first pass and should be iterated based on observed call rate (tool-use events per conversation).

A known failure mode we're already watching: claude.ai stuffs everything into `query` as a comma-separated string instead of splitting into `query` / `alternate_phrasing` / `keywords`. Workaround: English-priming hint in description; longer-term, structural examples in the description or a multi-field schema with stricter descriptions per field.

## 8. Security

- **DCR abuse**: host allowlist prevents callback-hijacking; no `/register` rate-limit (Cloud Run caps). Experimental feature on dev — acceptable.
- **Access token**: HS256 JWT, same secret as Cabinet (`oauth_session_secret` ≥ 32 chars), distinct `aud`. Stateless — no revocation of issued access tokens until expiry (1h). Refresh tokens can be revoked server-side and are rotated on every use.
- **Client secret**: plaintext at rest (SDK constraint, see § 6.4). Treat `{env}_mcp_oauth_clients` collection as a secret.
- **Consent binding**: user identity comes from the **existing Cabinet JWT cookie** at the consent-page step. The OAuth flow does NOT re-authenticate the user — if they were logged in as user X in Cabinet, the MCP token is issued for user X. Guard: if no cookie, `/mcp/consent` redirects to `/auth/login?next=<self>` before rendering.
- **Redirect URI**: strict equality match between `/authorize` and `/token` (SDK enforces).
- **PKCE**: S256 only (no `plain`). SDK enforces, and the service's consent JWT carries `code_challenge` so even if the SDK flow is bypassed, re-exchange fails.

## 9. Testing

All tests are SDK-light — we do not test the SDK itself, only our integration surface.

| Layer | File | Coverage |
|-------|------|----------|
| Port contract | `tests/unit/ports/test_mcp_client_repository_port.py` | ABC contract, in-memory fake roundtrip for all three entity types, one-shot consume enforcement, revoke idempotency |
| Service | `tests/unit/services/test_mcp_authorization_service.py` | DCR host allowlist (claude.ai / subdomain / localhost / rejected hosts / http-on-public), consent JWT roundtrip, PKCE rejection, auth code mint + one-shot consume, refresh rotation, JWT aud/type/expiry rejection |
| SDK adapter | `tests/unit/composition/test_mcp_sdk_oauth_provider.py` | Domain↔SDK type translation roundtrip, provider method delegation, error → TokenError/RegistrationError mapping |
| Consent blueprint | `tests/unit/web/test_mcp_consent_app.py` | GET with/without cookie (redirect vs HTML), POST approve → 302 with code + state, POST deny → 302 with error, bad JWT → 400 |

**Out of scope (intentional):**
- SDK internals (PKCE verification, /token handler)
- Firestore wire format (adapter roundtrip covered by integration layer)
- Real claude.ai connection (no test environment on their side)

## 10. Deployment

### 10.1 Env vars

- `MCP_RESOURCE_URI` — canonical resource URI (`https://dev.alekbot.app/mcp` on dev). Set in `cloudbuild-dev.yaml` via `$_SERVICE_URL/mcp` substitution. Not yet in prod.

### 10.2 Firestore

No composite indexes needed — all lookups are doc-id. Three collections auto-create on first write:
- `{env}_mcp_oauth_clients`
- `{env}_mcp_auth_codes`
- `{env}_mcp_refresh_tokens`

**Recommended post-deploy hardening** (not required): TTL policies on `expires_at` for auth codes + refresh tokens to auto-cleanup stale docs.

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=development_mcp_auth_codes \
  --enable-ttl --database=us-production

gcloud firestore fields ttls update expires_at \
  --collection-group=development_mcp_refresh_tokens \
  --enable-ttl --database=us-production
```

### 10.3 Adding the connector in claude.ai

1. Settings → Connectors → Add custom connector
2. URL: `https://dev.alekbot.app/mcp`
3. Advanced settings: **empty** (DCR handles registration)
4. Browser opens → `/mcp/consent` → login if needed → Approve
5. Connector becomes active; Claude can call `get_user_context` in conversations

**If description changes and you want claude.ai to pick them up:** remove and re-add the connector. claude.ai caches `tools/list` results at registration time.

## 11. Open Questions

- **Tool call rate**: first real call worked but Claude stuffed three phrases into `query` as a CSV string. Monitor and iterate on description wording.
- **Per-user tier limits**: current MCP instance uses fixed `total_limit=30`. Should we resolve per-user tier at query time? Requires user profile lookup in the hot path — trade-off vs. latency.
- **Cabinet UI for client management**: nice-to-have — list registered DCR clients, revoke, audit access token issuance. Out of scope for MVP.
- **Expose more tools**: `save_fact`, `get_recent_emails`, `create_reminder` — once memory search is validated.
- **Prod deployment**: enable on prod once dev has stable call history. Requires `MCP_RESOURCE_URI` in `cloudbuild-prod.yaml`.

## 12. References

- MCP Authorization spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- MCP Transports: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Claude Custom Connectors: https://support.claude.com/en/articles/11503834-build-custom-connectors-via-remote-mcp-servers
- Anthropic tool writing guidance: https://www.anthropic.com/engineering/writing-tools-for-agents
- RFCs: 6749 (OAuth 2.0), 7591 (DCR), 7636 (PKCE), 8414 (AS metadata), 8707 (resource indicator), 9728 (protected resource metadata)

## 13. Changelog

- 2026-04-15: IMPLEMENTED. Feature branch `feature/mcp-connector`, commits `9ef02f2` .. `57e20e7`. End-to-end verified on dev: DCR → consent → /token → `get_user_context` with live user facts returned in ~1s.
