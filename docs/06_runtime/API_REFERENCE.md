# API Reference

**Type:** Internal HTTP API (Quart / async Python)
**Auth:** JWT (HS256), HttpOnly cookie `access_token` or `Authorization: Bearer <token>`

All user-facing endpoints require authentication unless noted. Invalid/expired token → `401`.

---

## Auth (`/auth/*`)

> Full contract: [OAuth Web API](../05_building_blocks/oauth_web_api/README.md)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/auth/login` | ❌ | Redirect to Google OAuth consent |
| `GET` | `/auth/callback` | ❌ | OAuth callback; sets `access_token` cookie |
| `POST` | `/auth/refresh` | cookie | Refresh access token via refresh token |
| `POST` | `/auth/logout` | cookie | Revoke tokens, clear cookies |
| `GET` | `/auth/me` | ✅ | Current user + account info |
| `POST` | `/auth/link-oauth` | ✅ | Link additional OAuth provider |

---

## User Cabinet (`/api/user/*`)

> Full contract: [User Cabinet](../05_building_blocks/user_cabinet/README.md)

### Platform Identities

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/user/platforms` | Current platform link state (Slack, Telegram) |
| `POST` | `/api/user/link-platform` | Link Slack identity |
| `DELETE` | `/api/user/link-platform?platform=slack` | Unlink platform |
| `POST` | `/api/user/link-telegram` | Link Telegram identity |

### Facts

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/user/facts/browse` | Cursor-paginated facts (`limit`, `cursor`, `domain`) |
| `POST` | `/api/user/facts/search` | Semantic vector search (`{ "query": "..." }`) |
| `POST` | `/api/user/facts/{fact_id}/invalidate` | Mark fact as invalid — immediate, no LLM |

### Team Invites

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/user/invite-codes` | ✅ | List active codes |
| `POST` | `/api/user/invite-codes` | owner | Generate new invite |
| `POST` | `/api/user/join-team` | ✅ | Consume invite code |

---

## Web UI

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/cabinet` | cookie | User Cabinet SPA |
| `GET` | `/join?code=XYZ` | cookie / redirect | Team invite deep link |
| `GET` | `/cabinet/docs` | owner-only | ARC42 documentation (hidden URL) |

---

## Health

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | ❌ | Liveness probe |

---

## Notes

**No public API.** All endpoints are internal — consumed by the cabinet SPA or platform bots (Slack, Telegram). No versioning prefix (`/v1/`) — breaking changes handled via deployment.

**Two auth mechanisms:**
- Web UI → `access_token` HttpOnly cookie (set by `/auth/callback`)
- API clients → `Authorization: Bearer <token>` header

**Fact write path split:**
- Invalidation → direct API (`POST /invalidate`), bypasses ConsolidationAgent
- Creation / correction → goes through ConsolidationAgent pipeline (not exposed as API)
