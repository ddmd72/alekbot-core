# RFC: Secrets at Rest — every persisted secret, one credential store

**Status:** S1 built, S2 built, S3 built (v2, rewritten 2026-10-29; v1 dated 2026-07-20/22): the cipher, the keyed digest, the MCP store, the R1 reconnect notice and the credential store (`decisions/credential_store_cutover.md`) are in code; S4 onward are PROPOSED
**Owner:** AI Engineering
**Milestone:** Security — stage 0 of the multi-drive programme (runs before any multi-drive work)

**Related:** `ports/oauth_credentials_port.py`, `adapters/firestore_oauth_credentials_adapter.py` (replaced here);
`adapters/firestore_mcp_client_repository.py`, `domain/mcp.py`; the voice apps over `adapters/firestore_ephemeral_store.py`;
`adapters/firestore_invite_code_repo.py`; TELEGRAM_CHANNEL_READER_RFC (§7). The delivery plan and the one-off rollout steps live in the owner's private ops repo
(`alekbot/designs/secrets_at_rest_plan.md`, `alekbot/deploy/secrets_at_rest.md`); this RFC holds what stays true after the deploy.

## 1. Problem

The app persists secrets of its own outside Secret Manager, all raw. Firestore's default encryption protects the disk,
not the data from an identity *authorized to read Firestore*: a leaked service-account key, an over-broad binding, a
bug that dumps a collection, an export. Two classes, by what the app does with the value:

| Class | Store (`{prefix}` = env prefix) | Today | Use |
|---|---|---|---|
| Decryptable | `oauth_credentials` (Gmail, Google Tasks, Microsoft To Do, OneDrive) | `access_token`, `refresh_token` raw (`firestore_oauth_credentials_adapter.py:33-44`) | sent to the provider |
| Decryptable | `mcp_oauth_clients` | `client_secret` raw (`firestore_mcp_client_repository.py:50`), deliberate (`domain/mcp.py:109-117`) | the `mcp` SDK compares it |
| Decryptable | future Telegram session (TELEGRAM RFC) | — | sent to Telegram |
| Verify-only | `mcp_auth_codes` | doc id and `code` = the raw code (`firestore_mcp_client_repository.py:97,125`) | compared once |
| Verify-only | `voice_tickets` (`voice_ticket:` key) | doc id = the raw ticket (`voice_webhook_app.py:130`) | redeemed once (`get_and_delete`) |
| Verify-only | `domain_invite_codes` | doc id = the raw 9-hex code, 36 bits (`invite_code_service.py:47-48`) | compared on join |
| Verify-only | `mcp_refresh_tokens` | already `sha256(token)` (`mcp_authorization_service.py:449-450`) | compared on refresh |

**Completeness pass (2026-10-29, main @ b818f7a1).** Checked: every collection in `config/environment.py` and named
inline in `src/adapters/firestore_*.py`; every `secrets.*`/`uuid4` minting site; log lines interpolating a ticket, code
or token; Cloud Tasks payloads (none carries a secret); Logfire (LLM SDKs only, no HTTP header capture); GCS (content);
`prompt_content` (no credential reaches a model); cookies (login `state`, Cabinet session: not persisted). Found:
- **The voice ticket is not purely verify-only.** It also sits raw in `voice_pending_callback:` and `voice_web_call:`
  records (`voice_webhook_app.py:141`, `voice_web_call_app.py:79`), read back to build the callback and SFU URLs
  (`voice_webhook_app.py:173-187`, `voice_web_call_app.py:99-104`), and in derived keys (`voice_control_plane_app.py:87,91`;
  `voice_call_kind:{ticket}`, `voice_call_setup_service.py:68`).
- **Tickets are logged** in ~60 statements in seven files (main service and relay); invite codes at `invite_code_service.py:63,100`,
  `firestore_invite_code_repo.py:73,91` and `oauth_app.py:188`.
- **Short links** (`{prefix}short_links`): doc id = the code, `target_url` = an `/f/<token>` capability link. Out of
  scope: the same links sit in session history and `prompt_content`, so this collection alone protects nothing (§9).
- **Draft and erase codes** (`$skill save`, `$memory save`, `$drive-notes erase`): user-scoped, chat-only. Not secrets.

## 2. Decisions

