# RFC: Conversation History Cache Split (Pause-Based Static/Dynamic Split)

**Status:** PLANNED
**Date:** 2026-03-30
**Owner:** AI Engineering
**Scope:** `base_agent.py`, `prompt_assembly_service.py`, `prompt_builder.py`, `prompt_builder_port.py`, `smart_response_agent.py`, `quick_response_agent.py`
**Goal:** Maximize Anthropic prompt cache hit rate during active conversations by keeping
the static prefix (everything before `PROMPT_CACHE_BOUNDARY`) stable across turns.

**Related RFC:** `HEXAGONAL_PROMPT_CACHING_RFC.md`

---

## 1. Problem

Conversation history is currently placed inside `knowledge_base {}` — before
`PROMPT_CACHE_BOUNDARY`. Every new turn appends to history, changing the static prefix.
Anthropic cache TTL = 5 minutes. Result: zero cache hits during active conversation.

---

## 2. Design

### 2.1 Session boundary detection

Split history into two parts based on the gap between the **current request timestamp**
and the **timestamp of the most recent message in history**:

```
PAUSE_THRESHOLD = 5 minutes  (= Anthropic prompt cache TTL)

gap = now - history[-1].created_at

if gap >= PAUSE_THRESHOLD:
    static_history  = all messages         # full history to static; session ended
    dynamic_history = []
else:
    find last inter-message gap >= PAUSE_THRESHOLD scanning backwards
    static_history  = history[:split_idx]  # turns before current active session
    dynamic_history = history[split_idx:]  # turns within current active session
    if no gap found: static_history=[], dynamic_history=history (one long session)
```

`Message.created_at` is already populated — no schema changes needed.

### 2.2 Static zone rule

When building `conversation_history {}` block for the system prompt, **always use
`msg.parts[].text` (summary)** — never `full_text`. This applies regardless of whether
the message falls within `HISTORY_FULL_TURNS`. Full content is not needed in the static
zone; summaries are always available from turn 1.

### 2.3 Dynamic zone

`dynamic_history` (current session turns) is passed as the `messages` array prefix to
the LLM — exactly as `conversation_history` is used today in
`_execute_agent_delegation_loop`. `_apply_history_tiering` is applied to these turns
as now (HISTORY_FULL_TURNS window, full_text for recent, text for older within the window).
No changes to this path.

### 2.4 Prompt structure after RFC

```
knowledge_base {
    biographical_context: '''...'''
}

conversation_history {
    [User]: <summary>
    [Assistant]: <summary>
    ...
}

email_for_triage { ... }          ← if present (extra_static_blocks)

[blueprint / instructions]

<!-- CACHE_BOUNDARY -->

current_date_time { ... }
query_specific_context: '''...'''
```

`conversation_history {}` is a **separate top-level block** placed between
`knowledge_base {}` and the blueprint. It is NOT a field inside `knowledge_base {}`.

During an active session (gap < 5 min): `knowledge_base {}` + `conversation_history {}`
+ blueprint = stable → Anthropic cache **hits** on every turn.

On first message after pause (gap ≥ 5 min): static prefix rebuilds (one miss, but
Anthropic TTL had already expired regardless).

---

## 3. Implementation

### 3.1 `base_agent.py` — split function + updated `_load_conversation_context`

Add a static helper (same layer as `_apply_history_tiering`):

```python
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

HISTORY_CACHE_PAUSE = timedelta(minutes=5)

@staticmethod
def _split_history_at_session_boundary(
    history: List[Message],
    now: datetime,
) -> Tuple[List[Message], List[Message]]:
    """
    Split history into (static_turns, dynamic_turns).

    static_turns  — turns before the current active session; use .text (summary) only.
    dynamic_turns — turns within the current active session; pass to messages array.

    Session boundary = last inter-message gap >= HISTORY_CACHE_PAUSE (5 min).
    If no such gap, entire history is dynamic (one long uninterrupted session).
    If gap >= threshold from last message to now, entire history is static (session ended).
    """
    if not history:
        return [], []

    gap_to_now = now - history[-1].created_at
    if gap_to_now >= HISTORY_CACHE_PAUSE:
        return history, []

    for i in range(len(history) - 1, 0, -1):
        gap = history[i].created_at - history[i - 1].created_at
        if gap >= HISTORY_CACHE_PAUSE:
            return history[:i], history[i:]

    return [], history
```

Update `_load_conversation_context` to return the split:

```python
async def _load_conversation_context(
    self,
    session_store,
    session_id: str,
    current_message_parts: list,
    context_window: int,
) -> Tuple[List[Message], List[Message]]:
    """
    Returns (static_history, dynamic_history).

    static_history  — pre-session turns for system prompt conversation_history {} block.
    dynamic_history — current session turns for messages array (tiering applied by caller).
    """
    history = await session_store.get_session_history(
        session_id=session_id,
        limit=context_window,
    )
    now = datetime.now(timezone.utc)
    return self._split_history_at_session_boundary(history, now)
```

