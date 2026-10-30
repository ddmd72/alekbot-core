# R1: Reconnect and one notice when a past connection has no credential

**Date:** 2026-10-29 (Drive amended 2026-10-30, multi-drive slice 1b)
**Status:** Built (secrets at rest, slice S2); Drive reads drive connections from multi-drive slice 1b
**Scope:** `domain/reconnect_policy.py`, `IntegrationReconnectService`, `WorkerHandler` (`reconnect_notice_sweep`),
`DriveSyncSchedulerService`, the Gmail / To Do feature-state stores, `DriveConnectionService.reconnect_needed`, Cabinet
status for the three integrations.
**Context:** `docs/10_rfcs/SECRETS_AT_REST_RFC.md` §6.2; [gmail_grant_revoked_reconnect.md](gmail_grant_revoked_reconnect.md);
`docs/10_rfcs/MULTI_DRIVE_RFC.md` §4.4 (Drive).

## Problem

Credentials move to one credential store. A user whose old credential is not carried over (or is lost) would see
"Not connected" and get no message, although their feature state still shows they were connected. The existing notices
fire only on a rejected refresh, which needs a credential to reject.

## Decision

1. **R1.** A feature state that shows a past connection (Gmail: an indexing state; To Do: a config with a primary list)
   and no credential means Reconnect in the Cabinet and one chat notice per user and integration. **Drive** (from
   multi-drive slice 1b) reads its drive connections instead of a sync state: a `NEEDS_RECONNECT` connection, or an
   `ACTIVE` one whose credential is gone, is owed Reconnect and the notice; a `DISCONNECTED` one never is
   (`DriveConnectionService.reconnect_needed`).
   The rule is pure (`reconnect_needed`, `r1_due`). It keys on the shared `Integration` enum (`domain/integration.py`: `MAIL` is Gmail,
   `TASKS` is To Do, `DRIVE` is the drive), the same enum the Cabinet groups credentials by; there is no second enum. The ops alert
   path of an empty notice reads `mail_reconnect_notice`, `tasks_reconnect_notice`, `drive_reconnect_notice`. Existence of a
   credential is read with the cipher-free `OAuthCredentialsPort.list_keys`, so a decrypt failure never reads as "no credential".
2. **F1, deliberate disconnect.** A Cabinet disconnect of Gmail marks the feature state (`disconnected_by_user_at`),
   so R1 skips it; a new consent clears the mark. To Do needs no mark: its disconnect resets the config, so no primary
   list remains (D-P5). Drive needs none either: its disconnect ends the connection (`DISCONNECTED`, terminal) before
   the grant is removed, so a deliberate disconnect never reads as a loss; a later consent of the same account is a new
   connection. The sync state's `disconnected_by_user_at` is no longer written by the Cabinet.
3. **F2, the once-per-transition flag.** The flag lives on the feature state and is written as a single field: Gmail
   `set_reconnect_flags`, To Do `set_reconnect_notified`, Drive `update_sync_flags`. A whole-record rewrite could undo a
   concurrent save. The flag goes up before the send and comes down only when nothing can have been sent; a credential
   that is back lowers it.
4. **D-P4, where users come from.** The hourly task `reconnect_notice_sweep` (Gmail and To Do) lists users from the
   feature stores. The existing fan-outs (daily review, To Do renewal) list from the credential store, which is empty
   for exactly the users R1 is for. Drive's notice stays in the 10-minute drive fan-out, which already visits every
   sync state.

## Alternatives considered

- **Drive keeps a sync-state mark after multi-drive.** A sync state is per connection and is purged with it; the
  connection's own state already says "ended by the user", so a second flag could only disagree with it.

- **Read the old credential collection to find users.** Keeps the old store alive and readable, the thing the RFC
  removes.
- **A one-shot notice at cutover.** Misses every later loss, and a missed send could not be retried.
- **Flags on the credential record.** The record is the thing that is missing in this case.

## Triggers

- The hourly scheduler job (Gmail, To Do) and the drive fan-out (Drive).
- The Cabinet status calls, which show Reconnect without sending anything.
- The one connect callback (`/auth/connect/callback`; the activation per integration), which clears Gmail's
  deliberate-disconnect mark and To Do's flag; the drive activation returns its connection to `ACTIVE`.

## Consequences

- Silence for a deliberate disconnect depends on the mark: a disconnect that predates it reads as a lost grant once.
  For Drive it depends on the order in the Cabinet disconnect (the connection ends, then the grant goes).
- One more hourly job; a failure for one user is logged and counted, never stops the rest.
