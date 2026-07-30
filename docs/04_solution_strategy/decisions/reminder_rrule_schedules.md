# Reminders: RRULE schedules behind a port, and the full note visible to the orchestrator

**Date:** 2026-07-30
**Status:** Accepted
**Amends:** `docs/10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md` (schedule model + context projection)

## Context

`ReminderRecurrence` was `{type, interval}` — one unit, one multiplier. "Tuesdays and
Fridays", "08:00 and 20:00", "the last Sunday of the month" were inexpressible, so the agent
answered such requests with **duplicate reminders**, which then drift apart: separate `due`,
separate `last_fired`, separate edits, two notifications.

Independently, the reminder object was projected lossily on both read paths. The orchestrator's
`active_reminders {}` block carried `{note_id, text, due}` only — it could not see the schedule
it was expected to edit, nor `complexity`, nor `last_fired`. The specialist's own block showed
the recurrence *type* but not the interval, so no layer in the system could answer "how often
does this fire?". And `NoteUpdate.recurrence=None` already meant "leave unchanged", so a
repeating reminder could never be made one-time — only deleted and recreated.

## Decision

**1. The schedule is an RFC 5545 RRULE string, stored without `DTSTART`.** The note's `due` is
the anchor and the first fire, so the rule stays a pure pattern. `AgentNote.recurrence: Optional[str]`;
`ReminderRecurrence` is gone.

**2. Evaluation lives behind `RecurrencePort`** (`normalize` / `first_occurrence` /
`next_occurrence` / `describe`, all synchronous — pure computation), implemented by
`DateutilRecurrenceAdapter`. `domain/` may not import `dateutil`, and three layers need the
algebra (RemindersService for the next fire, NotesAgent for validation and the creation-time
snap, the Cabinet for display), so it is a port rather than a helper.

**3. Rules are open-ended.** `COUNT`/`UNTIL`/`DTSTART` are rejected by `normalize`, along with
sub-hourly `FREQ` (the cron ticks every 15 min) and patterns that never occur. Every expansion
is bounded by a 10-year horizon, so an unmatchable rule returns `None` instead of spinning.

**4. Both read paths carry the whole note** — rule verbatim, complexity, last fire — rendered
only when set (the orchestrator's block sits after the cache boundary and is re-sent every request).
The orchestrator edits by quoting the rule it can now see.

**5. `NoteUpdate.clear_recurrence`** makes "stop repeating" expressible without delete+recreate.

## Alternatives rejected

- **Typed `days_of_week` on the existing value object.** Covers "Tue and Fri" and nothing else;
  the owner named "08:00 and 20:00" and "last Sunday of the month" as next week's requests —
  a second extension of the same shape was already visible, so the shape was wrong.
- **RRULE evaluated inline in the service** (`dateutil` is already imported there). Leaves
  NotesAgent unable to validate or snap without importing a service, which REQ-ARCH-22 forbids.
- **Hand-rolled RRULE subset in `domain/`.** Keeps the algebra pure, re-implements a standard.
- **Humanised rule stored alongside the RRULE.** Two fields to keep in sync; `describe()` at
  the point of display cannot drift.
- **Backfilling the legacy `{type, interval}` documents.** `_read_recurrence` translates on
  read (a total mapping) and the next update rewrites the doc; a migration adds risk for nothing.

## Consequences

- **Month-end semantics change.** `relativedelta` clamped Jan 31 → Feb 28; RFC 5545 skips to the
  next month that has a 31st. Clamping silently moved the reminder to a different day of the
  month. "The last day" is now said explicitly: `BYMONTHDAY=-1`.
- **Cabinet API contract:** `recurrence` is a string, plus `recurrence_label` (phrased
  server-side), `complexity`, `last_fired`. The UI keeps four presets and adds a custom-rule
  field; an empty string clears the schedule.
- **Prompt tokens are part of the change, not a follow-up.** `COGNITIVE_PROCESS_NOTES` and
  `PROTOCOL_SMART_AGENT_SELECTION` taught the old model; both are patched by
  `scripts/prompt/migrate_reminder_rrule_tokens.py` (anchored edits, backup, `--revert`).
  Apply them **with the deploy** — an RRULE-teaching prompt against the old code emits a string
  into an object-typed tool parameter.

## Verification

`tests/unit/adapters/test_dateutil_recurrence_adapter.py` — normalize (rejects bounded rules,
DTSTART, unknown/sub-hourly FREQ, never-occurring patterns), parity with the old arithmetic
(hourly/daily/weekly/monthly, DST wall clock, UTC output), the schedules the old model could not
express, and the creation-time snap. `tests/unit/ports/test_recurrence_port.py` — contract.
`tests/unit/adapters/test_firestore_agent_note_adapter.py` — RRULE persistence, `clear_recurrence`,
legacy-map reads. `tests/unit/agents/test_notes_agent.py` — rule normalization + tool paths.
`tests/unit/services/test_reminders_service.py` — claim/enqueue flow against the real evaluator.
