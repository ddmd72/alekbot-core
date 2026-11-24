<!-- living-doc
covers:
  src/composition/mcp_setup.py
  src/composition/mcp_sdk_oauth_provider.py
  src/composition/token_text_fastmcp.py
  src/composition/drive_mcp_setup.py
  src/composition/drive_connector_tools.py
  src/composition/drive_link_setup.py
  src/composition/voice_mcp_setup.py
  src/composition/tasks_mcp_setup.py
  src/web/mcp_consent_app.py
  src/web/mcp_dispatcher.py
  src/web/mcp_error_logger.py
  src/web/drive_link_app.py
  src/adapters/firestore_mcp_client_repository.py
  src/adapters/firestore_drive_link_store.py
  src/ports/mcp_client_repository.py
  src/ports/drive_link_store.py
  src/services/mcp_authorization_service.py
  src/services/connector_text_service.py
  src/services/drive_link_service.py
  src/services/voice_mcp_token_service.py
  src/domain/mcp.py
  src/domain/mcp_resource.py
  src/domain/connector_texts.py
  src/domain/voice_mcp_token.py
  src/domain/external_task_mcp_contract.py
  src/domain/external_task_mcp_result.py
  src/ports/media_stream_port.py
  src/adapters/http_media_stream_adapter.py
-->
# Remote MCP Server

## Purpose

This block lets an outside MCP client (claude.ai custom connectors, Claude Code, the MCP Inspector) use the owner's
exocortex as a tool provider. Claude has the better chat experience but knows nothing about the owner; the exocortex
has the memory. The connector closes that gap at conversation time, in one direction: the client calls in, the
exocortex never calls out through this block.

One authorization server fronts three protected resources, each its own FastMCP app with its own scope:

- **facts**, `/mcp`, scope `user_context`: the tool `get_user_context(queries)`. It hands the phrases to the memory
  search agent (`FactsMemoryAgent`, the same entry point Smart, Quick and Lelik use) and returns the matching facts as
  text. The connector holds no search logic.
- **drive**, `/drive-mcp`, scope `drive_read`: read-only drive tools (`search_drive`, `find_passages`,
  `read_passages`, `open_drive_file`, `list_folder`, `get_note`, `get_download_link`), all thin handlers over the chat's
  own drive operations. Nothing that writes is registered. What a call may see is each drive's *connector access*
  (set in the Cabinet; unset means nothing).
- **tasks**, `/tasks-mcp`, scope `external_tasks`: the external task queue's worker tools (`list_tasks`, `claim_task`,
  `renew_lease`, `submit_task_result`, `release_task`, `fail_task`), thin handlers over `ExternalTaskService`. It takes
  worker keys only so far: the authorization server binds its scope and mints its tokens, bound to one worker, but the
  scope is not registrable yet (`EXTERNAL_TASK_QUEUE_RFC.md` §5.4, §9 step 4). The queue itself is
  [`external_task_queue`](../external_task_queue/README.md).

Every tool call of every app reads the token's user fresh (`InactiveUserCheck`) and answers the tool error
`ACCOUNT_LOCKED_TOOL_ERROR` for a locked account (`CABINET_DEVICE_SESSIONS_RFC.md` §7.8): an access token issued before
the lock still verifies until it expires.

A third thing lives here because it shares the verifiers: the **voice token kind**, a per-call, read-only token that
lets Lelik's xAI voice session call the drive resource.

What it is not: not the Cabinet login (that is Firebase OAuth; the consent page only reads its cookie to learn who is
approving); not a generic MCP framework (the tool sets are fixed in code); not the MCP *client* side, which is
[`mcp_infrastructure`](../mcp_infrastructure/README.md).

## Owned data

Firestore, database `us-production`, collection names from `EnvironmentConfig` with the environment prefix.

