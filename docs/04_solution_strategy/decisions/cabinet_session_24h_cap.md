# Cabinet session: one login lasts 24h, no silent refresh

**Date:** 2026-09-28
**Status:** Live on `feat/mobile-web-call`

## Decision

A Cabinet browser session is capped at **24 hours from login**, with no silent refresh.

- `ACCESS_TOKEN_TTL` default 3600 → **86400** (`config/auth.py`).
- `REFRESH_TOKEN_TTL` default 2592000 (30 days) → **86400**.
- `SessionService.verify_refresh_token` rejects any refresh token whose `iat` is older than the
  **current** `refresh_token_ttl`, whatever its own `exp` says. Tokens issued under the 30-day
  default therefore die at deploy instead of a month later.
- An expired session sends the call page (`/cabinet/call`) to
  `/auth/login?next=/cabinet/call`. This happens on load and on a 401 from the start-call API.
  The page used to show a bare "Could not connect".

## Why

The owner set the rule: Cabinet data (facts, directives, mail index) is sensitive, and a browser
must not stay authorized for longer than a day. The problem that surfaced it was the mobile call:
the access token lived 1h and the frontend never called `/auth/refresh`. Nearly every call started
from the Home Screen or Siri therefore began with a failed call or a full Google login.

## Alternatives rejected

- **Silent refresh on page load / on 401** (the first plan). It keeps a browser signed in for as
  long as the refresh token lives, which is exactly what the owner ruled out.
- **Keep 1h access and use a 24h refresh.** It is equivalent in security, because both tokens are
  stateless HS256 JWTs with no revocation and both sit in `httponly` cookies. It also adds a
  refresh round-trip, plus the role bug below. A 24h access token is simpler.
- **A long-lived, call-only scoped token.** A call to Lelik reads the biographical cache aloud.
  Narrowing the scope to "call" would not narrow what it exposes.

## Trade-offs

- A role change reaches an issued access token only after up to 24h (it was up to 1h). There was
  no revocation before either.
- The Home Screen app has its own cookie jar, separate from Safari's. Each entry point logs in
  once a day on its own.

## Known debt (not fixed here)

`/auth/refresh` mints the new access token from a stub `BillingAccount` whose `iam_policy` is
empty, so the refreshed token always carries `role: viewer`. An owner would lose the Team tab.
Nothing calls the endpoint today. Fix it by loading user and account from the repositories (the
TODO in the handler) before anything starts using it.
