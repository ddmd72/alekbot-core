# A redelivered `/worker` Cloud Task is skipped, claimed by task name

**Date:** 2026-10-09
**Status:** Live; widened in S1b (2026-10-22), pending deploy
**Scope:** `/worker` (`main.py`) and `WorkerHandler`: every Cloud Tasks delivery (all `task_type`s,
`telegram_update`, queued Slack events). Introduced for `agent_execution` (deep research, documents,
images, `tell_alek` errands); widened to every delivery in S1b.

## Problem

On 2026-10-09 one deep research produced **two** HTML reports.
- The research job enqueued **one** `create_html_page` task (`tasks/0038899772573220363`,
  `dispatch_deadline` 720 s).
- Cloud Tasks delivered it twice to the same instance, at 18:07:24 and 18:10:53, while the
  first attempt was still running.
- Both attempts returned 200 (262 s and 247 s), and each generated and posted a report.

## What is known, and what is not

Ruled out:
- the dispatch deadline (720 s);
- the service timeout (1800 s);
- a proxy in front of the service (`dev.alekbot.app` resolves to Google Frontend through a domain
  mapping);
- non-2xx responses in the window;
- a systematic cause: many `/worker` requests of 200–1385 s in the past week had no repeat, and
  this is the only duplicate in two weeks.

Why Cloud Tasks re-dispatched is **not** known. Queue logging was off, and `/worker` did not log the
`X-CloudTasks-*` headers. Cloud Tasks delivers at least once, and the documentation states that a
task may be executed more than once. A handler with side effects therefore has to be idempotent,
whatever the trigger.

## Decision

1. **Observe.**
   - `/worker` logs `X-CloudTasks-TaskName`, `X-CloudTasks-TaskRetryCount` and
     `X-CloudTasks-TaskExecutionCount` for every Cloud Tasks request (`CloudTaskDelivery`, `domain/`).
   - Queue `agent-tasks-dev` logs every attempt (`--log-sampling-ratio=1.0`).
   - The next duplicate will therefore show whether it was a retry or a redelivery.
2. **Claim by task name, in `WorkerHandler.handle`, for every Cloud Tasks delivery.**
   - `WorkerHandler` atomically creates a claim document named after the task
     (`{prefix}worker_task_dedup`, `TaskClaimStore`, fields `holder` = process id (written for diagnosis, never read), `attempt` = uuid per
     delivery, `state` = `running`|`done`, `expires_at` = now + 3600 s, garbage collection only) before dispatching.
   - A delivery that finds a claim returns 200 `{"status": "duplicate"}` and runs nothing.
   - **The claim is released when the attempt raises, returns >= 500, or returns 429** (queued Slack
     event, session busy). Cloud Tasks retries those, and the retry must run. Other results keep the
     claim: a `FAILED` agent response returns 200, the user was told, and nothing retries.
   - **Fails open:** if the claim store errors, the task runs. A duplicate is better than a lost task.
   - Requests without Cloud Tasks headers (Cloud Scheduler, manual curl) are never claimed.
   - `WorkerHandler` is the only dispatcher: `telegram_update` and the queued Slack event (no
     `task_type`) go to the adapters attached at wiring time; any other `task_type` is a 400
     (it used to fall through silently to the Slack handler).
   - `execute_reminder` needs nothing more: the fire-time claim is atomic and there is one task per
     fire, so only a redelivery overlapped, and the task claim now covers it.

## Same collection, other marks (S1b)

`{prefix}worker_task_dedup` also holds marks written through `FirestoreDedupStore`; liveness follows each
doc's own `expires_at`, so TTLs differ per writer:
- 48 h daily-review marks `daily_review:{user}:{slot_date}` (one review per local date, `start_daily_email_review`);
- 72 h OpenAI deep-research webhook claims `openai_deep_research:<job_id>` (OpenAI's retry window).
`try_mark_processed` treats only `AlreadyExists` as "already marked"; an expired-but-unpurged mark is
re-claimed atomically (before S1b it returned True without rewriting the mark).

## Chat intake (S1b)

- Slack and Telegram Cloud Tasks have deterministic names (event id / update id); `ALREADY_EXISTS` is success
  (already queued). The queued path no longer uses an intake mark; the inline Telegram path (no queue) still does.
- A busy session waits in-process up to 100 s (FIFO), then re-queues under a named task (12 x 15 s at most), then
  posts a "resend" notice. The task is no longer answered 429 for a busy session. Non-user Slack events (bot, edits,
  other types) skip this.

## Alternatives rejected

- **Dedup at enqueue (named tasks).** Prevents double *enqueue*, not double *delivery*. The job
  enqueued once.
- **Return 429 to an in-flight duplicate so Cloud Tasks re-checks later.** With this queue's retry
  config (minBackoff 0.1 s, 5 attempts) every retry is used up within seconds. In practice that is
  the same as skipping, with more noise.
- **Dedup inside each delivering agent.** Covers one agent; the at-least-once contract applies to
  all of them.

## Cost of the choice

**A claim knows its holder; a dead attempt's claim is taken over.** The main service runs at most one
instance (`--max-instances=1`), and `LongTurnService.check_retry` already reads "running but not live in
this process" as a dead attempt. `try_claim` therefore decides:
- same `attempt` (the SDK retried a create that landed) -> ours; `state=done` -> duplicate;
- `running` while the key is in this process's in-memory running set -> a true concurrent duplicate (the
  `holder` field is not compared); any other `running` claim (another process, or this one but not running
  here) -> dead attempt (one-instance assumption), taken over with a `last_update_time` precondition (one winner); an expired claim likewise.
- A 2xx/4xx result marks the claim `done` (a redelivery within 1 h is a duplicate); release (raise, >= 500, 429)
  and `done` act only when the doc still carries our `attempt`.
- An OOM mid-task no longer loses the task: the retry takes over and runs, and chat turns reach
  `ConversationHandler`'s long-turn retry check. Assumption: one instance. A redelivery during a rolling
  deploy overlap can run a task twice (accepted, as for `LongTurnService`).
- An attempt cancelled by the dispatch deadline (the connection closes, Quart cancels) releases its
  claim and is retried, which is Cloud Tasks' intent.

A task still running past its dispatch deadline (`companion_consolidation` sets 600 s explicitly; `email_indexing` and agent tasks without a
manifest deadline get the 600 s default; the Cloud Run timeout is 1800 s)
is cancelled at the deadline (see above); its retry runs.

## Deployment

One-time TTL policy on the collection, as for the other `expires_at` collections
(`docs/07_deployment/README.md`). The claim's 1 h expiry is garbage collection and the done-window, not the liveness rule.
