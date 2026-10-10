# RFC: User Drive — the user's long-term file area

**Status:** Step 1 implemented on `feat/user-drive` (2026-10-10); consolidation token migration, live routing probe, deploy and live acceptance pending. (Draft revision 6, 2026-10-09; steps 2–3 named only.)
Supersedes the draft `ONEDRIVE_FILES_RFC.md` (revisions 1–3, never merged).
- Revision 4: the drive is a domain concept and OneDrive its first adapter — nothing the model sees or history
  stores names a provider (§4.3); every drive operation has its own intent (§4.4); roles of the file stores (§4.5);
  consolidation (§4.12).
- Revision 5, after a Fable review and owner decisions: the coordinator does not pre-fetch drive refs (§4.4);
  timeouts and cancellation (§4.13); token cache, expiry and throttling (§4.2); percent-encoded paths (§4.3);
  human-readable names and duplicate handling on save (§4.7); `update` limited to append and replace-by-file,
  full in-place editing deferred (§4.9); confirmations are receipts, not consent (§4.10); no search until step 2
  (§4.11, spike A6); vision caps (§4.6).
- Revision 6, after a second Fable review and owner decisions: the agent answers the model honestly when a mutation
  outlives its wait, and mutations of one user run one at a time (§4.13); a started mutation completes even if the
  turn is cancelled (§4.13); token cache lives at most 5 minutes (§4.2); Unicode-normalized name comparison and the
  extension rule (§4.7); vision accepts only the image formats every provider takes (§4.6).

**Owner decisions (2026-10-09):**
- Long-term files live in the owner's cloud drive, not in our storage. First provider: **OneDrive**, where the owner
  already keeps files. The drive solves upload, sync, phone access, versioning and the recycle bin.
- **Provider is an adapter detail.** Switching to Google Drive must not change prompts, intents or code outside
  the adapter and the OAuth edge.
- Access is limited to the **App Folder** (OneDrive scope `Files.ReadWrite.AppFolder`): the bot cannot see the rest
  of the drive. The whole App Folder, every subfolder included, is the bot's area. What is critical, the owner keeps
  outside it.
- The owner organises the area with **their own subfolders**. The bot never picks a place on its own.
- "Remember this file" saves to **`Inbox/`** by default; an explicitly named folder overrides it.
- **Inside the area the bot may do anything**: save, open, move, rename, create folders, update, delete files and
  folders. **Every destructive action (delete, replace) gets a receipt in chat** — a notice after the fact, not a
  request for consent (§4.10).
- Saved files get **human-readable names**; a name clash is resolved without ever overwriting (§4.7).
- `update` in step 1 is **safe only**: append text, or replace with another file. Full editing comes later (§4.9).
- Search comes **with indexing and embeddings** (step 2). The provider's plain search does not work inside the App
  Folder (spike A6, §7), so step 1 has listing only (§4.11).
- Drive operations get a **120 s** timeout (§4.13). Per-file download cap **45 MB** (§4.6).
- **The drive is the only long-term file store.** Chat attachments are transport (§4.5).
- **File operations must not leak into long-term memory** (§4.12).
- Order of work: (1) connect and work with the area → (2) indexing as knowledge (to be designed together) →
  (3) long audio/video, meeting minutes.

## 1. Problem

Files the user sends to chat are stored in GCS (`{user_id}/files/`), reachable only by the name in the
`[File: …]` label in history. There is no list, no search, no index; once history tiering drops the label, the file
is effectively lost. Large files (meeting recordings) cannot reach the bot at all: the chat path caps at 5 MB
(`src/utils/file_conversion.py:23`) and Cloud Run rejects request bodies over 32 MiB.

The motivating case is meeting recordings (step 3). It needs a place where large files arrive on their own, and
that place is the general problem: a long-term file area the bot can work with.

## 2. Why a cloud drive, why OneDrive first, why App Folder

- A cloud drive is where the owner already keeps files; upload and large files are its problem, not ours.
- Microsoft OAuth (consumers tenant), token refresh and Graph subscriptions already exist for To Do
  (`src/web/oauth_app.py:673-800`, `src/adapters/microsoft/todo_adapter.py:117-156`).
- **App Folder** (`/me/drive/special/approot`, `Apps/<app name>/`) is least privilege enforced by the provider, not
  by our code. It supports subfolders, search (`/special/approot:/{path}:/search`) and delta
  (`/special/approot:/{path}:/delta`) — the last one is what step 2 needs. Graph resolves `special/approot` even
  after the owner renames or moves the folder. Source: OneDrive dev center, "What is an App Folder" (checked
  2026-10-09). Google Drive has an equivalent (`drive.file` scope / app data), so the concept carries over.
