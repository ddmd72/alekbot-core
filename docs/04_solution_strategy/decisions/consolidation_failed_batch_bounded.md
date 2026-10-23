# Decision: a FAILED consolidation batch is revived only by the hourly sweep, at most 3 times

**Status:** Adopted
**Date:** 2026-10-22

## Problem

`process_user_batches` reset FAILED batches to `retry_pending` (attempts 0) at the start of every task. After 3
failed attempts a batch went FAILED, and when a newer batch was pending the worker re-enqueued at once, so the next
task revived the same oldest batch: back-to-back paid retries without end, newer batches starved. With nothing else
pending the hourly sweep did the same, 3 paid attempts every hour forever.

## Decision

- `ConsolidationQueue.reset_recoverable_batches(user_id, revive_failed=False)`: the default (every task) revives
  zombie PROCESSING batches only. The sweep revives FAILED ones through `ConsolidationService.revive_failed_batches`,
  called per stuck user in `_handle_sweep_consolidation` before it enqueues the task.
- Lifetime counter `revivals` on the batch document, `+1` per FAILED revival, never reset. A batch is revived while
  `revivals < MAX_FAILED_REVIVALS` (3): at most 4 rounds x 3 attempts = 12 paid attempts in its life.
- Past the cap the batch stays FAILED with its messages in the queue document. The service logs one ERROR when the
  last allowed round fails: grep `[Librarian] CONSOLIDATION_GAVE_UP` (batch_id, user_id[:8], revivals).
- `get_pending_batches` selects `pending`/`retry_pending` only, so a FAILED batch never blocks a newer one.

- **Lost runs count (2026-10-23).** A run that kills its worker never records a failure, so its batch used to loop
  (reset to attempts 0 as a zombie, run, die). `reset_recoverable_batches` now sets a zombie's `attempts` to +1 and, at
  `MAX_BATCH_ATTEMPTS` (3), marks it FAILED (ERROR `CONSOLIDATION_FAILED`, `CONSOLIDATION_GAVE_UP` past the revival cap).
  Both callers count: the lease holder's 60 s threshold, where S2 would otherwise repeat the loop every ~16-18 min, and
  the sweep's 1800 s one, where the same loop ran hourly. A CPU-throttled zombie that was no poison batch spends an attempt
  too; the sweep revives it, and the total stays at 12 lost runs in the batch's life.

## Rejected

- A separate new port method for the sweep path: `test_port_contracts` pins the abstract set, and one flag is enough.
- A `given_up` marker written by the sweep: the terminal failure is already seen by the service, so the log line
  needs no extra write and fires exactly once.
- Cooldown timestamps per batch (backoff between revivals): the hourly sweep already spaces rounds; more state, no gain.
- Deleting a given-up batch: its messages are the only copy of that window of conversation; the owner decides.

## Revisit when

A transient outage longer than ~3 hours (3 sweeps) is common enough that batches are given up while the cause is fixed:
raise the cap or add a manual "revive" lever.
The companion queue has the same pre-fix revive-on-every-task code and is not changed here.

## Update 2026-10-23 (Facts S2)

A zombie reset (a `processing` batch whose run was lost) now counts as a run: `attempts` + 1, `failed` at
`MAX_BATCH_ATTEMPTS` (3), on the lease path (60 s threshold) and on the sweep path (1800 s). Before, it set `attempts` to 0, so
a batch that kills its worker looped for ever. The bound is unchanged in size: 4 rounds x 3 runs = 12 runs in a batch's life,
where a run is an agent failure or a lost run. A FAILED revival still resets `attempts` to 0. See `fact_versions_one_writer.md`.
