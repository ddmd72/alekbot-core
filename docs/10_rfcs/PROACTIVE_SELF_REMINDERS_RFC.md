# RFC: Proactive Self-Reminders

**Status:** IMPLEMENTED
**Date:** 2026-03-22
**Replaces:** AGENT_NOTES_RFC.md (superseded — do not use)

---

## 1. Problem

The current self-reminder mechanism (`manage_self_reminders`) is **passive**: reminders surface
only when the user starts a conversation, because RouterAgent injects them into context. This
creates two real failures:

1. **"Remind me in 2 hours"** — the bot has no way to fire a reminder after 2 hours if the user
   hasn't written anything. It can create the note, but can't act on it proactively.

2. **"Tell me Valencia news every morning"** — the LLM cannot confidently use the reminder
   mechanism for recurring tasks because it doesn't understand that the reminder will *actually
   execute* at the right time. Without a clear mental model, it invents complex workarounds or
   gives up.

The root cause is semantic: the LLM treats reminders as "a sticky note I might see later" instead
of "a message I'm sending to my future self that will execute regardless of whether the user writes."

---

## 2. Design Goal

**A reminder is a deferred instruction.** When it fires, the system initiates a full agent
pipeline with the reminder's `instruction` as input — exactly as if the user had written that
text themselves. The bot gets to use its full toolset.

A reminder has two distinct text fields:

| Field | Purpose | Length | Audience |
|-------|---------|--------|----------|
| `text` | Short display label | ≤15 words | RouterAgent passive injection, transparency notifications, working memory context |
| `instruction` | Full execution context | No limit | Sent to QuickAgent when the reminder fires |

**Why two fields:** The passive injection (RouterAgent context) must stay compact — it's
injected into every prompt turn. The execution instruction must be complete — it's the only
context available when the reminder fires (no conversation history, no user message).

Example:
```
text:        "Send Valencia morning news briefing"
instruction: "The user asked to receive a daily morning news briefing about Valencia, Spain.
              Search for today's news: events, weather, cultural highlights. Format as a
              concise morning briefing. Send it to the user directly."
```

---

## 3. Architecture

```
Cloud Scheduler (every 15 min)
    │
    ▼
POST /worker  { task_type: "fire_due_reminders" }
    │
    ▼
WorkerHandler._handle_fire_due_reminders()
    │
    ├─ AgentNotePort.list_due_reminders(as_of=now_utc)  → List[AgentNote]
    │
    └─ for each due reminder:
           UserNotificationService.notify(
               user_id, account_id,
               system_alert=note.instruction,   ← full context, not text label
               session_id=new UUID,
               context={"system_initiated": True},
           )
           │
           └─ QuickAgent executes the instruction with full toolset, delivers to user's channel
    │
    └─ for each fired reminder:
           if recurrence: AgentNotePort.reschedule(note_id, next_due, last_fired=now)
           else:          AgentNotePort.delete_note(note_id, user_id)
```

**Key insight:** `UserNotificationService.notify()` already does exactly what is needed.
Zero new delivery infrastructure required.

---

## 4. Timezone

**Decision:** User sets their timezone in the Cabinet web UI (first iteration).
Proactive setting from chat is a deferred feature.

### What timezone is used for

1. **Current datetime in prompts** — RouterAgent currently injects `datetime.now(UTC)`. With
   this change, it injects `datetime.now(user_tz)` so the LLM reasons in the user's local time.

2. **Reminder `due` field** — always stored as UTC in Firestore. When the LLM writes a due
   date ("tomorrow at 9am"), NotesAgent resolves it to UTC using the user's timezone before
   storing. The timezone lives in the user profile, not in each reminder.

3. **Recurrence `next_due` computation** — performed in the user's timezone to preserve
   wall-clock time across DST transitions (e.g. "every day at 9am" stays at 9am local even
   when clocks change).

4. **Transparency notifications** — due dates displayed in user's local time.

### Implementation

`UserBotConfig` gains a new field:
```python
timezone: str = "UTC"   # IANA timezone name, e.g. "Europe/Kyiv", "America/New_York"
```

Cabinet UI: timezone selector (IANA tz list, searchable dropdown).

RouterAgent `_inject_runtime_context()`:
```python
user_tz = pytz.timezone(user_config.timezone or "UTC")
now_local = datetime.now(user_tz)
# replaces current UTC datetime injection
```

NotesAgent: receives `user_tz` via `message.context["user_timezone"]`, which AgentContextBuilder
populates from `UserBotConfig`. Uses it when computing `due` UTC from LLM-provided wall-clock time.

---

