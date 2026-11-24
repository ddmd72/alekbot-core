# External tasks: every chat task waits for confirmation, no taint tracking

**Date:** 2026-11-24
**Status:** Accepted. Implemented in steps (see "Rollout"). RFC: `docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md` (§5.3, §5.9
and §6.1 are superseded by this record). Owner's map: `docs/05_building_blocks/external_task_queue/README.md`.

## Decision

The external task queue is a generic outbound channel: a task carries text the bot wrote to a worker outside the
system. This record fixes how a task gets out, and how its outcome comes back.

1. **No taint tracking.** The queue no longer asks whether the turn that creates a task read outside content. The
   per-execution tracker, the saved-message bit, the workspace taint mark and the flag on reminders are removed in
   later steps. The wrapping of content that already enters a model (emails, drive documents, a worker's result when
   a tool returns it) is unchanged; it is a different mechanism.
2. **Every task created in a chat turn waits for the owner's confirmation.** The task is stored as `held` with a
   short code (kept only as a keyed digest, returned once). After the answer the owner is shown the title and the full
   text, word for word, and a separate message with the command `$task send CODE`. The task goes to the worker only if
   the owner sends that command within the hold time, otherwise it lapses silently. The shape is the one of
   `$memory save` (`memory_facts_owner_confirmed.md`): the model's output is stored server-side, the owner sees the
   literal artifact, the code never reaches a model, and the command is handled before any agent. On Slack the
   command message also carries a «Send» button, the button form of the same command: its press is checked to be the
   task owner's and ends in the same confirmation with the same answers; Telegram keeps the command alone, and the
   command keeps working on Slack (owner's map, "The Send button").
3. **A task created while one of the owner's reminders runs goes straight to the queue.** The owner approved the
   reminder when it was set, so the control point is the reminder-creation notice, not a second confirmation. The
   mode comes from code-set context (the run's origin), never from model output.
4. **Completion arrives to the bot as a system alert.** When a task ends, the delivery path starts a background turn
   with a code-made alert (ids and labels only, never the title, the result or a failure reason), the bot reads the
   worker's result from the workspace file, wrapped as outside content, and tells the owner. That alert has its own
   notification kind and run origin, and the queue is not offered in it, so a result cannot start new work.
5. **The specialist move.** Task creation moves from Smart's local tools to a specialist agent; the entry mode is
   passed to it from the context the code sets.

The entry is a code-side enum, `TaskEntry` (`CHAT`, `REMINDER`), passed to `ExternalTaskService.create`. It is not
stored on the task.

## Why

The earlier design held a task only when the turn had read outside content, and queued it at once otherwise. That
needed a tracker through the delegation engine, a persisted bit on saved messages, a mark on workspace files and a flag
on reminders, all to answer one question per create. The realistic threat is a task the owner never asked for, written
by the bot on its own initiative; always asking for confirmation stops exactly that, with one rule and no state. A
poisoned brief inside a task the owner did ask for is stopped by neither design: the owner is shown the text, which is
the only check that works there.

## Alternatives rejected

- **Hold only when the turn is tainted (the previous design).** Friction without protection: it confirms the tasks that
  needed no confirmation in the cases the tracker happened to flag, and misses what it cannot see. It is also the
  largest source of cross-cutting code in the repo for one decision.
- **Confirm nothing and rely on the receipt.** The receipt shows a task after it left; a bot-initiated task would
  already be with the worker.
- **Confirm reminder-created tasks too.** A fired reminder has no owner present to answer, so the task would lapse.
  The owner's control there is the reminder's own notice at creation.
- **Deliver completion as a literal post.** The bot then never learns the outcome and cannot act on it in
  conversation; the alert gives it the outcome without giving the worker any instruction channel.

## Residual risk

A reminder-fired turn that read outside content can queue a task with no confirmation. It is accepted: the reminder
was approved by the owner, and the task's text is bounded and capped.

A privacy gate on what a task text may contain is future work; it is not part of this decision.

## Rollout

1. Every chat create waits for confirmation (`TaskEntry`, `create(entry)`, `created_tainted` no longer written; old
   documents still load, the stored field is ignored on read and dropped at the next write).
2. The taint mechanisms are removed, one surface per change.
3. Delivery as a system alert.
4. The specialist move.
5. Reminder-created tasks.

## Revise when

- The owner finds the confirmation step too heavy for tasks they asked for explicitly.
- A bot-initiated task reaches a worker despite the confirmation (the check is in code, so this means a path was missed).
