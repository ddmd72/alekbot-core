# RFC: Drive MCP connector — the drive's read tools for an outside MCP client

**Status:** Proposed (2026-11-03). Three owner questions open (§13); the text below follows the recommended answer of
each and marks the places they touch.
**Owner:** AI Engineering
**Milestone:** External interop. Extends the facts connector ([REMOTE_MCP_SERVER_RFC.md](REMOTE_MCP_SERVER_RFC.md),
building block [`remote_mcp_server`](../05_building_blocks/remote_mcp_server/README.md)) with a second protected
resource on the same authorization server.
**Builds on:** [MULTI_DRIVE_RFC.md](MULTI_DRIVE_RFC.md) (connections, mounts, access policy §11.3, Cabinet §11.7),
[GOOGLE_DRIVE_RFC.md](GOOGLE_DRIVE_RFC.md) (account-wide drives, outer folders),
[USER_DRIVE_INDEXING_RFC.md](USER_DRIVE_INDEXING_RFC.md) (search §25, passages §28, open bound §29),
[DRIVE_SPECIALIST_RFC.md](DRIVE_SPECIALIST_RFC.md) (`DriveOperations`, the tools this RFC re-exposes),
[DRIVE_FILE_NOTES_RFC.md](DRIVE_FILE_NOTES_RFC.md) (`get_note`).
**Peer:** [EXTERNAL_FILES_RFC.md](EXTERNAL_FILES_RFC.md) (the open path: `FileConversionService.open` → `DocumentService`).

Notation: **D-n** settled owner decisions (§2), **E-n** engineering decisions (§12, each with its reason), **Q-n** open
owner questions (§13). Code cites are `main` @ `d13c3f04`; SDK cites are MCP Python SDK 1.27.0 (`requirements.txt:64`
pins `mcp==1.27.*`) under `site-packages/mcp/`. *Inference* marks what was not seen in code or a primary source. Client
facts (claude.ai, Claude Code) come from the provider's connector and MCP documentation as of 2026-11-02; where they are
community reports, the slice that depends on them measures them. Examples are invented.

---

## 0. In one paragraph

The owner's drives become readable from claude.ai (and Claude Code) through a second MCP connector at
`https://<host>/drive-mcp`, separate from the facts connector at `/mcp`: its own protected resource, its own scope
`drive_read`, its own consent text, one shared authorization server. No model of ours runs: the client's model calls
six read tools — `search_drive`, `find_passages`, `read_passages`, `open_drive_file`, `list_folder`, `get_note` — that sit
directly on the chat specialist's `DriveOperations`, plus `get_download_link` in the last slice. Nothing that writes is
registered, the handlers see a read-only facade, and the connector's `DriveOperations` has no notifier. Every word the
client reads about the tools (descriptions, parameter texts, result guidance lines) comes from two Firestore prompt
tokens, re-read every 60 s and served per `tools/list`, so wording changes by token upload with no deploy and no
reconnect. Each drive gets a Cabinet setting, **Connector access** (default: nothing), and content outside it is
unreachable by every tool and by the link.

## 1. Problem and the bar

Today the drive is reachable only through Smart → the drive specialist in chat. The owner wants to ask about their files
from claude.ai, where the client's own model does the reasoning. The facts connector proves the transport and auth;
the drive adds three risks the facts connector does not have: (1) private file text goes to an outside client that may
also hold sending tools; (2) the drive is writable from chat, so any write path reachable from the connector is a
destructive one; (3) some chat read paths have side effects (posting to chat, paid model reads).

**The bar.** A token for one connector is useless on the other. Nothing the connector can call writes to the drive,
notes, chat or memory. With connector access at its default, the connector sees no drive at all. Chat's drive
behaviour, prompts and tool texts are byte-identical after every slice (existing tests prove it, none edited).
A connector call costs no model call of ours (Q1).

## 2. Settled owner decisions

| # | Date | Decision |
|---|---|---|
| D1 | 2026-10-28 | The connector asks, the drive returns results; a separate resource, scope and consent from the facts connector. |
| D2 | 2026-11-02 | No agent of ours in between: the drive's read tools are exposed directly; no model of ours runs, so there is no tier or model setting. |
| D3 | 2026-11-02 | No evals, no specialist change: the specialist's prompt and tool texts stay byte-identical; the search rules a client needs are duplicated for the connector, not moved. |
| D4 | 2026-11-02 | Connector wording lives in its own Firestore prompt tokens (tool and parameter descriptions, result lines); it changes by token upload, not deploy. A missing token fails loudly; there is no inline default. |
| D5 | 2026-11-02 | Every `tools/list` is answered from the tokens at request time (a short cache is fine); no design where a wording change forces a reconnect. Client-side caching is the client's limitation. |
| D6 | 2026-11-02 | Folder access per drive in v1: a Cabinet setting next to the search scope; default nothing; the widest choice is what the bot sees on that drive; content outside the chosen folders is unreachable by every tool and the link. |
| D7 | 2026-11-02 | The deleted recordings/notes search is not offered. No call cap; one log line per tool call. A 30-day grant, the whitelist re-checked at refresh (Q2: sliding or absolute). An own one-time 5-minute link, last slice; its reader is the client's model: a plain GET consumes and streams, HEAD is free. |

