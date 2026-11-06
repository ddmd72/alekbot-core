# RFC: Google Drive connection (multi-drive, stage 3)

**Status:** ACCEPTED (rev 1.1: the change feed defers the natives' head revision to the indexer, GE-36; rev 1, 2026-11-02: security and end-to-end reviews folded in; the owner's sandbox decision in §10); built, live acceptance after deploy.
**Owner:** AI Engineering
**Builds on:** [MULTI_DRIVE_RFC.md](MULTI_DRIVE_RFC.md) (stages 1 and 2: connections, handles, the router and registry,
the account-wide feed, scopes, the access policy, the Cabinet list). Its D-rows (§2), E-rows (§16) and T-rows (§13) are
cited as written there; this RFC adds **GE-n** engineering decisions (§9) and names its gaps **G1, G1b, G2–G14** (§5).
**Milestone:** Multi-drive programme, stage 3: a Google Drive account with full access, several accounts per user.
**Execution plan:** the plan (private ops repo) holds the frozen core, the parts, the test plan and the owner steps.
Probe results quoted here (counts, statuses, field names only) are kept there too.

Code cites are `main` @ `7eb60baa`. Google behaviour is tagged **[DOC]** (a Google Drive API v3 or OAuth page) or
**[PROBE]** (seen live on a consenting account, 2026-11-02; read-only except writes inside one disposable probe folder,
trashed afterwards). Probes P-a, P-d, P-e, P-f and P-g are folded in; **[INF]** marks the few inferences left.

---

## 0. In one paragraph

A user connects one or more Google accounts as drives. The Google adapter implements `UserDrivePort` over Drive API v3:
the account-wide change feed (`feed_scope=ACCOUNT`) with an emulated full enumeration inside its own opaque cursor,
file and drive identities in the `google_drive` id space, native Docs/Sheets/Slides read through an export in a format
the parser already reads, and ordinary files written only inside the connection's write folder. The Google OAuth edge
keeps its consent and scopes (its revoke call is fixed, which helps Gmail too): one registry entry asks for the `drive`
scope plus the edge's identity scopes. Fifteen gaps (G1, G1b, G2–G14) were checked against the stage-2 bar of
"adapter + OAuth edge + one registry entry"; eleven need a provider-neutral change, named with its reason in §2 and
designed in §5. The largest are the streaming of Google recordings into the media sandbox without the bearer token
entering it (G2) and folders outside My Drive: the picker, path resolution and what the model sees (G8, G11). The
owner's decision on indexing shared folders before the separate parser service exists is in §10.

## 1. Owner decisions this RFC builds on (settled)

| # | Decision | Source |
|---|---|---|
| D2 | Google's permissions decide what is visible; how shared items enter the tree is the adapter's job. | parent §2 |
| D10 | Native Docs/Sheets/Slides: v1 read and search only, no editing. | parent §2 |
| D3, D11 | Ordinary files: read in the scope, write only into the connection's write folder (as OneDrive). | parent §2 |
| D12 | Several connections of one provider work: several Google accounts per user. | parent §2 |
| Q1 → (b) | The Cabinet scope picker also offers the top folders outside My Drive (tops of shared chains, shared-drive roots the account can see) through a provider-neutral addition; shortcuts are never followed. | owner, 2026-11-02 |
| Q2 | The OAuth app is "In production, unverified"; a refresh token has no 7-day expiry. No action. | owner, 2026-11-02 |
| Q3 | Google recordings are transcribed by **streaming**, without the bearer entering the sandbox (E24); no size cap of our own beyond today's. | owner, 2026-11-02 |
| — | Change key of ordinary files: `headRevisionId`; content identity for "also on": `md5Checksum`. Natives have no content hash, so no "also on" for natives. | owner, 2026-11-02 |
| — | Forms and other natives that cannot be exported are not indexed (metadata only). | owner, 2026-11-02 |

## 2. The bar, and what goes beyond it

The parent promises stage 3 as "adapter + OAuth edge + one registry entry" (parent §0, §1, E20). What fits:

| Part | What | Files |
|---|---|---|
| Adapter | `GoogleDriveAdapter(UserDrivePort)`, its token provider, its Drive v3 mapping (§3) | `src/adapters/google/google_drive_adapter.py`, `google_drive_tokens.py`, `google_drive_mapping.py` |
| OAuth edge | consent unchanged: `GoogleOAuthProvider` composes `spec.required_scopes` + `openid email`, `prompt=consent select_account`, PKCE, `GRANT_WIDE` (`src/adapters/google/google_oauth_provider.py:53-72`); its `revoke` sends the refresh token and checks the answer (GE-28, `:115-127`) | `google_oauth_provider.py` |
| Registry | one `ConnectorSpec` (§4.1) | `src/composition/connector_registry.py:43-58` |
| Composition | the adapter in `drive_adapters` at both build sites (`src/composition/service_container.py:220-229`, `indexer_main.py:144-152`), only when the Google client is configured (GE-31); the indexer's deploy step mounts it (§8) | wiring; `cloudbuild-dev.yaml` |

**Gaps checked against the bar.** Eleven need a change outside the bar (G1, G1b, G2, G4, G8–G14); each is
provider-neutral: no Google string enters `domain/`, `services/`, `web/` or `src/web/static/` (T-1 holds), no new
`DriveCapabilities` field (T-7 unchanged), and the fake providers exercise it. G3, G5, G6 and G7 need none.

| Gap | What breaks without it | Neutral change | Why it is neutral |
|---|---|---|---|
| G1 | A native's name has no extension (or a misleading one), so `kind_of` says `other` and D10 never happens (`src/domain/drive_index_policy.py:78-95`) | a mime → kind table for the document mimes the parser reads; the mime wins over a disagreeing extension, except between two text kinds | IANA mimes; Graph's mime follows the extension |
| G1b | Google's Markdown export of a Doc embeds its images as base64 `data:` URIs: most of an image-heavy Doc's text, over `MAX_TEXT_CHARS` [PROBE]; chat refuses such a file on size before reading it | the markdown/text reading drops `data:` image targets, keeping alt text, before every text cap | an uploaded `.md` can carry the same URIs |
| G2 | Google has no header-free media URL; the transcript stage refuses a header-carrying `MediaSource` (E37, `src/services/drive_transcript_stage.py:1527-1546`) | `MediaAudioPort` takes a `MediaSource`; a header-carrying one reaches the child only as a one-use loopback URL | any provider whose media needs a header |
| G3 | Google folders have no child count; the density rule never fires | none: adapter reports 0, accepted (GE-11) | — |
| G4 | Drive allows `/` in names (seen live); paths join names with a bare `/` (`src/domain/user_drive.py:242-243`) | one escape of `/` in a name wherever a path is built from names, and in segment matching | parent §11.1 already says read names are escaped "for display and resolution" |
| G5 | Does not exist: trashing or untrashing a folder emits a change for the folder **and** every descendant [PROBE P-a] | none | — |
| G6 | A native's `version` and `modifiedTime` both move on a rename [PROBE P-d]; as a change key they would re-index (model money) on every rename | the adapter reads the head revision (GE-4); the feed returns that key pending and the port's `resolve_content_versions` reads it for the items the indexer keeps (GE-36) | any provider whose change key costs a read; OneDrive's cTag is final and passes through |
| G7 | Parent §7.1/E21 say "reported `can_download=False` when an export would exceed the cap": not computable from metadata; Google refuses at export time with 403 `exportSizeLimitExceeded` [PROBE P-e] | that refusal maps to the existing `DriveFileTooLarge(Error)` at download time; GE-22 supersedes the sentence | no code seam |
| G8 | The picker walks down from My Drive's root; shared folders and shared-drive roots are not under it, and paths printed for them do not resolve | a port method listing the folders outside the root, a picker section, and path resolution from those chain tops | `[]` for an App Folder; the fake account-wide provider has outer folders |
| G9 | A large Docs export can run for minutes; `DOWNLOAD_TIMEOUT_S = 120` (`src/domain/drive_index_params.py:51`) bounds both the adapter's transfer and the planner's worst case (`src/domain/drive_index_planner.py:1166-1171`) | `DOWNLOAD_TIMEOUT_S` = 300 | one bound for any provider's transfer |
| G10 | Chat open writes the bytes to a temp file named by the item's extension ("" → `.bin`) and markitdown converts by path (`src/services/file_conversion_service.py:175-181`) | the temp suffix comes from the mime when G1 lets the mime decide | stdlib `mimetypes` |
| G11 | With only outer folders chosen, listing the mount is refused and nothing names the readable folders to the model (`src/domain/drive_mounts.py:300-340`, `src/domain/drive_access_policy.py:178-180`) | the mount line names the chosen folders; listing an out-of-scope root lists them | a full-access OneDrive with chosen folders has the same need |
| G12 | `_resolve` validates every segment against the portable set, reads included (`src/services/user_drive_service.py:255-257`), so a Google folder named `Clients: 2026` cannot be listed by path, and a write folder under it breaks every default save | validate only a segment the bot creates | the rule is about what the bot writes |
| G13 | With `DRIVE_PARSER_USER` unset the parser children run as the indexer's own user (`src/adapters/drive_parser/media_launch.py:54-61`), which now holds full-drive tokens in memory | on Cloud Run a missing parser user fails the start | every provider |
| G14 | Extra granted scopes are stored silently (`src/services/oauth_connect_service.py:144-163`); "drive + identity only" is checked once live | a WARNING counting granted scopes beyond the spec's and the edge's identity scopes | every realm |

