# RFC: Channel Binding — Direct Agent Sessions via Slack Channels

**Status:** DRAFT
**Date:** 2026-04-06
**Owner:** Dmytro

**Related:** lazy loading (AgentFactoryPort), agent_manifest.py (AgentDescriptor)

---

## 1. Problem Statement

All messages route through Router → Quick/Smart. Three limitations:

- **No direct agent access.** To use Deep Research, the user must phrase a request
  correctly for Smart to delegate. Misrouted requests waste tokens and time.
- **Single session.** One user = one conversation context. No way to have parallel
  thematic discussions (work analysis in one place, personal in another).
- **Session pollution.** A 50-message translation session pollutes conversation
  history, biographical cache, and consolidation pipeline.

**Desired outcome:** Slack channels are first-class session boundaries. A channel can be
permanently bound to an agent — all messages in that channel go directly to that agent,
bypassing Router. Multiple unbound channels work as independent Smart sessions.

---

## 2. Core Concept

**`channel_id` is the routing key. A binding overrides the default route.**

```
#translators    →  binding: translator_agent  →  TranslatorAgent directly
#deep-research  →  binding: deep_research     →  DeepResearchAgent directly
#work-analysis  →  binding: null              →  Router → Smart (separate session)
#personal (DM)  →  binding: null              →  Router → Smart (main session)
```

Binding = one record: `channel_id → agent_type`. Null binding = normal flow.

Multiple unbound channels = multiple independent Smart sessions — this works today
because `session_id` already derives from `channel_id`/`thread_ts`. No changes needed.

---

## 3. Design

### 3.1 ChannelBinding — domain type

```python
# src/domain/channel_binding.py
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ChannelBinding:
    channel_id: str
    agent_type: str            # maps to AgentDescriptor.agent_type
    intent: str                # primary intent to call on delegation
    created_by: str            # user_id who activated
```

### 3.2 Storage — Firestore + in-memory cache

Collection: `{env_prefix}channel_bindings`, keyed by `channel_id`.

```
channel_bindings/
  C0123TRANSLATOR/
    channel_id: "C0123TRANSLATOR"
    agent_type: "translator"
    intent: "translate"
    created_by: "user_abc123"
```

In-memory cache in `ChannelBindingService` with TTL (5 min). Cache miss → Firestore
lookup. Bindings are rare-write, frequent-read — caching is trivial.

Firestore persistence guarantees bindings survive Cloud Run redeploys.

### 3.3 Port

```python
# src/ports/channel_binding_port.py
class ChannelBindingPort(ABC):
    @abstractmethod
    async def get(self, channel_id: str) -> Optional[ChannelBinding]: ...

    @abstractmethod
    async def save(self, binding: ChannelBinding) -> None: ...

    @abstractmethod
    async def delete(self, channel_id: str) -> None: ...
```

Port justified: Firestore in prod, dict in tests, potential Redis in future.

### 3.4 Routing — one branch in ConversationHandler

```python
# ConversationHandler.handle_message(), before Router dispatch
channel_id = context.metadata.get("channel") or context.session_id
binding = await self._channel_binding_service.get(channel_id)

if binding:
    await self._handle_bound_channel(context, response_channel, binding)
    return
# ... existing Router flow unchanged (unbound channels ALWAYS go through Router)
```

**Bound channels** bypass Router entirely — the routing decision is already made
(channel → agent). `_handle_bound_channel`:
1. `ensure_agents_for_user(user_id)` — standard
2. `coordinator.handle_delegation(intent=binding.intent, query=context.text, context=...)` — same path as Smart delegation
3. Deliver response
4. **No consolidation** — bound channels never feed the consolidation pipeline
5. Session store write — TBD per agent needs (Phase 2)

**Unbound channels** always go through the full Router pipeline — semantic lens
enrichment, complexity triage, Quick/Smart selection. This is the existing flow and
must not change. Multiple unbound channels = multiple independent Router sessions,
each with their own session history and enrichment.