- Graph `DELETE` moves an item to the recycle bin (folders with their subtree); OneDrive keeps file versions. Bot
  actions are therefore recoverable by the owner.

Rejected: own GCS area with a Cabinet upload page (rebuilds what the drive has); whole-drive read (`Files.Read`)
with a configured folder (the bot would see everything; narrowing later is harder than widening); several
providers at once.

Rejected: a provider MCP server instead of our own adapter (checked 2026-10-09). Microsoft's official OneDrive /
SharePoint MCP server is tenant-hosted and authenticates through Entra (work/school accounts); it does not serve a
personal account (`consumers`). Personal-account servers are community code — handing it an OAuth token to the
owner's files is a supply-chain risk. Independently of that: generic connectors ask for `Files.ReadWrite.All`, not
the App Folder; tools exposed straight to the model bypass the code-held invariants (§4.4, §4.10); and a 20–30-tool
surface reintroduces the selection confusion §4.4 designs against. "More features" is not an argument: a connector
wraps the same Graph API, and e.g. versions are one endpoint (`/items/{id}/versions`) to add when needed. An MCP
server remains possible later as another adapter behind `UserDrivePort`.

## 3. Step 1 — scope

| The user says | The bot does | Intent |
|---|---|---|
| "remember this file" (a chat attachment, or a document the bot produced) | saves it under a readable name to `Inbox/`, or to the folder the user named | `save_file_to_drive` |
| "what's in Meetings?" | lists a folder (bounded) | `list_files_in_drive` |
| "find the contract with X" | not in step 1 — lists folders instead; search comes in step 2 (§4.11, spike A6) | — |
| "open it" | downloads and reads with the existing converters | `open_file_from_drive` |
| "move it to Meetings/2026", "rename to …" | moves and/or renames a file or folder | `move_file_in_drive` |
| "make a folder Contracts" | creates a folder (with missing parents) | `create_folder_in_drive` |
| "add a line to my notes" / "here is the new version, update it" | appends text, or replaces with another file (receipt) | `update_file_in_drive` |
| "delete it" | deletes a file or folder (receipt) | `delete_file_from_drive` |

`open_file` and `delete_file` keep serving chat uploads, delivered documents and skill files, unchanged.

Out of scope: indexing/embeddings and proper search (step 2), long audio/video (step 3), full in-place editing,
change notifications, a second provider, saving chat attachments to the drive automatically.

## 4. Design

### 4.1 Connection (provider edge)
- New credentials record `OAuthCredentials(provider="microsoft_onedrive")`, separate from `microsoft_todo`, so each
  can be revoked on its own and scopes don't mix. Scope: `Files.ReadWrite.AppFolder offline_access`.
- Same Azure app registration as To Do (`Alek-bot`); the owner adds the delegated `Files.ReadWrite.AppFolder`
  permission (§8). The app folder is named after the registration at first call — `Apps/Alek-bot`
  (`Приложения/Alek-bot` in a Russian UI); renaming the registration later does not rename it.
- Routes `/auth/connect-onedrive` + `/callback`, mirroring the To Do pair; Cabinet gets a connect/disconnect row
  labelled with the provider. This is the only user-facing place the provider's name appears, apart from the
  adapter's `display_name` (§4.3).

### 4.2 Port, adapter, transport
- `UserDrivePort` (`src/ports/user_drive_port.py`) — a system boundary, with a second consumer coming in step 2
  (indexing). Methods: `is_connected`, `disconnect`, `get_root`, `get_item`, `list_children`, `search`, `download`,
  `upload`, `replace_content`, `move` (new parent and/or new name), `create_folder`, `delete`, plus a
  `display_name` property. All keyed by `user_id` and an opaque item id.
- Domain `src/domain/user_drive.py`: `DriveItem` (`item_id`, `name`, `path` relative to the area root, `is_folder`,
  `size_bytes`, `mime_type`, `modified_at`, `web_url`), the ref and name helpers, the operation
  outcomes, the errors, `DEFAULT_INBOX_FOLDER = "Inbox"`, the size caps.
- `UserDriveService` (`src/services/user_drive_service.py`) holds the rules on top of the port: destination-folder
  resolution (§4.8), duplicate handling on save (§4.7), append (§4.9), root protection and the subtree count
  (§4.10), list bounds. The agent stays a thin dispatcher.
- `OneDriveAdapter` (`src/adapters/microsoft/onedrive_adapter.py`) — Graph REST over aiohttp, like To Do. Upload session above
  the simple-upload limit; `conflictBehavior=rename`, so a save never overwrites. Move/rename is one `PATCH`.
  Composition picks the adapter; nothing else imports it.
