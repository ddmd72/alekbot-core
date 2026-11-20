<!-- living-doc
covers:
  src/ports/cipher_port.py
  src/adapters/gcp_kms_cipher_adapter.py
  src/composition/cipher_setup.py
  src/domain/secret_digest.py
-->
# Secrets at Rest

## Purpose

The app keeps secrets of its own in Firestore: OAuth grants of the integrations, the claude.ai connector's client
secrets, one-time codes and tickets. Firestore's disk encryption does not protect them from an identity that may read
Firestore (a leaked key, an over-broad binding, an export, a bug that dumps a collection). This block is the pair of
primitives that makes such a read useless on its own, and the rule for which one a secret gets.

Two classes of secret, by what the app does with the value:

- **Decryptable**: the app must send it onward (a refresh token to Google or Microsoft, a client secret to the MCP SDK,
  a voice ticket back into a callback URL). It is stored as **ciphertext** of `CipherPort`, bound to its record.
- **Verify-only**: the app only compares a presented value (an authorization code, a voice ticket, an invite code, an
  OAuth state). It is stored as a **keyed digest**; the raw value is in no field and no key.

What it is not: it protects no content (facts, sessions, the drive index, files), it does not replace Secret Manager for
the app's own configuration secrets, and it does not narrow what a compromised runtime identity can reach. It makes a
Firestore-only reader see ciphertext and digests; a usable token needs Firestore read **and** the KMS grant.

## Owned data

The block owns two primitives and no collection. Each store below keeps its own data and is described by its own doc;
this table says what each does with a secret.

| Store (collection, `{prefix}` = environment prefix) | Secret | Treatment |
|---|---|---|
| `{prefix}credentials` (`FirestoreCredentialStore`) | the grant of every integration kind | one ciphertext of `{access_token, refresh_token, account_label}`, AAD `credentials/{user_id}:{credential_id}` |
| `{prefix}mcp_clients` (`FirestoreMCPClientRepository`) | a confidential client's secret | ciphertext, AAD `mcp_clients/{client_id}` |
| `{prefix}mcp_auth_codes` (same repository) | authorization code | digest (purpose `mcp_code`) is the doc id; no `code` field |
| `{prefix}invite_codes_v2` (`FirestoreInviteCodeRepository`) | invite code | digest (purpose `invite`) of the normalised code is the doc id; only a 4-character `hint` is stored |
| ephemeral `oauth_flow:` records (`OAuthConnectService`) | OAuth `state` and the PKCE verifier | key is a digest of the state; the verifier is ciphertext, AAD `oauth_flows/{digest}` |
| ephemeral `voice_*` records | voice ticket | every key derived from a ticket is `{prefix}:{digest}` (`src/domain/voice_ticket_keys.py`); the two hand-out records hold the ticket as ciphertext, AAD `voice/{the record's store key}` |
| geo and drive link stores, voice MCP client id | a bot-issued capability id | digest under the service's own purpose (`GeoMarkService`, `DriveLinkService`, `src/domain/voice_mcp_token.py`) |

Plain fields that are queried or operational (`user_id`, `kind`, `account.realm`, `account.subject`, `scopes`,
`token_expiry`, `needs_reconnect`, `drive_id`, timestamps) stay plaintext on a credential record. Listings and
existence checks (`list_keys`, `list_by_account`, `list_user_ids`) read only those fields and never call KMS, so a
cipher outage never reads as "not connected".

**Configuration.** `KMS_CRYPTO_KEY` (the Cloud KMS key name) and `SECRET_DIGEST_KEY` (hex) are both listed in
`src/config/settings.py::load_settings` and both required. The key's rotation is automatic; no key version is ever
disabled or destroyed, because an idle record decrypts under the version it was written with.

## Flows

### Boot, fail closed

1. `src/composition/boot_setup.py::build_boot_checks` runs at the start of `build_application_graph`, before anything touches the database: it calls
   `src/composition/cipher_setup.py::build_cipher` and `digest_key`. An empty or whitespace key name, an empty or
   non-hex digest key, or one under `DIGEST_KEY_MIN_BYTES` raises `ValueError` and the process exits. The check runs on
   every start, locally too (local dev uses the real KMS key).
