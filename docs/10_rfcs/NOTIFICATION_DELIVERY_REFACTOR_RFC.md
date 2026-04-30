# Notification Delivery Refactor — RFC

**Status:** Proposed
**Date:** 2026-04-30
**Author:** Dmytro Deleur

## 1. Context

On 2026-04-30 the daily email review failed to deliver. Investigation
of `alek-bot-dev` Cloud Run logs revealed not one bug but **six**
distinct defects in the notification path that interact and cascade:

| # | Defect | Effect |
|---|---|---|
| 1 | `SmartAgentConfig.timeout_ms = 300_000` is a fixed ceiling for ALL invocations, regardless of expected workload | Background analytical tasks (email review on 31 emails) cannot complete in 5 minutes; timeout fires while Smart is still on turn 4 of an 8-turn delegation loop |
| 2 | `SmartResponseAgent._execute_lock` (`asyncio.Lock`) serializes concurrent `execute()` calls per user | Two concurrent `notify()` (e.g. email review + reminder fire) queue up; the waiter's `wait_for(300s)` budget is consumed during lock-wait, leading to cascading near-instant timeouts when the lock finally releases |
| 3 | `RemindersService.fire_due_reminders` updates `last_fired` AFTER awaiting `notify()` | If `notify()` blocks ≥ 5 min, the next 5-min cron tick sees the same note as due; 4-min `last_fired` guard expires before the field is written → duplicate fire, paid twice |
| 4 | `UserNotificationService.notify()` returns silently when agent reports `AgentStatus.FAILED` | Caller (reminders, worker) thinks notification was delivered; reschedules / commits state as if successful |
| 5 | `SmartAgentConfig.config_max_retries = 0` and BaseAgent `process()` retries treat `TimeoutError` and transient `LLMProviderError` identically | No retry on real transient failures; no useful retry would help on structural timeout either, but the lack of typed error handling is a separate hygiene problem |
| 6 | `BaseAgent` warning log on timeout omits `task_id` and the actual timeout value | Three concurrent `process()` calls under one `agent_id` produce indistinguishable warnings; debugging requires correlation by timestamp arithmetic |

Triggering incident: email review's Smart run (`1ab5cf67`, 08:00–08:05
Madrid) hit defect #1, holding `_execute_lock` for the full 5 minutes;
reminder cron fire (`d277145e`, 08:05) hit defect #2 and spent the next
5 minutes blocked on the lock; concurrent cron tick at 08:10 hit
defect #3 and produced duplicate fire `b828dcd4`; that duplicate
delegated `create_html_page` (ASYNC) but ran out of timeout budget
~600ms before reaching `deliver_response`, so the document was
delivered without its accompanying chat message. Defect #4 hid all of
this from `RemindersService` and `WorkerHandler`, which logged
"delivered" while no end-user delivery actually occurred for the email
review.

This RFC consolidates four corrective steps that, taken together,
remove the architectural defects rather than patching their symptoms.

## 2. Goals

1. Eliminate `_execute_lock` as a latent serialization bottleneck.
2. Make notification SLA explicit and per-call instead of one global
   ceiling.
3. Make notification delivery success/failure observable to callers.
4. Make reminder firing idempotent under cron concurrency and Cloud
   Tasks retry.
5. Document the architectural decisions so future work does not regress.

Non-goals:
- Increasing Cloud Run vCPU. (Separately considered and deferred —
  see [docs/04_solution_strategy/decisions/cloud_tasks_vs_jobs.md].)
- Migrating reminders/consolidation to Cloud Run Jobs. (Same.)
- Introducing typed retry policies. (Defect #5 — left for follow-up
  RFC; not load-bearing for this incident.)

## 3. Architectural Principles Affirmed

- **Agent = computation, not state.** Per-call data (provider, model,
  thinking effort, intent remap) flows through `AgentMessage`, never
  via mutation of `self.*`. This is the architectural reason behind
  fix #4 / Step A below; the existing lock is a workaround for a
  long-standing principle violation.
- **Hexagonal boundaries strict.** Domain may not import ports; ports
  may not import services; services may not import concrete adapters.
  All new types respect existing boundaries.
