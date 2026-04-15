# Remote MCP Server (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the **remote MCP server** that exposes alekbot's memory search as a Model Context
Protocol tool over HTTPS, so claude.ai (and any other MCP-aware client) can add it as a
Custom Connector and call `get_user_context` during conversations.

This is the **inverse direction** of [`mcp_infrastructure/`](../mcp_infrastructure/README.md):
that block describes alekbot acting as an MCP *client* (talking to Google Maps); this block
describes alekbot acting as an MCP *server* (claude.ai talks to us).

### When to Read

- **For AI Agents:** Before touching anything under `src/*/mcp*` except `adapters/mcp/`.
- **For Developers:** When changing OAuth flow, adding new MCP tools, or debugging
  auth/routing issues on `/mcp*` endpoints.

### When to Update

This document MUST be updated when:

- [ ] A new tool is exposed on the FastMCP instance (anything besides `get_user_context`).
- [ ] OAuth scopes change beyond `user_context`.
- [ ] Firestore collection schemas for MCP entities change.
- [ ] ASGI routing at the top of `main.py` changes (`parent_asgi`, path allowlist).
- [ ] The MCP SDK version is bumped (pinned at `mcp==1.27.*`).
- [ ] The consent page UI or auth flow is modified.

### Cross-References

- **RFC:** [../../10_rfcs/REMOTE_MCP_SERVER_RFC.md](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md)
  — design rationale, all the "why" questions
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
  — the actual RRF implementation the tool calls
- **OAuth Multi-Tenant:** [../oauth_multi_tenant/README.md](../oauth_multi_tenant/README.md)
  — how Cabinet JWT cookies are minted (consent page relies on them)
- **MCP Infrastructure:** [../mcp_infrastructure/README.md](../mcp_infrastructure/README.md)
  — the *inverse* direction (alekbot as MCP client)

---

## 1. Overview

### What it does

Exposes one MCP tool, `get_user_context(query, alternate_phrasing?, keywords?)`, to any
remote MCP client. When Claude on claude.ai calls the tool, the server runs a 6-vector RRF
search across the user's indexed facts and returns a formatted text block of memory facts,
RRF-ordered and grouped by domain.

### What it is not

- **Not** a generic MCP framework. It exposes exactly one tool; to add more, edit
  `composition/mcp_setup.py`.
- **Not** an OAuth provider for Cabinet login. The Cabinet login (Firebase OAuth) is
  independent; it's the *input* to the MCP OAuth flow (we read the Cabinet JWT cookie at
  the consent step to identify the user).
- **Not** the same as [`mcp_infrastructure/`](../mcp_infrastructure/README.md). That block
  is a client SDK; this block is a server.

### Why this exists

