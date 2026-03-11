# RFC: history_addendum — Generic Specialist Context Persistence

**Status:** DRAFT — Future consideration, not scheduled
**Author:** Dmytro Deleur
**Created:** 2026-03-03

---

## Problem

Currently, persisting specialist results in session history requires touching 4 files:

1. **Specialist** — produces typed data (e.g., email search results)
2. **Quick loop** — explicitly accumulates the specialist's data type
3. **Smart loop** — same accumulation, duplicated
4. **ConversationHandler** — knows the specific metadata key and appends it to `full_text`

### Concrete example: `email_search_context`

```
EmailSearchAgent returns results
  ↓
QuickResponseAgent._execute_quick_delegation_loop()
  accumulates: email_contexts list per turn
  _QuickLoopResult.email_search_context = email_contexts or None
  ↓
QuickResponseAgent.execute()
  if email_search_context:
      metadata["email_search_context"] = email_search_context
  ↓
ConversationHandler.handle_message()
  email_search_context = response.metadata.get("email_search_context")
  if email_search_context:
      response_text += "\n\n" + json.dumps({"email_search_context": ...})
```

Adding a second specialist that needs history persistence = same 4 files, again.

---

## Proposed Solution

`history_addendum` as a first-class field on `AgentResponse`:

```python
@dataclass
class AgentResponse:
    # ... existing fields ...
    history_addendum: Optional[Dict[str, Any]] = None
    """
    Arbitrary key-value data the specialist wants appended to the model's
    history entry (full_text only). The LLM sees this in subsequent turns
    and can use it without re-calling the specialist.

    Specialists declare what they want preserved. Orchestrators collect and
    forward. ConversationHandler appends generically — without knowing the
    semantics of the keys.
    """
```

### Flow

```
EmailSearchAgent.process()
  return AgentResponse.success(
      result=...,
      history_addendum={"email_search_context": [{"you_searched": ..., "you_received": [...]}]}
  )
  ↓
Orchestrator delegation loop (Quick or Smart):
  specialist_response = await coordinator.handle_delegation(intent, query)
  if specialist_response.history_addendum:
      accumulated_addendum.update(specialist_response.history_addendum)
  ↓
Orchestrator.execute():
  return AgentResponse.success(
      ...,
      history_addendum=accumulated_addendum or None,
  )
  ↓
ConversationHandler.handle_message():
  addendum = response.history_addendum
  if addendum:
      response_text += "\n\n" + json.dumps(addendum, ensure_ascii=False, separators=(",", ":"))
  # Zero knowledge of what keys are inside.
```

### Result: Adding a new specialist with history persistence

1. Specialist returns `history_addendum` in its `AgentResponse` ✅
2. Wire specialist class in `user_agent_factory.py` ✅
3. Register `AgentDescriptor` in `agent_manifest.py` ✅

**Zero changes** to Quick, Smart, or ConversationHandler.

---

## Trade-offs

| | Current (explicit) | Proposed (generic) |
|---|---|---|
| Adding new specialist | 4 files | 1 file (specialist only) |
| Debugging | Explicit — grep `email_search_context` in handler | Implicit — need to know addendum comes from specialist |
| Key collision risk | None (handler handles each key) | Low but possible if two specialists use same key |
| Typing | Typed (`List[Dict]`) in loop result | Untyped (`Dict[str, Any]`) at ConversationHandler level |

Key collision is manageable — specialists should namespace their keys (e.g., `email_search_context`, `calendar_event_context`, not `results`).

---

## Migration

Not a breaking change. Migrate EmailSearchAgent first as a proof of concept:
1. Add `history_addendum` field to `AgentResponse` (backward-compatible, defaults to `None`)
2. EmailSearchAgent sets it; remove explicit handling from Quick/Smart loops
3. ConversationHandler switches from `metadata["email_search_context"]` to `history_addendum`
4. Remove `email_search_context` from orchestrator `_QuickLoopResult` / `AgentLoopResult`

---

## When to Implement

Trigger: second specialist needs history persistence. Until then, the current
`email_search_context` implementation is acceptable — the cost of premature abstraction
(extra indirection for a single use case) exceeds the maintenance benefit.
