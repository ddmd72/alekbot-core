# RFC: Per-User Secrets at Rest — Application-Layer Encryption via CipherPort

**Status:** PROPOSED
**Date:** 2026-07-20 (revised 2026-07-22)
**Owner:** AI Engineering
**Milestone:** Security — Credential Storage Hardening

**Related:** `ports/oauth_credentials_port.py` + `adapters/firestore_oauth_credentials_adapter.py`
(the credential store being hardened); `adapters/firestore_mcp_client_repository.py` +
`domain/mcp.py` (second plaintext secret store — §3.5); TELEGRAM_CHANNEL_READER_RFC (first *new*
consumer — reuses this mechanism for the MTProto session, §7, and is **blocked on this RFC shipping**).

---

## 1. Problem Statement

Per-user access credentials are currently stored **in Firestore as plaintext**, in two independent
places.

**1. OAuth credentials.** `FirestoreOAuthCredentialsAdapter._to_firestore()` writes `access_token`
and `refresh_token` as raw strings (`firestore_oauth_credentials_adapter.py:33-42`), for providers
`gmail`, `google_tasks`, and `microsoft_todo`. The `refresh_token` is a long-lived key to the user's
entire mailbox / task account.

**2. MCP OAuth client secrets.** `MCPClient.client_secret` is stored raw in
`{env}_mcp_oauth_clients` (`domain/mcp.py:20-32`). This one is *deliberate* and documented — the
`mcp` SDK authenticates clients via `hmac.compare_digest(client.client_secret, presented_secret)`
with no hashing hook. That rationale is sound but **narrower than it reads**: it rules out *hashing*,
not *encryption* (§3.5).

**Firestore encrypts at rest by default — but that does not address this threat.** Google's default
at-rest encryption protects against someone stealing the physical disk; it does **nothing** against an
actor who is *authorized to read Firestore*: a leaked service-account key, an over-broad IAM binding, a
bug that dumps a collection, an export to BigQuery/GCS, or a compromised admin. Any of those yields
every user's live tokens in cleartext today.

**Desired outcome:** sensitive credential fields are encrypted at the **application layer** before they
touch Firestore, using a managed KMS key. A Firestore reader sees only ciphertext; obtaining a usable
token requires *both* Firestore read access *and* a separate KMS decrypt permission. The mechanism is a
single reusable primitive so every per-user secret (OAuth tokens now, Telegram session later) is
protected the same way.

---

## 2. Key Decisions

### 2.1 Encrypt at the application layer, keep the datastore

The fix is **not** "move secrets into Secret Manager." Secret Manager is the wrong fit for OAuth
credentials specifically, on three axes:

- **Write frequency.** `save_credentials` runs after *every* token refresh — roughly hourly per active
  Gmail user (see the port docstring). Secret-Manager-per-secret would mint a new secret version every
  hour per user → version churn, cost, and write-quota pressure.
- **Queryability.** The port must answer `list_users_by_provider` / `list_connected_providers`
  (fan-out for indexing and renewal jobs). Secret Manager has no query — you would list all secrets and
  parse names.
- **Structure.** A credential is a record (token / refresh / expiry / scopes / email), not a single
  opaque string.

Firestore already fits all three (structured, queryable, high-write). So we **keep Firestore** and
encrypt only the *secret fields* within each document. Non-secret, queryable fields (`user_id`,
`provider`, `email_address`, `token_expiry`, `scopes`) stay plaintext so existing queries keep working.

### 2.2 One primitive: `CipherPort` (KMS-backed), datastore chosen per access pattern

The generalization is a single **`CipherPort`** — `encrypt(str) -> str` / `decrypt(str) -> str` — that
any per-user-secret adapter composes. The *encryption mechanism* is shared and uniform; the *datastore*
is whatever fits each secret's access pattern:

- OAuth credentials → Firestore + `CipherPort` on token fields (this RFC).
- Telegram MTProto session → Firestore + the **same** `CipherPort` (TELEGRAM RFC, later).

This supersedes the Telegram RFC's earlier "Secret-Manager-per-user" storage choice. Once a
`CipherPort` exists for OAuth, the crypto code is already paid for, so reusing it for Telegram is free
and gives **one encryption mechanism across all per-user secrets** — consistency chosen over having two
storage backends. The Telegram RFC §2.5/§5 are updated to point here.

### 2.3 Direct KMS encrypt (no envelope/DEK)

Secrets here are small — OAuth tokens and a StringSession are well under KMS's **64 KiB** symmetric
plaintext limit. So the adapter calls KMS `encrypt`/`decrypt` **directly** on the field; no
data-encryption-key / envelope layer is needed. Envelope encryption stays a documented future option
if we ever encrypt large blobs.

---

## 3. Architecture

### 3.1 Port (`ports/cipher_port.py`)

