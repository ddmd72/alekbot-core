# RFC: Multi-drive, stages 1 and 2: connections, handles, tagged refs, mounts

**Status:** ACCEPTED (rev 1.2, 2026-10-30). Slice 1a built.
**Owner:** AI Engineering
**Milestone:** Multi-drive programme. Stage 0 (secrets at rest) is done: [SECRETS_AT_REST_RFC.md](SECRETS_AT_REST_RFC.md),
S1–S5. This RFC is stage 1 (connections: preparatory, the user sees nothing change) and stage 2 (the product core for
several connections, proven on a second OneDrive App Folder account and an account-wide fake provider). Stage 3+ is
adapter work only (the Google drive adapter with its native-document emulation, its OAuth edge, one registry entry)
and out of scope; §6, §7 and §11.3 define the seams it plugs into.
**Revisions:** rev 1 (2026-10-30); rev 1.1 (2026-10-30, review round 1: cut-over keeps paid transcripts, provider-neutral
scopes and the account-wide feed moved into stage 2); rev 1.2 (2026-10-30, accepted; slice 1a built: the engineering decisions of the slice added as
E26–E32, the port signatures `save -> str` and `list_keys`, `authorization_url(..., *, state)`; the flow no longer stores the hint label).
**Builds on:** [USER_DRIVE_RFC.md](USER_DRIVE_RFC.md), [USER_DRIVE_INDEXING_RFC.md](USER_DRIVE_INDEXING_RFC.md) (index,
tick, transcripts, mirror), [DRIVE_SPECIALIST_RFC.md](DRIVE_SPECIALIST_RFC.md), [DRIVE_FILE_NOTES_RFC.md](DRIVE_FILE_NOTES_RFC.md).
**Peer:** [EXTERNAL_FILES_RFC.md](EXTERNAL_FILES_RFC.md) (accepted, not built). The tagged ref of §9 is the drive row of
its §4.1 table, and its P9 classifier delegates to §9's codec. That RFC is not edited here. Its mentions of
`open_file_from_drive` are stale: the drive specialist replaced that verb.
**Execution plan:** the execution plan (private ops repo) holds the frozen core, the parts, the test plan and the owner
steps per slice. This RFC holds what stays true after the deploy.

Notation: **D-n** owner decisions (§2), **E-n** engineering decisions (§16, each with its reason and the rejected
alternative), **I-n** invariants, **T-n** architecture and contract tests (§13). Code cites are `main` @ `3a78bdfb`.

---

## 0. In one paragraph

A **drive connection** becomes a first-class entity: one per connected drive, several per user, several per provider.
Credentials of every integration are addressed by an opaque id, never by a provider name. One OAuth edge per provider
realm sits behind one port and one generic route pair, with a server-side flow in place of the unsigned connect cookies.
The drive port takes a `DriveHandle` per call; a composition router dispatches it to the adapter registered for the
connection's kind. Everything the indexer keys becomes connection-scoped, and ticks and the fan-out run per connection.
Refs carry a short connection tag (`drive:<tag>:<item>`). Stage 1 ships all of this with one connection per user and
prints every path as today. The live index is purged and rebuilt, not migrated; paid transcripts, notes, the
`_Transcripts` mirror state, usage and the initial-build record survive by stable keys. Stage 2 adds the mount tree
(`<mount>/<path>`) under names the user gives the drives, a primary pointer, per-connection access policy, the
searched-by-default flag, "also on", the owner's note on a whole drive (the one drive thing chat can write) and the
Cabinet connection list, where every drive setting lives; and the provider-neutral machinery an account-wide drive
needs (ancestry filter, selected folders, "whole drive"). The bar: a new connection of a new provider is an adapter, an OAuth edge and one registry
entry, and several connections of one provider work.

## 1. Problem and the bar

Today every drive code path assumes one drive per user:
- the credential port addresses a grant by `(user_id, provider)` and the store refuses a second record of a kind
  (`src/ports/oauth_credentials_port.py:32-90`, `src/adapters/firestore_credential_store.py:92-97`);
- `UserDrivePort` takes `user_id` in every method (`src/ports/user_drive_port.py:25-130`), and the OneDrive adapter
  hardcodes its credential kind (`src/adapters/microsoft/onedrive_adapter.py:65,245,248,532,544`);
- sync state, lease, task keys and records are per user (`src/domain/drive_task_keys.py:17-30`,
  `src/domain/drive_index.py:155-156`), and the fan-out iterates users (`src/services/drive_sync_scheduler_service.py:120-123`);
- a ref names only an item (`drive:<item_id>`, `src/domain/user_drive.py:18,181-194`);
- provider shapes sit in domain: raw Graph facets (`src/domain/drive_index.py:174,261`), `DriveItem.ctag`
  (`user_drive.py:105`), `ONEDRIVE_PROVIDER_KEY` and the unprefixed quickXorHash content key (`src/domain/drive_note.py:69`,
  `src/domain/drive_transcript_windows.py:61-67`);
- OAuth is four route pairs in `src/web/oauth_app.py` with HTTP inside the web layer (`:798-828`, `:944-975`) and in
  `src/services/google_oauth_service.py` (TD-V6), and the callback trusts plain `*_connect_user_id` cookies
  (`oauth_app.py:552,642,762,920`).

**The bar (D12).** After stage 2: a connection of a new provider, an App-Folder-shaped one or an account-wide one
with no area root (Google Drive full access is the next), is a `UserDrivePort` adapter + an `OAuthProviderPort` edge +
one `ConnectorRegistry` entry. Agents, prompts, services, domain, the Cabinet and composition rules do not change.
Two connections of one provider work. No legacy special case anywhere: no "the old connection's key prefix is the user
id", no overloaded `provider` argument, no "an unprefixed hash means quickxor". So everything provider-neutral that
an account-wide drive needs is built in stage 2: the account-wide feed filtered by ancestry, selected-folder scopes and
"whole drive", their access rows, the Cabinet's scope picker and the empty default scope (§7.1, §11.3, §11.7).
Provider specifics stay adapter emulation: Google native documents are exported by the adapter's `download`, marked
`can_write_content=False` and reported as not downloadable when an export would exceed the existing caps; no domain
field or branch exists for them (§7.1). Stage 1 proves the seams below the agent with the fake-provider-twice test;
stage 2 proves them with an account-wide fake provider (T-4) and the product side live, on a second OneDrive account.

## 2. Settled owner decisions

These are not reopened. Dates are the owner's rounds.

| # | Decision | Date |
|---|---|---|
| D1 | A connection is a first-class entity; N per user; the model sees one tree with mount points; refs carry the connection. | 2026-10-29 |
| D2 | Ownership and sharing are not a product question: the provider's permissions decide; how such items enter the tree is the adapter's job. | 2026-10-29 |
| D3 | Default access per connection: read everything in the indexed folders, write into one folder; changeable per connection in the Cabinet. Index selected folders, "whole drive" an option. | 2026-10-29 |
| D4 | Search covers all drives by default, narrowed by words; a per-connection flag "not searched by default" (searched only when named). | 2026-10-29 |
| D5 | The same file on two drives: one result plus an "also on: …" line. | 2026-10-29 |
| D6 | Cross-drive move: not in v1. | 2026-10-29 (concept round) |
| D7 | The Inbox and deep-research copies go to the primary drive. A recording's transcript goes to that connection's own `_Transcripts`; a connection with no write folder gets no mirror (no fallback to the primary). | 2026-10-29 (concept round) |
| D8 | Budgets: a one-time initial build ($40) per new connection; monthly budgets shared per user. No fresh $40 when the same provider account is reconnected. | 2026-10-29 (concept, stage-1 rounds) |
| D9 | Disconnect: the index is purged, notes are archived (found like notes of deleted files); paid transcripts are kept. Double transcription across providers is accepted. | 2026-10-29 |
| D10 | Google native Docs/Sheets/Slides: v1 read and search only (stage 3). | 2026-10-29 |
| D11 | A new connection is read-only until the owner picks a write folder. | 2026-10-29 (concept round) |
| D12 | Hexagonal with no crutches; several connections of one provider must work; the cleanup is the first, preparatory stage. | 2026-10-29 |
| D13 | Old `drive:` refs in stored history are ignored: no rewrite, no legacy resolver; they answer "search again" once. | 2026-10-29 (stage-1 round) |
| D14 | New redirect URIs are an owner action before the stage-1 deploy. | 2026-10-29 (stage-1 round) |
| D15 | Stage 1 changes nothing the user sees, except the index rebuild after cut-over and D13. Canonical `<mount>/` printing is stage 2. | 2026-10-29 / 2026-10-30 |
| D16 | The live drive index is not migrated: it is purged and rebuilt by the indexer after cut-over; the owner accepts the model spend. Stored transcripts, owner notes, the `_Transcripts` mirror roots and ledger, monthly model usage and the initial-build record survive by stable keys. | 2026-10-30 |
| D17 | The user names each drive; the name is what paths and the model use. | 2026-10-30 |
| D18 | Every per-connection setting (name, folders or whole drive, searched by default, write folder, primary, indexing, transcription, connect, disconnect) is set in the Cabinet only; in chat the bot only points to the Cabinet. | 2026-10-30 |
| D19 | In chat the bot keeps only notes about a drive: the owner's note on a whole connection, written through the drive specialist like file and folder notes. | 2026-10-30 |

## 3. Entities and placement

One public class per file (root `CLAUDE.md`). Layer semantics as there: domain is pure, ports only at real boundaries,
composition the only layer that crosses all others.