2. It is deliberately outside the MCP build: `main()` swallows an error of that build and continues "without MCP", which
   would turn a missing key into a silently absent connector.
3. The graph hands the one cipher and the one digest key to every consumer (`src/composition/application_graph.py`,
   `src/composition/mcp_setup.py::build_mcp_components`, `src/composition/oauth_connect_setup.py`,
   `src/composition/service_container.py`, which builds the credential store).
4. The indexer has no `load_settings()`: `indexer_main.py` reads `KMS_CRYPTO_KEY` from its environment through the same
   `build_cipher` and refuses to start without it. It holds no digest key. The relay, sandbox and deep-research job get
   no key grant and build neither primitive.
5. There is no boot-time KMS round trip: a KMS outage at cold start must not take the bot down. A wrong key surfaces on
   the first encrypt or decrypt instead.

### Encrypt and decrypt

`src/adapters/gcp_kms_cipher_adapter.py::GcpKmsCipherAdapter` makes one KMS call per value, direct (no data key, no
envelope: every secret is far below the symmetric plaintext limit). `aad` is sent as additional authenticated data.
CRC32C is sent and checked both ways; a mismatch, an unverified flag or an SDK error logs once and raises. The KMS
client is created lazily on first use inside the running loop (the indexer builds its app outside any loop).

### Keyed digest

`src/domain/secret_digest.py::keyed_digest` is a hex HMAC-SHA256 of `purpose`, a NUL byte and the value. The purpose
separates uses, so one value digested for two purposes gives unrelated digests. `fingerprint` is the first 8 hex
characters of an unkeyed sha256, for log correlation only, never a lookup key.

### Per store

- **Credential save** (`src/adapters/firestore_credential_store.py::FirestoreCredentialStore.save`): in one transaction,
  the same account (`realm` plus `subject`) keeps its `credential_id`, another account gets a new random one. `update`
  rebuilds the secret and the mutable fields from its argument, returns `False` for a missing record, and never takes
  `credential_id`, `kind` or `account.*` from it. A save without an account subject is refused.
- **MCP client** (`FirestoreMCPClientRepository.save_client` / `get_client`): the read-side AAD is built from the id the
  caller requested, and a body whose `client_id` differs from its doc id raises, so a whole-record copy under another id
  neither decrypts nor reads back as the other client.
- **OAuth connect** (`src/services/oauth_connect_service.py::OAuthConnectService.start` / `complete`): the flow is keyed
  by the digest of the state and consumed once (`get_and_delete`); the PKCE verifier is sealed.
- **Voice** (`src/web/voice_webhook_app.py`, `src/web/voice_web_call_app.py`): the pending-callback and web-call records
  must give the ticket back to whoever builds a URL, so they hold ciphertext with their own AAD. The web-call record
  also holds the call-control token as a second ciphertext under a different AAD, so the two cannot stand in for each
  other. Every other ticket-derived key is a digest; the redeeming route digests the presented ticket. Logs print
  `fingerprint(ticket)`.
- **Invite codes** (`src/services/invite_code_service.py`): 16 random bytes as unpadded base32, shown grouped once at
  creation. `src/domain/invite_code.py::normalise_invite_code` runs before every digest. A lookup rebuilds the invite from
  the presented value, so `InviteCode.code` is `None` on a list. Invite codes are never logged.
- **Migration** was by reconnect, not copy: each new store started empty and the old plaintext collections are never read
  (see invariant 6).

## Invariants and traps

1. **The KMS adapter is the only `CipherPort` implementer in `src/`.** No passthrough, no-op or test cipher in any
   runtime. Enforced by AST in `tests/unit/test_secrets_at_rest_architecture.py::test_the_kms_adapter_is_the_only_cipher_in_src`.
   Tests use `tests/support/aes_gcm_cipher.py::AesGcmTestCipher`: real AES-GCM with the same AAD semantics, so a store test
   proves no plaintext reaches a field. Never write an identity cipher for a test.
2. **`aad` is keyword-only with no default**, of the form `<store>/<record key>`. A ciphertext copied to another record or
   store does not decrypt. Identity comes from the record key, never from the body of the record. Same test module.
