# RFC: Platform Session Isolation

**Status:** SUPERSEDED by per-channel session_id in [BOUND_THREAD_AGENTS_RFC.md](BOUND_THREAD_AGENTS_RFC.md) Phase 2 (2026-04-07)
**Created:** 2026-03-29
**Scope:** Session management, notification delivery, consolidation overflow

> **Superseded.** Session ID is now `f"{user_id}:{channel_id}"` — resolved deterministically
> in Slack and Telegram adapters with no Firestore query. Since Slack channel IDs (C.../D...)
> and Telegram chat IDs are naturally disjoint, platform isolation is automatic. The namespace
> prefix approach proposed below was never implemented. See BOUND_THREAD_AGENTS_RFC.md § 8
> Phase 2 for the actual implementation.

---

## 1. Problem Statement

Slack and Telegram currently share a single conversation session per user. Both adapters call `get_latest_session_id(user_id)` which returns the most recently active session document regardless of which platform created it. The `Session` domain model has no `platform` field.

**Consequences:**

- A message sent on Telegram is visible in context when the user next opens Slack, and vice versa.
- The agent's context window is filled with history from both platforms simultaneously — unrelated casual exchanges from one platform pollute focused work on the other.
- There is no way to have a "fresh start" on one platform without affecting the other.
- Background notifications delivered to one platform load context from the shared session, which may contain history from the other platform.

For a personal exocortex the platforms represent different interaction modes (e.g. mobile/quick vs desktop/deep). Shared context is the wrong default.

---

## 2. Current Architecture

### Session ID resolution

Both adapters resolve the session with the same function:

```python
# src/adapters/slack/http_adapter.py  L256-258
# src/adapters/telegram/webhook_adapter.py  L88-102
async def _resolve_session_id(self, user_id: str) -> str:
    latest = await self.session_store.get_latest_session_id(user_id)
    return latest or user_id
```

Socket mode (development) sets `session_id = user_id` directly — no lookup.

### Session storage schema

```
Firestore collection: {env}_sessions
  document id: {session_id}   ← arbitrary string, often a UUID
    owner_id:      "{user_id}"          ← no platform field
    last_activity: timestamp
    history:       [Message, ...]
    expires_at:    now + 90 days
```

`get_latest_session_id(owner_id)` queries:
```
WHERE owner_id == user_id
ORDER BY last_activity DESC
LIMIT 1
```

Returns the single latest document regardless of platform.

### Notification session context

`UserNotificationService.notify()` resolves the session for the background notification agent:

```python
# src/services/user_notification_service.py  L147
effective_session_id = session_id if session_id is not None else user_id
```

Falls back to raw `user_id` — loads whatever session `user_id` maps to (which is the mixed cross-platform session).

`notify_document_link()` saves the document delivery event to history using `user_id` as session_id directly:

```python
# src/services/user_notification_service.py  L266
await self._session_store.append_messages_batch(
    session_id=user_id,
    ...
)
```

### Consolidation overflow

`FirestoreSessionStore.append_messages_batch()` extracts batches when `len(history) > max_history_length` (default 200 messages) and fires the overflow callback. Extracted batches go to consolidation → facts written to shared vector DB.

Currently: both Slack and Telegram messages accumulate in the same session, triggering a single overflow. The consolidation agent sees interleaved multi-platform history.

---

## 3. Proposed Solution: Namespace-Based Platform Isolation

### Core idea

Prefix `owner_id` in the session document with the platform name:

```
owner_id: "slack:{user_id}"      # Slack session
owner_id: "telegram:{user_id}"   # Telegram session
```

The session_id (document key) remains an arbitrary UUID as today. Only `owner_id` changes. `get_latest_session_id` queries by `owner_id` — no schema or index changes needed.

### Changes required

**1. Adapters — session resolution**

```python
# Slack HTTP adapter
async def _resolve_session_id(self, user_id: str) -> str:
    namespaced = f"slack:{user_id}"
    latest = await self.session_store.get_latest_session_id(namespaced)
    return latest or namespaced

# Slack socket adapter (dev mode)
context = MessageContext(
    session_id=f"slack:{user_id}",
    ...
)

# Telegram webhook adapter
async def _resolve_session_id(self, user_id: str) -> str:
    namespaced = f"telegram:{user_id}"
    latest = await self.session_store.get_latest_session_id(namespaced)
    return latest or namespaced
```

**2. Notification service — session context**

`notify()` already loads `channel_info` (platform + channel_id) before creating the response channel. The same `channel_info.platform` should determine which session the agent uses:

```python
# src/services/user_notification_service.py
effective_session_id = (
    session_id
    if session_id is not None
    else f"{channel_info.platform}:{user_id}"
)
```

`notify_document_link()` currently writes to `session_id=user_id`. Should write to `f"{channel_info.platform}:{user_id}"` — the link delivery event should appear in the history of the platform where it was delivered.

**3. Socket mode session_id initial value**

