# Decision: Private file storage with capability-token redirect + server-side agent re-fetch

**Date:** 2026-06-01
**Status:** Accepted (code on `feature/private-file-storage`; infra pending — see Trigger)
**Context:** Pre-public-release security hardening. The `alek-media-dev` GCS bucket was public
(`allUsers:objectViewer`) and held every user artifact — uploads, generated HTML/PDF/DOCX,
deep-research reports, and the daily **email review** (a summary of the user's inbox = PII).
Protection rested only on unguessable UUID paths (security-by-obscurity): a leaked link
(Referer, browser history, forwarded message) exposed the object forever.

**Decision:** Make the bucket private and gate every access path. Two distinct consumers, two
mechanisms — decoupled on purpose:

- **User opens a file** → the link delivered to chat is a capability route on our own domain,
  `https://<host>/f/<token>`, not a storage URL. `token` is an HS256 JWT
  (`FileAccessTokenService`, signed with `OAUTH_SESSION_SECRET`) carrying `{key, user_id, exp,
  gated}`. The `/f/<token>` route verifies it, then mints a fresh **5-minute** GCS V4 signed URL
  and 302-redirects. The bucket has no public ACL.
- **Agent re-reads a delivered file** → NOT via the external `fetch_url` (provider grounding on a
  URL). The agent uses the existing `open_file` intent with the object **key** (written to
  history instead of the URL). `FileConversionService` dispatches by ref shape: a delivered key
  (`{prefix}/{user_id}/…`) is read server-side via `MediaStoragePort.fetch` with an ownership
  check; a bare filename is a user upload via `FileStoragePort`. No external HTTP, no dependency
  on the bucket being public.

**Why these specific choices:**
- **Token TTL ≠ signed-URL TTL.** GCS V4 signatures are capped at 7 days — too short for "open
  the link a month later." So the long-lived capability is OUR JWT (30 days default; 5 days for
  email review); the short signed URL is minted per click. A signed URL is never the thing that
  lives in chat.
- **Signed URL over a still-public bucket would be a no-op** — the object is reachable by its
  plain path regardless. Privatization is what makes the token load-bearing; both ship together.
- **Agent re-fetch must not go through the provider.** `fetch_url` is fetched by the LLM
  provider's servers (Anthropic/Gemini), which have neither our cookie nor access to a private
  object — privatizing the bucket would silently break re-fetch. Server-side `open_file` keeps
  re-read working AND decouples it from the user link's TTL (the system reading its own object is
  not the same as a user opening a share link).
- **One `open_file` intent, dispatch by ref shape (not two tools).** Giving the LLM both
  `open_file` and a `read_document` would force it to guess a choice the code derives
  deterministically from the ref. The resolver picks the backend; the model never decides.
- **Keys carry `user_id` (`{prefix}/{user_id}/{uuid}-{name}`)** so ownership is verifiable. A key
  an agent carries from history is checked against the requesting user before serving — defense
  beyond uuid-obscurity. `email_review/` is `gated` (the `/f` route also requires a valid Cabinet
  cookie) and gets the 5-day TTL.
- **Funnel in the service layer, dumb adapter.** `store()` returns the object **key**;
  `DocumentDeliveryService`/`FileLinkService` mint the link. The adapter never imports the token
  service (REQ-ARCH: adapters must not depend on services).

**Rejected alternatives:**
- *Keep public bucket + short-TTL lifecycle only:* obscurity remains the only access control; a
  leaked link works until expiry with no auth. Unacceptable for PII (email review).
- *Put the signed URL directly in chat:* dies in ≤7 days (GCS cap); breaks "open it later."
- *Agent re-fetch via `fetch_url` on a token URL:* external provider can't follow our redirect to
  a private object and has no session; fragile and TTL-bound.
- *Second `read_document` intent for delivered docs:* needless tool-surface; makes the LLM choose
  what the ref already determines.

**Coverage note:** mock-at-port unit tests cannot catch a change to `store()`'s *return
semantics* (the mock IS the return value) — this is why the URL→key switch slipped past 800+
green unit tests during development. Closed with a seam integration test
(`tests/integration/test_private_file_storage_roundtrip.py`) that wires the REAL adapter +
services with only the GCS SDK mocked, and is mutation-verified to fail on a contract swap.

**Trigger to complete / revisit:** the bucket is still public until the infra step runs —
(1) `serviceAccountTokenCreator` on the runtime SA (signBlob for keyless V4 signing),
(2) remove `allUsers` from `alek-media-dev`, (3) lifecycle rules (email_review/ → 5d, else → 30d).
Order matters: grant signBlob and deploy BEFORE removing `allUsers`, or `/f` links break.
`alek-docs-dev/prod` (the public arc42 docs site) stay public by design.