```python
class CipherPort(ABC):
    @abstractmethod
    async def encrypt(self, plaintext: str, *, aad: Optional[str] = None) -> str:
        """Encrypt a UTF-8 secret. Returns opaque base64 ciphertext safe to store in a string field.
        `aad` (additional authenticated data) binds the ciphertext to its context — it is NOT stored
        in the ciphertext and MUST be supplied identically on decrypt."""

    @abstractmethod
    async def decrypt(self, ciphertext: str, *, aad: Optional[str] = None) -> str:
        """Inverse. Raises on tamper, wrong key, or mismatched `aad`."""
```

- **String in / string out.** Secrets are strings; base64 ciphertext stores cleanly in Firestore.
- **AAD** binds each ciphertext to *where it lives* — e.g. `f"{user_id}:{provider}:refresh_token"`.
  KMS cryptographically ties decrypt success to the same AAD, so a ciphertext cannot be copy-pasted
  from one user/field to another inside the DB (defends against confused-deputy / row-swap tampering).
  Cheap, high-value defense-in-depth.
- The port is **storage-agnostic and secret-agnostic** — no mention of KMS, OAuth, or Telegram.

### 3.2 Adapter (`adapters/gcp_kms_cipher_adapter.py`)

- Wraps `google.cloud.kms_v1.KeyManagementServiceAsyncClient`.
- `encrypt` → `client.encrypt(name=<key>, plaintext=…, additional_authenticated_data=…)` →
  `base64(response.ciphertext)`. `decrypt` inverse.
- **Key resource name from config**, never hardcoded (SECRETS RULE): `KMS_CRYPTO_KEY =
  projects/<p>/locations/<l>/keyRings/<ring>/cryptoKeys/<key>`, added to `src/config/settings.py`.
- **Rotation is transparent.** KMS ciphertext references the key (not a pinned version); decrypt works
  across rotations while old versions are retained, and the next `save` naturally re-encrypts under the
  new primary. No re-encrypt job required for rotation.
- **New dependency:** `google-cloud-kms` (not currently vendored).

**Local dev:** production **fails closed** — missing `KMS_CRYPTO_KEY` in a deployed environment
refuses to start (mirrors `config/auth.py:167` "Refusing to start"). Local dev may wire an explicit
`PassthroughCipherAdapter` (identity encrypt/decrypt) in `composition/`, selected *only* when no
project/KMS is configured — never silently active in prod.

### 3.3 OAuth adapter migration (`FirestoreOAuthCredentialsAdapter`)

Inject `CipherPort`. Consumers do **not** change — `get_credentials` / `save_credentials` keep their
signatures; encryption is entirely internal to the adapter (the hexagonal payoff: the port already
isolates every caller from storage details).

- `_to_firestore`: encrypt `access_token` + `refresh_token`
  (AAD `f"{user_id}:{provider}:{field}"`); write a schema marker `enc_v: 1`. Other fields unchanged.
- `_from_firestore`: **dual-read for zero-downtime migration** — `enc_v` present → decrypt;
  absent (legacy doc) → read plaintext. New writes are always encrypted, old reads still work.

### 3.4 Backfill (`scripts/migrations/encrypt_oauth_credentials.py`)

One-time, idempotent: stream docs lacking `enc_v`, encrypt tokens, write back with `enc_v: 1`; skip
already-encrypted docs. Runs against live Firestore with the `us-production` database. After the
backfill is verified, a follow-up removes the legacy plaintext read branch in `_from_firestore`
(closing the transitional window).

### 3.5 MCP client secrets (`FirestoreMCPClientRepository`)

`MCPClient.client_secret` is stored plaintext by design, because the `mcp` SDK compares the presented
secret against the stored value with `hmac.compare_digest` and offers no hashing hook
(`domain/mcp.py:20-32`).

**That rationale rules out hashing, not encryption.** `CipherPort` is decrypt-on-read: the adapter
returns the raw secret to the SDK exactly as today, and the SDK's comparison is unaffected. The
plaintext-at-rest exposure disappears at no behavioural cost. So this is in scope, and it is the
cheapest possible consumer — same dual-read pattern, AAD `f"{client_id}:mcp_client_secret"`, and the
`domain/mcp.py` docstring is corrected to say *not hashed* rather than *not protected*.

**Explicitly deferred — MCP authorization codes.** `{env}_mcp_auth_codes` stores the code itself as
the document ID, in the clear. Note the asymmetry inside the same repository: refresh tokens are
stored as `sha256(token_value)`, auth codes are not. The exposure is materially smaller — codes are
single-use, expire in 10 minutes, and an attacker holding one still needs the PKCE `code_verifier`
(only the challenge is stored) plus the client secret. The correct fix is also *different* from this
RFC's mechanism: hash the document ID to match the refresh-token treatment, rather than encrypt a
field. Deferred as its own small change; trigger to revisit is any widening of MCP beyond its current
dev-only, experimental status.

---

## 4. Security & Threat Model

What application-layer encryption adds over Firestore's default at-rest encryption:

