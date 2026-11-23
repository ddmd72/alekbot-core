# Cabinet device sessions: 15 min access, 14 days idle, 30 days absolute

**Date:** 2026-11-23. **Status:** On `feat/device-sessions-web`. **Supersedes** `cabinet_session_24h_cap.md`.
**Spec:** `docs/10_rfcs/CABINET_DEVICE_SESSIONS_RFC.md` §7.1, §7.2 (the ASVS V7.1.1 record).

## Decision

A Cabinet or call-page sign-in starts a server-side device session (`DeviceSessionPolicy`). The access JWT carries its
`sid` and lives 15 minutes; the rotating refresh secret renews it. The session ends after 14 days without a refresh
and 30 days after sign-in, whatever its activity. Every route that asks who is signed in goes through one verifier that
checks the session record (`src/web/cabinet_auth.py`), so a revoked session is refused within 30 s.

## Why these lifetimes deviate from NIST

NIST SP 800-63B-4 at AAL2 asks for an overall timeout of at most 24 h and an inactivity timeout of at most 1 h; ASVS
4.0.3 V3.3.2 named 12 h / 30 min for L2. ASVS 5.0 V7.1.1 requires the deviation to be documented and justified:
- each account is one person, on their own devices, each behind a device lock;
- a session is revocable server-side in ≤30 s, and every new session is announced in chat;
- where the user sets one, the facts sit behind a PIN with five tries, and a full lock is one chat message away;
- the NIST numbers cost a Google sign-in per entry point per day, the defect this replaces (each Home Screen app has its
  own cookie jar).

## Alternatives rejected

The 24 h sliding stateless session (the superseded record): no revocation of a single device. Long JWTs with a
revocation list: still a read per request, and no devices list. Details: RFC §9.