- **Transport rules** (the Fable review found the per-call cost was what made timeouts likely):
  - **Token:** `MicrosoftGraphTokenProvider` (`src/adapters/microsoft/graph_auth.py`, extracted from To Do, shared
    by both adapters) caches the access token in memory for **at most 5 minutes** (and never past five minutes
    before expiry), with a per-user `asyncio.Lock` around refresh: one credentials-store read per 5 minutes, not per
    HTTP call. Owner decision: a Cabinet disconnect or a reconnect with another account therefore takes effect on
    every Cloud Run instance within 5 minutes — for To Do too, where it is immediate today. An hour (the token's
    life) was acceptable for the disconnect case, but a reconnect to a different account would keep writing to the
    old drive for that long, silently; 5 minutes costs almost nothing. Microsoft itself cannot revoke an issued
    access token early, so an "instant" kill switch does not exist at the provider either. The To Do suite must
    stay green unchanged.
  - **One HTTP session per operation**, not per request.
  - **Expiry and revocation:** a refresh answered `invalid_grant`, or a Graph `401` that survives one forced
    refresh, becomes `DriveNotConnectedError` — the agent tells the user to reconnect in the Cabinet. Refresh
    tokens lapse when the drive goes unused for long, so this path will be hit.
  - **Throttling:** `429` and `503` are retried up to three times, honouring `Retry-After` (capped at 30 s; an
    HTTP-date or unparsable value falls back to exponential backoff). The 401 retry has its own counter, so
    throttling never consumes the forced refresh.

### 4.3 What the model sees and history stores: no provider
The label, the ref and every intent description name **the drive**, never a provider:
- Ref: **`drive:<item_id>`**. The id is opaque — only the adapter knows its shape. Same prefix pattern as
  `skill:<name>/<path>` (`src/domain/skill.py:29`).
- Label: `[Drive: Встречи/2026/standup.m4a (12.3MB) ref=drive:…]`.
- Descriptions say "the user's drive (their long-term file area)". Receipts say "диск"; the adapter's
  `display_name` may be appended, never hardcoded in a template.

