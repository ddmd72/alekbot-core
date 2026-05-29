# Decision: EmailEmbeddingRepairService wired into Cloud Scheduler

**Status:** Adopted — R13.1 closed (option a: wire it up)
**Date:** 2026-05-29
**Context:** Inspection finding R13.1 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

`EmailEmbeddingRepairService` is wired into the production worker dispatch via a
new `repair_email_embeddings` task type. A Cloud Scheduler entry runs every 6
hours in both dev and prod, hitting `POST /worker`. The service was previously
implemented in full (`src/services/email_embedding_repair_service.py`) but
never instantiated anywhere — no scheduler entry, no composition wiring, no
worker handler. The log message at `email_indexing_service.py:725` that
promised repair was a false promise until this commit.

## Why wire it up (not delete)

Two real production failure modes are now covered:

1. **Transient Gemini API failures.** `GeminiEmbeddingAdapter` retries 3× with
   exponential backoff. Failures past that point set `embedding_pending=True`
   on the `IndexedEmail` doc. Without a repair pass, the doc has no vector →
   invisible to `find_nearest` forever.
2. **Quota exhaustion on indexing bursts.** The semaphore caps in-flight calls
   at `GEMINI_EMBED_CONCURRENCY=20`; tier-2 RPM bursts past that can still hit
   429s that survive the retry loop.

Both modes are unrecoverable without an out-of-band repair pass. The class was
already structurally correct (port-injected `EmailRepository` + `EmbeddingService`,
no LLM calls, idempotent on `embedding_pending` flag), so wiring is mechanical:
constructor injection in `main.py`, dispatcher case in `WorkerHandler`, scheduler
entry in cloudbuild. No domain changes, no port changes.

## Schedule + drain pattern

Hourly tick (`0 * * * *`), aligned with `start_email_indexing` cadence —
failures from any given indexing tick are detected within ≤1h. The handler
implements the same drain-on-demand pattern as `_handle_consolidation` and
`_handle_email_indexing`: `EmailEmbeddingRepairService.run()` returns
`(repaired_count, has_more)`; when `has_more=True` (batch saturated at
`batch_size=100`), the handler immediately re-enqueues another
`repair_email_embeddings` Cloud Task via `enqueue_worker_task`. The queue
drains in back-to-back Cloud Tasks without waiting for the next scheduler
interval. Batch cap of 100/run holds per-task API budget bounded.

## What changed

- `src/handlers/worker_handler.py` — new optional `email_embedding_repair`
  constructor param + `_handle_repair_email_embeddings()` method (with
  re-enqueue on saturated batch) + dispatcher case for `repair_email_embeddings`.
- `src/services/email_embedding_repair_service.py` — `run()` returns
  `Tuple[int, bool]` (`repaired_count`, `has_more`).
- `main.py` — constructs `EmailEmbeddingRepairService(email_repo, embedding)`
  conditional on both ports being present; injects into `WorkerHandler`.
- `cloudbuild-{dev,prod}.yaml` — Cloud Scheduler entries with 300s
  attempt-deadline (room for 100 emails × ~3s embedding each, with margin).
- `docs/07_deployment/SCHEDULERS.md` — full reference entry.
- `CLAUDE.md` — `WorkerHandler` task-type list.

## Rejected alternatives

- **Delete the service** (audit option b). Loses the safety net for transient
  failures; emails silently lost from search. Functional regression at
  zero cost saving (the code already exists).
- **Hybrid: delete service, keep `embedding_pending` flag** (audit option c).
  Flag becomes pure observability (count-only) with no remediation path. Same
  functional regression as full delete; trades silent data loss for
  visible-but-unaddressed metric.
- **Move repair into `EmailIndexingService.run_indexing_job` as in-page retry.**
  Couples retry policy with foreground indexing latency; failed emails block
  the whole batch. Out-of-band repair is the correct shape.
- **One pass per scheduler tick, no re-enqueue.** Drain time would scale
  linearly with the scheduler interval — 600 pending at hourly cadence would
  take 6h to drain instead of seconds. Inconsistent with consolidation /
  email-indexing handler patterns.