## 3. The adapter

### 3.1 Shape

`GoogleDriveAdapter(oauth_credentials, client_id, client_secret, *, connector: ConnectorSpec)`, in the `google`
adapter subpackage next to the OAuth edge. It finds its grant by `handle.credential_id` and keys every cache by
`(user_id, credential_id)` or `connection_id` (T-3). Its token provider (`GoogleDriveTokenProvider`) follows
`graph_auth.py`: a 5-minute cache per `(user_id, credential_id)`, one `asyncio.Lock` per key deduping refreshes, the
refreshed token written back through `OAuthCredentialsPort.update`, `invalid_grant` → `DriveReauthRequired`, a token
endpoint failure → `DriveThrottled` (systemic). It does not share code with `gmail_provider_adapter.py` (a top-level
adapter module; REQ-ARCH-23 forbids the import and Gmail's refresh is not re-keyed). Transport is `aiohttp`, one session
per call as in the OneDrive adapter; every metadata call is bounded by `GOOGLE_CALL_TIMEOUT_S = 60`, every content
transfer (`alt=media`, export) by `DOWNLOAD_TIMEOUT_S` (G9).

Field mask `F` on every file read: `id, name, mimeType, parents, size, md5Checksum, headRevisionId, modifiedTime,
createdTime, trashed, webViewLink, shortcutDetails(targetId, targetMimeType), capabilities(canDownload, canModifyContent,
canRename, canMoveItemWithinDrive, canTrash, canAddChildren, canReadRevisions), imageMediaMetadata(time, width, height, cameraMake,
cameraModel, location), videoMediaMetadata(durationMillis, width, height), thumbnailLink, driveId` [DOC files].
Every list pages on `nextPageToken`, `pageSize` 1000 [DOC files.list, changes.list]; every call sends
`supportsAllDrives=true` and every list `includeItemsFromAllDrives=true` (an account can see shared-drive items it does not own [DOC]).

### 3.2 Method map

| Port method | Google call | Notes |
|---|---|---|
| `get_root` | `files.get('root', F)` + `about.get(user(permissionId))`, both cached per credential until `forget` | the real root id is `root_item_id` (children name it in `parents`); `drive_identity` per GE-1 |
| `get_item` | `files.get(id, F)` | `path` from the parent walk (N1); `trashed` → `DriveItemNotFoundError` |
| `list_children` | `files.list(q="'<id>' in parents and trashed=false", F)` | all pages |
| `search` | `files.list(q="(name contains '<q>' or fullText contains '<q>') and trashed=false", pageSize=limit)` | `'` and `\` escaped with `\` [DOC search]; the scope filter stays the service's (`src/services/user_drive_service.py:356-369`) |
| `download` | binary: `files.get(id, alt=media)`; native: `files.export(id, mimeType=<export>)` (§3.5) | export over the cap → `DriveFileTooLargeError` (G7); a native that cannot be exported → `DriveItemNotFoundError("no downloadable content")`, as OneDrive with no download URL |
| `download_to_file` | the same calls, streamed to `dest_path` in 64 KiB chunks, cut at `max_bytes` | over `max_bytes` or an over-cap export → `DriveFileTooLarge`; runs in the indexer process (E24 holds) |
| `upload` | ≤ 5 MB `uploadType=multipart`; above, `uploadType=resumable` in 8 MiB chunks (a multiple of 256 KiB) [DOC uploads] | `parents=[parent_id]`; never a Google `mimeType` on upload (that converts the file [DOC uploads]); conflict modes emulated (§3.6) |
| `replace_content` | `PATCH upload/drive/v3/files/{id}?uploadType=media|resumable` | refused before the call for natives (`can_write_content=False`), and by the adapter itself as a second line (`DriveAccessDenied`) |
| `move` | `files.get(id, parents)` then `files.update(id, addParents, removeParents, name)` | single parent [DOC folder]; name collision emulated (§3.6) |
| `create_folder` | `files.create({name, mimeType: folder, parents: [p]})` | `child_by_name` first → `DriveNameConflictError` |
| `delete` | `files.update(id, {trashed: true})` | **never** `files.delete` (permanent) [DOC delete]; recoverable for 30 days |
| `child_by_name` | `files.list(q="'<p>' in parents and trashed=false")`, compared by `name_key` | the oldest by `createdTime` (T-3's oldest match); the conflict rule of §3.6 |
| `area_path` | none | `""`: an account drive has no area |
| `get_changes` | §3.3 | |
| `get_change` | `files.get(id, F)` | `trashed` → `deleted=True`; 404 → `DriveItemNotFoundError`, never a deletion (port `:90-94`) |
| `list_children_changes` | as `list_children`, `pageSize=limit` | cursor = own JSON `{folder_id, page_token}`; a token of another folder restarts the listing |
| `get_thumbnail` | `thumbnailLink` from `files.get`, its `=s<n>` suffix set to `max_edge_px`, fetched **without** the bearer | another host; the rewrite resizes and answers 200 without a token [PROBE P-f]; https only, and the link is a secret: never logged, never in an error (OneDrive's rule, `onedrive_adapter.py:542-567`); over the vision cap → `DriveThumbnailTooLarge`; none → None |
| `media_source` | `MediaSource(url=<alt=media URL>, headers={"Authorization": …}, expires_at=<token expiry>)` | the token is refreshed first unless it lives `MEDIA_TOKEN_MIN_LIFE` (10 min) more; the sandbox never sees the header (G2) |
| `outer_folders` (new, G8) | `files.list(q="mimeType='<folder>' and trashed=false")`, keep those with no `parents`; plus each member shared drive's root, named from `drives.list` | cached per connection for 5 minutes (N1's TTL), dropped by `forget`; §5 G8 |
| `capabilities` | constant | §3.7 |
| `forget` | drop the root/identity/path caches and the token cache of the handle | T-3 |

**N1 — paths.** Drive has no path field. `DriveItem.path` is built by walking `parents` up to the root through a
per-connection cache `id → (name, parent)` (TTL 5 minutes, entries of folders dropped when `get_changes` reports their
rename, move or trash), joined with `join_drive_path` (so G4's escape applies). A chain that ends at an item with no
visible parent prints from that top folder, as the index does (`src/domain/drive_search.py:401-425`); a chain cut at
`MAX_CHAIN_DEPTH` (64) likewise. A member shared drive's root is named from `drives.get` (its `files.get` name is not the
drive's [INF]), the same name the enumeration emits (§3.3).

### 3.3 The change feed and the cursor

`changes.list` is account-wide with no folder parameter: one cursor per account, `feed_scope=ACCOUNT` (parent §7.1).
Tokens "do not expire" [DOC changes.list]; a non-numeric token answers 400 `invalid`, a numeric token far ahead answers
200 [PROBE]. So the cursor is the adapter's own opaque JSON, bound to the drive identity (the OneDrive pattern,
`src/adapters/microsoft/onedrive_adapter.py:571-625`):

```
{"v": 1, "drive": "<drive_identity>", "phase": "enumerate" | "feed",
 "start": "<changes start token>", "corpora": [...], "list": "<files.list page token>", "page": "<changes page token>"}
```

| Situation | Behaviour |
|---|---|
| cursor `None` | `changes.getStartPageToken` first (kept in `start`), then the emulated enumeration (below); every page `full_enumeration=True`, `complete=False` |
| enumeration | `files.list(q="trashed=false", corpora=user, orderBy=folder)` (folders first [DOC files.list orderBy], so a page's files find their parents in the page or the tick's memo); then, per member shared drive from `drives.list`, one pass that first emits the drive's root as a folder change (`parent_id=""`, name from `drives.list`) and then lists `corpora=drive, driveId=<id>, orderBy=folder`; `corpora` holds the passes left |
| a stored `files.list` page token refused (400) | the current pass restarts from its first page, logged at WARNING (observations are idempotent); a refusal on a pass's first page is a plain failure. The lifetime of list tokens is undocumented: slice A measures it (one page, wait 60 min, continue) |
| last enumeration page | `complete=True`; the cursor switches to `phase=feed` at `start`, so the first feed round replays what changed meanwhile (idempotent observations) |
| feed round | `changes.list(pageToken, includeRemoved=true, restrictToMyDrive=false, fields=nextPageToken, newStartPageToken, changes(changeType, removed, driveId, fileId, time, file(F)))`; `complete` only when `newStartPageToken` arrives (pages can be short [PROBE]) |
| `changeType=drive`, `removed=true` (membership of a shared drive lost) | `DriveResyncRequired`: the next enumeration no longer sees its items and the epoch sweep removes them |
| `changeType=drive`, otherwise (the shared drive renamed) | the drive root's folder change with its new name |
| `removed=true` | `DriveChange(item_id, deleted=True, identity)` (deletion or lost access [DOC changes]) |
| `file.trashed=true` | `deleted=True`; a folder's trash and untrash each emit a change for every descendant [PROBE P-a] |
| no visible parent | `parent_id=""`; the scope verdict admits it only under "whole drive" or below a chosen outer folder (parent §11.3, E79) |
| every page | `root_item_id` = the real My Drive root id, `drive_identity` per GE-1 |
| cursor of another drive identity, unreadable, or wrong `v` | `DriveResyncRequired(<fresh enumeration cursor>)`, logged at WARNING |
| `changes.list` 400 `invalid` | `DriveResyncRequired` |

**Quota.** A Cabinet scope change restarts the feed (E71): one full enumeration costs 100 units per 1000 items
[DOC limits], well inside 325 000 units per minute per user.

**The cost that binds: parent reads.** On an ACCOUNT feed every change needs a scope verdict. A parent that is neither
in the page nor stored costs one `get_change` from `ITEM_CALLS_PER_TICK` = 20 (`src/domain/drive_index_params.py:23`,
`src/services/drive_indexing_service.py:1249-1311`); verdicts are memoised only within a tick, and out-of-scope folders
are never stored. Expected per feed page: (distinct parents outside the page and the index) × (their depth to a chain
top) reads. With a few chosen folders on an account whose activity is mostly elsewhere, that can exceed 20 per tick and
leave out-of-scope changes `UNRESOLVED`. `orderBy=folder` keeps the enumeration's share down; the live acceptance
measures the backlog (§7). If it does not drain, the follow-up is a stored out-of-scope folder verdict (a stage-2
planner change, not in this RFC).

### 3.4 Items and their neutral fields

| `DriveItem` / `DriveChange` field | Binary file | Native (Docs, Sheets, Slides, Drawings) | Folder | Shortcut, un-exportable native |
|---|---|---|---|---|
| `identity` | `google_drive:{fileId}` (no account part: a file shared to two connected accounts has one identity and one note, parent §7.2) | same | same | same |
| `mime_type` | Google's `mimeType` | the export mime (§3.5) | `""` | `""` |
| `size_bytes` | `size` | `size` (storage bytes, not the export's; the cap is enforced on the stream) | 0 | 0 |
| `content_version` | `headRevisionId` (a rename leaves it, `version` moves [PROBE]); `md5:{md5Checksum}` if it is ever absent [INF] | `r:{head revision id}` when `capabilities.canReadRevisions`, else `m:{modifiedTime}` (G6) | `""` | `""` |
| `content_identity` | `ContentIdentity("md5", md5Checksum, size)` | None: no "also on" for natives | None | None |
| `modified_at`, `created_at` | `modifiedTime`, `createdTime` | same | same | same |
| `fs_modified_at` | None (v3 has no client file time; the card falls back to `modified_at`) | None | None | None |
| `web_url` | `webViewLink` | same | same | same |
| `child_count` | — | — | 0 (G3) | — |
| `media` | from `imageMediaMetadata` (time read as `YYYY:MM:DD HH:MM:SS` without a zone, else None) and `videoMediaMetadata.durationMillis`; `package`, `malware` False | empty | empty | shortcut: `alias_of="google_drive:{targetId}"` → `Reason.REMOTE_ITEM`, metadata only, never followed |
| `access` | `can_download=canDownload`, `can_write_content=canModifyContent`, `can_rename_move=canRename and canMoveItemWithinDrive`, `can_delete=canTrash`, `can_add_children=canAddChildren` | the same with `can_write_content=False` (D10) | same as binary | `can_download=False` |

`ownedByMe` is never read (not populated on shared-drive items [DOC files]); `capabilities` decide.

### 3.5 Native documents

| Native | Export mime | Kind (G1) | Note |
|---|---|---|---|
| Document | `text/markdown` | `markdown` | 200 on every Doc sampled, structure kept; images arrive as `data:` URIs, dropped by G1b [PROBE P-e] |
| Spreadsheet | `…spreadsheetml.sheet` (xlsx) | `xlsx` | every sheet is read (`src/adapters/drive_parser/readers.py:418-440`); CSV would export only the first sheet [DOC export-formats]; `MAX_SHEET_DOWNLOAD_BYTES` = 10 MB = Google's export cap; 200 live |
| Presentation | `text/plain` | `text` | the slides' text; pptx export was refused over the cap on one deck while txt worked [PROBE P-e] |
| Drawing | `application/pdf` | `pdf` | |
| Form, Site, Map, Vid, any other | none | `other` | metadata only; `can_download=False` (owner decision) |

Exports are limited to 10 MB and take no Range [DOC export]; Google refuses an over-cap export with **403
`exportSizeLimitExceeded`** after a few seconds [PROBE P-e], mapped by reason (G7). One export ran for minutes [PROBE], hence G9 (`DOWNLOAD_TIMEOUT_S` = 300 s).
The parser reads by kind, not by name (`readers.py:103-108`, `_read_text` `:348-362`). A native's name may still carry
an extension (`brief.docx`, `budget.csv`); G1's rule lets the export mime win over a disagreeing extension, so it reads
as its export kind.

Why not docx for Docs [PROBE P-e]: the docx export was refused over the cap for both image-heavy Docs whose Markdown
export succeeded, and where it worked, the DOCX reader kept fewer list items than the Markdown
(fewer list items and headings on the same Doc).

### 3.6 Writes and conflict modes

Drive keeps several items with one name in a folder, also names that differ only in case or Unicode form, and has no
conflict parameter [DOC absence: no name-conflict reason in errors]. The emulation follows the service's rule, as
OneDrive does: two names conflict when their `name_key` (NFC + casefold, `src/domain/user_drive.py:224-226`) is equal,
so `Report.pdf` conflicts with `report.pdf`. `child_by_name` therefore lists the parent's children (`'` and `\` escaped
in the query) and compares by `name_key`, oldest by `createdTime` first. Modes: `fail` → `DriveNameConflictError`;
`rename` → the first free `name (n).ext`; `replace` → `replace_content` on the oldest match, else create. `move` with
`new_name` and `create_folder` check the same way. A user's own create between the check and the write can still make
a duplicate; the bot's writes are serialised by the per-user mutation lock (parent §6). Writes land only where the
access policy allows (the write folder, parent §11.3); natives are never written (`can_write_content=False`). Every name
of the portable set (parent §11.1) is accepted as is.