If a less capable client is ever connected, a "delegate to the drive specialist" tool can be added; nothing here
blocks it.

## 3. The auth seam: one authorization server, two protected resources

### 3.1 Today

One FastMCP instance is both the authorization server (AS) and the resource server for `/mcp`
(`src/composition/mcp_setup.py:243-260`), scope `user_context` (`:46`), stateless HTTP (`:259`). Access tokens are HS256
JWTs, 1 h, checked for `aud == MCP_RESOURCE_URI` (`src/services/mcp_authorization_service.py:362-386`); the minted
audience is `resource or default` with no check of `resource` (`:444`), and the SDK passes `resource` through unchecked
(`mcp/server/auth/handlers/authorize.py:204`). Client registration (DCR) accepts and defaults to `user_context` only
(`mcp_setup.py:251-255`), and the SDK refuses an authorize scope the client did not register
(`mcp/shared/auth.py:74-82`). Refresh tokens live 30 days and every rotation re-arms the 30 days
(`mcp_authorization_service.py:121`, `:318-358`). The dispatcher sends `/mcp`, the AS paths and every
`/.well-known/oauth-protected-resource*` path to the facts app by prefix (`main.py:571-596`, `:578`) and runs only the
facts app's session manager in its lifespan (`:582-591`). The consent page (`src/web/mcp_consent_app.py:107-116`) has
one text, about memory; it requires a Cabinet login, which the whitelist gates at sign-in
([cabinet_oauth_whitelist_gate.md](../04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md)).

### 3.2 Shape

The SDK forbids `auth_server_provider` and `token_verifier` on one instance (`mcp/server/fastmcp/server.py:219-220`) and
builds AS routes at the server root, so the facts instance stays AS + resource server and a **second FastMCP instance is
resource-server-only**: `token_verifier` set, `streamable_http_path="/drive-mcp"`,
`AuthSettings(issuer_url=<host>, resource_server_url=<host>/drive-mcp, required_scopes=["drive_read"])`,
`stateless_http=True`. The SDK then serves its protected-resource metadata (PRM) at
`/.well-known/oauth-protected-resource/drive-mcp` (`mcp/server/auth/routes.py:190-205`) with `scopes_supported =
["drive_read"]` and the same issuer, and wraps the endpoint in `RequireAuthMiddleware` with the required scope
(`server.py:1005-1014`). The drive resource URI is derived from `MCP_RESOURCE_URI` (same scheme and host, path
`/drive-mcp`): no new deploy key (E4).

**Dispatcher.** `/drive-mcp`, `/drive-mcp/` and the exact PRM path `/.well-known/oauth-protected-resource/drive-mcp` go
to the drive app, matched **before** the facts prefix match; everything else is unchanged. The lifespan runs both
session managers (E5). If the drive app fails to build (a missing token, §5.3), `/drive-mcp` answers 404 and the facts
connector runs as before; the AS fixes below live in the shared service, so a swallowed drive build
(`main.py:527-532`) never leaves drive audiences mintable unchecked.

### 3.3 AS changes (shared service)

1. **`resource` validated.** It must equal one of the two canonical URIs (a trailing slash normalised); anything else is
   refused at `/authorize`, never minted. claude.ai sends the canonical connector URL on authorize and token requests.
2. **Scope bound to resource, both ways (E1).** `user_context` ↔ `/mcp`, `drive_read` ↔ `/drive-mcp`. `resource` given:
   requested scopes must all belong to it; none requested → the resource's own. `resource` absent: derived from the
   requested scope (`drive_read` → drive), else the facts default. Claude Code may omit `resource` (a community report)
   but takes its scopes from the PRM, so the drive connector still gets a drive token. A mix is `invalid_scope`.
3. **DCR registers both scopes (E2).** `valid_scopes` and `default_scopes` become `user_context drive_read`.
   Registration grants nothing; the grant is bound at `/authorize`.
4. **Verification per resource.** `verify_access_token(token, *, resource)` checks `aud` against the given resource and
   `type`; the drive verifier passes the drive URI, the facts provider the facts URI. The SDK middleware checks the
   scope. A facts token on `/drive-mcp` fails `aud`; a drive token on `/mcp` fails `aud` and scope.
5. **Consent text per resource.** The consent JWT already carries `resource` and scopes, bound at `/authorize`; the
   page picks its text by the bound scope (`drive_read` → the drive text; proposed wording in §13). The facts text is
   unchanged.
6. **Whitelist re-check at refresh (E3).** `rotate_refresh_token` reads the user's email and the whitelist entry before
   minting; a removed email answers `invalid_grant` and revokes the token. With 1 h access tokens a removal takes effect
   within an hour. It applies to both resources (the service is shared): strictly tighter than today for facts.
7. **Grant lifetime.** As today, 30 days from the last refresh (sliding), pending **Q2**.
8. **Grant id (E13, slice 4).** A `grant_id` minted at code exchange (or at the first rotation of a refresh token stored
   without one), copied through every rotation and into the access token (`gid`); the link stores it and checks at
   fetch that the grant still has an active refresh token.

## 4. The tool set, read-only by absence

### 4.1 Tools