- **Idempotency via stable identifiers.** Reminder fires are
  identified by `(note_id, due_at)` — not by `now()` or by claim
  tokens. The `due` value itself is the natural idempotency key; we
  use it directly in Firestore preconditions instead of inventing
  separate claim records.
- **At-most-once delivery for background notifications.** Acceptable
  trade-off for reminders and daily digests: a missed fire is
  preferable to a duplicate. Document the trade-off; do not paper
  over it.

## 4. Step A — Remove `SmartResponseAgent._execute_lock`

### Motivation

The lock exists at [src/agents/core/smart_response_agent.py:132](../../src/agents/core/smart_response_agent.py#L132)
solely because complexity override (lines 181–187) mutates instance
attributes:

```python
override = self.resolver.resolve(message.context, self.user_config)
if override:
    self.llm = override.execution_context.provider           # mutation
    self.model_name = override.execution_context.model_name   # mutation
    self._set_execution_context(override.execution_context)   # mutation
```

Two concurrent `process()` calls would race on these. The lock
serializes them — at the cost of stalling unrelated notifications
behind whichever caller is currently inside `_execute_locked`.

### Design

**Per-call execution context** is passed explicitly through the
existing `AgentMessage.context` channel; nothing is stored on `self.*`
that varies per call.

#### A.1 — Rename `TaskExecutionOverride` → `ExecutionOverride` (co-located with the resolver)

`ExecutionOverride` is structurally a value object (immutable
dataclass) describing a per-call override of the agent's default
execution parameters. Its natural home would be `domain/`, but every
candidate placement is blocked by an existing layer rule:

- `domain/` cannot import from `ports/`, and `ExecutionOverride`
  references `AgentExecutionContext` from `ports/llm_port.py`
  (`AgentExecutionContext` is a *whitelisted* exception that
  intentionally carries a runtime `LLMPort` reference — see
  `tests/unit/arch_tech_debt.py`).
- `ports/` cannot import from other `ports/` (REQ-ARCH-06 /
  `test_ports_do_not_import_other_ports`).
- `services/` cannot import from other `services/` (REQ-ARCH-22 /
  `test_services_do_not_import_other_services`). A separate file
  `services/execution_override.py` would force the resolver and any
  agent in another service file to violate this rule.

The clean placement that respects every layer rule is **co-locating
the value object with its sole producer** in
`src/services/task_execution_resolver.py`. Consumers
(`SmartResponseAgent`, etc.) import `ExecutionOverride` from that
module — agents are free to import from services without violating
REQ-ARCH-22.

```python
# src/services/task_execution_resolver.py
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..ports.llm_port import AgentExecutionContext


@dataclass(frozen=True)
class ExecutionOverride:
    """Per-call override of an agent's default execution parameters.

    Carried through AgentMessage.context["execution_override"] when set;
    resolved at execute() time. Never mutates the agent instance.
    """
    execution_context: AgentExecutionContext
    thinking_effort: Optional[str] = None
    intent_remap: Dict[str, str] = field(default_factory=dict)


class TaskExecutionResolver:
    ...
    def resolve(...) -> Optional[ExecutionOverride]: ...
```

Service stays in `services/`; the local class is simply renamed
(`TaskExecutionOverride` → `ExecutionOverride`) with a frozen-dataclass
upgrade. No new cross-layer imports — services
already import ports.

#### A.2 — SmartAgent resolves override locally per-call, no mutation

```python
async def execute(self, message: AgentMessage) -> AgentResponse:
    # NO LOCK. NO MUTATION OF self.*.
    effective = self._resolve_effective(message)
    return await self._run(message, effective)

def _resolve_effective(self, message: AgentMessage) -> _Effective:
    """Resolve per-call execution context + thinking effort.

    Priority:
      1. Explicit override on message (set by caller).
      2. Resolver based on message.context["task_complexity"].
      3. Agent defaults (self._default_ctx, self._default_thinking_effort).
    """
    override: Optional[ExecutionOverride] = message.context.get("execution_override")
    if override is None:
        override = self.resolver.resolve(message.context, self.user_config)
    if override is not None:
        return _Effective(
            ctx=override.execution_context,
            thinking_effort=override.thinking_effort or self._default_thinking_effort,
        )
    return _Effective(ctx=self._default_ctx, thinking_effort=self._default_thinking_effort)

async def _run(self, message: AgentMessage, eff: _Effective) -> AgentResponse:
    # All downstream code receives `eff` explicitly:
    #   - _build_llm_request takes (eff.ctx, eff.thinking_effort)
    #   - DelegationEngine.execute receives call_llm = lambda req: eff.ctx.provider.generate(req)
    # Nothing reads self.llm or self.model_name; those are removed.
```

`_Effective` is a private dataclass inside `smart_response_agent.py`
— no external API.

#### A.3 — Remove

- `self._execute_lock`, `_execute_locked` wrapper, `_set_execution_context`
- `self.llm`, `self.model_name` (use `eff.ctx.provider` and
  `eff.ctx.model_name` at call sites)
- The "Complexity override applied: model=..." log moves into `_resolve_effective`
  and reads from `eff.ctx`.

### Hexagonal boundaries

| File | Change | New imports | Imports removed |
|---|---|---|---|
| `services/task_execution_resolver.py` | Local `TaskExecutionOverride` renamed to `ExecutionOverride` (frozen dataclass) and kept in same module | (no new imports) | — |
| `agents/core/smart_response_agent.py` | Remove lock, per-call ctx | (no new) | `asyncio.Lock` no longer needed |

### Tests

- `tests/unit/ports/test_execution_override.py` — frozen dataclass
  invariants (immutability, equality).
- `tests/unit/agents/test_smart_concurrent_execution.py` (new) — two
  concurrent `agent.process(msg)` with different `execution_override`
  in `message.context`; assert no interference (each call sees its own
  provider/model).
- Update `tests/unit/agents/test_smart_response_agent.py` — drop any
  test that depended on `self.llm` mutation.

### Risk and rollback

- Per-call ctx is a strictly more general design than the current
  mutating model. If anything reads `self.llm` outside SmartAgent
  (it should not), `grep` will surface it. Risk low.
- Rollback: revert single commit; lock returns. No data migration.

## 5. Step B — Typed `NotificationKind` + per-kind SLA

### Motivation

Today `notify()` does not pass a per-message `timeout_ms`, so every
agent call inherits the agent's static `config.timeout_ms` (300 s for
Smart). This is correct for interactive conversation but wrong for
analytical batch tasks like email review, which need 15–25 minutes.

