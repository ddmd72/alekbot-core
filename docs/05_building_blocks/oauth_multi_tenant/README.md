<!-- living-doc
audited: 2026-11-23
covers:
  src/adapters/firestore_credential_store.py
  src/ports/oauth_credentials_port.py
  src/services/oauth_connect_service.py
  src/services/integration_reconnect_service.py
  src/composition/oauth_connect_setup.py
  src/adapters/google/google_oauth_provider.py
  src/adapters/microsoft/microsoft_oauth_provider.py
  src/ports/oauth_provider_port.py
  src/composition/connector_registry.py
  src/domain/connector_spec.py
  src/domain/oauth_credential.py
  src/domain/oauth_flow.py
  src/domain/oauth_grant.py
  src/domain/credential_key.py
  src/domain/connect_outcome.py
  src/domain/flow_intent.py
  src/domain/revocation_scope.py
  src/domain/provider_account.py
  src/domain/integration.py
  src/services/credential_revocation_service.py
  src/services/authentication_service.py
  src/services/iam_service.py
  src/services/invite_code_service.py
  src/services/configuration_service.py
  src/composition/identity_setup.py
  src/adapters/firestore_user_repo.py
  src/adapters/firestore_account_repo.py
  src/adapters/firestore_whitelist_repo.py
  src/adapters/firestore_invite_code_repo.py
  src/domain/invite_code.py
  src/domain/whitelist.py
  src/ports/account_repository.py
  src/ports/iam_port.py
  src/ports/invite_code_repository.py
  src/ports/platform_auth_port.py
  src/ports/user_repository.py
  src/ports/whitelist_repository.py
-->
# OAuth Multi-Tenant

## Purpose

This block answers two questions for every request the system serves: **who is this person and may they be here**
(identity, tenancy, the whitelist, invites, roles), and **which outside accounts have they connected** (the OAuth
grants for Gmail, Google Tasks, Microsoft To Do and the drives, how a consent is taken, stored, revoked and noticed
when lost).

The tenant is the `BillingAccount`: a user registered through the Cabinet gets a new account of their own and is its
owner. A user can later move into another
account through a team invite.

What it is not: the web routes, cookies and JWTs of the Cabinet sign-in are [oauth_web_api](../oauth_web_api/README.md);
the Cabinet pages are [user_cabinet](../user_cabinet/README.md); refreshing an access token belongs to each data
adapter's token provider, not to this block (`OAuthProviderPort` has no refresh); what a drive connection does after
a consent is the drive block's.

## Owned data

Firestore, `us-production` database, environment prefix on collection names (`EnvironmentConfig`).

