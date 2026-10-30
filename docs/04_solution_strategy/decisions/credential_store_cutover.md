# Decision: integration credentials move to a new encrypted store, by reconnect

**Date:** 2026-10-29
**Status:** S3 built; the port was re-keyed by credential in multi-drive slice 1a (`OAuthCredential`, `get(user_id, credential_id)`, `save` returns the id; `docs/10_rfcs/MULTI_DRIVE_RFC.md` §4.2)
**Context:** `docs/10_rfcs/SECRETS_AT_REST_RFC.md` §2.3, §2.4, §6. Gmail, Google Tasks, Microsoft To Do and OneDrive grants were
stored as plaintext tokens in `{prefix}oauth_credentials`. S1 built the cipher; S3 applies it to these grants.

## Decision

- **A new collection, `{prefix}credentials`** (`FirestoreCredentialStore`, replacing `FirestoreOAuthCredentialsAdapter`), doc id
  `{user_id}:{credential_id}`. `credential_id` is opaque, random and immutable: it survives every refresh and every consent of the
  same account.
- **Identity on the record.** `account.realm` (`google` | `microsoft`) and `account.subject` (the provider's stable account id) are
  plaintext fields. At most one record per (user, kind) in this stage (I5).
- **Plaintext / secret split.** Queried and operational fields stay plaintext (`user_id`, `credential_id`, `kind`, `account.*`,
  `scopes`, `token_expiry`, `needs_reconnect`, `drive_id`, timestamps). `secret` is ONE `CipherPort` ciphertext of
  `{access_token, refresh_token, account_label}`, so a read or a write is one cipher call. Listings and the existence checks
  (`list_keys`, which returns `CredentialKey`s) read plaintext only and never touch KMS.
- **AAD = record identity**: `credentials/{user_id}:{credential_id}`. A ciphertext moved to another record does not decrypt (I2).
- **The identity is read at every consent, and a failed read stores nothing (I5, I9).** Google: scopes `openid email`, userinfo
  `sub`. Microsoft (To Do and OneDrive): scope `User.Read`, Graph `/me` `id`. A missing `sub` / `id` raises before the save;
  `save` also refuses an empty `account_subject`.
- **A same-account consent keeps the `credential_id`**; another account gets a new id and the old record is deleted, in one
  transaction.
- **An update keeps the record identity (D-P2).** `update` (refresh, flag write) is exists-or-`False`, rebuilds the
  secret and the mutable fields from its argument, and never takes `credential_id`, `kind` or `account.*` from it.
- **Fail loud.** A decrypt or KMS error raises and is logged without values (I4); it is never "not connected". Two records for one
  (user, kind) raise rather than guess.
- **Migration by reconnect.** The new store starts empty and the old collection is never read or written by the new code: no
  copy, no dual read. Every integration shows "not connected" (R1 shows Reconnect and sends one notice) until the user consents once.
- **The To Do consent rule.** A consent is a new grant, so `TaskSetupService.setup` always re-subscribes: it drops every stored
  subscription (deleting each at Graph, best effort), registers one, and enqueues `reindex_task_list` exactly once. The re-read is
  an upsert that deletes nothing. The renewal sweep keeps `ensure_subscriptions` (an active subscription is kept, nothing re-read).
- **Services.** The main service and the indexer hold the key grant; the indexer reads `KMS_CRYPTO_KEY` from its environment
  (`--set-secrets`) and refuses to start without it. The relay, sandbox and deep-research job get no grant.

## Rejected alternatives

- **Copy-migration** (decrypt nothing, re-encrypt the old records): needs a one-off script holding the plaintext, and leaves the
  exposed grants alive. Revoking the old grants and asking for one consent retires them at once.
- **Dual read** (new store first, old as fallback): keeps the plaintext reachable and a legacy branch in every reader (RFC §2.7).
- **Passthrough cipher** (a no-op cipher for old records): I3 forbids a no-op cipher in any runtime.
- **Keeping the doc id `{user_id}_{provider}`**: it cannot carry an opaque, stable record identity.

## Consequences

- Each user reconnects each integration once; the old Google refresh tokens are revoked right after the deploy, the Microsoft
  consent is removed by each account holder (RFC §6.3).
- A OneDrive reconnect stores `drive_id` for the first time (a failed drive id read stores an empty one and leaves the switch
  check off); the same drive resumes from the cursor.

## Triggers to revisit

- **Multi-drive 1a** re-keyed the port by credential and applies the revoke rule over `account.realm` + `account.subject`
  (`CredentialRevocationService`); every kind is still one record per user. **Slice 1b** allows more than one drive record per
  user and moves `drive_id` to `DriveConnection`.
- A new credential kind (the Telegram reader session) is one more `kind` in this store.