| Tool | Calls | Connector-specific |
|---|---|---|
| `search_drive` | `DriveOperations.search` (`src/infrastructure/drive_operations.py:278`) | drive names come from `list_folder("/")`, not the specialist's `drive_state` |
| `find_passages` | `.find_passages` (`:313`) | no cross-call ledger: each call prints its passages (`shown` empty) |
| `read_passages` | `.read_passages` (`:334`) | no "one page per request" rule |
| `open_drive_file` | `.open` (`:440`) | `drive:` refs only; no native declaration; media pre-check (§6.2) |
| `list_folder` | `.list_folder` (`:350`) | `/` lists only drives the connector sees, with their connector access (§7.4) |
| `get_note` | `DriveNoteService.get` (`src/services/drive_note_service.py:191-207`) | access check added (§7.5) |
| `get_download_link` (slice 4) | the one-time link (§8) | any file kind; there is no `get_recording_link` |

**Not registered:** the seven write tools, the ten note writers, `report_to_orchestrator`,
`search_deleted_recordings_and_notes` (`src/domain/drive_tool_texts.py:120-166`, `src/domain/drive_note_tool_texts.py:178`).

### 4.2 Read-only three ways

1. Nothing that writes is registered.
2. The handlers hold a facade exposing exactly `search`, `find_passages`, `read_passages`, `list_folder`, `open`, a
   subset of `DriveOperations.READ_METHODS` (`drive_operations.py:222-223`, whose classification a test already pins);
   `fetch_stored`, `search_deleted`, `preflight` and `folder_path` stay out (E6).
3. The connector's `DriveOperations` is built with `notification=None` and no localization, so no branch can post to
   the owner's chat or send a receipt (`_receipt`, `:1181-1200`, already no-ops without them).

### 4.3 Side effects of the read paths, from code

Search, passages and page reads only read: every repository call of `src/services/drive_search_service.py` is a `find_*`,
`get_*` or count. `get_note` reads the store alone. Listing reads the provider and the notes
(`drive_operations.py:350-435`). The query embedding of search and `find_passages` is the only external call
(`drive_search_service.py:1006`, `:1048`, `:1637`). `open` has four effects in chat; the connector removes each:

- **(a) A video is posted to the chat** (`drive_operations.py:1062-1080`; a stored video `:750-772`). Without a notifier
  the branch still downloads the video before failing (`:1064`). → The media pre-check answers a line before any
  download (§6.2).
- **(b) A non-`drive:` ref opens chat uploads, delivered documents, `workspace:` and `skill:` files**
  (`:445-446` → `fetch_stored`, `:655-695`): content outside the drive and any folder choice. → Refused before any call.
- **(c) A recording with no stored transcript** is downloaded and then refused as unsupported: the document service
  transcribes only chat uploads (`src/services/document_service.py:246-249`). → The pre-check answers "not transcribed
  yet" with no download. (The concept this RFC comes from expected a paid transcription here; that path is gone.)
- **(d) A scan or an image opened as text runs the paid model reader.** With no native declaration a PDF goes parser →
  reader when its text layer is missing, thin or garbage, and an image goes straight to the reader
  (`src/domain/document_extraction.py:32`, `:101-116`; `document_service.py:180-225`), an LLM run billed to the ledger
  (`src/composition/document_service_setup.py:34`), up to 200 pages (`src/domain/drive_index_params.py:77`). → Pending
  **Q1**; recommended: the connector's read request skips the reader link (as `CALLER_PREFETCH` already skips audio,
  `document_service.py:248`) and answers a line pointing to the file's passages (the index read the scan once already)
  or, from slice 4, its download link.

No "mark read" and no history write exists on these paths. The only internal write left is a provider token refresh
storing a rotated token, as in chat (*inference*).

### 4.4 Context outside a chat turn

Reads use only `user_id`; `account_id`, `origin_channel_id` and `origin_platform` feed receipts and video posts
(`drive_operations.py:1071-1076`, `:1181-1200`), which are gone here. The handler takes `user_id`/`account_id` from the
verified access token, as the facts tool does (`mcp_setup.py:315-316`), passes `{"user_id", "account_id", "run_origin":
"mcp:drive"}` and no `native_formats`, and wraps each call in `RequestContext` (`mcp_setup.py:325`), the connector
audience (§7.2), and a 120 s `wait_for`; `open` gets `deadline_at = now + 120 − OPEN_MARGIN_S`
(`src/domain/document_read.py:33`), as the specialist computes it (`src/infrastructure/drive_tools.py:232-235`) (E8).
120 s sits under claude.ai's documented 240 s per tool call and under Claude Code's 2-minute auto-backgrounding.

Composition builds one connector `DriveOperations` from the graph's singletons (conversion, drive, search, notes), as
`src/composition/user_agent_factory.py:883-891` does for chat, minus the notifier: no per-user agent build. The
specialist's per-run guards — the identical-search skip, the passages ledger, one page per run
(`drive_tools.py:196-270`) — cost only the client's tokens when absent; none is a safety rule on a read path.

Input is liberal, as the facts tool's (`src/composition/CLAUDE.md`,
[mcp_tool_schema_liberal_input.md](../04_solution_strategy/decisions/mcp_tool_schema_liberal_input.md)): flat strings,
defaults, normalising validators; an empty required ref is a correctable error line, not a protocol error.

## 5. Token-backed wording

### 5.1 How tokens work today

