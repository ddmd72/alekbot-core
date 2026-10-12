# Decision: The Gmail reconnect notice is sent at most once; a retry is a separate task

**Date:** 2026-10-12
**Status:** Accepted, deferred (found while building User Drive Step 2, Slice 1)
**Context:** [gmail_grant_revoked_reconnect.md](gmail_grant_revoked_reconnect.md); `WorkerHandler._flag_gmail_reconnect`;
`NotifyResult.sent`; the drive equivalent, RFC Δ6 in [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md).

**Decision:** On a revoked Gmail grant the worker sets `needs_reconnect` first, then sends one notice, and the flag
stays up whatever the notice does. A notice that never left (no channel, a formatter failure, `notify` raising) is
therefore lost for that breakage; the Cabinet still shows **Reconnect Gmail**. The drive fan-out retries such a notice
because it tracks "user told" in its own flag (`reconnect_notified` on the sync state). Gmail has only one flag,
`needs_reconnect`, and it means "the grant is revoked", not "the user was told".

**Rejected alternatives:**
- *Roll `needs_reconnect` back when nothing was sent* (built and reverted on 2026-10-12): the Cabinet then shows
  "Connected" for a revoked grant until the next failing run raises it again — the silent "Connected" that
  `gmail_grant_revoked_reconnect.md` fixed.
- *Retry the notice on every failing run:* one message a day until the user reconnects.

**The retry, when built:** a separate "notified" field next to `needs_reconnect` on the Gmail credentials (the drive
pattern), raised before the send and lowered only when `NotifyResult.sent` is false.

**Trigger to revisit:** a user who never learned that Gmail needs a reconnect, or "not sent" outcomes for the Gmail
notice in the logs.
