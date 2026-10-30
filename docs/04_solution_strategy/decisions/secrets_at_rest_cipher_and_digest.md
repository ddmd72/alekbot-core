# Decision: stored secrets — direct KMS cipher with record-bound AAD, keyed digest for verify-only values

**Date:** 2026-10-29
**Status:** Accepted (S1 built; first user: the MCP store)
**Context:** `docs/10_rfcs/SECRETS_AT_REST_RFC.md` (v2). The app persists secrets of its own in Firestore. Firestore's disk
encryption does not protect them from an identity that may read Firestore (leaked key, over-broad binding, export, a bug that
dumps a collection). S1 builds the primitives and applies them to the MCP store; later slices apply them to the OAuth credentials.

## Decision

- **`CipherPort`** (`src/ports/cipher_port.py`): `encrypt(plaintext, *, aad)` / `decrypt(ciphertext, *, aad)`, base64 out. `aad` is
  required and has the form `<store>/<record key>`.
- **`GcpKmsCipherAdapter`**: one Cloud KMS `ENCRYPT_DECRYPT` call per value, **direct encrypt, no envelope** (every secret is far
  below the 64 KiB plaintext limit). CRC32C is sent and checked both ways (`plaintext_crc32c`, AAD crc, the `verified_*` flags,
  `ciphertext_crc32c`); a mismatch, a false flag or an SDK error logs once and raises, never returns a degraded value.
- **AAD = record identity.** A ciphertext copied under another record or store does not decrypt. The MCP store goes one step
  further for a whole-record copy: the read-side AAD is built from the id the caller *requested*, and a body whose `client_id`
  differs from its doc id is refused. Identity comes from the record key, never from the body.
- **Keyed digest for verify-only values** (`src/domain/secret_digest.py`): hex HMAC-SHA256 over `purpose || 0x00 || value` under
  `SECRET_DIGEST_KEY` (hex, at least 32 bytes). MCP authorization codes are stored under their digest as the doc id, with no raw
  code in any field; lookup digests the presented value. `purpose` separates uses.
- **Invite codes (S5)** are the second verify-only value. A new code is 16 random bytes (128 bits) as 26 unpadded base32
  characters, shown grouped `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX` once, at creation (the Cabinet says "copy it now"). Typed input is
  normalised before every digest (`domain/invite_code.normalise_invite_code`: upper case, spaces and dashes removed). The store
  (`FirestoreInviteCodeRepository`, `digest_key` required) uses `keyed_digest(key, "invite", normalised)` as the doc id and stores
  no `code` field, only `hint` (the last 4 characters); a lookup rebuilds the invite from the presented value, so `InviteCode.code`
  is optional and `None` on a list. The list shows type, state, expiry and the hint. No log line carries the code. New collection
  `{prefix}invite_codes_v2`; the old `domain_invite_codes_v1` is never read (outstanding 9-hex codes stop working).
- **MCP refresh tokens stay unkeyed sha256**: 384 random bits gain nothing from a key.
- **Fail closed in the composition root.** `build_application_graph` builds the cipher and the digest key first
  (`composition/cipher_setup.py`). They are not built inside the MCP build, whose errors `main.py` swallows ("continue without
  MCP"): a missing key must be a failed start, not a silently absent connector. The check runs on every start, locally too.
- **Lazy KMS client**: created on first use inside the running event loop (the indexer builds its app synchronously, outside any loop).
- **No boot-time KMS round trip.** A KMS outage at cold start would take the whole bot down for a feature one connector uses.
  Misconfiguration surfaces at once anyway: the connector re-add encrypts a client secret and the first `/token` decrypts it
  (all registered claude.ai clients are confidential).
- **New collection** `{prefix}mcp_clients`; the plaintext `mcp_oauth_clients` is never read (no dual read, no legacy branch).

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Firestore CMEK alone | hides nothing from an API reader; fixed at database creation |
| Envelope encryption with a cached DEK | extra layer and cache lifetime for values of tens of bytes; direct encrypt is one call |
| Passthrough cipher for local dev | a plaintext path in production code; local dev uses the real key (the developer's identity is granted on it) |
| Dual read of old plaintext and new ciphertext | keeps plaintext paths alive; migration is by reconnect instead |
| Boot-time KMS probe | couples cold start to KMS availability (above) |

## Consequences

- Two required settings in `load_settings()`: `KMS_CRYPTO_KEY`, `SECRET_DIGEST_KEY`. Tests use an in-memory AES-GCM cipher with the
  same AAD semantics (`tests/support/`), never an identity function.
- Dependencies: `google-cloud-kms`, `google-crc32c`.
- Every request that stores or reads a client secret costs one KMS call.
- Rotation: automatic every 90 days; idle records decrypt under their old version, so no version is disabled or destroyed.

## Triggers to revisit

- A secret near the 64 KiB limit (a session blob): add a data-key layer for it only.
- Further narrowing: the key grant is already held only by the main service's and the indexer's identities (the relay, sandbox and job get none); a per-secret or per-purpose key is the next step.
- KMS latency or cost shows up in traces on a hot path: cache decrypted values per request, not across requests.
- A provider-side need for a second cipher (another cloud): the port already isolates it.
