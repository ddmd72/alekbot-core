# Cabinet session: 24h tokens, a refresh slides the session

**Date:** 2026-09-28, amended 2026-11-18 (sliding session, owner decision)
**Status:** Live on `feat/mobile-web-call`; the amendment on `qp/d26-cabinet-tokens`

## Decision

A Cabinet browser session is a **sliding session**: a login gives 24 hours, and a refresh may
extend it. It is not a hard cap of 24 hours from login (the rule of 2026-09-28, amended).

- `/auth/refresh` mints a new access token with a full `ACCESS_TOKEN_TTL` from the moment of the
  refresh, from the stored user and account.
- The refresh token is not rotated: it lives `REFRESH_TOKEN_TTL` from login, and an expired one is
  refused. A session therefore lasts at most `REFRESH_TOKEN_TTL + ACCESS_TOKEN_TTL` (48h with the
  defaults) from login.
- Nothing in the Cabinet frontend calls `/auth/refresh` today, so a login still lasts 24h in
  practice until a client calls it.
- `ACCESS_TOKEN_TTL` default 3600 → **86400** (`config/auth.py`).
- `REFRESH_TOKEN_TTL` default 2592000 (30 days) → **86400**.
- `SessionService.verify_refresh_token` rejects any refresh token whose `iat` is older than the
  **current** `refresh_token_ttl`, whatever its own `exp` says. Tokens issued under the 30-day
  default therefore die at deploy instead of a month later.
- An expired session sends the call page (`/cabinet/call`) to
  `/auth/login?next=/cabinet/call`. This happens on load and on a 401 from the start-call API.
  The page used to show a bare "Could not connect".

## Why

The owner set the first rule: Cabinet data (facts, directives, mail index) is sensitive, and a
browser must not stay authorized for longer than a day. On 2026-11-18 the owner chose a sliding
session over enforcing that cap at refresh. The problem that surfaced it was the mobile call:
the access token lived 1h and the frontend never called `/auth/refresh`. Nearly every call started
from the Home Screen or Siri therefore began with a failed call or a full Google login.

## Alternatives rejected

- **An absolute 24h from login, enforced at refresh** (an `auth_time` claim checked by
  `/auth/refresh`). Rejected by the owner on 2026-11-18 in favour of the sliding session.
- **Silent refresh on page load / on 401** (the first plan, 2026-09-28). It keeps a browser signed
  in for as long as the refresh token lives, which the first rule ruled out. The sliding session
  allows a refresh to extend the session; wiring a client to call it is not decided here.
- **Keep 1h access and use a 24h refresh.** It is equivalent in security, because both tokens are
  stateless HS256 JWTs with no revocation and both sit in `httponly` cookies. It also adds a
  refresh round-trip, plus the role bug below. A 24h access token is simpler.
- **A long-lived, call-only scoped token.** A call to Lelik reads the biographical cache aloud.
  Narrowing the scope to "call" would not narrow what it exposes.

## Trade-offs

- A role change reaches an issued access token only after up to 24h (it was up to 1h). There was
  no revocation before either.
- Revocation latency: a refresh re-checks the whitelist (since 2026-11-18, `cabinet_oauth_whitelist_gate.md`), so an
  email removed from the whitelist keeps only its current access token, up to 24h.
- The Home Screen app has its own cookie jar, separate from Safari's. Each entry point logs in
  once a day on its own.

## Known debt (fixed)

`/auth/refresh` used to mint the new access token from a stub `BillingAccount` with an empty
`iam_policy`, so a refreshed token carried `role: viewer` and tier FREE, and a deleted user still
got one. It now loads the stored user and account (`AuthenticationService.resume_session`) and
refuses with 401 when the user is absent or inactive or the account is absent.