| Control | Effect |
|---------|--------|
| Ciphertext in Firestore | A DB dump / export / broad read yields unusable ciphertext, not tokens |
| Two-key requirement | A usable token needs **both** Firestore-read **and** KMS `useToDecrypt` — separate IAM, separate grantees |
| Least privilege | `cloudkms.cryptoKeyVersions.useToDecrypt` on the runtime SA only, distinct from Firestore access |
| Audit | Cloud Audit Logs record every decrypt with caller identity — anomalous credential access is detectable |
| AAD binding | Ciphertext cannot be relocated across users/fields inside the DB |
| In use | Plaintext exists only transiently in instance memory during a provider call; never logged, never in `prompt_content` BigQuery, tokens on the log-redaction deny-list |
| Rotation | KMS key rotation + re-encrypt-on-write; CMEK/HSM available for stronger key custody |

Blast-radius summary: a leaked service-account key that can read Firestore but lacks KMS decrypt is
**useless** for stealing credentials — the exact gap that exists today.

---

## 5. Scope / Non-Goals

- **In scope:** `CipherPort` + KMS adapter; encrypting OAuth `access_token`/`refresh_token` + backfill
  (§3.3–3.4); encrypting MCP `client_secret` (§3.5).
- **Not encrypting non-secret fields** (`user_id`, `provider`, `expiry`, `scopes`) — needed plaintext
  for queries. `email_address` is PII but not an access credential — left plaintext in MVP; encrypting
  it is a documented possible extension (it is not queried by the current port).
- **MCP authorization codes — explicitly deferred** with rationale in §3.5 (different fix: hash the
  doc ID, not encrypt a field).
- **No consumer changes** — Gmail/Microsoft adapters, the `mcp` SDK token handler, indexing, and
  renewal jobs are untouched. Encryption is internal to each adapter.
- **No envelope/DEK layer** — payloads < 64 KiB (§2.3).
- **Other collections** (facts, sessions, etc.) are out of scope — this RFC is per-user *credentials*.

**Completeness note.** The two stores in §1 are the result of an audit of what this system persists
that grants access. MCP refresh tokens are already `sha256`-hashed and need no change. If a third
per-user secret appears later, it composes `CipherPort` — that is the point of the primitive.

---

## 6. Rollout Order

The mechanism ships **first**, as a standalone primitive, before any new feature depends on it:

1. `CipherPort` + `GcpKmsCipherAdapter` + `PassthroughCipherAdapter` (local) + unit/wire tests + KMS
   key provisioning + `settings.py` wiring.
2. Migrate `FirestoreOAuthCredentialsAdapter` to encrypt tokens (dual-read) + backfill script; run and
   verify the backfill on live data.
3. Remove the legacy plaintext read branch once backfill is confirmed.
4. Migrate `FirestoreMCPClientRepository.client_secret` (§3.5) — same dual-read + backfill shape, much
   smaller collection. Sequenced after OAuth so the pattern is settled on the higher-value store first.
5. **(Later, separate work)** Telegram `FirestoreTelegramSessionRepository` composes the same
   `CipherPort` — the mechanism is already proven in production by then.

Steps 1–4 are a **hard prerequisite** for TELEGRAM_CHANNEL_READER_RFC, by owner decision: secrets are
stored correctly before a full-account MTProto session is added to the system.

---

## 7. Relationship to the Telegram RFC

TELEGRAM_CHANNEL_READER_RFC originally proposed Secret-Manager-per-user for the MTProto session. With
this RFC accepted, that is superseded: the Telegram session is stored Firestore + `CipherPort`, uniform
with OAuth credentials. The Telegram RFC's §2.6 and §5 reference this mechanism instead of Secret
Manager. Telegram is the **validation case** for reuse, implemented only after this mechanism is live —
it should require *zero* new crypto code, only a new repository adapter composing `CipherPort`.

---

## 8. Testing

- **Port substitution:** consumers/tests use `AsyncMock(spec=CipherPort)` — no real KMS.
- **KMS adapter wire test** (`tests/unit/adapters/`, mock at the KMS SDK boundary, not the port — per
  `ADAPTER_WIRE_TESTING.md`): `encrypt` calls KMS with the right key + AAD and base64-encodes;
  `decrypt` round-trips; mismatched AAD → raises.
- **Round-trip property:** `decrypt(encrypt(x, aad), aad) == x`; `decrypt` with wrong AAD raises.
- **OAuth adapter:** save encrypts tokens + sets `enc_v`; get decrypts; **dual-read** — a legacy doc
  without `enc_v` still reads (plaintext branch) until backfill. Queryable fields stay plaintext
  (assert `list_users_by_provider` still works over ciphertext docs).
- **Backfill script:** idempotent (re-run is a no-op on `enc_v` docs); converts a plaintext doc.
- **MCP client repo:** `save_client` writes ciphertext; `get_client` returns the raw secret so
  `hmac.compare_digest` still matches (assert an end-to-end client-auth comparison passes against an
  encrypted-at-rest client); dual-read for legacy plaintext clients.
- **Redaction:** token and client-secret plaintext never appear in emitted log records.
- **Fail-closed:** prod composition without `KMS_CRYPTO_KEY` refuses to start; local passthrough is
  only selected when explicitly unconfigured.