**Multi-user bound channels:** `user_id` comes from the Slack event, not the channel.
`ensure_agents_for_user(user_id)` creates the agent for the specific user who sent
the message. Each user in the channel gets their own agent instance.

### 3.5 Session behaviour — bound vs unbound

| | Unbound channel | Bound channel |
|---|---|---|
| **History source** | SessionStore (Firestore) | Platform API (Slack `conversations.history`) |
| **Session write** | Yes (normal flow) | No |
| **Consolidation** | Yes (normal flow) | Never |
| **Bio context** | Yes (Router enrichment) | Agent decides (most skip) |

Bound channels are stateless from the system's perspective. The platform chat IS
the session. On each message, `_handle_bound_channel` fetches the last N messages
via Slack API and passes them as conversation history to the agent. No Firestore
reads or writes for history.

This makes bound channels inherently incognito-like: no persistent trace in the
system beyond billing counters and application logs.

### 3.5 Commands

```
$agent translator     →  bind this channel to translator_agent
$agent off            →  unbind this channel, return to normal flow
$agent                →  show current binding (or "not bound")
$primary              →  set this channel as primary notification destination
```

Handled in `ConversationHandler.handle_command()`.

**`$agent <type>` validation:**
- Agent type must exist in `AgentRegistry`
- User's account must have this agent_type in `allowed_direct_agents` (Phase 4)

**`$primary` validation:**
- Channel must not be bound (bound channels cannot be primary)
- Only one primary per user (overwrite previous)

### 3.6 Notification Delivery — primary channel + origin tracking

**Current behaviour:** `UserNotificationService` saves "last active channel" on every
message. All async results and system notifications go there. This is an implicit
heuristic that breaks with multiple channels.

**Problems:**
- Deep Research requested in #research, then user writes in DM → result arrives in DM
- User chats in #work-analysis → reminders, daily review, billing all go there
- User chats in #translator (bound) → system notifications leak into a utility channel

**Fix: two explicit mechanisms replace the implicit heuristic.**

#### 3.6.1 Primary Channel — system notifications destination

`primary_channel` is a user-level setting: the channel where system-initiated
notifications are delivered (reminders, daily email review, billing, scheduled tasks).

```python
# NotificationStatePort — extended
async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None: ...
async def get_primary(self, user_id: str) -> Optional[NotificationChannel]: ...
```

**Rules:**
- DM (first connected channel) = primary by default
- `$primary` in any unbound channel → that channel becomes primary
- Bound channels cannot be primary (reject with error)
- `last_active_channel` behaviour removed from ConversationHandler — no more
  implicit save on every message

**Migration:** existing users have `last_active_channel` stored. Until they run
`$primary`, the system falls back to stored `last_active_channel`. After first
`$primary`, the explicit value takes over.

#### 3.6.2 Origin Channel — async result delivery

Async task context carries `origin_channel_id` — the channel where the user
initiated the request.

```python
# In ConversationHandler, when building AgentMessage context:
context={
    ...
    "origin_channel_id": channel_id,    # where to deliver async results
    "origin_platform": response_channel.platform,
}
```

Propagation:
- `coordinator.handle_delegation()` → context passed to Cloud Task payload
- `WorkerHandler` → `AgentWorkerHandler` → on completion, reads `origin_channel_id`
- `UserNotificationService.notify()` — new optional parameter:
  `channel_id_override: Optional[str] = None`. When set, delivers to that channel
  instead of primary.

#### 3.6.3 Notification routing table

| Notification type | Destination | Example |
|-------------------|-------------|---------|
| Async result with `origin_channel_id` | origin channel | Deep Research result → #research |
| Async result without origin | primary channel | Legacy tasks in flight during migration |
| Reminder | primary channel | "Check project deadline" → DM |
| Daily email review | primary channel | Morning briefing → DM |
| Billing summary | primary channel | Token usage → DM |