**`mcp_clients`** (`mcp_oauth_clients_collection`): clients registered by Dynamic Client Registration. Doc id =
`client_id`; the body's `client_id` must equal it on read, else the read raises. Fields are those of
`src/domain/mcp.py::MCPClient`: `client_secret`, `client_name`, `redirect_uris`, `scope`,
`token_endpoint_auth_method`, grant and response types, `created_at`. The secret is `CipherPort` ciphertext with AAD
`mcp_clients/{client_id}`: plaintext in memory, because the SDK's client authentication compares the stored value to
the presented one and has no hashing hook, ciphertext at rest. A cipher failure propagates.
`client_name` is anonymous input: `normalize_client_name` turns control characters into spaces and cuts it to
`CLIENT_NAME_MAX`. Written by `MCPAuthorizationService.save_client`; read at `/authorize`, `/token` and the consent page.

**`mcp_auth_codes`** (`mcp_auth_codes_collection`): one-shot authorization codes. Doc id = the keyed digest of the
code (`SECRET_DIGEST_KEY`, purpose `mcp_code`); the raw code is stored nowhere. The document holds the client, `user_id`
and `account_id` (bound at consent), `redirect_uri`, the PKCE challenge, `resource`, `scopes`, `expires_at`, and
`worker_id`: the worker picked at consent for a grant of the tasks resource, null on every other.