A simple `timeout_ms: int` parameter on `notify()` would work but
spreads magic numbers across callers. The enterprise pattern is to
classify the call by **purpose** and let the SLA table own the
numbers.

### Design

#### B.1 — Domain enum

```python
# src/domain/notification_kind.py
from enum import Enum


class NotificationKind(str, Enum):
    """Classification of background notification by purpose.

    Drives SLA: timeout budget, retry policy, observability tags.
    """
    INTERACTIVE       = "interactive"        # synchronous user-facing reply
    REMINDER          = "reminder"           # self-reminder fire
    DAILY_DIGEST      = "daily_digest"       # email review, news briefing
    DOCUMENT_DELIVERY = "document_delivery"  # async doc/PDF/HTML callback
    DEEP_RESEARCH     = "deep_research"      # async research result
```

#### B.2 — SLA table

```python
# src/infrastructure/notification_sla.py
from dataclasses import dataclass

from ..domain.notification_kind import NotificationKind


@dataclass(frozen=True)
class NotificationSLA:
    timeout_ms: int


NOTIFICATION_SLA: dict[NotificationKind, NotificationSLA] = {
    NotificationKind.INTERACTIVE:       NotificationSLA(timeout_ms=300_000),    # 5 min — interactive UX
    NotificationKind.REMINDER:          NotificationSLA(timeout_ms=600_000),    # 10 min — Smart + 1–2 specialists
    NotificationKind.DAILY_DIGEST:      NotificationSLA(timeout_ms=1_500_000),  # 25 min — multi-turn analysis
    NotificationKind.DOCUMENT_DELIVERY: NotificationSLA(timeout_ms=120_000),    # 2 min — formatting only
    NotificationKind.DEEP_RESEARCH:     NotificationSLA(timeout_ms=300_000),    # 5 min — kick-off only; execution lives in Cloud Run Job
}
```

`infrastructure/` is the right home: per-doc CLAUDE.md, infrastructure
holds tunable behavior parameters with typed config. Sibling pattern:
`agent_config.py`.

