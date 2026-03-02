# OAuth Web API

**Status:** ✅ Implemented (Session 5 - 2026-01-31)

**Purpose:** Web-based OAuth authentication API for Google sign-in, JWT session management, and user registration.

**Related:** [MULTI_TENANT_OAUTH_RFC.md](../../10_rfcs/MULTI_TENANT_OAUTH_RFC.md)

---

## Overview

OAuth Web API provides HTTP endpoints for authenticating users via Google OAuth (Firebase), managing JWT sessions, and linking OAuth identities to existing users. This is the **web UI authentication layer** that complements Slack/Telegram authentication.

### Key Features

- ✅ **Google OAuth Flow** - Full OAuth 2.0 / OIDC flow with Firebase
- ✅ **JWT Session Management** - Stateless access/refresh tokens
- ✅ **CSRF Protection** - State parameter validation
- ✅ **HttpOnly Cookies** - XSS attack prevention
- ✅ **Master Account First** - Creates BillingAccount before UserProfile
- ✅ **Identity Linking** - Link OAuth to existing Slack/Telegram users
- ✅ **CORS Support** - Web UI integration ready

---

## Architecture Position

```
┌─────────────────────────────────────────────────────────────────┐
│                        Clients                                  │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐      │
│  │   Web UI      │  │   Slack Bot   │  │ Telegram Bot  │      │
│  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘      │
└──────────┼──────────────────┼──────────────────┼──────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌──────────────┐   ┌──────────────────────────────┐
    │ OAuth Web API│   │   Slack/Telegram Adapters    │  ← Adapter Layer
    │ (Quart/Flask)│   │   (Socket Mode / HTTP)        │
    └──────┬───────┘   └──────────┬───────────────────┘
           │                      │
           ▼                      ▼
    ┌─────────────────────────────────────────────┐
    │       AuthenticationService                 │  ← Application Layer
    │  (Orchestration - auth flows)               │
    └──────────────────┬──────────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────────┐
    │       AuthProviderRegistry                  │  ← Application Layer
    │  - FirebaseAuthAdapter                      │  ← Adapter Layer
    │  - (Future: Other OAuth providers)          │
    └──────────────────┬──────────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────────┐
    │  Domain Entities: UserProfile,              │  ← Domain Layer
    │  BillingAccount, AccountTier                │
    └─────────────────────────────────────────────┘
```

**Layer:** Adapter / Web Interface
**Pattern:** REST API (Quart/Flask async)
**Integration:** Uses `AuthenticationService` (application layer) + `SessionService` (JWT)

### Hexagonal Architecture Position

```
┌─────────────────────────────────────────────────────────────────┐
│                      ADAPTER LAYER (Ports)                      │
│                                                                 │
│  ┌─────────────────┐                    ┌──────────────────┐   │
│  │ OAuth Web API   │                    │ FirebaseAdapter  │   │
│  │ (HTTP/REST)     │                    │ (Google OAuth)   │   │
│  └────────┬────────┘                    └────────┬─────────┘   │
└───────────┼──────────────────────────────────────┼─────────────┘
            │                                      │
            ▼                                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                   APPLICATION LAYER (Services)                  │
│                                                                 │
│  ┌──────────────────────────┐    ┌────────────────────────┐    │
│  │ AuthenticationService    │◄───┤ SessionService (JWT)   │    │
│  │ - OAuth flow logic       │    │ - Token generation     │    │
│  │ - User registration      │    │ - Token validation     │    │
│  └──────────┬───────────────┘    └────────────────────────┘    │
└─────────────┼───────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      DOMAIN LAYER (Entities)                    │
│                                                                 │
│  ┌──────────────┐  ┌─────────────────┐  ┌─────────────────┐   │
│  │ UserProfile  │  │ BillingAccount  │  │   AccountTier   │   │
│  │ - user_id    │  │ - account_id    │  │   (enum)        │   │
│  │ - email      │  │ - tier          │  └─────────────────┘   │
│  │ - account_id │  │ - is_active     │                        │
│  └──────────────┘  └─────────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│                 INFRASTRUCTURE LAYER (Ports)                    │
│                                                                 │
│  ┌──────────────────┐         ┌────────────────────────────┐   │
│  │ UserRepository   │         │ AccountRepository          │   │
│  │ (Firestore)      │         │ (Firestore)                │   │
│  └──────────────────┘         └────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**Key Points:**
- OAuth Web API = **Primary Adapter** (inbound - receives HTTP requests)
- FirebaseAuthAdapter = **Secondary Adapter** (outbound - calls Google OAuth)
- AuthenticationService = **Application Service** (orchestrates OAuth flow)
- Domain entities = **Pure business objects** (no dependencies)

---

## API Contracts

### 1. `GET /auth/login`

**Purpose:** Initiate OAuth flow by redirecting to Google OAuth consent screen.

**Query Parameters:**
- `provider` (optional, string) - OAuth provider name (default: from config)

**Response:**
- **Status:** `302 Found`
- **Headers:** `Location: https://accounts.google.com/o/oauth2/v2/auth?...`
- **Cookies:**
  - `oauth_state` (HttpOnly, 10 min TTL) - CSRF protection token

