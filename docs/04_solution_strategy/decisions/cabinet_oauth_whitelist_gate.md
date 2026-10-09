# Cabinet sign-in: the whitelist gates every Google OAuth login

**Date:** 2026-10-09
**Status:** On `fix/cabinet-oauth-whitelist`

## The hole

Any Google account could sign in to the Cabinet. `/auth/callback` →
`AuthenticationService.handle_oauth_callback` registered every unknown Google identity: a new
`BillingAccount` (FREE) with the caller as OWNER. No whitelist was read anywhere on that path. An
ID token without an email was registered too ("edge case" branch).

The whitelist (`{prefix}domain_whitelist_v1/config`) was enforced in two places only:

- `IAMService.authorize` for Slack/Telegram, on every message;
- `InviteCodeService.consume_team_invite`.

`IAMService.authorize` also had a `platform="oauth"` branch that checked the whitelist for new
OAuth users. Nothing ever called it, from the initial commit on. The code read as if the web path
were gated, which is why the hole went unnoticed. It was never a regression.

**Blast radius.**
- Tenant isolation held: a stranger got an empty tenant, and the ID-addressed Cabinet routes
  check ownership.
- Chat stayed closed: Branch 1 of `IAMService.authorize` checks the whitelist on `user.email`.
- What was exposed was everything that spends the owner's keys or touches integrations: the Lelik
  web call (xAI + Cloudflare SFU), Gmail connect and indexing (LLM triage), the Microsoft To Do
  connection, phone OTP (Twilio SMS, an SMS-pumping vector) and MCP consent.
- An inventory on 2026-10-09 found no foreign accounts. Every non-whitelisted user was the owner's
  own test account; they were deleted.

## Decision

`AuthenticationService._require_allowed_email(claims)` runs right after ID-token verification and
**before any user lookup**, in both `handle_oauth_callback` and `link_oauth_identity`. It requires:

1. an email in the **verified ID token**. userinfo is never used for this decision, because it is
   only metadata;
2. `email_verified is True` (`None` is rejected);
3. `WhitelistEntry.is_allowed(email)`, which matches an exact email or a domain,
   case-insensitively.

Any failure raises `AccessDeniedError` (`domain/exceptions.py`). It deliberately does not subclass
`ValueError`, because the handlers map `ValueError` to 400. `/auth/callback` answers 403 with a
plain page and sets no session cookies. `/auth/link-oauth` answers 403. The reason is logged and
never shown to the caller.

The gate applies to **existing users as well**, with the same semantics as the chat path:
removing an email from the whitelist revokes Cabinet access at the next login. It also closes the
link-by-email branch, which used to attach a Google identity to an existing user by email without
looking at `email_verified`.

The dead `platform="oauth"` branch of `IAMService.authorize`, `IAMService._create_new_user`, and
the `email` parameter of `PlatformAuthPort.authorize` were deleted. A second, uncalled whitelist
path is what made the real one look present.

## Revocation latency: ≤24h, accepted

The check runs at login, not per request. A Cabinet session is capped at 24h from login with no
silent refresh (`cabinet_session_24h_cap.md`), so a revoked email loses access within a day. A
per-request check in `auth_required` would add one Firestore read to every API call. For instant
revocation, rotate the session signing secret, which logs everyone out.

## Alternatives rejected

- **Wire `IAMService.authorize("oauth", …)` into the callback.** That branch looked users up by
  email before the whitelist, created users with a placeholder `external_user_id`, and duplicated
  `register_new_user`. Two registration paths for one flow is the drift that caused this.
- **Gate in `oauth_callback` (web layer).** `link_oauth_identity` and any future caller of the
  service would bypass it. The service is the one place every OAuth sign-in passes through.

## Also deleted: `IdentityResolver`

`src/services/identity_resolver.py` contained an auto-registering `_register_new_user` for
platform identities. It had no callers, because `IAMService` replaced it, and it was likely the
source of the two email-less Slack users created in January 2026. It was deleted along with its
test so that no second registration path is left in the code.