Note: `max_retries` and `retry_backoff_seconds` are deliberately NOT
in this table. Retry is queue-level (Cloud Tasks queue config) for
worker tasks and `BaseAgent.config_max_retries` for in-process; the
SLA table only governs timeout. Mixing concerns there causes the
exact opacity that defect #5 above describes.

#### B.3 — `UserNotificationService.notify()` signature change

```python
async def notify(
    self,
    *,
    kind: NotificationKind,
    user_id: str,
    account_id: str,
    system_alert: str,
    ...
) -> NotifyResult:  # see Step C
    sla = NOTIFICATION_SLA[kind]
    message = AgentMessage.create(
        ...,
        timeout_ms=sla.timeout_ms,
    )
    ...
```

`kind` is keyword-only (forces explicit choice at every call site;
no silent default).

#### B.4 — Caller migration

| Caller | Kind |
|---|---|
| `WorkerHandler._handle_daily_email_review` | `DAILY_DIGEST` |
| `WorkerHandler._handle_execute_reminder` (new — Step D) | `REMINDER` |
| `AgentWorkerHandler` (async doc delivery) | `DOCUMENT_DELIVERY` |
| `LanguagePreferenceService.notify_language_change` (if exists) | `INTERACTIVE` |
| `DeepResearchDelivery.deliver` | `DEEP_RESEARCH` |

### Hexagonal boundaries

`domain/notification_kind.py` — pure stdlib. `infrastructure/notification_sla.py`
— imports domain only. `services/user_notification_service.py` already
imports from infrastructure (`agent_config.py` precedent).

### Tests

- `tests/unit/domain/test_notification_kind.py` — enum values stable
  (string-typed for Firestore/JSON serialization).
- `tests/unit/services/test_user_notification_service_sla.py` —
  parametrized: each `NotificationKind` produces an `AgentMessage`
  with the expected `timeout_ms`.

## 6. Step C — `NotifyResult` (defect #4)

### Motivation

`UserNotificationService.notify()` currently swallows
`AgentStatus.FAILED` with a `return` — caller has no way to react.

### Design

```python
# src/domain/notification_kind.py (same file)
from dataclasses import dataclass
from typing import Optional

from .agent import AgentStatus


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of a notify() call. Returned to caller for retry / reschedule decisions."""
    delivered: bool
    agent_status: AgentStatus
    error: Optional[str] = None
```

`notify()` returns `NotifyResult` for both success and failure paths:

```python
if response.status != AgentStatus.SUCCESS:
    logger.warning(f"[Notification] Agent returned {response.status} ...")
    return NotifyResult(delivered=False, agent_status=response.status, error=...)

# ... deliver to channel, save history ...

return NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)
```

### Caller behavior

- `WorkerHandler._handle_daily_email_review` returns HTTP 500 if
  `not result.delivered` → Cloud Tasks queue retries (configurable;
  current queue retries 3 times).
- `WorkerHandler._handle_execute_reminder` (new) returns HTTP 500 on
  failure → same.
- `AgentWorkerHandler` (async doc) logs ERROR on failure but returns
  HTTP 200 — the document is already in GCS; retrying would re-upload
  and re-deliver the file.

### Partial delivery semantics (b828dcd4 case)

When `create_html_page` (ASYNC) was successfully delegated but Smart
timed out before sending the chat message, the file went to GCS via
the ASYNC Cloud Task path while the chat message was lost. After this
RFC:

- Step A removes the cascading lock-wait, so Smart has its full SLA
  budget. Step B raises the budget for `DAILY_DIGEST`. Together they
  make the partial-delivery race extremely rare.
- We do NOT add an explicit "partial delivery" state. Trade-off: rare
  case, retry would re-send the file. Documented; revisit if observed
  in practice.

### Tests

- `tests/unit/services/test_user_notification_service_result.py`
  (new) — three cases: success → `delivered=True`; agent FAILED →
  `delivered=False`; exception → `delivered=False` with error.
- Update existing reminder/worker tests to assert on `NotifyResult`.

## 7. Step D — Idempotent reminder fire (defect #3)

### Motivation

`fire_due_reminders` synchronously awaits `notify()`. With pre-Step-A
behavior, that await blocks ≥ 5 min on `_execute_lock`, allowing the
next 5-min cron tick to re-fire the same note.