## 5. Recurrence Model

Simpler than `TaskRecurrence` — we compute `next_due` in Python, no dependency on MS Graph.

```python
@dataclass
class ReminderRecurrence:
    """How to reschedule after firing."""
    type: str     # "hourly" | "daily" | "weekly" | "monthly"
    interval: int = 1  # every N units (e.g. interval=2, type="daily" → every 2 days)
```

`next_due` computation (`compute_next_due(current_due, recurrence, user_tz)`):

| type    | interval | next_due                              |
|---------|----------|---------------------------------------|
| hourly  | N        | current_due + N hours (UTC arithmetic)|
| daily   | N        | current_due + N days (in user_tz, preserves wall-clock time) |
| weekly  | N        | current_due + N×7 days (in user_tz)   |
| monthly | N        | current_due + N months via `dateutil.relativedelta` (in user_tz) |

For `daily`/`weekly`/`monthly`: compute in user timezone, convert result to UTC for storage.
This preserves the wall-clock time across DST transitions.

**One-time reminder:** `recurrence=None`. Fired once, then deleted.

---

## 6. Domain Changes

### `src/domain/agent_note.py`

```python
@dataclass
class ReminderRecurrence:
    type: str      # "hourly" | "daily" | "weekly" | "monthly"
    interval: int = 1

@dataclass
class AgentNote:
    note_id: str
    user_id: str
    text: str                              # Short display label (≤15 words)
    instruction: str                       # Full execution context (no limit)
    created_at: datetime
    due: datetime                          # UTC
    recurrence: Optional[ReminderRecurrence] = None
    last_fired: Optional[datetime] = None  # UTC

@dataclass
class NoteCreate:
    user_id: str
    text: str                              # Short display label
    instruction: str                       # Full execution context
    due: datetime                          # UTC (NotesAgent converts from wall-clock)
    recurrence: Optional[ReminderRecurrence] = None

@dataclass
class NoteUpdate:
    note_id: str
    user_id: str
    text: Optional[str] = None
    instruction: Optional[str] = None
    due: Optional[datetime] = None         # UTC
    recurrence: Optional[ReminderRecurrence] = None
```

Removed: `visible_after`, `expires_after` (unused in new model).

### `src/domain/user.py` — `UserBotConfig`

```python
timezone: str = "UTC"   # IANA timezone, set via Cabinet UI
```

---

## 7. Port Changes

### `src/ports/agent_note_port.py` — two new methods

```python
async def list_due_reminders(self, as_of: datetime) -> List[AgentNote]:
    """
    Cross-user scan: returns all notes with due <= as_of.
    Called by fire_due_reminders worker only.
    """

async def reschedule(self, note_id: str, next_due: datetime, last_fired: datetime) -> None:
    """
    Update due to next_due and last_fired. Called after firing a recurrent reminder.
    System-level operation — no user_id ownership check (cron owns the lock).
    """
```

---

## 8. Firestore Changes

### Collection: `{env_prefix}orchestrator_notes`

New/changed fields per document:

| Field         | Type              | Notes                                             |
|---------------|-------------------|---------------------------------------------------|
| `instruction` | `string`          | Full execution context. New required field.       |
| `recurrence`  | `map` or `null`   | `{type, interval}` or null                        |
| `last_fired`  | `timestamp` or `null` | Updated after each fire                       |

Removed fields (existing records may have them — adapter ignores):
- `visible_after`
- `expires_after`

Migration: existing records without `instruction` → adapter uses `text` as fallback.

### Required Firestore Index

```
Collection: {env_prefix}orchestrator_notes
Field: due ASC
```

This allows `WHERE due <= :now` queries without full collection scan.
Without this index, `list_due_reminders` degrades to O(all_notes).

---

## 9. Worker Changes

### `src/handlers/worker_handler.py`

New `task_type`:

```
fire_due_reminders  → WorkerHandler._handle_fire_due_reminders()
```

Handler logic:

```python
async def _handle_fire_due_reminders(self) -> Tuple[dict, int]:
    now = datetime.now(timezone.utc)
    due = await self._notes_port.list_due_reminders(as_of=now)

    fired, skipped = 0, 0
    for note in due:
        account_id = await self._user_repo.get_account_id(note.user_id)
        if not account_id:
            skipped += 1
            continue

        # Idempotency: skip if already fired in this cron window
        if note.last_fired and (now - note.last_fired).total_seconds() < 14 * 60:
            skipped += 1
            continue

        await self._notification_service.notify(
            user_id=note.user_id,
            account_id=account_id,
            system_alert=note.instruction,    # full context, not the short label
            session_id=str(uuid.uuid4()),
            extra_context={"system_initiated": True},
        )

        if note.recurrence:
            user_tz = await self._get_user_timezone(note.user_id)
            next_due = compute_next_due(note.due, note.recurrence, user_tz)
            await self._notes_port.reschedule(note.note_id, next_due, last_fired=now)
        else:
            await self._notes_port.delete_note(note.note_id, note.user_id)

        fired += 1

    return {"fired": fired, "skipped": skipped}, 200
```