**Example:**
```bash
curl -i "http://localhost:5001/auth/login?provider=firebase"

HTTP/1.1 302 Found
Location: https://accounts.google.com/o/oauth2/v2/auth?client_id=...&state=...
Set-Cookie: oauth_state=abc123...; HttpOnly; Max-Age=600; SameSite=Lax
```

**Implementation:**
- File: [src/web/oauth_app.py:64-110](../../../src/web/oauth_app.py#L64)
- Generates CSRF state token using `secrets.token_urlsafe(32)`
- Calls `auth_provider.get_authorization_url()` to build OAuth URL
- Stores state in `oauth_state` cookie for validation in callback

---

### 2. `GET /auth/callback`

**Purpose:** Handle OAuth callback after user authorization.

**Query Parameters:**
- `code` (required, string) - Authorization code from OAuth provider
- `state` (required, string) - CSRF protection token (must match cookie)

**Response:**
- **Status:** `200 OK`
- **Body:**
```json
{
  "success": true,
  "user": {
    "user_id": "64235d4b-ac08-4d4b-b504-07568f88aff1",
    "email": "dmytro_es@ddmd13.com",
    "display_name": "Dmytro Deleur"
  },
  "account": {
    "account_id": "9330ff95-2cc8-45c4-a7c7-89e6016f2eb1",
    "tier": "free"
  },
  "access_token": "eyJhbGci..."
}
```

**Cookies Set:**
- `access_token` (HttpOnly, 1 hour TTL) - JWT access token
- `refresh_token` (HttpOnly, 30 days TTL) - JWT refresh token

**Cookies Cleared:**
- `oauth_state` (CSRF token no longer needed)

**Error Codes:**
- `400` - Missing code or invalid state (CSRF validation failed)
- `500` - OAuth flow failed (token exchange error)

**Implementation:**
- File: [src/web/oauth_app.py:115-205](../../../src/web/oauth_app.py#L115)
- **Step 1:** Verify CSRF state token (must match `oauth_state` cookie)
- **Step 2:** Call `auth_service.handle_oauth_callback(code, redirect_uri)`
  - Exchanges code for OAuth tokens
  - Registers new user or authenticates existing user
  - Returns `(UserProfile, BillingAccount, oauth_tokens)`
- **Step 3:** Create JWT session tokens via `session_service`
  - `create_access_token(user, account)` - 1 hour TTL
  - `create_refresh_token(user, account)` - 30 days TTL
- **Step 4:** Set HttpOnly cookies and return JSON response

---

### 3. `POST /auth/refresh`

**Purpose:** Refresh expired access token using refresh token.

**Headers or Cookies:**
- `refresh_token` (cookie or `Authorization: Bearer <token>`)

**Response:**
- **Status:** `200 OK`
- **Body:**
```json
{
  "success": true,
  "access_token": "eyJhbGci..."
}
```

**Cookies Updated:**
- `access_token` - New JWT access token (1 hour TTL)

**Error Codes:**
- `401` - Missing, expired, or invalid refresh token

**Implementation:**
- File: [src/web/oauth_app.py:210-284](../../../src/web/oauth_app.py#L210)
- **Step 1:** Extract refresh token from cookie or `Authorization` header
- **Step 2:** Verify refresh token via `session_service.verify_refresh_token()`
- **Step 3:** Extract `user_id` and `account_id` from JWT payload
- **Step 4:** Create new access token (TODO: fetch user/account from DB)
- **Step 5:** Update `access_token` cookie

**Note:** Currently trusts refresh token payload. In production, should fetch user/account from database to ensure user is still active.

---

### 4. `POST /auth/logout`

**Purpose:** Logout user by clearing session cookies.

**Response:**
- **Status:** `200 OK`
- **Body:**
```json
{
  "success": true,
  "message": "Logged out"
}
```

**Cookies Cleared:**
- `access_token`
- `refresh_token`

**Implementation:**
- File: [src/web/oauth_app.py:289-306](../../../src/web/oauth_app.py#L289)
- Clears session cookies
- **Note:** JWT tokens remain valid until expiration (stateless logout)
- For immediate revocation, implement token blacklist (future enhancement)

---

### 5. `GET /auth/me`

**Purpose:** Get current authenticated user information.

**Headers or Cookies:**
- `access_token` (cookie or `Authorization: Bearer <token>`)

**Response:**
- **Status:** `200 OK`
- **Body:**
```json
{
  "user": {
    "user_id": "64235d4b-ac08-4d4b-b504-07568f88aff1",
    "email": "dmytro_es@ddmd13.com",
    "external_user_id": "firebase|102717594365791155484"
  },
  "account": {
    "account_id": "9330ff95-2cc8-45c4-a7c7-89e6016f2eb1",
    "tier": "free",
    "role": "owner"
  }
}
```

**Error Codes:**
- `401` - Not authenticated or token expired

**Implementation:**
- File: [src/web/oauth_app.py:311-358](../../../src/web/oauth_app.py#L311)
- **Step 1:** Extract access token from cookie or `Authorization` header
- **Step 2:** Verify access token via `session_service.verify_access_token()`
- **Step 3:** Return user/account info from JWT payload

---

### 6. `POST /auth/link-oauth`

**Purpose:** Link Google OAuth identity to existing user.

**Use Case:** User already logged in via Slack/Telegram, wants to add Google OAuth for web UI access.

**Headers or Cookies:**
- `access_token` (cookie or `Authorization: Bearer <token>`) - Current user session

**Request Body:**
```json
{
  "code": "4/0AanRO...",
  "state": "csrf_token_here"
}
```

**Response:**
- **Status:** `200 OK`
- **Body:**
```json
{
  "success": true,
  "message": "Google OAuth linked successfully",
  "user": {
    "user_id": "{{user_id}}",
    "external_user_id": "firebase|102717594365791155484",
    "email": "dmytro_es@ddmd13.com"
  }
}
```

**Cookies Cleared:**
- `oauth_state` (CSRF token no longer needed)

**Error Codes:**
- `400` - Missing code or invalid state
- `401` - Not authenticated (missing or expired access token)
- `409` - OAuth identity already linked to another user
- `500` - Server error

**Implementation:**
- File: [src/web/oauth_app.py:363-461](../../../src/web/oauth_app.py#L363)
- **Step 1:** Verify access token → get current `user_id`
- **Step 2:** Verify CSRF state token
- **Step 3:** Call `auth_service.link_oauth_identity(user_id, code, redirect_uri)`
  - Exchanges code for OAuth tokens
  - Extracts `external_user_id` (e.g., `firebase|102717594365791155484`)
  - Checks if `external_user_id` already linked to another user (409 error if yes)
  - Links `external_user_id` to current user
- **Step 4:** Return success response

---

### 7. `GET /auth/connect-gmail`

**Purpose:** Initiate incremental Gmail OAuth to grant `gmail.readonly` scope.

**Authentication:** Requires valid `access_token` (cookie or `Authorization: Bearer`).

**Flow:** Redirects user to Google's OAuth consent screen. On return, the callback stores credentials in Firestore and triggers a background email indexing job via Cloud Tasks.

**Response:**
- `302 Found` → Google OAuth URL (with `gmail.readonly` scope + CSRF state cookie).
- `401` — Not authenticated.

**Implementation:** `src/web/oauth_app.py` — `connect_gmail()`

---

### 8. `GET /auth/connect-gmail/callback`

**Purpose:** Handle Gmail OAuth callback — exchange authorization code for tokens and persist credentials.

**Authentication:** CSRF state cookie (set by `GET /auth/connect-gmail`).

**Flow:**
1. Validate CSRF state.
2. Exchange `code` for Gmail OAuth tokens via `GmailOAuthService`.
3. Persist `OAuthCredentials` to `oauth_credentials` Firestore collection (keyed by `user_id`).
4. Enqueue `email_indexing` Cloud Tasks job via `WorkerHandler`.
5. Redirect user to Cabinet UI with success indicator.

**Response:**
- `302 Found` → Cabinet UI URL.
- `400` — Invalid or missing state / code.
- `401` — Not authenticated.

**Implementation:** `src/web/oauth_app.py` — `connect_gmail_callback()`

---

## Technology Stack

### Framework
- **Quart** - Async Python web framework (Flask-like API)
- **asyncio** - Async I/O for non-blocking operations
- **CORS Middleware** - Cross-origin requests support

### Authentication
- **JWT (PyJWT)** - Stateless session tokens
  - Access token: 1 hour TTL (short-lived)
  - Refresh token: 30 days TTL (long-lived)
- **HMAC-SHA256** - Token signing algorithm
- **HttpOnly Cookies** - XSS protection
- **CSRF State Tokens** - CSRF protection (`secrets.token_urlsafe`)

### Services Used (Application Layer)

1. **AuthenticationService** ([src/services/authentication_service.py](../../../src/services/authentication_service.py))
   - **Layer:** Application (orchestration)
   - `handle_oauth_callback(code, redirect_uri)` - OAuth flow handler
   - `link_oauth_identity(user_id, code, redirect_uri)` - Link OAuth to existing user
   - Uses: AuthProviderRegistry, UserRepository, AccountRepository

2. **SessionService** ([src/services/session_service.py](../../../src/services/session_service.py))
   - **Layer:** Application (JWT management)
   - `create_access_token(user, account)` - Generate JWT access token
   - `create_refresh_token(user, account)` - Generate JWT refresh token
   - `verify_access_token(token)` - Verify and decode access token
   - `verify_refresh_token(token)` - Verify and decode refresh token

3. **AuthProviderRegistry** ([src/services/auth_provider_registry.py](../../../src/services/auth_provider_registry.py))
   - **Layer:** Application (provider registry)
   - `get_provider(name)` - Get OAuth provider adapter
   - Manages: `FirebaseAuthAdapter` (Google OAuth - Adapter layer)

4. **AuthConfig** ([src/config/auth.py](../../../src/config/auth.py))
   - **Layer:** Infrastructure (configuration)
   - `oauth_redirect_uri` - OAuth callback URL
   - `access_token_ttl` - Access token TTL (3600s)
   - `refresh_token_ttl` - Refresh token TTL (2592000s = 30 days)
   - `oauth_session_secret` - JWT signing key

---

## Security Features

### 1. CSRF Protection
**Mechanism:** State parameter + HttpOnly cookie

```python
# /auth/login
state = secrets.token_urlsafe(32)  # Random CSRF token
response.set_cookie("oauth_state", state, httponly=True, max_age=600)
redirect_to_oauth_provider(state)

# /auth/callback
stored_state = request.cookies.get("oauth_state")
if stored_state != request.args.get("state"):
    return 400  # CSRF validation failed
```

### 2. XSS Protection
**Mechanism:** HttpOnly cookies (not accessible via JavaScript)

```python
response.set_cookie(
    "access_token",
    token,
    httponly=True,  # ✅ JavaScript can't read this
    secure=True,    # ✅ HTTPS only in production
    samesite="lax", # ✅ CSRF protection
)
```

### 3. JWT Token Structure

**Access Token (1 hour):**
```json
{
  "sub": "user_id",
  "iat": 1769890991,
  "exp": 1769894591,
  "account_id": "9330ff95-2cc8-45c4-a7c7-89e6016f2eb1",
  "external_user_id": "firebase|102717594365791155484",
  "role": "owner",
  "tier": "free",
  "email": "dmytro_es@ddmd13.com",
  "type": "access"
}
```

**Refresh Token (30 days):**
```json
{
  "sub": "user_id",
  "iat": 1769890991,
  "exp": 1772482991,
  "account_id": "9330ff95-2cc8-45c4-a7c7-89e6016f2eb1",
  "type": "refresh"
}
```

**Signing:**
- Algorithm: HMAC-SHA256
- Secret: `OAUTH_SESSION_SECRET` from `.env`
- Library: PyJWT

### 4. CORS Configuration
**Current:** Open CORS (`Access-Control-Allow-Origin: *`)
**Production TODO:** Restrict to specific origins

```python
@app.after_request
async def add_cors_headers(response):
    # TODO: Restrict to specific origins in production
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response
```

---

## Integration with Main Application

### Startup Sequence

```python
# main.py:215-250
async def main():
    # 1. Initialize services
    auth_service = AuthenticationService(...)
    session_service = SessionService(...)
    auth_registry = AuthProviderRegistry(...)

    # 2. Create OAuth app
    oauth_app = create_oauth_app(
        auth_service=auth_service,
        session_service=session_service,
        auth_registry=auth_registry,
        auth_config=auth_config,
    )

    # 3. Run OAuth app in parallel with Slack bot
    async def run_oauth_app():
        await oauth_app.run_task(host="0.0.0.0", port=5001)

    asyncio.create_task(run_oauth_app())
    logger.info("✅ OAuth Web App started on port 5001")
```

### Dependency Graph

```
main.py
  └─> create_oauth_app()
       ├─> AuthenticationService (domain logic)
       │    ├─> AuthProviderRegistry
       │    │    └─> FirebaseAuthAdapter
       │    ├─> UserRepository (Firestore)
       │    └─> AccountRepository (Firestore)
       │
       ├─> SessionService (JWT management)
       │    └─> AuthConfig (TTL, secret)
       │
       └─> AuthConfig (.env configuration)
```

---

## Environment Configuration

### Required `.env` Variables

```bash
# --- FIREBASE / OAUTH CONFIG ---
USE_OAUTH_COLLECTIONS=true
FIREBASE_PROJECT_ID=YOUR_GCP_PROJECT_ID
FIREBASE_WEB_API_KEY=YOUR_FIREBASE_WEB_API_KEY
GOOGLE_APPLICATION_CREDENTIALS=/path/to/firebase-admin-key.json
OAUTH_REDIRECT_URI=http://localhost:5001/auth/callback
OAUTH_SESSION_SECRET=YOUR_RANDOM_SESSION_SECRET

# --- GOOGLE OAUTH CONFIG ---
GOOGLE_OAUTH_CLIENT_ID=YOUR_OAUTH_CLIENT_ID.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=YOUR_OAUTH_CLIENT_SECRET
```

### OAuth Client Configuration (GCP Console)

**Authorized JavaScript Origins:**
- `http://localhost:5001` (local dev)
- `http://127.0.0.1:5001` (local dev)
- `https://your-domain.com` (production)

**Authorized Redirect URIs:**
- `http://localhost:5001/auth/callback` (local dev)
- `https://your-cloud-run-url.run.app/auth/callback` (Cloud Run)
- `https://your-domain.com/auth/callback` (production)

---

## Testing

### Manual Testing Flow

```bash
# 1. Start bot
python main.py

# 2. Open OAuth login in browser
open "http://localhost:5001/auth/login"

# 3. Sign in with Google account

# 4. Verify callback response (JSON)
{
  "success": true,
  "user": { "user_id": "...", "email": "...", "display_name": "..." },
  "account": { "account_id": "...", "tier": "free" },
  "access_token": "eyJhbGci..."
}

# 5. Test /auth/me endpoint
curl -b cookies.txt "http://localhost:5001/auth/me"

# 6. Test token refresh
curl -X POST -b cookies.txt "http://localhost:5001/auth/refresh"

# 7. Test logout
curl -X POST -b cookies.txt "http://localhost:5001/auth/logout"
```

### Verification Script

```bash
python verify_oauth_user.py
```

**Checks:**
- ✅ UserProfile created in `development_users_oauth`
- ✅ BillingAccount created in `development_accounts_oauth`
- ✅ JWT token structure matches RFC
- ✅ `external_user_id` format: `firebase|<uid>`
- ✅ Master Account First (account created before user)

---

## Future Enhancements

### 1. Token Blacklist (Immediate Logout)
**Problem:** JWT tokens remain valid until expiration (stateless).
**Solution:** Implement Redis-based token blacklist for `/auth/logout`.

### 2. Refresh Token Rotation
**Problem:** Long-lived refresh tokens pose security risk.
**Solution:** Issue new refresh token on each `/auth/refresh` call.

### 3. Multi-Provider Support
**Current:** Only Firebase/Google OAuth.
**Future:** Add GitHub, Microsoft, Apple OAuth adapters.

### 4. Rate Limiting
**Problem:** No rate limiting on OAuth endpoints.
**Solution:** Add Flask-Limiter or similar middleware.

### 5. Audit Logging
**Problem:** No audit trail for OAuth events.
**Solution:** Log all authentication events to Firestore/CloudLogging.

---

## When to Update This Document

Update this document when:
- ✅ New OAuth endpoint added
- ✅ JWT token structure changed
- ✅ Security mechanism modified (CSRF, cookies, etc.)
- ✅ OAuth provider added/removed
- ✅ Service dependencies changed
- ✅ Configuration requirements changed

---

## References

- [MULTI_TENANT_OAUTH_RFC.md](../../10_rfcs/MULTI_TENANT_OAUTH_RFC.md) - Full OAuth architecture
- [OAUTH_GCP_SETUP_GUIDE.md](../../guides/OAUTH_GCP_SETUP_GUIDE.md) - GCP setup instructions
- [src/web/oauth_app.py](../../../src/web/oauth_app.py) - Implementation
- [src/services/authentication_service.py](../../../src/services/authentication_service.py) - Auth logic
- [src/services/session_service.py](../../../src/services/session_service.py) - JWT management

---

**See also:** [User Cabinet](../user_cabinet/README.md) — authenticated self-service portal (platform linking, facts browser, semantic search). Uses the same JWT mechanism but lives in a separate blueprint (`user_cabinet_app.py`).

---

**Last Updated:** 2026-03-02
**Status:** ✅ Implemented and verified