**Paths.** Graph returns `parentReference.path` **percent-encoded** (`itemReference` docs: "Percent-encoded path
… e.g. `/drive/root:/Documents/my%20file.docx`"). The adapter makes it relative to the area root and then
unquotes it, so a label shows `Встречи/2026`, not `%D0%92…`. If a path does not sit under the area root (prefix
mismatch), the adapter logs a warning and re-reads the item once; it never silently labels the item as top-level.

**Refs must outlive the turn.** Tool results are not part of session history, so a `drive:` ref returned by
`list_files_in_drive` would be gone on the next turn ("open the second one"). Every drive intent therefore
returns the items it touched as `history_context={"drive_context": [...]}` (path + ref per item) — the mechanism
`EmailSearchAgent` uses (`email_search_context`). `ConversationHandler` appends `*_context` blocks to `full_text`
only; consolidation reads the summary, so these blocks never reach long-term memory (§4.12). They live as long as
the turn stays inside the full-text tiering window (`history_recent_full_turns`); after that the model lists
again. That is enough for "open the second one"; long-term recall of files is step 2.

Identity is the item id, not the path: the owner moves and renames things, the provider id survives both.

**Honest limit.** A provider switch is still a data migration: the new provider has new ids, so `drive:` refs
already in history stop resolving. Code and prompts do not change; history refs do. Refs that survive a switch
need our own registry (stable id → provider + provider id). Step 2 creates one record per file for indexing anyway;
its id becomes the ref then. Not built in step 1.

### 4.4 Intents: the store is in the name
Every operation on a drive file has its own intent with `drive` in the name; the chat-file intents are untouched.
The rule the model needs is literal: **a `[Drive: …]` label → an intent with `drive`.** A shared `open_file`
across stores was rejected: after opening a chat file through it, the model reasons "I am working with files" and
calls drive intents on a chat ref. Prepositions follow direction: `to` — enters the drive, `in` — stays inside,
`from` — read or removed out of it.

Code backs the names, because the model will still miss sometimes:
- **Mutating intents are strict.** A drive intent given a non-`drive:` ref — and `delete_file` given a `drive:`
  ref — fails with a hint naming the right intent. Deleting from the wrong store is impossible by construction.
- **Reading is lenient.** `open_file` given a `drive:` ref just opens it (advertise strictly, accept liberally —
  `decisions/mcp_tool_schema_liberal_input.md`).

**No pre-fetch.** `AgentCoordinator._execute_sync/_execute_async` call `_resolve_file_refs` before dispatch: any
`file_ref` is downloaded and converted to text in advance (`agent_coordinator.py:521-523, 575-605`). For
`FILE_MANAGEMENT` that is pure waste — the agent fetches what it needs itself — and for drive refs it would download
before a delete and log every not-connected call at ERROR (an `#alerts-dev` page per call). A descriptor flag
`prefetch_file_ref` (default `True`) is set to `False` on `FILE_MANAGEMENT`. This also closes the backlog item
"File resolution double-fetch" for chat `open_file`.

All drive intents live on `FILE_MANAGEMENT` — one zero-LLM agent for every store. Known cost: seven more intents in
the orchestrator's tool description on every request. One `manage_drive(action=…)` would save tokens but throw
away the name signal, which is the whole point; not done. When the drive is not connected the intents are still
declared and fail with a connect hint (§4.11); hiding intents per user needs a mechanism we do not have.

### 4.5 Roles of the file stores: transport vs storage
The To Do / reminders overlap showed the failure: two equal stores for one concept and no rule which is canonical,
so the model drifted to the closer one. Files get an explicit hierarchy instead:
- **Chat attachments and documents the bot produced (GCS) are transport**: input and output of a conversation,
  temporary by definition (the agent already tells the model "files are kept for 90 days").
- **The drive is the only long-term store.** Anything to be kept lives there.

So the stores form a pipeline (chat → "remember" → drive), not competitors. Two structural facts keep drift
unlikely: chat files have no list, so "what do I have in …" can only go to the drive; and saving is triggered by the
user's explicit word, not chosen by the model. The remaining trap is "it is already saved" — a chat file has a label
and a store, so on "remember this" the model may do nothing. `save_file_to_drive`'s description carries one line:
chat attachments and generated documents are temporary; "remember / save / keep / put" a file always means this
intent. Prompt-tested (§7).

### 4.6 Reading (`open_file_from_drive`)
- `FileConversionService` dispatches `drive:` refs to `UserDrivePort`.
- **Trap:** today the mime type is guessed from the ref string (`mimetypes.guess_type(ref)`), and `drive:<id>` has
  no extension. For drive refs, name and mime type come from `get_item` metadata.
- **Every size check happens on metadata, before downloading:**
  - any download: `MAX_DRIVE_DOWNLOAD_BYTES` = 45 MB (the existing video re-send cap);
  - text conversion: the existing 5 MB `MAX_FILE_BYTES`;
  - **vision** (the file itself handed to the model): images ≤ 5 MB, PDFs ≤ 20 MB — below the strictest provider
    input limits, so a large scan cannot fail Smart's whole turn. Anything over its cap gets a clear "too large to
    open yet" answer instead of a download.
  - **Vision formats:** only JPEG, PNG, GIF and WebP images go to the model — the set every provider accepts.
    Others (HEIC, the iPhone default; TIFF; BMP) get a clear "this format cannot be viewed yet" answer.

### 4.7 Saving (`save_file_to_drive`)
- `context.file_ref`: a chat attachment ref or a delivered-document key — both resolve through
  `FileConversionService.resolve_bytes`. `context.folder` (optional): a path the user named explicitly; absent →
  `Inbox/`. `context.name` (optional): a readable name the orchestrator chooses when the source name is poor.
- **Default name.** Delivered documents are keyed `docs/{user}/{uuid}-{filename}`
  (`document_delivery_service.py:87`), so the default name strips that uuid prefix; chat uploads keep their name.
  The source extension is appended unless the requested name already has a known file extension ("Договор v2.1"
  → "Договор v2.1.pdf"; "notes.md" stays). Characters OneDrive forbids (`" * : < > ? / \ |`) become `_`.
- **Name comparison** (duplicates, clash detection) uses Unicode NFC plus case folding: files from macOS/iOS arrive
  with decomposed (NFD) names, and a plain comparison would treat `й` and `й` as different.
- **Name clashes, never an overwrite:**
  - a file with the same name and the **same content** already sits in the folder → nothing is uploaded; the
    reply says it is already there and returns the existing ref. Same content = same size and same bytes; the
    existing file is downloaded only in that rare same-name, same-size case, so no provider-specific hash
    algorithm is needed. This also absorbs a retried save after a timeout (§4.13);
  - same name, **different content** → saved alongside under the provider's suffixed name (`conflictBehavior=
    rename`), and the reply states the actual name, so nobody believes it kept the original;
  - replacing content is only ever `update_file_in_drive` (§4.9).
- Parallel saves into a folder that does not exist yet ("save these three") race to create it; a create answered
  `409` re-lists the parent and uses the folder that won.

### 4.8 Destination folders (`save_file_to_drive`, `move_file_in_drive`, `create_folder_in_drive`)
Resolved segment by segment, deterministically (the agent is zero-LLM):
- each segment matches an existing child folder **case-insensitively** (`встречи` → `Встречи`), so a casual
  spelling does not create a twin;
- a segment with no match is created, with every missing segment below it;
- the reply names the final path and the folders created, so a wrong guess is visible at once.
Typos and synonyms are the orchestrator's job: it can call `list_files_in_drive` first.

### 4.9 Move, rename, update
- `move_file_in_drive(file_ref, folder?, new_name?)`: files and folders. The item id is unchanged, so refs in
  history keep working. No receipt: reversible, and the reply states the new path.
- `create_folder_in_drive(folder)`: per §4.8.
- `update_file_in_drive(file_ref, append_text | source_ref)` — **safe modes only in step 1**:
  - `append_text`: added at the end of a text file (`.md`, `.txt`, `.csv`, `.json`, `.yaml`, or a `text/*` type,
    ≤ 5 MB), on a new line. Nothing existing is lost, so no receipt.
  - `source_ref`: the whole content replaced by another file (a new version the user sent, or a document the bot
    just generated). Explicit, versioned by the provider, and receipted with old → new size.
  - **Not in step 1:** the model writing new full text over a file. "Add a line" answered by rewriting the file
    risks truncating it to that line. Full in-place editing (read → change a part → write, with a diff or
    fragment-level edits) is its own design, after step 1.

### 4.10 Deleting (`delete_file_from_drive`) and receipts
- Files and folders; a folder goes to the recycle bin with its subtree and restores as a whole. The area root
  cannot be deleted or moved.
- **Receipts, not consent.** The owner allowed the bot to delete and replace; what they asked for is to always
  see it happened. So after a destructive action a deterministic, localized message goes to chat via
  `UserNotificationService.notify_raw`, independent of how the orchestrator words its reply (the reminder-CRUD
  pattern). Nothing waits for approval.
  - `Удалено с диска: Встречи/2026/standup.m4a`;
  - `Удалена папка: Встречи/2025 (файлов: 14)` — counted before deletion by walking the subtree (Graph
    `folder.childCount` covers direct children only), so an unintended deletion is noticed at once;
  - `Обновлено: Договоры/аренда.pdf (12KB → 14KB; предыдущая версия в истории версий)`.
- The receipt is part of the operation: the delete or replace and its receipt run together, shielded from
  cancellation (§4.13), so a receipt is never lost to a timeout.

### 4.11 Listing, search, not connected
- `list_files_in_drive(folder?)`: children of a folder, folders first, bounded (count cap, stated when cut).
- `search_files_in_drive(search_text)`: the provider's plain search scoped to the area, nothing more. The
  description states what the spike shows it matches (names, or names and content) and that a file saved moments
  ago may not appear yet (provider indexing lag). **If the spike shows search does not work inside the App Folder,
  the intent is dropped from step 1** and only listing remains until step 2. Proper search — ranking, content,
  meaning — is designed with indexing and embeddings in step 2; no interim fallback is built.
- Without credentials, or after the access expired, every drive intent fails with "connect your drive in the
  Cabinet". No fallback to GCS.

### 4.12 Long-term memory: file operations stay out
**What reaches consolidation from a file interaction** (checked in code):
- the user turn — "remember this file" plus the `[File: …]` / `[Drive: …]` label;
- the model turn's **summary** (`p.text`, `src/domain/consolidation_serialization.py`) — e.g. "Saved to Inbox/";
- **not** the receipts: `notify_raw` only sends, it never appends to history;
- **not** file content or `drive_context`: tool results are not in history, and `*_context` blocks go to
  `full_text` only.

**Risk.** Consolidation turns the save/move/delete exchanges into facts like "user keeps the lease contract in
Inbox". Beyond noise, such a fact **goes stale** the moment the owner moves the file by hand, and then
contradicts the drive — which is itself the record of what exists and where. Same reasoning as the existing
reminders clause: a thing with its own source of truth is not restated in memory.

**Change.** One line in `Trivial_Exclusions` of the `CONSOLIDATION_TAXONOMY` token (Firestore; backup before the
edit, as with every token change; applied only with the owner's go-ahead):
> "File operations: saving, opening, moving, renaming, deleting files or folders on the user's drive or in chat — the
> drive is the record of what exists and where."

What the user *says about* a file's subject stays eligible ("my lease ends in December 2027" is a fact whether or
not a contract file exists). Whether facts may be drawn from opened file content is a step 2 question.

**Verification.** A Stage-1 dry run built on `scripts/consolidation/test_stage1_classification_dryrun.py`'s
pattern — real Stage-1, fact writes intercepted, the rule swapped into the *assembled* prompt so Firestore is
untouched — fed a synthetic batch of file exchanges plus one real fact. Baseline vs new, three runs each (single runs
are not evidence — `decisions/directive_applicability_gate.md` § Variance). **A leak is any written fact that
mentions a file location or operation, including a mixed one** ("lease ends Dec 2027, contract in Договоры/") — the
likeliest leak is exactly that mix. Expected with the line: zero leaks, and the real fact still written on its
own. If baseline already shows none, the line is still added (the risk grows with use) and recorded as preventive.

### 4.13 Timeouts and cancellation
`FileManagementAgent` runs under `BaseAgent`'s `asyncio.wait_for` with `timeout_ms=30_000`
(`user_agent_factory.py:828`, `base_agent.py:680`), which cancels mid-operation. Drive work is slower than GCS:
a 45 MB download, chunked uploads, a subtree walk, search with a follow-up read per hit. On a timeout `BaseAgent`
returns "Agent failed… timeout" (`base_agent.py:634-662`, which also counts a circuit-breaker failure), and
`DelegationEngine` hands the model "rejected… correct your input and try again" (`delegation_engine.py:715`) — while
the provider may already have applied the change. So:
- the agent's timeout becomes **120 s**;
- every mutating operation — provider call **plus its receipt** — runs as a task inside `asyncio.shield`: it
  completes and its receipt goes out whatever happens to the call that started it;
- **the agent waits for it at most 100 s** — inside its own 120 s, so `BaseAgent`'s timeout never fires for a
  mutation. If the operation is still running, the agent answers the model with a success-shaped status: "the
  operation is still running and may complete; check with a listing before retrying; a receipt follows for
  deletes and replaces". No false "failed", no circuit-breaker failure, no invitation to retry blindly;
- **mutations of one user run one at a time** (a per-user `asyncio.Lock`). A retry that arrives while the first is
  still running waits for it, and then the duplicate check (§4.7) sees the finished first save. Without the lock a
  mid-flight retry would upload a second copy or append a line twice — the duplicate check alone only covers
  retries after completion;
- a shielded task that ends after nobody waits for it logs its outcome, including any exception (a done-callback),
  so no failure is lost to "Task exception was never retrieved".

**Cancellation (owner decision).** If the user cancels a long turn while a drive mutation has started, the mutation
finishes and its receipt posts. A file operation half-done is worse than one done and shown.

Three timeouts still open the agent's circuit breaker for all its intents, chat `open_file` included; reads are not
shielded, and the transport rules (§4.2) are what keep their timeouts rare.

## 5. Steps 2 and 3, and later (named, not designed)
- **Step 2 — indexing and search.** Delta on the area → per-type extraction → vectors → memory search, modelled on
  Gmail indexing (`IndexedEmail`); proper search replaces the plain one (§4.11). Open: what qualifies (everything vs
  a triage like email classification), granularity (per file vs chunks), how a file the owner put there themselves
  enters, update and delete semantics, whether chat attachments go to the drive, the stable-id registry (§4.3),
  memory vs index for file content (§4.12).
- **Step 3 — long audio/video (meeting recordings).** The motivating case of this RFC. Notes from the 2026-10-09
  discussion, to start the step 3 design from:
  - **Owner's need:** (1) a text file with the content of a meeting from an audio or video recording — the priority;
    (2) later, real video understanding (what is on screen: slides, shared documents).
  - **Sources:** Nextcloud, Zoom, phone recordings. Step 3 starts with files the owner can download and drop into
    the drive; length and language per meeting are not known in advance (multilingual audio is normal here).
  - **Why today's path cannot do it** (checked in code): chat attachments cap at 5 MB (`MAX_FILE_BYTES`) and
    converted text is truncated at 30k characters (`MAX_CONVERTED_CHARS`, `src/utils/file_conversion.py`);
    `video/*` has no conversion path (only a fallback prompt); transcription runs inline in the chat turn; OpenAI
    speech-to-text takes ≤ 25 MB per request, so long audio needs chunking; `gpt-transcribe` has no speaker
    diarization.
  - **Pipeline sketch:** a **Cloud Run Job** (like DeepResearch — not the 1-vCPU request service; Cloud Run's /tmp
    is RAM): download by item id via the pre-authenticated download URL → ffmpeg (already in the Dockerfile)
    extracts audio from audio or video, mono 16 kHz, low bitrate → chunks of ~10 min → parallel transcription via
    `AudioTranscriptionPort` (`OpenAITranscriptionAdapter`, per-user `voice_languages`) → stitch in order →
    LLM pass (map-reduce for long transcripts) → minutes: summary, decisions, action items with owners, open
    questions; optionally the full transcript as a second file → `.md` saved **next to the recording** on the drive
    + a chat message.
  - **The minutes format** is the owner's own procedure and belongs in a **skill** (§6), editable without a deploy.
  - **Open questions:** speaker diarization (check current provider docs for a diarizing model, e.g. OpenAI's
    `gpt-4o-transcribe-diarize` — verify, do not assume); trigger (explicit "make minutes of this recording" vs a
    watched folder such as `Meetings/`); failure of one chunk (retry, then an explicit gap marker); cost per hour of
    audio; whether minutes facts go to long-term memory (relates to §4.12 and step 2).
  - Rejected earlier on 2026-10-09: a Cabinet upload page with browser-to-GCS signed uploads — superseded by the
    drive, which already solves upload of large files.
- **Later — full in-place editing** of drive files (§4.9).

## 6. Why an agent, not a skill
A skill (`docs/10_rfcs/AGENT_SKILLS_RFC.md`) is a procedure: text loaded into Smart's context that composes existing
tools, with no code and no access of its own. This feature is a capability — an external API behind OAuth, byte
transfer, a size check before download — and its safety invariants (receipt for every destructive action,
subtree count, root protection, store-mismatch refusal, no overwrite on save) must hold every time, which text the
model usually follows does not guarantee. `FileManagementAgent` is zero-LLM; there is no prompt to move into a
skill. Skills enter at step 3: the minutes format composes these capabilities (find the recording → transcribe →
structure → save next to it) and is edited without a deploy.

## 7. Files and verification

New: `src/domain/user_drive.py`, `src/ports/user_drive_port.py`, `src/adapters/microsoft/onedrive_adapter.py`,
`src/adapters/microsoft/graph_auth.py`, `src/services/user_drive_service.py`. Changed:
`src/agents/file_management_agent.py`, `src/services/file_conversion_service.py`,
`src/infrastructure/agent_manifest.py`, `src/infrastructure/agent_registry.py` + `agent_coordinator.py`
(`prefetch_file_ref`), `src/web/oauth_app.py`, Cabinet, `src/composition/` wiring, `src/utils/capabilities.py`,
locales, the `CONSOLIDATION_TAXONOMY` token. Docs: root `CLAUDE.md`, `src/agents/CLAUDE.md` (FileManagement),
`docs/05_building_blocks/file_storage/README.md`, `docs/07_deployment/README.md`.

- **Phase 0 spike (before code)**, `scripts/onedrive/probe_appfolder.py` against the owner's account, with
  **Cyrillic and space-containing names**: App Folder creation and name; nested subfolders; `parentReference.path`
  shape and encoding on GET, `/children`, search, and the PATCH / PUT / POST responses the adapter builds labels
  from; simple-upload limit; `PATCH` move keeps the id; DELETE of a non-empty folder lands in the recycle bin and
  restores whole; overwrite creates a version; the suffix format of `conflictBehavior=rename`; search inside the
  area (subfolders? names or content? lag).
- Wire tests for `OneDriveAdapter` at the aiohttp boundary (`docs/how_to/ADAPTER_WIRE_TESTING.md`), with
  percent-encoded fixtures.
- Unit: ref and name helpers; mime-from-metadata branching; every size cap checked before download; folder
  resolution (case-insensitive, missing segments created, `409` race); duplicate save; append; root protected;
  store-mismatch refusal on every mutating intent and lenient `open_file`; receipts on delete/replace (subtree
  count, sizes), none on move/rename/append; shielded mutation survives cancellation; a mutation over its 100 s
  wait answers "still running"; one user's mutations run one at a time; HEIC refused for vision; NFC name
  comparison; token cache expires after 5 minutes; not-connected and expired access; `prefetch_file_ref=False` skips pre-fetch; no provider name in labels, refs or descriptions; To Do suite
  unchanged.
- Routing prompt tests on the live Smart: "запомни этот файл" → `save_file_to_drive`, "запомни, что …" →
  `save_to_memory`; "запомни в Встречи" fills `folder`; a chat file opened on one turn, then "move it to Встречи"
  on the next → `save_file_to_drive`, not `move_file_in_drive`; a listing, then "open the second one" on the next
  turn → `open_file_from_drive` with the second ref. The previous turn is seeded in the shape production stores:
  the reply text plus the `{"drive_context": [[…]]}` JSON block, refs not repeated in the prose.
- Consolidation dry run per §4.12.
- Live: connect; save by default and into a named folder; save the same file twice (no duplicate) and a different
  file under the same name (suffix, stated); move a file by hand in OneDrive and open it by the old ref; rename and
  move through the bot; append to a `.md`; replace a file by a new version (receipt with sizes); delete a
  file and a folder and see the receipts; restore the folder from the recycle bin.

### Spike results (2026-10-10)

Run against the owner's account with `scripts/onedrive/probe_appfolder.py`. Sign-in used the device-code flow
instead of the browser + localhost redirect (the spike ran from a remote session, which a localhost redirect
cannot reach); the registration has "Allow public client flows" enabled for this. Checks A1–A8 are unchanged.

- **App Folder:** created as `Alek-bot` under the account's apps folder. On this account that folder is
  localised and had been moved by the owner (`/drive/root:/ARCHIVE/Приложения/Alek-bot`; moved back to the
  drive root after the spike). The adapter must take the root's absolute path from `special/approot`, never
  assume `/Apps/…`.
- **A1 `parentReference.path`:** present on GET item, `/children`, and the PATCH / PUT / POST responses. Search
  returned no hits (A6), so the search shape is unobserved. **Two prefix forms:** POST (create folder) responses
  use `/drives/<drive-id>/root:/…`; GET, `/children`, PUT and PATCH use `/drive/root:/…`.
- **A1b encoding:** **not percent-encoded.** Cyrillic and spaces come back raw (`…/Приложения/Alek-bot/Проба …/
  Встречи 2026`) on every observed response. `unquote` is a no-op on them; it would only alter a name that
  literally contains `%` followed by two hex digits.
- **A2:** `PUT …/content` of exactly 4 MiB → 201.
- **A3:** `PATCH` move + rename → 200, id kept.
- **A4:** `DELETE` of a non-empty folder → 204; GET afterwards → 404. Recycle-bin restore of the whole subtree:
  confirmed by the owner (folder, subfolder and files came back).
- **A5:** GET item carries `@microsoft.graph.downloadUrl`.
- **A6:** `GET /me/drive/special/approot/search(q=…)` returned **no hits** for a name query and for a content
  query, polled for 60 s each, on files in a nested subfolder.
- **A6 re-probe (`scripts/onedrive/probe_search.py`, 15 min budget):** a fresh file in a nested folder; both
  `special/approot/search(q=…)` and `items/{approot-id}/search(q=…)`, for the full name, a name token and a
  content token, every 30 s. **No hits for ~11 minutes (200, empty), then every query → 401
  `unauthenticated` "Unauthorized when calling Substrate Search"** with the same, still-valid token that kept
  working for create/delete. **Outcome per §4.11: search does not work inside the App Folder;
  `search_files_in_drive` is dropped from step 1** (Tasks 7, 8, 11, 12 of the step 1 plan lose it). Listing
  remains; search comes with step 2's own index.
- **A7:** `PUT …/content` on an existing item → 200; `/versions` lists 2.
- **A8:** `conflictBehavior=rename` on a clash produced `заметка 20261010154421 1.txt` — `<stem> 1<ext>`.

- **Layout (owner decision 2026-10-10):** the three Graph modules live in one adapter sub-package,
  `src/adapters/microsoft/` (`graph_auth.py`, `todo_adapter.py`, `onedrive_adapter.py`), because REQ-ARCH-23
  forbids a top-level adapter importing another; imports inside one sub-package are allowed (as in `slack/`,
  `telegram/`). The To Do adapter moves there from `src/adapters/microsoft_todo_adapter.py`.
- **Paths (owner decision 2026-10-10, after the spike):** keep `unquote` (a no-op on the raw paths observed,
  correct if Graph ever encodes), and normalise both prefix forms — `/drive/root:` and `/drives/<id>/root:` —
  to one before comparing with the app-folder root, so a create-folder response does not take the re-read path.

## 8. Manual steps for the owner
1. Azure registration `Alek-bot`: add delegated `Files.ReadWrite.AppFolder`.
2. Spike only: add redirect URI `http://localhost:8765/callback`; remove it after the spike.
3. Deploy: secret `ONEDRIVE_REDIRECT_URI_DEV` = `<service URL>/auth/connect-onedrive/callback`, and the same URI
   registered on `Alek-bot`.
4. Connect the drive in the Cabinet.
5. Approve the `CONSOLIDATION_TAXONOMY` edit after seeing the dry-run numbers (§4.12).
