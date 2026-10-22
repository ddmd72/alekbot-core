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

## Rejected

- A separate new port method for the sweep path: `test_port_contracts` pins the abstract set, and one flag is enough.
- A `given_up` marker written by the sweep: the terminal failure is already seen by the service, so the log line
  needs no extra write and fires exactly once.
- Cooldown timestamps per batch (backoff between revivals): the hourly sweep already spaces rounds; more state, no gain.
- Deleting a given-up batch: its messages are the only copy of that window of conversation; the owner decides.

## Revisit when

A transient outage longer than ~3 hours (3 sweeps) is common enough that batches are given up while the cause is fixed:
raise the cap or add a manual "revive" lever. Zombie revivals are still uncounted (a batch that kills its worker loops).
The companion queue has the same pre-fix revive-on-every-task code and is not changed here.