- **Users** (`EnvironmentConfig.domain_users_collection`, `FirestoreUserRepository`): `user_id` (internal UUID),
  `external_user_id` (`firebase|<sub>`, set by the Google sign-in), `email`, `auth_metadata` (the last sign-in's
  provider data; its `email` is what a session renewal re-checks), `platform_identities` (platform name to the
  platform's user id), `account_id`.
- **Accounts** (`EnvironmentConfig.account_collection_name`, `FirestoreAccountRepository`): the `BillingAccount`,
  its `iam_policy` (user id to the stored `Role` value) and `account_defaults`.
- **Whitelist** (`EnvironmentConfig.domain_whitelist_collection`, singleton document `config`,
  `FirestoreWhitelistRepository`): allowed emails and allowed domains. No cache; a missing document is an empty list,
  which allows nobody.
- **Invite codes** (`EnvironmentConfig.domain_invite_codes_collection`, `FirestoreInviteCodeRepository`):
  `InviteType.SELF_LINK` or `TEAM_INVITE`, the account, the role (team invite), expiry and the claim and completion
  stamps. The raw code is shown once at creation; storage holds a digest and a short hint
  (`domain/invite_code.py::InviteCode`, `HINT_LENGTH`). A new code never replaces a stored one: creation is a
  transaction that refuses an existing id (`FirestoreInviteCodeRepository.create`).
- **Credentials** (`EnvironmentConfig.credentials_collection`, `FirestoreCredentialStore`): one document per stored
  grant, id `{user_id}:{credential_id}`. Plaintext, queried fields: `user_id`, `credential_id` (opaque, random, kept
  across every refresh and re-consent of the same provider account), `kind`, `integration`, `account_id`,
  `account.realm` and `account.subject`, `scopes`, `token_expiry`, `needs_reconnect`. One `secret` ciphertext holds the
  access token, the refresh token and the account label, with the document id in its AAD, so a ciphertext copied to
  another document does not decrypt.
- **Connect flows** (`EnvironmentConfig.oauth_flows_collection` through `FirestoreEphemeralStore`): one pending flow
  per started consent, key `oauth_flow:<keyed digest of the OAuth state>`, single use, short-lived
  (`oauth_connect_service.py::_FLOW_TTL_S`). The PKCE verifier is stored sealed.

The connector kinds are data, in `composition/connector_registry.py` (`_SPECS`; a frozen kind has `offered` False). Each `ConnectorSpec` names its integration (the
Cabinet group: mail, tasks, drive), its realm (`google`, `microsoft`), the scopes a consent must grant, whether a user
may hold several credentials of the kind (`multiple`: the two drives), and for drives the `id_space` prefix.

## Flows

**Sign-in and registration** (`services/authentication_service.py::AuthenticationService.handle_oauth_callback`).
The code is exchanged and the ID token verified through the auth provider, then the whitelist gate
(`_require_allowed_email`) runs before any user read or write; no email, an unverified email or a non-listed one
raises `AccessDeniedError`. The user is then found by `external_user_id`; else by email, and the Google identity is
linked to that existing user (a locked, i.e. inactive, user found either way raises `AccountLockedError` before anything
is written: `_refuse_locked`); else `register_new_user` creates a free-tier `BillingAccount` and the user, and writes
the user as `owner` in `iam_policy`. A pending team-invite cookie is consumed after the callback, best effort
(`web/oauth_app.py`). Session renewal re-runs the whitelist check on the stored email and refuses an inactive user
(`AuthenticationService.resume_session`), so an email taken off the list, or a locked account, stops renewing.

**Chat authorization** (`services/iam_service.py::IAMService.authorize`). Called by the Slack and Telegram adapters on
every message, no cache. A platform user that exists, is active and whose email is still whitelisted is allowed; an unknown one is
rejected with a "register in the Cabinet first" message (its link is `DeployTopology.cabinet_url`, the service's own
host — never a host written in code), a locked (inactive) one with the fixed `ACCOUNT_LOCKED_REPLY`
(checked before the whitelist), a de-listed one with a "revoked" message. Chat can never
create an account.

**Linking a Google identity.** A user record without a Google identity can link one from the Cabinet (`web/oauth_app.py`: `/auth/link-oauth`, `AuthenticationService.link_oauth_identity`): the same whitelist gate, and an identity already linked to another user is refused.

**Platform linking.** After registering, the user binds a platform id from the Cabinet (`web/user_cabinet_app.py`:
`/api/user/link-platform` for the allowed platforms, Telegram and phone have their own validating routes). The binding is
`FirestoreUserRepository.link_platform_identity`, one transaction that refuses an id already bound to another user.

**Team invite.** Only an account owner can generate one (`web/user_cabinet_app.py`, the only role gate in the code); accepting is `services/invite_code_service.py::InviteCodeService.consume_team_invite`). Checks, in order: code
exists and is valid (or the same user is resuming), type is a team invite, the joiner is not already a member, the
joiner's email is whitelisted, the target account exists, the role parses. Then the code is claimed in one transaction
(`InviteCodeRepository.claim`), the role is written (`AccountRepository.set_member_role`), the user's `account_id` is
moved (a write of that one field, so a concurrent change of another profile field survives), and the code is stamped
complete. A failure after the claim leaves the code claimed and unfinished; the same user
can retry until a grace period past expiry (`_RESUME_GRACE`); a completed code is refused to everyone.