### 3.7 Capabilities, limits, errors

`capabilities(handle) = DriveCapabilities(max_path_chars=GOOGLE_MAX_PATH_CHARS (32 768), writable=True,
feed_scope=FeedScope.ACCOUNT)`. Drive documents no path length, only 100 folder levels [DOC folder]; the type needs a
positive value (`src/domain/drive_capabilities.py:22-24`), so a sentinel that never binds; the depth limit surfaces as
`DrivePathTooLong`.

| Google answer | Port outcome |
|---|---|
| 404 `notFound` (missing **or** no access, not distinguished [DOC errors]) | `DriveItemNotFoundError`, never a deletion |
| 401 | one forced refresh; refresh `invalid_grant` → `DriveReauthRequired`; 401 after a good refresh → `DriveAccessDenied` |
| 403 `insufficientFilePermissions`, `appNotAuthorizedToFile`, `domainPolicy` | `DriveAccessDenied` |
| any 403/404 on `revisions.list` | never an error: `content_version` falls back to `m:` for that item (G6) |
| **403** `userRateLimitExceeded`, `rateLimitExceeded`; **429**; 5xx | truncated exponential backoff, 3 retries, at most 32 s [DOC limits]; then `DriveThrottled(hint)`. Google signals rate limits with 403: **classified by `reason`, never by status** |
| 403 `dailyLimitExceeded` | `DriveThrottled(TICK_DEADLINE_S)` (no retry) |
| 403 `storageQuotaExceeded` | `DriveStorageFull` |
| 403 `myDriveHierarchyDepthLimitExceeded` | `DrivePathTooLong` |
| 403 `exportSizeLimitExceeded` (an export call) | `DriveFileTooLarge` in `download_to_file`, `DriveFileTooLargeError` in `download` (G7) |
| 400 on a stored `files.list` page token | the pass restarts (§3.3) |
| 403 `numChildrenInNonRootLimitExceeded`, any other 4xx | `ValueError` (logged with status and reason, never the body's text) |
| a call past its timeout / a transport error | `asyncio.TimeoutError` / `OSError`, classified by the tick as for OneDrive (§8.2 of USER_DRIVE_INDEXING) |

## 4. The OAuth edge and the registry

### 4.1 The registry entry

```
GOOGLE_DRIVE = "google_drive"
ConnectorSpec(kind=GOOGLE_DRIVE, integration=Integration.DRIVE, realm=_GOOGLE,
              required_scopes=("https://www.googleapis.com/auth/drive",), multiple=True,
              id_space=GOOGLE_DRIVE, label="Google Drive", offered=<False until slice D, then True>)
```

The edge asks for `drive` + `openid email` and nothing else (`google_oauth_provider.py:58`); it never sends
`include_granted_scopes`, so a drive grant never carries Gmail's scope and Gmail's grant never carries `drive` (E1: one
credential per kind). `OAuthConnectService._check_scopes` refuses a consent with the drive box unticked (Google's
granular consent [DOC oauth-web]). P-k (the drive token's granted `scope` lists only drive + identity scopes) is the
slice-D live check, and G14 keeps it as a standing signal: a grant carrying more scopes than asked logs a WARNING.

### 4.2 Identity, account chooser, reconnect, revoke

| Topic | Rule |
|---|---|
| Account | `ProviderAccount(realm="google", subject=<OpenID sub>)` from userinfo, as for Gmail; the same account as the user's Gmail credential (D8: one initial-build record per account) |
| Drive identity | `google_drive:account:{permissionId}` from `about.get` (GE-1) |
| Account chooser | every connect, add and reconnect shows Google's chooser: `prompt=consent select_account` (E86), already sent |
| Reconnect hint | `login_hint` on `RECONNECT` only (`google_oauth_provider.py:70-71`) |
| Reconnect match | same `credential_id`, else same `drive_identity` (E33); a reconnect with another `sub` is refused before anything is stored (parent §5.2) |
| Activation | `get_root(probe handle).drive_identity`; an `about.get` failure fails the connect and removes the credential (E34) |
| Revoke rule | `GRANT_WIDE`: Google revokes every scope the account granted to the client [DOC oauth-web]. A drive disconnect revokes at Google only when `list_by_account(realm, subject)` is empty (parent §4.3, I1), so disconnecting a drive never kills Gmail or a second drive of the same account, and the reverse |
| Revoke call (GE-28) | today the edge posts the stored **access** token and ignores the answer (`google_oauth_provider.py:115-127`); that token has usually expired by disconnect time, so nothing is revoked while the log says it was [INF]. The edge posts `credential.refresh_token or credential.access_token`, treats any non-200 as a failure (a WARNING with the status only, never raised), and logs success only on 200. This also fixes Gmail's disconnect, which uses the same edge. Slice D's acceptance checks the positive side |
| Refresh tokens | each consent issues one; Google keeps at most 100 per account per client [DOC oauth]. A replaced credential's old refresh token is overwritten, not revoked: revoking it would end the whole grant, the new token included |

Several Google accounts per user: each consent of another `sub` is another credential and another `DriveConnection`
(`multiple=True`, E15); mounts are named in the Cabinet (D17), e.g. `Springfield` for Alex Doe's personal account and
`Acme` for the work one.

## 5. The neutral deltas

### G1 — mime → kind for documents

`kind_of(name, mime, is_folder)` (`src/domain/drive_index_policy.py:78-95`) gains a domain table of the document mimes
the parser reads: the OOXML docx/xlsx/pptx mimes, `application/pdf`, `text/markdown`, `text/plain`, `text/csv` →
`docx`, `xlsx`, `pptx`, `pdf`, `markdown`, `text`, `csv`. Rule: when the extension gives no kind, the table decides; when
both decide and disagree, the mime wins, except when both kinds are text kinds (`markdown`, `text`, `csv`), where the
extension keeps its finer reading (`notes.md` served as `text/plain` stays `markdown`). Then today's image/audio/video
fallback. A Doc named `brief.docx` (export `text/markdown`) reads as markdown; a Sheet named `budget.csv` (export xlsx)
reads as xlsx. OneDrive: Graph's `file.mimeType` follows the extension [INF], so its kinds do not change, except that an
extensionless file with one of these mimes is now read (named in the deploy note). **Files:**
`src/domain/drive_index_policy.py`. **Tests:** extensionless + each mime; a disagreeing container extension loses; two
text kinds keep the extension; no `vnd.google-apps` string in domain (T-1).

### G1b — `data:` images in Markdown and text

A pure domain function `drop_data_uri_images(text)` (new `src/domain/text_data_uris.py`) rewrites image syntax whose
target is a `data:` URI: an inline `![alt](data:…)` becomes its alt text (nothing when empty); a reference definition
`[id]: <data:…>` or `[id]: data:…` is removed and each `![alt][id]` that used it becomes its alt text; an HTML
`<img src="data:…">` becomes its `alt`; an **unterminated trailing** `data:` image target (a file cut mid-URI) is
dropped to the end. Nothing else changes; a `data:` link that is not an image stays.

Where and in which order:
- **Indexer.** `_read_text` (`src/adapters/drive_parser/readers.py:348-362`) for the `markdown` and `text` kinds, after
  `_read_prefix` (`:717-722`, 8 MB) and before `_finish` truncates at `MAX_TEXT_CHARS` (`:772-773`). A Markdown export
  over 8 MB (the export cap is 10 MB) is cut mid-URI by the prefix; the unterminated rule removes the tail.
- **Chat.** Today two 5 MB checks run before any reading: the item's metadata size (`src/services/file_conversion_service.py:170`)
  and the local file size (`src/utils/file_conversion.py:223-228`). For an item whose mime is `text/*` the order
  becomes: (1) no refusal on `size_bytes` (a native's storage size says nothing of its text); (2) the bytes are read up
  to `MAX_TEXT_EXPORT_BYTES` = 10 MB (a new domain constant, the export cap); (3) images are dropped; (4) `MAX_FILE_BYTES`
  (5 MB) applies to the result. Other mimes keep today's order.

**Tests:** inline, reference-style, HTML and unterminated forms; alt text kept; a non-image `data:` link left alone; a
4 MB fixture shrinks under the indexer's cap; a 6 MB `text/markdown` item that is mostly `data:` images opens in chat.

### G2 — streaming a header-carrying media source (E24, retires E37)

**Today.** The stage turns a `MediaSource` into a bare URL and refuses one with headers (`drive_transcript_stage.py:1540-1544`);
`MediaAudioPort.probe(url)` / `extract_windows(url, …)` (`src/ports/media_audio_port.py:18-33`) send the URL on stdin to
a fresh parser child whose ffmpeg reads it over `https,tls,tcp` with Range seeks (`src/adapters/drive_parser/media.py:36,66-73`).
The child shares the container's network (`decisions/drive_parser_sandbox_resource_isolation.md`), which is why no
bearer may enter it. ffmpeg reads Google's `alt=media` fine when it holds the header, seeks included [PROBE]. No
header-free alternative exists [PROBE P-g]: `files.download` answers a finished operation whose `downloadUri` also needs
the `Authorization` header (403 without it), and `webContentLink` is a browser link.

**Design.** The guarantee moves to the one place that launches the child:
1. `MediaAudioPort.probe(source: MediaSource)` and `extract_windows(source: MediaSource, …)`. The stage passes the
   source it reads fresh for every step (`:444`, `:501`) and loses its refusal branch.
2. `SubprocessMediaExtractor`: a header-free source runs exactly as today. A source with headers opens a
   `LoopbackMediaRelay` (new, `src/adapters/drive_parser/media_relay.py`) for **one child run**, and the child receives
   only the relay's URL.
3. The relay run's ffmpeg input guard is `-protocol_whitelist http,tcp` with no TLS options; the extractor passes an
   `http://` URL only when it is its own open relay's. `FORMAT_WHITELIST` admits only self-contained demuxers
   (`media.py:36-41`), so a crafted file cannot make ffmpeg open a second resource.

**The relay is the in-indexer path only; it is superseded.** With the sandbox's media op (UNTRUSTED_CONTENT_SANDBOX_RFC.md §7 S5, O18, live)
Google media reaches the sandbox through a reverse tunnel the indexer opens: the sandbox asks for byte ranges, the indexer fetches them with the header
and answers. No URL and no header enter the sandbox, and the relay and the extractor branch are deleted in the same deploy.

**What the sandbox can reach, and for how long** (security review 2026-11-02 folded in):

| Property | Rule |
|---|---|
| What the child gets | `http://127.0.0.1:<ephemeral port>/<nonce>`; nonce = 32 random bytes (`secrets.token_urlsafe`). No header, token, provider host or file id |
| What the URL serves | `GET`/`HEAD` of **one** upstream URL fixed when the relay opens (never derived from the request); the nonce is compared with `secrets.compare_digest`; any other path → 404, any other method → 405; the query string is ignored |
| Requests from the child (the relay is an HTTP server inside the token-holding process) | no request body accepted; at most 32 request headers and 8 KiB of header bytes, else refused before anything reaches Google and the connection closed: 431 from the relay's handler, or 400 when aiohttp's parser refuses first (a single header line over 8 KiB; more than 32 headers on an aiohttp whose parser enforces `max_headers`); `Range` forwarded only when it matches `^bytes=\d*-\d*(,\d*-\d*)*$`, else dropped |
| Forwarded upstream | the valid `Range` only, plus the source's own headers added by the relay; the upstream URL must be `https://` (the relay refuses to open otherwise); redirects are **not** followed: a 3xx → 502 to the child. If a redirect is ever needed, it is followed **without** `Authorization`, never with it |
| Returned to the child | status, `Content-Type`, `Content-Length`, `Content-Range`, `Accept-Ranges`; nothing else |
| Bind | `127.0.0.1` only, port chosen by the OS; at most 4 concurrent upstream streams (ffmpeg reconnects and seeks) |
| Lifetime | opened just before the child starts, closed in the extractor's `finally` (`media_extractor.py:174-175`, which also runs on cancellation) when the child exits or is killed; `close` aborts in-flight upstream streams at once, with no graceful wait; a timer closes it at the run's wall + 30 s even if `finally` never runs. A probe run lives 60 s, an extraction run at most `TRANSCRIBE_EXTRACT_WALL_S` (240 s) |
| Volume | the relay counts bytes and closes at `TRANSCRIBE_MAX_BYTES` (8 GiB), today's bound; no new cap |
| Token | held only in the indexer process (the relay object), dropped at close; never in argv, env, stdin, a file or a log. The adapter returns a token living ≥ 10 min (`MEDIA_TOKEN_MIN_LIFE`), more than any run; the relay refuses a source whose `expires_at` falls inside the run's wall (`MediaUnreadable`, a transient) |
| Logs | upstream status class (2xx, 3xx, 4xx, 5xx) and byte counts only, so a redirect shows as such; never the URL, nonce or headers |
| Who else could use the URL | any process in the container during the run: the URL is in ffmpeg's argv and `/proc/<pid>/cmdline` is world-readable, so a lingering process from an earlier exploited parse could read it. It yields the bytes of that one file for that one run, never the token: less than today's OneDrive pre-signed URL gives |
| Via the metadata server, not the relay | an exploited parser can ask the metadata server for the indexer's service-account token and, through Firestore and KMS, decrypt every stored credential: the residual the owner accepted in `drive_parser_sandbox_resource_isolation.md` (`:55-59`). Stage 3 adds full-drive Google grants to what it reaches (§10, owner decision) |
| Who runs the child | the parser user; on Cloud Run a missing parser user fails the start (G13) |

**Cost.** Every byte ffmpeg reads passes through the indexer's event loop (1 vCPU). The slice measures it in the
cloud on one long recording before the owner's acceptance.

**Files:** `src/ports/media_audio_port.py`, `src/adapters/drive_parser/media_relay.py` (new),
`media_extractor.py`, `media.py`, `src/services/drive_transcript_stage.py`. **Tests:** relay wire tests (nonce and
`compare_digest`, path, method, body refused, header caps, the `Range` pattern, forwarded headers, `https` upstream
only, no redirect, response header set, close on exit, on cancellation and on the timer with in-flight streams
aborted, byte cap, expiry refusal); an extractor test that the child's payload, argv and env hold no header value (scan
for the token string); the stage transcribes a header-carrying source through a fake extractor; caplog holds no token,
nonce or URL.

### G3 — folder child counts (no neutral change)

Google folders carry no child count. The adapter reports 0, so the density probe never runs for Google
(`drive_index_planner.py:216-220`, `:263`) and folder cards show no count bucket (`src/domain/drive_card.py:97-106`).
Dense trees are indexed file by file under the per-connection caps (E36). Rejected: counting children per folder change
(a paged list per folder, eating `ITEM_CALLS_PER_TICK`).

### G4 — `/` inside a name

A domain function `path_segment(name)` replaces `/` in a name with `∕` (U+2215, DIVISION SLASH) for printing. It is
applied wherever a path is built from names: `join_drive_path` (`src/domain/user_drive.py:242-243`, also the adapter's
N1 walk), `resolve_chain` (`src/domain/drive_search.py:425`), `chain_path` (`src/domain/drive_note_lifecycle.py:137`),
the mirror root path (`src/services/drive_transcript_mirror.py:522`) and a transcript's source path
(`drive_transcript_mirror.py:995`). Segment matching does the same: `match_child_folder` (`user_drive.py:229-239`)
compares `path_segment(child.name)` with the segment. `DriveItem.name` stays the provider's name, so a rename round trip
and `child_by_name` are exact. The tool text's folder description (`src/domain/drive_tool_texts.py:33`) gains "copy a
path as printed: a `/` inside a name prints as `∕`". Example: the community-garden planning sheet `Garden plan 2026/27`
prints as `Springfield/Garden plan 2026∕27` and resolves back. **Tests:** join, chain, match, the transcript source
path, and a T-4 round trip (list a folder whose name holds `/` by its printed path). Rejected: the adapter rewriting
names (lossy: a rename would rename the file); percent-escaping (`%` then needs escaping, and the model mangles it).

### G5 — descendants after an untrash: does not exist

Trashing a folder emits one change for the folder and one for every descendant (`trashed=true`), and untrashing emits
them all again (`trashed=false`); the feed coalesces each item to its latest state [PROBE P-a]. No re-listing rule is
needed. (P-a2, whether a change to a shared-drive item of a non-member reaches the feed, was not probed; a Cabinet scope
change re-enumerates either way.)

### G6 — the natives' change key (adapter only)

On a Doc a rename moves `version` and `modifiedTime`; only the revision list is rename-proof (a content edit adds a
revision, a rename does not) [PROBE P-d]. For a native whose `capabilities.canReadRevisions` is true, the adapter reads
`revisions.list(fileId, fields=nextPageToken, revisions(id), pageSize=1000)`, all pages; the **last** item is the head
(the list runs oldest first) and `content_version = "r:" + head id`. When `canReadRevisions` is false (typical for a
native shared to the account view- or comment-only) there is no call and `content_version = "m:" + modifiedTime`, so
such a native re-cards on a rename. A refused or empty list falls back the same way and never fails the page (§3.7).
The calls run inside `get_change`, `list_children_changes` and every item read, natives only, at most 4 in flight per
call; walks that only need a parent (`UserDriveService._walk`, `src/services/user_drive_service.py:176-192`) pay them too,
which the gate keeps small. `get_changes` makes none: GE-36 supersedes its part of this paragraph (the feed returns the
key pending; the indexer resolves it only for items it keeps). Decision and rejected alternatives: GE-4, GE-36.

### G7 — over-cap exports

Not computable from metadata (no export size before exporting). The adapter maps 403 `exportSizeLimitExceeded` on an
export call [PROBE P-e] to `DriveFileTooLarge` (indexer: the file is demoted `TOO_LARGE`,
`drive_indexing_service.py:2876-2878`) and to `DriveFileTooLargeError` in `download` (chat: the existing cannot-open
line). GE-22 records that this supersedes the parent's §7.1/E21 sentence; the parent is aligned in slice D's docs step.

### G8 — folders outside My Drive

1. **Port** (`src/ports/user_drive_port.py`): `outer_folders(handle) -> List[DriveItem]`, the folders the account sees
   that no parent chain from the root reaches: tops of shared chains and each member shared drive's root (named from
   `drives.list`; the same root the enumeration emits, §3.3). An area drive returns `[]`. Implemented by the router
   (one dispatch, T-8), OneDrive (`[]`), `FixtureDrive` (`[]`), `FakeDriveAdapter` (its parentless folders), Google
   (§3.2, cached per connection for 5 minutes).
2. **Picker** (`UserDriveService.picker_folders`, `src/services/user_drive_service.py:228-233`; route
   `src/web/user_cabinet_app.py:1803-1813`): without a parent it returns `folders` (root children, as today) and
   `outer` (`outer_folders`, by name); the Cabinet shows `outer` under a neutral heading "Outside this drive's root" and
   lets the owner pick one, or a folder inside one, as a scope folder. The verdict, the planner and the policy need
   nothing: a scope folder whose chain never reaches the root is accepted (E79, `src/domain/drive_access_policy.py:132`,
   concerns only a root write folder).
3. **Resolution** (`UserDriveService._resolve`, `:245-291`): a path whose first segment matches no root child is matched
   against `outer_folders(handle)` (the parentless chain tops the index and N1 print from, `src/domain/drive_search.py:401-425`)
   by `path_segment(name)`, **independent of the scope settings**. The walk continues from that top. Its chain is the
   real ancestry, read with `_walk` (`:176-192`, `reached_root=False`), never `_chain_of` (`:89-91`, which asserts the
   root). The access policy then decides as for any item: inside a chosen folder reads pass, a write needs the write
   folder, and a root write folder still refuses it (E79). So printed paths resolve under "whole drive", under a
   chosen folder below a top, and for a write folder inside an outer folder (`_inbox_path`, `:330-337`). A root child
   of the same name wins (the shadowing rule of parent §11.1).

Example: Lukas Weber shares the `Acme Hamburg` folder from Acme's shared drive; Alex picks its subfolder `Q3` in the
`Acme` connection's picker; `Acme/Acme Hamburg/Q3/Q3 budget` is listed, searched and opened.

**Tests:** T-3 case (`outer_folders` empty for an area drive, the parentless folders and drive roots for an account
drive); T-4 with `fake_b`: under whole drive, under a chosen top and under a chosen folder below a top, a printed path
lists; a write folder inside an outer folder takes a default save; a root write folder refuses an outer path; a root
child named like an outer top shadows it. Rejected: a reserved virtual folder id answered by `list_children` (a fake id
becomes a tree node every caller must special-case); resolving from the chosen scope folders (breaks under "whole
drive" and for a folder chosen below a top, and ties path naming to Cabinet settings).

### G9 — the transfer bound

`DOWNLOAD_TIMEOUT_S` 120 → 300 (`drive_index_params.py:51`): the bound on one content transfer of any provider, read by
both adapters and by `can_start_file` (`drive_index_planner.py:1166-1171`). Effect: a routed file's worst case grows from
1 200 s to 1 380 s against the 1 800 s deadline, so it may start only in a tick's first 420 s instead of the whole
480 s start budget (`TICK_START_BUDGET`, `drive_index_params.py:9`, read at `drive_indexing_service.py:363-364`):
about 12 % less start window for routed files; unrouted files are unchanged (worst case 780 s); a hanging OneDrive
download waits longer before it ends. Chat `download` of a native waits for its export the same way. One constant is
kept (GE-8): a per-provider bound would need a capability field the planner reads (T-7: a reader and a contract case
per value) for a 60-second difference in one start window.

### G10 — chat open of an export

`FileConversionService._resolve_drive_content` names its temp file by the item's extension, else `.bin`, and markitdown
converts by path (`src/services/file_conversion_service.py:175-181`). The suffix comes from
`mimetypes.guess_extension(item.mime_type)` when the name has none **or** G1's rule lets the mime win over the
extension: the `Q3 budget` sheet (xlsx) converts as `.xlsx`, a Sheet named `budget.csv` too. Docs (Markdown) and Slides
(text) take chat's plain-text path. **Tests:** an extensionless xlsx item converts; a disagreeing extension takes the
mime's suffix; an agreeing name keeps its own.

### G11 — what the model sees of a drive with chosen folders

With only outer folders chosen, `list_folder("Acme")` resolves to the root, which is out of scope, and is refused with
`OUTSIDE_SCOPE_LINE` (`src/domain/drive_access_policy.py:178-180`); the mount line (`src/domain/drive_mounts.py:300-340`)
names no folder, so the model learns of `Acme Hamburg` only if a search happens to hit it.
1. `mount_line` gains `indexes only <paths>` when folders are chosen: their canonical paths (an outer one prints from its
   top, `Acme/Acme Hamburg/Q3`), at most 5, then `+N more`; the caller reads the paths, as it reads the write folder's.
2. `UserDriveService.list_folder` on a mount whose root is out of scope, asked for the root (empty path or the mount name
   alone), returns the readable tops (the chosen folders, already outermost, E85) with the heading line
   `Folders indexed on <drive>:` (a flag on `ListOutcome`, `src/domain/user_drive.py:186-189`, read by the listing
   renderer). Any other out-of-scope path is still refused. This builds the "virtual listing of scope folders" that
   E72 left as stage-3 polish and supersedes E72's refusal (GE-29).

**Files:** `drive_mounts.py`, `user_drive.py`, `user_drive_service.py`, the listing renderer in
`src/infrastructure/drive_operations.py`. **Tests:** T-4 on `fake_b` with only outer folders chosen: the mount line names
them, `list_folder("<mount>")` lists them, a path outside them is refused; with the root in scope, listing is unchanged.

### G12 — a name the bot may not write is still readable by path

`_resolve` validates every segment against the portable set before walking, reads included
(`src/services/user_drive_service.py:255-257`, `:94-98`). Drive allows `" * : < > ? |` and a trailing dot or space;
`Clients: 2026` is an ordinary Google folder name. Then `list_folder("Acme/Clients: 2026")` fails, and a write folder
under it breaks every default save. The check moves to the one branch that **creates** a missing segment (after
`match_child_folder` missed and `create=True`); a segment that matches an existing folder is read as named.
`split_folder_path`'s refusal of `\` stays (such a folder is reached by ref). **Tests:** resolve, list and save under a
folder named `Plans: 2026?` on `fake_b`; creating `Plans: 2027?` is still refused.

### G13 — no parser child without the parser user on Cloud Run

With `DRIVE_PARSER_USER` unset, `resolve_sandbox` (`src/adapters/drive_parser/media_launch.py:54-61`) and
`_resolve_sandbox` (`src/adapters/subprocess_content_extractor.py:318`) only warn, and the children run as the indexer's
own user, able to read its memory, which now holds full-drive access tokens. On Cloud Run (`K_SERVICE` set) both raise
at start instead; locally the warning stays. Only the image sets the variable today (`Dockerfile:42`). **Tests:** both
resolvers with and without `K_SERVICE`.

### G14 — extra granted scopes are a signal

`_check_scopes` refuses only missing scopes, and extra granted scopes are stored silently
(`src/services/oauth_connect_service.py:144-163`). `OAuthProviderPort` gains `identity_scopes` (Google: `openid email`;
Microsoft: the identity scopes its edge asks, in the normalised short form of E31). `_store` logs a WARNING with the kind
and the count of granted scopes outside `spec.required_scopes` ∪ `identity_scopes`; the grant is still stored. **Tests:**
a grant with an extra scope warns (counts only); today's grants of every kind do not.

## 6. Tests

| Layer | What | Where |
|---|---|---|
| Boundary fake | a stateful Drive v3 at the aiohttp boundary: two accounts, two bearer tokens, files, natives with exports and revisions, trash, shared-drive items and member drive roots, a change log, the error reasons of §3.7 (a call with the wrong grant lands in the wrong account) | `tests/support/google_drive_fake.py` |
| Contract (T-3) | `UserDriveContract` over the Google adapter: `id_space = "google_drive"`, `feed_scope = ACCOUNT`, `max_path_chars = 32 768`; conflict modes, 404 is not deletion, resync, oldest match, recoverable delete, typed facets (`alias_of` from a shortcut), every portable name, the `MediaSource` case (headers present: the contract's "or the bytes are streamed" branch is the relay, covered by G2's tests), two handles share no cache, `outer_folders` | `tests/unit/adapters/test_google_drive_contract.py` |
| Wire | reason-classified 403 (rate limit vs permission); `orderBy=folder` enumeration, the drive-root change per member drive, the enumeration → feed switch and `complete`; `changeType=drive` removed → resync, renamed → root change; a refused stored list token restarts its pass; `trashed` → deleted; cursor binding and the 400 `invalid` resync; export mimes per native and the over-cap mapping; `canReadRevisions` false → no revisions call; a refused revisions list never fails the page; multipart/resumable switch at 5 MB and 256 KiB chunk multiples; never a `mimeType` on upload; never `files.delete`; conflict emulation by `name_key` (`Report.pdf` vs `report.pdf`) and query escaping; the thumbnail fetched with no `Authorization` header, https only, its URL absent from logs on 4xx, timeout and transport error; `media_source` header and `expires_at` ≥ 10 min; the parent walk and its cache invalidation | `tests/unit/adapters/test_google_drive_*_wire.py` |
| Token provider | keyed by `(user_id, credential_id)`; refresh deduped under the lock; `invalid_grant` → `DriveReauthRequired`; endpoint 5xx → `DriveThrottled` | `tests/unit/adapters/test_google_drive_tokens.py` |
| OAuth | the `google_drive` spec through `oauth_provider_contract.py` (the URL asks for `drive openid email` only, PKCE and state present); `revoke` posts the refresh token, logs success only on 200 and a WARNING with the status otherwise, never raises (GE-28); G14's warning | existing contract, new cases; `tests/unit/adapters/test_google_oauth_revoke_wire.py` |
| Neutral deltas | G1, G1b, G4, G9, G10, G12, G13, G14 unit tests; G2 relay and extractor tests; G8, G11, G12 T-4 cases on `fake_b` (stage-2 harness, E76) | per §5 |
| Composition | the indexer builds the Google adapter from `GOOGLE_OAUTH_CLIENT_ID/SECRET` and, without them, starts with the other kinds only | `tests/unit/test_indexer_main_wiring.py` (or its existing home) |
| Architecture | T-1 (no `google`/`gdrive` string in domain, services, web, static); REQ-ARCH-23 (the `google` subpackage imports no other adapter); T-7 (no new capability field); T-8 (the router's `outer_folders` is one dispatch) | existing tests |

New behaviour gets new tests; an existing test failing on a delta goes to the reviewer (root `CLAUDE.md` REVIEWER RULE).
The `MediaAudioPort` signature change adapts existing extractor and stage tests mechanically (`test(mechanical): …`).

## 7. Slices

| Slice | Content | Files (disjoint across parts) | Done when |
|---|---|---|---|
| **Core** (first, alone) | G1 table and rule; G9 constant; `outer_folders` on the port, router, OneDrive, `FixtureDrive`, `FakeDriveAdapter`, the contract case; `MediaAudioPort` takes a `MediaSource` (the extractor refuses a header-carrying one for now: E37 moves, no behaviour change); G14 (`identity_scopes` on the OAuth port and both edges, the warning); the `google_drive` spec with `offered=False` | `drive_index_policy.py`, `drive_index_params.py`, `user_drive_port.py`, `drive_provider_router.py`, `onedrive_adapter.py`, `media_audio_port.py`, `media_extractor.py` (signature only), `drive_transcript_stage.py`, `oauth_provider_port.py`, `google_oauth_provider.py`, `microsoft_oauth_provider.py`, `oauth_connect_service.py`, `connector_registry.py`, test support fakes | `make check` green; no user-visible change |
| **Part A** Google adapter and edge | §3, §4 behind the spec: adapter, token provider, mapping, boundary fake, contract and wire tests; the revoke fix (GE-28) | `src/adapters/google/google_drive_*.py`, `google_oauth_provider.py` (revoke only), `tests/support/google_drive_fake.py`, `tests/unit/adapters/test_google_*` | contract and wire suites green; list-token lifetime measured |
| **Part B** Media relay and sandbox user (G2, G13) | relay, extractor branch, relay-run input guard; both sandbox resolvers fail closed on Cloud Run | `media_relay.py`, `media_extractor.py`, `media.py`, `media_launch.py`, `subprocess_content_extractor.py`, their tests | relay and extractor tests green |
| **Part C** Paths, reading, listing (G1b, G4, G8 resolution, G10, G11, G12) | `drop_data_uri_images` and its call sites with the chat order; `path_segment` and its call sites, the tool text line; `_resolve` from `outer_folders` and validation on create only; the mount line and the root listing; temp suffix | `text_data_uris.py` (new), `readers.py`, `src/utils/file_conversion.py`, `user_drive.py`, `drive_search.py`, `drive_note_lifecycle.py`, `drive_transcript_mirror.py`, `drive_tool_texts.py`, `drive_mounts.py`, `user_drive_service.py`, `drive_operations.py`, `file_conversion_service.py` | unit and T-4 cases green |
| **Part D-1** Cabinet picker (G8) | the `outer` list in the folders route and its Cabinet section | `user_cabinet_app.py`, `src/web/static/` cabinet files | Cabinet tests green |
| **Slice D** (last, alone) | composition at both build sites (the Google adapter only with its client configured, GE-31); the indexer's deploy secrets; `offered=True`; the user-facing capabilities reference (`src/utils/capabilities.py`); docs (§8) | `service_container.py`, `indexer_main.py`, `cloudbuild-dev.yaml`, `connector_registry.py`, `capabilities.py`, docs, the wiring test | live acceptance below |

Parts A, B, C and D-1 run in parallel after the core.

**Live acceptance** (owner), and what each step measures:
1. Connect a Google account in the Cabinet (`Springfield`), choose folders and a write folder: the initial build ends
   (index count stable, no `UNRESOLVED` left).
2. Open a Doc and a Sheet in chat; search finds a native's text; save a file into the write folder.
3. In a second Google connection (`Acme`), choose only an outer folder below its top (`Acme Hamburg/Q3`): the mount
   line names it, `list_folder("Acme")` lists it, a file under it is found and opened.
4. **Parent-read backlog** on a connection with many shared items: per tick, the parent reads used (of 20) and the `UNRESOLVED`
   count, until the initial build ends. Pass: the count falls to zero within the initial build. Otherwise the stored
   out-of-scope verdict (§3.3) becomes the next unit before more outer folders are chosen.
5. Transcribe one Google recording (`Hamburg talk.mp4`); the relay's CPU cost is read from the tick's logs.
6. Disconnect the second Google connection while Gmail of the same account keeps working (I1).
7. **Revoke, positive side:** on an account whose only credential is a drive, disconnect it: the app is gone from that
   account's third-party access list.
8. P-k: the drive token's granted scopes are `drive` + identity only (and G14 logged nothing).

## 8. Deploy

On deploy the Cabinet offers "Google Drive" in "Add a drive" (from `GET /api/connectors`). No data migration: a new kind
adds connections; existing OneDrive connections, their index and their keys are untouched. No new Firestore index (the
index collections are already connection-scoped).

- **The indexer's Google client.** The indexer's deploy step (`cloudbuild-dev.yaml:169`) mounts
  `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` (the main service already has them, `:43`); its comment's
  secret count changes. Both build sites build the Google adapter only when both values are present. Without them the
  Google kind is not registered: its connections fail as "no drive adapter" (`DriveNotConnectedError`,
  `src/composition/drive_provider_router.py:38-43`) and every other kind runs; the indexer never crashes on a missing
  Google key. The drive stack in `service_container.py` is built when any drive adapter is configured, not only behind
  the Microsoft keys.
- G1 may admit a few extensionless OneDrive files on their next observation (more reads, bounded by the caps).
- G9 narrows the start window of routed files by about 12 % (§5 G9).
- G13: a deploy without `DRIVE_PARSER_USER` now fails at start on Cloud Run instead of running unsandboxed.
- Owner steps: rollout steps in the private ops repo.

**Docs at land:** `docs/05_building_blocks/user_drive/README.md` gains the Google provider (identity, feed, natives,
outer folders, the relay), `src/agents/CLAUDE.md` → Drive if a trap appears, the parent RFC's §7.1/E21 sentence (GE-22),
E37 (retired by G2) and E72 (superseded by G11), and the RFC index status.

## 9. Engineering decisions (GE-rows)

| # | Decision | Why | Rejected |
|---|---|---|---|
| GE-1 | Drive identity `google_drive:account:{permissionId}` (`about.get`). | The drive's own id from the drive API, as OneDrive's `driveId`; stable per account; the `account:` part keeps it from ever equalling an item identity (file ids hold no `:`). Mirror roots and the ledger key on it. | OAuth `sub`: the edge's identity, a second meaning for one value, and it would tie the drive identity to the identity scopes being on the drive grant. The root folder id: `google_drive:{rootId}` equals the root item's own identity. |
| GE-2 | Item identity `google_drive:{fileId}`, no account part. | A file id is the same for every account that sees it: one identity, one note across two connected accounts (parent §7.2). | Prefixing the account: two notes for one file. |
| GE-3 | Binaries: `content_version = headRevisionId`, else `md5:{md5Checksum}`. | A rename leaves both; `version` moved on a rename [PROBE]; md5 is always present on binaries [DOC]. | `version`, `modifiedTime`, an empty key. |
| GE-4 | Natives: `"r:" + head revision id` from `revisions.list` (last item, all pages) when `canReadRevisions`; else `"m:" + modifiedTime` without a call (G6). | The only rename-proof field: a rename moves `version` and `modifiedTime`, a content edit adds a revision [PROBE P-d]; a re-card costs model money, a list call costs quota only; the gate skips calls certain to fail. | `modifiedTime` or `version` alone (re-card on every rename); exporting and comparing a text hash (a planner change on the paid path, and an export of up to 2 min per rename inside the tick); calling revisions for every native. |
| GE-5 | `ContentIdentity("md5", …)` for binaries; None for natives. | md5 is the one hash always present on binaries [DOC files]; natives have none, so no "also on" for them; md5 never matches OneDrive's quickxor (parent §11.4). | sha256 (only "when available"). |
| GE-6 | Natives export: Docs `text/markdown`, Sheets xlsx, Slides `text/plain`, Drawings pdf; others metadata only. | Formats the parser reads; Markdown keeps a Doc's structure and passed where docx was refused; xlsx keeps every sheet; Slides' pptx hit the cap where text did not [PROBE P-e]. | docx for Docs (refused on image-heavy Docs, fewer list items read); CSV for Sheets (first sheet only); pptx for Slides. |
| GE-7 | G1: a domain mime table; the mime decides without an extension and wins over a disagreeing one, except between two text kinds. | The kind decides admission and reader; IANA mimes are neutral; a native's name extension says nothing of its export. | Appending `.docx` to names (breaks paths, `child_by_name`, renames); extension always first (a Doc named `brief.docx` is never read). |
| GE-8 | G9: `DOWNLOAD_TIMEOUT_S` = 300 for every provider. | One bound read by the adapters and the planner's worst case; the cost is 60 s of one start window. | A per-provider bound (a capability field and its contract cases for that difference); a Google-only timeout the planner does not know. |
| GE-9 | G2: the relay sits inside the media extractor, opened per child run. | The one place that launches the child enforces "no header in the child"; the narrowest lifetime; the stage loses a branch. | A stage-level relay port (lives a whole step, a new port); the whole file to the work dir (`/tmp` is memory: a cap far below 8 GiB); headers to ffmpeg (the bearer enters the sandbox, E24); a pipe on stdin (no seeks). |
| GE-10 | G4: `/` printed as `∕` at every path join and matched the same way. | Lossless for the provider, readable for the model. | Adapter renaming; percent-escaping. |
| GE-11 | G3: Google folders report `child_count=0`. | No field; counting costs a paged list per folder change. | Counting in the adapter. |
| GE-12 | G8: `outer_folders` on the port, member shared-drive roots included. | A real boundary question with an empty answer for area drives; a full-access OneDrive's shared items have the same shape. | A virtual folder id in `list_children`. |
| GE-13 | G8: a path whose first segment matches no root child resolves from an `outer_folders` top, with its real chain (`reached_root=False`); the policy decides. | The index and N1 print outer items from their chain top, whatever the scope; a real chain keeps E79 intact. | Resolving from the chosen scope folders (breaks under "whole drive" and below a top, couples naming to Cabinet settings); a chain built with `_chain_of` (claims the root, admits writes E79 refuses); refs only. |
| GE-14 | Enumeration: `corpora=user`, then per member shared drive its root as a folder change and one `corpora=drive` pass; `orderBy=folder` throughout. *Superseded in part by [DRIVE_SCOPE_WALK_RFC](DRIVE_SCOPE_WALK_RFC.md) §5.8, §6 (accepted, not built): only the whole drive is enumerated; a folder scope is built by walking its folders.* | Complete; `user` holds items shared to the account from non-member shared drives [PROBE]; `files.list` never returns a shared drive's root, so without the emitted root its paths and the N1 walk disagree; folders first keep parent reads down. | `allDrives` (may answer `incompleteSearch`); leaving drive roots unrecorded. |
| GE-15 | Conflict modes emulated by listing the parent and comparing `name_key`, under the per-user mutation lock. | Drive has no conflict parameter and keeps case twins; the service's rule (NFC + casefold) is OneDrive's. | An exact `name=` query (misses `report.pdf` against `Report.pdf`); accepting duplicates silently. |
| GE-16 | A Google drive token provider of its own in the `google` subpackage. | REQ-ARCH-23; Gmail's refresh is not keyed by credential. | Importing or moving Gmail's refresh. |
| GE-17 | Thumbnails fetched without the bearer, https only; the link is a secret, never logged or put in an error. | `thumbnailLink` is another host and answered without it [PROBE P-f]; a capability URL grants a rendition for hours; OneDrive's rule (`onedrive_adapter.py:542-567`). | Sending the header; logging the exception text. |
| GE-18 | Rate limits classified by `reason`. | Google answers 403 for rate limits and for permission. | Status-only mapping (a rate limit read as access denied). |
| GE-19 | `delete` trashes, never deletes. | The port's recoverable-delete contract. | `files.delete`. |
| GE-20 | Shortcuts and un-exportable natives: `mime_type=""`, metadata only. | No content to read; no Google mime reaches domain records. | Reporting Google's own mime. |
| GE-21 | Natives' `size_bytes` = Google's `size`. | What Drive shows; the download cap is enforced on the stream; chat no longer refuses a text-mime item on it (G1b). | 0 (cards and listings would read "empty"). |
| GE-22 | G7: an over-cap export is mapped at download time; this supersedes the parent's §7.1/E21 "reported `can_download=False`" sentence. | No export size exists before exporting. | An export per item to learn its size. |
| GE-23 | G10: the chat temp suffix comes from the mime when the name has none or the mime wins (G1). | markitdown converts by path. | Passing the mime into markitdown (a wider utility change). |
| GE-24 | The spec ships `offered=False` in the core and flips in slice D. | No connect button before the adapter works. | Offering it from the first slice. |
| GE-25 | The drive stack is built when any drive adapter is configured. | A Google-only configuration must still build drives. | Keeping the Microsoft-key gate. |
| GE-26 | G1b: `data:` images are dropped in a domain function called by the parser's text reader and chat's plain-text path, before every text cap; chat's 5 MB check moves after the drop for `text/*` items. | Any Markdown can carry them; the cap must count text, not images. | Stripping in the Google adapter (provider-specific, and an uploaded `.md` would keep them); a Markdown-to-HTML pass (changes more than images); keeping chat's size checks first (an image-heavy Doc with little text is refused). |
| GE-27 | The relay hardens its child-facing side: GET/HEAD, no body, header caps, a `Range` pattern, `compare_digest`, https upstream only, abort on close. | The untrusted child now talks to an HTTP server inside the process holding every connection's access token. | Trusting a loopback client. |
| GE-28 | Revoke with the refresh token; only a 200 is success. | An expired access token revokes nothing, and the log said it had; the refresh token ends the grant. Fixes Gmail's disconnect too. | The stored access token; ignoring the answer. |
| GE-29 | G11: the mount line names the chosen folders, and listing an out-of-scope root lists them; supersedes E72's refusal. | The model must be able to find the readable folders without a lucky search. | E72's refusal; the mount line alone (a listing is what the model asks for). |
| GE-30 | G12: the portable-set check applies only to segments the bot creates. | The rule is about what the bot writes; reading must accept every provider name. | Validating reads (refuses legal Google names, breaks default saves under them). |
| GE-31 | The Google adapter is built only when its client id and secret are configured; otherwise its kind is unregistered and other kinds run. | A missing key must disable one kind, never the indexer. | `os.environ[...]` (a `KeyError` stops all indexing); `.get` with an empty secret (every refresh fails silently). |
| GE-32 | G13: on Cloud Run a missing parser user fails the start. | The sandbox's uid boundary is what keeps the indexer's memory, now holding full-drive tokens, out of reach. | The warning alone (fails open after a deploy override). |
| GE-33 | G14: extra granted scopes log a WARNING; the grant is kept. | Turns P-k into a standing signal at no product cost. | Refusing such a grant (a provider change could block every connect). |
| GE-34 | `changeType=drive`: removed → resync; otherwise the drive root's change. | A lost membership must purge that drive's items; a rename must rename the root. | Skipping drive changes. |
| GE-35 | A refused stored `files.list` token restarts its pass. | List tokens have no documented lifetime; a stuck cursor would stall the build forever. | Mapping it to a plain `ValueError`. |
| GE-36 | The feed and the enumeration defer a native's head revision: `get_changes` returns such a native with `content_version_pending` and its `m:` key, and the port's `resolve_content_versions` reads it (`r:` head id; a refusal keeps `m:`; at most 4 in flight). The indexer resolves a pending change only for an item it plans as a live record, after the page's scope verdicts, in one batched call per page, and skips the call when the stored record has the change's modified time (it keeps the stored key). The planner refuses a pending change. Supersedes G6/GE-4's "calls run inside `get_changes`". | Read inside the feed, every exportable native of the account paid its full revision history before the scope filter dropped most of them: a full pass ran for a long time and indexed nothing until it ended. An unchanged modified time rules out both an edit and a rename. | Reading the revision after the planner (a record stored with a key that is not final); dropping the revision read from the feed with no resolution step (a rename re-cards, GE-4); resolving before the verdicts (out-of-scope reads again); one sequential read per kept item (a page waits on each). |

## 10. Owner decision on the parser sandbox (resolved)

**Question (security review F1).** An exploited parser child can ask the metadata server for the indexer's
service-account token; that account reads the credential collection and decrypts with the one KMS key, so it reaches
every stored credential. The owner accepted this residual in `drive_parser_sandbox_resource_isolation.md` (`:55-59`),
whose "trigger to revisit" names files nobody on the whitelist chose to put there, such as shared folders. Outer shared
folders from other organisations (G8) hit that trigger. What is new: today the same path yields Gmail read-only and
OneDrive App Folder grants; stage 3 adds Google refresh tokens with the `drive` scope (read, write and trash across the
account). It applies only once outer shared folders from other organisations are chosen for indexing: a new connection
indexes nothing until folders are chosen (parent §11.3).

**Decision (owner, 2026-11-02): option 1.** Google ships with this RFC. The separate zero-IAM sandbox service is
[UNTRUSTED_CONTENT_SANDBOX_RFC.md](UNTRUSTED_CONTENT_SANDBOX_RFC.md) (C1 drive parsing, C3 media), built after the
document service of [EXTERNAL_FILES_RFC.md](EXTERNAL_FILES_RFC.md), as adapter swaps under its parse ports (owner order
of 2026-11-03: EXTERNAL_FILES D14, sandbox O12). Until it runs, choosing outer shared folders from other organisations for indexing is the
owner's choice per connection, with the residual risk of `drive_parser_sandbox_resource_isolation.md`. Rejected:
building the separate service before Google ships (option 2); not offering outer folders in the picker until it exists
(option 3). No other part of this RFC depends on the answer.

No other owner question is open: the owner decisions of §1 cover every product choice found, and the probes settled
the engineering branches.