Socket mode sets `session_id = user_id` directly without a lookup. This needs to become `session_id = f"slack:{user_id}"` (or resolved via `_resolve_session_id` like HTTP mode does).

### What does NOT change

- `SessionStore` port interface — `get_latest_session_id(owner_id: str)` signature unchanged.
- `FirestoreSessionStore` implementation — no changes.
- Firestore indexes — the query is still `WHERE owner_id == ?`.
- Consolidation logic — batches from `slack:{user_id}` and `telegram:{user_id}` both write facts to the same shared vector DB. Long-term memory remains unified.
- `NotificationStatePort` — stores `(user_id, platform, channel_id)` as today.
- `Session` domain model — no `platform` field needed; the namespace is in `owner_id`.

---

## 4. Overflow Behavior After Separation

With isolation, each platform accumulates history independently:

- `slack:{user_id}` session fills to 200 messages → overflow → consolidation batch
- `telegram:{user_id}` session fills to 200 messages → overflow → consolidation batch

Both batches write facts to the same `domain_facts` collection. The consolidation agent sees platform-homogeneous history per batch (all Slack or all Telegram), which is cleaner for fact extraction than the current interleaved input.

**Practical effect:** Low-volume Telegram usage (quick questions, reminders) will rarely trigger overflow on its own. The Telegram session will accumulate slowly. Slack (where heavier usage typically happens) overflows on its own schedule. There is no cross-contamination.

---

## 5. Notification Delivery: Full Picture

Background events (reminders, deep research, async documents, email review) are delivered to the user's last active channel via `NotificationStatePort`. The platform of that channel is already known.

With isolation:

| Scenario | Delivery platform | Session used for agent context |
|---|---|---|
| User active on Slack, reminder fires | Slack | `slack:{user_id}` |
| User active on Telegram, reminder fires | Telegram | `telegram:{user_id}` |
| Deep research triggered from Telegram | Telegram | `telegram:{user_id}` (session_id passed through at trigger time) |
| Daily email review (scheduled, no trigger context) | Last active platform | `{platform}:{user_id}` derived from channel_info |

For deep research: the `session_id` is captured at the time the user triggers the request and stored in the Cloud Task payload. With isolation this captured `session_id` is already namespaced (e.g. `telegram:{user_id}`) because it came from the Telegram adapter. No change needed in `AgentWorkerHandler`.

For scheduled tasks (reminders, daily email review) that have no originating session: the notification service derives the session from `channel_info.platform` at delivery time. The agent gets appropriate platform context.

---

## 6. Migration

Existing sessions in Firestore have `owner_id = "{user_id}"` (no platform prefix). After deploying the change, `get_latest_session_id("slack:{user_id}")` will find no documents and return `None` → fallback to `"slack:{user_id}"` as the new session_id. A fresh session is created. The old session (with unnamespaced `owner_id`) becomes orphaned — it will expire via TTL (90 days) and be cleaned up automatically.

**Effect on users:** On first message after deployment, each platform starts a fresh session. Long-term memory (consolidated facts) is fully intact — the exocortex still knows everything, just the short-term conversation context resets. This is acceptable and arguably desirable (clean separation).

**Optional migration script:** If preserving existing conversation context matters, a script can:
1. Query all sessions where `owner_id` does not contain `:`
2. For each session, examine the `last_activity` and infer platform (not reliably possible without a platform field)
3. Or simply fork: copy each session to both `slack:{user_id}` and `telegram:{user_id}`

Given that the session TTL is 90 days and conversation context has limited long-term value (facts are already consolidated), the clean-break approach is preferred.

---

## 7. Open Questions

**Q1: What happens to users who only use one platform?**
No change in behaviour. If user only uses Slack, they get a `slack:{user_id}` session. Equivalent to today's `{user_id}` session.

**Q2: What if user wants to share context across platforms intentionally?**
Long-term memory (vector search) already provides this. If user says "remember X" on Telegram, it gets consolidated and is retrievable on Slack via `search_memory`. Explicit cross-platform short-term context sharing can be a future feature (e.g. `$sync-context` command that copies last N messages from one platform session to another).

**Q3: Session isolation for account-level (team) use cases?**
Out of scope for this RFC. Team accounts (multiple users sharing one account) with Slack channel isolation is tracked separately.

**Q4: Namespace separator — colon vs underscore?**
`slack:{user_id}` uses `:` which is not a valid Firestore document ID character in some contexts but is safe in a field value (`owner_id` is a field, not a document ID). The document ID remains a UUID as today. No issue.

---

## 8. Affected Files

| File | Change |
|---|---|
| `src/adapters/slack/http_adapter.py` | `_resolve_session_id` — namespace with `slack:` |
| `src/adapters/slack/socket_adapter.py` | `session_id = f"slack:{user_id}"` |
| `src/adapters/telegram/webhook_adapter.py` | `_resolve_session_id` — namespace with `telegram:` |
| `src/services/user_notification_service.py` | `notify()` and `notify_document_link()` — derive session from platform |

No changes to ports, domain models, Firestore adapters, or consolidation logic.
