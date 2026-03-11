# RFC: Orchestrator Notes (Agent Notepad)

**Status:** CONCEPT
**Date:** 2026-03-08

---

## 1. Context & Motivation

The LLM orchestrator (Quick/Smart) has no persistent memory between turns.
If it decides "remind the user about X tomorrow", that intent exists only in the
response text — the next turn starts fresh with no record of it.

**Agent Notepad** gives the orchestrator a first-class mechanism to maintain its own
personal todo list across turns. Notes are persisted in Firestore and injected into
every subsequent prompt turn by the Router enrichment phase. The orchestrator sees
its own notes in context and can act on them, update them, or delete them.

### Semantic distinction: orchestrator's list vs. user's list

`pending_notes` is the **orchestrator's own list of obligations to itself** —
things it decided to remember, track, or do on behalf of the user, but which have
not been explicitly requested as a user task.

This is categorically different from TasksAgent (Google Tasks): user tasks are
created by the user's intent and managed in Google Tasks. Orchestrator notes are
created by the orchestrator's own reasoning and are invisible to the user.

A typical lifecycle: orchestrator creates a note → acts on it in a future turn →
deletes the note. The user may or may not see any trace of this.

This makes the orchestrator a **stateful agent with its own obligations**, not a
stateless function. It has open commitments between turns. This is a deliberate
step toward more agentic behavior.

### Non-goals

- Notes are **not** long-term memory (use Facts + Consolidation for that).
- Notes are **not** user-facing tasks (use TasksAgent for that).
- Notes are **not** a solution for long-context sessions (that is a separate
  agent with session history, orthogonal to this RFC).
- Notes are **not** pushed proactively to Slack/Telegram (Phase 1).
  A future scheduler can trigger a synthetic LLM turn; see Section 9.

---

## 2. Design Principles

1. **Write via intents, read via context injection.**
   The orchestrator writes notes by delegating to `NotesAgent`. It reads them
   automatically — Router injects all active notes after the cache boundary every
   turn. No `list_notes` intent is needed.

2. **NotesAgent has no LLM.**
   CRUD on short note strings needs no reasoning. Pure Firestore I/O.

3. **`visible_after` is the extension point.**
   A note with `visible_after` set to a future timestamp is silently excluded from
   injection until that time arrives. Today: orchestrator manages this field directly
   via the intent payload. Future: Cloud Scheduler fires a synthetic LLM turn —
   the injection code is unchanged.

4. **`expires_after` for auto-cleanup.**
   Notes past their expiry are excluded from injection and can be purged by a
   background job.

5. **Hard cap: 10 notes, 15 words each.**
   Enforced in the adapter (`create_note` raises if cap is reached; `text` validated
   to ≤15 words). The orchestrator must understand the constraint and **prioritize**:
   if at cap, it must delete a lower-priority note before creating a new one.
   The cap is a feature, not a bug — it forces the orchestrator to treat notes as
   a scarce resource and curate actively.

6. **Notes are ephemeral by design; Consolidation handles persistence.**
   Behavioral observations ("user is vegetarian") belong in the Facts pipeline, not
   in `pending_notes`. After Consolidation fires, the fact appears in `biographical_context`
   and the orchestrator should recognize the note is redundant and delete it.
   `COGNITIVE_PROCESS_NOTES` must make this lifecycle explicit.

7. **Context budget priority.**
   If context budget is tight, `pending_notes` is protected. The tuning levers are
   `BIOGRAPHICAL_LIMIT` and `HISTORY_FULL_TURNS` (reduce to 2–3 turns), not the
   notes cap. Notes are the most time-sensitive dynamic content in the prompt.

---

## 3. Architecture

```
User message
    ↓
RouterAgent
    ├── LLM triage + enrichment (existing)
    ├── list_active_notes(user_id, as_of=now)  ← NEW: AgentNotePort call
    └── inject notes into message.context["agent_notes"]
            ↓
QuickAgent / SmartAgent
    ├── build_for_agent() → PromptBuilder (existing)
    │     └── pending_notes {} block appended after PROMPT_CACHE_BOUNDARY  ← NEW
    └── delegation loop
            ↓ intent: create_note / delete_note / update_note
         NotesAgent(BaseAgent)  ← NEW specialist, no LLM
            └── FirestoreAgentNoteAdapter(AgentNotePort)
                    └── Firestore: {env}_orchestrator_notes
```

---

## 4. Domain Model

### 4.1 `src/domain/agent_note.py`