Claude on claude.ai has great UX but no memory of the user. alekbot has rich memory
(biographical facts, consolidated knowledge, indexed emails) but a simpler UX. A remote
MCP connector bridges the two: Claude keeps its UX and gains retrieval into alekbot's
memory at conversation time. See [RFC § 1](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#1-problem-statement).

---

## 2. Hexagonal Placement

```
src/domain/
    mcp.py                                 # MCPClient, MCPAuthCode, MCPRefreshToken (pydantic)

src/ports/
    mcp_client_repository.py               # MCPClientRepository (ABC)

src/adapters/
    firestore_mcp_client_repository.py     # port impl

src/services/
    mcp_authorization_service.py           # OAuth 2.1 business logic — pure DI

src/composition/
    mcp_sdk_oauth_provider.py              # SDK shim: implements OAuthAuthorizationServerProvider
    mcp_setup.py                           # builds FastMCP + provider + registers the tool

src/web/
    mcp_consent_app.py                     # Quart blueprint: /mcp/consent GET/POST

main.py                                     # parent ASGI dispatcher + lifespan chain
```

**Layering rationale (hexagonal):**

- `domain/mcp.py` — pure pydantic, no I/O. `MCPClient`, `MCPAuthCode`, `MCPRefreshToken`.
- `ports/mcp_client_repository.py` — storage contract (ABC with 9 abstract methods).
- `adapters/firestore_mcp_client_repository.py` — implements the port against Firestore
  with env-prefixed collection names. Follows the pattern from
  `FirestoreOAuthCredentialsAdapter`.
- `services/mcp_authorization_service.py` — all OAuth 2.1 business logic: DCR host allowlist,
  consent JWT mint/verify, auth code lifecycle, access JWT mint/verify, refresh token
  rotation. **Zero `config/` imports** — constructor takes individual values
  (`jwt_secret`, `mcp_resource_uri`, TTLs) as plain args. This is required by REQ-ARCH-01
  (`services/` cannot import `config/`).
- `composition/mcp_sdk_oauth_provider.py` — the shim implementing
  `mcp.server.auth.provider.OAuthAuthorizationServerProvider`. Lives in `composition/`
  rather than `adapters/` because REQ-ARCH-01 forbids `adapters/ → services/` imports,
  and this shim delegates to `MCPAuthorizationService` on every method. Composition is
  the only layer allowed to cross all boundaries.
- `composition/mcp_setup.py` — wiring factory. Reads `AuthConfig`, constructs the repo →
  service → SDK provider → `FastMCP` chain, registers the `get_user_context` tool, returns
  an `MCPComponents` dataclass.
- `web/mcp_consent_app.py` — Quart blueprint that owns the consent page UX (the only
  piece of the OAuth flow the SDK does NOT implement — the developer renders the consent
  HTML and validates the user).
- `main.py` — glues everything together: builds a dedicated per-service
  `SearchEnrichmentService` singleton for MCP, calls `build_mcp_components`, registers the
  consent blueprint on the existing Quart app, and wraps both in a plain ASGI dispatcher
  that routes by path.

---

## 3. External Contract

### 3.1 What claude.ai sees

When the user pastes `https://dev.alekbot.app/mcp` into claude.ai Settings → Connectors:

| Path | Method | Owner | Purpose |
|------|--------|-------|---------|
| `/mcp` | POST | FastMCP | MCP JSON-RPC protocol (initialize, tools/list, tools/call, ping) |
| `/.well-known/oauth-protected-resource/mcp` | GET | FastMCP | RFC 9728 protected-resource metadata |
| `/.well-known/oauth-authorization-server` | GET | FastMCP | RFC 8414 AS metadata |
| `/authorize` | GET/POST | FastMCP | OAuth 2.1 /authorize handler (→ provider.authorize() → redirect to `/mcp/consent`) |
| `/token` | POST | FastMCP | OAuth 2.1 token exchange (auth_code + PKCE, refresh_token) |
| `/register` | POST | FastMCP | RFC 7591 Dynamic Client Registration |
| `/mcp/consent` | GET/POST | Quart | Browser-facing consent UI (Approve/Deny) |

Note: the OAuth server endpoints live at **server root** (`/authorize`, `/token`, etc.) — not
under `/mcp/*`. This is a deliberate design: the SDK registers routes at absolute paths and
uses `issuer_url` for metadata URL construction, so we set `issuer_url = https://host` (root)
to make the metadata URLs match the actual routes. See
[RFC § 4.3](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#43-oauth-server-at-server-root-not-under-mcp).

### 3.2 Tool definition

One tool: `get_user_context`.

```json
{
  "name": "get_user_context",
  "title": "Get user context from alekbot memory",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query":              { "type": "string" },
      "alternate_phrasing": { "type": "string" },
      "keywords":           { "type": "array", "items": { "type": "string" } }
    }
  }
}
```

Description (full text in `composition/mcp_setup.py::_TOOL_DESCRIPTION`): instructs Claude
to always call it before answering, formulate `query`/`keywords` in English (since records
are stored in an English-aligned embedding space), and reserve `alternate_phrasing` for the
user's original language.

---

## 4. OAuth 2.1 Flow

Full sequence diagram in [RFC § 6.2](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#62-oauth-21-end-to-end-flow).
Here's the short version:

1. **Discovery:** claude.ai POSTs `/mcp`, gets 401 + `WWW-Authenticate` pointing at
   `/.well-known/oauth-protected-resource/mcp`. Follows the chain to AS metadata.
2. **DCR:** claude.ai POSTs `/register` with its callback URL. The SDK generates a random
   `client_id` (uuid4) + `client_secret` (32-byte hex), calls our
   `register_client` (via the SDK shim) which persists it to Firestore. Our service
   validates the `redirect_uri` host against an allowlist (`claude.ai`, `claude.com`,
   `localhost`, `127.0.0.1`).
3. **Authorize:** browser GET to `/authorize` with PKCE challenge + resource indicator.
   SDK calls `provider.authorize(client, params)` which calls
   `service.build_consent_redirect(client, params)`. The service mints a short-lived
   **consent-state JWT** containing the full `AuthorizationParams`, and returns
   `https://host/mcp/consent?req=<jwt>`. The SDK 302-redirects the browser there.
4. **Consent UI:** `/mcp/consent` (Quart blueprint) verifies the consent JWT. If the user
   has no Cabinet JWT cookie, redirects to `/auth/login?next=<self>` → Firebase flow →
   back here. Renders an HTML form with the client's `client_name` + requested scopes +
   Approve/Deny buttons.
5. **Approve:** POST to `/mcp/consent` with `action=approve`. The blueprint reads the
   Cabinet JWT cookie to get `user_id` + `account_id`, then calls
   `service.issue_auth_code_for_consent(consent_jwt, user_id, account_id)`. The service
   mints a random `code`, persists an `MCPAuthCode` bound to the user/account, and returns
   `(code, decoded_consent_request)`. The blueprint 302-redirects to the client's
   `redirect_uri?code=<code>&state=<state>`.
6. **Token exchange:** claude.ai POSTs `/token` with `grant_type=authorization_code`,
   `code`, `code_verifier`, `client_id`, `client_secret`. The SDK authenticates the client
   (compares `client_secret` verbatim via `hmac.compare_digest`), validates the code
   (load, expiry, redirect_uri match, PKCE S256), then calls
   `provider.exchange_authorization_code(client, auth_code)`. The shim calls
   `service.finalize_auth_code(code)` which consumes the code atomically (one-shot enforcement)
   and mints an access JWT + refresh token. The SDK returns them to claude.ai.
7. **Protocol calls:** claude.ai POSTs `/mcp` with `Authorization: Bearer <access_jwt>`.
   The SDK's `BearerAuthBackend` middleware calls `provider.load_access_token(jwt_str)`
   which calls `service.verify_access_token(jwt_str)`. If valid, the SDK attaches our
   `AlekAccessToken` subclass to `request.user.access_token`. The MCP protocol layer
   dispatches `tools/call` to our tool handler.
8. **Tool execution:** handler reads `user_id` + `account_id` from `request.user.access_token`,
   sets a `RequestContext` contextvar, calls `SearchEnrichmentService.enrich_context(...)`,
   formats the result facts into a text block, returns it as `{content: [{type: "text", text: ...}]}`.
9. **Refresh:** when the access token expires (1h), claude.ai POSTs `/token` with
   `grant_type=refresh_token`. The SDK validates and calls
   `provider.exchange_refresh_token()`. The shim calls `service.rotate_refresh_token(value)`
   which revokes the old refresh token (by sha256 hash) and mints a fresh pair.

---

## 5. Storage

Three Firestore collections, env-prefixed via `EnvironmentConfig`:

### 5.1 `{env}_mcp_oauth_clients`

DCR-registered OAuth clients. Doc ID = `client_id` (uuid4 string, generated by the SDK).

| Field | Type | Notes |
|-------|------|-------|
| `client_id` | string | uuid4 |
| `client_secret` | string | **plaintext** (SDK constraint — see below) |
| `client_secret_expires_at` | int / null | Unix seconds; null = no expiry |
| `client_name` | string | Client-supplied display name (e.g. "Claude") |
| `redirect_uris` | list[string] | Validated against host allowlist at registration |
| `grant_types` | list[string] | `["authorization_code", "refresh_token"]` |
| `response_types` | list[string] | `["code"]` |
| `scope` | string | Space-separated scope list |
| `token_endpoint_auth_method` | string | `client_secret_post` or `client_secret_basic` |
| `created_at` | datetime | UTC |

**Why plaintext `client_secret`:** The MCP SDK's `ClientAuthenticator` at `/token` compares
the stored value directly to what the client presents using `hmac.compare_digest`. There's
no hashing hook in the SDK — if we store a hash, compare fails. Mitigation: the Firestore
collection is service-account-scoped; treat it as sensitive. See
[RFC § 6.4](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#64-token--session-model).

### 5.2 `{env}_mcp_auth_codes`

Short-lived authorization codes. Doc ID = the random `code` value (32 url-safe bytes).

| Field | Type | Notes |
|-------|------|-------|
| `code` | string | one-shot — deleted on consume |
| `client_id` | string | FK to `mcp_oauth_clients` |
| `user_id` | string | Bound at consent-approve time from Cabinet JWT |
| `account_id` | string | Same source |
| `redirect_uri` | string | Must match the `/token` request |
| `code_challenge` | string | PKCE S256 challenge |
| `code_challenge_method` | string | Always `"S256"` |
| `resource` | string / null | RFC 8707 resource indicator |
| `scopes` | list[string] | |
| `expires_at` | datetime | UTC, ~10 min from issue |

**TTL policy (recommended):** enable Firestore TTL on `expires_at` for auto-cleanup of
unused codes:

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=development_mcp_auth_codes \
  --enable-ttl --database=us-production
```

### 5.3 `{env}_mcp_refresh_tokens`

Long-lived refresh tokens, rotated on every use. Doc ID = `sha256(token_value)` hex.

| Field | Type | Notes |
|-------|------|-------|
| `token_hash` | string | sha256 hex of the opaque token value |
| `client_id` | string | |
| `user_id` | string | |
| `account_id` | string | |
| `scopes` | list[string] | |
| `resource` | string / null | |
| `expires_at` | datetime | UTC, ~30d from issue |
| `revoked_at` | datetime / null | Set on rotation — `is_active` helper returns False |

The plaintext token is never stored — only its hash. Rotation: on every `/token` refresh
call, the old token's `revoked_at` is stamped, a new token is generated and stored.

**TTL policy (recommended):**

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=development_mcp_refresh_tokens \
  --enable-ttl --database=us-production
```

### 5.4 Stateless: access tokens and consent state

Neither is stored in Firestore:

- **Access tokens** — HS256 JWTs signed with `auth_config.oauth_session_secret`
  (reused from Cabinet JWT signing). Claims: `sub=user_id`, `account_id`, `client_id`,
  `scope`, `type="mcp_access"`, `aud=<mcp_resource_uri>`, `iat`, `exp`. TTL 1h. Validated
  on every `/mcp` request by the SDK via `provider.load_access_token` →
  `service.verify_access_token`.
- **Consent state** — HS256 JWT passed in the URL `?req=<jwt>` between
  `/authorize` and `/mcp/consent`. Claims: all `AuthorizationParams` fields plus
  `aud=mcp-consent`, `iat`, `exp`. TTL 10min. Verified on GET and POST of `/mcp/consent`.

### 5.5 No composite indexes

Every query in the adapter is a doc-id lookup (`.document(...)`). No `.where()`, no
`.order_by`, no vector search. Collections auto-create on first write.

---

## 6. ASGI Routing

### 6.1 The dispatcher

`main.py` wraps both `FastMCP.streamable_http_app()` and the existing Quart `main_app` in
a plain ASGI callable that routes by path:

```python
def is_mcp_path(path: str) -> bool:
    if path == "/mcp" or path == "/mcp/":
        return True
    if path in ("/authorize", "/token", "/register", "/revoke"):
        return True
    if path == "/.well-known/oauth-authorization-server":
        return True
    if path.startswith("/.well-known/oauth-protected-resource"):
        return True
    return False

async def parent_asgi(scope, receive, send):
    if scope["type"] == "lifespan":
        async with _fastmcp.session_manager.run():
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
    if is_mcp_path(scope.get("path", "")):
        await _mcp_asgi(scope, receive, send)
    else:
        await main_app(scope, receive, send)
```

**Why not `Mount("/mcp", ...)`:** Starlette's `Mount` does not match exact `POST /mcp`
(no trailing slash) and strips the prefix from the path, breaking the SDK's absolute-path
routes and the RFC 9728 PRM path. See
[RFC § 6.3](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#63-asgi-routing-at-the-top).

**Lifespan:** FastMCP's StreamableHTTP transport requires a running session manager
(`_fastmcp.session_manager.run()`). We run it inside the parent's lifespan so it stays
alive for the process. Quart has no `before_serving` / `after_serving` hooks in this
project (grep confirmed), so a second lifespan chain is not needed.

### 6.2 Path ownership

- **FastMCP owns**: `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`,
  `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`
- **Quart owns**: everything else, including `/mcp/consent`, `/auth/*`, `/api/*`, `/slack/*`,
  `/cabinet`, `/worker`, `/health`, `/telegram/*`, etc.

`/mcp/consent` goes to Quart by exclusion: the dispatcher only matches `/mcp` and `/mcp/`
exactly — not `/mcp/consent`. Safe as long as no one adds another path starting with
`/mcp/` owned by FastMCP.

---

## 7. Transport Security

FastMCP auto-enables DNS rebinding protection when `host` is loopback (default
`127.0.0.1`), which locks `allowed_hosts` to loopback addresses only. On Cloud Run our
real public hostname is `dev.alekbot.app`, so the default rejects every production request
with `"Invalid Host header"`. Fix: pass explicit `TransportSecuritySettings` with the
public host from `mcp_resource_uri`:

```python
parsed = urlparse(auth_config.mcp_resource_uri)
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        parsed.netloc,
        f"{parsed.netloc}:*",
        "127.0.0.1:*",
        "localhost:*",
    ],
    allowed_origins=[
        f"{parsed.scheme}://{parsed.netloc}",
        f"{parsed.scheme}://{parsed.netloc}:*",
        "http://127.0.0.1:*",
        "http://localhost:*",
    ],
)
```

Localhost remains allowed for local development and the MCP Inspector tool.

---

## 8. Consent Binding — Who Is "You"?

Two independent identification layers:

### 8.1 Client identification — "who is claude.ai?"

Handled by DCR at `/register`. claude.ai registers itself with a unique `client_id`.
This identifies the **application**, not the human. Anyone can register with
`client_name="Claude"` — the only protection is the redirect-URI host allowlist.

### 8.2 User identification — "which user is approving?"

Handled by the **existing Cabinet JWT cookie** at the consent-page step, NOT by the
OAuth flow itself. Flow:

1. Browser arrives at `/mcp/consent?req=<jwt>`
2. Blueprint reads `access_token` cookie (set by Cabinet login after Firebase OAuth)
3. If missing → 302 to `/auth/login?next=<self>` → Firebase OAuth flow → cookie set →
   back to `/mcp/consent`
4. `session_service.verify_access_token(cookie)` returns `{sub: user_id, account_id: ...}`
5. On POST approve, these IDs are stamped into the newly-minted `MCPAuthCode`
6. The access JWT that claude.ai eventually gets carries `sub=user_id` → the tool handler
   scopes search to that user

This means: **the user who is logged into Cabinet in their browser at the moment of
clicking Approve is the identity bound to the MCP token**. If they were logged in as user X
in Cabinet, the MCP connector will have access to user X's memory.

---

## 9. Tool Handler Internals

Defined in `composition/mcp_setup.py` as a closure inside `build_mcp_components`:

```python
@fastmcp.tool(name="get_user_context", title=..., description=...)
async def get_user_context(
    query: str,
    ctx: Context,
    alternate_phrasing: Optional[str] = None,
    keywords: Optional[List[str]] = None,
) -> str:
    request = ctx.request_context.request
    access_token = request.user.access_token
    if not isinstance(access_token, AlekAccessToken):
        return "(authentication error — invalid token type)"

    user_id = access_token.user_id
    account_id = access_token.account_id

    with RequestContext(user_id=user_id, account_id=account_id):
        enriched = await search_enrichment_service.enrich_context(
            keywords=keywords or [],
            search_phrase_1=query,
            search_phrase_2=alternate_phrasing or query,
            dedup_threshold=0.98,
            skip_semantic_dedup=False,
        )

    return _format_enriched_facts(enriched.facts)
```

**Key points:**

- Identity flows via `ctx.request_context.request.user.access_token` — set by the SDK's
  `BearerAuthBackend` middleware after it calls `provider.load_access_token`. No
  context-var plumbing in the tool handler — the SDK's type system carries it.
- `AlekAccessToken` is our subclass of `SDKAccessToken` that adds `user_id` + `account_id`.
  The `isinstance` check is belt-and-braces safety.
- `RequestContext(user_id, account_id)` is the project's existing contextvar mechanism.
  The Firestore fact repository reads it inside `search_facts()` to scope queries to the
  right account. Without this, search returns nothing or leaks data across accounts.
- `SearchEnrichmentService` is the same service `UserAgentFactory` uses for Quick/Smart —
  no reimplementation. For MCP we build a dedicated singleton at main.py wiring time
  (not per-user) because the tool handler serves many users on the same instance. See
  [RFC § 4.7](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md#47-searchenrichmentservice-as-a-dedicated-singleton-for-mcp).
- `_format_enriched_facts` produces a compact markdown-ish text block: `## <domain>`
  headers, `- <content>` lines, optional RRF score suffix. No JSON wrapping — plain text
  is what claude.ai wants in a tool result.

---

## 10. Deployment

### 10.1 Env vars

- `MCP_RESOURCE_URI` — canonical resource URI, e.g. `https://dev.alekbot.app/mcp`.
  Set in `cloudbuild-dev.yaml` via the `$_SERVICE_URL/mcp` substitution.
  Not yet in `cloudbuild-prod.yaml` — add when ready to promote.

### 10.2 Firestore

No indexes needed (doc-id lookups only). TTL policies are recommended but optional —
see § 5.2 and § 5.3.

### 10.3 Adding the connector in claude.ai

1. Settings → Connectors → Add custom connector
2. URL: `https://dev.alekbot.app/mcp`
3. Advanced settings: **empty** (DCR handles registration)
4. Browser opens → `/mcp/consent` → Cabinet login if needed → Approve
5. Connector is active

**If tool description or schema changes:** remove and re-add the connector. claude.ai
caches `tools/list` results at registration time; a fresh DCR is the most reliable way
to force a refresh.

---

## 11. Testing

94 unit tests across 4 layers:

| File | Layer | Coverage |
|------|-------|----------|
| `tests/unit/ports/test_mcp_client_repository_port.py` | Port contract | ABC, in-memory fake roundtrip, one-shot consume, revoke idempotency |
| `tests/unit/services/test_mcp_authorization_service.py` | Service | DCR host allowlist, consent JWT, PKCE, code mint + finalize, refresh rotation, access JWT verify |
| `tests/unit/composition/test_mcp_sdk_oauth_provider.py` | SDK shim | Type translation, delegation, error mapping |
| `tests/unit/web/test_mcp_consent_app.py` | Consent blueprint | GET with/without cookie, POST approve/deny, bad JWT |

Run with:

```bash
./venv/bin/python -m pytest tests/unit/services/test_mcp_authorization_service.py \
    tests/unit/ports/test_mcp_client_repository_port.py \
    tests/unit/composition/test_mcp_sdk_oauth_provider.py \
    tests/unit/web/test_mcp_consent_app.py -v
```

All architecture-isolation tests (REQ-ARCH-01/11/12/21) pass.

### 11.1 End-to-end smoke test (dev)

After `make deploy-dev`:

```bash
# AS metadata
curl -sS https://dev.alekbot.app/.well-known/oauth-authorization-server | jq .

# PRM metadata
curl -sS https://dev.alekbot.app/.well-known/oauth-protected-resource/mcp | jq .

# MCP endpoint — expect 401 + WWW-Authenticate
curl -sS -i -X POST https://dev.alekbot.app/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head
```

Expected: two 200 JSONs + one 401 with `WWW-Authenticate: Bearer resource_metadata="..."`.

### 11.2 MCP Inspector

For protocol-level testing, use Anthropic's MCP Inspector:

```bash
npx @modelcontextprotocol/inspector https://dev.alekbot.app/mcp
```

It handles the full OAuth flow including browser-based consent and lets you call
`tools/call` manually.

---

## 12. Common Failure Modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Authorization with the MCP server failed` in claude.ai UI | `"Invalid Host header: dev.alekbot.app"` in logs — FastMCP DNS rebinding protection with loopback allowlist | Pass explicit `TransportSecuritySettings` with public netloc (already fixed — see § 7) |
| `404 /mcp` on POST | Starlette Mount not matching exact `/mcp` without trailing slash | Replace Mount with plain ASGI dispatcher (already fixed — see § 6) |
| `404 /.well-known/oauth-protected-resource/mcp` | Mount under `/mcp` prefixed the RFC 9728 path incorrectly | Move issuer to server root, use dispatcher (already fixed — see RFC § 6.3) |
| `invalid_redirect_uri` on DCR | Client supplied a callback host not in the allowlist | Check `_ALLOWED_REDIRECT_HOSTS` in `composition/mcp_setup.py` |
| Tool returns `"(authentication error — no user context available)"` | `request.user` is None — BearerAuthBackend didn't attach the token | Check `provider.load_access_token` returns a non-None `AlekAccessToken` |
| Tool returns empty/wrong facts | `RequestContext` not set → repo queries wrong account | Verify the `with RequestContext(...)` wraps `enrich_context` |
| claude.ai passes everything as CSV in `query` field | Tool description not explicit enough about splitting into three fields | Strengthen description wording |

---

## 13. Code References

- Domain: [`src/domain/mcp.py`](../../../src/domain/mcp.py)
- Port: [`src/ports/mcp_client_repository.py`](../../../src/ports/mcp_client_repository.py)
- Adapter: [`src/adapters/firestore_mcp_client_repository.py`](../../../src/adapters/firestore_mcp_client_repository.py)
- Service: [`src/services/mcp_authorization_service.py`](../../../src/services/mcp_authorization_service.py)
- SDK shim: [`src/composition/mcp_sdk_oauth_provider.py`](../../../src/composition/mcp_sdk_oauth_provider.py)
- Factory: [`src/composition/mcp_setup.py`](../../../src/composition/mcp_setup.py)
- Consent blueprint: [`src/web/mcp_consent_app.py`](../../../src/web/mcp_consent_app.py)
- main.py wiring: [`main.py`](../../../main.py) (search for `MCP components`)
- Config: [`src/config/auth.py`](../../../src/config/auth.py) (`mcp_*` fields on AuthConfig)
- Env collections: [`src/config/environment.py`](../../../src/config/environment.py)
  (`mcp_oauth_clients_collection` etc.)

---

## 14. Status & Roadmap

**Status:** ✅ Live on dev, end-to-end verified with a real claude.ai connector call.

### Planned enhancements

- **Iterate on tool description** based on observed claude.ai tool-use patterns (currently
  Claude sometimes stuffs three phrases into `query` as CSV).
- **Per-user tier limits** — resolve `semantic_limit` from the user's `UserBotConfig` at
  query time instead of a fixed `total_limit=30`. Trade-off: extra Firestore lookup on
  the hot path.
- **Cabinet UI for DCR client management** — list registered clients, revoke, inspect
  last-use timestamps.
- **More tools** — once memory search is validated, consider `save_fact`, `get_recent_emails`,
  `create_reminder`.
- **Production deployment** — add `MCP_RESOURCE_URI` to `cloudbuild-prod.yaml` after
  dev bake-in.
- **TTL policies** in Firestore to auto-clean stale auth codes and refresh tokens.

---

**Last Updated:** 2026-04-15 (initial)
**Status:** ✅ Live (dev only)
**Feature branch:** `feature/mcp-connector`