Bound channels never receive system notifications (they don't update primary,
and origin is only set when the user explicitly sends a message there).

---

## 4. What This Enables

### Today (Phase 1 — channel binding + notification rework)
- `$agent translator` in #translators — permanent binding
- `$agent research` in #research — direct Deep Research access
- Multiple unbound channels — parallel Smart sessions
- `$primary` — explicit notification destination
- Async results delivered to origin channel, system notifications to primary

### Phase 2 — forced orchestrator routing
`$route smart` / `$route quick` / `$route auto` — per-channel override for Router
triage decision. Unbound channels only. Message still goes through full pipeline
(enrichment, consolidation, session store) — only the Quick/Smart selection is forced.
Use case: long thematic discussion that always needs Smart, without Router triage overhead.

### Phase 3 — incognito (minimal delta)
Bound channels already provide most of incognito: no SessionStore, no consolidation.
Remaining gap: debug prompts in GCS and user content in application logs. This is a
small addition (flag on binding or `$agent translator --incognito`), not a separate RFC.
Adds: suppress `PromptDebugLogger` output, strip content from billing records,
sanitize `_on_agent_start`/`_on_agent_success` log messages.

### Phase 4 — account permissions
`UserBotConfig.allowed_direct_agents: List[str]` — which agent types each
account can activate via `$agent`. Admin controls access per-account via Cabinet UI.

---

## 5. Strengths

1. **Minimal code change.** One branch in `handle_message()`, one command in
   `handle_command()`, one Firestore collection, one port. No new handlers,
   services, or routing infrastructure.

2. **Reuses everything.** `coordinator.handle_delegation()` already handles
   agent resolution, lazy loading, sync/async dispatch. Bound channel just
   provides a different trigger (command vs LLM decision).

3. **Platform-agnostic core.** `channel_id` comes from MessageContext metadata.
   On Slack it's a channel. On Telegram it's a chat_id. Core doesn't care.

4. **Persistent.** Firestore bindings survive redeploys. Create channel once,
   bind once, use forever.

5. **Multi-user.** Multiple users in one Slack channel = all get the bound
   agent. Channel membership IS access control.

6. **Parallel sessions for free.** Multiple unbound channels already work as
   separate sessions (session_id derives from channel). This just makes
   it explicit and adds agent override.

7. **Notification model fixed.** Replaces implicit "last active" heuristic with two
   explicit mechanisms: `primary_channel` (system notifications) and
   `origin_channel_id` (async results). Fixes existing bug where async results
   go to the wrong channel. Backward-compatible: `last_active` used as fallback
   until user sets `$primary`.

---

## 6. Weaknesses and Risks

### 6.1 Channel proliferation

20 agents = potentially 20 channels. Slack workspace gets cluttered.

**Mitigation:** This is a UX choice, not a technical problem. User creates only the
channels they actually use. Most agents will have 1-2 permanent channels. Slack
channel sections/folders help organize.

**Risk level:** LOW.

### 6.2 Consolidation rule

Bound channels never consolidate. This is a hard rule, not configurable. If an
edge case emerges where a bound agent should contribute to long-term memory,
an opt-in flag can be added to `AgentDescriptor` later. Until then — simplicity wins.

**Risk level:** LOW. No known use case for bound-channel consolidation.

### 6.3 Binding validation

`$agent nonexistent_type` should fail gracefully. Agent type must exist in registry.
For lazy agents, the agent class must be creatable (dependencies available).

**Mitigation:** Validate against `AgentRegistry` at bind time. Return clear error
if agent type unknown or unavailable. Don't persist invalid bindings.

**Risk level:** LOW.

### 6.4 Concurrent binding changes

Two users in the same channel run `$agent translator` and `$agent research`
simultaneously. Last write wins — Firestore upsert.

**Mitigation:** Acceptable for single-user system. For multi-user: binding
commands are admin-only (creator controls the channel). Or: reject re-bind
if already bound (`$agent off` first).

**Risk level:** LOW.