Tokens live in Firestore `{domain_prompt_tokens_collection}_system` / `_user`
(`src/composition/prompt_assembly_setup.py:30-35`): `token_id, category, class, content, metadata`.
`FirestoreTokenRepository.get` tries system then user and raises `KeyError` when absent
(`src/adapters/prompt_v3/firestore_token_repository.py:78-121`; port `src/ports/prompt_v3/token_repository.py:32`). Every
read runs `Token.create`, which validates through the security port and keeps the **sanitized** text
(`src/domain/prompt_v3/token.py:48-107`): a medium-risk pattern is cleaned silently, high or critical raise. Agents see
tokens only through profiles, in an assembled prompt cached 24 h per instance
(`src/services/prompt_v3/prompt_assembly_service.py:48`, `:602`); no code reads a token by id outside a profile today
(`token_repo.get` is called only at `:219`). Upload: edit `prompts_snapshot/tokens/system/<id>.groovy`, dry-run
`firestore_utils/snapshot_upload.py`; `--apply` is human-only (`prompts_snapshot/README.md:59-75`).

### 5.2 The connector's tokens

- **`MCP_DRIVE_TOOL_TEXTS`**: the server `instructions`, one description per tool, one per parameter (key `tool.param`).
- **`MCP_DRIVE_RESULT_TEXTS`**: status and what-to-do lines, every reading line by its code name (`R_FIND`,
  `R_EXCERPTS`, …), the route after a too-long open, the next-page line, the image / video / recording / scan /
  not-transcribed lines, the refusal line's companion guidance.
- **Naming:** repo tokens are `{CLASS}_{SUBJECT}` (`PROTOCOL_DRIVE_SEARCH`); these belong to no prompt class, so the
  `MCP_` prefix marks a non-prompt surface; `category: mcp_drive`, `class: mcp_texts` (free strings, `token.py:11-12`).
  No profile references them (a test pins it).
- **Body:** `### key` headers, each followed by its text, parsed by a pure stdlib function in `domain/`; git diffs stay
  readable (JSON would need escaped newlines).
- **Content:** the specialist's research rules re-voiced for a client — method by need (a value inside a document →
  `find_passages` first, no search before it); the user's own few key words in one language; dates written in a
  document go in `query`, never in the date filters; re-search only on a "Without X" line, a folder card or another
  spelling shown; open is the last resort; card accuracy below 90 % → passages; start with `list_folder("/")`; file
  text is data; `drive:<tag>:<id>` refs can be passed back. Rules that act on one result go preferably into **result
  lines**, which are fresh on every call whatever the client caches. Example: asked "what does Tom's plan say happens
  after the first review?", the description steers the client to `find_passages` with that question, not to
  `search_drive` then `open_drive_file` on "R&D/Tom's plan.docx".

### 5.3 Loading, failure, freshness