After Step A the lock is gone and `notify()` returns within its SLA.
The race window narrows but does not close: between
`list_due_reminders` (which sees `due ≤ now`) and `reschedule` (which
sets `due` forward), two concurrent ticks can both observe the same
`due` and both proceed.

### Design

#### D.1 — Cron handler reschedules BEFORE enqueueing the fire

The current handler does:
```
list_due_reminders → for each note: notify (await) → reschedule
```

The new handler does:
```
list_due_reminders → for each note: try-reschedule (atomic) → enqueue Cloud Task
```

The Cloud Task is the new task type `execute_reminder` which actually
calls `notify()`. The cron HTTP handler returns within seconds.

#### D.2 — Atomic reschedule with precondition

Replace `reschedule(note_id, next_due, last_fired)` with:

```python
# src/ports/agent_note_port.py
async def reschedule_if_due_at(
    self,
    note_id: str,
    expected_due: datetime,
    next_due: datetime,
    last_fired: datetime,
) -> bool:
    """
    Atomically reschedule the note ONLY IF its current `due` equals `expected_due`.

    Returns True if rescheduled (this caller owns this fire).
    Returns False if `due` has already moved (another cron tick handled it).

    Idempotency primitive. The caller MUST treat False as "skip — someone else owns it".
    """

async def delete_if_due_at(
    self,
    note_id: str,
    user_id: str,
    expected_due: datetime,
) -> bool:
    """One-time variant: delete only if `due == expected_due`. Same semantics."""
```

Firestore implementation: `transaction.update` with a precondition on
`due`. If the read inside the transaction shows a different `due`,
the transaction fails — return False without raising.

#### D.3 — Worker-side idempotency (`last_delivered_due`)

Cloud Tasks queue retries on HTTP 500 (current default: 3 attempts).
Without delivery tracking, a retry of `execute_reminder` re-runs Smart
and re-delivers — duplicating the user-visible message.

Add one field to AgentNote and one port method:

```python
# src/domain/agent_note.py
@dataclass
class AgentNote:
    ...
    last_delivered_due: Optional[datetime] = None  # set by worker on successful delivery


# src/ports/agent_note_port.py
async def mark_fire_delivered(self, note_id: str, due_at: datetime) -> None:
    """Record that the fire scheduled for `due_at` has been delivered.

    Idempotency token consumed by `_handle_execute_reminder` to short-circuit
    duplicate Cloud Tasks deliveries.
    """
```

Worker:

```python
async def _handle_execute_reminder(self, payload: dict) -> Tuple[dict, int]:
    note_id  = payload["note_id"]
    user_id  = payload["user_id"]
    due_at   = datetime.fromisoformat(payload["due_at"])

    note = await self._notes_port.get_note(user_id, note_id)
    if note is None:
        return {"status": "note_gone"}, 200            # one-time deleted, recurrent missing — both fine
    if note.last_delivered_due == due_at:
        return {"status": "already_delivered"}, 200    # Cloud Tasks retry, idempotent no-op

    user_profile = await self._user_repo.get_user(user_id)
    if user_profile is None:
        return {"status": "no_user"}, 200

    result = await self._notification.notify(
        kind=NotificationKind.REMINDER,
        user_id=user_id,
        account_id=user_profile.account_id,
        system_alert=_build_reminder_alert(note),
        agent_id_override=f"smart_response_agent_{user_id}",
        task_complexity=note.complexity.value if note.complexity else "simple_analytics",
    )
    if not result.delivered:
        return {"error": result.error or "delivery_failed"}, 500

    await self._notes_port.mark_fire_delivered(note_id, due_at)
    return {"status": "ok"}, 200
```

#### D.4 — `_CRON_WINDOW_SECONDS` removed

The 4-minute `last_fired` guard is no longer needed: `reschedule_if_due_at`
provides exact-once cron-side semantics, and `last_delivered_due`
provides exact-once worker-side semantics. Remove the constant; its
purpose is fully subsumed.

### Hexagonal boundaries