### 2.1 Encrypt at the application layer, keep the datastore
Secret Manager does not fit OAuth credentials: tokens refresh about hourly (a version per refresh: churn, cost, destroy
hygiene), the store must be queried, and a credential is a record, not a string. Firestore fits; we encrypt the secret
fields. Firestore CMEK is no substitute: it hides nothing from an API reader and is fixed at database creation.

### 2.2 One primitive: `CipherPort`, backed by Cloud KMS
`ports/cipher_port.py`: `encrypt(plaintext: str, *, aad: str) -> str`, `decrypt(ciphertext: str, *, aad: str) -> str`,
base64 out. **`aad` is required**, of the form `<store>/<record key>`: a ciphertext cannot move to another record or
store. `decrypt` raises on a wrong key, tampering or a mismatched AAD. The port names no provider, store or secret.
`adapters/gcp_kms_cipher_adapter.py` wraps the async KMS client and sends and checks the CRC32C fields both ways
(`plaintext_crc32c`, `additional_authenticated_data_crc32c`, the `verified_*` flags, `ciphertext_crc32c`); a mismatch
raises. New dependency `google-cloud-kms`. **Direct encrypt, no envelope:** every secret is far below the 64 KiB
symmetric plaintext limit; a data-key layer stays an option for large blobs.

### 2.3 The credential store, in its final data shape
A **new collection** `{prefix}credentials`, doc id `{user_id}:{credential_id}`; `credential_id` is opaque, random and
immutable; tenancy is structural (the doc id carries the user).