### 3.2 `smart_response_agent.py` and `quick_response_agent.py`

Both agents currently call `_load_conversation_context` and get a flat list. Update to
unpack the tuple and pass each part to the right destination:

```python
static_history, dynamic_history = await self._load_conversation_context(
    session_store=self.session_store,
    session_id=session_id,
    current_message_parts=current_message_parts,
    context_window=self.CONTEXT_WINDOW,
)

system_prompt = await self.prompt_builder.build_for_agent(
    ...
    static_history=static_history,   # new param — replaces conversation_history
    ...
)

# dynamic_history replaces the old conversation_history in the messages array:
tiered_dynamic = self._apply_history_tiering(dynamic_history)
# pass tiered_dynamic as messages prefix in _execute_agent_delegation_loop (unchanged call site)
```

### 3.3 `prompt_builder_port.py` — update `build_for_agent` signature

Replace `conversation_history: Optional[List[dict]]` with
`static_history: Optional[List[Message]]`:

```python
async def build_for_agent(
    self,
    agent_type: str,
    user_id: Optional[str] = None,
    account_id: Optional[str] = None,
    routing_metadata: Optional[RoutingMetadata] = None,
    capabilities: Optional[ProviderCapabilities] = None,
    biographical_facts: Optional[List[Dict]] = None,
    static_history: Optional[List[Message]] = None,   # replaces conversation_history
    include_biographical: bool = True,
    kb_preamble: bool = False,
    agent_notes: Optional[List[dict]] = None,
    extra_static_blocks: Optional[List[str]] = None,
) -> str:
```

Import: `from ..domain.llm import Message` (domain layer — allowed in ports).

### 3.4 `prompt_builder.py` — pass through to `assemble()`

Remove `conversation_history` handling. Pass `static_history` through to
`assembly_service.assemble()`. No formatting here — formatting is in `_inject_runtime_context`.

### 3.5 `prompt_assembly_service.py` — `assemble()` and `_inject_runtime_context()`

**`assemble()` signature:**
```python
async def assemble(
    self,
    agent_type: str,
    user_id: Optional[str],
    account_id: Optional[str],
    biographical_facts: Optional[List[Dict]] = None,
    static_history: Optional[List[Message]] = None,   # replaces conversation_history
    query_specific_context: Optional[str] = None,
    kb_preamble: bool = False,
    agent_notes: Optional[List[dict]] = None,
    user_timezone: str = "UTC",
    extra_static_blocks: Optional[List[str]] = None,
) -> str:
```

**`_inject_runtime_context()` — build `conversation_history {}` block:**

```python
if static_history:
    lines = []
    for msg in static_history:
        role = "User" if msg.role == "user" else "Assistant"
        # Always use .text (summary) — never full_text
        text = " ".join(
            part.text for part in msg.parts if part.text
        ).strip()
        if text:
            lines.append(f"    [{role}]: {text}")
    if lines:
        conv_block = "conversation_history {\n" + "\n".join(lines) + "\n}"
```

**Placement** — after `knowledge_base {}` block, before blueprint:

```python
if kb_parts:
    kb_block = "knowledge_base {\n" + "\n\n".join(kb_parts) + "\n}"
    if kb_preamble:
        extra = ("\n\n" + "\n\n".join(extra_static_blocks)) if extra_static_blocks else ""
        conv = ("\n\n" + conv_block) if conv_block else ""
        prompt = kb_block + conv + extra + "\n\n" + prompt
    else:
        prompt = prompt + "\n\n" + kb_block
```

Remove `conversation_history` from inside `kb_parts` entirely.

---

## 4. Files to Touch

| File | Change |
|---|---|
| `src/agents/base_agent.py` | Add `_split_history_at_session_boundary()`; update `_load_conversation_context` to return `Tuple[List[Message], List[Message]]` |
| `src/agents/core/smart_response_agent.py` | Unpack tuple; pass `static_history` to `build_for_agent`; pass `dynamic_history` (after tiering) to messages array |
| `src/agents/core/quick_response_agent.py` | Same as Smart |
| `src/ports/prompt_builder_port.py` | Replace `conversation_history` with `static_history: Optional[List[Message]]` |
| `src/services/prompt_builder.py` | Replace `conversation_history` with `static_history`; pass through to `assemble()` |
| `src/services/prompt_v3/prompt_assembly_service.py` | Replace `conversation_history` with `static_history`; render as separate `conversation_history {}` block after `knowledge_base {}`; use `msg.text` only |

---

## 5. Tests to Update

- `tests/unit/agents/core/test_smart_response_agent.py` — `_load_conversation_context` mock returns tuple
- `tests/unit/agents/core/test_quick_response_agent.py` — same
- `tests/unit/services/test_prompt_assembly_service.py` — `static_history` param; verify `conversation_history {}` block placement and summary-only content
- `tests/unit/agents/test_base_agent.py` — new `_split_history_at_session_boundary` unit tests: empty history, all static, all dynamic, mixed, gap exactly at threshold
