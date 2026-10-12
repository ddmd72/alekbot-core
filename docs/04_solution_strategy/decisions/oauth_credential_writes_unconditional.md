# Decision: Credential writes stay unconditional (refresh, disconnect, reconnect flag)

**Date:** 2026-10-12
**Status:** Accepted, deferred (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §7.7; `src/adapters/microsoft/graph_auth.py`,
`OneDriveAdapter.flag_reconnect`, `WorkerHandler._flag_gmail_reconnect`.

**Decision:** Every write of an `OAuthCredentials` document stays a plain read-then-save, for the drive, To Do and
Gmail alike. Three writers race without a precondition: a token refresh (read, POST to the token endpoint, save the
new tokens), a disconnect (delete), and the reconnect flag (read, save with `needs_reconnect=True`). A refresh that
finishes after a disconnect recreates the deleted document; a flag written from a copy read before a reconnect puts
the old tokens back and marks the fresh grant broken. Each needs a user action inside a window of one token request
(at most 30 s since the token POST got its own timeout, RFC Δ42), and no such incident is on record.

**Rejected alternatives:**
- *A conditional save on the port (`save_if_unchanged`, Firestore `update_time` precondition):* the right shape, but
  a port change across three providers and every caller's retry path, for a race never observed.
- *Field-level updates for the flag only:* closes the flag race, leaves the refresh/disconnect one open.
- *A per-user lock around credential writes:* the writers run in two Cloud Run services; only Firestore can arbitrate.

**Trigger to revisit:** a disconnected drive or mailbox that comes back connected, a fresh grant shown as "Reconnect",
or a second credential writer (a background job that refreshes on its own schedule).