| Field | Plaintext? | Why |
|---|---|---|
| `user_id`, `credential_id`, `kind` (`gmail`, `google_tasks`, `microsoft_todo`, `microsoft_onedrive`) | yes | queried, not secret |
| `account.realm` (`google`, `microsoft`), `account.subject` (the provider's stable account id) | yes | queried by stage 1's revoke rule |
| `scopes`, `token_expiry`, `needs_reconnect`, `created_at`, `updated_at` | yes | operational, not secret, not personal |
| `drive_id` (OneDrive only) | yes | compared by the indexer's drive-switch gate; stage 1 moves it to `DriveConnection` |
| `secret` = ciphertext of `{access_token, refresh_token, account_label}` | **no** | the label (email, display only) is personal, never queried |

One ciphertext, AAD `credentials/{user_id}:{credential_id}`: one KMS call per read and per write, no partial swap. The
Telegram session (§7) is one more `kind` (realm `telegram`, subject its user id, payload the session), not a new store.

**Identity, read by the consent edge; every record has it from its first write (I9).** In code, the Microsoft
consents request `Tasks.ReadWrite offline_access` and `Files.ReadWrite.AppFolder offline_access` (`oauth_app.py:705,826`):
no profile scope, so `/me` is not readable and no account identity is stored today. Google: OpenID `sub` + email from userinfo (Gmail already asks `openid email`; the
Google Tasks consent gains both). Microsoft, To Do and OneDrive alike: the consent gains **`User.Read`**; subject = Graph
`/me` `id`, label = `mail` or `userPrincipalName`. One value then names a Microsoft account across both kinds. No
`openid` for Microsoft: a second identity source nothing reads. OneDrive keeps reading `drive_id` from the App Folder's
`parentReference.driveId` (`oauth_app.py:829-847`), the field the delta reader binds its cursor to (`onedrive_adapter.py:88`).

### 2.4 The port contract stays "credential of user U for kind P": a seam, not a crutch
`OAuthCredentialsPort` keeps every method and every docstring promise (tenancy, no expiry logic, "update never
re-creates a revoked record", errors propagate) except two lines of its docstring, which are dropped: the
`{user_id}_{provider}` keying and the legacy-document reads (`oauth_credentials_port.py:19-23`); the new store has
neither (§2.7). **An update keeps the stored record identity** (`credential_id`, `kind`, `account.realm`,
`account.subject`) and never takes it from its argument: every refresh rebuilds the credential field by field
(`graph_auth.py:127-138`, and the Gmail and Google Tasks adapters), so identity fields on the argument would be lost on
the first refresh. Stage 0 holds **at most one record per (user, kind)**, today's honest
contract, found by an equality query on `user_id` + `kind`. A consent runs in a transaction: the same account keeps its
`credential_id`; a different account gets a new one and the old record is deleted. `update_credentials` keeps its
"exists" precondition. Stage 1 changes **addressing** (by credential or connection, no `provider` parameter) and allows a
second record per kind. It does not touch the **data**: stage 0 leaves ~33 call sites untouched, and stage 1 re-keys
them against final data.

### 2.5 Verify-only values: a keyed digest, never the raw value
Stored as `HMAC-SHA256(key, purpose ‖ 0x00 ‖ value)` (hex): the doc id where the raw value is today, with no raw copy in
any field. `key` is a new Secret Manager secret via `load_settings()` (required), main service only; `purpose`
(`mcp_code`, `voice_ticket`, `invite`) separates the uses. The digest is a pure `domain/` function (stdlib `hmac`), no port.
- **MCP codes:** doc id = digest, the `code` field goes, `load_authorization_code` rebuilds from the presented code.
- **Voice tickets:** `voice_ticket:<digest>` and digest-based derived keys, `voice_call_kind:<digest>` included. The two
  hand-out records (`voice_pending_callback:`, `voice_web_call:`) carry the ticket as `CipherPort` ciphertext (AAD
  `voice/<record key>`), so the ticket is decryptable only there, for the record's own life: 300 s for the callback
  record, the call's TTL for the web-call record (`voice_web_call_app.py:78-81`).
- **Invite codes: 128 bits** (26 base32 characters, grouped) *and* digested. The digest key sits in Secret Manager,
  which every runtime identity and the analysis identity can read today, and they also read Firestore. Behind that key
  a 36-bit code falls to an offline search; a 128-bit one does not. The Cabinet shows a code once, at creation; the
  list shows type, state, expiry and a non-secret hint. Invites move to a new collection like every other store, and
  the old one is deleted with the other old stores (§6.3 step 5): left in place, the Cabinet list
  (`user_cabinet_app.py:436`) would show its raw codes.
- **MCP refresh tokens stay unkeyed `sha256`:** 384 random bits gain nothing from a key. Re-keying needs plaintext we do
  not hold, so every connector would re-authorize, or a mixed lookup would be the branch §2.7 forbids.
- **Logs:** correlation by `sha256(value)[:8]`, safe because every remaining value is ≥ 122 bits; invite codes are never
  logged in any form.

### 2.6 KMS key and access
One software key (`ENCRYPT_DECRYPT`), **automatic rotation** every 90 days. Every refresh re-encrypts under the
primary; idle records decrypt under their version. No version is disabled or destroyed in stage 0 (I7). About $0.06
per version-month + $0.03 per 10 000 ops: **< $1/month** (`06_secrets_inventory.md`, KMS facts). The key name comes from
configuration (`KMS_CRYPTO_KEY`, `load_settings()`), never a tracked file. `roles/cloudkms.cryptoKeyEncrypterDecrypter`
is granted **on the key**, to the **main service's** and the **indexer's** identities only. The services already run under
separate identities (main service, indexer, voice relay, sandbox, deep-research job); the indexer (`indexer_main.py`; it
refreshes OneDrive) and the main service need the key, while the relay, sandbox and job get no grant and no secret.
**Fail closed:** both services refuse to start without `KMS_CRYPTO_KEY` (the main service also needs the digest key),
mirroring `config/auth.py:162-170`. The cipher and the digest key are built in the composition root
(`build_application_graph`), outside the MCP build, whose errors are swallowed (`main.py:517-522`: "Bot will continue
without MCP connector"): fail closed means a failed start, never "MCP silently off". The indexer has no `load_settings()`
(`indexer_main.py:99-140`): it reads the key name from its environment and fails closed the same way.

### 2.7 No legacy branch, anywhere
No dual read, no `enc_v` plaintext path, no passthrough cipher in any runtime, no copy. The new stores start **empty**,
the new code never touches the old collections, and every grant is re-created by a consent (§6). **Local dev uses the
real KMS key** (the owner's identity can decrypt). **Tests** use an in-memory AES-GCM cipher with a test key
(`tests/support/`): real authenticated encryption with the same AAD semantics, never an identity function.

## 3. Rejected alternatives

| Alternative | Why not |
|---|---|
| Secret Manager per credential | write churn, no query, a record is not a string (§2.1) |
| Copy old records into the new store (earlier v2 draft) | needs ordering rules for consents, disconnects and rotation during the copy; carries no identity (none stored); leaves the exposed grants alive. Reconnecting every record fixes all three |
| Encrypt in place with `enc_v` + dual read; passthrough cipher for local dev (v1) | plaintext paths in production code; local dev has KMS access |
| One ciphertext per token (v1) | two KMS calls per op; tokens of two grants could be paired |
| Unkeyed hash, or keyed digest alone, for invite codes | 36 bits: enumerable offline; the key is readable by the Firestore readers (§2.5) |
| KMS-held MAC key for digests | a KMS call per check, no gain once every value is ≥ 122 bits |
| Re-addressing the port in stage 0 | doubles stage 0 (~33 call sites); stage 1 re-keys them anyway (§2.4) |
| Waiting for per-service identities | not needed: `roles/editor` holds no `cloudkms.cryptoKeyVersions.useToEncrypt/useToDecrypt` and cannot grant itself a role on the key |

## 4. Threat model

| Actor / event | Today | After stage 0 |
|---|---|---|
| Identity with Firestore read (leaked analysis key, export, dump) | every token, client secret, live code | ciphertext and digests. A usable token needs Firestore read **and** KMS decrypt, held by different grantees |
| Same identity also reading Secret Manager | same | invite codes safe (128 bits); the digest key alone opens nothing |
| Row swap inside Firestore (attacker with write) | possible | AAD binds each ciphertext to its record; decrypt fails, loud |
| Log reader | live tickets (5 min), invite codes (7 days) | fingerprints only |
| Runtime identity compromised, or an identity that can act as it | everything | everything. The key grant is held only by the main service's and the indexer's identities, so the relay, sandbox and job reach nothing here; further narrowing is the least-privilege track (separate work) |
| Plaintext exposed before stage 0 (old collection, backups, PITR, exports) | — | not undone by encryption or deletion; cured by revoking the old grants (§6.3 step 2) |

KMS data-access audit logs (enabled for the key) record every decrypt with its caller. Plaintext lives only in process
memory for a provider call (and the Graph token cache); it is never logged and never reaches `prompt_content`.

## 5. Invariants

- **I1** No secret of §1 reaches Firestore, logs or task payloads raw: only as `CipherPort` ciphertext or as a digest.
- **I2** Every ciphertext is bound by AAD to `<store>/<record key>`; `aad` has no default.
- **I3** No runtime path reads a plaintext secret from Firestore, and no runtime holds a no-op cipher.
- **I4** A decrypt or KMS failure raises and is alerted: never "not connected", never a delete, a flag, a drive purge
  or a reconnect notice.
- **I5** At most one credential per (user, kind) in stage 0; `credential_id` survives refreshes and same-account consents.
- **I6** Main service and indexer refuse to start without the key configuration.
- **I7** No KMS key version is disabled or destroyed while a record may reference it (stage 0: never).
- **I8** A reconnect of the same account re-indexes and purges nothing (§6.2); the To Do exception: a consent re-subscribes and re-reads the list exactly once.
- **I9** No credential record exists without its account identity: an identity read that fails stores nothing.

## 6. Migration contract: by reconnect, no copy

Live state is in the private ops repo. Every grant is re-created by
a consent on the new code, which also supplies the identity (§2.3). The old grants are revoked right after the deploy, so
one consent per integration is the only reconnect.

The stores switch in **separate deploys**, not one (order and per-deploy owner steps: the delivery plan in the private
ops repo, `alekbot/designs/secrets_at_rest_plan.md`): MCP clients and codes first, then the credential store, then
voice tickets, then invites. §6.3's revoke → reconnect sequence is the **credential deploy's** sequence; §6.1 per store
stays as written.

### 6.1 What the deploy does to each store
- **Credentials:** the new store is empty; the old collection is never read or written by the new code.
- **MCP clients:** the new collection is empty, so the claude.ai connector can neither authorize nor refresh. **Owner
  step:** remove the connector and add it again. The old clients' MCP refresh tokens are orphans (deleted in step 5).
- **Verify-only stores:** raw values outstanding at the deploy stop working. What the user notices: an invite issued
  before it answers "invalid" and is reissued; a connector sign-in in progress fails and is restarted; a call being set
  up at that moment does not connect and is dialled again. Deploy outside a call: a call running across the deploy can
  lose a late delegation answer.

### 6.2 Between deploy and reconnect; the reconnect invariant (I8)
With no record, every integration reads "not connected". Today (verified):
- **Gmail:** `needs_reconnect` and its one notice exist only for a refresh rejected on an existing record
  (`worker_handler.py:1060-1081`). With no record the Cabinet shows "Connect Gmail", and daily review and search skip.
- **Drive:** the notice fires only on `needs_reconnect` (`drive_sync_scheduler_service.py:279-334`). With no record the
  tick records an access loss and starts the 30-day grace silently (`drive_index_planner.py:858-864`).
- **To Do:** no reconnect state and no notice exist at all.

**Requirement R1** (in stage 0, not a follow-up): an integration whose own state shows a past connection (Gmail indexing
state, drive sync state, To Do task config) but has no credential shows **Reconnect** in the Cabinet and sends one chat
notice per user and integration. R1 reads those feature states, never the old credential collection. Its sweep also
**lists users from the feature stores, not the credential store**: today's daily-review and To Do renewal fan-outs list
from the credential store (`email_review_service.py:48`, `task_setup_service.py:53-55`) and would see nobody. Gmail and
To Do run in a new scheduled worker task; Drive in its existing fan-out.
- **A deliberate disconnect is not a loss (F1).** A disconnect marks the feature state "disconnected by user", and R1
  skips such a state; a consent clears the mark. Gmail and Drive disconnects delete only the credential and keep the
  feature state (`user_cabinet_app.py:1199-1217`, `onedrive_adapter.py:247-250`); To Do's deliberate disconnect resets its config
  (`task_setup_service.py:235`), leaving no past connection for R1 to see, so only Gmail and Drive need the mark.
- **The once-per-transition flag lives on the feature state (F2)**, not on the credential record: Gmail indexing state,
  drive sync state, To Do config. Gmail's current flag sits on the credential and its path returns when there is no
  record (`worker_handler.py:1071-1073`); To Do has no notice path today.

I8, checked against the code:
- **Gmail: holds.** The callback only saves the credential (`oauth_app.py:570-584`). Indexing starts only from the
  Cabinet; indexed emails and the indexing state are keyed by user, not by grant.
- **Drive: holds, within the grace.** Same drive → `RESUME_AFTER_REGAIN`: the cursor resumes, nothing is purged
  (`drive_index_planner.py:867-869`). `drive_id` is stored for the first time (empty on the live record), so the switch
  check (`:865`) becomes live, **if the drive id read at connect succeeds**: on failure an empty string is stored and the
  check stays off (`oauth_app.py:829-850`). Both sides read `parentReference.driveId` of the same call, so the same drive compares
  equal; another drive purges, by design. A reconnect over 30 days after the deploy → `PURGE_EXPIRED`, a paid rebuild.
- **To Do: does not hold strictly, by rule.** The callback enqueues `setup_microsoft_todo`. A consent is a new grant, so
  the setup always re-subscribes: it drops every stored subscription (deleting it at Graph), registers one, and
  `reindex_task_list` re-reads the primary list exactly once, even when the old subscription still read "active". The
  re-read is an upsert, purges nothing, and is correct, because webhook notifications during the gap went unprocessed.
  The renewal sweep is unchanged (an active subscription is kept, nothing re-read). The one re-read is accepted (owner,
  2026-10-29; Q3).

### 6.3 Sequence
1. **Deploy** main service and indexer with key, digest key and grants in place (I6). The stores start empty; R1 notices go out.
2. **Revoke the old grants, at once after the deploy.** Google: revoke each old record's **refresh** token. Today's
   disconnect revokes the *access* token, usually expired, and does not reach the grant (`user_cabinet_app.py:1210`).
   The Google revoke refuses to run while the new store holds a Google record (revoking is grant-wide, so it would end the
   new grant too): it runs before the first Google reconnect. Microsoft consumer accounts offer no per-app token
   revocation, so **owner step:** every Microsoft account holder removes the app's consent on their own account's
   app-permissions page, proven by a refresh with the old token that fails. One-off procedures live in the private ops repo. Both revocations are
   expected to be grant-wide: Google ends the grant to the OAuth client Gmail and Google Tasks share, and Microsoft ends
   every grant of the app. Why now: the new code never reads the old collection, so nothing depends on the old grants;
   revoking first retires the exposed grants without delay, and one consent per integration is then the only reconnect.
   Proof: a refresh with an old refresh token fails.
3. **Reconnect**, once: each user reconnects each integration in the Cabinet. The consents request the identity scopes
   (§2.3, I9), and tokens are ciphertext from the first write.
4. **Verify** after the reconnect (a check, not a gate before anything): each integration did its normal work through
   the new store (a Gmail search or daily review, a drive tick resumed from its cursor, a To Do webhook); the stored
   drive id is non-empty (F7); the KMS audit log shows only the main service's and the indexer's identities.
5. **Delete** the old collections (credentials, MCP clients and their refresh tokens, raw verify-only documents; voice
   ones also age out by TTL), within 7 days of the deploy: they are the plaintext being removed. Old MCP refresh tokens are selected by the **old
   clients' ids**: the new clients' refresh tokens share the collection. Older copies need no action here (Q2 resolved).

### 6.4 Rollback
- Per store, the rollback is its deploy's previous revision, plus a connector re-add for MCP and, for credentials, the
  previous revision of both services, an empty new credential collection (`{prefix}credentials`) and a reconnect of every
  integration (its consents recreate the old collection). Invites issued since the deploy are lost.
- **Image:** exempt the pre-cutover images from Artifact Registry cleanup until step 5 + horizon, or rebuild from that commit.

## 7. Relationship to other work
- **TELEGRAM_CHANNEL_READER_RFC** (still blocked on this one): its session is one more credential `kind` (§2.3), zero new crypto code.
- **Multi-drive stage 1** inherits the store as is (opaque `credential_id`, identity, plaintext/secret split). It re-keys
  the port (§2.4), moves `drive_id` to `DriveConnection`, and applies its revoke rule over `account.realm` + `account.subject`.
- **Data privacy track** (separate): content is not encrypted here; its first step is IAM least privilege.

## 8. Testing contract
- **Cipher:** the AES-GCM test cipher round-trips; a wrong AAD, a flipped byte or another key raises. The KMS adapter's
  wire test (mocked at the SDK boundary, `ADAPTER_WIRE_TESTING.md`) checks key name, AAD and CRC32C fields; a missing
  `verified_*` flag or a CRC mismatch raises.
- **Credential adapter** (fake Firestore with semantics): no token or label in any plaintext field; the existing
  port-contract tests run unchanged over ciphertext records. A same-account consent keeps `credential_id`, another
  account replaces it. `update` after revoke returns `False` and writes nothing. A ciphertext moved to another record
  fails (I2), and a KMS failure propagates (I4).
- **Architecture:** every credential or MCP-client store has a `CipherPort`; the KMS adapter is the only one in `src/` (I3).
- **Verify-only:** no raw MCP code, ticket or invite reaches a Firestore write or a log record (capture both); invites
  are ≥ 128 bits; the voice hand-out records hold ciphertext; derived keys use the digest. **Startup:** I6.
- **I8** (fakes with semantics):
  - Gmail: a consent over an existing indexing state writes only the credential and enqueues no indexing.
  - Drive: no credential + a sync state → `RECORD_LOSS`, index kept. A same-drive consent → `RESUME_AFTER_REGAIN` from
    the stored cursor. Another drive → `PURGE_DRIVE_SWITCH`. Past 30 days → `PURGE_EXPIRED`.
  - To Do: a consent, with an active or a lapsed subscription → exactly one `reindex_task_list`, a re-subscription (old ones deleted at Graph), no index delete; the renewal sweep keeps an active subscription and re-reads nothing.
- **R1:** no credential + a past-connection state → one notice per integration and "Reconnect"; the next fan-out sends
  none; a consent clears it. A deliberate disconnect → no notice. **I9:** each consent edge stores subject and label; a failed identity read stores nothing.
- **Live check (owner-run):** after step 2 a refresh by an old refresh token fails; after step 3, step 4's list.

## 9. Out of scope
Content encryption (facts, sessions, drive index, vectors, workspace, media); capability links in history and
`prompt_content`, with the short links that mirror them (§1); per-service identities and least privilege (§3, last row);
rate limiting invite redemption; draft and erase codes (§1); app-level secrets already in Secret Manager.

## 10. Open questions
- **Q1** (resolved) Does revoking one Google refresh token end the client's whole grant? Moot for the sequence:
  revocation precedes the only reconnect, so grant-wide or per-token both end the old grant before the new consent.
- **Q2** (resolved, 2026-10-29) Backups and exports of the old collections: no retained copy outlives the grants. Step 2's
  revocation ends every grant an older copy could hold; the personal content that older copies still hold belongs to the
  data privacy track (§7). Facts about the copies live in the private ops repo.
- **Q3** (resolved) To Do's I8 exception: the one re-read is accepted (owner, 2026-10-29).