### `cloudbuild-dev.yaml` / `cloudbuild-prod.yaml`

New Cloud Scheduler job:

```yaml
- name: fire-due-reminders
  schedule: "*/15 * * * *"   # every 15 minutes
  uri: /worker
  body:
    task_type: fire_due_reminders
```

---

## 10. NotesAgent Changes

### Tool declarations — add `instruction` and `recurrence`

```json
"create_self_reminder": {
  "parameters": {
    "text": {
      "type": "string",
      "description": "Short display label — ≤15 words. Shown in working memory context."
    },
    "instruction": {
      "type": "string",
      "description": "Full execution context. This is what runs when the reminder fires — write
                       it as a complete instruction with all necessary context: what to do, why,
                       any relevant details from the current conversation. No length limit."
    },
    "due": { "type": "string", "description": "ISO-8601 datetime in user's local time." },
    "recurrence": {
      "type": "object",
      "description": "Optional. Repeat after firing.",
      "properties": {
        "type":     { "type": "string", "enum": ["hourly", "daily", "weekly", "monthly"] },
        "interval": { "type": "integer", "description": "Every N units. Default 1." }
      },
      "required": ["type"]
    }
  },
  "required": ["text", "instruction", "due"]
}
```

Same additions to `update_self_reminder`.

NotesAgent resolves `due` from user's local time to UTC using `user_timezone` from context.

### Transparency notifications

Every mutation (create / update / delete) triggers a brief `notify_raw()` to the user.
Primary protection against unauthorized self-activation.

```python
# After successful create:
due_local = note.due.astimezone(user_tz).strftime('%d %b %Y %H:%M %Z')
await self._notification_service.notify_raw(
    user_id=user_id, account_id=account_id,
    text=f"📌 Reminder set: \"{note.text}\" — {due_local}"
           + (f" (repeats {note.recurrence.type})" if note.recurrence else "")
)

# After successful update:  "📝 Reminder updated: ..."
# After successful delete:  "🗑️ Reminder deleted: ..."
```

`notify_raw()` is best-effort: failure is logged and silently swallowed, never blocks CRUD.
NotesAgent constructor gains `notification_service: Optional[...]`. Factory wires it in.

### Recursion protection

`system_initiated=True` is passed in context when the cron fires. NotesAgent **allows**
`create_self_reminder` during system-initiated execution — chaining is a valid use case
(e.g. "check X, if not done, remind me again tomorrow" creates a follow-up reminder).

The hard cap (30 notes) and transparency notifications are the backstop against abuse.
A possible future refinement: depth counter (`reminder_depth: int`) to block chains
deeper than 2, but this is intentionally deferred — not needed for MVP.

---

## 11. Orchestrator Protocol Update

`PROTOCOL_AGENT_SELECTION.groovy` — new section for `manage_self_reminders`:

```groovy
manage_self_reminders_agent {
    intent: "manage_self_reminders"

    semantics: """
        A reminder is a deferred instruction with two parts:
          text        — short label for display (≤15 words)
          instruction — complete execution context (no limit)

        When a reminder fires, its INSTRUCTION is run as a new conversation.
        You receive it exactly as if the user typed it — with access to all your tools.
        There is NO conversation history at that point. The instruction must be self-contained.

        Write instruction as if briefing a colleague who knows nothing about the conversation:
          - What to do
          - Why / what the user expects
          - Any relevant context from the current conversation
    """

    when_to_use: [
        "User asks to be reminded of something at a specific time",
        "User wants a recurring task (daily news, weekly check-in)",
        "You identify a follow-up that needs to happen regardless of user activity",
    ]

    examples: [
        {
            user: "remind me every morning about Valencia news"
            text: "Send Valencia morning news briefing"
            instruction: "The user asked for a daily news briefing about Valencia, Spain.
                          Search for today's news (events, weather, cultural highlights, local
                          news). Format as a short morning briefing and send to the user."
            due: "tomorrow 08:00 (user's local time)"
            recurrence: { type: "daily", interval: 1 }
        },
        {
            user: "remind me in 2 hours to review the proposal"
            text: "Review proposal in 2 hours"
            instruction: "Remind the user to review the proposal they mentioned.
                          Say: 'Hey, 2 hours ago you asked me to remind you to review the proposal.'"
            due: "now + 2h"
            recurrence: null
        },
        {
            user: "check in with me every Monday about the project status"
            text: "Weekly project status check-in"
            instruction: "The user wants a weekly project status check-in every Monday.
                          Ask: 'It's Monday — how is the project going? Any blockers or updates?'"
            due: "next Monday 09:00 (user's local time)"
            recurrence: { type: "weekly", interval: 1 }
        }
    ]
}
```

