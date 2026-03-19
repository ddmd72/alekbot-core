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
| `GET` | `/auth/connect-gmail` | ✅ | Redirect to Google Gmail OAuth consent (gmail.readonly scope) |
| `GET` | `/auth/connect-gmail/callback` | ❌ | Gmail OAuth callback; stores credentials in `oauth_credentials` |
| `GET` | `/auth/connect-microsoft-todo` | ✅ | Redirect to Azure consumers OAuth (Tasks.ReadWrite offline_access) |
| `GET` | `/auth/connect-microsoft-todo/callback` | ❌ | MS OAuth callback; stores credentials + enqueues `setup_microsoft_todo` Cloud Task |

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
| `GET` | `/api/user/facts` | Latest facts (simple list, no cursor) |
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

## Gmail Email Indexing (`/api/gmail/*`)

All Gmail endpoints require authentication. Gmail must be connected first via `/auth/connect-gmail`.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/gmail/status` | ✅ | Connected Gmail account info + `IndexingState` cursor |
| `POST` | `/api/gmail/index` | ✅ | Trigger new indexing job (`{ "mode": "incremental" \| "backfill" \| "reindex" }`) |
| `GET` | `/api/gmail/jobs/{job_id}` | ✅ | Get indexing job status and stats |
| `POST` | `/api/gmail/jobs/{job_id}/cancel` | ✅ | Cancel a running indexing job |
| `DELETE` | `/api/gmail/disconnect` | ✅ | Revoke Gmail OAuth tokens and remove credentials |
| `DELETE` | `/api/gmail/data` | ✅ | Delete all indexed email data for this user |

**Job lifecycle:** Cabinet triggers `POST /api/gmail/index` → job created → Cloud Tasks dispatches paginated `email_indexing` tasks → each page processed by `WorkerHandler` → re-enqueued if `next_page_token` present → on completion: `UserNotificationService` sends Slack/Telegram alert.

---

## Microsoft Tasks Webhook

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/webhook/microsoft-tasks/{user_id}` | clientState | Graph API change notifications for task create/update/delete. `?validationToken=XYZ` → echo for subscription validation. Body: Graph notification payload; `clientState` checked against `MICROSOFT_TASKS_WEBHOOK_SECRET`. |

**Flow:** User edits task in MS To Do app → Graph sends POST here → `task_indexing.index_task_by_ref()` or `deindex_task()` → Firestore search index updated. Also triggers self-healing subscription renewal.

---

## Microsoft Tasks Cabinet API (`/api/tasks/microsoft/*`)

All Tasks endpoints require authentication. Microsoft To Do must be connected first via `/auth/connect-microsoft-todo`.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/tasks/microsoft/status` | ✅ | Integration status: `{connected: bool, subscriptions: [{list_id, expires_at}]}` |
| `POST` | `/api/tasks/microsoft/reindex` | ✅ | Trigger full reindex of all task lists (enqueues Cloud Tasks) |
| `GET` | `/api/tasks/microsoft/lists` | ✅ | List all MS To Do task lists for the user (proxies Graph API) |
| `DELETE` | `/api/tasks/microsoft/disconnect` | ✅ | Delete all Graph subscriptions, revoke OAuth tokens, clear search index |

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