| File | Change |
|---|---|
| `domain/agent_note.py` | Add `last_delivered_due: Optional[datetime] = None` |
| `ports/agent_note_port.py` | Replace `reschedule` with `reschedule_if_due_at`, add `delete_if_due_at`, add `mark_fire_delivered`, add `get_note(user_id, note_id)` if missing |
| `adapters/firestore_agent_note_adapter.py` | Implement new methods via Firestore transactions |
| `services/reminders_service.py` | New control flow: try-reschedule → enqueue, no awaiting notify |
| `handlers/worker_handler.py` | New task type `execute_reminder` |
| `infrastructure/agent_manifest.py` (or wherever task types are listed) | Register `execute_reminder` |

### Tests

- `tests/integration/adapters/test_firestore_reschedule_if_due_at.py`
  (new) — two concurrent `reschedule_if_due_at` calls with same
  `expected_due`: exactly one returns True.
- `tests/unit/services/test_reminders_service.py` — update for new
  control flow; assert `enqueue_worker_task("execute_reminder", ...)`
  is called only after successful `reschedule_if_due_at`.
- `tests/unit/handlers/test_worker_handler_execute_reminder.py` (new)
  — three paths: first call delivers and marks; retry sees
  `last_delivered_due == due_at` and short-circuits; failed
  `notify()` returns 500.

### Migration

`last_delivered_due` is a new optional field with default `None`.
Existing notes load fine. No backfill required.

`reschedule_if_due_at` replaces `reschedule` — single call site in
`RemindersService.fire_due_reminders`. Atomic edit per commit.

`_CRON_WINDOW_SECONDS` constant removal: dead code after the new
control flow. Single commit.

## 8. Test Coverage Mandate

This refactor touches concurrency, idempotency, distributed-state
preconditions, and silent-failure paths — exactly the surfaces where
defects hide and regressions silently regrow. Coverage is **not
optional, not partial, not "we'll add tests later"**. It is part of
the definition-of-done for every commit.

### 8.1 — Unit test mandate

Every public function, port method, dataclass invariant, and code
path introduced or modified by this RFC has a unit test. Concretely:

| Layer | What is mandatory |
|---|---|
| `domain/notification_kind.py` | All enum values; serialization round-trip; `NotifyResult` immutability and equality |
| `services/task_execution_resolver.py` (`ExecutionOverride`) | Frozen dataclass invariants; equality; field defaults |
| `infrastructure/notification_sla.py` | Every `NotificationKind` has an SLA entry; SLA values asserted explicitly (regression catches accidental changes); table is exhaustive (no missing enum value) |
| `services/task_execution_resolver.py` | Resolver returns `ExecutionOverride` when `task_complexity` is set; returns `None` when absent or invalid; resolves user override correctly when present |
| `agents/core/smart_response_agent.py` | `_resolve_effective` priority chain (explicit override > resolver > defaults); concurrent `process()` calls with different overrides do not interfere; `self.llm` / `self.model_name` / `self._execute_lock` are gone (negative test); error path returns `AgentResponse.failure` not exception |
| `services/user_notification_service.py` | `notify(kind=...)` propagates `sla.timeout_ms` to `AgentMessage.timeout_ms` for every `NotificationKind`; returns `NotifyResult(delivered=True)` on success; returns `NotifyResult(delivered=False, agent_status=FAILED)` when agent reports FAILED; returns `NotifyResult(delivered=False, error=...)` on exception in route_message; `kind` is keyword-only (positional call raises TypeError) |
| `services/reminders_service.py` | Successful claim → enqueue Cloud Task with correct payload (note_id, user_id, due_at ISO format); failed claim (False return) → no enqueue; one-time reminder → `delete_if_due_at` instead of `reschedule_if_due_at`; `notify()` is no longer called from cron handler (negative test) |
| `handlers/worker_handler.py` (`_handle_execute_reminder`) | First call → notify → mark_fire_delivered, returns 200; retry with `last_delivered_due == due_at` → no notify, returns 200 with `already_delivered`; failed notify → no mark, returns 500; missing note → returns 200 `note_gone`; missing user → returns 200 `no_user` |
| `domain/agent_note.py` | `last_delivered_due` field default is `None`; equality / hash unchanged by new field for existing values |

Every caller migration in commit 5 (worker_handler, reminders_service,
agent_worker_handler, deep_research_delivery, language_preference_service)
gets a unit test asserting the correct `kind` is passed and
`NotifyResult` is correctly handled.

