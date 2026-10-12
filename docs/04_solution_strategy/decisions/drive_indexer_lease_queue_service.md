# Decision: The drive tick runs under a lease, on its own queue, on a separate indexer service

**Date:** 2026-10-11
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §7.1, §9, §12.5; owner decision D5 (§21).

**Decision:**
- **One writer per user is a lease.** A tick takes `DriveSyncState.lease_until` in a Firestore transaction, set to
  start + `TICK_DEADLINE` + a margin, so a holder never outlives it. A tick that finds a live lease answers `busy`;
  a nudge that finds one re-enqueues itself under a new key.
- **Own queue** `drive-sync-{suffix}`, `max-concurrent-dispatches=1`, deadline 1800 s, dedup keys per family
  (scheduler bucket, continuation, nudge, toggle). Concurrency 1 caps memory; correctness comes from the lease.
- **Own service** `alek-bot-indexer-{suffix}` (`indexer_main.py`, same image, gen2, 2 GiB, concurrency 1,
  min 0, max 1, `--ingress=internal` plus the in-app OIDC check, plan Δ21) runs the whole tick. The main
  service fans out and nudges; it never indexes.

**How the code applies it:**
- Lease: `acquire_lease` / `release_lease` in `src/adapters/firestore_drive_index_repo.py`, both transactional;
  the span is `TICK_DEADLINE_S` (1800) + `LEASE_MARGIN` (1 min) from `src/domain/drive_index_params.py`. Release
  happens in the tick's `finally` and clears the lease only when this tick still owns it.
- `save_sync_state` never writes `lease_owner`/`lease_until` (like the fan-out flags); only the lease transactions
  do. Changed after review (fix R4): in the plan a tick that outlived its lease saved its whole state, lease fields
  included, over the next holder's lease.
- Busy outcomes: a nudge comes back after `NUDGE_RETRY_DELAY_S` (60 s) under `{user_id}:nudge:{mutation_id}:r{n}`;
  a toggle that finds a live lease is dropped (plan Δ19c): the running tick re-reads the flag before its write
  batches (an opt-out enqueues a continuation whose gate purges), and the 10-minute fan-out is the backstop.
- Queue: provisioned by `cloudbuild-dev.yaml`, never by the app (`GcpTaskQueue.create_queue_if_not_exists` would use
  concurrency 5): one dispatch at a time, one per second, three attempts with 60–600 s backoff. The 1800 s deadline
  is per task: every `drive_sync` enqueue passes `deadline_seconds=TICK_DEADLINE_S`. A manual
  `gcloud tasks create-http-task` without it gets Cloud Tasks' 600 s default.
- Callers: the main service's `start_drive_sync` fan-out (`DriveSyncSchedulerService`) and the nudges from
  `UserDriveService` mutations; the indexer enqueues its own continuations and nudge re-queues
  (`DRIVE_INDEXER_URL`, read with `os.getenv`; unset, there is no drive queue: no fan-out enqueues, no nudges).
- Ingress and identity (`src/web/indexer_worker_app.py`): only Cloud Tasks on the same project reaches the service.
  The `/worker` OIDC check verifies signature, the service-account email and an audience pinned to the indexer's own
  URL (`{url}` or `{url}/worker`), and fails closed on Cloud Run without `SERVICE_ACCOUNT_EMAIL`. A manual task whose
  run.app URL form differs from `INDEXER_URL_DEV` therefore gets 401.
- The indexer has no Logfire telemetry: its ticks are visible in Cloud Logging only.

**Rejected alternatives:**
- *Tick on the main service:* it peaks at 74–90 % of 1 GiB, parsers amplify memory (Phase 0: a 5 KB XLSX at
  ~1.75 GB), and an indexing crash would take chat down with it.
- *Queue concurrency 1 as the only guard:* Cloud Tasks delivers duplicates and redelivers after a deadline overrun
  ([`worker_task_dedup.md`](worker_task_dedup.md)); only a lease excludes a second writer.
- *The shared worker queue:* concurrency 5 lets two ticks of one user race and puts drive work next to chat-side tasks.
- *A Cloud Run Job per tick:* a cold container per user every 10 minutes for work that fits a 30-minute task
  ([`cloud_tasks_vs_jobs.md`](cloud_tasks_vs_jobs.md)).

**Trigger to revisit:** ticks wait past `STALE_ALERT_AFTER` as users grow (raise `maxConcurrentDispatches`).

**See also:** [worker_oidc_and_docx_sandbox.md](worker_oidc_and_docx_sandbox.md) (the main service's `/worker` OIDC
check, whose audience stays unpinned), [drive_index_opt_in_access_loss_grace.md](drive_index_opt_in_access_loss_grace.md)
(what the tick does under the lease), `docs/07_deployment/SCHEDULERS.md` (the `start_drive_sync` job).