| Class | File | Stage |
|---|---|---|
| `OAuthCredential` (renamed from `OAuthCredentials`; out of `domain/email.py`): `credential_id, user_id, account_id, kind, integration, account: ProviderAccount, access_token, refresh_token, token_expiry, scopes, needs_reconnect`. `drive_id` and the field name `email_address` leave (the label is `account.label`, still inside the ciphertext). | `src/domain/oauth_credential.py` | 1 |
| `Integration` enum (`MAIL`, `TASKS`, `DRIVE`) | `src/domain/integration.py` | 1 |
| `ProviderAccount(realm, subject, label)` | `src/domain/provider_account.py` | 1 |
| `OAuthFlow` (server-side connect flow) | `src/domain/oauth_flow.py` | 1 |
| `GrantResult(access_token, refresh_token, token_expiry, scopes, account)` | `src/domain/oauth_grant.py` | 1 |
| `ConnectorSpec` (type only: `kind, integration, realm, required_scopes, multiple, id_space, label`; values live in composition) | `src/domain/connector_spec.py` | 1 |
| `DriveConnection` (entity, §4.4) | `src/domain/drive_connection.py` | 1 |
| `DriveConnectionStatus` (today's `DriveConnection` DTO renamed, without `drive_id`; its one consumer `/api/drive/status` retires with 2b's connection list) | `src/domain/drive_connection_status.py` | 1 (retired 2) |
| `DriveUserSettings(user_id, primary_connection_id)` | `src/domain/drive_user_settings.py` | 1 |
| `DriveHandle(user_id, connection_id, credential_id, kind)` | `src/domain/drive_handle.py` | 1 |
| `ConnectionScope(user_id, connection_id)` | `src/domain/connection_scope.py` | 1 |
| `DriveRef` codec (`drive:<tag>:<item_id>`, §9) | `src/domain/drive_ref.py` | 1 |
| `DriveCapabilities` | `src/domain/drive_capabilities.py` | 1 (fields grow per stage, §7.1) |
| `DriveMediaFacts`, `ContentIdentity(family, digest, size)`, `MediaSource(url, headers, expires_at)` | one `src/domain/drive_*.py` file each | 1 |
| `ItemAccess` | `src/domain/drive_item_access.py` | 2 |
| Mount-name validation and former-name hold (`validate_mount_name`, in the mounts module) | `src/domain/drive_mounts.py` | 2 |
| Mount tree resolution (pure: names, former names, canonical paths, shadowing) | `src/domain/drive_mounts.py` | 2 |
| Access policy (`decide(op, policy, capabilities, item_access, ancestry) -> Allowed | Refused(line)`) | `src/domain/drive_access_policy.py` (not `drive_access.py`, which holds the cannot-open notice) | 2 |
| `OAuthCredentialsPort` re-keyed (§4.2) | `src/ports/oauth_credentials_port.py` | 1 |
| `OAuthProviderPort` (§5) | `src/ports/oauth_provider_port.py` | 1 |
| `DriveConnectionRepository` (connections + user settings) | `src/ports/drive_connection_repository.py` | 1 |
| `UserDrivePort` by handle (§6) | `src/ports/user_drive_port.py` | 1 |
| `FirestoreDriveConnectionRepository` (+ in-memory twin in `tests/support/`) | `src/adapters/firestore_drive_connection_repo.py` | 1 |
| `MicrosoftOAuthProvider` (next to `graph_auth.py`) | `src/adapters/microsoft/microsoft_oauth_provider.py` | 1 |
| `GoogleOAuthProvider` (replaces `services/google_oauth_service.py`; new subpackage) | `src/adapters/google/google_oauth_provider.py` | 1 |
| `OAuthConnectService` (start flow, complete flow, reconnect rules) | `src/services/oauth_connect_service.py` | 1 |
| `CredentialRevocationService` (the revoke rule, §4.3) | `src/services/credential_revocation_service.py` | 1 |
| `DriveConnectionService` (create, resolve ref, primary, flags, reconnect flag, disconnect) | `src/services/drive_connection_service.py` | 1 (Cabinet actions 2) |
| `ConnectorRegistry` (kind → spec, drive adapter, OAuth edge) | `src/composition/connector_registry.py` | 1 |
| `DriveProviderRouter(UserDrivePort)` (precedent: `UserAgentFactory` implements `AgentFactoryPort`) | `src/composition/drive_provider_router.py` | 1 |
| Generic connect routes (the four per-integration pairs go); `GET /api/connectors` (the registry's kinds and labels per integration, which the Cabinet renders its connect buttons from) | `src/web/oauth_app.py`, `src/web/user_cabinet_app.py` | 1 |
| Cut-over script (§12) | `scripts/infrastructure/multi_drive_cutover.py` | 1 |

Removed: `services/google_oauth_service.py`; `OAuthCredentialsPort.list_users_by_provider` and `list_connected_providers`
(the latter has no caller, `oauth_credentials_port.py:86`); `UserDrivePort.display_name`, `is_connected`, `disconnect`,
`connection`, `flag_reconnect`, `download_url`; `ONEDRIVE_PROVIDER_KEY`, `CONTENT_KEY_FAMILY`, `AUDIT_AREA` from domain;
`UserBotConfig.drive_indexing_enabled` and `drive_transcribe_enabled` (moved to the connection; `drive_copy_deep_research`
stays per user, it is about the primary).

## 4. Credentials and connections

### 4.1 The grant and the mount are two entities

A credential is a **grant** with an identity: which integration, which provider account, which tokens. A drive
connection is a **mount** of a drive that a grant reads. Gmail and To Do keep one credential and gain nothing from a
connection document; drives get both (E1). Drives read reconnect state only from the connection (`state`), never from
the credential's `needs_reconnect`, which the connection service keeps in step. Stage 0 already gave every credential an opaque `credential_id`, a doc id
`{user_id}:{credential_id}` and `account.realm` / `account.subject` (`firestore_credential_store.py:62`,
`decisions/credential_store_cutover.md`). Stage 1 adds the plaintext field `integration` (Cabinet grouping) and
moves `drive_id` to the connection.

### 4.2 The credential port, re-keyed

No method takes a `provider` argument; a `kind` value reaching the port always comes from an injected `ConnectorSpec`,
never from a literal (T-6):

```
get(user_id, credential_id) -> Optional[OAuthCredential]
save(credential) -> str                       # the store chooses credential_id on first save (stage 0, D-P2 kept) and returns it
update(credential) -> bool                    # keeps the stored identity fields (stage 0 D-P2)
delete(user_id, credential_id) -> None
list_for_user(user_id, kind: Optional[str] = None) -> List[OAuthCredential]
list_keys(user_id, kind: Optional[str] = None) -> List[CredentialKey]   # plaintext fields only, never a cipher call (E28)
list_by_account(realm, subject) -> List[CredentialKey]     # across users; the revoke rule (§4.3)
list_user_ids(kind) -> List[str]                           # replaces list_users_by_provider
```

The store no longer enforces "one record per (user, kind)": cardinality is product policy, decided by
`OAuthConnectService` from `ConnectorSpec.multiple` (drives: many; Gmail, Google Tasks, To Do: one, a consent of
another account replaces the record through the revoke rule). **Call-site migration is a slice step, not a data copy:**
the ~39 get/save/update/revoke/is_connected call sites outside the port and store move to the new methods in slice 1a
(the plan lists them). Single-credential services (Gmail, Google Tasks, To Do) receive their `ConnectorSpec` by
constructor from the registry and filter by `spec.kind`: the `TASKS` integration has two kinds (Google Tasks, frozen,
and To Do), so filtering by integration would feed Google Tasks users into To Do's subscription renewal. The literals
in `task_setup_service.py:23,55` and `integration_reconnect_service.py:49` go. The OneDrive adapter finds its credential
by `handle.credential_id`. Kind literals (`gmail`, `google_tasks`, `microsoft_todo`,
`microsoft_onedrive`) live only in the registry and the adapters (T-6).

### 4.3 The revoke rule

`CredentialRevocationService.remove(user_id, credential_id)`: read the credential, delete our record, then revoke at the
provider **only if** the realm's edge declares `revocation_scope == GRANT_WIDE` **and** `list_by_account(realm, subject)`
returns nothing (no other credential of that account remains, for any user: a Google grant belongs to the client and
the account, not to our user). Microsoft edges declare `NONE` and never revoke at the provider (as today). A revoke
failure is logged at WARNING and changes nothing stored. Today a Gmail disconnect revokes unconditionally
(`src/web/user_cabinet_app.py:1235,1404`) and would kill a Google drive or Tasks grant of the same account; the
Cabinet disconnects call this service instead. (I1: a disconnect never ends a grant another stored credential uses.)

### 4.4 `DriveConnection`

Fields: `connection_id` (random, immutable), `user_id`, `account_id`, `credential_id`, `kind`, `drive_identity`
(adapter-built, §7.2), `ref_tag` (§9), `mount_name`, `indexing_enabled`, `transcribe_enabled`, `state`
(`ACTIVE` | `NEEDS_RECONNECT` | `DISCONNECTED`), `created_at`, `disconnected_at`; stage 2 adds `searched_by_default`
(default true), `write_folder_id` (None until picked, D11), `scope_folder_ids` and `scope_whole_drive` (§11.3).
Doc `{prefix}drive_connections/{user_id}:{connection_id}` (structural tenancy, as credentials). Rules:
- A reconnect keeps the id; a disconnect ends the connection (state `DISCONNECTED`, the doc and its tag stay reserved
  forever, so an old ref never resolves to a newer connection and a purge never races new keys); a later connect of
  the same account is a new connection.
- A connect whose `(kind, account)` matches an `ACTIVE` or `NEEDS_RECONNECT` connection of the user is that
  connection's reconnect: two connections of one account and kind would be one drive twice.
- `NEEDS_RECONNECT` is set by `DriveConnectionService.flag_reconnect(handle)` when the adapter raises
  `OAuthGrantRevokedError` (today the port's `flag_reconnect`); a successful consent clears it. R1's reconnect notice
  (SECRETS_AT_REST R1, `decisions/reconnect_notice_r1.md`) reads connections: an `ACTIVE` connection with no credential,
  or `NEEDS_RECONNECT`, gets the one notice; `DISCONNECTED` never does.
- The primary pointer is `DriveUserSettings.primary_connection_id`, doc `{prefix}drive_user_settings/{user_id}`, read
  fresh on every use and written only by `DriveConnectionService` (E2). Stage 1: the cut-over sets it to the one
  connection. Invariant I2: the primary is `ACTIVE` and has a write folder (stage 1: the App Folder root).

## 5. The OAuth edge

### 5.1 `OAuthProviderPort`, one implementation per realm

```
realm -> str                                             # "google", "microsoft": data, compared, never branched on
revocation_scope -> RevocationScope                      # GRANT_WIDE | NONE
authorization_url(flow: OAuthFlow, spec: ConnectorSpec, redirect_uri, *, state) -> str   # maps flow.intent to the realm's prompt params (E29)
exchange(code, flow, spec, redirect_uri) -> GrantResult  # tokens, granted scopes, ProviderAccount
revoke(credential) -> None
```

`MicrosoftOAuthProvider` (`adapters/microsoft/`) takes the Graph token exchange and `/me` read out of
`oauth_app.py:798-828,944-975`; `GoogleOAuthProvider` (`adapters/google/`) takes the token exchange, userinfo and
revoke out of `services/google_oauth_service.py`. Token **refresh** stays with the data adapters' token providers
(`graph_auth.py`, the Gmail and Tasks adapters), which already own it (E3), but `graph_auth.py` is re-keyed (§6). The App Folder root read (drive id and
localised root path) is not identity: it is the drive adapter's (`get_root(handle)`, §6), called by the drive
activation after the credential is saved.

### 5.2 One route pair, a server-side flow

- `GET /auth/connect/<kind>` starts a connect; `GET /auth/connect/<kind>?connection=<id>` (drives) or
  `?credential=<id>` (single-credential kinds) starts a reconnect. `GET /auth/connect/callback` is the one callback for
  every realm (E4). The four pairs `/auth/connect-gmail`, `-google-tasks`, `-microsoft-todo`, `-onedrive` go. The
  Cabinet sign-in (`/auth/login`, `/auth/callback`) is untouched.
- `OAuthConnectService.start` writes an `OAuthFlow` (`user_id, kind, intent CONNECT|ADD|RECONNECT, target id, expected
  subject, PKCE verifier, return path, expires_at = now + 10 min`) through the ephemeral store. The hint label (the account
  email) is used only at start to build the authorization URL and is not stored: the flow record holds no plaintext personal data. Doc key = the keyed
  digest of `state` (`SECRET_DIGEST_KEY`, the voice-ticket pattern of stage 0); the PKCE verifier is `CipherPort`
  ciphertext with AAD `oauth_flows/<digest>` (stage 0's rule: every persisted secret is encrypted or digested). PKCE
  S256 is sent to both realms.
- The callback consumes the flow with  (single use), refuses when the Cabinet session's user is not
  `flow.user_id`, when the flow expired, or when granted scopes miss `spec.required_scopes` (stage 0's
  `OAuthScopeNotGrantedError` path moves here). A reconnect whose `GrantResult.account.subject` differs from the
  expected subject is refused ("you signed in with another account"), stores nothing and revokes nothing. This replaces
  the unsigned `*_connect_user_id` cookies and the cookie-compared `state`.
- Completion saves the credential, then runs the **activation of its integration**: one callable per integration
  (not per kind) built in composition and injected into `OAuthConnectService`. The drive activation delegates to
  `DriveConnectionService` (create or reactivate the `DriveConnection`, read its root, nudge its tick), so a new drive
  kind adds no callable; To Do: today's re-subscribe and list re-read; Gmail: today's post-consent steps. No service
  imports another (REQ-ARCH-22).
- Consent parameters are the edge's: it maps `flow.intent` and the hint label to its realm's parameters (Google merges
  `prompt=consent select_account`, since it needs `consent` for a refresh token, `google_oauth_service.py:83`). Services
  hold no OAuth parameter name. `CONNECT` maps to today's parameters per realm (no visible change, D15). Stage 2 uses
  `ADD` (the account chooser) for "Add a drive" and sends the hint on every `RECONNECT`.

### 5.3 TD-V6 closes; REQ-ARCH-18 reaches `src/web`

HTTP leaves `services/` and the OAuth part of `web/`; `HTTP_CLIENT_WHITELIST_FILES` (`tests/unit/arch_tech_debt.py:96-98`)
loses `google_oauth_service.py` and TD-V6 is closed. REQ-ARCH-18 (`tests/unit/test_req_arch_01_hexagonal_isolation.py:700-704`)
adds `src/web`. One more web file imports an HTTP client: `src/web/worker_oidc_verifier.py` (the `/worker` gate's
certificate fetch). It is out of this RFC's scope (a security gate with a history of breaking callers); it becomes
its own tech-debt entry in `arch_tech_debt.py`, named and dated, so the extended scan passes today (E5).

## 6. The drive port by handle; registry and router

Every `UserDrivePort` method takes `handle: DriveHandle` in place of `user_id`; the rest of each signature is unchanged
except:
- `get_thumbnail(handle, item_id, max_edge_px: int)` (was `size: str`);
- `media_source(handle, item_id) -> MediaSource(url, headers, expires_at)` replaces `download_url` (url and headers are
  secret, never logged). The parser sandbox, which can reach the network, receives a `MediaSource` only when its
  `headers` are empty (a pre-signed URL, as Graph's); otherwise the indexer streams the bytes into the sandbox's work
  directory with `download_to_file`. No provider token ever enters the sandbox (`decisions/drive_parser_sandbox_resource_isolation.md`);
- `capabilities(handle) -> DriveCapabilities` (async, the ports convention);
- `forget(handle)` drops the adapter's caches, locks and token cache of a connection after a disconnect;
- `display_name`, `is_connected`, `disconnect`, `connection`, `flag_reconnect` leave the port (labels to the registry,
  state to `DriveConnectionService`).

`DriveHandle` is slim on purpose: the adapter cannot read mount name, policy or tag, so the inward direction of the
hexagon is enforced by the type. Adapters key caches, locks and token caches by `(user_id, credential_id)` or
`connection_id`, never by `user_id` alone (T-3: two handles of one adapter share no cache). The hidden one today is
`GraphTokenProvider` (`src/adapters/microsoft/graph_auth.py:60-102`): its `_cache`, `_locks`, `invalidate` and
`cached_token` key by `user_id` and it reads `get_credentials(user_id, self._provider)`, so a second OneDrive handle
would get the first account's token. It is re-keyed to `(user_id, credential_id)` (`headers(user_id, credential_id)`),
still shared with To Do, and T-3's no-shared-cache case runs through it.

**`ConnectorRegistry`** (composition) holds, per kind, the `ConnectorSpec`, the drive adapter (drive kinds) and the
realm's `OAuthProviderPort`. It is the only place that names a provider. **`DriveProviderRouter(UserDrivePort)`**
(composition) holds `{kind: UserDrivePort}` and forwards each call by `handle.kind`; every method is one dispatch
call (T-8). Services keep their one `UserDrivePort` dependency (`services/user_drive_service.py:66-73`); only
`user_id → handle` changes, resolved from the `DriveConnection` (E6). Adapters are injected into the router, never
imported by it (REQ-ARCH-23 holds).

Callers get a handle from the connection: by ref (§9) for an item, by the primary for a path in stage 1, by the mount
for a path in stage 2. The ~20 files naming `UserDrivePort` change their calls; `DriveOperations` stays one instance per
user, and its mutation lock stays per user on purpose: it serialises writes across the user's connections, which is
cheap at a handful of drives and keeps one ordering for receipts.

## 7. Capabilities and neutral shapes

### 7.1 Capabilities are product effects, and arrive with their reader

A field exists only when a service reads it and a contract case covers each value (T-7); the contract is otherwise
uniform, with adapter emulation wherever cheap (unique child names, recoverable delete). Per handle, because a grant
decides some of them.

| Field | Reader | OneDrive App Folder | Stage |
|---|---|---|---|
| `max_path_chars` | `UserDriveService` path check (today's 400 from `area_path`, `user_drive_port.py:82-84`) | 400 | 1 |
| `writable` | access policy (§11.3) | True | 2 |
| `feed_scope` (`AREA`: one cursor covers an area with a root; `ACCOUNT`: one cursor covers the account, no area root) | the planner's ancestry filter and the default scope (§11.3) | `AREA` | 2 |

There is no `native_documents` field. A provider's native documents (Google Docs, Sheets, Slides) are the adapter's
emulation (stage 3): its `download` returns an export in a format the parser admits, the item's `ItemAccess` has
`can_write_content=False` (so append and replace are refused by the policy that exists), and an item whose export would
exceed the existing download cap is reported `can_download=False`, which the existing "cannot open" path handles.
Domain and services see an ordinary file.

### 7.2 Neutral shapes

- **Item identity.** `DriveItem.identity` and `DriveItem.drive_identity` are adapter-built strings, unique across
  providers and opaque to domain: `{id_space}:{…}`, where `id_space` is declared by the registry entry (E7). OneDrive's
  `id_space` is `microsoft_onedrive`, its item identity `microsoft_onedrive:{driveId}:{itemId}` and drive identity
  `microsoft_onedrive:{driveId}`. A note's binding key is `i:` + identity, which is byte-identical to today's
  `i:microsoft_onedrive:{driveId}:{itemId}` (`drive_note.py:461-463`): no note re-binding. A file shared to two Google
  accounts gets one identity from the Google adapter, so one note (stage 3's concern, made possible here).
- **Content.** `DriveItem.ctag` becomes `content_version`. Hashes leave the facet mapping for
  `content_identity: Optional[ContentIdentity(family, digest, size)]`, rendered `{family}:{digest}:{size}`; the family
  comes from the adapter (OneDrive `quickxor`). No family is implied by absence (D12).
- **Facets.** Raw Graph `facets` become typed `DriveMediaFacts` (photo, image, audio, video, location, file kind,
  package, malware flag, `alias_of`), filled by the adapter. `alias_of` is stage 1, not 2: the admission rule
  `Reason.REMOTE_ITEM` reads `"remoteItem" in change.facets` today (`drive_index_policy.py:167`) and must keep working
  after the rebuild (OneDrive `remoteItem`; later Google shortcuts). The readers (`drive_card`, `drive_capture`, `drive_index_policy`,
  `drive_index_planner`, `drive_enrich_plan`, `drive_index_run`, `drive_search`) read typed fields. The rebuild (D16)
  means no stored record needs a transform.
- **Media and thumbnails.** `MediaSource` (url, headers, expiry) and `max_edge_px`, as in §6.
- **Constants.** `ONEDRIVE_PROVIDER_KEY` becomes the registry's `id_space`; `CONTENT_KEY_FAMILY` becomes
  `ContentIdentity.family`; `AUDIT_AREA="@app-folder"` becomes "the mirror root's parent" (data from the mirror root).
- Stage 2 adds `DriveItem.access: ItemAccess(can_download, can_write_content, can_rename_move, can_delete,
  can_add_children)`, all true for the App Folder except items the bot cannot open (USER_DRIVE_INDEXING §26.2).

## 8. Connection scope: keys, ticks, fan-out, money

- `ConnectionScope(user_id, connection_id)` feeds every connection-scoped key. Doc ids are `{connection_id}:…` with
  `user_id` a stored field (connection ids are random and globally unique; E8). Connection-scoped: sync state and lease
  (`{connection_id}`), index records, chunks, hints, transcript jobs and batches, tick / continuation / nudge / toggle
  task keys, the `drive_sync` payload (`{user_id, connection_id}`; a payload without `connection_id` is refused). A read
  whose stored `user_id` differs from the scope's is treated as absent and logged at ERROR (I3).
- User-scoped by design (each listed with its reason in T-5): monthly model usage `{user}:{YYYY-MM}` (D8, shared per
  user); notes (`i:` by identity, `r:` by tag); the transcript store (by content key, §10); mirror roots and ledger (by
  drive identity, §10); the initial-build record (by account, §10).
- **Ticks and fan-out per connection.** `DriveSyncSchedulerService.fan_out` lists `ACTIVE` connections with indexing on
  (from `DriveConnectionRepository`), not users; each gets its tick task, staleness alert and cap alerts. The queue
  stays `max-concurrent-dispatches=1` and the indexer concurrency 1, so a user's connections tick in turn.
  `DriveIndexingService.tick(scope)` loads the connection and its handle. Step-1 mutation nudges carry the connection.
- **Transcription per connection.** The T1 stage runs per connection (its jobs and batches are connection-scoped); the
  monthly `transcribe` cap stays per user.
- **Money written by several connections (I4).** The monthly spend increment and the transcribe reservation are
  Firestore transactions on the user's month doc, so two connections' ticks cannot lose an update (T-9).
- **Queries and indexes.** Every query of the four index collections filters `connection_id` where it filtered
  `user_id`; vector search keeps an equality prefilter, now on `connection_id`. Stage 2 searches run one query per
  searched connection and merge in the service (E9). The new composite and vector indexes must be `READY` before the
  stage-1 deploy (the plan lists them); the `user_id` forms are deleted after acceptance.

## 9. Tagged refs

- **Form.** `drive:<tag>:<item_id>`. The tag is 2 to 6 characters `[a-z0-9]`, starting with a letter, random, unique
  per user, immutable, never reused (DISCONNECTED connections keep theirs). Example: `drive:a1:item-1`. A mount rename
  breaks no ref. This is the drive row of EXTERNAL_FILES_RFC §4.1 (`drive:<item id>` there becomes
  `drive:<tag>:<item id>`, label `[Drive: <path> (size) ref=drive:<tag>:<id>]`).
- **Codec.** `src/domain/drive_ref.py`: `format(tag, item_id)`, `parse(text) -> DriveRef(tag, item_id) | UntaggedRef(raw)`,
  `is_drive_ref(text)`. Parsing splits on the **first** `:` after `drive:`, so an item id may contain `:`; a part before
  it that is not `[a-z][a-z0-9]{1,5}` makes the ref untagged. It replaces `make_drive_ref` / `parse_drive_ref` /
  `is_drive_ref` (`user_drive.py:181-194`; ~25 parse callers, ~7 format callers) and the other prefix tests
  (`domain/workspace.py:73`, `services/workspace_service.py:449`, `agents/file_management_agent.py:101,107`,
  `infrastructure/drive_tools.py:394`). When P9's classifier (`domain/file_ref.py`) is built, its drive branch calls
  this codec; this RFC does not build P9 (E10).
- **Resolution** is in the service: `DriveConnectionService.resolve_ref(user_id, ref)` returns the connection and item
  id, or a code-built line. Untagged, or a tag the user never had: `Ref <ref> is from before the drives were
  reorganised; search for the file again.` A `DISCONNECTED` connection's tag: `Ref <ref> belongs to a drive that is no
  longer connected.` No legacy resolver, no history rewrite (D13): a stored `drive_context` entry (`domain/drive_history.py`)
  with an untagged ref answers the first line once, and the model's next search produces tagged refs.
- `FileConversionService` (`file_conversion_service.py:57-66,109-150`) resolves the tag to a handle before
  `get_item` / `download`, through `DriveConnectionService` injected by constructor.
- Notes' ref key becomes `r:{tag}:{item_id}` (`drive_note.py:466-468`); existing notes gain it by the cut-over (§12).
- Any prompt text that shows a ref example moves to the tagged form in the same deploy.

## 10. Keys that survive the rebuild

Everything the indexer re-derives is purged (§12). What survives, and whether its key changes:

| Store | Key today | Key after | Change and why |
|---|---|---|---|
| Stored transcripts (paid) | `safe_doc_id({user}:{content_key})`, content key `<quickXorHash>:<size>` or `item:{itemId}:{cTag}` (`drive_transcript.py:250`, `drive_transcript_windows.py:61-67`) | `safe_doc_id({user}:{family}:{digest}:{size})`, fallback `safe_doc_id({user}:item:{identity}:{content_version})` | **Re-keyed.** An unprefixed hash meaning quickxor is the special case D12 forbids; a fallback by bare item id is ambiguous across connections and providers, and by identity it survives a disconnect and reconnect without paying again. The copy takes the head **and every document of its `parts` subcollection** (the body of a long transcript, `firestore_drive_index_repo.py:1593`; parts keep their `user_id` field), rewrites the raw `content_key` field and sets `last_used_at` to the cut-over time. |
| Mirror ledger | `{user}:{kind}:{source_item_id}`, reads filter `drive_id`; archive `{user}:archive:{sha256(drive_id/day/path)[:32]}` (`drive_mirror.py:146-154`) | `{user}:{kind}:{sha256(mirror drive_identity + "/" + source identity)[:32]}`; archive `{user}:archive:{sha256(drive_identity/day/path)[:32]}`; fields `drive_identity`, `source_identity`; reads filter `drive_identity`; `content_key` fields and pins in the new form | **Re-keyed.** Two connections can see one source item (a shared file on two accounts of one provider), each with its own mirror (D7): the key must carry the mirror's drive. Hashing keeps the id short. |
| Mirror roots | `{user}:{drive_id}` (`firestore_drive_mirror_repo.py:99-100`) | `{user}:{drive_identity}` | **Re-keyed.** Bare drive ids rely on provider id spaces being disjoint (not verified); the identity is unambiguous. Keyed by drive, not connection: the folder outlives a connection and a reconnect must recognise it. |
| Owner notes | `match_keys` `i:microsoft_onedrive:{driveId}:{itemId}`, `r:{item_id}` | `i:` unchanged (§7.2); `r:{tag}:{item_id}` | **`r:` key added in place** (old one removed at cleanup). |
| Monthly model usage | `{user}:{YYYY-MM}` (`drive_usage.py:33-34`) | unchanged | Per user by D8. |
| Initial-build record | `{user}:initial` (`drive_usage.py:37-38`) | `safe_doc_id({user}:initial:{realm}:{subject})` | **Re-keyed, state carried.** D8: one build per connection, none for a reconnect of the same account; keyed by the account, a reconnect (a new connection of the same account) finds the same record. The cut-over copies the record with its spent and closed state to the user's one OneDrive account, so it opens no new budget. |

The copy steps live in the cut-over script (§12): dry run by default, idempotent (a re-run writes nothing new), counts
per collection, copy (old documents kept until `--cleanup`, so a rollback finds them).

**I5 (mirror after a rebuild).** After a purge with keep and a rebuild, the mirror moves or archives no file whose
recording still exists and whose stored transcript exists. Today's mirror code holds it, given I6: the purge never
writes the mirror collections, a loss mark clears when the record is present, only `current` recordings are due, and
the scan reads an item before archiving it. The acceptance test runs the cut-over sequence on the in-memory world.

**I6 (keys are the runtime's).** Every key the script writes is byte-identical to the key the new code computes at run
time for the same item, drive and account. The script imports the product's key functions and the OneDrive adapter's
identity functions and copies no formula. Its preflight refuses unless each mirror root's `drive_id` and the drive part
of every note's `i:` key equal the credential's `drive_id`; its dry run prints the computed initial-build key with that
record's spent and closed state. A mismatch would, at run time, create a second `_Transcripts` root and rewrite every
transcript into it, unbind notes from their files, or open a fresh $40 build against D8. Test T-14.

**I7 (no orphan sweep before re-attachment).** The daily orphan sweep of stored transcripts
(`drive_transcript_stage.py:1480-1500`, `TRANSCRIPT_ORPHAN_TTL` 30 days, `drive_index_params.py:241`) keeps a head only
when a record carries its key or the ledger pins it. After a purge no record carries any key until the transcript stage
re-attaches the stored transcripts (`drive_transcript_stage.py:421-429`), and the sweep runs at the start of the first
work-off. So: each sync state gets `transcripts_reattached_at`, set when the transcript stage finishes its first pass
over the connection's recordings after the state was created; the user's sweep runs only when every `ACTIVE`
connection of the user has it set. A connection with transcription off never re-attaches, so its user is not swept
(the store keeps more, never pays twice). The cut-over's `last_used_at` stamp is the second guard (E22).

**I8 (paid transcripts outlive a disconnect, D9).** Before a disconnect purges a connection, every stored transcript
head that one of its records carries gets `retained_at` (and `retained_reason: disconnect`); the sweep never deletes a
retained head. A later connection that reads the same content (same key) re-attaches it without paying. Without this,
D9 would hold only for the sweep's 30 days (E23).

## 11. Stage 2: the product core for several connections

### 11.1 The mount tree

- Every connection has a `mount_name`: the user's own name for the drive (§11.8 says who sets it, the rules and what a
  rename does). Provider names never reach the model; mount names do (E11).
- **Canonical paths** are `<mount>/<path>` and every answer prints them, also with one connection (E12; D15 put this in
  stage 2). Example: `Springfield/birth-certificate.pdf`, `Acme/R&D/Tom's plan.docx`.
- **Resolution.** A path whose first segment is a mount name resolves in that mount. Any other path resolves in the
  primary (every stored history path and the model's habit, `domain/drive_history.py:59-88`). A primary folder named
  like another mount is shadowed by the mount; the canonical path disambiguates. Pure, in `domain/drive_mounts.py`.
- Listing `/` lists the mounts, one line each: mount name, primary or not, write folder or read-only, searched by
  default or not.
- Names the bot writes follow one portable character set, a product rule held in domain; every adapter must accept
  every name in that set, escaping on its side if its provider forbids more (a T-3 contract case), so a new provider
  never edits domain. Names it reads are escaped for display and resolution (no capability; the contract is uniform).

### 11.2 Tools and prompts

- `search_drive(query, drives=[])` and `find_passages(…, drives=[])`: `drives` empty = every connection with
  `searched_by_default`; `read_passages` and the open tools take no `drives` (their ref names the drive); a named mount is searched even when it is not searched by default; an unknown name is refused
  with the list of mounts. The coverage line names the drives not searched.
- `list_folder`, `create_folder`, `save_text_file`, `copy_file_to_drive`, `move_file`: canonical or primary-relative
  paths. An empty destination folder means the Inbox in the primary's write folder (D7).
- No `connection_id` parameter anywhere: the ref carries the source, the path the destination (CRUD-shaped; an empty
  field never destroys).
- The specialist's preflight block (`agents/drive_agent.py:109`) gets one line per connection. `drive_context` entries
  keep their shape with canonical paths and tagged refs.
- `PROTOCOL_DRIVE_SEARCH`, `COGNITIVE_PROCESS_DRIVE` and the reading lines gain the mount rule, `drives` and "also on".
  Before/after G3 per the drive eval process (`main` with the Firestore prompts vs the branch with
  `--prompts-from-snapshot`, same `--repeats`).

### 11.3 Scope and access policy

**Scope** is what a connection indexes and reads (D3): `scope_folder_ids` (selected folders) or `scope_whole_drive`.
The default comes from the adapter's `feed_scope`: `AREA` → the area root (an App Folder, the migrated connection: today);
`ACCOUNT` → empty, so nothing is indexed or read until the owner picks folders or "whole drive" in the Cabinet (the
read side of D11). On an `ACCOUNT` feed (one cursor for the whole account) the planner keeps a change only if its
parent chain reaches a scope folder: it resolves unseen parents with `get_item`, memoised per tick, and treats a folder
moving into or out of scope as the existing folder cascade. Items the adapter places outside any parent chain (shared
items with no reachable parent) are in scope only under "whole drive" (D2: the adapter decides where they enter the
tree). This is stage 2 and provider-neutral; T-4's `fake_b` exercises it.

`drive_access_policy.decide(op, policy, capabilities, item_access, ancestry)` is pure and called by `UserDriveService`
before every provider call; the model is told the policy (preflight lines) but cannot widen it.

| Operation | Rule (every connection) |
|---|---|
| read, list, open, search | inside the scope (selected folders' subtrees, or the whole drive); outside: `This folder is not among the folders indexed for <drive>; the owner can add it in the Cabinet.` |
| create, save, append, replace, rename, move within the drive | source and destination inside the write folder's subtree; with no write folder: `This drive is read-only until a write folder is chosen in the Cabinet.` |
| delete | recoverable delete with receipt, inside the write folder's subtree |
| anything the item's `access` forbids | refused before the call |
| a move or copy whose source and destination are on different connections | `Moving or copying a file between drives is not available yet.` (D6) |

The write folder must lie inside the scope. The migrated connection's write folder is the area root (today's
behaviour). The area root, every scope root and the write folder root are protected like the root today
(`_refuse_root`, `user_drive_service.py:238-277`). Chat writes into a connection's `_Transcripts` stay refused.
Ancestry comes from the port's parent chain, memoised per execution.

### 11.4 Search across connections and "also on"

One query per searched connection, merged into one result list (E9). Results with the same `ContentIdentity` on several
connections collapse into one: the primary's copy, else the first mount in name order, plus `also on: <canonical
path>, …` (D5). Identities of different families never match (quickxor vs md5); the duplicate shows twice, accepted
with D9's double transcription.

### 11.5 Bot-owned artefacts

- **Inbox** (`DEFAULT_INBOX_FOLDER`) and **deep-research copies** (`services/deep_research_drive_copy_service.py:60-108`):
  under the primary's write folder (D7). With no primary they are refused with a line, never written elsewhere.
- **Transcript mirror:** a connection's `_Transcripts` sits under its write folder root (App Folder migrated: the area
  root, unchanged). A connection with no write folder gets no mirror; its recordings are still transcribed if its
  `transcribe_enabled` is on, and found by search (D7).

### 11.6 Notes

Binding by identity (`i:`), lookup by tag (`r:{tag}:{item}`); `NOTES_PER_USER_MAX` stays per user. A disconnect archives
the connection's notes through the confirmed-gone path, found by `search_deleted_recordings_and_notes`. A later
connection of the same drive does not revive them in v1 (E13).

### 11.7 The Cabinet connection list

`GET /api/drive/connections` lists connections (mount name, provider label and account label for the owner's eyes,
state, primary, write folder, searched by default, indexing, transcription for the owner). Actions, each through
`DriveConnectionService`:
- **Add a drive** → a kind picker rendered from `GET /api/connectors` (the registry's drive kinds and their labels; no
  kind is written in `src/web/static/`), a name field prefilled with the default (§11.8), then `/auth/connect/<kind>`
  with intent `ADD`; the new connection is read-only, indexing off (indexing is opt-in, the switch is the consent,
  today's rule), searched by default, scope by its `feed_scope` default (§11.3).
- **Rename** (§11.8); **choose folders to index, or "whole drive"** (a folder picker over `list_children`; changing it
  starts the scope cascade, and narrowing it purges what left the scope); **choose write folder** (the same picker,
  inside the scope; required before it can be primary),
  **searched by default**, **indexing** (`PUT` per connection; replaces `PUT /api/drive/indexing`).
- **Set primary**: only an `ACTIVE` connection with a write folder (I2), one transactional write of the pointer.
- **Disconnect**: the connection's purge (the existing purge, connection-scoped, keeping stored transcripts, D9),
  stored transcripts it uses retained first (I8), notes archived, mirror roots and ledger kept (they describe files on the drive), credential removed by the revoke
  rule, `forget(handle)`, state `DISCONNECTED`. Disconnecting the primary asks for a new primary among connections with
  a write folder; with none, the user has no primary (§11.5).
- **Reconnect** for `NEEDS_RECONNECT` (intent `RECONNECT`, the stored label as hint).
- The initial build: a connection's first build draws on its account's initial-build record (§10): a new account
  opens $40, a reconnected account does not (D8). The record is per account, so two kinds of one account (one realm,
  one subject) share it, as D8's "same provider account" reads.

### 11.8 The user's name for a drive

Owner requirement and decision (2026-10-30): the user names each drive, **in the Cabinet only** (§11.9). The
name **is** the mount name: what paths print and what the model and the tools use. It is distinct from the ref tag,
which code generates, the user never sees as a name, and which never changes, so a rename breaks no ref (§9).

**Who sets it.** Only the Cabinet: the name field of the connection list, and of "Add a drive" (prefilled with the
default). It calls `DriveConnectionService.rename(user_id, connection_id, name)`, which validates, writes in a Firestore
transaction over the user's connection docs (uniqueness cannot race; a user has a handful) and appends `{name, at}` to
the connection's `name_history`. No model, tool or background run can rename a drive. The name lives on the connection,
never in memory.

**Validation** (`domain/drive_mounts.validate_mount_name`; each failure is a Cabinet message naming it):
- NFC, trimmed, inner runs of spaces collapsed; 1 to 40 characters;
- the portable name set of §11.1: no `/ \ : * ? " < > |`, no control characters, no leading or trailing `.`;
- unique per user, case-insensitively (casefold), among connections that are not `DISCONNECTED` and among other
  connections' former names still held (below); a `DISCONNECTED` connection's name is free (its tag is not);
- reserved: `_Transcripts`, `Archive`, `Long paths`, `Inbox` (bot-owned folder names, so a path never reads as both)
  and a single `.` or `..`;
- a name equal to a top-level folder of the primary is accepted with a warning: `The folder Acme in Drive will be
  reached as Drive/Acme.` (the shadowing rule of §11.1).

**Default before the user names it:** `Drive`, then `Drive 2`, `Drive 3`, … (the lowest free number), never the
provider name or the account label (E11). The migrated connection is `Drive`.

**How a phrase reaches a connection** ("on my work drive"). The model resolves it, code verifies it:
- the specialist's preflight lists every connection: name, primary, write folder or read-only, searched by default,
  and the drive's owner note if there is one (§11.10); that is what lets "my work drive" match `Acme` when the note says
  "Acme work files";
- every tool argument that names a drive (`drives`, the first segment of a path, `whole_drive_name` of the note tools) must equal
  a current name (casefold) or a held former name; anything else is refused with the list of names. Code does no fuzzy
  matching, so a guess never silently picks the wrong drive;
- when the phrase fits two drives or none, the specialist asks the user which one (its ordinary "What to do" line).

**What a rename does to what was already printed.**
- Refs in history keep working: they carry the tag.
- Paths in history start with the old name. The connection keeps its **former names for 30 days**: a path or a
  `drives` value with a held former name resolves to that connection and the answer adds `Acme is now called Acme Hamburg.`;
  printed paths use the new name. While held, no other connection can take the name. After 30 days the name is free and
  a path with it falls back to the primary's resolution (and normally finds nothing; the model searches again).
- Search narrowing by name follows the same rule.

**Rejected:** a separate alias list per connection (more state, collisions between aliases and names, and the owner note
covers the descriptive phrases); a fuzzy matcher in code (a wrong guess on a write lands on the wrong drive); letting
the tag be user-chosen (a rename would break refs); forgetting the old name at once (every path in recent history
would silently resolve in the primary).

### 11.9 Settings live in the Cabinet

Owner decision (2026-10-30): **every per-connection setting is set in the Cabinet only**: name, indexed folders or
whole drive, searched by default, write folder, primary, indexing, transcription (still the owner's script for
transcription on), connect and disconnect. There is no settings tool in chat. The bot gets one line of knowledge, in
Smart's drive block of `PROTOCOL_AGENT_SELECTION`, in the drive specialist's protocol token and in
`src/utils/capabilities.py`: *to configure a drive (name, folders, search default, write folder, transcription,
connect or disconnect), send the user to the Cabinet* (with the Cabinet link). A commission that asks the specialist to
change a setting gets that line back, done nothing. Reading the settings stays in chat: the preflight and the `/`
listing show them (§11.1, §11.2).

**Why** (the owner's call; it also settles the risk): a chat path is reachable by any text the model reads, so a
prompt-injected document could otherwise widen writes, start paid indexing or purge an index; the Cabinet click is the
owner's own act and needs no confirmation code.

### 11.10 The owner's note on a drive

Owner decision (2026-10-30): in chat the bot keeps **only notes** about a drive. The owner's note describes a whole
drive (connection), as notes describe files and folders today (DRIVE_FILE_NOTES_RFC), e.g. "Acme work files:
R&D, Tom's plan". It is written in chat or on a call through the drive specialist, exactly like file notes;
everything else about a drive is the Cabinet's (§11.9). **Design:** the same notes store, SCD2 versions, receipts, undo
and erase, with a **connection binding** `c:{connection_id}` in `match_keys` in place of the item binding `i:` (no `r:`
key: a drive note is addressed by the drive's name, resolved to the connection at call time).
- **Tools.** The existing note tools take exactly one target: `file_ref` (as today, for a file or a folder) or the new
  `whole_drive_name` (a current or held former drive name, §11.8); both or neither are refused. Its own parameter
  description reads: "Only for a note about the entire drive. For a file or a folder, even a top-level one, pass
  file_ref." So "note that Acme/R&D is Tom's" cannot land on the whole drive by filling a drive name. Same owner-turn gating (`is_owner_turn`, declared and
  re-checked per call), same per-run cap of writes, counted in `NOTES_PER_USER_MAX`, same `DRIVE_NOTES_ENABLED` switch.
  The receipt names the drive: `Note on drive Acme saved (v1): "Acme work files: R&D, Tom's plan"`.
- **A rename** changes nothing in the note (the binding is the connection id); receipts and the preflight print the
  current name.
- **Where it shows.** In the specialist's preflight line of that drive (bounded to its first 200 characters, sent as
  the owner's words, never as an instruction), in the `/` listing, and in the Cabinet connection list. It is not merged
  into any card and not searched: the indexer's notes step skips `c:` notes (a test pins it).
- **Disconnect** archives it with the connection's other notes: found by `search_deleted_recordings_and_notes`, labelled
  with the drive's last name. **Reconnect** of a `NEEDS_RECONNECT` connection keeps it (same connection id). A new
  connection of the same drive does not revive it (E13).

**Rejected:** binding to the drive's root item (`i:` of the root): the root outlives the connection and is shared by
two connections of the same drive, a full-drive connection has no single root (My Drive plus shared drives), and the
root is protected and has no card; binding by `drive_identity`: the same mismatch (a note says what *this* connection
is, and two connections can cover one drive); a plain `description` field on the connection doc: it would lose
versions, undo, erase and receipts that notes already have.

## 12. Cut-over on live data (stage 1)

Slice 1b ships with a one-off cut-over; slice 1a and stage 2 need none (new fields default; their deploys are ordinary).

**Slice 1a (owner, before its deploy):** register the new callback `<service URL>/auth/connect/callback` in the Google
and Azure consoles (D14). The old URIs stay registered until stage 1 is accepted, so either revision can be rolled back
to. The 1a deploy changes no stored credential except the `integration` field the old code ignores.

**Slice 1b (owner, before its deploy):**
- create the new composite and vector indexes and wait for `READY`;
- **drain transcription with it on.** With a user's transcription flag off no transcript step runs (poll, collect,
  finalize and extract are all gated, `drive_indexing_service.py:1754-1775`, `:4603-4607`), so an in-flight job and its
  batch would freeze, and a purge would book the submitted batch as spend and drop it (`:4424-4432`). So: run the
  cut-over dry run with transcription **on** until it shows no job in `working` or `waiting` and no open reservation
  (starts happen only on new recordings or at a month turn); then switch transcription off and confirm with a second
  dry run. If a job started in between, switch it back on and wait again.

**Order:**
1. **Pause** `drive-sync-dev` and the Cloud Scheduler jobs `start_drive_sync` and `reconnect_notice_sweep` (the old main
   service keeps enqueueing ticks, nudges and toggles, and between the deploy and step 3a the new one would see users
   with no `DriveConnection`); **purge the queue's tasks** (old payloads carry no `connection_id` and would be refused
   and retried).
2. **Deploy** the main service and the indexer from the same revision.
3. **Cut-over script, `--apply`** (`multi_drive_cutover.py` in `scripts/infrastructure/`). Its preflight refuses unless:
   the queue is paused and empty (if tasks arrived, purge again and re-run), the two Scheduler jobs are paused, no
   transcript job is in flight and no reservation is open, every old sync state's lease is free, **no connection has a
   sync state yet** (so a re-run after the resume can never purge the rebuilt index), the new indexes are `READY`, and
   the I6 equality checks pass. The dry run prints the counts per collection and, per user, the computed initial-build
   key with that record's spent and closed state.
   a. per user with an OneDrive credential: create the `DriveConnection` (new id and tag, name `Drive`, kind, credential,
      `drive_identity` from the OneDrive adapter's identity function, the two flags copied from `UserBotConfig`) and the
      primary pointer;
   b. **purge with keep** of the old per-user index. First write `purging=True, purge_keep_transcripts=True` on the old
      sync state under its lease (the trigger today's rebuild path writes), so an old revision, if rolled back to before
      the step completes, finishes the purge itself. Then, with the rules of today's purge
      (`drive_indexing_service.py:4329-4445`): settle open reservations (the preflight expects none); delete transcript
      jobs and batches, hints, chunks and records whose doc ids carry the old `{user_id}:` prefix (never a
      connection-scoped id), then the sync state doc `{user_id}`. The transcript store and its pins, notes, mirror roots
      and ledger, and usage docs are not touched. The old key shapes are known only to this script (E14);
   c. the copies of §10: transcript heads with all their parts and `last_used_at` = now, ledger, mirror roots,
      initial-build record; notes gain `r:{tag}:{item}`;
   d. print counts per collection.
4. **Resume** the queue and the two Scheduler jobs. The fan-out finds each `ACTIVE` connection with indexing on and no
   sync state, creates a fresh one and the indexer rebuilds: one full enumeration, then content and enrichment under the
   monthly caps. The orphan sweep stays off for the user until re-attachment (I7).
5. **Transcription back on** per connection (the owner's flag script) after the connection's first complete round. The
   transcript stage then re-attaches every stored transcript by content key, paying nothing, and sets
   `transcripts_reattached_at`. Until this step, opening a recording returns no transcript.
6. **Owner checks**, after that first transcript pass plus `MIRROR_ARCHIVE_GRACE`: `drive_mirror_status.py` (no archive
   moves, no new `_Transcripts` root: I5, I6), no paid window for a recording that had a stored transcript, the index
   growing, one chat search, a drive file note read, a To Do and a Gmail action.

**Accepted visible effects (D15):** the rebuild (search answers from a growing index, with its coverage line), the
"search again" line for refs older than the cut-over, and transcripts missing until step 5.

**Rollback** (queue and Scheduler jobs paused again first):
- **Before step 5:** redeploy the previous revision and run `--purge-new`, which deletes only the connection-scoped
  documents of the four index collections, the connections' sync states and the connection documents; it never touches
  the transcript store, ledger, mirror roots, notes or usage. The old revision finds its sync state flagged `purging`
  (or gone) and rebuilds under the old keys, which the copies never deleted (the credentials kept `drive_id`,
  `UserBotConfig` kept the flags, the old redirect URIs are still registered). If the initial build was still open at
  the cut-over, the old record lacks the spend made since (the dry run line shows it), so that part can be spent twice.
- **After step 5:** first `--copy-back`: transcript heads with their parts and ledger documents written or changed
  under the new keys since the cut-over are copied to the old keys (with the old revision's own key functions), so the
  old revision neither pays for them again nor meets unknown files in `_Transcripts`. Then as above.

**After acceptance:** `--cleanup` deletes the old-key heads together with their parts, the old ledger, root and
initial-build documents, the `r:{item}` note keys, the credentials' `drive_id`, the `UserBotConfig` flags and the
`user_id` index forms; the owner removes the old redirect URIs. `drive_index_rebuild.py`, `drive_transcribe_flag.py` and
`drive_initial_build_reset.py` take a connection (or an account) from stage 1 on.

## 13. Architecture and contract tests

New requirement ids continue the series (the last is REQ-ARCH-32). The plan names the files.

| # | Test | Asserts |
|---|---|---|
| T-1 | REQ-ARCH-12/13 extended | no string constant matching `onedrive|graph|microsoft|google|gdrive|approot|quickxor|ctag|takenDateTime` in `src/domain`, `services`, `agents`, `infrastructure`, `web`, nor in the drive prompt tokens of `prompts_snapshot/`, nor (a text scan) in `src/web/static/`; the registry and adapters are exempt. |
| T-2 | REQ-ARCH-18 extended to `src/web`; REQ-ARCH-17 | no HTTP client import there; `HTTP_CLIENT_WHITELIST_FILES` empty; the OIDC verifier is its own named debt entry (E5). |
| T-3 | `tests/contracts/user_drive_contract.py` | one suite run against the OneDrive adapter (aiohttp boundary fake), `FixtureDrive` and `FakeDriveAdapter`: conflict modes, 404 is not deletion, resync, oldest-match `child_by_name`, recoverable delete, typed facets only (`alias_of` included), adapter-built identities in their `id_space`, every name of the portable set accepted, a `MediaSource` for the sandbox has no headers or the bytes are streamed, two handles of one adapter share no cache (through `graph_auth` for OneDrive). A sibling suite for `OAuthProviderPort` (authorization URL carries PKCE and state, exchange returns an account, `revocation_scope` honoured). |
| T-4 | **Fake provider twice** (acceptance, `build_application_graph` over in-memory fakes) | Stage 1: `FakeDriveAdapter` registered as `fake_a` (two connections, two accounts) and `fake_b` (one), the same item id under two connections on purpose: connect through a fake edge, save, open by tagged ref, search per connection, a note on each, a tick per connection, disconnect + purge of one. Every drive doc id carries its connection; refs round-trip; the purge leaves the others intact; revoke fires only for the last credential of an account. Stage 2: `fake_b` becomes an account-wide provider (`feed_scope=ACCOUNT`, no area root, some items outside any reachable parent): a new connection indexes nothing until folders are chosen; with two folders chosen, only their subtrees are indexed and readable; a folder moved out of scope leaves the index; "whole drive" admits the parentless items; writes stay inside the write folder. No service, agent, prompt, Cabinet file or composition rule changed to add `fake_b` beyond its registry entry. |
| T-5 | AST over `src/domain/drive_*` | every `*_key` / `*_doc_id` takes a `ConnectionScope`, or is in a listed user-scoped set with its reason (usage month, initial build by account, notes, transcript store, mirror roots and ledger). |
| T-6 | Credential port shape | no `OAuthCredentialsPort` method has a `provider` parameter; kind literals appear only in `composition/connector_registry.py` and adapters (AST over `src/`, text scan of `src/web/static/`); single-credential services get their kind from an injected `ConnectorSpec`. |
| T-7 | Capabilities | every `DriveCapabilities` field has a service reader and a contract case per value. |
| T-8 | Router | every `DriveProviderRouter` method body is a single dispatch call. |
| T-9 | Money parity | two connections' ticks under `asyncio.gather` lose no monthly-spend increment or transcribe reservation. |
| T-10 | Cut-over (script, in-memory Firestore) | dry run writes nothing; `--apply` twice equals once; counts per collection; kept stores untouched by the purge; every transcript part copied and `last_used_at` stamped; the purge deletes only `{user_id}:`-prefixed ids; the preflight refuses with a busy lease, with tasks queued, and once a connection has a sync state; `--purge-new` leaves the store, ledger, roots, notes and usage alone; `--copy-back` restores what the old revision needs; I5 on the rebuilt world. |
| T-11 | OAuth flow | a flow is single use, expires at 10 min, is refused for another session user, and a reconnect with another subject stores and revokes nothing. |
| T-12 | Drive names | `validate_mount_name` cases (length, portable set, casefold uniqueness, reserved names, held former names); a rename keeps every ref and resolves a former-name path for 30 days with the notice; no tool, agent or background path can write a connection setting (the specialist's tool list has no settings writer; a settings commission returns the Cabinet line). |
| T-13 | Drive notes | a `c:` note is written, undone and erased through the note tools by `whole_drive_name`; a folder note with `file_ref` never lands on the drive; exactly one target; owner-turn gating; shown in the preflight bounded; skipped by the indexer's notes step; archived on disconnect and found by the deleted-files search; kept across a reconnect. |
| T-14 | Keys are the runtime's (I6) | for the same item, drive and account, every key the cut-over script writes equals the key the new runtime computes (transcript store, fallback, ledger, archive, mirror root, initial build, `i:`, `r:`); the script imports those functions (AST: no key formula in the script). |
| T-15 | Paid transcripts kept (I7, I8) | after a purge, no stored transcript is swept before the connection's `transcripts_reattached_at`; with transcription off on one connection, its user is never swept; a disconnect retains the heads its records carried, and the sweep skips them after 30 days; a later connection with the same content re-attaches without a paid window. |

## 14. Slices

Each slice ships and is deployed before the next deploys.

| Slice | Content | User sees | Done when |
|---|---|---|---|
| **1a** Credentials and the OAuth edge | `OAuthCredential`, `Integration`, `ProviderAccount`; port re-keyed and the ~39 call sites migrated; `OAuthProviderPort` with the Google and Microsoft edges; server-side flow and the generic routes; `GET /api/connectors` and the Cabinet's connect buttons rendered from it; the revoke rule; TD-V6 closed, REQ-ARCH-18 on `src/web`; T-2, T-6, T-11, the OAuth half of T-3. The drive port still addresses by user; the OneDrive adapter finds the user's one drive credential through `list_for_user`. | nothing (the connect buttons point to the new routes; consent screens as today) | every integration connects, reconnects and disconnects through the generic routes; `integration` is set on every credential (a small idempotent backfill in the slice). Owner: new redirect URIs before the deploy. |
| **1b** Drive connections | `DriveConnection`, repository, service, primary pointer; `DriveHandle`, router, registry; port by handle; capabilities (`max_path_chars`); neutral shapes; `ConnectionScope` keys and indexes; per-connection ticks and fan-out; flags moved; tagged refs and the stale-ref lines; money transactions; the sweep gate and disconnect retention (I7, I8); T-1, T-3 drive half, T-4 (stage-1 part), T-5, T-7 to T-10, T-14, T-15; the cut-over script; prompt ref examples. | the rebuild; "search again" for old refs | the cut-over of §12 ran; G2 equal to before on the rebuilt fixture; a G3 dry run equal to before; the owner's checks of §12 step 5 pass. |
| **2a** Mount tree | `drive_mounts.py`; canonical printing; `drives` fields; preflight per connection; prompt tokens; the stage-2 half of the specialist eval | every path shows its mount (`Drive/…` until renamed) | G3 before/after shows no regression; old primary-relative paths still resolve. |
| **2b** Second connection | Cabinet list and actions (kind picker from `/api/connectors`, names with validation and former names, scope picker and "whole drive", write folder, primary, searched by default, indexing, disconnect); `feed_scope` and the planner's ancestry filter; the scope rows of the access policy; the Cabinet line in Smart's and the specialist's tokens and `capabilities.py`; drive notes (`c:` binding, `whole_drive_name` target); T-4 (stage-2 part, account-wide `fake_b`); T-12, T-13; access policy with `ItemAccess` and `writable`; the `ADD` intent (account chooser) and reconnect hints; search across connections and "also on"; bot-owned artefact placement; per-connection disconnect with note archive; initial build per account | a second drive | live acceptance on a second OneDrive App Folder account under the same app registration: add, name it in the Cabinet, write a note on it in chat, ask "find the plan on my work drive", choose a write folder, search both, "also on" for one file copied to both, set primary, disconnect the second (purged, notes archived, the first untouched). |

## 15. What changes in the owner's drive map at land

`docs/05_building_blocks/user_drive/README.md`: the §0 index gains the rows "which drive a file is on; what a mount and
the primary are; why a ref says search again" (§1 overview and §2 architecture) and "what a disconnect removes and
keeps" (§7). §1 and §2 describe connections, handles, the router and the registry; §3 (the index) changes the doc-id
column to connection-scoped keys and names the user-scoped stores; §4.13 (initial build) keys by account; §4.23
(transcripts, mirror) names the per-connection mirror and the identity-keyed ledger and roots; §4.24 (notes) the tagged
`r:` key, the disconnect archive and the note on a whole drive (`c:`); §4.23 also the sweep gate and the retention on
disconnect; §5 (the tick) is per connection, with the account-wide feed's ancestry filter and scopes (stage 2); §6
(search) gains `drives`, the merge and "also on" (stage 2); §7.2 (deploy order) gains the cut-over; §8 (code references) the new files. Root `CLAUDE.md`'s "User
Drive" and "User Drive indexing" paragraphs and `src/agents/CLAUDE.md` → Drive change in the same PR as each slice.

## 16. Engineering decisions

| # | Decision | Reason | Rejected |
|---|---|---|---|
| E1 | Credential (grant) and `DriveConnection` (mount) are two entities. | Gmail and To Do gain nothing from a connection doc; product state (name, scope, write folder, tag) belongs to the mount, tokens and identity to the grant, and only drives have both. | One generic `Connection` for every integration: three entities where two suffice. |
| E2 | Primary pointer in a fresh-read `drive_user_settings` doc. | One source, no "exactly one primary" flag invariant; `UserBotConfig` is agent-cached for about an hour. | `is_primary` per connection; the pointer on `UserBotConfig`. |
| E3 | Refresh stays with the data adapters' token providers (`graph_auth.py` re-keyed by credential); the port has no `refresh`. | Refresh is a data-path concern already owned there (`graph_auth.py` dedup and cache). | Refresh on `OAuthProviderPort`: services would become refresh callers. |
| E4 | One callback URI for every realm; the flow names the realm. | One registered URI per console, no web code per provider. | A callback per realm or per kind. |
| E5 | The `/worker` OIDC verifier's HTTP becomes its own named debt entry, not moved here. | A security gate outside this scope with a record of breaking callers. | Moving it in slice 1a; keeping REQ-ARCH-18 off `src/web`. |
| E6 | Every port method takes a `DriveHandle`; one router in composition. | Services keep one port dependency; the change is mechanical `user_id → handle`. | A port instance bound per connection from a factory: a memoise/evict lifecycle and the stale-instance bug class. |
| E7 | Item and drive identities are adapter-built `{id_space}:{…}` strings; `id_space` is declared per registry entry. | Domain never parses provider ids; OneDrive's identity reproduces today's note keys byte for byte. | A per-item `drive_id` field: a file shared to two Google accounts would get two identities (two notes). |
| E8 | Index doc ids `{connection_id}:…` with `user_id` a field and checked on read. | Connection ids are globally unique; repository queries already filter the `user_id` field. | `{user}:{conn}:…`: only length. |
| E9 | Stage-2 search: one query per searched connection, merged in the service. | Keeps the equality prefilter the vector indexes use; "searched by default" is a selection, not a query shape. | One query with a multi-value connection filter. |
| E10 | The ref codec is `domain/drive_ref.py`; P9 is not built here. | P9 is an unruled proposal of another RFC; the codec is what P9's drive branch would call. | Building `domain/file_ref.py` in stage 1. |
| E11 | Default mount names `Drive`, `Drive 2`, …, never the provider or the account label. | Provider names never reach the model; the account label is personal. | The provider's display name; the email address. |
| E12 | Canonical `<mount>/` printing always in stage 2, also with one connection. | One rule; a conditional prefix would change a file's printed path the day a second drive is added. | Printing the mount only when there are two or more connections. |
| E13 | A new connection of a drive does not revive notes archived by an earlier disconnect (v1). | Rare; the archived note is findable; revival needs a new lifecycle transition. | Automatic revival by identity on the first round. |
| E14 | The legacy purge is a step of the one-off cut-over script; product code knows only the new keys. | No legacy branch in `src/` (D12); the script is deleted after acceptance. | Purging on the old revision before the deploy: needs a "purge now" release first, and the old fan-out can restart a rebuild between purge and pause. Teaching the new purge the old shapes: a legacy special case. |
| E15 | `ConnectorSpec.multiple` decides cardinality in `OAuthConnectService`, not in the store. | Product policy, not storage; the store stops raising on two records of a kind. | Keeping the store's one-per-kind check (it would forbid two drives). |
| E16 | The flow carries an intent (`CONNECT`, `ADD`, `RECONNECT`) and a hint; each edge maps them to its realm's parameters; stage 1 uses only today's. | Services hold no OAuth parameter names; Google must merge `consent` and `select_account`; stage 1 looks the same (D15). | Static `auth_params` on the spec plus service-added prompts. |
| E17 | A drive note is a note with a connection binding `c:{connection_id}`. | Reuses versions, undo, erase, receipts and gating; the note says what this connection is. | The drive root item's `i:` binding (outlives the connection, no single root on full drives); a `description` field (no versions or undo). |
| E18 | Former drive names are held for 30 days and resolve with a notice. | Paths in recent history keep working after a rename without a legacy resolver; the hold expires. | Forgetting old names at once; aliases forever. |
| E19 | Drive phrases are resolved by the model from names and notes in the preflight; code accepts only exact (casefold) names. | A wrong guess is refused, never applied to the wrong drive. | Fuzzy matching in code; an alias list per connection. |
| E20 | The account-wide feed filter, selected-folder scopes, "whole drive", their access rows and the scope picker are stage 2, proven by an account-wide fake. | They are provider-neutral; leaving them to stage 3 would make the next provider change planner, policy and Cabinet, failing D12. | Deferring them to stage 3; weakening the bar to "providers inside the current capability set". |
| E21 | Native documents are adapter emulation (export on `download`, `can_write_content=False`, not downloadable over the cap); no `native_documents` capability. | Domain sees an ordinary file; the existing policy and cannot-open path cover it. | A capability and an open-path branch. |
| E22 | No orphan sweep for a user until every connection has re-attached since its sync state was created; the cut-over also stamps `last_used_at`. | After a purge no record carries a transcript key, and the sweep runs first in the work-off; a stamp alone only delays the loss by 30 days. | Only stamping `last_used_at`; pinning every copied head forever (defeats the sweep for recordings truly deleted). |
| E23 | A disconnect marks the transcripts its records carried `retained`; the sweep never deletes a retained head. | Realises D9 beyond the sweep's 30 days. | Relying on the 30-day window; deleting with the index. |
| E24 | The network-capable parser sandbox gets a header-less URL or streamed bytes, never provider headers. | A full-drive bearer token must not enter the sandbox; deciding now keeps stage 3 a pure adapter. | Passing `MediaSource.headers` to ffmpeg. |
| E25 | Single-credential services filter by the `kind` of an injected `ConnectorSpec`, not by integration. | `TASKS` has two kinds (Google Tasks and To Do); an integration filter is the overloaded argument relocated. | `list_for_user(user, integration)`. |
| E26 | `drive_id` stays on the credential until slice 1b, where its home, the `DriveConnection`, arrives. | Moving it earlier needs the connection entity; the OneDrive activation stores it on the saved credential meanwhile. | A second store for it in 1a. |
| E27 | All four kinds are `multiple=False` in 1a: a consent of another account replaces the record, as before. Slice 1b flips the drive. | 1a is invisible to the user; many drives need the connection entity and the handle. | `multiple=True` for the drive before the connection exists. |
| E28 | Every existence check (connected, status, disconnect, sweep, one-per-kind replacement) reads `list_keys`, which never calls the cipher; `get` is used only where a token is needed. | The stage-0 property: a decrypt or KMS failure never reads as "not connected". | `list_for_user` for existence checks. |
| E29 | `save` returns the `credential_id` the store chose, and `authorization_url(flow, spec, redirect_uri, *, state)` takes the state as an argument. | The connect routes keep the saved record by id without a re-read; the stored flow keeps only the digest of the state, so the URL cannot be built from the flow. | A re-read after save; storing the plaintext state in the flow. |
| E30 | One `Integration` enum (`domain/integration.py`: `MAIL`, `TASKS`, `DRIVE`) serves the Cabinet grouping and R1's reconnect notices; `reconnect_policy` imports it and no second enum of the name exists. The ops alert path strings of R1 changed with it (`mail_`, `tasks_`, `drive_reconnect_notice`). | Two enums of one name drift; the notice texts are unchanged. | Keeping R1's `GMAIL/TODO/DRIVE` enum beside it. |
| E31 | `ConnectorSpec.required_scopes` holds product scopes only; each edge adds the identity scopes it needs, and Microsoft's granted scopes are normalised to the short form before the check. `ConnectorSpec.offered` is False for Google Tasks (frozen: stored grants work, no button, no entry in `GET /api/connectors`). | The scope check compares like with like; the registry is the single place that knows which kinds the Cabinet offers. | Identity scopes in every spec; a Cabinet-side list of kinds. |
| E32 | `GMAIL_INDEX_PROVIDER` (`domain/email.py`) is the email index's own key, not a credential kind; the credential kind literals live in the registry. | The email index job and the credential are different records; the index key names a provider of mail, not a grant, and T-6 keeps credential kind literals in the registry. | Reusing the `gmail` kind constant for the index. |

## 17. Owner-only questions

None open after the owner decisions of §2. Owner **actions** (not questions): register the new redirect URI before
slice 1a deploys (D14); drain transcription with it on, then switch it off, before the cut-over, and back on after the first round (§12); run the cut-over
script and the checks; for stage 2, consent a second Microsoft account to the app; remove the old redirect URIs after
acceptance.