### 8.2 — Integration test mandate

Concurrency and Firestore preconditions cannot be unit-tested with
mocks — substitution at the port level cannot detect transaction
contention. These get integration tests against the Firestore
emulator (`make dev-emulator`):

| Test | Asserts |
|---|---|
| `tests/integration/adapters/test_firestore_reschedule_if_due_at.py` | Two `asyncio.gather` calls of `reschedule_if_due_at` with same `expected_due` → **exactly one** returns True. The other returns False. Final state matches the winner's `next_due`. Run 50 iterations to surface non-determinism |
| `tests/integration/adapters/test_firestore_delete_if_due_at.py` | Same pattern for one-time reminder delete |
| `tests/integration/adapters/test_firestore_mark_fire_delivered.py` | `mark_fire_delivered` is idempotent (calling twice is safe); does not affect other note fields |
| `tests/integration/agents/test_smart_concurrent_per_user.py` | Two concurrent `process()` for the same `smart_response_agent_<user>` instance with different `execution_override` complete in parallel (wall-clock < 2 × single-call time); each receives the correct provider/model in its own `_run` |
| `tests/integration/services/test_reminders_e2e.py` | Full path: insert due note → call `fire_due_reminders` → assert Cloud Task enqueued with correct payload, note rescheduled. Then call `_handle_execute_reminder` with that payload → assert `last_delivered_due` written, notification delivered to mock channel |
| `tests/integration/services/test_reminders_concurrent_cron.py` | Insert one due note, run `fire_due_reminders` from two coroutines concurrently → exactly one Cloud Task enqueued total; both calls return without error |

### 8.3 — Contract validators (per existing repo pattern)

`tests/contracts/agent_contracts.py` (or extend existing file) — add
named `ContractRule` for:

- `notify_returns_notify_result` — every concrete `notify()` call
  returns a `NotifyResult`, never `None` or implicit return.
- `notify_kind_required` — every `notify()` call passes `kind=`
  explicitly.
- `agent_no_per_call_mutation` — `SmartResponseAgent.execute()` does
  not assign to `self.llm`, `self.model_name`, or
  `self._agent_execution_context`. Static check; runs in CI.

### 8.4 — Coverage threshold

After commit 8, run `pytest --cov=src/services/user_notification_service
--cov=src/services/reminders_service --cov=src/agents/core/smart_response_agent
--cov=src/handlers/worker_handler --cov-report=term-missing` and assert:

- **100%** statement coverage on `user_notification_service.py`,
  `reminders_service.py`, `smart_response_agent.py` — these are the
  files that bit us. No uncovered branch is acceptable.
- **≥ 95%** on `worker_handler._handle_execute_reminder` and
  `firestore_agent_note_adapter.py` (allowing for edge-case error
  paths that require dedicated mocks).

Below threshold → commit blocked.

### 8.5 — No retroactive test relaxation

The CLAUDE.md rule applies in full: **no existing test is modified or
deleted to make new code pass without explicit per-test approval**.
If a refactored caller breaks an existing test, the test is reported
to the user with the failing assertion; the user decides per-test.

## 9. Migration Plan (Commit Sequence)

Each step is one commit, one PR, fully revertable. **Each commit
must ship with its full test suite (unit + integration where
applicable) per § 8 mandate. A commit without its tests is
considered incomplete and must not be merged.**

