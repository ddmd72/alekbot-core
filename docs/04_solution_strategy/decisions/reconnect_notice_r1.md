# R1: Reconnect and one notice when a past connection has no credential

**Date:** 2026-10-29
**Status:** Built (secrets at rest, slice S2); not deployed
**Scope:** `domain/reconnect_policy.py`, `IntegrationReconnectService`, `WorkerHandler` (`reconnect_notice_sweep`),
`DriveSyncSchedulerService`, the Gmail / To Do / Drive feature-state stores, Cabinet status for the three integrations.
**Context:** `docs/10_rfcs/SECRETS_AT_REST_RFC.md` §6.2; [gmail_grant_revoked_reconnect.md](gmail_grant_revoked_reconnect.md).

## Problem

Credentials move to one credential store. A user whose old credential is not carried over (or is lost) would see
"Not connected" and get no message, although their feature state still shows they were connected. The existing notices
fire only on a rejected refresh, which needs a credential to reject.

## Decision

1. **R1.** A feature state that shows a past connection (Gmail: an indexing state; Drive: a sync state; To Do: a config
   with a primary list) and no credential means Reconnect in the Cabinet and one chat notice per user and integration.
   The rule is pure (`reconnect_needed`, `r1_due`). It keys on the shared `Integration` enum (`domain/integration.py`: `MAIL` is Gmail,
   `TASKS` is To Do, `DRIVE` is the drive), the same enum the Cabinet groups credentials by; there is no second enum. The ops alert
   path of an empty notice reads `mail_reconnect_notice`, `tasks_reconnect_notice`, `drive_reconnect_notice`. Existence of a
   credential is read with the cipher-free `OAuthCredentialsPort.list_keys`, so a decrypt failure never reads as "no credential".
2. **F1, deliberate disconnect.** A Cabinet disconnect of Gmail or Drive marks the feature state
   (`disconnected_by_user_at`), so R1 skips it; a new consent clears the mark. To Do needs no mark: its disconnect
   resets the config, so no primary list remains (D-P5).
3. **F2, the once-per-transition flag.** The flag lives on the feature state and is written as a single field: Gmail
   `set_reconnect_flags`, To Do `set_reconnect_notified`, Drive `update_sync_flags`. A whole-record rewrite could undo a
   concurrent save. The flag goes up before the send and comes down only when nothing can have been sent; a credential
   that is back lowers it.
4. **D-P4, where users come from.** The hourly task `reconnect_notice_sweep` (Gmail and To Do) lists users from the
   feature stores. The existing fan-outs (daily review, To Do renewal) list from the credential store, which is empty
   for exactly the users R1 is for. Drive's notice stays in the 10-minute drive fan-out, which already visits every
   sync state.

## Alternatives considered

- **Read the old credential collection to find users.** Keeps the old store alive and readable, the thing the RFC
  removes.
- **A one-shot notice at cutover.** Misses every later loss, and a missed send could not be retried.
- **Flags on the credential record.** The record is the thing that is missing in this case.

## Triggers

- The hourly scheduler job (Gmail, To Do) and the drive fan-out (Drive).
- The Cabinet status calls, which show Reconnect without sending anything.
- The one connect callback (`/auth/connect/callback`; the activation per integration), which clears the deliberate-disconnect mark.

## Consequences

- Silence for a deliberate disconnect depends on the mark: a disconnect that predates it reads as a lost grant once.
- One more hourly job; a failure for one user is logged and counted, never stops the rest.