**Connecting an integration** (`services/oauth_connect_service.py::OAuthConnectService`; routes
`GET /auth/connect/<kind>` and `GET /auth/connect/callback` in `web/oauth_app.py`).
1. `start` resolves the spec and the realm's edge, picks the intent (`FlowIntent.CONNECT`, `ADD`, or `RECONNECT` when a
   credential id is given; a reconnect wins over add and must name a credential the user holds), stores the flow, and
   returns the edge's consent URL. The edges (`GoogleOAuthProvider`, `MicrosoftOAuthProvider`) use PKCE S256, add their
   identity scopes to the spec's, and send a login hint only on reconnect. Microsoft uses the `consumers` tenant
   (personal accounts only) and always shows the account chooser.
2. `complete` consumes the flow once (`get_and_delete`), then refuses in this order: unknown or expired flow (`state`), a
   session user other than the flow's (`user`), an exchange failure (`exchange`), a granted-scope set missing a required
   scope (`scope`), a reconnect that came back with another provider account (`account`). Outcomes are
   `domain/connect_outcome.py::ConnectStatus`.
3. `_store` updates the record of the same provider account (tokens, scopes, expiry; `needs_reconnect` cleared; a
   missing new refresh token keeps the old one) or saves a new record. For a kind with `multiple` False the new save
   removes the user's other records of the kind through the revoke rule.
4. The integration's activation runs (`composition/oauth_connect_setup.py::build_connect_activations`): mail and To Do
   clear the reconnect state; To Do also enqueues the `setup_microsoft_todo` worker task; drive creates or reconnects
   its `DriveConnection`. An activation that raises is logged and the connect still counts; one that raises
   `ConnectActivationFailed` removes the just-saved credential and the outcome is `activation`.

**Removing a credential** (`services/credential_revocation_service.py::CredentialRevocationService.remove`). Our
record is always deleted. The provider grant is revoked only when the realm's revoke is `RevocationScope.GRANT_WIDE`
(Google; Microsoft is `NONE` and never revoked) and no credential of the same provider account remains across all users
(`OAuthCredentialsPort.list_by_account`), so removing Gmail never kills the grant the user's Google Drive still uses.
A revoke failure is logged; the removal still counts.

