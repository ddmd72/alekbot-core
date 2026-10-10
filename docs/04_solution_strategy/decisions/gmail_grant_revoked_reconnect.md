# A revoked Gmail grant flags the credentials and tells the user once

**Date:** 2026-10-10
**Status:** Live
**Scope:** `GmailProviderAdapter.refresh_token`, `EmailReviewService`, `WorkerHandler`
(`daily_email_review`, `email_indexing`), `OAuthCredentials`, `/api/gmail/status`, Cabinet Gmail card.

## Problem

The owner enabled the daily email review in the Cabinet on 2026-10-09 and never received one.
- Google rejected the Gmail refresh token with `invalid_grant — Token has been expired or revoked`
  somewhere between the last good refresh (05:07 UTC) and the first review (11:12 UTC).
- The review logged one WARNING and returned 200. The auto-index failed the next morning with
  `failed_auth`. Nobody was told.
- `/api/gmail/status` answered `connected: true`, because it only checks whether credentials are
  stored. The Cabinet showed a green "Connected".

Ruled out: the 7-day refresh-token expiry of an OAuth app in Testing status (the grant was weeks
old and refreshed daily), Cabinet sign-ins on the same OAuth client (~30 with `prompt=consent`
in Sep–Oct, the token survived them), a Gmail disconnect (no request), and any GCP-side change
(no OAuth client or secret change in the audit log). What is left is on the Google account side,
for example a password change (which revokes refresh tokens with Gmail scopes) or access removed
in the account settings. That side is not visible to us.

## Decision

1. **Typed error.** `GmailProviderAdapter.refresh_token` raises `OAuthGrantRevokedError`
   (`domain/exceptions.py`, a `ValueError`) on `invalid_grant` only. Other token-endpoint errors
   stay a plain `ValueError`. The message is unchanged, so the indexing service's keyword-based
   `failed_auth` classification still matches.
2. **Persistent flag.** `OAuthCredentials.needs_reconnect` (default `False`, stored in Firestore;
   documents without the field read `False`). Nothing clears it explicitly: a reconnect saves
   freshly exchanged credentials, and so does a successful refresh, and both build the default.
3. **One notice per breakage.** `WorkerHandler._flag_gmail_reconnect` runs when the daily review
   or an indexing page gets `OAuthGrantRevokedError`. It sets the flag and posts one notice
   through `notify()` (formatter agent, user's language) to the primary channel, only on the
   transition from `False` to `True`. Both paths keep hitting the dead token every day until the
   user reconnects, and that must not become a daily message. The task still returns 200,
   because a Cloud Tasks retry cannot fix a revoked grant. A failed notice is logged, not raised.
4. **Visible in the Cabinet.** `/api/gmail/status` returns `needs_reconnect`. The Gmail card shows
   a red dot, "Reconnect required", a one-line explanation, and a **Reconnect Gmail** button
   (`/auth/connect-gmail`, the same consent flow) in place of the index button.

## Alternatives considered

- **A new `mark_needs_reconnect` method on `OAuthCredentialsPort`** (an atomic
  read-and-set in the adapter). It is cleaner, but it changes a port contract that
  `tests/unit/ports/test_email_ports.py` pins exactly. A get, then a save through the existing
  methods, is enough: the only race is the daily review and an indexing page failing within the
  same few milliseconds, and the worst case is a duplicate notice.
- **Stop running the review and indexing while flagged.** That saves one HTTP call a day. The
  failing call costs nothing and needs no extra code path.
- **Flag from interactive email search too** (`EmailSearchService._maybe_refresh`). Not done: the
  user is in the chat at that moment and sees the failure. Flagging there would also suppress the
  background notice, which is the only one that tells them what to do.