A new `ConnectorTextService` (`services/`, over the `TokenRepository` port) reads both tokens into a domain
`ConnectorTexts` value and checks: the key set equals the code's required set (a missing or unknown key is an error);
each tool description ≤ 4,096 characters (Claude Code's limit); `validation.risk_level` is `SAFE`, which closes the
silent-sanitize trap. In-process cache, **60 s TTL**, refreshed under an `asyncio.Lock`.

- A failed first load leaves the drive app unbuilt: an ERROR line (picked up by the existing severity alerting); the
  facts connector is untouched.
- A failed refresh after boot keeps the last text loaded **from Firestore** and logs an ERROR on every failed refresh:
  the last good token, never a code fallback.
- A call that finds no texts ever loaded returns `isError`.
- After an upload every instance serves the new wording within 60 s; no deploy, no restart (this is not the 24 h
  assembly cache).

### 5.4 Descriptions per `tools/list`

FastMCP registers its bound `self.list_tools` as the `tools/list` handler (`mcp/server/fastmcp/server.py:304`) and builds
the list from the tool manager on every call (`:315-329`). A subclass `TokenTextFastMCP(FastMCP)` in `composition/`
overrides `list_tools`: for each registered tool it takes the name and schema (types, required), deep-copies the
schema, fills `description` and every `properties[p].description` from `ConnectorTexts`, and adds
`annotations=ToolAnnotations(readOnlyHint=True)` and `_meta` (§6.1). The registered tool objects are never mutated
(shared across concurrent requests). On a cache miss in `tools/call` the low-level server refreshes its tool cache
through the same handler (`mcp/server/lowlevel/server.py:476-490`), so a call also needs loaded texts — consistent with
fail-loud. Server `instructions` come from `create_initialization_options` (`lowlevel/server.py:164-188`), which
stateless mode calls per request (`mcp/server/streamable_http_manager.py:185-199`); overriding it on the low-level
server makes them token-backed too.

**`listChanged` is not declared (E9).** Declaring is one line (`NotificationOptions(tools_changed=True)`,
`lowlevel/server.py:217`). Emitting is impossible: with `stateless_http=True` (needed on multi-instance Cloud Run) every
POST runs a fresh session and no stream outlives its request, so `send_tool_list_changed`
(`mcp/server/session.py:477`) reaches nobody. Declaring a capability we never emit could make a client that honours it
(Claude Code) trust notifications over re-fetching. The SDK speaks protocol 2025-11-25 (`mcp/types.py:27`): no `ttlMs`.
**Client side:** claude.ai web and Claude Code re-fetch per conversation or session; desktop and mobile reportedly keep
the first list (community reports); the repo's own note says claude.ai caches at registration
(`mcp_tool_schema_liberal_input.md:119-120`). Slice 2 measures it once.

### 5.5 Texts addressed to the specialist or Smart

Chat keeps today's constants byte for byte (existing tests pin them); the connector gets token text through one seam
per surface:

| Today's text (audience) | Where | Connector seam |
|---|---|---|
| open "for the orchestrator… You do not see what is opened"; passages "delivered to the orchestrator" | `drive_tool_texts.py:131-146` | tool descriptions from tokens |
| `DRIVES` "as drive_state lists them"; `OPEN_REF`/`FILE_REF` "the commission or the state block" and chat/workspace/skill refs; `PASSAGES_QUESTION` "language of the commission"; `PASSAGES_FILE_REFS` | `drive_tool_texts.py:50-51`, `:64-68`, `:87-96` | parameter descriptions from tokens |
| `ROUTE_SPECIALIST` "… report now", `ROUTE_ORCHESTRATOR` "commission work_with_drive_files" | `src/domain/drive_open_texts.py:18-24` | the refusal names no tool; its caller appends a route (`drive_tools.py:183`); the connector appends `ROUTE` from tokens |
| `PAGE_NEXT` "when the commission asks for it" | `src/domain/drive_excerpt_render.py:76-77`, printed by `page_line` (`:243-248`) from `render_excerpts` (`:190`) | `render_excerpts` and `page_line` gain a `page_next` argument defaulting to today's constant; the connector re-renders the call's typed result (`DriveOutcome.excerpts`) with the token text (E10) |
| reading lines with "commission" / "ask for the file's passages" / "the drive assistant" (`R_EXCERPTS`, `R_PAGED`, `R_TRUST`, `R_PASSAGE`, `R_ACCURACY_VALUE`, `R_ACCURACY_IMAGE`, `R_NOT_READ`), `UNFINISHED_LINE` | `src/domain/drive_answer_texts.py:21`, `:71-107`, `:137-138` | `reading_lines` splits into `reading_keys(blocks)` (the trigger logic of `src/domain/drive_answer.py:183-229`, unchanged) and a text map: chat maps to today's constants, the connector to token texts |
| stand-ins "you do not see the text", `ONE_PAGE`, `NOTE_PREFIX`, write lines | `drive_answer_texts.py:32-63`, `drive_open_texts.py:45-48` | unused (the client sees bodies; no report, no writes) |
| vision "is attached. You can see and analyse it directly"; video "sent to the user as a file" | `drive_operations.py:1082-1091`, `:1077-1080` | never reached: no native declaration, the media pre-check (§6.2) |

Audience-neutral and reused as is: search cards, coverage and "Without X" lines (`src/domain/drive_search_texts.py`),
passage head lines, the cut and refusal lines of the open bound (`drive_open_texts.py:13-16`, `:26-33`), the untrusted
wrappers and `REFERENCE_REMINDER` (`src/domain/untrusted_text.py:87-91`, `:139`). A new pure
`compose_connector_result(outcome, texts)` in `domain/` builds each result: the status line, the reading lines selected
by `reading_keys` on the one block, the block text (passages: the call's own render and the language line). No
"Specialist note", no `drive_context`, no delivery items.

## 6. Output size and binaries

### 6.1 Size

Existing bounds: search text ≤ 16,000 characters (`RESULT_CHARS_MAX`, `drive_index_params.py:142`); passages ≤ 24,000
plus folder notes ≤ 4,000 (`:275`, `:284`); `read_passages` ≤ 10 passages (`:279`); open ≤ 60,000
(`OPEN_MAX_CHARS`, `src/domain/document_limits.py:39`), and an indexed longer file is refused with a route before any
download (`drive_operations.py:1028-1036`); listings cut with "… more items not shown" (`:388-389`). The worst case with
reading lines is about 65,000 characters, on open. So `open_drive_file` carries `_meta["anthropic/maxResultSizeChars"] =
100000` (`tool(meta=…)`, `mcp/server/fastmcp/server.py:452`), since Claude Code files a result over 50,000 characters
to disk unless the tool declares its own limit; every tool has a hard 100,000-character guard that cuts at a line and
appends a coded "how to continue" line (it should never fire; claude.ai's documented maximum is about 150,000). Known
degradation: 60,000 Cyrillic characters can pass Claude Code's 25,000-token default, and the client then gets a file
path. Acceptable.

### 6.2 Binaries and media

Nothing but text: no image, PDF or `resource_link` blocks; everything goes in `content[].text` (resource links and
embedded PDFs are reported ignored by Anthropic clients, and `structuredContent` handling on claude.ai is undocumented).

- **Native vs text** is already the consumer's declaration: `context["native_formats"]` (`src/domain/native_format.py:26`,
  read at `drive_operations.py:1025-1027`); none means text only (`src/services/file_conversion_service.py:206-207`).
  The connector declares none, so a PDF opens as parser text under the same open bound. No new switch is needed for
  this.
- **Media pre-check** (one keyword on `DriveOperations.open`, default off = today's behaviour; E7): after the item's
  metadata read and the access check, before any download —
  an **image** answers a token line ("its description is in its search card or passages; the picture itself comes by
  `get_download_link`");
  a **video** answers a link line, never a post;
  a **recording** returns its stored transcript (`_transcript_of`, `:958-968`) or a "not transcribed yet" line;
  a **scan PDF** (pending Q1) gets no model read: the read request skips the reader and the result is the
  "read its passages" line.
- Until slice 4 the image/video/recording lines say the file itself cannot be fetched through the connector; slice 4
  swaps in the link.

## 7. Connector access per drive

### 7.1 The setting

`DriveConnection.connector_access` (`src/domain/drive_connection.py:54-73` gains the field): `None` — the default, also
for every stored document without the field — means **nothing**; `whole` means everything the drive's search scope
covers; or a tuple of folder ids inside that scope. It is kept apart from `scope_folder_ids` because an empty scope
folder list means "everything" on an area feed (`src/domain/drive_access_policy.py:119-120`), the one fail-open trap
here.

### 7.2 The audience

A domain `ContextVar` `CURRENT_DRIVE_AUDIENCE` (precedent `src/domain/run_deadline.py:14`), **set by the MCP tool
handler** around each call and by the link route around each download; spawned tasks inherit it. It is read at the
existing choke points: `UserDriveService.access` / `check_access` (`src/services/user_drive_service.py:168-174`,
`:200-223`) and `DriveSearchService._scope_admit` (`src/services/drive_search_service.py:470-487`). `decide` and the
new rule take it as an argument and stay pure.

### 7.3 The rule

Under the connector audience a read passes only if `in_scope(search scope)` **and** `in_connector(access)` hold on the
same chain. `in_connector`: `None` → refused; `whole` → true; folders → the chain contains one of them. `reached_root`
is not required, so folders inside a Google shared drive or a shared chain (`UserDrivePort.outer_folders`,
`src/ports/user_drive_port.py:125-129`) work (E11). An unknown ancestor or a cut chain that holds no connector folder →
refused. `requires_ancestry` answers true for every read under the audience with folders chosen. The connector's index
admit **fails closed**: chat's admit fails open on a failed capabilities lookup by design (`drive_search_service.py:477-479`)
and stays so.

### 7.4 Hidden drives and listings

A drive whose connector access is `None` is removed wherever the mount table, the primary, the live connection list
and ref resolution are read under the audience (`src/services/drive_connection_service.py:225-305`, E12), so
`list_folder("/")`, search, "also on" lines, `drive:` refs with its tag and whole-drive notes never show it. Under the
audience the primary is the primary if visible, else the first visible mount, else "no drive". The `/` listing shows
each visible drive's connector access, not its write folder or search scope (`drive_operations.py:393-435` prints both
in chat; E10b). Listing a drive whose root lies outside its connector access shows the connector folders, as G11 does
for the search scope (`user_drive_service.py:360-385`). Listing above an allowed folder is refused.

### 7.5 `get_note`

`DriveNoteService.get` reads the store with no access check (`drive_note_service.py:191-207`). The connector handler
resolves a `drive:` ref and runs `check_access(READ)` under the audience first; it refuses `drive_note:` refs (deleted
or archived items have no live chain, and D7 leaves deleted items out); a whole-drive note is allowed only for a drive
the connector sees.

### 7.6 Cabinet

A **Connector access** block under the drive's "What the bot sees" block (strings in `src/web/static/cabinet.html`,
four locale dictionaries, e.g. `drive.sees_title` `:1925`): "Nothing" (default) / "Everything the bot sees on this
drive" / chosen folders through the existing picker, limited to the search scope (outer folders included,
`src/web/user_cabinet_app.py:1817-1833`); route `PUT /api/drive/connections/<id>/connector-access`, next to `…/scope`
(`:1727`). A scope change that would strand a connector folder is refused, as the write folder is protected today
(`SCOPE_LEAVES_WRITE_FOLDER_LINE`, raised by `DriveConnectionService.set_scope`, `drive_connection_service.py:366-389`).
Setting every drive to "Nothing" is the owner's instant kill switch: access is read fresh on every call (Q3).

### 7.7 Refusals

One neutral code line that never says whether the item exists (proposed wording in §13). The chat lines
(`OUTSIDE_SCOPE_LINE` and the rest, `drive_access_policy.py:60-82`) are not shown to the connector.

## 8. One-time download link (slice 4)

The provider's file URL (what chat's recording link returns, `drive_operations.py:1042-1050`) opens only for someone
signed in to the owner's account. Ours: `https://<host>/d/<id>`, a 128-bit random opaque id (not a JWT, so the link does
not look like a credential to the client's fetch filter) with server-side state: user, connection, item, grant id,
`expires_at` (+5 min), `used_at`, `purge_after`. It is not a reuse of `/f/<token>` (`src/web/file_access_app.py`:
GCS-only, long-lived, a JWT).