3. **A cipher failure is never a state.** A wrong key, a tampered ciphertext, a wrong AAD or a KMS error raises (and is
   logged without values); it never becomes "not connected", a delete, a flag, a purge or a reconnect notice. This is why
   existence checks read plaintext fields only. Do not add a `try/except` that turns a decrypt error into `None`.
4. **Stores take their cipher and digest key as required keyword-only arguments.** A store built without them cannot
   exist (`test_the_mcp_store_requires_a_cipher_and_a_digest_key`, `test_the_credential_store_requires_a_cipher`). The
   indexer's store is built over its own fail-closed cipher, checked by AST in the same module.
5. **The deploy mounts the keys.** The main service mounts both (`tests/unit/composition/test_cipher_setup_fail_closed.py::test_the_main_service_deploy_mounts_both_secrets`),
   the indexer mounts `KMS_CRYPTO_KEY` (`test_both_services_ship_with_the_key` in the architecture module). A missing
   mount is a failed start, not a degraded one.
6. **No legacy branch.** No dual read, no plaintext fallback, no copy-migration. The plaintext collections
   (`oauth_credentials`, `mcp_oauth_clients`, the first invite collection) are never read, and the adapter of the first is
   gone (`test_no_code_reaches_the_old_credential_collection`). New collections carry a new name; do not reuse an old
   name for new-format data.
7. **A digest is only as strong as the value it hides.** The digest key is configuration, not a separate trust domain
   from Firestore readers (owner decision recorded in the RFC), so an offline search over a low-entropy value would
   succeed. This is why invite codes are 128 bits and digested, and why a short human-typed code must never become a
   verify-only secret here. MCP refresh
   tokens are the exception by design: unkeyed sha256 of 384 random bits, because a key adds nothing.
8. **Fingerprints are for logs only.** `fingerprint` is unkeyed and 8 characters; it is safe only because every value it
   is applied to is long and random. Never use it as a key or apply it to a guessable value.
9. **Rotating `SECRET_DIGEST_KEY` orphans every digest at once** (outstanding codes, tickets, invites stop resolving); it
   is not a routine operation. Rotating the KMS key is routine and invisible.
10. **Plaintext lives in process memory only**, for the provider call; logs carry `fingerprint` or nothing.
11. **A new secret decides its class first.** Needs to be sent onward: `CipherPort` with an AAD naming its store and key.
    Compared only: `keyed_digest` with its own purpose string, and the raw value in no field, key or log line. Adding a
    secret to an existing store changes its AAD or purpose only through a new collection or record format.
12. **The key grant is held by two identities** (main service, indexer). The relay, sandbox and job build neither
    primitive; a new consumer of the cipher there needs a new grant, which is an owner decision.

## Links

- RFC (point-in-time design, threat model and migration contract): `docs/10_rfcs/SECRETS_AT_REST_RFC.md`.
- Decisions: [`secrets_at_rest_cipher_and_digest.md`](../../04_solution_strategy/decisions/secrets_at_rest_cipher_and_digest.md)
  (cipher, digest, voice tickets, invite codes, fail-closed boot),
  [`credential_store_cutover.md`](../../04_solution_strategy/decisions/credential_store_cutover.md) (the credential store),
  [`reconnect_notice_r1.md`](../../04_solution_strategy/decisions/reconnect_notice_r1.md) (what a user sees when a
  past connection has no credential).
- Related blocks: [Gmail Email Indexing](../gmail_email_indexing/README.md) (a revoked grant),
  [Remote MCP Server](../remote_mcp_server/README.md) (the client and code stores),
  [OAuth Multi-Tenant](../oauth_multi_tenant/README.md).

## Where to start reading

- `src/ports/cipher_port.py::CipherPort` — the contract and its error promise.
- `src/adapters/gcp_kms_cipher_adapter.py::GcpKmsCipherAdapter` — the one implementation.
- `src/domain/secret_digest.py::keyed_digest` — the digest and the fingerprint.
- `src/composition/cipher_setup.py::build_cipher` and `digest_key` — the fail-closed checks.
- `src/composition/boot_setup.py::build_boot_checks` — where they run.
- `src/adapters/firestore_credential_store.py::FirestoreCredentialStore` — the biggest consumer.
- `tests/unit/test_secrets_at_rest_architecture.py` — the guards that keep the rules above true.