---

## 7. Files Created / Changed

### New files
- `src/domain/channel_binding.py` — `ChannelBinding` dataclass
- `src/ports/channel_binding_port.py` — `ChannelBindingPort` ABC
- `src/adapters/firestore_channel_binding.py` — Firestore implementation
- `src/services/channel_binding_service.py` — cache + CRUD facade
- `src/adapters/slack/channel_history.py` — Slack API history fetch → `List[Message]`
- `tests/unit/services/test_channel_binding_service.py`

### Modified files
- `src/handlers/conversation_handler.py` — bound channel routing branch in
  `handle_message()`, `$agent` / `$primary` commands in `handle_command()`,
  `origin_channel_id` in AgentMessage context, remove implicit `save_channel()`
- `src/ports/notification_state_port.py` — add `save_primary()` / `get_primary()`
- `src/adapters/firestore_notification_state.py` — implement new methods
- `src/services/user_notification_service.py` — `channel_id_override` parameter
  on `notify()` and `notify_raw()`, fallback chain: origin → primary → last_active
- `src/handlers/worker_handler.py` — pass `origin_channel_id` from Cloud Task
  payload to notification delivery
- `main.py` — wire `ChannelBindingService` into ConversationHandler
- `src/composition/service_container.py` — create Firestore adapter

### Not changed
- `src/infrastructure/agent_coordinator.py` — untouched
- `src/infrastructure/agent_registry.py` — untouched (no new fields)
- `src/infrastructure/agent_manifest.py` — untouched (agents are normal agents)
- Agent classes — untouched (they don't know about bindings)

---

## 8. Rollout Plan

**Phase 1 — Channel binding + notification rework**
1. `ChannelBinding` domain type
2. `ChannelBindingPort` + Firestore adapter
3. `ChannelBindingService` (cache + CRUD)
4. Platform history source (Slack `conversations.history` → `List[Message]`)
5. `ConversationHandler`: bound channel branch (no session write, no consolidation,
   history from platform API) + `$agent` / `$primary` commands
6. Remove implicit `save_channel()` on every message from ConversationHandler
7. `NotificationStatePort`: `save_primary()` / `get_primary()` methods
8. `$primary` command: explicit primary channel setting
9. `origin_channel_id` in AgentMessage context
10. `UserNotificationService`: `channel_id_override` + fallback chain
    (origin → primary → last_active legacy)
11. `WorkerHandler`: propagate origin channel to notification

**Phase 2 — Isolation flags**
8. Add flags to `ChannelBinding` (consolidation, bio, session store)
9. ConversationHandler: check flags before enrichment/persistence

**Phase 3 — Incognito (separate RFC)**
10. No debug GCS, billing content stripping, log sanitization

**Phase 4 — Account permissions**
11. `allowed_direct_agents` on UserBotConfig
12. Cabinet UI for admin management

---

## 9. Open Questions

1. **Binding scope.** Can the same channel be rebound to a different agent
   (`$agent translator` then `$agent research`)? Proposed: yes, `$agent off`
   first then rebind. Or direct rebind overwrites.

2. **Default intent.** Each binding needs an `intent` for `handle_delegation()`.
   For agents with one capability (translator → translate) this is obvious.
   For agents with multiple (compute → compute_math, compute_finance, etc.) —
   use the "generic" intent (`compute`), or let the agent decide internally?

3. **File attachments.** Bound channel messages go through the normal
   ConversationHandler flow — file processing already works. No special
   handling needed unless isolation flags (Phase 2) skip file upload.

4. **Primary channel and Telegram.** Primary channel is persisted with platform
   info. If user uses both Slack and Telegram, should there be one primary
   per platform, or one global primary? Proposed: one global primary —
   notifications go to one place.

5. **Unbinding a primary channel.** If `$agent translator` is run on a channel
   that is currently primary — reject? Or auto-move primary to DM?
   Proposed: reject with message "Run `$primary` in another channel first."