```python
"""
Agent Note domain model.

A short-lived contextual annotation written by the orchestrator to itself.
Injected into subsequent prompt turns by Router enrichment.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class AgentNote:
    """Persisted orchestrator note, injected into future prompts."""
    note_id: str
    user_id: str
    text: str
    created_at: datetime
    visible_after: Optional[datetime] = None   # None = visible immediately
    expires_after: Optional[datetime] = None   # None = never expires


@dataclass
class NoteCreate:
    """Input for creating a new note."""
    user_id: str
    text: str
    visible_after: Optional[datetime] = None
    expires_after: Optional[datetime] = None


@dataclass
class NoteUpdate:
    """Input for updating an existing note."""
    note_id: str
    user_id: str
    text: Optional[str] = None
    visible_after: Optional[datetime] = None
    expires_after: Optional[datetime] = None
```

---

### 4.2 `src/ports/agent_note_port.py`

```python
"""
AgentNotePort — abstract interface for orchestrator note storage.

Port justification: Firestore adapter today; future alternatives possible
(Redis for ephemeral notes, encrypted storage, external task system).
Port methods carry user_id explicitly — no auth artifacts at port boundary.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from ..domain.agent_note import AgentNote, NoteCreate, NoteUpdate


class AgentNotePort(ABC):

    @abstractmethod
    async def create_note(self, data: NoteCreate) -> AgentNote:
        """Create a new note. Returns the created note with generated note_id."""

    @abstractmethod
    async def delete_note(self, note_id: str, user_id: str) -> bool:
        """Delete note by ID. Returns True if deleted, False if not found."""

    @abstractmethod
    async def update_note(self, data: NoteUpdate) -> AgentNote:
        """Update an existing note. Returns updated note."""

    @abstractmethod
    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        """
        Return all notes currently active for the user:
          (visible_after IS NULL OR visible_after <= as_of)
          AND
          (expires_after IS NULL OR expires_after > as_of)
        Ordered by created_at ASC (oldest first → stable order in prompt).
        """
```

---

## 5. Firestore Collection Design

Collection: `{env_prefix}_orchestrator_notes`

| Field | Type | Notes |
|-------|------|-------|
| `note_id` | string | Document ID (UUID) |
| `user_id` | string | Owner; indexed |
| `text` | string | Note content (≤500 chars enforced in adapter) |
| `created_at` | timestamp | Set at creation |
| `visible_after` | timestamp \| null | `null` = visible immediately |
| `expires_after` | timestamp \| null | `null` = never expires |

### Firestore query strategy

`list_active_notes` cannot express `OR` on two separate fields in a single
Firestore composite query. Strategy: query by `user_id` only, filter
`visible_after` and `expires_after` in Python. Notes per user are expected
to be O(1–10) — in-Python filter is negligible.

Single-field index on `user_id` is sufficient; no composite index required.

---

## 6. Adapter Sketch — `src/adapters/firestore_agent_note_adapter.py`

```python
class FirestoreAgentNoteAdapter(AgentNotePort):

    MAX_NOTE_LENGTH = 500

    def __init__(self, db: firestore.AsyncClient, env_prefix: str):
        self._db = db
        self._col = f"{env_prefix}_orchestrator_notes"

    MAX_NOTES_PER_USER = 10
    MAX_WORDS_PER_NOTE = 15

    async def create_note(self, data: NoteCreate) -> AgentNote:
        word_count = len(data.text.split())
        if word_count > self.MAX_WORDS_PER_NOTE:
            raise ValueError(f"Note text exceeds {self.MAX_WORDS_PER_NOTE} words ({word_count})")
        active = await self.list_active_notes(data.user_id, as_of=datetime.now(timezone.utc))
        if len(active) >= self.MAX_NOTES_PER_USER:
            raise ValueError(f"Note cap reached ({self.MAX_NOTES_PER_USER}). Delete a note first.")
        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        await self._db.collection(self._col).document(note_id).set({
            "user_id": data.user_id,
            "text": data.text,
            "created_at": now,
            "visible_after": data.visible_after,
            "expires_after": data.expires_after,
        })
        return AgentNote(note_id=note_id, user_id=data.user_id, text=data.text,
                         created_at=now, visible_after=data.visible_after,
                         expires_after=data.expires_after)

    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        docs = await self._db.collection(self._col).where("user_id", "==", user_id).get()
        result = []
        for doc in docs:
            note = self._doc_to_note(doc.id, doc.to_dict())
            if note.visible_after and note.visible_after > as_of:
                continue
            if note.expires_after and note.expires_after <= as_of:
                continue
            result.append(note)
        return sorted(result, key=lambda n: n.created_at)

    # delete_note and update_note: fetch → ownership check → mutate
```

---

## 7. NotesAgent — `src/agents/notes_agent.py`

No LLM. Dispatches on intent string, calls port, returns structured dict.