- **GET:** a transactional consume → the grant still has an active refresh token and the whitelist holds → a fresh
  connection read → `check_access(READ)` under the connector audience (the folder is checked at fetch, not at issue) →
  `media_source` (`src/ports/user_drive_port.py:120-124`) → stream.
- **HEAD** answers without consuming. If a client burns the use before reading (its fetch tool's caching and retry
  behaviour is undocumented; measured in the slice), the fallback is "N reads within 5 minutes", not a page.
- **Headers:** `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`;
  `Content-Disposition: attachment` by default, inline only for images and PDF, never `text/html` inline on our domain.
- **Large files:** a 302 to the provider's pre-signed URL when `MediaSource.header_free`, if streaming hits Cloud Run
  limits (verified in the slice).
- Issued by `get_download_link(file_ref)` (any kind) and by the §6.2 image/video/recording lines; one link per call.
  Example: asked for invoice `FA-0137` from the locksmith, the client finds it with `search_drive` and gets one link
  that opens once within five minutes.

## 9. Observability, money, injection

**Observability.** One span per call, `start_span("mcp.drive.<tool>", {tool, status, chars, ms})`
(`src/utils/telemetry.py:281`), and one line `[DriveMCP] tool=… user=<8> status=… chars=… ms=…`. **The query, question
or path is never logged** (the drive rule: `src/agents/drive_agent.py:186-188`; `[DriveSearch]` logs counts only,
`drive_search_service.py:611-619`). The facts tool logs its query (`mcp_setup.py:318-323`); the drive tool does not copy
that. `DriveOperations._call` already logs the op and the ref or folder label (`drive_operations.py:787`), as in chat.
Link issue, consume and refuse lines carry no path. There are no `prompt_content` rows: no LLM call.

