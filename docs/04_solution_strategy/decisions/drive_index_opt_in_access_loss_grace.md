# Decision: Drive indexing is opt-in; losing access keeps the index for a 30-day grace period

**Date:** 2026-10-11
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §6, §7.6, §7.7; owner decision D3 (§21).

**Decision:**
- Nothing is copied until the user turns indexing on in the Cabinet (`UserBotConfig.drive_indexing_enabled`).
  Turning it off purges the index inside a tick, under the lease. Search reads the flag uncached, so the index is
  invisible at once.
- A disconnect or a rejected refresh (`needs_reconnect`) deletes nothing. The tick's gate stamps
  `DriveSyncState.access_lost_at` once and stops. The fan-out keeps visiting the user, and once `ACCESS_LOSS_TTL`
  (30 days) has passed, the tick purges in bounded batches. Regaining the same drive before then clears the stamp
  and continues from the stored cursor, with no new epoch. A different `drive_id` purges at once.

**How the code applies it** (gate: `access_action` in `src/domain/drive_index_planner.py`; tick:
`DriveIndexingService.tick` in `src/services/drive_indexing_service.py`):
- The gate is keyed on facts only (flag, credentials, `needs_reconnect`, stored vs. connected `drive_id`,
  `access_lost_at`), and is evaluated twice: before the lease and again under it, since the facts may have moved.
- A purge first writes `purging=True`, so a started purge always finishes (`PURGE_THEN_RESTART`), then deletes in
  batches of `RECORD_WRITE_BATCH` within the tick's time budget and enqueues its own continuation. With indexing still
  on (drive switch, restart), the last batch enqueues onboarding of the connected drive at once.
- Both directions of the Cabinet toggle enqueue a `drive_sync` task under the `toggle` key family (plan Δ2), so
  onboarding starts at once instead of after up to one tick interval.
- An opt-out seen mid-tick (the gate is re-read before write batches, at most every 5 s, plan Δ19b) drops the tick's
  result and enqueues a continuation; that continuation's gate purges. Changed after review (premise audit P2): the
  plan's tick only dropped the result, leaving the purge to a fan-out that skipped the user for up to 30 min.
- A drive switch is caught twice. The gate compares `drive_id`s, and the delta cursor carries the drive id and App
  Folder id it came from: a mismatch with the fresh App Folder read raises a resync before the old URL is followed,
  and the first page of the restarted enumeration, from the other drive, purges. Changed after review (fix R3): the
  plan's cursor was unbound, so an account switch whose connect-time drive id read failed looped on `error`.
- A rejected refresh during a tick sets `needs_reconnect` and saves `last_error=RECONNECT_NEEDED` onto the latest
  saved state; page-by-page progress (cursor, epoch) is kept. Changed after review (fix R2): the plan's handlers
  wrote onto the tick-start copy and lost that progress.
- The one reconnect notice (plan Δ6) is posted by the fan-out on the main service, not by the indexer: it loads the
  user's agents first, sets `reconnect_notified` before sending and rolls it back only when nothing could have been
  sent. A cut-off notice is never repeated but may be lost, at most once, as on the Gmail path. Changed after review
  (fixes R7, R8): the plan's order could repeat a notice, and lost it whenever the user's agents were not loaded.
- `is_stale` is false while `access_lost_at` is set: a lost grant is the user's to fix, not an ops alert.

**Rejected alternatives:**
- *Index every connected drive:* copies names and vectors of the user's files without a separate, informed choice.
- *Purge on any access loss:* tokens drop often and a disconnect can be accidental, while a full re-index is slow
  (the Gmail indexing experience).
- *A Firestore TTL stamped on every record at loss (rev 3.3 to 3.9):* per-document writes and a resume rule for one
  extra case, a scheduler dead for 30 days. Rev 3.10 replaced it with the tick-side purge; users see no difference.
- *Purge from an exception handler:* purge is keyed on facts (flag, credentials, drive id), never on an exception.

**Trigger to revisit:** a user loses an index they expected to keep, or keeps one they expected gone.

**See also:** [gmail_grant_revoked_reconnect.md](gmail_grant_revoked_reconnect.md) (the reconnect-notice precedent),
[drive_indexer_lease_queue_service.md](drive_indexer_lease_queue_service.md) (the lease every purge runs under).