```python
class NotesAgent(BaseAgent):

    def __init__(self, config: AgentConfig, notes_port: AgentNotePort):
        super().__init__(config)
        self._notes = notes_port

    async def execute(self, message: AgentMessage) -> AgentResponse:
        intent = message.payload.get("intent")
        user_id = message.context.get("user_id")
        self._on_agent_start(intent)

        if intent == Intent.CREATE_NOTE:
            note = await self._notes.create_note(NoteCreate(
                user_id=user_id,
                text=message.payload["text"],
                visible_after=_parse_dt(message.payload.get("visible_after")),
                expires_after=_parse_dt(message.payload.get("expires_after")),
            ))
            result = {"note_id": note.note_id, "status": "created"}

        elif intent == Intent.DELETE_NOTE:
            deleted = await self._notes.delete_note(message.payload["note_id"], user_id)
            result = {"note_id": message.payload["note_id"], "deleted": deleted}

        elif intent == Intent.UPDATE_NOTE:
            note = await self._notes.update_note(NoteUpdate(
                note_id=message.payload["note_id"],
                user_id=user_id,
                text=message.payload.get("text"),
                visible_after=_parse_dt(message.payload.get("visible_after")),
                expires_after=_parse_dt(message.payload.get("expires_after")),
            ))
            result = {"note_id": note.note_id, "status": "updated"}

        self._on_agent_success(len(str(result)), 0, str(result))
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id,
                                     result=result, confidence=1.0)
```

`NotesAgent` has no LLM dependency. No entry in `AgentContextBuilder.STRATEGIES`.
Constructor injection: `notes_port: AgentNotePort` only.

---

## 8. Context Injection

### 8.1 RouterAgent change

`RouterAgent.__init__` gains `notes_port: Optional[AgentNotePort] = None`.

In `execute()`, after enrichment, before routing:

```python
agent_notes = []
if self.notes_port and self.user_id:
    try:
        agent_notes = await self.notes_port.list_active_notes(
            user_id=self.user_id,
            as_of=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("⚠️ [RouterAgent] Failed to load agent notes: %s", exc)
```

Added to `routed_message.context`:

```python
"agent_notes": [
    {
        "note_id": n.note_id,
        "text": n.text,
        "expires_after": n.expires_after.isoformat() if n.expires_after else None,
    }
    for n in agent_notes
],
```

### 8.2 Prompt injection point

`pending_notes {}` is appended **after** `PROMPT_CACHE_BOUNDARY` alongside
`current_date_time {}`. This keeps notes out of the Anthropic-cached prompt
prefix — notes change every turn and must not pollute the cache.

Format in prompt:

```
<!-- CACHE_BOUNDARY -->

pending_notes {
    - [id: 9f3a…] "Remind user to confirm dentist appointment" (expires: 2026-03-10)
    - [id: c1b2…] "User mentioned vegetarian diet — factor into meal suggestions"
}

current_date_time {
    2026-03-08 14:32 Saturday (UTC)
    ...
}
```

### 8.3 Implementation path for injection

`UserPromptBuilder.build_for_agent()` receives the message context (via
`routing_metadata` or a dedicated `agent_notes` parameter). It formats the
`pending_notes {}` block and passes it to `PromptAssemblyService._inject_runtime_context()`
as an additional dynamic part. The exact parameter name (new param vs. extending
`query_specific_context`) is a READY-TO-IMPLEMENT decision — both options work.

---

## 9. Future: Scheduler Path (Phase 2)

Phase 1 (this RFC): orchestrator writes `visible_after` at creation time.
The note silently waits; it appears in context when the user sends the next message
after `visible_after` has passed.

Phase 2 (future): Cloud Scheduler fires a **synthetic turn** for each user who
has notes where `visible_after <= NOW()`:

```
Cloud Scheduler (every 5 min)
    ↓
/worker?task_type=notes_wakeup
    ↓
WorkerHandler → NotesWakeupHandler
    ↓
ConversationHandler.handle_synthetic(user_id, trigger_text="__notes_wakeup__")
    ↓
RouterAgent → active notes already injected → Quick/Smart orchestrator acts
```

**No changes to RouterAgent or PromptBuilder required for Phase 2.**
`list_active_notes` filters by `visible_after <= NOW()` already.
Only the Cloud Scheduler trigger + `WorkerHandler` dispatch are new.

---

## 10. AgentManifest Changes