**Money.** No model call of ours (Q1). Search and `find_passages` cost one query embedding each plus Firestore reads;
open and list cost provider downloads and listings; `get_note` and `read_passages` are reads. The embedding adapter
writes no billing (`src/adapters/gemini_embedding_adapter.py`), so connector spend is invisible in the ledger, exactly
like chat's embeddings today. With no call cap, heavy use costs embeddings plus Firestore reads.

**Injection.** The risk is private file text, and text the owner did not write, reaching a client that may hold sending
tools. Ours: nothing registered can write; no notifier; only the chosen folders, default nothing; hidden drives
absent; bodies in nonce wrappers; result lines say file text is data; links single-use, 5 minutes, re-checked at fetch;
one log line per call. Not ours: what the client does with text or links. The owner's levers are narrow folders and not
pairing this connector with sending tools in one client project.

## 10. v1 shape

1. A resource-server-only FastMCP at `/drive-mcp`, its own PRM, scope `drive_read`; one AS validating `resource` and
   binding scope both ways; DCR registering both scopes.
2. Consent text per resource; 30-day grant (Q2); whitelist re-check at refresh; a stable grant id (slice 4).
3. Six read tools (+ `get_download_link` last); no write, note-write, report or deleted-search tool registered.
4. A read-only facade over a connector `DriveOperations` with no notifier; open takes `drive:` refs only, no native
   declaration, the media pre-check, no model reader (Q1).
5. Tokens `MCP_DRIVE_TOOL_TEXTS` + `MCP_DRIVE_RESULT_TEXTS`: key set and safe validation checked, 60 s cache.
6. Per-request `tools/list` from tokens through an overridden `list_tools`; token-backed `instructions`; no `listChanged`.
7. Chat byte-identical: the `reading_keys` split, a `page_next` argument, the connector's own route line.
8. One result per call in `content[].text`; `readOnlyHint`; `maxResultSizeChars` on open; a 100k guard.
9. Connector access per drive, default nothing, on search (index and provider), passages, open, listing, notes and the
   link; unknown ancestor → deny; hidden drives absent.
10. The Cabinet "Connector access" block, its route, four locales.
11. The one-time link `/d/<id>`: GET consumes and streams, HEAD free, re-checked at fetch, `no-store`, attachment by default.
12. One span and one log line per call, no user words; a 120 s per-call timeout.

## 11. Slices

Vertical, each deployable alone; the connector shows no drive content until slice 3 removes the knob.

1. **Auth seam.** `resource` validation, scope binding, DCR scopes, per-resource verification, the RS-only app with one
   stub tool, the dispatcher and PRM split, both session managers, consent text per resource, the whitelist re-check at
   refresh. *Done:* claude.ai adds the drive connector and lists the stub; a facts token gets 401 on
   `/drive-mcp` and a drive token 401 on `/mcp` (tests both ways); an unknown `resource` gets no code; facts unchanged
   (its tests green, none edited); a de-whitelisted user's refresh answers `invalid_grant`.
2. **Read tools on whole drives, token-backed.** The facade, the handlers, the media pre-check, `drive:`-only open, no
   native declaration and no reader, `ConnectorTextService`, both tokens drafted in `prompts_snapshot/`, the `list_tools`
   override, the `reading_keys` split, the `page_next` seam, the composer, span and log, size meta and guard; gated by a
   deploy knob (the connector sees nothing unless set) until slice 3. *Done:* chat suites green with no test edited; an
   offline test shows the snapshot tokens cover every key; one live call per tool from claude.ai and from Claude Code; a
   token upload changes a description in a new conversation within 60 s with no deploy (this also measures claude.ai's
   list caching); a missing key leaves the app unbuilt and logs an ERROR.
3. **Connector access.** The field, the audience variable, the rule, the fail-closed index admit, mount hiding, the
   `get_note` check, the Cabinet block and route; the knob removed. *Done:* fake-provider acceptance for nothing /
   folders / whole / a folder moved out / an unknown ancestor / a shared-drive folder; live, a file outside the chosen
   folder (for example the 12 Oak Street bathroom invoice when only "R&D" is chosen) is neither found, opened, listed,
   quoted nor note-read.
