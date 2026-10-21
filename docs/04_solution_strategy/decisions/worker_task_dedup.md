# A redelivered `/worker` Cloud Task is skipped, claimed by task name

**Date:** 2026-10-09
**Status:** Live
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
     (`{prefix}worker_task_dedup`, `DedupStore.try_mark_processed`, TTL 1800 s, which is the longest
     `dispatch_deadline` Cloud Tasks allows) before dispatching.
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

## Alternatives rejected

- **Dedup at enqueue (named tasks).** Prevents double *enqueue*, not double *delivery*. The job
  enqueued once.
- **Return 429 to an in-flight duplicate so Cloud Tasks re-checks later.** With this queue's retry
  config (minBackoff 0.1 s, 5 attempts) every retry is used up within seconds. In practice that is
  the same as skipping, with more noise.
- **Dedup inside each delivering agent.** Covers one agent; the at-least-once contract applies to
  all of them.

## Cost of the choice

If an instance dies mid-task (OOM) without raising, the claim stays. A redelivery within 30 minutes
is then skipped and the task is lost. Before this change it would have re-run. This is rare, and an
OOM would likely repeat on a re-run anyway.

A task still running past its dispatch deadline (600 s default for `email_indexing`,
`companion_consolidation` and agent tasks without a manifest deadline; the Cloud Run timeout is 1800 s)
gets Cloud Tasks' retry at the deadline acked as a duplicate. If the original then fails, nothing
retries it (the consolidation sweeps cover consolidation).

## Deployment

One-time TTL policy on the claim collection, as for the other `expires_at` collections
(`docs/07_deployment/README.md`).