```python
class Intent:
    # ... existing ...
    CREATE_NOTE = "create_note"
    DELETE_NOTE = "delete_note"
    UPDATE_NOTE = "update_note"

NOTES = AgentDescriptor(
    agent_id="notes_agent",
    agent_type="notes",
    capabilities={
        Intent.CREATE_NOTE: ExecutionMode.SYNC,
        Intent.DELETE_NOTE: ExecutionMode.SYNC,
        Intent.UPDATE_NOTE: ExecutionMode.SYNC,
    },
    description="Orchestrator notepad — write, update, or delete contextual notes",
    capability_descriptions={
        Intent.CREATE_NOTE: (
            "Create a note to remember something across turns. "
            "payload: {\"text\": \"...\", \"visible_after\": \"<ISO8601 or null>\", "
            "\"expires_after\": \"<ISO8601 or null>\"}"
        ),
        Intent.DELETE_NOTE: (
            "Delete a note by ID. Use when you have acted on it and no longer need it. "
            "payload: {\"note_id\": \"...\"}"
        ),
        Intent.UPDATE_NOTE: (
            "Update the text or timing of an existing note. "
            "payload: {\"note_id\": \"...\", \"text\": \"...\", "
            "\"visible_after\": \"...\", \"expires_after\": \"...\"}"
        ),
    },
    internal=False,  # Visible to Quick and Smart orchestrators
)
```

`NOTES` added to `ALL_DESCRIPTORS`.

---

## 11. Files Overview

### New files (6)

| File | Notes |
|------|-------|
| `src/domain/agent_note.py` | `AgentNote`, `NoteCreate`, `NoteUpdate` |
| `src/ports/agent_note_port.py` | 4 abstract methods |
| `src/adapters/firestore_agent_note_adapter.py` | Firestore CRUD + in-Python active filter |
| `src/agents/notes_agent.py` | No LLM; dispatches on intent |
| `tests/unit/agents/test_notes_agent.py` | AsyncMock(spec=AgentNotePort) |
| `tests/unit/ports/test_agent_note_port.py` | ABC contract test |

### Modified files (5)

| File | Change |
|------|--------|
| `src/infrastructure/agent_manifest.py` | +3 Intent constants, +NOTES descriptor, +ALL_DESCRIPTORS |
| `src/composition/service_container.py` | +`FirestoreAgentNoteAdapter` init, +`"notes_provider"` in `agent_services()` |
| `src/composition/user_agent_factory.py` | +`notes_port` param, +`NotesAgent` instantiation, +register, +eviction |
| `src/agents/core/router_agent.py` | +`notes_port` param, +`list_active_notes()` call, +`agent_notes` in context |
| `src/services/prompt_builder.py` | +`pending_notes {}` block after PROMPT_CACHE_BOUNDARY |

No changes to `agent_config.py` (NotesAgent has no LLM — no tier/timeout needed).
No changes to `agent_context_builder.py` (no strategy entry — no LLM).
No OAuth required.

---

## 12. Cognitive Process Token

File: `firestore_utils/uploads/COGNITIVE_PROCESS_NOTES.groovy`

The token must explain to the orchestrator (Quick/Smart):

- What `pending_notes {}` is (its own operational todo list, injected every turn)
- When to create a note (defer something to a future turn; operational intents only)
- When to delete a note (after acting on it, or when Consolidation has absorbed
  the fact into `biographical_context` and the note is now redundant)
- **Cap rule:** maximum 10 notes, 15 words each. At cap: delete before creating.
  Prioritize: keep time-sensitive operational notes, discard stale ones.
- **Not for behavioral observations.** "User is vegetarian" → Facts pipeline, not here.
  `pending_notes` is for "do X" obligations, not "know Y" facts.
- `visible_after` usage: for time-deferred reminders (ISO 8601 format)
- `expires_after` usage: for safety cleanup

This token is added to the Quick/Smart profiles alongside the existing cognitive
process tokens. Exact Groovy DSL format: follow the pattern of
`COGNITIVE_PROCESS_QUICK.groovy`.

---

## 13. Open Questions (resolve at READY-TO-IMPLEMENT)

1. **Injection parameter name.** Add `agent_notes: Optional[str]` as a dedicated
   parameter to `PromptAssemblyService._inject_runtime_context()`, or fold it into
   `query_specific_context`? Separate param is cleaner and avoids format collision.

3. **Security validation.** Notes are orchestrator-written, not user-written.
   Are they `TRUSTED` zone or `UNTRUSTED`? Probably TRUSTED (orchestrator text,
   not raw user input), but verify against the existing trust zone taxonomy.

4. **Note ID visibility to orchestrator.** Does the orchestrator need to see the full
   `note_id` UUID in the prompt? Alternative: a short 8-char prefix is enough for
   delete/update disambiguation. Shorter IDs = less prompt noise.

5. **Scheduler synthetic turn format.** Phase 2 detail: what `trigger_text` causes
   the orchestrator to act on visible notes without user input? Needs a dedicated
   system prompt path (not a standard user query).