4. **One-time link.** The grant id, the store, the route, `get_download_link`, the image/video/recording lines, the
   headers. *Done:*
   the link opens once; HEAD does not consume it; it dies at 5 minutes, when its folder leaves connector access, and
   when the grant is revoked.

## 12. Engineering decisions

- **E1** Scope bound to resource both ways, `resource` absent → derived from the scope. *Why:* Claude Code may omit
  `resource`; a token must never carry an audience its scope does not belong to. *Rejected:* "absent → facts default"
  alone (the drive connector would not work from Claude Code).
- **E2** DCR accepts and defaults to both scopes. *Why:* the SDK checks authorize scopes against the registered client
  scope; registration is not a grant. *Rejected:* separate client registries per resource (two AS, which the SDK's
  root-absolute routes make awkward).
- **E3** The whitelist re-check at refresh lives in the shared service and covers both connectors. *Why:* one rule, and
  a de-whitelisted user should lose memory access too. *Rejected:* drive-only (two refresh paths).
- **E4** The drive resource URI is derived from `MCP_RESOURCE_URI`. *Why:* no new deploy key (the deploy-substitution
  trap).
- **E5** Both session managers run in the dispatcher lifespan; the drive PRM path is matched before the facts prefix.
- **E6** The facade exposes exactly five methods, a subset of `READ_METHODS`. *Why:* defence in depth; `preflight`
  carries specialist state lines and `fetch_stored` opens content outside the drive.
- **E7** No native declaration instead of a vision switch; one media pre-check keyword on `open`. *Why:* the open path
  already decides native from the consumer's declaration; the pre-check avoids downloads whose result is a line.
- **E8** A 120 s `wait_for` per call and `open(deadline_at=now + 120 − OPEN_MARGIN_S)`.
- **E9** `listChanged` not declared: stateless HTTP cannot deliver it.
- **E10** One trigger logic for reading lines (`reading_keys`), two text maps; `page_next` on `render_excerpts` /
  `page_line`; the connector re-renders from the typed result. **E10b** The connector's `/` listing shows connector
  access. *Why:* chat constants untouched, one renderer.
- **E11** The connector rule needs a connector folder on the chain, not a reached root. *Why:* Google shared chains.
- **E12** Hidden drives are filtered at the connection service's reads under the audience, primary falling back to the
  first visible mount.
- **E13** A stable grant id across refresh rotations, in the access token. *Why:* the link's "grant revoked" check, and
  a future grant list.
- **E14** Tools call `DriveOperations` directly, not `DriveToolRun` (its collector, stand-ins and gates are
  specialist-shaped); the specialist's per-run guards are not reproduced.
- **E15** `MCP_` token prefix, `### key` body, key set and safe validation checked at load; 60 s cache; a failed refresh
  keeps the last Firestore text; nothing ever loaded → no app / `isError`; descriptions filled per request by a
  `list_tools` override, never by mutating shared tool objects; per-result rules in result lines.
- **E16** The connector's index admit fails closed; chat's stays fail-open.
- **E17** `maxResultSizeChars` on open only; a 100k hard guard on every tool.
- **E18** The link is an opaque id with server state; GET consumes, HEAD is free; the folder is checked at fetch.
- Out of scope: DCR's deprecation in favour of client ID metadata documents in the 2026-07-28 MCP spec; both connectors
  use DCR today.

## 13. Open owner questions

- **Q1 — the model reader on open.** A scan PDF or an image opened as text runs our paid model reader (§4.3 d), up to
  200 pages per open, with no call cap. Recommended: the connector never runs it; a scan answers "read its passages", an
  image "its description is in its card; the file by link". Alternatives: allow it (billed, unbounded), or allow it only
  for files the index has not read.
- **Q2 — the 30-day grant.** Today's refresh re-arms 30 days at every rotation (sliding). Recommended: keep sliding for
  both connectors (dies after 30 days unused, on whitelist removal, or on client revoke). Alternative: absolute, a
  monthly re-consent in claude.ai.
- **Q3 — revocation surface.** There is no Cabinet list of MCP grants. Recommended: none in v1; "Connector access:
  Nothing" on every drive is the instant kill switch for content. Alternative: a "connected apps" list with Revoke
  (about one more slice; the grant id of E13 makes it possible).

**Proposed user-visible wording** (for approval with the questions):

- Consent page, drive resource — title "Authorize drive connector"; body "**{client}** is asking to read your drives
  through alekbot. It can search, open and quote files only in the folders you choose for it in the Cabinet (Drives →
  Connector access); until you choose, it sees nothing. It cannot change, move or delete anything."
- Cabinet block — title "Connector access"; explanation "What apps connected through the drive connector (for example
  claude.ai) can read on this drive. They can never change anything."; choices "Nothing" / "Everything the bot sees on
  this drive" / "Choose folders…"; refused scope change "This would leave folders the connector reads outside what the
  bot sees: change the connector access first."
- Server name `alekbot-drive`; suggested connector name in claude.ai "alekbot drive"; URL `https://<host>/drive-mcp`.
- Refusal line — "Not available through this connector: it is outside the folders shared with it, or it does not exist."