**`mcp_refresh_tokens`** (`mcp_refresh_tokens_collection`): doc id = `sha256` hex of the opaque token. Fields:
client, user, account, `scopes`, `resource`, `expires_at`, `revoked_at`, and `grant_id`, the grant's stable id,
minted at code exchange and copied through every rotation and into the access token as `gid`. A token stored before
the field existed gets one at its first rotation. `worker_id` (a tasks grant's worker, copied through every rotation and
into the access token as `wid`), `granted_at` (the code exchange, copied through every rotation) and `issued_at` (this
token's own issue) serve the task queue's grants; each reads as null on a document stored before it.

The deployment runbook lists a TTL policy on `expires_at` for both token collections
([deployment](../../07_deployment/README.md)); the code never depends on it: a refresh token is checked against
`expires_at` and `revoked_at` on every read, and a code is consumed once.

**`drive_links`** (`drive_links_collection`): the drive connector's one-time download links. Doc id = the keyed
digest of the raw link id (purpose `drive_link`). Fields are those of `src/domain/drive_link.py::DriveLink`: user,
account, connection, ref tag, item, `grant_id`, `expires_at`, `reads`, `used_at`, and `purge_after` (the TTL field).
Written by `FirestoreDriveLinkStore.create` and `consume`.

**Stateless, stored nowhere:**

- *Access token*: HS256 JWT under `AuthConfig.oauth_session_secret`; claims `sub`, `account_id`, `client_id`, `scope`,
  `type=mcp_access`, `aud` (the bound resource URI), `gid`, `wid` (a tasks grant only), `iat`, `exp`.
- *Consent state*: HS256 JWT in the `?req=` of the consent URL, `aud=mcp-consent`, carrying the whole authorization
  request.
- *Voice token*: see Voice token below; its live-call record is the one stored thing, in the voice tickets store.

TTLs (access token, refresh token, code, consent request) are `AuthConfig.mcp_*_ttl`; the facts resource URI is
`MCP_RESOURCE_URI`, the drive URI is derived from it (`src/domain/mcp_resource.py::drive_resource_uri`: same host, path
`DRIVE_PATH`; no second deploy key), and so is the tasks URI (`tasks_resource_uri`, path `TASKS_PATH`).

## Flows

### Routing

`main.py` builds the facts app (`src/composition/mcp_setup.py::build_mcp_components`), then the drive app, then the
tasks app, then wraps them and the Quart app in one plain ASGI callable (`src/web/mcp_dispatcher.py::build_shared_asgi`,
`build_mcp_parent_asgi`). `mcp_owner` decides by path: tasks paths first (`/tasks-mcp`, its protected-resource
metadata), then drive paths (`/drive-mcp`, its protected-resource metadata), then the facts set (`/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, the two `.well-known` families),
everything else to Quart, including `/mcp/consent`, `/d/<id>` and `/auth/*`. The lifespan enters every built FastMCP
session manager. `McpErrorLogger` wraps each app but acts only on `/mcp` and `/mcp/`: it logs why they answered 400 or above (401 excluded), without the `Authorization`
header.

The authorization-server endpoints sit at the server root and `issuer_url` is the root, because the SDK registers its
routes at absolute paths; `resource_server_url` is `/mcp`, so the protected-resource metadata and the
`WWW-Authenticate` hint agree. `main.py` builds the drive connector in its own `try`: a failure there (a missing
connector-text token, no file conversion) leaves `/mcp` up and `/drive-mcp` falls through to Quart as a 404, and the
`/d/` route is registered only when the drive app built. The tasks app is built the same way
(`build_tasks_mcp_or_none`): no task queue, no prompt token store or a failed first load of its texts leaves it out and
`/tasks-mcp` answers 404.

### Authorization (every resource)

1. **Discovery.** An unauthenticated call gets 401 and a `WWW-Authenticate` pointing at the resource's protected-resource
   metadata; the authorization-server metadata is served by the facts app for both.
2. **Registration** (`/register`, the SDK). `MCPSdkOAuthProvider.register_client` →
   `MCPAuthorizationService.save_client`: every redirect URI must be https (http only on loopback) and its host must be
   one of `_ALLOWED_REDIRECT_HOSTS` or a subdomain. The client may register `user_context` and `drive_read`; none given
   registers both (`_facts_auth_settings`).
3. **Authorize** (`/authorize`). `build_consent_redirect` requires a registered `redirect_uri`, PKCE with `S256`, binds
   resource and scope (`src/domain/mcp_resource.py::bind_resource`, below), requires the scope to be registered for the
   client, and returns `<host>/mcp/consent?req=<consent JWT>`.
4. **Consent** (`src/web/mcp_consent_app.py::build_mcp_consent_blueprint`). No live Cabinet session (the one verifier,
   `src/web/cabinet_auth.py`: a revoked device grants nothing) redirects to `/auth/login?next=<self>`. Otherwise the page names the client and the scopes; a drive grant
   gets the drive wording (`is_drive_grant`). On approve, `issue_auth_code_for_consent` stamps the Cabinet user and
   account onto a new code and redirects to the client with `code` and `state`; deny redirects with
   `error=access_denied`. The user who is logged into the Cabinet at the moment of Approve is the identity the token
   carries.
5. **Code exchange** (`/token`). The SDK checks client secret, expiry, redirect URI and PKCE, then
   `finalize_auth_code` consumes the code and mints the access token and a refresh token with a new `grant_id`; the
   code of a user whose account is locked (`CABINET_DEVICE_SESSIONS_RFC.md` §7.8) is consumed and refused.
6. **Refresh.** `rotate_refresh_token` re-reads the user (exists, is active, has an email, whitelisted); failing that it revokes the
   presented token and answers `invalid_grant`, and a repository error fails closed. It re-binds the grant's resource
   and scopes, then revokes the old token and saves the new one (revoke first, so a replayed token cannot mint). The
   code exchange itself does not re-check the whitelist.
7. **Bearer check** on every call. `MCPSdkOAuthProvider.load_access_token` (facts) and
   `src/composition/drive_mcp_setup.py::DriveTokenVerifier` (drive) call `verify_access_token`: signature, `type`, and
   `aud` equal to the resource in canonical form (`canonical_resource`: lowercase scheme and host, no trailing slash).
   The drive verifier copies `gid` into the token it hands the tools; the facts one does not need it.

**Resource and scope binding.** `bind_resource` is applied at authorize and again at refresh. With `resource` given it
must canonicalise to a served resource's URI, every requested scope must be that resource's own, and no scope means the
resource's own. Without `resource`, `drive_read` alone selects drive, `external_tasks` alone selects tasks, an empty or
`user_context`-only request selects facts, and a mix or an unknown scope is refused. A token never carries an audience
its scope does not belong to. The service serves the tasks resource only when its `scope_of` holds the tasks scope
(composition's `SCOPE_OF` does); without it the tasks URI and scope are refused.

**A grant of the tasks resource** (`EXTERNAL_TASK_QUEUE_RFC.md` §5.4 "OAuth") is bound to one worker:
`issue_auth_code_for_consent(..., worker_id)` refuses a tasks consent without a worker and any other consent with one,
and stamps the worker on the code; the code exchange copies it onto the refresh token and into the access token as
`wid`, and every rotation keeps it. The user's flag (`external_tasks_enabled`) joins the allow rule for this resource
only (`_allow_reason(user_id, resource)`): it is checked at consent (no code), at code exchange (the code is consumed
and refused) and at every refresh rotation (the presented token is revoked). The service does not check that the
worker is the user's: the consent page does, and every tool call does through the queue.

**The Cabinet's grant list** (`src/web/cabinet_external_workers.py`, EXTERNAL_TASK_QUEUE_RFC §5.6). The External workers
card lists each worker's live grants through `MCPAuthorizationService.worker_grants` (one entry per `grant_id`: the
registered client's name, `granted_at`, and the live token's `issued_at` as "last refresh"), read by
`list_worker_grants` (three equality filters: `user_id`, `worker_id`, `revoked_at` null; expiry checked in code).
Revoke (`DELETE /api/external-workers/<worker_id>/grants/<grant_id>`, resolved among that worker's own live grants)
calls `revoke_grant`, which revokes every unrevoked token of the grant in one transaction: the grant's next refresh
fails, and its live access tokens are refused on their next tasks call by `grant_allowed`. Removing a worker calls the
queue's `revoke_worker` and then `revoke_worker_grants`: the route calls both services (REQ-ARCH-22). `main.py` builds the
MCP components before the Cabinet blueprint so the Cabinet gets this service; a failed build leaves the bot up without
MCP and the card lists no grants.

### Facts tool call

`src/composition/mcp_setup.py::build_mcp_components` registers one tool, `FACTS_TOOL`. The handler takes the verified
`AlekAccessToken` from the request (none or another type is a `ToolError`), sets `RequestContext(user_id, account_id)`,
asks `memory_search(user_id)` for that user's `FactsMemoryAgent`, refuses an agent whose `account_id` differs from the
token's, and calls `search_facts(queries, path="connector")`. The result is `format_search_result`, plain text; zero
results is the success text `NO_RECORDS_TEXT`, never an error and never a hint to retry.

### Drive tool call

`src/composition/drive_mcp_setup.py::build_drive_mcp_app` builds a `TokenTextFastMCP` (a resource server only; the SDK
forbids an auth provider and a token verifier on one instance, hence two apps). `_register_tools` wraps each tool in
`_Caller.run`, which for one call:

1. takes user, account and token kind from the verified token;
2. loads the connector texts (`ConnectorTextService.texts()`; `ConnectorTextsUnavailable` is an `isError` result);
3. for a voice token refuses a tool outside its claim, before any read (`tool_permitted`);
4. runs the operation under `RequestContext`, the connector audience (`connector_audience()`; this is what makes drive
   access checks apply the Cabinet's connector access instead of the chat's rules) and a `CALL_TIMEOUT_S` `wait_for`;
5. composes one text result (`compose_connector_result`), cut by `cap_result`, in `content[].text` only, and writes one
   span and one `[DriveMCP]` line that never carry the query, the question, or a link URL.

A ref that is not a `drive:` ref, an item outside connector access and a missing item answer the same neutral refused
line (no existence oracle). `get_note` is registered only when the note service and the item reads exist. Every
registered tool must match `src/domain/drive_connector_contract.py` exactly (`_check_contract` raises at build).

**Texts come from tokens.** Every word the drive tools say to a client (server instructions, tool and parameter
descriptions, result lines) is in two Firestore prompt tokens, `MCP_DRIVE_TOOL_TEXTS` and `MCP_DRIVE_RESULT_TEXTS`
(`TOOL_TEXTS_TOKEN`, `RESULT_TEXTS_TOKEN`), never inline. `ConnectorTextService` loads and validates them (risk level
`safe` or `low`), caches them for the `ConnectorTextService` `ttl_s` and, when a refresh fails, keeps serving the last good value. A first load that
fails raises, posts one ops alert and the drive app is not built. `ConnectorTexts.parse_bodies` fails on a missing
key and on a tool text over `MAX_TOOL_DESCRIPTION_CHARS`; an unknown key is dropped with one warning, so a token
uploaded ahead of a deploy does not break the running revision. `TokenTextFastMCP.list_tools` fills the descriptions on
every listing; the server instructions are served from the texts last read.

### Tasks tool call

`src/composition/tasks_mcp_setup.py::build_tasks_mcp_app` builds a third `TokenTextFastMCP`, a resource server only.

- **Verifier** (`TasksTokenVerifier`): a credential with the worker-key prefix (`WORKER_KEY_PREFIX`) goes to
  `ExternalTaskService.resolve_key` and then `ExternalWorkerUserGate` (the whitelist), the REST endpoint's two checks;
  anything else is refused. A refused key is no token, so the SDK answers `401` at the door, where REST answers `403`
  for a locked, switched-off, revoked or not-whitelisted key. The token handed to the tools has kind `WORKER_KEY`, the
  worker id, scope `external_tasks`, no expiry, and in its `token` field the key's stored digest, never the key. The
  facts and drive verifiers know nothing of keys, so a worker key is `401` there.
- **One call** (`_TaskCaller.run`): the principal from the token; the account lock (`ACCOUNT_LOCKED_TOOL_ERROR`); the
  texts; the worker's limits, the REST endpoint's own buckets (`calls`, plus `claims` or `submits`, per `user:worker`),
  so both surfaces count against one budget; the service operation, which re-checks the worker and the flag. One span
  `mcp.tasks.<tool>` and one `[TaskMCP] tool=… user=<8> worker=<fingerprint> status=… ms=…` line; never a task text, a
  result, a reason, a key or a header.
- **Results** (`src/domain/external_task_mcp_result.py`): text only. A claim prints code-written provenance first (task
  id, created time, lease id and end, who wrote the brief), then the title, then the brief as written. A claim that
  finds nothing is a success; every refusal, an empty id and a rate limit are `isError` with their line, which names
  the refusal code; `not_enabled` and `worker_revoked` share one neutral line. Only `list_tasks` is listed read-only
  (`TokenTextFastMCP(read_only_tools=...)`).
- **Texts** come from `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`, loaded and validated as the drive texts; the
  key sets are `src/domain/external_task_mcp_contract.py`.

### One-time download link

`get_download_link(file_ref)` (and an open that is withheld as an image or an untranscribed recording, which prints
the link after its withheld line) calls `DriveLinkService.issue`: a token without `gid` is refused; the ref is resolved
and the file checked under the connector audience; a folder is refused with its own line. The URL is
`<scheme>://<host of MCP_RESOURCE_URI>/d/<raw id>`; only the keyed digest of the id is stored.

`GET|HEAD /d/<id>` (`src/web/drive_link_app.py::create_drive_link_blueprint`) is anonymous: the 128-bit id is the
capability. `DriveLinkService.open` consumes one read first, then re-checks the grant
(`MCPAuthorizationService.grant_allowed`: an active refresh token with that `grant_id`, and the whitelist), then
re-resolves the ref under the connector audience (the connection must be the one the link was issued on), then the
access check, then fetches the first chunk. The bytes stream through the service, never a redirect. HEAD runs every
check and consumes nothing. Every refusal is the same 404.

### Voice token

A second access-token kind for Lelik's own calls (`LELIK_VOICE_MCP_RFC` §5), for the drive resource only.

- **Key.** HS256 under `VOICE_MCP_SIGNING_KEY`, never the OAuth secret (`voice_mcp_setup.signing_key` refuses a short
  key, the OAuth secret, the public dev placeholder, and a missing key while the knob is on). Header `kid`
  `VOICE_MCP_KID`, `aud` = `voice:` + the canonical resource URI, a `tools` claim and a `cid` claim (keyed digest of the
  call ticket).
- **Mint.** `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`, at `/voice/session-config`
  only, for the plain companion call on xAI (`voice_mcp_eligible`), with the `VOICE_MCP_TOOLS` knob on
  (`voice_mcp_tools_enabled`). `VoiceMcpTokenService.mint` re-reads the whitelist (a refused user gets the call without
  tokens and an alert), writes the live-call record `voice_mcp_call:{cid}` with the token's TTL, then signs the token
  in memory; nothing stores a token.
- **Verify.** Both resources route on the unverified header: `kid=voice-mcp` goes to the voice verifier only, anything
  else to the OAuth verifier only (`is_voice_token`). The voice verifier checks key, type, audience, expiry, claim
  shapes and the live-call record (its user and account must match the claims). Granted tools are
  `VOICE_ALLOWED_TOOLS` narrowed by the claim; a claim never adds a tool. The facts resource has no voice token and
  refuses every one.
- **At the tools.** A voice call runs with the drive audience *unset* (it reads what chat reads; the Cabinet's
  connector access does not apply) and its result is cut at `VOICE_RESULT_MAX_CHARS`.
- **Revoke.** `/voice/submit-transcript` deletes the live-call record whenever the voice token service exists
  (whatever the knob); a relay that dies leaves it to the TTL.

## Invariants and traps

- **Accept liberally, advertise strictly** (the tool schema). `tests/unit/composition/test_mcp_setup.py` asserts the
  emitted `get_user_context` schema has no `null` type, only `queries` required, no `minLength`, `maxLength`,
  `maxItems` or `pattern`, and no `additionalProperties: false`. FastMCP validates with pydantic, so an advertised
  constraint turns a recoverable input into a failed call and costs the client a round-trip. Bounds live in the
  description and are enforced by `normalize_queries` (a `BeforeValidator`): a bare string is one phrase, a JSON array
  in a string is parsed, items are stripped and empties dropped, only the first `QUERIES_MAX` are kept (the rest counted
  in the log). `isError` is only for authentication, a backend fault, or no phrases at all.
  `decisions/mcp_tool_schema_liberal_input.md`.
- **claude.ai caches `tools/list` at registration.** A changed facts tool schema or description needs the connector
  removed and re-added. A client that caches `tools/list` needs the same re-add to see new drive or tasks texts, which the server reads from tokens on each listing; a changed tasks tool parameter needs it too.
- **No `Mount`.** Starlette's `Mount("/mcp")` does not match an exact `POST /mcp` and strips the prefix, which breaks
  the SDK's absolute routes; that is why the dispatcher is plain ASGI. `/mcp/consent` reaches Quart only because the
  facts matcher is exact on `/mcp` and `/mcp/`: a new FastMCP path under `/mcp/` would shadow it. The tasks and drive
  checks run first because the facts metadata test is a prefix match (`tests/unit/test_mcp_dispatch.py`).
- **DNS-rebinding protection** must list the public host. FastMCP's default allowlist is loopback, which on Cloud Run
  rejects every request as `Invalid Host header`; `build_transport_security` allows the public host from
  `MCP_RESOURCE_URI` plus loopback, protection stays on. Every app runs `stateless_http`: Cloud Run may send the next
  request to another instance.
- **A token's audience matches its scope.** `bind_resource` runs at authorize and at refresh
  (`tests/unit/services/test_mcp_resource_binding.py`, `tests/unit/composition/test_drive_mcp_auth_seam.py`); the
  verifier compares audiences in canonical form, so tokens minted with the raw configured URI still verify.
- **One-shot code and one-exchange refresh are transactions.** `consume_auth_code` reads and deletes, and
  `revoke_refresh_token` reads and revokes, each in one Firestore transaction and the revoke reports whether the token
  was active: of concurrent exchanges exactly one wins, the others get `invalid_grant`
  (`test_mcp_oauth_refusals.py`). A link fetch in the gap between revoke and save sees no active token and is refused;
  the client asks again.
- **Rotation re-checks the user; the grant check does too.** The refresh and `grant_allowed` both re-read user, email
  and whitelist; the log carries the reason, never the email (`test_mcp_refresh_whitelist.py`).
- **`/revoke` is served** (RFC 7009; `_facts_auth_settings` enables `RevocationOptions`). The SDK authenticates the
  client and revokes only that client's own refresh token; another client's token answers 200 and changes nothing
  (`test_mcp_token_revocation.py`). The access token is a stateless JWT and stays valid until `exp`: revoking
  stops refreshing, not the current access token.
- **The consent page is hostile-input territory.** The client name comes from anonymous registration; every value is
  HTML-escaped and the page carries a CSP of `default-src 'none'` with only inline style, `frame-ancestors 'none'`,
  `X-Frame-Options`, `nosniff` and `Referrer-Policy: no-referrer` (its URL holds the consent token)
  (`tests/unit/web/test_mcp_consent_rendering.py`).
- **Secrets at rest.** The client secret is ciphertext, codes are stored by keyed digest, refresh tokens by `sha256`,
  link ids by keyed digest. Changing `SECRET_DIGEST_KEY` invalidates codes and links in flight
  (`decisions/secrets_at_rest_cipher_and_digest.md`).
- **Link limits are an owner ruling.** `LINK_MAX_READS` is 1 within `LINK_TTL_S`; raise it (never above 3) only after a
  client's pre-fetch has been measured live; `tests/unit/domain/test_drive_link.py` pins both bounds. The headers
  (`SAFETY_HEADERS`, a sandbox CSP, `no-store`, inline only for PNG, JPEG, GIF, WebP and PDF, anything that runs script
  served as an attachment of `application/octet-stream`) are in `src/domain/drive_link.py`.
- **The drive connector's read-only, no-oracle behaviour is by absence and by one refusal line**, not by a flag: no
  write tool is registered, and "outside access", "not a drive ref" and "missing" are indistinguishable
  (`tests/unit/composition/test_drive_mcp_tools.py`, `test_drive_mcp_access_acceptance.py`).
- **Voice tokens never meet the OAuth path.** Each verifier refuses the other's tokens on key, type and audience; a
  forged `kid` changes only who refuses it. No log line carries a token, a ticket or an `Authorization` header; voice
  lines carry `client=voice` (`test_voice_mcp_token_service.py`, `test_voice_mcp_token_hardening.py`).
- **`main.py` swallows MCP build errors**, so anything that must fail closed (the cipher, the digest key, the voice key
  and knob) is built and checked in `build_application_graph`, not in these setup functions.

## Links

- [`REMOTE_MCP_SERVER_RFC.md`](../../10_rfcs/REMOTE_MCP_SERVER_RFC.md): the design record of the facts connector (its
  first tool shape is superseded).
- [`DRIVE_MCP_CONNECTOR_RFC.md`](../../10_rfcs/DRIVE_MCP_CONNECTOR_RFC.md): the drive connector (tools, token texts,
  connector access, the link, wide results).
- [`LELIK_VOICE_MCP_RFC.md`](../../10_rfcs/LELIK_VOICE_MCP_RFC.md): the voice token kind and the xAI session consumer.
- [`EXTERNAL_TASK_QUEUE_RFC.md`](../../10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md) §5.4: the tasks resource.
- [`mcp_tool_schema_liberal_input.md`](../../04_solution_strategy/decisions/mcp_tool_schema_liberal_input.md),
  [`secrets_at_rest_cipher_and_digest.md`](../../04_solution_strategy/decisions/secrets_at_rest_cipher_and_digest.md).
- Deployment keys and the TTL runbook: [`07_deployment/README.md`](../../07_deployment/README.md) (`MCP_RESOURCE_URI`,
  `VOICE_MCP_SIGNING_KEY`, `VOICE_MCP_TOOLS`).
- Memory side of the facts tool: [`memory`](../memory/README.md). Drive side: [`user_drive`](../user_drive/README.md).

## Where to start reading

- `src/web/mcp_dispatcher.py::mcp_owner`: which app owns a path.
- `src/composition/mcp_setup.py::build_mcp_components`: facts app, authorization provider and the one tool.
- `src/services/mcp_authorization_service.py::MCPAuthorizationService`: every OAuth rule (allowlist, consent JWT, code,
  rotation, token verify, grant check).
- `src/domain/mcp_resource.py::bind_resource`: the scope-to-resource rule.
- `src/composition/drive_mcp_setup.py::build_drive_mcp_app`: the drive app and `_Caller.run`, the shape of one call.
- `src/services/drive_link_service.py::DriveLinkService`: issue and the fetch re-checks.
- `src/services/voice_mcp_token_service.py::VoiceMcpTokenService`: mint, verify, revoke.
- `src/composition/tasks_mcp_setup.py::build_tasks_mcp_app`: the tasks app, its key verifier and `_TaskCaller.run`.