**Lost credential: reconnect state and the one notice**
(`services/integration_reconnect_service.py::IntegrationReconnectService`). Feature state, not the credential store,
says a connection existed (Gmail indexing state; To Do config with a primary list; the drive's connections). A past
connection with no credential, and not a deliberate disconnect, shows Reconnect in the Cabinet (`status`) and the hourly
`sweep` sends one chat notice per user and integration, guarded by a flag that goes up before the send and down again
only when nothing can have been sent. `consented` clears the state on a new consent; `mark_disconnected` (Gmail only)
records a deliberate disconnect. The drive's notice runs in the drive fan-out instead of the sweep. Rules:
`domain/reconnect_policy.py::r1_due`, `reconnect_needed`.

**Configuration inheritance** (`services/configuration_service.py::ConfigurationService`). The limit getters
(for example `get_semantic_search_limit`) resolve user override, then `account_defaults`, then the system default. `ConfigurationService.effective_for(user_id)`
resolves per-intent availability (`UserBotConfig.intent_access`) over the same levels, read fresh on every call
(`CONFIG_OVERRIDE_HIERARCHY_RFC.md` §3.3).

## Invariants and traps

- **Every entry path has its own whitelist check; there is no shared gate.** Cabinet sign-in, link and renewal
  (`AuthenticationService`), chat messages and button choices (`IAMService.authorize`), team-invite accept
  (`consume_team_invite`), the remote MCP server's grants at each refresh rotation
  (`services/mcp_authorization_service.py`), the voice MCP token mint (`services/voice_mcp_token_service.py`).
  `IAMService` does not gate the web. The account lock (`UserProfile.is_active`) is likewise checked per entry point, and
  a new entry point adds it (`tests/unit/test_locked_user_entry_points.py`; `CABINET_DEVICE_SESSIONS_RFC` §7.8). A new entry path needs its own check. Why: `decisions/cabinet_oauth_whitelist_gate.md`.
- **Chat cannot register.** Accounts are created only by `register_new_user`, reached only from the web sign-in.
- **One user's credential is never served to another.** The store checks the stored `user_id` on `get`
  (`FirestoreCredentialStore.get`); only `list_by_account` and `list_user_ids` read across users.
- **`credential_id` is chosen by the store**, never by a caller, and an update never takes identity fields (kind,
  integration, account, realm, subject) from its argument. `save` refuses a credential without account subject,
  `integration` or `account_id` and stores nothing. Contract: `ports/oauth_credentials_port.py::OAuthCredentialsPort`,
  shape test `tests/unit/test_req_arch_33_credential_port_shape.py`.
- **An existence check never decrypts.** `list_keys`, `list_by_account`, `list_user_ids` read plaintext fields only, so
  a cipher failure can never read as "not connected"; a decrypt failure on `get` or `list_for_user` raises.
- **Services hold no kind literal and no OAuth parameter name.** A service gets its `ConnectorSpec` by constructor and
  filters by `spec.kind`, never by integration: one integration can have several kinds (To Do and Google Tasks are both
  tasks). The edge, not the service, maps intent and login hint to its realm's parameters.
- **A consent grant wider than asked is stored as is**; only the count is logged, never a scope string
  (`OAuthConnectService._warn_extra_scopes`). The edge's own identity scopes do not count as extra.
- **Cardinality is policy above the store.** The store holds any number of records per user and kind; `spec.multiple`
  and `_store` decide.
- **`update` never recreates a deleted record** (it is a transaction that returns False when the document is gone), so a
  refresh finishing after a disconnect writes nothing; `save` is called only by the connect flow. Still unconditional:
  an `update` from a stale copy overwrites tokens and flag, for example a reconnect flag written from a copy read
  before a new consent puts the old tokens back. The decision record
  `decisions/oauth_credential_writes_unconditional.md` predates the store and describes the old repository.
- **Role values are stored lowercase** in `iam_policy` (`ports/iam_port.py::Role`); a team invite keeps its role uppercase and
  converts before writing. Writing a member is a field-path update, never a whole-map write, so concurrent adds are kept.
- **Profile writes after a read are field-level.** OAuth link and team join write only the changed fields
  (`update_user_fields`), and `unlink_platform_identity` a single dotted path, so a concurrent lock or language change
  is not undone by a whole-document `update_user`/`create_user` rewrite (`ports/user_repository.py::UserRepository`).
- **Joining a team overwrites the user's `account_id`.** Data the user owned in their previous account is not migrated
  (`consume_team_invite` comment).
- **A deliberate disconnect must not read as a loss.** Gmail records it (`mark_disconnected`); To Do's disconnect resets
  its config; a drive disconnect ends the connection.
- **Test trap:** a reconnect must not trigger a reindex (`tests/unit/test_reconnect_does_not_reindex.py`).

## Links

- [`oauth_web_api`](../oauth_web_api/README.md) (routes, cookies, JWT), [`user_cabinet`](../user_cabinet/README.md),
  [`gmail_email_indexing`](../gmail_email_indexing/README.md).
- RFCs: [`MULTI_DRIVE_RFC`](../../10_rfcs/MULTI_DRIVE_RFC.md) (connectors, connect flow, revoke rule),
  [`SECRETS_AT_REST_RFC`](../../10_rfcs/SECRETS_AT_REST_RFC.md) (credential store, R1 reconnect notice).
- Decisions (`docs/04_solution_strategy/decisions/`): `cabinet_oauth_whitelist_gate.md`, `credential_store_cutover.md`,
  `oauth_credential_writes_unconditional.md`, `reconnect_notice_r1.md`, `gmail_reconnect_notice_at_most_once.md`,
  `gmail_grant_revoked_reconnect.md`, `secrets_at_rest_cipher_and_digest.md`.

## Where to start reading

- `src/services/authentication_service.py::AuthenticationService.handle_oauth_callback` (sign-in, registration).
- `src/services/iam_service.py::IAMService.authorize` (chat authorization).
- `src/services/invite_code_service.py::InviteCodeService.consume_team_invite` (team join).
- `src/services/oauth_connect_service.py::OAuthConnectService.complete` (the connect flow).
- `src/composition/connector_registry.py::ConnectorRegistry` (the kinds and their specs).
- `src/adapters/firestore_credential_store.py::FirestoreCredentialStore` (the credential record).
- `src/services/integration_reconnect_service.py::IntegrationReconnectService.sweep` (lost-credential notices).