| # | Commit | Files | Tests required |
|---|---|---|---|
| 1 | Rename `TaskExecutionOverride` → `ExecutionOverride` (frozen, co-located with resolver) | `services/task_execution_resolver.py` | unit: `test_execution_override.py` (frozen, equality, defaults), `test_task_execution_resolver.py` (return type, all branches) |
| 2 | SmartAgent: drop `_execute_lock`, per-call ctx | `agents/core/smart_response_agent.py` | unit: `_resolve_effective` priority chain (3 cases) + negative tests on removed mutations + error path; integration: `test_smart_concurrent_per_user.py` (parallel calls do not interfere, wall-clock proof) |
| 3 | `NotificationKind` enum + `NOTIFICATION_SLA` | `domain/notification_kind.py`, `infrastructure/notification_sla.py` | unit: enum values stable, `NOTIFICATION_SLA` exhaustive over enum, every kind's timeout asserted explicitly |
| 4 | `NotifyResult` + signature change in `notify()` | `domain/notification_kind.py`, `services/user_notification_service.py` | unit: 3 outcome paths (SUCCESS, FAILED, exception), `kind=` keyword-only enforcement, `timeout_ms` propagation per kind |
| 5 | All `notify()` callers pass `kind` and handle `NotifyResult` | `handlers/worker_handler.py`, `services/reminders_service.py`, `services/agent_worker_handler.py`, `services/deep_research_delivery.py`, etc. | unit per caller: correct `kind`, correct branching on `NotifyResult.delivered` |
| 6 | AgentNote `last_delivered_due` + port methods | `domain/agent_note.py`, `ports/agent_note_port.py`, `adapters/firestore_agent_note_adapter.py` | unit: domain field default, port abstract surface; integration: `test_firestore_reschedule_if_due_at.py` (50-iter concurrency), `test_firestore_delete_if_due_at.py`, `test_firestore_mark_fire_delivered.py` (idempotency) |
| 7 | `RemindersService` new control flow: try-reschedule → enqueue | `services/reminders_service.py` | unit: claim success → enqueue, claim failure → no enqueue, no notify call from cron handler; integration: `test_reminders_concurrent_cron.py` (two parallel cron ticks → exactly one Cloud Task) |
| 8 | `WorkerHandler._handle_execute_reminder` | `handlers/worker_handler.py` | unit: 5 paths (deliver+mark, retry-already-delivered, notify failed → 500, missing note, missing user); integration: `test_reminders_e2e.py` (full claim → enqueue → execute → mark cycle) |
| 9 | Coverage gate: assert thresholds from § 8.4 in CI | `pyproject.toml` or `pytest.ini` | — (CI config) |
| 10 | RFC + decision records (this file + decisions/) | docs only | — |

## 10. Verification (end-to-end, after all unit + integration tests pass)

After commit 8, manual triggers:

1. **Email review**:
   `make trigger-daily-email-review-dev` (or curl from CLAUDE.md
   manual trigger section).
   Expected log sequence:
   - `[Worker] daily_email_review: delivered N emails to Smart`
   - `Delegating intent='create_html_page'` (turn 6–8)
   - `Document link delivered to slack ...` (chat-message-bearing
     `deliver_response` BEFORE the doc link, OR after — but both)
   - No `timeout on attempt 1` warnings
   - No `Failure recorded` warnings
2. **Reminder fire** (one-time test reminder for dev_user, due in
   30 s):
   - cron `fire_due_reminders` log shows
     `enqueued execute_reminder for note=…`
   - `execute_reminder` Cloud Task runs, completes with `status=ok`
   - Slack DM receives the chat message
   - Second cron tick sees the same note as already rescheduled
     (`due` is in the future); no duplicate fire
3. **Reminder Cloud Tasks retry** (simulate by raising
   `RuntimeError` once in `notify()`):
   - First Cloud Task attempt → 500 → retry
   - Second attempt: `notify()` succeeds; `mark_fire_delivered` runs
   - Third attempt (if any from the original 500): sees
     `last_delivered_due == due_at`, returns `already_delivered`,
     no duplicate user-visible message

## 11. Out of Scope

- **Typed retry policy in BaseAgent** (defect #5). Worth doing —
  separate CIRCUIT_BREAKER_AND_RETRY_RFC. Acceptable to skip now;
  Cloud Tasks queue-level retry covers worker tasks; SmartAgent
  `config_max_retries=0` is fine after Step A removes the lock.
- **Improved logging** (defect #6). Should add `task_id` to
  `_execute_with_timeout` warning and include actual timeout value.
  One-line fix; included in commit 2.
- **CPU upgrade to 2 vCPU**. Considered and rejected — see
  [decisions/cloud_tasks_vs_jobs.md](../04_solution_strategy/decisions/cloud_tasks_vs_jobs.md).

## 12. Decision Records Created

- [docs/04_solution_strategy/decisions/per_call_execution_context.md] — Agents do not store per-call state on `self.*`.
- [docs/04_solution_strategy/decisions/cloud_tasks_vs_jobs.md] — Cloud Tasks for notify/reminder/consolidation; Cloud Run Jobs only for DeepResearch and future long-running batch.