---

## 12. Safety & Limits

| Concern | Mitigation |
|---------|-----------|
| **Unauthorized self-activation** | Every create/update/delete sends `notify_raw()` to user in real time. Primary protection. |
| Infinite recursion | Hard cap (30 notes) prevents accumulation. Transparency notifications expose any rogue chain. `reminder_depth` counter deferred to v2. |
| One-level chaining | **Explicitly allowed** — valid use case. `system_initiated=True` does not block creation. |
| Reminder spam | Soft cap 20 (alert in result), hard cap 30 (exception in adapter) |
| No active channel | `notify()` / `notify_raw()` silently skip — note still rescheduled/deleted |
| Cron double-fire | `last_fired` check: skip if `last_fired >= now - 14 min` |
| Very old overdue note | Fire once, then reschedule or delete. No backfill of missed occurrences. |

---

## 13. Open Questions

**Q2: What happens when reminder fires and user has no channel?**
Silent skip, note rescheduled. Next occurrence will fire. Log at INFO level. Acceptable.

**Q4: Multiple reminders firing in same 15-min window**
If a user has 5 daily reminders all set to 9am, they all fire. Intended. Hard cap prevents
extreme cases. Firing is sequential per user to avoid notification flood.

**Q5: `list_due_reminders` cross-user Firestore scan**
At low scale (MVP): single query `WHERE due <= now` across all users. Firestore index on `due`
makes this efficient. At high user count: partition by `due_hour` bucket. Not needed for MVP.

**Q6: Cabinet UI timezone UX**
What happens if user never sets a timezone? Default "UTC" — reminders fire on UTC clock.
Acceptable. RouterAgent should proactively ask for timezone during onboarding or first
reminder creation (future feature).

---

## 14. Files Changed

| File | Change |
|------|--------|
| `src/domain/agent_note.py` | Add `ReminderRecurrence`, `instruction` field, `recurrence`/`last_fired`, remove `visible_after`/`expires_after` |
| `src/domain/user.py` | Add `timezone: str = "UTC"` to `UserBotConfig` |
| `src/ports/agent_note_port.py` | Add `list_due_reminders()`, `reschedule()` |
| `src/adapters/firestore_agent_note_adapter.py` | Implement new port methods, `instruction` field, Firestore index |
| `src/agents/notes_agent.py` | Add `instruction` + `recurrence` to tool decls, timezone-aware `due` resolution, transparency notifications |
| `src/services/agent_context_builder.py` | Pass `user_timezone` into NotesAgent context |
| `src/handlers/worker_handler.py` | Add `fire_due_reminders` task type + handler, `compute_next_due()` utility |
| `src/composition/user_agent_factory.py` / `ServiceContainer` | Wire `notification_service` into NotesAgent |
| `src/handlers/conversation_handler.py` | RouterAgent injects datetime in user timezone |
| `src/web/cabinet.py` (or equivalent) | Timezone picker UI + save endpoint |
| `cloudbuild-dev.yaml` / `cloudbuild-prod.yaml` | Cloud Scheduler job for `fire_due_reminders` |
| `firestore_utils/` | Index deployment for `due ASC` |
| `firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.groovy` | Add `manage_self_reminders` section |

---

## 15. Implementation Order

1. **Domain**: `ReminderRecurrence`, `instruction` field, `UserBotConfig.timezone`
2. **Cabinet UI**: Timezone picker + save endpoint (unblocks everything else)
3. **RouterAgent**: Current datetime in user timezone
4. **Port + Adapter**: `list_due_reminders`, `reschedule`, `instruction` field
5. **NotesAgent**: `instruction`/`recurrence` in tool decls, timezone-aware due, transparency notifications
6. **Worker**: `fire_due_reminders` task type + `compute_next_due()`
7. **Cloud Scheduler**: New trigger in cloudbuild yamls
8. **Protocol**: Update `PROTOCOL_AGENT_SELECTION.groovy`
9. **Tests**: Unit + integration for worker handler, NotesAgent tool declarations
