<!-- living-doc
covers:
  src/agents/notes_agent.py
  src/domain/agent_note.py
  src/ports/agent_note_port.py
  src/adapters/firestore_agent_note_adapter.py
  src/services/reminders_service.py
  src/services/execute_reminder_task.py
  src/domain/reminder_alert.py
  src/domain/literal_notice.py
  src/ports/recurrence_port.py
  src/adapters/dateutil_recurrence_adapter.py
  scripts/prompt/migrate_reminder_rrule_tokens.py
-->
# Self-Reminders

## Purpose

A self-reminder is a **deferred instruction the bot writes to itself**. When it fires, the instruction runs as a full
system turn through the Smart agent with its whole toolset, in the owner's channel session (so Smart sees that
channel's history). The turn input is a system-marked alert that wraps the instruction, not text the owner typed. It does not wait for the owner to write first: "in two hours, check the flight" and "every morning,
brief me" both work, and the schedule is the only thing the owner sees as "a reminder".

Two text fields carry the idea. `text` is a short label (capped by `MAX_WORDS_PER_NOTE` in the adapter) shown in
compact prompt blocks; `instruction` is the full execution context and the only part of the note the fired turn
receives, so the specialist writes it self-contained.

What it is not: not the owner's task list (that is [Microsoft To Do](../tasks_integration/README.md)), not a memory write
(the bot never writes memory itself; see [Memory](../memory/README.md) for how a fired turn's messages are treated), and not a delivery mechanism of its own: delivery is `notify()` from
[Rich Content and Notifications](../rich_content_protocol/README.md).

## Owned data

Collection `EnvironmentConfig.orchestrator_notes_collection` (`{prefix}orchestrator_notes`), one document per note, id
`{epoch_ms}-{uuid hex}` (older documents carry a bare epoch-ms id; the epoch prefix is what the orchestrator uses to
"gauge the age" of a reminder). No TTL: a note leaves by deletion only. Single-field indexes on `user_id` and `due`
suffice.

Fields (`src/domain/agent_note.py::AgentNote`):

- `user_id`, `text`, `instruction`, `created_at`.
- `due` (UTC) is the next fire time **and** the anchor of the schedule.
- `recurrence`: an RFC 5545 RRULE string without `DTSTART`, or null for a one-time note. The anchor is always `due`, so
  the rule is a pure pattern.
- `last_fired`: when the cron last claimed a fire. For a one-time note this is the at-most-once guard (see Invariants).
- `last_delivered_due`: the `due` of the fire whose turn finished; the idempotency stamp against Cloud Tasks retries.
- `complexity` (`TaskComplexity`, optional): chosen by the specialist from the instruction; picks the model tier the
  fire runs at and the time budget it gets. Null means the default `simple_analytics`.
- `workspace_file` (optional): a workspace path the run uses; bound by code, not by the model (see Flows).

Writers: `NotesAgent` (chat), the Cabinet reminder routes (`src/web/user_cabinet_app.py`, `/api/user/reminders`), and the
fire path (`RemindersService`, `ExecuteReminderTask`). Readers: the same, plus `RouterAgent._note_context` for the
orchestrator prompt. The adapter bounds the owner by `MAX_NOTES_PER_USER` active notes, on every create path (chat and
Cabinet POST; the Cabinet maps the refusal to 400, while `NotesAgent._execute_tool` does not catch it); the specialist
also warns the model past `_NOTES_SOFT_THRESHOLD`. The cap is a read before the write, so concurrent creates can pass it
together.

Legacy shapes are read, never backfilled: an old `{type, interval}` recurrence map is
translated on read (`FirestoreAgentNoteAdapter._read_recurrence`) and stays stored until an update writes `recurrence`
itself, a note without `instruction`
reads its `text`, and the removed `visible_after` / `expires_after` fields are ignored.

## Flows

### Create, edit, delete (chat)

1. The Smart orchestrator sees the active reminders in its prompt (`src/agents/core/router_agent.py::RouterAgent._note_context` feeds the
   `active_reminders {}` block of `src/services/prompt_v3/prompt_assembly_service.py`; it omits the instruction) and delegates the intent `manage_self_reminders` (declared in
   `src/infrastructure/agent_manifest.py`) in natural language. The block is a snapshot, stale by design within a turn:
   a specialist's result supersedes it.
2. `src/agents/notes_agent.py::NotesAgent.execute` runs a bounded tool loop (`MAX_TURNS`) over the full list
   (this time with each `instruction`); CRUD tools run against `AgentNotePort` (`NotesAgent._execute_tool`), followed by a
   tool-less summary call. Every mutation sends a transparency notice to the owner (`NotesAgent._notify`; best-effort,
   failures are swallowed); create and update pass the model's words as `literal`, delete does not. The create and
   update notices are literal notices (`src/domain/literal_notice.py::render_literal_notice`, shared with the
   `$memory save` preview and the external task receipt): the instruction the reminder will run, byte for byte, as the
   file `reminder.txt` (on an update: the stored instruction after it, changed or not), then a literal message with the
   name and the schedule line. `UserNotificationService.notify_raw(..., attachment=notice)` posts the file before the
   text. A reminder with an empty instruction gets the message alone. The notices are shown only: a reminder needs no
   confirmation, since Smart may set one at Lelik's request.
3. `due` arrives as ISO-8601; a time without an offset is the owner's wall time in `UserBotConfig.timezone`
   (`src/domain/local_time.py::wall_time_instant`). A wall time the clocks skip is refused, never guessed.
4. A recurrence goes through `RecurrencePort.normalize` (a rejected rule comes back to the model as a tool error it can
   fix) and the proposed `due` is then **snapped** with `first_occurrence`: the rule owns the schedule, the proposed due
   only means "not before". "Tue and Fri" with a Wednesday due fires Friday. The Cabinet POST route does the same; the Cabinet PUT route
   does not snap, it stores the given rule and due as they are.
5. Editing: `NoteUpdate` is PATCH, so None means "unchanged"; "stop repeating" and "drop the workspace file" are the
   explicit `clear_recurrence` / `clear_workspace_file` flags.
6. `workspace_file` is bound by code: `NotesAgent.execute` validates the path from the delegation context before any LLM
   call and applies it to exactly one create or update (`_BindGuard`), so the model cannot rewrite or mis-attach it
   ([Agent Workspace RFC](../../10_rfcs/AGENT_WORKSPACE_RFC.md) § 8.3).

### Fire (cron tick)

1. Cloud Scheduler posts `fire_due_reminders` (cadence in [SCHEDULERS](../../07_deployment/SCHEDULERS.md)); `WorkerHandler._handle_fire_due_reminders` calls
   `src/services/reminders_service.py::RemindersService.fire_due_reminders`.
2. `AgentNotePort.list_due_reminders` scans all users for `due <= now`.
3. Each note is **claimed atomically** before anything is enqueued. A recurring note moves to its next fire
   (`reschedule_if_due_at`, a transaction that requires `due` to still equal what the tick read); a one-time note is
   marked fired but **not deleted** (`claim_one_time_if_due_at`), so the worker can still read it. A lost claim (another
   tick won) is skipped silently.
4. The next fire is the rule's first occurrence **after now**, stepped from the old `due` so the time-of-day grid is kept
   (`RemindersService._next_due_after_now`). Occurrences missed during an outage are skipped, not replayed. A rule with
   no next occurrence leaves the note untouched and logged: it stops firing, its `due` stays in the past (so `list_active_notes` hides it from the orchestrator) and only the Cabinet
   list, which shows every note, still shows it.
5. A won claim enqueues one `execute_reminder` Cloud Task (`RemindersService._enqueue_fire`), named by note and fire
   time, with the REMINDER deadline injected by composition. The create is retried by name, because no later tick will
   enqueue a claimed fire.

### Deliver one fire (Cloud Task)

1. `src/services/execute_reminder_task.py::ExecuteReminderTask.__call__` re-reads the note (ownership-scoped) and answers
   `note_gone`, `no_user` or `already_delivered` (`last_delivered_due == due_at`) without running anything.
2. It builds the alert (`src/domain/reminder_alert.py::build_reminder_alert`), resolves the SLA tier from the note's
   complexity through the same merge `TaskExecutionResolver` uses, and calls `UserNotificationService.notify` with
   `NotificationKind.REMINDER` (`src/infrastructure/notification_sla.py`): the budget is per tier, so a deep-reasoning
   reminder gets a longer clock than a one-line one. The turn runs on the owner's Smart agent, tagged with a fire key for
   the workspace.
3. The alert tells the model the reminder is its own earlier intent, hands it `note_id` and the rule, and invites it to
   act on anything valuable now. The history keeps only a name-only summary line
   (`build_reminder_alert_summary`) once the turn ages out.
4. Outcome by HTTP status: only "the turn never started" (`is_retryable_failure`) answers 500 and is retried. Every
   outcome once the turn started is final and 200: ok, `empty_answer_final`, `turn_cap_final`, `failed_final`. A retry
   would repeat the actions the turn already took.
   A turn may delete its own note (the "until X" case); the stamp then finds no note and is a no-op, so the outcome stays
   final and 200.
5. Final outcomes stamp `last_delivered_due` first, then tell the owner once that the reminder did not run when nothing
   was sent (`UIMessage.REMINDER_NOT_RUN`), then delete a one-time note.

## Invariants and traps

- **Claim before enqueue, and the claim is the dedup.** `(note_id, due)` is the natural key: `reschedule_if_due_at` and
  `claim_one_time_if_due_at` fail unless `due` is unchanged, and the Cloud Task name repeats it. Concurrent ticks cannot
  produce two fires. A crash or timeout after the turn started but before `mark_fire_delivered` re-runs the turn on the
  Cloud Tasks retry (an accepted residual, noted in `ExecuteReminderTask.__call__`). Tests: `tests/unit/services/test_reminders_service.py`,
  `tests/unit/services/test_execute_reminder_task.py`, `tests/unit/adapters/test_agent_note_store_kit.py`.
- **One-time at-most-once rests on `last_fired >= due`.** `due` does not move for a one-time note, so after the first
  claim every later tick fails the guard. Until the worker deletes the note, `list_due_reminders` still returns it on
  every tick, each ending as a lost claim. That is expected, not a leak.
- **`list_active_notes` means `due > now`.** A claimed one-time note awaiting delivery is therefore invisible to the
  orchestrator and not counted against the cap. A recurring note stays active because its `due` already moved, except when the rule yields no next occurrence (see Fire).
- **The cron cadence is a contract with the rule validator.** `src/adapters/dateutil_recurrence_adapter.py` refuses any
  two occurrences closer than `_MIN_MINUTELY_INTERVAL` minutes (a probe of the first `_GAP_PROBE_OCCURRENCES`
  occurrences, not a proof), because a finer rule cannot be honoured at the scheduler's tick and would fire every tick.
  Change the floor and the Scheduler job together ([SCHEDULERS](../../07_deployment/SCHEDULERS.md)). Stored rules are
  not re-validated.
- **Rules are open-ended by design.** `COUNT`, `UNTIL`, `DTSTART`, `EXDATE`, `RDATE` and never-occurring patterns are
  rejected by `normalize`; every expansion is bounded by `_HORIZON_YEARS`, so a bad rule yields None instead of hanging.
  "Until X happens" is written into the `instruction`: the run that sees X deletes the reminder.
- **Wall clock, DST and month ends.** Evaluation happens on the owner's local wall clock and converts back to UTC, so
  "daily at 09:00" stays 09:00. DAILY and coarser rules keep the anchor's pass of a repeated autumn hour; HOURLY and
  MINUTELY step in real time. A rule needing the 31st skips short months (RFC 5545), it does not clamp; "the last day"
  is `BYMONTHDAY=-1`. Tests: `tests/unit/adapters/test_dateutil_recurrence_adapter.py`, `test_recurrence_instants.py`,
  `test_recurrence_real_time_steps.py`.
- **The rule is shown raw to models, phrased to people.** Prompt blocks and the alert carry the exact RRULE so the
  orchestrator can quote and edit it; owner-facing notices and the Cabinet use `RecurrencePort.describe`.
- **One note, one schedule.** Any repeat pattern is one note with one rule, never several notes; the
  manifest description tells the orchestrator to state the whole schedule and not split it.
- **Prompt tokens ship separately from code.** `COGNITIVE_PROCESS_NOTES` and `PROTOCOL_SMART_AGENT_SELECTION` teach the
  RRULE vocabulary and live in Firestore; they are patched by `scripts/prompt/migrate_reminder_rrule_tokens.py`, which
  must be applied together with the deploy of the matching code.
- **The fire's cost is a Cloud Run request.** The dispatch deadline derives from the REMINDER SLA
  (`src/infrastructure/notification_sla.py::dispatch_deadline_s`); services may not import it, so composition injects it
  into `RemindersService`. Left unset, the Cloud Tasks default deadline would cut a long fire and retry the whole turn.
- **Text a reminder holds is untrusted-formatted.** Notices carry the model's words literally
  (`notify_raw(..., literal=True)`): the platform shows them and obeys nothing in them.

## Links

- [Proactive Self-Reminders RFC](../../10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md): the original design (the schedule model
  there is superseded).
- [Notification Delivery Refactor RFC](../../10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md): the claim / enqueue / deliver
  split (§ 7, Step D).
- [Reminders: RRULE schedules](../../04_solution_strategy/decisions/reminder_rrule_schedules.md): the current schedule
  model, the 5-minute floor and the no-replay rule.
- [Per-tier SLA](../../04_solution_strategy/decisions/notification_sla_per_tier.md): why the budget scales with
  complexity.
- [Task dedup](../../04_solution_strategy/decisions/worker_task_dedup.md): Cloud Task names as a dedup, and their limits.
- [Agent Workspace RFC](../../10_rfcs/AGENT_WORKSPACE_RFC.md) § 8.3: the `workspace_file` binding.

## Where to start reading

- `src/services/reminders_service.py::RemindersService.fire_due_reminders`: the cron tick and the claim.
- `src/services/execute_reminder_task.py::ExecuteReminderTask`: one fire, its outcomes and retry rules.
- `src/agents/notes_agent.py::NotesAgent._execute_tool`: create, update and delete, with the snap.
- `src/adapters/dateutil_recurrence_adapter.py::DateutilRecurrenceAdapter`: validation and wall-clock expansion.
- `src/adapters/firestore_agent_note_adapter.py::FirestoreAgentNoteAdapter`: the transactional claims and legacy reads.
- `src/domain/reminder_alert.py::build_reminder_alert`: what the fired turn is told.
