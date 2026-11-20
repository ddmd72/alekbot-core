<!-- living-doc
covers:
  indexer_main.py
  src/web/indexer_worker_app.py
  src/domain/drive_*.py
  src/domain/document_extraction.py
  src/domain/document_kind.py
  src/domain/batch_transcription.py
  src/domain/connection_scope.py
  src/domain/deep_research_drive_copy.py
  src/domain/place_lookup.py
  src/domain/text_data_uris.py
  src/domain/user_drive.py
  src/ports/drive_connection_repository.py
  src/ports/drive_index_repository.py
  src/ports/drive_mirror_repository.py
  src/ports/drive_note_repository.py
  src/ports/user_drive_port.py
  src/ports/content_extractor_port.py
  src/ports/image_triage_port.py
  src/ports/document_profiler_port.py
  src/ports/passage_hint_port.py
  src/ports/batch_transcription_port.py
  src/ports/media_audio_port.py
  src/adapters/drive_parser/
  src/adapters/google/google_drive_*.py
  src/adapters/microsoft/onedrive_adapter.py
  src/adapters/microsoft/graph_auth.py
  src/adapters/firestore_drive_connection_repo.py
  src/adapters/firestore_drive_index_repo.py
  src/adapters/firestore_drive_mirror_repo.py
  src/adapters/firestore_drive_note_repo.py
  src/adapters/in_memory_drive_*.py
  src/adapters/subprocess_content_extractor.py
  src/adapters/gemini_batch_adapter.py
  src/adapters/geo_place_table.py
  src/agents/drive_*.py
  src/infrastructure/drive_*.py
  src/services/drive_connection_service.py
  src/services/drive_disconnect_service.py
  src/services/drive_indexing_service.py
  src/services/drive_note_deleted_search.py
  src/services/drive_note_lifecycle.py
  src/services/drive_note_service.py
  src/services/drive_search_service.py
  src/services/drive_sync_scheduler_service.py
  src/services/drive_transcript_mirror.py
  src/services/drive_transcript_stage.py
  src/services/user_drive_service.py
  src/services/deep_research_drive_copy_service.py
  src/composition/drive_agent_build.py
  src/composition/drive_connection_setup.py
  src/composition/drive_indexing_setup.py
  src/composition/drive_model_setup.py
  src/composition/drive_note_setup.py
  src/composition/drive_passage_runner.py
  src/composition/drive_profiler_runner.py
  src/composition/drive_provider_router.py
  src/composition/drive_reader_runner.py
  src/composition/drive_specialist_build.py
  src/composition/drive_transcript_runner.py
  src/composition/drive_triage_runner.py
  src/assets/geo/
  scripts/geo/
  scripts/onedrive/
  scripts/infrastructure/drive_*.py
  scripts/infrastructure/create_drive_*.sh
  scripts/validation/drive_*
-->
# User Drive (Building Block)

## 0. How to answer questions about the drive index

Find the kind of question, then read the section named. The code is the ground truth; the RFCs hold the reasoning
([USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md): §24 content, §25 Search v2, §26 card accuracy
and capture, §27 transcripts, §28 passages, §29 the open bound; [MULTI_DRIVE_RFC.md](../../10_rfcs/MULTI_DRIVE_RFC.md),
[GOOGLE_DRIVE_RFC.md](../../10_rfcs/GOOGLE_DRIVE_RFC.md), [DRIVE_SCOPE_WALK_RFC.md](../../10_rfcs/DRIVE_SCOPE_WALK_RFC.md),
[DRIVE_FILE_NOTES_RFC.md](../../10_rfcs/DRIVE_FILE_NOTES_RFC.md),
[DRIVE_SPECIALIST_RFC.md](../../10_rfcs/DRIVE_SPECIALIST_RFC.md)). Decision records are in
`docs/04_solution_strategy/decisions/` (`drive_*.md`).

| Question | Read |
|---|---|
| What is in the index, what a record or a card holds, why a folder is one card | §3 |
| What a key is (`w:`, `x:`/`xl:`, `e:`/`ef:`, `lw:`, …); how word forms, other scripts and numbers meet | §3.1 |
| Which keys carry the connection and which belong to the user or the drive; why the transcript store survives a purge | §3 (keys by scope) |
| Why a file was or was not read; what happens to it from the moment it lands on the drive | §4 (steps 1–9) |
| When a new file becomes searchable; why an edited file still shows its old text | §4.15 |
| Why a scan, a screenshot or a PDF went to the model, and what the model returns | §4 steps 5–6 |
| What image triage decides; why a photo has no description | §4 step 5 (images), §4.15 |
| What the profiler and the passage hints write about a file, how it is verified; why a file is "not described yet" | §4.17 |
| What a record's `state` / `reason` means, which outcomes retry and which are final | §4.10 |
| What happens when the model provider is down; the Gemini fallback; which failures are charged to a file | §4.10, §4.16 |
| Why a file is waiting: quiet period, monthly budget, model outage, re-read cooldown, card cap, tick budget | §4 steps 1–3, §4.11 |
| A very large file: what is read, what is not, how it is marked | §4.11 |
| A new parser, reader, prompt, keyer or embedding model: what gets redone and what it costs; how to switch the embedding model | §4.12, §4.13 |
| Is the parser a security boundary; what a hostile document can and cannot make the bot do | §4.14 |
| Where the model prompts live; how to switch a model agent between GPT and Claude | §4.16 |
| How the tick schedules its work, leases, errors and continuations | §5 |
| A file or folder "the bot cannot open"; the chat notice; how it heals | §4.18 |
| How a file's capture (scan, photo of a document, screenshot, digital) is decided | §4.19 |
| What `card accuracy: N%` is; when Smart opens a file anyway | §4.20, §6.7 |
| What triage is told about a photo's place; what a `scene:` line is | §4.21 |
| Why a recording is searchable by what was said; its cost, states, who can switch it on | §4.23 |
| What `_Transcripts` is, why a file appears, moves or vanishes there; why the bot refuses to write into it | §4.23 (the mirror folder) |
| What the owner's note on a file or a drive is; which tool writes what; erase; deleted files | §4.24 |
| "What does the file say about X": stored passages instead of opening files; the write gate; why a long file was not opened whole | §4.25 |
| How a search decides which files to show and in which tier; "No file holds …"; "ignored as common" | §6.1, §6.2 |
| What the coverage line means; what Smart sees on a card; dates in queries and filters | §6.3, §6.5 |
| Which drives a search read; what "also on" and `drives not searched` mean | §6 (which drives) |
| How Smart reaches the drive: the specialist, its model, statuses, receipts | §6.7 |
| Which drive a file is on; what a connection, a handle, a ref tag, a mount and the primary are; why a ref says "search again" | §1.2 |
| What the user can change (scope, write folder, primary, rename, disconnect); why the bot cannot; what a scope change does to the index | §1.2, §3, §5 |
| What a disconnect removes and keeps | §1.2, §4.23, §4.24 |
| The Google Drive provider: identity, natives, shared items | §1.3 |
| Why was file X not indexed; logs, counters, owner scripts | §7, §7.1 |
| Deploy order, owner steps, rollback, levers | §7.2 |
| Where to start reading the code | §8 |

---

## 1. Purpose

The user's long-term file area: their cloud drives, behind the provider-neutral `src/ports/user_drive_port.py::UserDrivePort`.
The drive is the source of truth; the index is a disposable mirror that can always be rebuilt from it. The block does
five things:

- **File operations (Step 1).** The bot lists, opens, saves, moves, creates folders in, appends to and deletes files.
  The model and the history see only tagged `drive:<tag>:<item_id>` refs (§1.2) and `[Drive: …]` labels, never a
  provider name.
- **The drive specialist (§6.7).** Smart reaches the drive through one intent, `work_with_drive_files`; the
  `DriveAgent` does the work through local tools over `DriveOperations`.
- **The index (§3–§5).** An opt-in, per-connection index that finds a file by name, kind, date, folder, photo facets,
  place and by what it says (document text, scans read by a model, transcripts of recordings).
- **Search (§6).** Zero model calls: exact keys with strict AND, tiers that say how each file matched, and a coverage
  line that says what the search could not see. All intelligence is spent once, at index time.
- **Notes, transcripts, mirror (§4.23, §4.24).** The owner's notes on files, transcripts of audio and video, and the
  `_Transcripts` folder the bot keeps on the drive.

Also reachable: the drive is readable from an outside MCP client (claude.ai) through read-only tools (the search, passage, open, list, note and download-link tools) over the same
`DriveOperations`, with no model of ours in the loop (`docs/05_building_blocks/remote_mcp_server/README.md`,
[DRIVE_MCP_CONNECTOR_RFC.md](../../10_rfcs/DRIVE_MCP_CONNECTOR_RFC.md)). What it may read is each drive's Cabinet
setting `DriveConnection.connector_access` (nothing by default). It is enforced under the connector audience
(`src/domain/drive_audience.py::connector_audience`, a ContextVar set around each tool call) at
`UserDriveService.check_access`, at the search's index admit (fail closed) and in the connection reads. Its open never
runs the paid model reader.

What it is not: the index does not write memory facts, and the bot never edits a file the user did not ask it to edit.
Voice reaches the drive through Smart; only with `VOICE_MCP_TOOLS` on (off by default, `src/composition/voice_mcp_setup.py`) does Lelik call the drive MCP read tools directly with a voice token.

**Native caps by consumer (a trap of every open).** An open (`DriveOperations.open`, `fetch_stored`) attaches an image
or PDF natively only when the consumer declared it: the message context carries the model's `native_formats`
(`src/domain/native_format.py`, `NATIVE_FORMATS_KEY`; EXTERNAL_FILES_RFC §4.3) and the file is within that format's
`max_bytes` and, for a PDF of known page count, `max_pages`. Any other file takes the converter and arrives as text.
No declaration is text, never native.

### 1.1 Deep research copies and call text files

A Cabinet switch (`UserBotConfig.drive_copy_deep_research`, off by default; `GET/PUT /api/drive/deep-research-copy`) is
the user's consent to keep each finished deep research report on the drive
([decision](../../04_solution_strategy/decisions/deep_research_drive_copy.md)). Every deep research path ends in
`deliver_deep_research`, which marks the HtmlPageGenerator task's context; once `AgentWorkerHandler` has delivered the
HTML link, `DeepResearchDriveCopyService` saves the same HTML through `UserDriveService.save_to_primary` (a thin step
over `save`: never overwrites, identical bytes resolve to the existing file) into a folder named in the UI language. One
localized line follows the link. No retry, no queue; the copy never fails the delivery. The copy is an ordinary file,
indexed like any HTML. A disconnect that leaves no primary turns the switch off (`src/web/user_cabinet_app.py`, the
disconnect route).

A web call's text can be kept as one `.md` file (`CallTextFileService`,
[VOICE_CALL_LIVE_TEXT_RFC.md](../../10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md) §4.11): in the chat, or on the drive through
`UserDriveService.save_to_primary` into a localized `Inbox/<voice calls folder>` under the primary's write folder; it never
overwrites. `UserDriveService.can_save_to_primary` is the predicate behind the page's button: a primary with a write
folder on a writable drive, the same condition the save's refusal uses.

### 1.2 Connections, handles and refs (MULTI_DRIVE_RFC)

A drive is a first-class **connection**, not a property of the user. The grant (tokens, provider account) is the
`OAuthCredential`; the **mount** it reads is a `src/domain/drive_connection.py::DriveConnection` (random
`connection_id`, `credential_id`, `kind`, adapter-built `drive_identity`, `ref_tag`, `mount_name`, the switches
`indexing_enabled` and `transcribe_enabled`, `provider_account`, `state`: `ACTIVE`, `NEEDS_RECONNECT` or terminal
`DISCONNECTED`, and the access settings below). The credential holds no drive id.

- **Stores.** `{prefix}drive_connections` and `{prefix}drive_user_settings` (the **primary** pointer, read fresh on every
  use), through `FirestoreDriveConnectionRepository`. A stored document whose ids contradict its address is treated as
  absent and logged at ERROR.
- **One owner.** `DriveConnectionService` writes connections and settings. `activate` runs after a drive consent: a live
  connection with the same `credential_id`, else the same `drive_identity`, is a **reconnect** (same id and tag, `ACTIVE`
  again); otherwise a new connection with a fresh tag and the lowest free name. **Tags are never reused**: a DISCONNECTED connection keeps its tag reserved, so an old ref can never resolve to a later drive. A new connection is **read-only** and not
  primary until a write folder is chosen (`write_folder_id=None`); the first write folder set for a user with no primary
  makes it the primary, and a read-only drive cannot be the primary. A connect whose drive identity cannot be read fails
  and removes the credential it just saved.
- **Handle and router.** Every `UserDrivePort` method takes a `DriveHandle`. `DriveProviderRouter` forwards each call to
  the adapter of `handle.kind`; a kind with no adapter raises `DriveNotConnectedError`. Adapter caches are keyed by
  `(user_id, credential_id)` or the connection, never by user alone.
- **Refs.** Every ref the model or the history sees is `drive:<tag>:<item_id>` (`src/domain/drive_ref.py`; the tag names
  the connection). An **untagged** ref (written before the connections) is never resolved: the answer is "search for the
  file again". A tag of a DISCONNECTED connection answers "no longer connected".
- **Mounts and paths.** Every chat-facing path is `<mount>/<path>` (`src/domain/drive_mounts.py::canonical_path`,
  `resolve_path`). A path without a drive name is on the primary; with no primary and exactly one live mount it resolves
  to that mount. Below the chat-facing producers paths stay connection-relative (index, mirror, stored note snapshots).
- **Settings and the access policy.** Written only by `DriveConnectionService` from the Cabinet (the specialist has no
  tool for them, REQ-ARCH-34): `searched_by_default`, the **scope** (`scope_folder_ids` or `scope_whole_drive`, never
  both; neither means the feed's default: the App Folder, or nothing on an account-wide drive), the **write folder**
  (`None` = read-only), the primary, the name (former names are held for a while), `connector_access`. Before every
  provider call `UserDriveService` reads the connection fresh and runs the pure
  `src/domain/drive_access_policy.py::decide`: reads stay inside the scope; writes need a write folder, stay inside its
  subtree and the scope, never rename, move or delete a scope folder or the write folder, and respect the item's own
  access; a move or copy across drives is refused. Ancestry (`UserDriveService.ancestry`) is walked only when a rule needs it, so the App Folder default costs no provider call. A refusal is a code-built line that prints no path and never says
  whether the item exists. `set_scope` normalises nested selections and refuses a scope that would leave the write
  folder outside it. The conversion and note services take the access check as a required dependency (fail closed).
- **A scope change is a diff** ([DRIVE_SCOPE_WALK_RFC.md](../../10_rfcs/DRIVE_SCOPE_WALK_RFC.md)): the index stores the
  folder set it was built under, and the indexer applies a Cabinet change as the difference: a folder added is walked, a
  folder removed has its stored subtree removed under a guard, the rest is untouched. Whole drive keeps the restart (§5).
- **Cabinet.** The routes are `/api/drive/connections*`, `/api/drive/primary` and `/api/drive/deep-research-copy`
  (`src/web/user_cabinet_app.py`); `POST …/scope/preview` is a read-only count of what a scope would newly bring into
  the index (`UserDriveService.scope_preview`); the folder picker adds `picker_outer_folders` for folders outside the
  drive's root. Every rule refusal is `400 {error: line}`. "Add a drive" goes to `/auth/connect/<kind>?intent=add`; the
  name typed in the card is applied through the rename route after the consent returns.
- **Disconnect** (`DriveDisconnectService.disconnect`, per connection): the connection ends first (`DISCONNECTED`; the
  primary goes to the `heir_id` the Cabinet passes, or is cleared: no heir is chosen automatically, and the heir must
  be another `ACTIVE` connection with a write folder), then its notes are archived (§4.24), then its grant is removed
  under the revoke rule (only when no other live connection uses the credential), the adapter's caches are dropped
  (`forget`), and a `drive_sync` task lets the indexer purge the index. Every step after the transition is repeatable,
  so a re-run finishes an interrupted one. The purge keeps the user's transcript store (§4.23). **Removed:** index, grant,
  live notes (archived). **Kept:** transcript store, note history, everything on the drive itself.

### 1.3 The Google Drive provider (GOOGLE_DRIVE_RFC)

A Google connection is one account's whole Drive (`kind = google_drive`, `src/adapters/google/google_drive_adapter.py::GoogleDriveAdapter`).
The drive credential is its own grant, never folded into a Gmail grant. The adapter is built only when
`GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` are configured, at both build sites (`src/composition/service_container.py`,
`indexer_main.py`); without them the kind is unregistered and every other kind runs.

- **Identity.** The drive is `google_drive:account:{permissionId}`, an item `google_drive:{fileId}` with no account part,
  so one file seen from two connected accounts is one identity and one note.
- **Feed.** ACCOUNT-wide (`FeedScope.ACCOUNT`): the `changes` feed lists every file the account sees, the connection's
  scope filters it, the cursor is the adapter's own string. Writes happen only inside the write folder.
- **Natives.** Docs, Sheets, Slides and Drawings have no bytes; `download` exports them
  (`google_drive_mapping.py::EXPORT_MIMES`: Markdown, xlsx, plain text, pdf); other natives are metadata only. Natives are
  read and searched, never edited. An export over Google's cap (`GOOGLE_EXPORT_MAX_BYTES`) maps to `too_large`. The
  parser sniffs the kind from the bytes, never from the temp file's name; inline `data:` images in Markdown or text are dropped to their alt text (`src/domain/text_data_uris.py`), so a base64 payload never reaches the index or a prompt.
- **Outer folders.** Folders shared with the account and member shared drives hang under no chain from My Drive's root;
  the picker lists them (`picker_outer_folders`) so one can be a scope folder. With only such folders chosen, listing
  the mount names the chosen folders.
- **`∕` in names.** A `/` inside a Google name prints as `∕` (U+2215) at every path join and is matched the same way, so
  a path resolves back.
- **Media.** Google media needs an `Authorization` header that must never enter the sandbox: the indexer reads the drive
  itself and gives the sandbox only byte ranges (§4.23, `HttpRangeSource`).

---

## 2. Architecture

```
Cabinet toggle / Step 1 mutation (nudge) / Scheduler fan-out (main /worker start_drive_sync)
   → one drive_sync task {user_id, connection_id} per ACTIVE connection with indexing on     (Cloud Tasks queue drive-sync-*)
   → indexer service /worker (indexer_main.py, internal ingress + OIDC) → DriveIndexingService.tick(scope, nudge)
        gate → lease → observe (nudged items, then delta pages) → transcript cheap steps → content classes 0-2 → enrichment
        → transcript extraction → classes 3-4 → upgrade pass → mirror → continue
        UserDrivePort by DriveHandle (DriveProviderRouter) · ContentExtractorPort (sandbox parser, then reader model)
        ImageTriagePort · DocumentProfilerPort · PassageHintPort · MediaAudioPort + BatchTranscriptionPort
        GeminiEmbeddingAdapter · FirestoreDriveIndexRepository
Smart → work_with_drive_files → DriveAgent (LLM) → DriveOperations → DriveSearchService (zero-LLM) → index repository
```

**Why a polling tick and not provider push.** A change notification says only that something changed; the tick would still
read the delta feed. Push would add a public webhook on the main service, subscription renewal and an unverified scope
question, and a periodic sync would stay for missed notifications. For content it gains almost nothing, because an edit
waits out the quiet period either way (§4 step 2); the bot's own changes already arrive at once through the nudge.

Deploy units, queue, scheduler jobs and the two-pass first deploy: `docs/07_deployment/README.md` and
`docs/07_deployment/SCHEDULERS.md`. The indexer's own URL is read from `DRIVE_INDEXER_URL` with `os.getenv`
(`src/composition/drive_indexing_setup.py`); with it unset the queue is not built and `/worker` answers 503/disabled.

### 2.1 Ports and adapters

| Port | Adapter(s) | Note |
|---|---|---|
| `UserDrivePort` | `DriveProviderRouter` over `OneDriveAdapter` and `GoogleDriveAdapter` | CRUD plus the change feed (delta feed, per-item reads, media source, capabilities, streamed download, thumbnails). Items carry adapter-built neutral `identity`, `ContentIdentity` and `DriveMediaFacts` |
| `DriveConnectionRepository` | `FirestoreDriveConnectionRepository` | §1.2 |
| `DriveIndexRepository` | `FirestoreDriveIndexRepository`, `InMemoryDriveIndexRepository` | records, chunks, hints, sync state, lease, kNN, key queries, enrichment queue, usage. The in-memory one runs the tests and the eval |
| `DriveMirrorRepository`, `DriveNoteRepository` | `FirestoreDriveMirrorRepository`, `FirestoreDriveNoteRepository` | §4.23, §4.24 |
| `ContentExtractorPort` | `SandboxContentExtractor` (parser, in the deployed runtimes), `DriveReaderRunner` (model) | one downloaded file to text, chained per kind by `EXTRACTOR_CHAINS` |
| `ImageTriagePort`, `DocumentProfilerPort`, `PassageHintPort` | `DriveTriageRunner`, `DriveProfilerRunner`, `DrivePassageRunner` | §4.16, §4.17 |
| `MediaAudioPort`, `BatchTranscriptionPort` | `SandboxMediaAdapter`, `GeminiBatchAdapter` | §4.23 |
| `EmbeddingService` | `GeminiEmbeddingAdapter` | card, chunk, hint and query vectors |

The planner, card builder, run rules, text gate, chunker, keyer, enrichment plan, tiers and place lookup are pure domain
code (`src/domain/drive_*.py`, `place_lookup.py`) and decide everything the tick and the search do, so they are tested
without Firestore, a provider, a model or a clock. The model agents are reached through runners in `src/composition/`
because services may not import agents. The profiler, passages and triage ports report every outcome as a
`ModelReplyStatus` (`ok`, `refused`, `failed`, `invalid`, `stopped_at_cap`, `unavailable`), never an exception; the
reader raises `DriveModelUnavailable` (§4.10).

**Graph specifics (OneDrive).** `OneDriveAdapter` unquotes paths and strips both `/drive/root:` and `/drives/<id>/root:`
prefixes before anything reaches the model. Token refresh is shared with To Do (`GraphTokenProvider`; the cache is keyed
by `(user_id, credential_id)` and short-lived, so a disconnect reaches every instance quickly). The provider's own
search does not work inside the App Folder, which is why finding files goes through our index. A drive consent starts at `/auth/connect/<kind>` and returns to the one callback `/auth/connect/callback`; the activation reads the drive's identity and calls
`DriveConnectionService.activate`.

---

## 3. The index (owned data)

All collections live in the `us-production` database with the environment prefix
(`src/config/environment.py::EnvironmentConfig`, the `drive_*_collection` properties).

- **Keys by scope.** Connection-scoped documents carry the connection id first (`{connection_id}:…`) with `user_id` a
  stored field every read checks: index records, chunks, hint documents, the sync state (one per connection), transcript
  jobs and batches, and the `drive_sync` task keys. A connection id is random and holds neither `:` nor `/`, so no two
  connections collide. **User- or drive-scoped on purpose:** the monthly usage and the initial build (one per provider
  account, so a reconnect opens no new budget), the stored transcripts (keyed by content, so two copies of a recording on
  any connection share one), the mirror roots and ledger (keyed by `drive_identity`, so the folder outlives a
  connection), and the owner's notes. Firestore indexes use `connection_id` or `drive_identity` in place of `user_id`
  for these. A missing composite index is a store outage, not an empty result (§7.2).
- **One record per drive item** (`DriveIndexRecord`, doc id `{connection_id}:{item_id}`): the last observation (name,
  parent id, kind, size, raw facets, the neutral `identity`, `content_identity`, typed `media` facts), `scope`,
  `state` / `reason` (§4.10), scheduling fields (`due_at`, `priority`, deferrals), `name_tokens`, the card (`card_hash`,
  `has_card`, `card_vector`), the content bookkeeping (generations, `extracted_by`, attempts, charged outages, cooldown
  stamps), the Search v2 fields (profile, job states and queue fields, `about_keys`, photo fields, `valid_until`,
  `effective_date`), capture and `card_accuracy`, the notes copy (`owner_note_*`), the transcript and mirror fields, and
  the inaccessible marks (§4.18). **No path is stored**: paths are derived from the `parent_id` chain at read time, so a
  rename touches no descendant. Two flags sit beside the structural state and never change its scope or reason:
  `card_capped` (no card headroom) and `card_refused` (the embedding provider refused this card text `MAX_ATTEMPTS`
  times).
- **Chunks** (`DriveChunk`, `{prefix}drive_index_chunks`, `{connection_id}:{item_id}:{generation}:{index}`): text, offsets,
  `word_keys` (§3.1), a vector. Search returns a chunk only when its generation equals the record's `indexed_generation`
  (the **fence**), so a crash mid-write, a re-index and a demotion are invisible at once.
- **Hint documents** (`DriveHintDoc`, `{prefix}drive_index_hints`, one per hinted chunk, same doc id): grounded keywords
  and questions with their evidence quotes, `hint_keys`, a `hint_vector`. Fenced like chunks and deleted before them
  wherever a generation is collected.
- **Sync state** (`DriveSyncState`, doc id `connection_id`): the delta cursor and epoch, the lease, `applied_versions` and
  `applied_space`, `applied_scope` and the applied folder set, the model backoffs (one per agent type), the `counters`
  map the tick refreshes (search and the Cabinet read it, so a search runs no count query), alert flags (written only by
  `update_sync_flags`, the fan-out's), and the notes step's watermark. The fan-out creates a minimal one for an `ACTIVE`
  connection with indexing on and none yet, so staleness covers ticks that never land.
- **Model usage** (`{prefix}drive_model_usage`, `src/domain/drive_usage.py`): dollars per class per month and the
  one-time initial build per provider account. Outside the sync state so a purge keeps it. 
- **Transcripts and mirror:** `drive_transcript_jobs` (+ windows), `drive_transcript_batches`, `drive_transcripts` (+
  parts), `drive_mirror_roots`, `drive_transcript_mirror` (§4.23). **Notes:** `drive_notes` (§4.24).
- **Lease fence.** `save_sync_state` never writes the lease fields, `release_lease` clears them only for the owner, a tick
  writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and `save_sync_state` / `delete_sync_state` apply
  only while it still holds the lease (`LeaseLost`: a quiet stop).
- **Scope on the index.** The sync state carries `applied_scope` (`drive_access_policy.scope_key`), the **detector**: a
  tick that sees a different key from the connection's runs `_apply_scope_change`, which diffs the stored set against the
  new one (`src/domain/drive_scope_diff.py::plan_scope_diff`) and writes marks. The set itself is stored beside the key
  (`applied_whole`, `applied_roots`, the removal and walk progress fields and resume cursors) and is valid exactly when
  `drive_scope_diff.py::shape_valid` holds; a state that fails the check is adopted when its key equals the connection's
  and restarted otherwise. On an account-wide feed a change is kept only when its parent chain reaches a scope folder;
  the folders on the way to a scope folder are stored as **skeleton ancestor records** (`Reason.SCOPE_ANCESTOR`, no card,
  never searchable) so printed paths resolve. Search applies the same scope as a second line (§6).
- **Pending change keys.** A record's `content_version` is always final. A change whose key costs an extra provider read
  (a Google native's head revision) arrives pending; `DriveIndexingService._apply` resolves it before planning, only for
  an item it plans as a live record (`UserDrivePort.resolve_content_versions`, one batched call per page). The planner
  refuses a pending change.
- **Policy** (`src/domain/drive_index_policy.py::admission`, `POLICY_VERSION`). Admitted for content: the document kinds
  (PDF, DOCX, PPTX, XLSX/XLS, ODF, EPUB and Apple Books packages, HTML, Markdown, text, CSV, JSON, RTF, MSG, EML,
  subtitles, config files). Every image is admitted for triage. Audio and video are admitted for **transcription** only
  (`Admission.TRANSCRIBE`, §4.23). Everything else (`doc`, `ppt`, archives, source code, …) is `metadata_only /
  kind_not_admitted` with a card. A content kind over `size_cap(kind)` is `metadata_only / too_large` before any download
  (indexer only; chat opens keep their own cap, [decision](../../04_solution_strategy/decisions/drive_indexer_pdf_download_cap.md)).
  Dot-names, secret-shaped names and remote, package or malware items keep a card with their reason and are never read;
  OS and lock artefacts keep a record but no card. A dot-folder, a folder with a `.git` or `pyvenv.cfg` child, a
  `node_modules`, `__pycache__` or `site-packages` folder, or a dense tree (judged from a probe, kept until the child
  count doubles or halves) becomes one `opaque_root` card; the files inside get no record, so a deleted `.git` re-opens
  the folder. Why each rule exists:
  [drive_problem_content_classes.md](../../04_solution_strategy/decisions/drive_problem_content_classes.md),
  [drive_repo_uploaded_across_rounds.md](../../04_solution_strategy/decisions/drive_repo_uploaded_across_rounds.md).
  `STRUCTURE_POLICY_SINCE` is what a policy bump compares for structure decisions, so a bump that changes only content
  admission re-reads no README and re-probes no folder.
- **The card** (`src/domain/drive_card.py::card_text`, `CARD_VERSION`), each segment only when present: a profiled file:
  name, kind, doc type, what it is, title, entities as the file prints them, topics, key facets; a text file without a
  profile yet: name, kind, the reader's brief; machine text: its lead text (the only content signal, no chunks); a
  photograph: name, description, scene, objects, visible text, key facets; a collapsed folder: its README's profile or
  just its kind. The capture, the scene names and the owner's note join it (§4.19, §4.21, §4.24). The score is **never**
  in the card text. No lead text in a document's card, so card kNN and chunk kNN never vote twice on one signal. The key facets include the photo's taken date, camera, `place_name` (resolved offline from
  the geo table in `src/assets/geo/`, `PlaceIndex.place()`, which also returns the place's names in several languages as
  `place_forms`, so "Берлін", "Berlin" and "Germany" all key a photo taken there) and sizes. A card is re-embedded only
  when its hash changes.
- **Name keys** (`name_keys_v2`): native word forms of each name token, the transliteration skeleton, digit forms and
  trigrams. Trigrams find similar names only; they never count as covering a word.
- **Versions.** `src/domain/drive_index.py::CURRENT_VERSIONS` (policy, card, geo table, name keys, extraction, triage,
  accuracy, keys, profile, passages) plus each extractor's own version, and the embedding space, are stamped on each
  record; when one changes, the upgrade pass redoes exactly the stale part (§4.12). **The migration gate:** a sync state
  whose `applied_versions` lacks `MIGRATION_GATE_KEY` was built by an older generation and is never upgraded in place:
  its tick does no work (`awaiting_migration`), search answers "being rebuilt", the fan-out raises one ops alert, and it
  is not also reported stale, until the owner's rebuild purges it. A purge and a fresh onboarding stamp every key and are
  never caught by it. A new gate key is therefore a rebuild of every user.

### 3.1 Keys: the one normaliser

Search finds words by exact keys, not by ranking. A key is a short string with a family prefix; the index and the query
build every key with the same pure functions (`src/domain/drive_keys.py`, `drive_key_forms.py`), and two digests pin the
form tables and the rules' output on a fixed fixture, so any change fails a test unless `KEYS_VERSION` is bumped.

- **Tokens.** Split on punctuation, symbols and letter/digit boundaries, not on case; NFC and casefold, Cyrillic stress
  marks dropped, `ё` → `е`; hyphen compounds and short letter-digit runs keep their joined form beside the parts.
- **Forms (no truncation).** Each token gets a set of native forms; the index stores the set, a query word ORs it. Cyrillic
  forms drop listed endings and handle the fleeting vowel and Ukrainian alternations; Latin forms strip accents and a
  plural. A 2-letter word is keyed exactly; short function words and 1-letter words never. So different case forms of one
  word share a key.
- **Transliteration.** The index stores one *skeleton* per word (a sound-class Latin alphabet), a Latin word first spelled
  in Cyrillic; the query generates every variant. A skeleton match is always a **translation**, never exact. The family
  records the script the word came from (`x:` Cyrillic, `xl:` Latin), and **a Latin query word never reads `xl:`** (two
  Latin words meeting through a Cyrillic stem, such as two similar names, are not a translation).

| Family | What | Source class |
|---|---|---|
| `w:` | native forms of a word (chunk `word_keys`, name keys) | passage words (chunk), verified (name) |
| `x:` / `xl:` | skeleton of a Cyrillic / Latin word | translation |
| `n:` | number terms: amounts, numeric and written-out dates in several languages, ids, digit groups | passage words / verified |
| `m:` / `y:` | month-year / year of every date | passage words / verified |
| `e:` | an entity spelling the file prints (verified), a photo's `place_forms`; phrases join stems | verified |
| `ef:` | an entity spelling the profiler wrote that the file does not print | translation |
| `ex:` / `exl:` | skeleton phrases of entities | translation |
| `v:` | a verified key value (order number, IBAN, amount); a verified date's ISO form | verified |
| `k:` | the kind's names in several languages | verified |
| `lw:` / `tw:` / `dw:` / `lv:` | the profile's label, topic, life-area and validity words; the fixed names of `doc_type` and life areas join `lw:` / `dw:` | automatic |
| `pw:` / `pe:` / `pn:` | a photo's description, scene and object words, visible-text numbers | automatic |
| `sl:`, `cp:` / `cpa:` | scene labels (automatic); capture names (`cp:` detected, `cpa:` said by a model) | automatic |
| `va:` / `ya:` / `ma:` | a value the file does not hold (§4.20) | automatic |
| `l:` | stored for grouping, never queried | — |
| hint `w:` | forms of grounded passage keywords the chunk does not already hold | translation |

The labels are closed enums in `src/domain/drive_labels.py` (`DOC_TYPES`, `LIFE_DOMAINS`, `SCENE_LABELS`, the capture
and kind names): the model may not add a value; one outside the set becomes `other` or is dropped. There is **no
query-time synonym table or model**: every translation or synonym a search can use was written per file at index time.
Per-document caps (`WORD_KEYS_PER_CHUNK`, `HINT_KEYS_PER_CHUNK`, `ABOUT_KEYS_PER_RECORD`) keep the earliest text's keys;
over a cap `keys_capped` is set and the card says "keys cut".

---

## 4. Content: from a file on the drive to a searchable passage

Design: RFC §24. Decisions:
[model extractor and its agents](../../04_solution_strategy/decisions/drive_model_extractor_agents.md),
[model outages](../../04_solution_strategy/decisions/drive_model_outage_defers_model_work.md),
[the document date](../../04_solution_strategy/decisions/drive_document_date_effective_date.md),
[term selectivity](../../04_solution_strategy/decisions/drive_term_selectivity_distinct_files.md),
[the parser sandbox](../../04_solution_strategy/decisions/drive_parser_sandbox_resource_isolation.md),
[the text gate](../../04_solution_strategy/decisions/drive_text_gate_financial_documents.md).

```
 file lands on the drive (upload, or the bot's own save = nudge)  ──► delta page / get_change
 1 OBSERVE   plan_observation → classify: admission, size cap, item rules ─► metadata_only + card, or pending
 2 SCHEDULE  quiet period (nudge, first sight, upgrade: due at once) → due_at, class 0-4; card headroom first
 3 RUN START budget checks → generation_started (persisted before the download)
 4 FETCH     download_to_file (size cap) / image rendition → sniff_kind
 5 EXTRACT   chain_for(kind): parser (sandbox) ──model_route?──► reader (windows)      images: TRIAGE first
 6 BRIEF     reader's first window → brief → card, effective_date (until a profile supersedes it)
 7 TEXT      text gate → chunker → chunk cap → embeddings → word_keys
 8 COMMIT    chunks under the new generation → read fence → indexed() → collect other generations
 9 CARD      name | kind | profile or brief | description | facets → card_vector
10 ENRICH    profile job, passage-hint job (§4.17), in the tick's enrichment share
```

The tick runs on the indexer only, one connection at a time (`DriveIndexingService.tick`).

### Step 1: observe and classify

`plan_observation` → `classify` (`src/domain/drive_index_planner.py`) decides what the record is: not admitted, an item
rule or inside an opaque subtree → no content ever; a content kind over its size cap → `metadata_only / too_large`; an
image → a stored triage verdict for this content version decides (`valid_verdict`), none means `pending` with triage due;
an admitted content kind → `pending`. **A content outcome that still stands is kept** (`_outcome_stands`): the same content
version read under the current `EXTRACTION_VERSION` keeps its `indexed` / `failed` / demotion state, so a rename or a move
never re-reads the file. A file whose parent is not known yet is `unresolved`: a **scope hold, not a content outcome**, so
the file keeps the outcome that stands and is not read again when the parent resolves (without the hold, a dense folder
re-probed by a policy bump would re-read, and re-bill, every child). A new content version restarts attempts and charged
outages like any edit; the old generation keeps serving, marked `stale` in results, until the new one commits. A kind
change is another extractor chain (`_kind_changed`). A photograph's description fields live with its `photo` verdict and
are cleared with it; a verdict turning from `text` to `photo` clears the profile and passage hints instead.

### Step 2: when it runs

`schedule` / `content_due_at`: an edit sets `dirty_since`; content waits until the file has been untouched for `QUIET` or
dirty for `MAX_DEFER` (a web editor saves every minute or two while someone types). First sight, an upgrade, a release and
a nudge are due at once. The due loop (`_due_loop` → `_work_class`) works classes in order: 0 edits and nudges, 1 cascades
and structure, 2 first-seen, 3 maintenance (upgrades, releases, month-held work, files charged with an outage), 4 retries;
within a record content and triage run before the card, so a file is carded once.

**Card headroom comes first** (`_card_slot`). Search never shows content without a card, so before a cardless record's
content or triage starts the tick checks it will hold a card: headroom under `MAX_CARD_RECORDS_PER_USER`, or it outranks
the lowest-ranked carded record, which the card step then evicts. Otherwise the record is `card_capped`: no download,
parser, model or embedding work until it is released. An evicted record loses its card and its readable chunks; an indexed
one goes back to `pending`, and when headroom returns (class 3) it is read again, so a model-routed file is billed again.

### Step 3: starting a run

`_index_step`: a run that started a whole tick deadline ago and never ended is a crashed attempt (`attempt_crashed`); an
image is first checked against the model hold (`_model_hold`: re-read cooldown, outage backoff, dollars left);
`_Budget.file_allowed` allows at most `TICK_FILE_BUDGET` runs per tick, none after `TICK_START_BUDGET`, and only if the
file's worst case (`can_start_file`) still ends before `TICK_DEADLINE_S`; `generation_started` persists a new
`index_generation` and `attempt_started_at` **before** the download, so a retry never reuses a crashed run's generation and
the sweep can collect its partial chunks.

### Step 4: fetch and sniff

`_fetch`: a document is streamed by `download_to_file`, cut at `size_cap(kind)`. An image triaged as text is fetched as the
provider's rendition, stepping down through `READER_RENDITION_STEPS` when one is over the vision cap
(`DriveThumbnailTooLarge`, distinct from "no rendition"); with none that fits, the original is used only within the
provider's image limits, else the record is `metadata_only / no_text` or `too_large`. `sniff_kind`
(`src/domain/document_kind.py`) reads the first bytes: **magic bytes win over the name**, so a `.txt` that is a ZIP is an
archive, and a sniffed kind the policy does not admit is `metadata_only / kind_not_admitted`. A gone item (404/410) is
re-observed at once instead of being read as "no content" (`_item_gone`, §4.18).

### Step 5: extract, parser first, model second

`_extract` walks `chain_for(kind)` (`src/domain/document_extraction.py::EXTRACTOR_CHAINS`): PDF `(parser, reader)`, image
`(reader)`, every other admitted kind `(parser)`.

- **Parser.** In the deployed runtimes the indexer parses through the sandbox's light service
  (`src/adapters/sandbox_client/sandbox_content_extractor.py::SandboxContentExtractor`, required `SANDBOX_PARSE_URL`: no
  URL refuses the start, there is no in-process path and no knob); the service runs the same child as the local adapter
  (`src/adapters/drive_parser/child.py`, readers in `readers.py`), a fresh process per file under resource limits.
  `SubprocessContentExtractor` is that child run in-process, used by owner scripts and the probes. A parser failure
  reaches the outcome and the logs as its type, code position and a hash, never its message (`failure_label`): a parser's
  message can quote the document. A changed output for the same file is a parser version bump (`PARSER_VERSION`, §4.12).
- **Router** (`model_route`). A PDF goes on to the reader when the parser found no text, fewer than
  `MODEL_EXTRACT_DENSITY` characters per page, too many unreadable characters (`garbage_share`), or failed (`error`,
  `unsupported`). A parser `timeout` or `memory` kill is a bomb and is never sent on; `encrypted` is deterministic and
  never sent on. Other kinds keep the parser's outcome.
- **Reader** (`DriveReaderRunner`, driving `DriveDocumentReaderAgent`). Before it is called (`_model_hold`, then the chunk
  cap): an edit of a file the reader already read waits `MODEL_REREAD_COOLDOWN` after `model_read_at` (first sight, a nudge,
  an upgrade or a release reads at once); the model must not be in an outage backoff; the dollars left (open initial build,
  then the month's reader cap) must pay for the file's pages (`reader_pages`, up to `MODEL_EXTRACT_MAX_PAGES`); the user must
  have chunk headroom. This chunk-count read also precedes any billed call, so a store that cannot answer it ends the
  tick before anything is spent. Otherwise the record waits (`deferred`). The runner builds the agent with the user's own
  configuration and bills every call to the user's account; the provider and fallback are §4.16.
  A PDF is counted by the sandboxed child and read in windows of `MODEL_PAGES_PER_CALL` pages, at most `MODEL_IN_FLIGHT`
  at a time, joined in page order. A window answered at the output limit, or timing out, is halved once; a refusal
  (HTTP 413, 400, 422) halves down to one page; a single page still refused is skipped and the rest indexed `truncated`
  with `pages_refused`. Only a PDF whose every page was refused, or a refused image, is terminal (`too_large` when a refusal
  was 413, else `unsupported`: the same bytes always get the same answer). At `MODEL_FILE_BUDGET_S` the finished windows are
  kept and the rest cancelled; a file the budget cut while no finished window found text is `error` (retried), never
  `no_text`. An outage in any window raises `DriveModelUnavailable` carrying the pages, calls and cost already spent and
  whether it may be this file's own (`charges_file`, §4.10).
- **Outcome** (`src/domain/drive_index_run.py::outcome_step`): `text` goes on; `no_text`, `unsupported`, `too_large`,
  `encrypted` demote; `timeout`, `memory`, `error` retry; `budget_exhausted` waits for next month. `extracted_by`
  (`parser@N` / `reader@N`) records the link that decided, so a new version of that link re-reads exactly the files it
  decided (§4.12).

**Images: triage.** `_triage_step` / `_triage_batch` gather due images `TRIAGE_BATCH` per call; each image's thumbnail goes
after an "Image N" label with the camera prior as text (a hint, never a verdict). `DriveTriageRunner` returns, per image,
`text`, or `photo` with an enriched description (description, objects with spellings, a scene, visible text, scene
labels). The photo's place comes from its GPS facet and the geo table, never from the model. `TRIAGE_BATCH` is small to avoid the caption shift (a description landing on the neighbouring image): missing indexes and byte-identical images given
different verdicts get one correction turn. A batch the provider refuses (400/413/422) or that stops at the output cap is
halved down to the image; a refused image alone, or a thumbnail the providers do not take, is a photograph without a
description (deterministic). An image left unanswered, still capped alone (a text scan must not become a permanent
photograph) or from an invalid reply gets no verdict and takes a per-file retry. A 5xx, timeout or fallback refusal stops the
run's further calls and defers the images it left unsettled; a triage outage is never charged to an image. Verdicts:
`photo` → `metadata_only / photograph` with its description, carded; `text` → content due for the reader. No thumbnail yet
is retried with backoff; only at `MAX_ATTEMPTS` is it a photograph without a description. A batch starts only when the
dollars left pay for it (§4.11); the spend is written to the usage document after each batch.

### Step 6: the brief and the document date

The reader's first window also returns a brief (`DocumentBrief`): a one-line description, `doc_date`, parties, an amount. It
goes into the card; its `doc_date` becomes the record's `effective_date`, so a date filter ("bills dated in March")
matches the date printed on a scan uploaded in April. Parsed documents get no brief. Brief, profile and photo description
are model output from untrusted content and reach Smart only inside the untrusted wrapper. Once a file has a profile it
supersedes the brief, and its **verified** issue date becomes `effective_date` for every profiled document, parsed ones
included (`effective_from` in `src/domain/drive_enrich_plan.py`): the profile's issue date, else the brief's date, else the
taken or file date. The card's date label cites "written in the document" only while the stored date equals one of
those, so a stale one is never cited (§6.5).

### Step 7: text gate, chunks, keys, embeddings

`_read_text`, all CPU work in worker threads:
- **Text gate** (`src/domain/drive_text_gate.py::is_machine_text`; tables are exempt): a server log, SQL dump or telemetry
  saved as text is `metadata_only / machine_text`. Machine text is repetition **and** low token variety together; a bank
  statement or invoice repeats a template but keeps variety and amounts, so it passes.
- **Chunker** (`src/domain/drive_chunker.py::chunk_text`): about `CHUNK_CHARS`, ending at a paragraph, sentence or word;
  `CHUNK_OVERLAP` of trailing sentences repeats; a Markdown table is never overlapped, continued chunks start with the header
  row. At most `MAX_CHUNKS_PER_FILE` (`MAX_TEXT_CHARS` binds first). No chunk → `no_text`.
- **Chunk cap** (`chunk_cap_refuses`): the user's readable chunks may not pass `MAX_CHUNKS_PER_USER`; a file that would is
  `metadata_only / user_chunk_cap`, released when the headroom holds it. The cap exists because chunk kNN is an exact scan of
  the user's chunks ([decision](../../04_solution_strategy/decisions/drive_chunk_caps_raised.md)).
- **Embeddings:** one vector per chunk, at most `EMBED_IN_FLIGHT` calls at once, within `EMBED_FILE_BUDGET_S` (past it:
  `embed_timeout`, retried). An edit embeds only new or changed chunks: stored vectors are reused by a hash of the chunk text
  when they are in the current embedding space (`vectors_reused` in the tick line). A 400 on a chunk is this file's
  (`extract_error`); any other embedding error is systemic and ends the tick.
- **Word keys** (`word_keys_capped`, §3.1): every chunk gets the forms and skeletons of its words, its number terms and
  months, earliest text first. The number-term normaliser (`drive_content_terms.py`) is under `KEYS_VERSION`.
- **Hints of an edit are reused:** between the fence re-read and the commit, a new chunk takes an old chunk's grounded hint
  items when its text hash matches or most of its paragraphs came from that one old chunk; the items are grounded again
  against the new text. Chunks left without hints wait for the passages job.

### Step 8: commit under the fence

The chunks are written under the run's generation, then the record is read again: the result commits only if it still has
the content version and generation the run started from; otherwise the chunks are collected and the result dropped.
`indexed()` is the run's one record write; then every other generation's chunks are deleted. The sweep at the end of an
epoch also removes chunks with no record or a dead generation.

### Step 9–10: the card, enrichment

The card is due because its text changed (a brief, a description, a profile): `card_text` → vector → `card_vector`. Every
record write also recomputes `about_keys` and the enrichment job states (`drive_enrich_plan.planned`, in `_commit`), so a
rename, move, demotion or verdict always leaves consistent keys. A first-seen file whose profile job is ready when its
content commits is profiled in the same run, so its card is embedded once. The jobs themselves: §4.17.

### 4.10 Outcomes and statuses

| Where | Outcome | Record | Retried? |
|---|---|---|---|
| any run | text, chunks committed | `indexed` | — |
| parser / reader | `no_text` | `metadata_only / no_text` | no, until the content or extractor changes |
| parser / reader | `unsupported` (no reader, deterministic parser failure, a 400/422 refusal of an image or of every page of a PDF with no 413) | `metadata_only / kind_not_admitted` | no |
| fetch / parser / reader | `too_large` (download cap, container bomb, 413 on an image, every page refused with a 413) | `metadata_only / too_large` | no |
| parser | `encrypted` | `metadata_only / encrypted` | no |
| sniff | bytes show a kind the policy does not admit | `metadata_only / kind_not_admitted` | no |
| text gate | machine text | `metadata_only / machine_text` | no |
| chunk cap | over `MAX_CHUNKS_PER_USER` | `metadata_only / user_chunk_cap` | released when headroom holds it |
| card cap | no card headroom (§4 step 2) | `card_capped` flag; an indexed record → `pending`, chunks collected | released when headroom returns |
| triage | photograph | `metadata_only / photograph` | no |
| triage | invalid output after the parse retry, an answer stopped, a refusal before any HTTP exchange | `pending` → `failed / extract_error` | yes, `MAX_ATTEMPTS` |
| re-embed (§4.12) | the new embedding space refuses a stored chunk (400) | chunks collected, `chunks_refused_space` set | no, until another space |
| parser | `timeout`, `memory` | `pending` with backoff, then `failed / extract_timeout` or `extract_memory` | yes, `MAX_ATTEMPTS` |
| parser / reader / run | `error`, crashed run, chunk refused | `pending` → `failed / extract_error` | yes |
| fetch / triage | download or thumbnail failed | `pending` → `failed / download_error` | yes |
| fetch / triage / listing | "not found" by id for an item delta lists (§4.18) | state kept, `inaccessible_since` set, no attempt counted | by timed recheck |
| embeddings | per-file budget passed | `pending` → `failed / embed_timeout` | yes |
| reader / triage | the class's dollars spent for the month | `pending`, class 3, due at the month start | waits |
| reader / triage | **outage, not charged**: 429, network, open breaker, an account-level 4xx other than 400/413/422, missing prompt or key; any triage outage | `pending`, due at `model_backoff_until`; attempts untouched | waits |
| reader | **outage charged to the file**: a 5xx, a timeout of a single-page window or an image, an unexpected error, when the reader made a call for this file | `model_outages + 1`, `pending` in class 3 behind other due work; the `MAX_ATTEMPTS`-th → `failed / extract_error` | yes |
| run | **systemic**: drive disconnected or throttled, a rejected refresh, an embedding error other than a chunk's 400, a transient Firestore error or a missing / building composite index (`DriveIndexStoreUnavailable`) | the run is abandoned (attempt not counted); the tick ends with its §5 backoff | the tick |

A `failed` record keeps its previous readable generation (served as stale). Retry backoff doubles per attempt up to a cap
(`src/domain/drive_index_params.py`). A new content version restarts attempts, charged outages and deferrals. `failed` and
content demotions stand until the content version or the deciding extractor changes.

**Why a charged outage.** Some files always get a 5xx or a single-page timeout from the provider. Treated as a plain
outage, such a file came back after every backoff, re-read (and re-billed) the windows before the failing one, and drained
the month's pages. Charging it to the file bounds it at `MAX_ATTEMPTS` reads; a rate limit or an account error is
everyone's, so it is never charged (`src/infrastructure/drive_model_call.py::classify_model_error`).

**The primary down for an hour, or a day.** All four model agents fail over to Gemini Flash (§4.16): a 429, a 5xx, a
timeout, a network failure or an open breaker on the primary re-serves that call on Gemini, billed at Gemini's price. A 4xx
on the primary (account error, key, permission, model id) is not failed over. The outage starts only when the fallback fails
too (or there is no `GEMINI_API_KEY`, or the user's primary is Gemini): it is sorted by the **primary's** cause, so the
charging rules are unchanged. When both are down the model work waits and nothing else does: parsed documents, cards,
embeddings, renames, moves and deletions go on, and search keeps working and says "N documents are not read yet". The
backoff doubles up to `MODEL_BACKOFF_CAP_S`, so a day-long outage costs about one probe call an hour per user.
**Anthropic's "credit balance too low" is an HTTP 400, and it is an outage, not a refusal of the input**: the adapter
tells it from a content refusal by the message and raises `LLMAccountError`; `classify_model_error` makes it an uncharged
outage, so during a credit lapse files stay `pending` instead of being demoted page by page. A file charged `MAX_ATTEMPTS` times
stays `failed` until it is edited, the reader's version is bumped or the index is rebuilt (no per-file retry command;
`scripts/infrastructure/drive_content_requeue.py`, §7). The outage has no alert of its own: ERROR lines and the `deferred`
count in the tick line.

### 4.11 Budgets and bounds

Values live in `src/domain/drive_index_params.py`; the table names the constant.

| Bound | Constant | What happens at it |
|---|---|---|
| Runs per tick | `TICK_FILE_BUDGET` | the rest wait for the next tick |
| Start budget / tick deadline | `TICK_START_BUDGET` / `TICK_DEADLINE_S` | no run starts whose worst case would pass the deadline |
| Reader dollars per user per month | `READER_USD_PER_MONTH` | a file waits until the dollars left pay for its pages; never cut to what is left, except a PDF needing more pages than a whole month's cap |
| Triage / profile / passage dollars | `TRIAGE_USD_PER_MONTH`, `PROFILE_USD_PER_MONTH`, `PASSAGE_USD_PER_MONTH` | class 3 waits for the next month; profile and passage jobs are `parked` |
| Transcription dollars | `TRANSCRIBE_USD_PER_MONTH` | a **hard** cap (open reservations count); an optional deploy knob can only lower it; no initial-build share |
| Initial build, once per provider account | `INITIAL_BUILD_USD` | spent first, on every class, before the monthly caps; closes when spent or when the first onboarding has settled with nothing ready or parked; never reopens (a purge, rebuild or toggle does not renew it; only `scripts/infrastructure/drive_initial_build_reset.py` does) |
| Enrichment | `ENRICH_FILE_BUDGET_S`, `ENRICH_IN_FLIGHT`, `ENRICH_SHARE` | a cut file resumes from its saved passage progress; the shares bound when work **starts** |
| Pages read per file | `MODEL_EXTRACT_MAX_PAGES` | `truncated` |
| Re-read of an edited reader-read file | `MODEL_REREAD_COOLDOWN` | the edit waits; the newest version is read once |
| Reader outages charged to one content version | `MAX_ATTEMPTS` | `failed / extract_error` |
| Readable chunks / carded records per user | `MAX_CHUNKS_PER_USER`, `MAX_CARD_RECORDS_PER_USER` | `user_chunk_cap`; `card_capped`, decided before any content or triage work |
| Maintenance share of the start budget | `MAINTENANCE_SHARE` | upgrades and month-held work yield to edits and first-seen work |

**Budgets are dollars, priced at the model that ran** (failover included). Every paid reply is written at once to the
month's usage document (`_spend_usd`, shielded so a cut or cancelled sibling never loses a paid reply); the open initial
build pays first and the part it paid is kept apart so it never counts against the month's cap. A file or batch **starts
only if its expected cost at the primary's price fits** the dollars left. The outage state (`model_backoff_until`,
`model_backoff_by_agent`) is written after each reader file and triage batch (`_persist_usage`, a fenced write). The Cabinet
shows each class's dollars this month and the initial build's progress.

**Large files.** A 600-page PDF: over the indexer's PDF cap it is `too_large` from the listing and never downloaded (its
name card stays searchable). With a good text layer the parser reads until `MAX_TEXT_CHARS` and the chunker keeps the first
`MAX_CHUNKS_PER_FILE` chunks, so roughly the first part of a long book is searchable and the rest is not; the router judges
density as an average over all pages, so a mostly-text PDF with some scanned pages stays with the parser and its scanned
pages are not read. A scan: the reader gets the first `MODEL_EXTRACT_MAX_PAGES` pages and needs the dollars for all of them
before it starts, else it waits whole. Its profile reads only the head and tail, and its passage hints cover the first
`PASSAGE_MAX_CHUNKS_PER_FILE` chunks. Either cut sets `truncated`; Smart sees "only the beginning of the file was indexed"
on the card.

### 4.12 Upgrades: what a version change redoes

The upgrade pass (`_upgrade_step` / `_upgrade_page`) runs after the due loop, pages through records in class 3 within the
maintenance share, and re-plans each stale record (`stale_versions`, `extractor_stale`):

| Version | Redone |
|---|---|
| `POLICY_VERSION` | re-classified |
| `CARD_VERSION`, `NAME_KEYS_VERSION`, `GEO_TABLE_VERSION` | re-carded, re-keyed, re-placed; no content read |
| `EXTRACTION_VERSION` (chunker, text gate) | every file whose outcome was decided under the old version is read again (it stays readable meanwhile); model-routed files are re-billed |
| `KEYS_VERSION` | each record re-keyed from its **stored** text (`keys_pending`, class 3, never budgeted or held): readable chunks and hint documents are copied into a new generation with new keys, committed under the fence; no download, parser or model. While it runs the coverage line says the index is being re-keyed |
| `PROFILE_VERSION` / `PASSAGES_VERSION` | the jobs become `ready` and re-run from stored text: model calls, billed against the dollar caps |
| an extractor's version | only the files that link decided (`extracted_by`, `reextraction_due`) |
| `ACCURACY_VERSION` | each scorable record re-graded from its stored profile and chunk text: reads only |
| `TRIAGE_VERSION` | images triaged again |
| embedding space | cards re-embedded (search runs on keys and filters meanwhile, `refreshing`); readable chunks and hints re-embedded from stored text into a new generation (`_reembed_step`, under the fence), whatever the record's state, without touching attempts or the quiet period |

**The parser re-read compares text** ([decision](../../04_solution_strategy/decisions/drive_reread_by_text_change.md)):
a record stamped with an older `parser@N` is read again; `reader@N` records and transcripts are untouched. The new text is
chunked and compared with the stored chunks (`src/domain/drive_reread.py::same_read`). **Equal:** only the stamp changes
(`reread_unchanged`): no generation, no chunk write, no embedding, no profile or hint work. **Different:** the normal index
path, embedding only chunks no stored chunk holds. An upgrade re-read (stale stamp, content not edited since;
`drive_upgrade_read.upgrade_reread`) **never calls the paid reader**: when the parser's new text would route to the reader,
the parser text stands. The pass can be held off at the deploy that bumps a parser version (`DRIVE_UPGRADE_PASS_DEV=off`,
§7.2) until the owner has run the probe (`scripts/infrastructure/drive_parser_upgrade_probe.py`: read-only, prints counts
and dollars only, writes under `scripts/memory/`).

**Traps.** (1) The pass marks `keys`, `profile` and `passages` applied only when every record is settled for them, so held
and parked jobs are re-evaluated on every bump. (2) A record whose model work is held for next month's budget and which is
stale only for what that work applies to is parked (`month_parked`); a pass that finds only such records stores
`upgrade_parked_month` and does not run again until the month turns or the targets change, because without it such records
restarted a full read of the index every tick. (3) A chunk the new space's model refuses (a definite 400) is terminal **for
that space** (`chunks_refused`): the file's chunks are collected and it is not read again, so the space change completes. (4)
A pass that leaves stale records does not repeat until due work has run; `applied_versions` and `applied_space` are written
only after a clean pass.

**Switching the embedding model** is a code change in `src/adapters/gemini_embedding_adapter.py::GeminiEmbeddingAdapter`: its
model, output dimension or task prefixes make `EMBEDDING_SPACE` (`<model>/<dim>`) and any change of them must change that
string, because that starts the pass. The same adapter embeds memory facts and emails, whose vectors this pass does not touch;
a new **dimension** also needs new vector indexes (every `vectorConfig` in `config/firestore.indexes.json` and
`scripts/infrastructure/create_drive_vector_indexes.sh`), created and `READY` before the deploy.

### 4.13 What it costs

Search v2 budgets every model class in dollars (§4.11); a search itself makes no model call except one query embedding.

| Path | Bound |
|---|---|
| Profile (one call per text file; re-profiled at most once per `PROFILE_REFRESH_COOLDOWN` after an edit; templates cost nothing) | `PROFILE_USD_PER_MONTH` after the initial build |
| Passage hints (two chunks per call; an edit reuses unchanged chunks' hints) | `PASSAGE_USD_PER_MONTH` after the initial build |
| Transcription (Gemini Flash on the Batch API, audio windows; opt-in per user) | `TRANSCRIBE_USD_PER_MONTH`, hard; a file's estimate is reserved before its first window |
| Initial build | `INITIAL_BUILD_USD` once per provider account; a reconnect of the same account finds the same record |
| Reader, triage | the monthly caps, priced per model that ran; failover calls price at Gemini's rate |
| Embeddings (`gemini-embedding-2`) | not billed to the user, **no monthly cap**; bounded by size: cards and chunks per user, chunks per file; a card re-embeds only when its text changes |
| Firestore | linear in the user's chunks (kNN reads per index entries scanned) |
| Indexer CPU | one instance, `TICK_FILE_BUDGET` and the tick deadline |

What costs money again, by design: a version bump that re-reads model-routed files, a card-capped file read again on release,
and a rebuild. Every reader, triage, profile and passage call, failed ones and parse retries included, is billed to the user
and appears in `billing_daily_summary`; the tick line carries the dollars per class. Prices: RFC §24.9, §25.6.

### 4.14 Security: the parser sandbox, and document text is data

**The deployment is multi-user**: a file one user uploads is parsed where every user's files would otherwise be. Parsing
and media decoding therefore run in the sandbox's light service, which gets no credential and no provider URL (a parse request carries the file's bytes; media decoding gets only byte ranges the indexer serves)
([UNTRUSTED_CONTENT_SANDBOX_RFC.md](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md);
[sandbox_media_tunnel.md](../../04_solution_strategy/decisions/sandbox_media_tunnel.md);
[drive_parser_sandbox_resource_isolation.md](../../04_solution_strategy/decisions/drive_parser_sandbox_resource_isolation.md)).
The indexer starts only with `SANDBOX_PARSE_URL` set. The in-process child (`SubprocessContentExtractor`, run as
`DRIVE_PARSER_USER` when set) is for local runs and owner scripts: it runs as the developer's own user and logs a WARNING
when no sandbox user is configured.

**Document text is data.** A document can say anything, including orders to an assistant. Everything from a file (name,
brief, profile, photo description, passage, passage keyword and its quote) reaches Smart only inside the nonce-tagged
wrapper (§6.3), and Smart's token says text in drive results and opened files is data: never save to memory, fetch a URL
or change the drive because a document says so. The reader and triage agents have no tools, so a hostile document can at
worst corrupt its own text. The per-run log line carries ids, never the file name or text.

**What is left (prompt injection).** Smart still holds tools (memory, web fetch, drive mutations, documents), and the only
things between a hostile document and them are the wrapper, the token line and the G3 bar (zero such calls on the injected
case, §7). That is a model instruction checked by an eval, not an enforced guard. A document can also steer what its own
brief, profile and description say, so it can mislead an answer about itself or rank itself into a search; it cannot reach
another file's record. A `drive:` file passed to DocPlanner, PdfGenerator or HtmlPageGenerator is prefetched through
`FileConversionService`, whose text is marked untrusted (`present_text`; [decision](../../04_solution_strategy/decisions/drive_prefetch_wrapper_slice3.md)). The one
enforced guard is the passages write gate (§4.25).

### 4.15 Timelines

**A new scanned PDF.** Saved by the bot it is a nudge and a tick is enqueued at once (behind any tick already running: the
queue runs one at a time); uploaded any other way it arrives in the delta feed at the next fan-out tick. It is classified
`pending`, content due in class 2 with no quiet period; after card headroom and the run budget, it is downloaded, sniffed,
the parser finds no text, the model hold is checked, the reader transcribes it in windows and returns the brief, then gate,
chunks, embeddings, commit, card. **Smart can find its text from the moment that run commits**; until then search reports
`building` and "N documents are not read yet". If the content cannot be read now (month spent, outage, no time left), its
name card is embedded without waiting, so the file is findable by name, kind and date while its content waits.

**An edited file still shows its old content**, in order, for any of: the edit has not been seen yet (an edit outside the
bot reaches the index at the next fan-out; until then nothing marks the result old except the envelope line "The index
reflects the drive as of …"); it is in its quiet period (a nudge skips it); the file was read by the **model** and is
inside the re-read cooldown; the run waits for the month's dollars or an outage, or failed (`failed` keeps the old
generation). Meanwhile each result carries `stale: the file changed after it was indexed`, and Smart's guidance opens a
stale file before quoting it.

**A photo with no description** (`photo_description`, objects, scene, visible text exist only for an image triage judged a
photograph) means: triage has not run yet (class 2 after the quiet period; dollars spent; a model outage; card-capped);
triage judged it **text** (a receipt, a screenshot: it gets a brief and a profile instead); triage settled it as a
photograph **without** a description (the provider refused it, the thumbnail is over the vision cap or not a supported type,
no thumbnail after `MAX_ATTEMPTS`, or the model returned an empty description), which stands until the image changes or
`TRIAGE_VERSION` is bumped; or it is inside an opaque folder and has no record at all. The record's `triage_verdict` and
photo fields say which (§7.1).

### 4.16 The model agents: prompts and provider

**Prompts** live in Firestore (tokens, blueprints, profiles), the single source of truth, assembled by `build_drive_prompt`
with no biography, directives or user overrides. Per agent (`reader`, `triage`, `profiler`, `passages`; the transcriber
alike): tokens `COGNITIVE_PROCESS_DRIVE_<AGENT>` and `OUTPUT_FORMAT_DRIVE_<AGENT>`, a blueprint
`drive_<agent>_agent_v1.yaml` and a profile `drive_<agent>.yaml` (mirrored in `prompts_snapshot/`; the owner uploads them,
`docs/how_to/NEW_AGENT_PLAYBOOK.md`). **A trap:** triage v2 with the v1 tokens fails silently (the reply parses and images
are stamped with thin descriptions). A missing or incomplete prompt is a model outage (ERROR, never charged to a file). A
prompt change does not re-read anything already read: bump the agent's version (`READER_VERSION` in
`src/composition/drive_reader_runner.py`, `TRIAGE_VERSION`, `PROFILE_VERSION`, `PASSAGES_VERSION`; §4.12).

**Provider.** The strategies are `AgentProviderStrategy.STRATEGIES` entries `drive_reader`, `drive_triage`,
`drive_profiler`, `drive_passages` in `src/services/agent_context_builder.py`: default OpenAI (ECO), allowed `claude` and
`openai` (a `gemini` setting is ignored: as a primary it would resolve at ECO to a model never checked on these inputs),
required capability vision for reader and triage. Per user the provider is resolved in order by
`AgentContextBuilder.resolve_provider_name`: `config.agent_providers["drive_reader"]` (etc.), then the global
`provider_preference` when allowed, then the default. OpenAI is the default because in the owner's live check Claude Haiku
shifted the captions of a triage batch while the OpenAI model did not. To read with Claude, set `agent_providers` on the
user document (`{"drive_reader": "claude"}`, `drive_triage` too); there is no Cabinet control. It applies from the next
read, with no deploy, as long as the indexer holds that provider's key (a primary without a key is a model outage with no
failover, because the context is never built). To have already-read files read by the new provider, bump `READER_VERSION`
(re-reads and re-bills every reader-decided file of every user). Compare providers first with
`scripts/validation/drive_model_check.py read <folder> --providers claude,openai`, and re-record the eval bars with
`DRIVE_EVAL_PROVIDER` (the bars pin the reader's provider).

**Fallback.** All four fail over to Gemini Flash: `fallback: "gemini"` with `fallback_tier: BALANCED` in their strategy
entries, so the fallback resolves at BALANCED, not at the agents' ECO tier (`AgentContextBuilder._build`). **Those entries
must keep `fallback_tier: BALANCED`; without it the fallback silently resolves to the cheapest tier.** The failover is per
call (one window of a PDF may be read by Gemini while the rest are read by the primary) and `extracted_by` does not record
which. It does not re-read files already read. A non-account 4xx from Gemini on a failover call is
`LLMFallbackClientError`, a charged outage retried on the primary; so is a safety block Gemini returns as a response. The
indexer registers Gemini when `GEMINI_API_KEY` is set (it is, for embeddings).

**Runtime shape.** The agents (and the transcriber) run on the indexer service only (`indexer_main.py` →
`src/composition/drive_model_setup.py`). They have no manifest descriptor, no tools, no history and no user prompt
overrides, and are not routed to. Each is built per call with the user's own configuration by a runner in
`src/composition/` (`DriveReaderRunner` implements `ContentExtractorPort`, `DriveTriageRunner` `ImageTriagePort`,
`DriveProfilerRunner` `DocumentProfilerPort`, `DrivePassageRunner` `PassageHintPort`) because services cannot import
agents. `allow_provider_storage=False`. Limits live in `src/infrastructure/agent_config.py` (`DRIVE_READER`,
`DRIVE_TRIAGE`, `DRIVE_PROFILER`, `DRIVE_PASSAGES`; one parse retry each). Calls are billed to the user through the
runner's quota service.

### 4.17 Enrichment: the profile and the passage hints (Search v2)

Principle: the intelligence is spent **once, at index time**; a search makes no model call. A model's output is either
**verified** against the file's text (then it may make a match exact) or labelled **automatic** (it can only put a file in
the By-translation or Labelled tier, §6.2).

**The profile** (`DriveProfilerAgent`, runner `DriveProfilerRunner`). One call per text file, from its **stored** text only
(`src/domain/drive_profile.py::profile_input`: chunks reassembled, the head and tail cut at a boundary, heading lines of a
long file). What goes in per kind is one pure table, `PROFILE_POLICY`: documents, scans, HTML, Markdown, text, slides,
e-mail and subtitles get a full profile; sheets, JSON, EPUB and a collapsed folder's README excerpt get one without key
values. **Templates** cost no call (a short note, a script-rendered web page, machine text). The model never sees the file's
path, folder or siblings; the name travels with the request but outside the input fingerprint, so a rename or move never
re-profiles. The profile holds title, `what_it_is`, language, `doc_type` and `life_domains` (fixed sets), dates, entities,
key values, topics and four word lists in en, es, uk and ru.

**Grading, not dropping** (`grade` in `drive_profile.py`, `src/domain/drive_grade.py`): every date, key value, entity and
number stays in the profile, the card and the keys with an evidence grade: `in_text`, `in_text_as` (found, written
differently; `seen_as` keeps the text's form) or `not_in_text`. A value the text does not hold stays searchable in an
automatic class (`va:`, `ya:`, `ma:`, entities as `ef:` only) and **never** sets `effective_date` or `valid_until` and
never answers `find_expiring`. The verified `valid_until` (else `due`) goes top level; the verified issue date becomes
`effective_date` (§4 step 6). Descriptive fields and word lists cannot be verified and are automatic. On an edit the stored
profile is re-graded against the new text at once, before any call.

**The passage hints** (doc2query; `DrivePassageAgent`, runner `DrivePassageRunner`). `PASSAGE_CHUNKS_PER_CALL` chunks per call under opaque
indexes with the profile's title; per chunk keywords (subjects, names, synonyms, translations) and questions, each with an
`evidence` quote copied from the chunk. Only the first `PASSAGE_MAX_CHUNKS_PER_FILE` chunks of a file are hinted; EPUB,
sheets and JSON get none. **Grounding** (`src/domain/drive_hints.py::ground`): the quote must occur in the chunk on word
boundaries (its span is stored); every number, date or id in an item must be among the chunk's number terms; a keyword the
chunk already holds in all its forms adds no hint keys; a set whose embedding is further from the chunk than
`PASSAGE_MIN_SIMILARITY` is dropped whole. Grounding proves the quote is in the passage, not that the keyword translates
it, so a hint match is a **translation**, never exact, and the card shows the quote.

**States and the queue.** Each record has a profile job and a passage job, each in one state (`none`, `current`, `ready`,
`deferred`, `parked`, `held`), decided by the pure `profile_plan` / `passages_plan` on every record write
(`src/domain/drive_enrich_plan.py`). `deferred` is a refresh cooldown or a pending content run (no call is made on text
about to change); `parked` is "one call no longer fits the dollars left" (wakes when the month turns); `held` is a hold for
this exact version and input (a provider refusal, or `MAX_ATTEMPTS` charged failures); a version bump or new input makes
the job `ready` again. The queue reads `enrich_ready` records newest `effective_day` first, so a rebuild describes recent
files first. A model backoff is **per agent type** (`model_backoff_by_agent`), not a job state: a backed-off profiler never
hides passage work.

**Where it runs.** In `_enrich_phase`, after content classes 0–2 and before classes 3–4 (§5). Failures: a 429, network,
open breaker, account 4xx, missing key or prompt is an uncharged outage (the agent type backs off, the job stays `ready`); a
5xx, timeout or unexpected error is a charged attempt and a backoff, at `MAX_ATTEMPTS` the job is `held / failed`; invalid
output after the parse retry is charged with no backoff; a provider refusal holds a profile as `refused`, halves a passage
batch to one chunk. A run that could write nothing for a ready job holds it rather than spinning. **Content work, keys and
search never wait for enrichment**: an undescribed file is still found by name, text and meaning, and the coverage line
counts it (§6.3). The tick logs `rebuild progress` while described counts move; the fan-out alerts when enrichment has
not moved for a day while jobs are ready and budget is left, and when the fallback served most of a user's model dollars.

### 4.18 Files the bot cannot open

**Why it happens.** The bot's access is one permission entry for the app on the App Folder, inherited by everything in it.
An item with its own permission list that lacks that entry (a folder shared with people before it was moved in, a file shared
on its own) is still listed by the change feed, but every call that addresses it by id answers 404: the provider reports "no
access" as "not found". A copy of such a file takes its new folder's permissions and can be read.

**What the index does: existence is decided by delta alone.** `OneDriveAdapter.get_change` raises `DriveItemNotFoundError`
and never turns a 404 into a deletion. A download, rendition or thumbnail answering "not found" re-reads the item by id once
(`_item_gone`); a 404 again **marks** it (`marked_inaccessible`: `inaccessible_since` and companion fields). The run ends
without an attempt (never `failed`); the record keeps its state, card, name keys and any readable generation, shown `stale`.
A folder's re-listing, README listing or density probe that answers "not found" marks the folder. Marking a file checks its
parent with one item call; a 404 marks the parent (the root is never marked), and a marked folder marks its stored
sub-folders by inference. A parent lookup of an unresolved record still removes it as an orphan on a 404; a nudge read's 404
writes nothing; only a tombstone in the change feed removes a marked record. While marked: no content run, triage, profile
or passage job; card, keys from stored text and re-embeds go on.

**Healing.** A marked record is due for one id read in class 3 (`_recheck_step`): after `INACCESSIBLE_RETRY_MIN` once delta or
a listing saw the item or its parent again, and otherwise every `INACCESSIBLE_RETRY_SLOW` (a permission change alone may not
re-emit the item). A 404 restarts the clock; a readable answer clears the marks and the file takes the ordinary content path.

**Search.** The card keeps its tier; its `index:` line says the file exists but the bot cannot open it and to copy it into a
new folder inside the bot's folder. The coverage line counts them on their own, never as "not read yet", and only when the
search found nothing. `UserDriveService.list_folder_by_id` takes a ref; when list or open meets "not found" the agent asks
the index (`DriveSearchService.access_note`) and a record known to be closed answers with the same sentence.

**The notice** is sent by the fan-out (`DriveSyncSchedulerService._inaccessible_notice`), never by the indexer, read only
while the tick's `inaccessible_unnotified` counter is non-zero. A file is in it once a delta round that **started after** its
marking has completed, so a really deleted file is gone first, and once per marking (`inaccessible_notified_since`, written
only by the fan-out through `set_inaccessible_notified` and absent from `RECORD_FIELDS`, so a stale tick snapshot can never
clear it and repeat the notice). All files of a pass go in one notice to the default chat: first the paths as an attached text
file (`notify_file_bytes`), then, only once the list was sent, a fixed localized line with the count and the fix. No file
name is in the chat text or reaches a model. The list goes through the media port of the chat platform
(`UserNotificationService` `platform_media`); a notice whose list was not sent is not retried until the next UTC day unless
more files need telling. A purge deletes the records and their flags, so files still closed after a rebuild are announced
once more.

### 4.19 Capture: how a text-bearing file was made

Every file whose text is searchable records `capture` (`scan`, `document_photo`, `screenshot`, `digital`) and
`capture_source` (`detected`: code decided from the file's structure; `automatic`: a model said it). A photograph has none.

- **PDF, in the parser child** (`src/adapters/drive_parser/capture.py`): the first pages are walked under their own small
  time budget (out of time: no signals, the text outcome kept) and return `CaptureSignals`: which pages draw text, which hold
  a full-page image, the median dpi, the page size, whether a single-image page's JPEG names a camera in its EXIF.
- **The rules** (`src/domain/drive_capture.py::detect`, pure): a camera in EXIF → `document_photo`; every page a full-page
  raster in scanner dpi → `scan` (with or without an OCR layer: a searchable scan is still a machine reading); a text layer
  on every page and no full-page raster → `digital`; anything else is ambiguous.
- **`file_capture`** settles the rest: a PDF uses signals, then the reader's answer; an image triage called `text` uses the
  camera facet, then the reader, then triage; every other parsed text kind is `digital / detected`. A deterministic answer
  beats a model's. **An ambiguous PDF the parser read keeps no capture**: it is not sent to the model for that alone (it
  would bill every mixed PDF).
- **Index words:** `CAPTURE_NAMES` in `drive_labels.py` (`cp:` detected, `cpa:` model-said); the query side emits them only
  for forms in the table. The card shows `capture: scan (detected)`, and the capture is the text's source in the accuracy
  score (§4.20).

### 4.20 Card accuracy

`accuracy = clamp(round(100 · (1 − S) · (1 − V)) − R, 0, 100)` (`src/domain/drive_accuracy.py::accuracy`). `V` is the
weighted share of graded values (§4.17) the text does not confirm (`FIELD_WEIGHTS`, `GRADE_MISS`; native topics weigh 0: graded
and reported, never moving the score). `S` is the text's source by capture (`SOURCE_SHARE`: native text 0, a scan or a model
reading more). `R` is fixed points for reading problems (`READING_POINTS`: pages refused, read by the fallback, truncated).
A photograph, a folder, a metadata-only card and a template profile without a brief carry no score. The score is stored
(`card_accuracy`, `accuracy_version`) and rendered `card accuracy: 88%`; it is **never in the card text**, so changing a
weight re-scores (a version bump, pinned by `weights_digest`) without a re-embed.

**The rule is in two texts only** (the specialist's `PROTOCOL_DRIVE_SEARCH` and Smart's reading line, `R_ACCURACY` in
`src/domain/drive_answer_texts.py`; the threshold is `CARD_ACCURACY_TRUST` in `src/domain/drive_index_params.py`, which code uses to pick the line, and those texts): find / which files / do I have is answered from the cards
at any accuracy; a question about a value is answered from the card at the threshold or more, and below it the specialist
asks for the file's passages (born-digital) or for the file itself (image text: passages would repeat the same reading).
Code chooses the line (`ReadFacts.low_accuracy_image`). The access line (§4.18) overrides the stale and accuracy rules. A
value written like an ID or digit group is also keyed without separators.

The migration gate key is `MIGRATION_GATE_KEY` (§3): adding a key to `CURRENT_VERSIONS` that the gate names rebuilds every
existing index.

### 4.21 Photo location prior and scene labels

- **The prior.** Triage's line for each thumbnail carries the place and rounded coordinates when the record has them
  (`coarse_location` in `drive_card.py`, rounded coarsely: full-precision GPS never leaves the index). The rule is in
  `COGNITIVE_PROCESS_DRIVE_TRIAGE`: name a landmark only when recognised in the image; the location decides between
  look-alikes and never invents. Landmark names stay in the description and objects (automatic, never exact).
- **Scene labels.** Triage returns `scene_labels` from the fixed `SCENE_LABELS`; unknown values are dropped, capped at
  `PHOTO_SCENE_LABELS_MAX`; a text image returns none. Keyed `sl:` (automatic class) from `SCENE_NAMES` in four languages;
  a query word emits `sl:` only for a form in that table, and the strict-AND allowances ignore `sl:` keys.

### 4.23 Transcripts of recordings

Without transcription a recording is known by name, date and length only. With transcription on, the bot
listens in the background and writes down what was said, with speakers and times (`[12:40] Speaker 2: …`). That text is
indexed like any document, so a search finds the recording by what was said and Smart can open the transcript. Nothing is read
aloud or sent to a chat on its own. Decision records:
[drive_transcripts_t1.md](../../04_solution_strategy/decisions/drive_transcripts_t1.md),
[drive_transcripts_mirror_archive.md](../../04_solution_strategy/decisions/drive_transcripts_mirror_archive.md).

**Who gets it: two switches.** (1) The **kill switch** `DRIVE_TRANSCRIBE_ENABLED` on the indexer (deploy key
`DRIVE_TRANSCRIBE_ENABLED_DEV`, required in `.env`, exactly `on` or `off`: `make deploy` refuses otherwise, because a missing
key would silently freeze in-flight batches): off means the stage is not even built. (2) The connection's own flag
`DriveConnection.transcribe_enabled`, separate from "index my drive": a **newly created connection starts with it on**
(`DriveConnectionService._create`); earlier connections keep their stored value and a stored document without the field reads
as off (`firestore_drive_connection_repo.py`). The owner's script changes the flag (`scripts/infrastructure/drive_transcribe_flag.py`);
there is no Cabinet toggle. The spend is bounded by the kill switch and the monthly cap
(`DRIVE_TRANSCRIBE_USD_CAP_DEV` can only lower it).

**What happens to one recording** (`src/services/drive_transcript_stage.py::DriveTranscriptStage`, on the indexer):
1. A recording the policy admits becomes `ready` on the record (`transcript_state`, newest first). An upload still in
   progress waits out the quiet period; an edited recording waits a cooldown after it was last bought.
2. The stage asks the drive for a fresh short-lived URL and **probes** it (never stored, never logged): length, audio
   track, container. Over `TRANSCRIBE_MAX_SECONDS` or `TRANSCRIBE_MAX_BYTES`, or no audio: a **stub** (no model call). Else
   the whole file's cost estimate is **reserved** against the month's cap; if it does not fit the file is `parked` until next
   month, whole, never half bought.
3. The sandbox's media service cuts **audio-only** windows (about `TRANSCRIBE_WINDOW_S`, with an overlap) over HTTP ranges;
   the indexer serves the ranges itself (`HttpRangeSource`, `src/adapters/sandbox_client/media_range_source.py`), so a
   provider URL or token never enters the sandbox. Each window is uploaded to Google's Files API.
4. One **Gemini batch** per user per step carries every ready window as an independent request
   (`GeminiBatchAdapter`). Later ticks poll, collect, **book** the real cost into the month of the reservation, release the
   reservation and delete the provider's copy of each answered window.
5. Each answer is checked: valid JSON, the last segment reaches near the window's end (a silently truncated answer is a
   failed attempt), times monotonic and inside the window. A bad window is resent a limited number of times, then becomes a
   gap marker; a provider refusal is a gap marker at once.
6. The windows are **stitched** by code (`drive_transcript_stitch.py`): times re-based, the overlap cut at its middle,
   speakers matched across windows by the words in the overlap.
7. The result is **indexed** through the ordinary text path and stored under the file's content key; the card gains a
   `transcript:` line. A copy, rename or move has the same key and never pays again.

**Record states** (`transcript_state`, `src/domain/drive_transcript_plan.py`): `none`, `ready`, `working`, `waiting`
(submitted), `current` (done, or a stub), `held` (timed: cooldown, quiet, retry, resubmit), `parked` (the month's cap),
`failed` (`MAX_ATTEMPTS` charged attempts). A model outage is not a state: it pauses the stage's queue and records stay `ready`.
Outcomes: a transcript with text is `indexed`; `no_speech` / `no_audio` are `metadata_only / no_text` (not counted as
unread); `too_large` / `too_long` are `metadata_only / too_large`; a file the decoder cannot read fails after three
attempts. A batch is written down as `submitting` before it is sent; an unclear send is found again at the provider by name
and otherwise its money is released once; a poll error counts its money as spent.

**Money.** A fifth usage class, `transcribe`, a **hard** cap (open reservations count), no initial-build share. Batch is half
the interactive price, billed at the **held** final price of the Gemini Flash model so cost is never under-reported; a file's
reservation is an upper bound (`TRANSCRIBE_HOUR_USD_ESTIMATE` plus a per-window allowance). The batch has no
`prompt_content` row and no Logfire span.

**Search and open.** A transcribed card carries the transcript line; passages come from chunks with a time stamp at their
head. Opening a transcribed recording returns the transcript inside the untrusted wrapper, bounded like any drive text
(§4.25); with `original: true` it returns a link, never bytes into chat. A date said in a recording never changes the file's
date.

**Privacy.** Audio goes to Google only (Files API, deleted after collection). Transcripts are other people's speech: stored
like chunks, never logged, served as untrusted data. An opt-out purge removes jobs, batches and stored transcripts and books
any reservation backing audio still running at the provider; a **rebuild and a disconnect keep the stored transcripts**
(`purge_keep_transcripts`; heads the connection's records carried are marked `retained`), so audio is not bought twice.

| Event | What happens |
|---|---|
| Gemini outage, 429, a missing key | Uncharged: the stage stops extracting and submitting for a while; the rest of the tick goes on |
| The decoder cannot read the file, the drive refuses the URL | A charged attempt, `held / retry`, `failed` after three |
| A window answers invalid, short or with a response error | Resent (limited), then a gap marker |
| A batch expires or fails whole | Its open windows go back once |
| The cap does not fit the file | `parked` until the 1st of next month |
| The recording is edited or deleted while its batch runs | The result is booked and dropped; the reservation released |
| Process dies between two writes | The next tick finds the `working` record with no job and starts again; booking is once per batch |

#### The mirror folder

**Where it lives.** With transcription on, the bot also writes each transcript as a Markdown file into **`_Transcripts`**
(`src/domain/drive_mirror.py::ROOT_NAME`), a folder it owns under the connection's **write folder**: a drive with no write
folder gets no mirror, and when the owner moves the write folder the mirror follows. The tree of the source folders is
repeated inside; a short `_README.md` in the user's language says the folder is managed by the bot. The mirror calls no model.
The index never indexes the folder (its subtree is opaque), so a plain search never returns a mirror file.
`DriveTranscriptMirror` (`src/services/drive_transcript_mirror.py`, ports only) reconciles it.

**The files belong to the bot (owner ruling).** You do not edit transcript files on the drive: every `.md` in `_Transcripts`,
active or archived, is overwritten by the bot (at the weekly audit at the latest) and a rename or move inside the folder is
moved back. To keep your own version, **copy the file out**. Deleting an archived file makes the bot forget it; moving a file
out of `_Transcripts` makes it your copy. A **detached** transcript keeps its stored copy under a pin and is overwritten the
same way. **Chat drive writes are refused under, into or over `_Transcripts` and its ancestors** (the guard in
`UserDriveService`; the agent answers one localized line).

**What happens to a file.** The mirror step is the last step of `_work_off` (§5). A recording whose transcript is `current`
is made due by a flag on its record (`mirror_due`, set when the digest of its name, parent, link and transcript changes): the
step creates the file after the folder chain, rewrites it when the rendered bytes' hash changed, and moves or renames it with
one PATCH when the source moved. A name that is taken gets a numbered suffix; the reserved names (`ROOT_NAME`,
`README_NAME`, `ARCHIVE_NAME`, `LONG_PATHS_NAME`) win any collision. **Delete → archive:** when the source is deleted the
ledger gets a durable mark; after a complete round and a delay (so a restore cancels it; **moving a recording out of the App
Folder counts as a delete**) the file moves to `_Transcripts/Archive/<date>/…`. The bot never deletes an archived file; its
stored transcript stays pinned, and only the owner's delete of the archived file releases it. A source that merely left the
index's scope is **detached**: the file stays, pinned, and a weekly check turns it into an archive if the source is gone.
Only the deleted-files search (`search_deleted_recordings_and_notes`) reaches archived transcripts: it reads the ledger,
shows a few hits, works even with drive search off, and never mixes into a normal search.

**What keeps it honest.** Two collections no purge deletes: `drive_mirror_roots` (keyed by user and drive, not connection)
and `drive_transcript_mirror` (the ledger). Without them a purge would let the bot create a second root and index the
first as documents. Writes are idempotent: `conflictBehavior=fail` everywhere, a create records its intent first and a re-run
adopts only what the app created, so a foreign file is never overwritten; every write is checked to end under the current
root. A weekly audit overwrites an edited active or archived file, moves back a moved one, counts and leaves a foreign
item. If the root disappears nothing happens for `MIRROR_ROOT_GRACE` and until a complete round and a confirming read
agree; then archived documents are released and active ones re-created under a new root.
A mirror write failure for one file is charged to that file with a backoff and parked after repeated charges; throttling,
re-auth or a store outage is systemic and ends the tick; a full drive pauses the mirror. Indexing turned off ends the tick
`disabled` and the next tick purges the index and **leaves the drive alone**.

**The stop lever.** There is **no mirror-only off switch** (owner ruling): the mirror is part of transcription. To stop it,
turn off the user's flag or the kill switch; **both also stop transcription itself, batches in flight included**.
**Known residuals:** moving an active mirror folder back after the owner took it out also carries any of their own files
inside it into the opaque area, where they drop out of search; a `_Transcripts` that already exists before the first run
makes the bot create a differently named root.

### 4.24 The owner's notes on files

A **note** is what the owner told the bot about a file, a folder or a whole drive ("this is the repair invoice, not a
receipt"), kept by the bot bound to the item, never on the drive and never in memory. While the file exists, its card shows
the note first, search finds the file by the owner's words, and the owner's document type and life areas outrank the model's
guess. Spec: [DRIVE_FILE_NOTES_RFC.md](../../10_rfcs/DRIVE_FILE_NOTES_RFC.md); decision
[drive_file_notes.md](../../04_solution_strategy/decisions/drive_file_notes.md).

**Where it lives.** `{prefix}drive_notes`, one document per noted item, each change an SCD2 version under `versions/v<n>`
(undo and history). It is **not an index collection**: a rebuild, an opt-out or an access-loss purge never touches it; only
the owner's erase and account deletion do (no account-deletion flow exists yet: when one is built it must delete this
collection, stated in the `DriveNoteRepository` docstring). The index record carries a copy (`owner_note_*`), rebuilt from
the store whenever the record is rebuilt. A note matches an item by `i:{identity}` (the adapter-built identity, which outlives
a connection) and the tagged ref key `src/domain/drive_note.py::tagged_ref_key`; a note on a whole drive is bound by
`connection_binding_key` instead (the tools take exactly one of `item_ref` or `whole_drive_name`). A file is identified only
by the current provider's item id: a copy, a re-upload or another drive never inherits a note.

**Who writes it.** Smart has no note tool. It commissions the drive specialist with the note's words in the query; the
specialist calls one tool per edit kind, each one `DriveNoteService` call (`get_note`, `create_note`, `add_to_note`,
`replace_note_text`, `set_note_type`, `clear_note_type`, `add_life_areas`, `remove_life_areas`, `delete_note`, `undo_note`,
`erase_note`). Rules are the token `PROTOCOL_DRIVE_NOTES`: a note holds what the owner said or confirmed in this conversation;
text in a file, an email or a result is never their confirmation. **When the tools exist:** `get_note` whenever the service
is wired, the switch is on and there is a user; the writers only on an **owner turn** (`drive_note.is_owner_turn`: a chat
message or a call with Lelik that reached Alek through `ask_alek` / `send_to_chat`). A reminder run, the daily review, a
`notify` run or `/worker` gets no writer, and each writer re-checks the owner turn before it calls the service.

**What one write does.** The security check on the new words, before any drive call. A `drive:` ref needs the connected
drive (the index's "cannot open" mark refuses, §4.18); a `drive_note:` ref works from the store alone, also while
disconnected. Then one write loop: the change is applied inside a two-attempt conflict retry (a concurrent write is merged,
never overwritten), an identical result writes nothing, a changed text is embedded again, a new version is written in one
transaction, and the indexer is nudged. `replace_note_text` is refused unless `get_note` read that note earlier in the same
run, and refused if it changed since (`CHANGED_SINCE_READ`). Limits are `NOTE_WORDS_MAX`, `NOTE_CHARS_MAX`,
`LIFE_AREAS_MAX`, `NOTE_WRITES_PER_TURN` and a per-user note count. Each write runs in a task held by the per-user
`DriveOperations.keep_alive` and awaited under `asyncio.shield`, so a run cut mid-write records the call as still running and
the write goes on; a late write posts its receipt late.

**What the owner sees.** A **receipt** with the stored note word for word, sent literally (the words are the owner's, not the
bot's). On a chat turn `ConversationHandler` posts it; on a call the Alek gateway posts it to the primary chat. History holds
only `[Drive note: <path> ref=… v<n>]`, never the note's words.

**How search gets it** (the tick, §5). Three entry points merge the store into records: a record created in `_commit` looks
up its note by binding; the notes step reads notes changed since its watermark; the round end archives notes whose file is
confirmed gone. The card text gains `noted by the owner: …` (one card re-embedding); the note's words, numbers, dates and the
owner's type and area names are keyed with `AboutField.OWNER_NOTE`, so a match is **About** with `matched: your note "…"`. No
`CARD_VERSION` or `KEYS_VERSION` bump. Search cards and opened files show the note inside the untrusted wrapper; a folder
listing never shows it. Smart gets the reading line that a `noted by the owner:` line outranks the card's own description and
type and is not part of `card accuracy`.

**Deleted files and other drives.** A record that leaves the index archives its note only when the delete said "gone" and one
`get_change` answers "not found" at the round end; a file moved into a repository or a dense folder keeps its note active. A
daily audit catches deletions the checks missed. Archived notes never appear in a default search; the specialist's
`search_deleted_recordings_and_notes` finds them (with the transcripts of §4.23), each with its `drive_note:` ref. Notes bound
to a disconnected drive are dormant and appear there labelled.

**Erase.** `delete_note` keeps the history. Erasing needs a code: `erase_note` sends one for one note, and the owner types
`$drive-notes erase-all` for all of them; pasting `$drive-notes erase <code>` (or `erase-all <code>`) runs it before any model
call. A code works once, for a short time. **The switch:** `DRIVE_NOTES_ENABLED` (read with `os.getenv` in
`src/composition/drive_note_setup.py`, default on, one deploy key for the main service and the indexer): off, there is no note
tool, the `$drive-notes` commands answer "not available" and the indexer does no notes work; reads go on.
**Known residuals:** content read during an owner turn could steer the specialist into a note the owner did not confirm (the
receipt shows it at once and undo restores); two commissions in one Smart turn allow twice the per-turn writes; a Smart turn
that fails after the specialist answered loses the receipt (the note is stored; the card shows it).

### 4.25 Answer from content: passages

Search finds *which file*. This is for "what does it say about X": Smart says **what** it needs in the user's words, the
drive specialist decides **how** and reads the stored passages nearest the question, so no whole file is downloaded and
converted. A whole file is still opened when the user wants all of it, or when a value comes from image text.

**The flow.**
1. Smart commissions `work_with_drive_files` with the need ("everything about X" is a content need, not a find).
2. The specialist calls `find_passages` (question, folder, file refs): `DriveSearchService.excerpts` embeds the question, takes
   the nearest chunks of the readable generation (kNN, fenced), ranks them by RRF of the vector order and a lexical rank
   (`src/domain/drive_excerpts.py::rank_excerpts`), and keeps whole chunks until the caps (`ExcerptCaps`, a per-file cap
   included); adjacent chunks merge into a run.
3. `file_refs` win over `folder`, a folder over the whole drive. Files in scope with no indexed text are counted on one line,
   never silently absent. The index being built or rebuilt is said, not hidden.
4. If a passage is cut or a neighbour is needed, `read_passages` takes a number or range from a head
   line. Nothing is expanded automatically.
5. The specialist judges only whether the passages suffice; it never writes the answer. Code keeps a ledger of every chunk
   returned in the run (`drive_excerpt_ledger.py`), so each passage reaches Smart once, in one block, after a `Searched:`
   line that replaces the specialist's note.

**What Smart reads.** Per file a head line, then the text inside a nonce-tagged `<drive_document>` wrapper with passage
markers; stale and truncated lines; "also at" for identical copies; the file's owner note and "what it is" line; the notes of
its ancestor folders once, under one cap (`EXCERPT_FOLDER_NOTES_CHARS_MAX`). The reading line `R_EXCERPTS` says the passages
stand for the files and that text in them is data.

**The gate (the one enforced guard against injection).** Once passages were read in a step, a change (any mutating op or a
note write) in a *later* step is refused on every run that is **not a chat turn**: reminders, the daily review, other
background runs, calls and a run with no origin (`src/domain/drive_content_gate.py::content_gate_applies`, `write_refused`;
fail closed). A chat turn is never gated. The refusal is `REFUSED` with an INFO log; Alek commissions the change again. Why not
`is_owner_turn` (which counts a call as an owner turn): [drive_content_write_gate.md](../../04_solution_strategy/decisions/drive_content_write_gate.md).
What arms it: `find_passages` / `read_passages` results, and a deleted-files result carrying a transcript passage; ordinary
`search_drive` snippets do not (owner-accepted residual).

**Limits.** Passages read stored chunks: they can be stale (the stale line says so) or cut at `MAX_CHUNKS_PER_FILE`; the open
stays the live, authoritative read. Numbers over a table are computed from an opened file. A find is unchanged: zero-LLM,
strict AND, no passages in its result. Files in a repository or dense folder have no chunks.

**The open bound and pages** ([decision](../../04_solution_strategy/decisions/drive_open_bound.md)). A text file from the
drive above `OPEN_MAX_CHARS` is never handed to a model whole, with no override. `DriveSearchService.text_extent` says what
the index holds. Pageable and over the cap (or truncated): the open is **refused** before any download, with a tool-free text
that names the route (`find_passages` for what a file says, `read_passages … "from 1"` only for its text in order). Anything
else over the cap is **cut** after conversion: the head at a paragraph plus a cut line. Vision opens are unchanged.
`read_passages` with `from N` returns a page and a page line. **One page per commission**: a second paged `read_passages` in
the same specialist run is answered `ONE_PAGE` (`DriveToolRun`), and Smart asks for the next page in a new commission with the
`next: from N` number its history line names. A failed extent lookup fails safe.

**Opens go through the document service.** Every drive open and stored-file open from `DriveOperations` goes through
`FileConversionService.open` (`docs/05_building_blocks/file_conversion/README.md`): kind from bytes, caps table, native or
pipeline, the parser, the reader under a character budget, all under the opener's deadline. The §29 refusal and cut line stay
in `DriveOperations` (`_open_drive_item`), which applies the drive wrapper and the bound itself, then the owner's note block.
A recording keeps the transcript-first rule (§4.23): without a transcript the open never transcribes.

---

## 5. The tick

`DriveIndexingService.tick(scope, nudge)` runs only on the indexer, one connection at a time. The `drive_sync` payload is
`{user_id, connection_id}` (plus the nudge); a payload without a connection is refused (`src/web/indexer_worker_app.py`). The
fan-out lists the `ACTIVE` connections with indexing on, so a user with two drives gets two ticks, each under its own lease;
caps are per drive.

1. **Gate** (`src/domain/drive_index_planner.py::access_action`), from Firestore, never from the agent cache, on the
   connection and its sync state: connection absent or `DISCONNECTED` → purge, retaining the paid transcripts its records
   carried (nothing stored → `disabled`); indexing off with anything stored → purge (opt-out); a started purge always
   finishes, then onboarding; connection `NEEDS_RECONNECT` (read from the connection, never from the credential: a decrypt
   or KMS error is never read as "no credentials") → stamp `access_lost_at` once and stop, purge after `ACCESS_LOSS_TTL`; a
   stored `drive_identity` that differs from the connection's → purge (drive switch). A reconnect resumes from the stored
   cursor when the drive is the same. The delta cursor carries the drive id and App Folder id it came from: a mismatch
   restarts the enumeration, and a page from another drive purges and re-onboards. A 404 on a stored cursor restarts the
   enumeration like a 410. **The migration gate** (§3): a state without `MIGRATION_GATE_KEY` does no tick work
   (`awaiting_migration`).
2. **Lease** in a transaction. A live lease answers `busy`: a nudge re-enqueues itself under a new key, any other tick
   comes back once per holder after a delay.
3. **Observe** (`_observe`, `_apply`): items named in a nudge first (`get_change`, cards due in class 0), then delta pages
   until the round completes or the start budget is spent. A full enumeration (onboarding, an expired cursor) opens an
   **epoch**; the sweep at its end deletes what it did not see, then sweeps orphan chunks and hints.
4. **Work off** (`_work_off`) after a complete round, by class (§4 step 2). A class that runs out of budget stops the lower
   classes. Order: the notes' round-end checks; the epoch sweep; the scope step (below); with transcription on, the cheap
   transcript steps (poll, collect, finalize); classes 0–2 within their share, then the **enrichment** phase (§4.17, at
   least `ENRICH_SHARE` of the start budget), then classes 3–4 (keys work included) within the maintenance share; the
   transcript extraction step in its own window (`TRANSCRIBE_SHARE`); the **upgrade pass** only if no class stopped on its
   budget, then the card-cap and chunk-cap releases; and **last the mirror step** (§4.23) with a reserved tail, so the
   content classes stop shortly before the start budget's end while the mirror has work. Waiting for a batch continues
   nothing, and the mirror step never asks for a continuation: the periodic fan-out polls and paces it.
5. **Continue**: release the lease, then enqueue a continuation if anything is left.

**The scope diff** (`_apply_scope_change`, called directly before the observe step; `_scope_step` later does the top-level pass, skeleton re-derivation and removing roots; DRIVE_SCOPE_WALK_RFC). After the gate and before the feed it reads
the stored parent map of the saved, applied and removing roots (a read that runs out of item calls leaves the diff to the next
tick), then marks the work and stamps the new set. It deletes nothing itself. **Walk:** a root the index does not hold gets one
`get_change` and becomes a normal folder with `reobserve_pending`; the listing is the existing re-listing recursion
(`_reobserve_step`), which spends no item call but a time share (`WALK_SHARE`), is **class 1** (structure) ahead of the
content it brings, and descends only into folders new to the index. A listing that fails `MAX_ATTEMPTS` times is not dropped:
the folder stays pending, due again after `RETRY_CAP`, and counts as blocked. **Guarded removal:** a removed root is marked
`DELETING` with `cascade_unstored`, or turned into a path folder with `trim_pending`. Every removal step starts only through
`_removal_open`: the tick's ancestry is loaded, no feed change of the tick touched a folder, and a **fresh forced re-read** of
the connection shows the scope the tick started with; a closed guard makes the step wait, and a scope saved mid-tick is seen
by that re-read (no more paid work, no removal that tick). The cascade judges the folder again before removing anything: in
scope again, it is revived and listed; a path folder becomes a skeleton; only a negative verdict cascades, and never over a
child the scope keeps (roots, the paths to them, the mirror's folders). The mirror is told `unstored`, so the owner's file
notes survive a removal. **Whole drive keeps the restart:** a change to a scope that covers everything (and any change made
while an enumeration is unfinished) clears the cursor and bumps the epoch; the epoch sweep then drops what the new scope no
longer holds. **Skeleton paths** are re-derived when a root or path folder is deleted, moved or newly stored.

**Errors are classified by what the call is about** (§4.10 for content outcomes):
- *Per item.* A probe, a parent lookup or a re-listing page that fails defers that one item with backoff. A parent lookup that
  keeps failing transiently keeps the record `unresolved` and retries; only a definite not-found removes it as an orphan. Item
  calls share one budget (`ITEM_CALLS_PER_TICK`). A change that fails inside a page is skipped and logged at ERROR, and the
  page still applies.
- *Per record.* A card the embedding provider refuses (400) backs off alone; after `MAX_ATTEMPTS` it is `card_refused` until
  its text changes or `CARD_VERSION` is bumped.
- *Model outage.* Defers model work only; the profiler and passages back off per agent type.
- *Systemic.* The delta feed, the token endpoint, any other embedding error and the repository end the tick, after the card
  batch's finished vectors are committed. A throttled drive waits the provider's Retry-After the first time, anything else
  backs off exponentially to a cap. The error is written onto the state as last saved, so page-by-page progress is kept; an
  index run interrupted by a systemic error is abandoned without counting an attempt.

**Purge** is keyed on facts only, never on an exception: a rejected refresh (`invalid_grant`, `interaction_required`,
`consent_required`) sets `needs_reconnect` and stops. A purge deletes hint documents, then chunks, then records, then the
sync state; the usage and initial-build documents are kept, and the user's stored transcripts are kept by a rebuild, by a
disconnect and whenever the user has another connection. Indexing turned off mid-tick drops the in-flight result and enqueues a
continuation, whose gate purges. Decisions:
[drive_index_opt_in_access_loss_grace.md](../../04_solution_strategy/decisions/drive_index_opt_in_access_loss_grace.md),
[drive_indexer_lease_queue_service.md](../../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md).

---

## 6. Search

The specialist's `search_drive` tool (`query`, `folder`, `kind`, `date_from`, `date_to`, `drives`) calls
`DriveOperations.search`, which calls `DriveSearchService.search` (`src/services/drive_search_service.py`), zero-LLM: one
query embedding is the only model call. Per the tool text (`src/domain/drive_tool_texts.py`), `query` takes the subject in the
user's words in one language (a name, a place, a topic, a **document type or purpose**, or what to find inside); a file
format goes in `kind`; **a time span goes in the date fields**, not in the words.

**Which drives.** `search_drive` and `find_passages` take an optional `drives` (names, as `drive_state` lists them). Empty
searches the drive the `folder` names, else every drive **searched by default** (none flagged: the primary). A folder that
starts with a drive's name is searched on that drive; a prefix naming another drive is refused; an unknown name is refused
with the list of drives. One search runs per connection over its own index, then
`src/domain/drive_search_merge.py::merge_results` joins them: cards in (tier, mount order, own rank) order, tier totals
summed, coverage combined so anything one drive could not see stays named. Identical content on two drives is one card with
the other copies under `also on`. Passages are fetched per drive and merged the same way (`merge_excerpts`); their ledger is
keyed by ref tag because item ids can repeat across drives. Each drive's search skips records outside that connection's scope
(while a narrowed scope is still being re-indexed). **Every live drive not searched is named** (`drives not searched: …`, which
makes coverage incomplete so "complete" stays a reliable not-found; the reading line `R_NOT_SEARCHED` tells Smart to say so, and the specialist's `PROTOCOL_DRIVE_SEARCH` carries the same rule).
`read_passages` takes no `drives` (its ref names the drive).

**The pipeline, in order:**
1. **Gate** (read uncached): the opt-in flag, the sync state and the connection: `disabled`, `disconnected` (a different
   drive connected says so), or results marked `reconnect_needed`. While a purge runs, or before the first root is stored, a
   connected drive gets no results and `building`.
2. **The migration gate.** A sync state without `MIGRATION_GATE_KEY` is **not searched at all**: no query embedding, no read.
   The answer is the reference reminder, the envelope and `REBUILDING_LINE` (the index is being rebuilt, so nothing could be
   searched yet and the result says nothing about what the drive holds) with no tier, count or "No file holds"; a filter-only
   listing answers the same, never from old records.
3. **Folder scope:** a folder filter is a subtree, resolved by ref or path; several matching folders come back as candidates,
   never guessed; a folder that hides its contents (a repository, generated files, a dense or deleting folder) says so and
   points to listing it. A subtree up to `FOLDER_INMEM_CARDS` is matched in memory; past `FOLDER_INMEM_CHUNKS` the word and
   hint key streams run user-wide with a folder post-filter while the chunk and hint kNN run over the walked readable files
   only (`find_nearest_chunks_in`), so exactly the folder's top K. A larger (incomplete) walk runs the user-wide streams with a
   post-filter.
4. **Concepts → streams → document frequency → candidates and coverage → tiers → Related → render** (§6.1–§6.3).
5. **No concept left** (empty, punctuation, only function, generic and time words) with a filter is a **filter-only
   listing**: a plain query on kind and `effective_date`, newest first, `K` cards, with a count and the words it did not
   search for named. With no filter either, generic words are searched as name words by the name stream alone.

**Filters.** `kind` takes a format, an extension, a plural or a family (`spreadsheet`, `document`, `word`, `presentation`,
`photo`); an unknown value is refused. A single kind filters the about stream through its own composite; a family is
post-filtered (an `array-contains-any` would pass Firestore's disjunction limit). Date and folder filters are post-filters;
date-only bounds are UTC days ([decision](../../04_solution_strategy/decisions/drive_date_filters_utc_days.md)). A full
page that a filter thinned is never reported as "no match" (the `capped` hedge). **For an expiry query the date fields bound
the expiry date**: a concept covered by validity words (`lv:`) runs on `valid_until` from today (`find_expiring`, soonest
first), and expired files are counted on one line unless `date_from` is set. While the index's vectors are in another
embedding space than the query's, the kNN pages are skipped and the coverage line says the index is being refreshed. Duplicates
(same content hash and size) collapse into one card. The drive is not part of `SearchEnrichmentService`
([decision](../../04_solution_strategy/decisions/drive_index_not_in_search_enrichment.md)).

### 6.1 Concepts and streams

**Concepts** (`src/domain/drive_keys.py::query_concepts`; §3.1 builds every key):
1. The query is clipped to a length bound and tokenised as the index does.
2. **Phrase runs** of 2–3 consecutive tokens (function words included) give phrase keys (`e:`, `ex:`/`exl:`, `ef:`, `k:`,
   `pe:`) to the words they hold.
3. **Numbers first**: the maximal form of each number-like span (an amount, an ISO date, a joined id, a digit group); a month
   name with a year is one month concept.
4. **Removed words**, each named in the result's searched line: function words, generic search words (`GENERIC_WORDS`) and time
   words (`TIME_WORDS`). A place-like word is never generic.
5. **Word concepts**: every remaining word emits, **uncut**, word keys, hint keys and about keys; each list is fetched in
   batches of the Firestore `array-contains-any` limit, so a word's hits never depend on its neighbours.
6. At most `MAX_CONCEPTS`: numbers first, then words in query order; the rest are named as not searched.

**Streams** (parallel, Firestore only, lean projections: no text, no vectors): the **about** stream (records whose
`about_keys` hold a concept's key, `ABOUT_FETCH`), the **name** stream (whole name keys, then single trigram queries that find
similar names but never cover a word), the **word** stream (chunks whose `word_keys` hold a key, grouped by file,
`WORD_FETCH`), the **hint** stream (`HINT_FETCH`), and **meaning** (one query embedding: card, chunk and hint kNN with
distances). Meaning feeds only the Related tier.

**Document frequency** (`df`) per concept: the distinct files in the union of its about, word, hint and whole-token name hits,
**user-wide** (before any filter), flagged as a lower bound when a page was full. **Candidates**: the in-scope hits of the
rarest *required* concept, plus the next one's when its page was full; each candidate's coverage is then tested on its about
keys, name keys, and the `word_keys` of its matching chunks and `hint_keys` of their hint documents, read by id.

### 6.2 Tiers: how a file matched

A concept is covered in a file by a key of one of four **source classes** (§3.1): **verified record**, **passage words** (one
chunk's keys), **translation**, **automatic record**. Each concept takes its strongest cover, and a file's tier is the **weakest
class it needs** (`src/domain/drive_tiers.py::classify`):

| Tier | Exact? | The file is in it when |
|---|---|---|
| **About** | exact | every concept is covered by verified-record keys, or the name covers every word |
| **Mentioned in** | exact | every concept is covered by the record or by **one chunk's** words, at least one by the chunk; two or more chunk-covered words sit within `PROXIMITY_CHARS` |
| **By translation** | automatic | as above, but at least one concept is covered only by a translation key |
| **Labelled** | automatic | at least one concept is covered only by an automatic key |
| **Related by meaning only** | unverified | no tier above, and `meaning ≥ Z_RELATED`, or a similar name; a few lines, never counted, never called a match |

Each file is shown once, in its best tier; within a tier files missing no word first, then the meaning score, then newest. A
skeleton match is labelled as a spelling in another script (automatic).

**Strict AND, with allowances.** Within a concept its keys are OR; across concepts, AND. A concept's weight is the idf
`log(1 + N / df)`:
- **Absent** (`df = 0` user-wide): never dropped, never forgiven. Every tier but Related is empty and the result says so with
  one **count line**: `No file holds "X". Without "X": N files match the other words.` ("at least N" when a page was full). No
  cards, so Smart decides whether the word was phrasing or the subject. Under a filter, "No file in this scope holds …" only
  when the scope was actually read: a word whose page was full and no scoped read covered is never stated absent.
- **Low weight:** with three or more concepts a file may miss the single lowest-weight concept. **A number (an amount, an id,
  a year) is never that word**, so "electricity bill 2024" never returns a 2026 bill.
- **Common:** a word held by more than `COMMON_SHARE` of the carded files may be missed too, unless every word is common; it is
  named in the searched line and on the card. A number is never common. Absent and rare subject words still block, so honest
  negatives stay negative.
- **No file matches all** (every concept exists, no file holds them all): one footer line counts each word.

Labels never become a filter on their own (a specific request mapped to its broad label turned honest negatives into junk).
There is no judge.

**Related by meaning only.** First similar names, then files whose **meaning score** clears the floor. Each kNN source (card,
chunk, hint) scores its pool as a robust z-score; a source with too few files **abstains**; chunk and hint scores of a long
file are reduced by a length term; `meaning` is the max over the sources that did not abstain; the floor is `Z_RELATED`, an
uncalibrated value (RFC §25.7.1). When every source abstains on a very small drive and nothing else matched, the best two files
are shown as "closest by meaning; the drive is too small to tell related from unrelated".

### 6.3 What Smart sees

`render_results` (`src/domain/drive_search.py`) → `render_tiered` (`src/domain/drive_tier_render.py`), frozen texts in
`drive_search_texts.py`. Per result, in order: the **reference reminder** and **envelope** ("The index reflects the drive as of
…", the folder scope, "The index is full", the reconnect line); the **searched line** (what was searched, ignored and ignored
as common); the **coverage line**; the **tiers**, each with a heading, shown counts per tier and **group lines** past them
(top folder × kind × months); the **footers**.

**The coverage line** is `Coverage: complete.` or `Coverage: incomplete — <parts>`: the index is still being built or
documents are still being read; the index is being refreshed for a new embedding space or re-keyed; only names and metadata
were searched; "N text files are not described yet" (a profile or passage job `ready`, `deferred` or `parked`) and "could not
be described" (`held`), where folders, photos and files already described are not counted; "N documents are not read yet"
(the `not_read_yet` counter: `pending` with no readable generation, not card-capped; documents that **could not** be read are
not named, see [the decision](../../04_solution_strategy/decisions/drive_unreadable_files_cabinet_only.md)); a candidate page
was full; words held by many files that could not be checked; shown files whose keys were cut by a cap; words past the cap not
searched. **Files without passage keywords by policy** (EPUB, sheets, JSON, files past the hint cap) are **not** a gap: their
own words were searched, only translated and synonym keywords are missing by design, and counting them made every answer
hedge. The enrichment counts come from the sync state, so a search runs no count query. When the coverage line is incomplete,
each footer speaks of "the files searched" and none says "final".

**A card** is one nonce-tagged `<drive_document>` wrapper holding the card text (capped at `CARD_RENDER_MAX`), then:
the match explanation (`matched:` with each source labelled "automatic" when it came through an automatic key; a cut `passage:`
for a chunk or grounded-keyword match), the date, validity, capture, accuracy, owner-note, scene and index-state lines, and the
stale, truncated and keys-cut flags (`card_body` in `src/domain/drive_tier_render.py` owns the order). Related cards are shorter. Past `RESULT_CHARS_MAX` the lowest tiers' cards are cut first
and the result says so.

**The nonce is the defence**: only a wrapper-tag look-alike in the text loses its `<` (its gaps bounded so hostile whitespace
cannot make the escape quadratic), attributes are escaped, and a normal path reads as written. The same wrapper carries
opened drive text. When drive search is off, or the drive is disconnected, the result is one line saying so with "Do not search
the drive again in this conversation".

**Smart's drive guidance** is split by who acts on each rule: the search mechanics are the specialist's token
`PROTOCOL_DRIVE_SEARCH`; the rules Smart needs to answer reach Smart as **reading lines** (`src/domain/drive_answer_texts.py`,
the `R_*` texts, chosen by code from the blocks of each answer by `drive_answer.reading_lines`: tiers with a search block, the
passage rule with a passage, the accuracy rule with a card accuracy, the scan line with a scan, versions with two or more
cards, the not-found bullets). Changing a reading line is a code deploy; `PROTOCOL_DRIVE_SEARCH` is a Firestore upload. The
`drive_specialist` block of `PROTOCOL_AGENT_SELECTION` keeps the intent, when to use the drive and the safety line.

### 6.5 Dates in queries and filters

**The one date rule.** The `query` text keeps a date written *inside* a document in the words (it becomes an `n:` number term
and meets the document's own date in any of five languages); the date fields hold a **time span the user gives** (a span left
in the words is removed as time words and named in the ignored list). The `again` block of `PROTOCOL_DRIVE_SEARCH` keeps the retry rule: if a
date-filtered search does not show the file asked for, search again without the filter. For what expires the fields bound the
expiry date.

Which date a filter uses (`effective_date`: `_effective` in `drive_index_planner.py`, `effective_from` in
`drive_enrich_plan.py`; bounds are UTC days): a profiled document, the verified issue date of its profile, else the brief's
date (label `written in the document`); a photo, the taken date (`taken`); a transcribed recording and any other file, the file
date (`file date`): the file-system modified time the drive reports, else the drive's own modified time. A date said aloud in
a recording never re-dates it.

### 6.7 Who searches: the drive specialist

Smart sends one commission, `work_with_drive_files`: what the user wants, the words to find as the user wrote them, every
`drive:` ref, `[File: …]` label and delivered document key, and `file_contents` for texts to save or append. The `DriveAgent`
(`src/agents/drive_agent.py`; strategy entry `drive` in `src/services/agent_context_builder.py`: Claude, with OpenAI as the
fallback for the **first call only**, because after a tool turn the transcript is provider-locked) runs at most `DRIVE.max_turns`
within a soft deadline (earlier under the caller's budget) through local tools, each one `DriveOperations` call: `search_drive`,
`find_passages`, `read_passages`, `search_deleted_recordings_and_notes`, `list_folder`, `open_drive_file`, `get_recording_link`, `copy_file_to_drive`,
`save_text_file`, `create_folder`, `move_file`, `append_to_file`, `replace_file_content`, `delete_drive_item`, then
`report_to_orchestrator`; with the notes switch on, `get_note` and, on an owner turn, the note writers (§4.24). A turn-cap exit
is logged at ERROR. **Provider trap:** per user the provider is resolved by `AgentContextBuilder.resolve_provider_name` (per-agent override, then the global `provider_preference` when the strategy allows it, then the default); `openai` is in the `drive` strategy's allowed providers, so a user whose global preference is `openai` runs the specialist on OpenAI at the user's tier (the strategy comment in `src/services/agent_context_builder.py` notes OpenAI's BALANCED is a small model, which is why the fallback is pinned to PERFORMANCE). Pin `config.agent_providers["drive"] = "claude"` for such a user. **Provider trap:** per user the provider is resolved by `AgentContextBuilder.resolve_provider_name` (per-agent override, then the global `provider_preference` when the strategy allows it, then the default); `openai` is in the `drive` strategy's allowed providers, so a user whose global preference is `openai` runs the specialist on OpenAI at the user's tier (the strategy comment in `src/services/agent_context_builder.py` notes OpenAI's BALANCED is a small model, which is why the fallback is pinned to PERFORMANCE). Pin `config.agent_providers["drive"] = "claude"` for such a user. **Provider trap:** per user the provider is resolved by `AgentContextBuilder.resolve_provider_name` (per-agent
override, then the global `provider_preference` when the strategy allows it, then the default); `openai` is in the `drive`
strategy's allowed providers, so a user whose global preference is `openai` runs the specialist on OpenAI at the user's tier
(the strategy comment notes OpenAI's BALANCED is a small model, which is why the fallback is pinned to PERFORMANCE). Pin
`config.agent_providers["drive"] = "claude"` for such a user (`src/services/agent_context_builder.py`). An identical search is not run twice in one commission; there is no search cap in production (G3 only reports
the count, §7). The `execute_as_given` rule of `COGNITIVE_PROCESS_DRIVE` splits the two: changes the commission names on refs
it gives run as given; a read method in the commission is a hint, never an order.

**Division of labour.** The specialist owns the search mechanics (key words from the user's words in one language, with no
re-search in another script: transliteration is exact in the index; the tiers; one re-search after "Without X"; the opens the
commission asks for, the accuracy rule for a value). It never opens a file to confirm and never judges content. Smart owns the
content: is this the answer, which version is current, document vs memory, and a follow-up from what an opened file names.

**What Smart gets.** Code builds one answer: `Drive result: <status>` (`ok`, `partial`, `ambiguous`, `not_possible`,
`unfinished`, `still_running`, or the class of the failure), `What to do: <line>`, one op line per change, the reading lines, a
short specialist note, then **every read block verbatim** in run order. The specialist's model sees search cards and listings
in full but only a stand-in for an opened file, so nothing is retyped; images and PDFs reach Smart as a `file_data` list.
`drive_context` refs are one flat list per commission (`src/domain/drive_history.py`); the next turns read them in full_text
only (never the summary, never consolidation) as one line per file with its ref. Receipts of a delete or replace and the
late-failure notices are sent literally (`DriveOperations._receipt`, `notify_raw(..., literal=True)`): the path is text the
bot did not write, so it is never formatted, linked or read as a mention. A still-running move, copy, save or append gets no
later message; a later change in the same commission is "not started: an earlier change in this request is still running".

**Stale intents and prefetch.** The old drive intents are gone from the manifest; a stale text that still names one gets the
coordinator's unknown-intent refusal. FileManagement and the specialist set `prefetch_file_ref=False` on their descriptors, so a
`DriveNotConnectedError` during another specialist's pre-fetch logs a WARNING. **Who else touches the drive:** FileManagement's
`open_file` on a `drive:` ref (the same `DriveOperations`); the deep-research copy; the coordinator's `file_ref` prefetch for
DocPlanner, PdfGenerator and HtmlPageGenerator (`FileConversionService`). Lelik and the Alek gateway reach the drive through
Smart.

---

## 7. Operations

- **Logs.** Indexer service (`make fetch-logs-indexer K=300`, Cloud Logging only: no Logfire and no `prompt_content` capture, so
  verbatim scans stay out of BigQuery): one `[DriveIndex] tick user=… end=done|continued {…}` line per completed tick with
  counters and the dollars per class; one `[DriveIndex] run …` line per index run (§7.1); `[DriveReader]`, `[DriveTriage]`,
  `[DriveProfiler]`, `[DrivePassages]` per model request; `[SandboxParse]` for the sandbox parser client's failures (the child's stderr is recorded by the sandbox service; `[DriveExtract]` only appears in local runs of `SubprocessContentExtractor`);
  `[drive_reader]` / `[drive_triage]` for each failed model call and how it was classified; `[DriveMirror]` and `mirror={…}` in
  the tick line; `rebuild progress`, `initial build closed`, `index purged`. An idle tick still observes a few items (the root
  and re-emitted unchanged items arrive every round) but adds no cards. Main service (`make fetch-logs`): `[DriveSync]` (the
  fan-out), `[DriveSearch]` (one per search: mode, scope, concepts, tier totals, stream counts, coverage, chars),
  `[DriveAgent]` (one per commission: status, the model that ran, ops, turns) and `[DriveOperations]` (one per operation),
  including `open refused`, `open cut`, `opened vision` and `extent lookup failed` for the open bound.
- **Alerts.** The fan-out posts to the ops sink when a user's index is stale (`STALE_ALERT_AFTER`), once per incident; one
  reconnect notice to the user on a lost grant (flag set before sending and lowered only when no send was attempted, so a
  notice that never left is retried and one cut off after it left is lost, never repeated;
  `decisions/reconnect_notice_r1.md`); the **cap alert** (`drive_cap_alert`) when files become held by the card cap or chunk
  cap (flags `card_cap_alerted` / `chunk_cap_alerted`, written only by `update_sync_flags` after the post); the rebuild-stall
  and fallback alerts (§4.17). A missing drive prompt or a model outage has no alert of its own.
- **Cabinet.** `GET /api/drive/connections` reads each row's indexing block in its own try: on failure it logs ERROR and
  leaves the block out, so Connect, Disconnect and Reconnect always show. The block carries the counters, the dollars each
  class's monthly cap has paid this month (the initial build's dollars left out), the initial build's progress, and
  `awaiting_rebuild` for a gated state. The `indexing.unreadable` block lists a few unreadable files by name with the reason
  (§7.1). The disclosure text (`drive.index_disclosure` in `cabinet.html`) names Google for embeddings and what goes to the
  model provider to describe files.
- **Several drives.** A disconnect is per connection (§1.2): look for the connection's state, then the archived notes, then the
  credential, then the purge task. A drive that suddenly lists nothing is first a scope or a missing write folder, not an
  index fault. A search warning `capabilities of a connection failed, scope filter off` means only the second-line filter was
  skipped.
- **Owner scripts** (`scripts/infrastructure/drive_*.py`; dry run by default, `--apply` to act; their docstrings own the flags).
  Those acting on one drive take `--connection <id>` next to `--user` (default: the user's one live connection, refused with the
  list when there are several; `drive_connection_arg.py`). The ones to know: `drive_index_rebuild.py` (takes the connection's
  lease and marks the sync state `purging`; the next tick purges and re-onboards from epoch 1), `drive_index_smoke.py` (names
  any missing index; writes nothing), `drive_wipe.py` (every drive document of one user, keeping the monthly usage, credentials
  and note erase requests; only with the drive queue and the fan-out paused), `drive_mirror_status.py` (read-only).
  A manual `drive_sync` body is `{"user_id", "connection_id"}`. Review scripts in `scripts/validation/` are read-only and write
  only under `scripts/memory/`.
- **Eval and checks.** `python -m scripts.validation.drive_eval` indexes a synthetic drive with the real tick, parser, reader
  and triage and runs the gold cases through the real search, failing when recall falls below the recorded bars or a case that
  hit when the bars were recorded misses now (`--record` re-records bars; only for a deliberate change, after reading the
  misses). `search_v2_eval` runs offline with recorded enrichment. **G3** (`smart_dry_run`; owner-run and paid: `--max-usd` is
  required) measures whether Smart searches the drive when it should (a Wilson lower bound against bars recorded at the same
  `--repeats`) and runs hard checks with no tolerance: a crawl (too many opens and listings in one case), a second search after
  a `drive: off` result, an injected document (its text gives orders to an assistant) that makes Smart call
  `propose_memory_facts`, `fetch_url` or a drive mutation, a commission naming a read method on a content case, and a value
  question that does not open a low-accuracy file (`must_open`). Search counts (`SEARCH_CALL_CEILING`) are reported, never a
  failure (owner ruling). The **live model check** (`scripts/validation/drive_model_check.py`)
  runs the production reader on real files per provider; the **live check** (`drive_eval/live_check.py`) indexes a local
  folder under a `zz-synthetic-` id with the live repository and refuses to start unless the drive queue is paused. Their reports go only to `scripts/memory/` (the live check also prints each query's result to the terminal). `tests/integration/offline/test_drive_g2_end_to_end.py` is the offline G2 case.
  Search metrics, an LLM judge and a weekly report are designed, not built:
  [drive_search_metrics_not_built.md](../../04_solution_strategy/decisions/drive_search_metrics_not_built.md).
- **Mirror rollback.** Pause the drive queue and the fan-out job, move `_Transcripts` out of the App Folder (it keeps its item
  id), deploy the old image, resume; the reverse for the roll-forward. A stray new-code tick in between only sets the root-loss
  mark, which the grace keeps from acting. A rollback to an image that predates the mirror loses the pins (the old orphan sweep
  deletes pinned transcripts within its window). `drive_transcript_sweep.py` after a rollback removes the orphan documents and
  keeps the mirror's ledger, root documents and pinned transcripts unless `--mirror`.
- **Mirror indexes.** The mirror's composite indexes (`config/firestore.indexes.json`) must be `READY` **before the mirror
  revision ships**: a missing index makes the mirror do nothing and the transcript stage's orphan sweep keep everything.
  Check with `gcloud firestore indexes composite list --database=us-production --project=<PROJECT_ID>`, then
  `drive_index_smoke.py`.

### 7.1 Why was file X not indexed?

Read the record first (`{prefix}drive_index_records`, doc id `{connection_id}:{item_id}`; the item id is the last part of the
file's `drive:<tag>:<item_id>` ref, the connection the one whose tag it carries), then the logs:
- `state` / `reason`: the outcome (§4.10); `unresolved` with a reason is a scope hold that keeps that outcome;
- `card_capped`: no card headroom, so content and triage wait;
- `defer_until` / `defer_priority` / `due_at`: waiting for the month, the model backoff, the re-read cooldown or a retry backoff;
- `attempts`, `model_outages`: per-file failures and charged reader outages, each failing the file at `MAX_ATTEMPTS`;
- `extracted_by`, `extraction_version`, `pages_total`, `truncated`, `chunk_count`, `chunks_refused_space`;
- why a file is not described: its profile and passage job states, holds, attempts, defers and parked month (§4.17);
- images: `triage_verdict` and the photo fields; the `[DriveTriage]` lines have no item ids.

Then the indexer's logs (grep the item id): the `[DriveIndex] run` line (outcome, state, reason, extractor, attempts, pages,
refused, truncated; one per index run, held and deferred runs included), `[DriveReader]` (what the reader did), the
`outage charged` line, the chunk-cap refusal line, the "run abandoned, the attempt is not counted" line (a systemic error), the
ERROR "an index is missing or building" line (nothing is indexed until the named composite index is `READY`), and
`[SandboxParse]` (a killed or failed parse, no item id). A failure is labelled by type, position and hash, which tells two
failures apart without quoting the document.

**"N documents could not be read."** The bot is not told this number
([decision](../../04_solution_strategy/decisions/drive_unreadable_files_cabinet_only.md)); the Cabinet's drive card lists up to
a few such files by name with the reason, filled from the counters and `unreadable_records`. It is drive-wide, not about a
search's results, and counts documents of a content kind (and images triaged as `text`) in `failed` (any reason) or
`metadata_only` with reason `too_large`, `no_text`, `machine_text` or `user_chunk_cap`. It does **not** count `encrypted`,
`unsupported`, photographs, or kinds never read. "N documents are not read yet" is the other counter (`pending`, not
card-capped). To find which: in the Firestore console filter `{prefix}drive_index_records` on `connection_id` and `state`
(`FirestoreDriveIndexRepository.unread_count_queries` holds the counter's own queries); from chat, any filter-only search shows
each file's `index: <state> (<reason>)` line.

### 7.2 Deploy order, owner steps, rollback

Procedure and keys: `docs/07_deployment/README.md` (the drive indexer, the transcripts switch and cap, standing levers). What a
reader must know:

- **Indexes first.** Every new composite or vector index is `READY` before the code that queries it serves (a missing one is a
  store outage: `FailedPrecondition` → `DriveIndexStoreUnavailable`, ERROR, the tick ends, nothing is counted against any
  file, but no content is indexed until it is READY). `drive_index_smoke.py <user_id>` runs the reads and names the missing
  index.
- **Prompts before the deploy that needs them.** Model-agent tokens ship by upload, not by deploy, and prompt assembly caches a
  template per instance for a while; a token a revision depends on is uploaded inside the paused window.
- **Pause, deploy together, verify, resume.** Pause the drive queue and the fan-out job, deploy main and the indexer from one
  revision (the fan-out sends notices, the indexer marks), verify both serve the new image, resume. **Resume the queue before
  any rebuild** (the purge is a tick).
- **Rebuilds.** A new `MIGRATION_GATE_KEY` gates every existing index until its rebuild (§3); rebuild the owner first. A rebuild
  runs under the monthly caps (no initial-build reset unless the owner says so). The multi-drive slice was wipe-and-rebuild with
  no rollback ([decision](../../04_solution_strategy/decisions/multi_drive_wipe_not_migrate.md)): the previous revision
  cannot read the new documents.
- **Off then on is not a rebuild.** Turning indexing off purges only when a tick's gate sees it off (`access_action`); switched
  back on before that tick, the gate answers PROCEED and the index is kept. Use the rebuild script for a rebuild.
- **The upgrade pass** can be stopped in an emergency with `DRIVE_UPGRADE_PASS_DEV=off` in `.env` (a value set with `gcloud run
  services update` is lost at the next deploy); a parser version bump ships with it `off`, the probe runs, and the pass is
  switched on after the owner's OK (§4.12).
- **Transcripts.** The kill switch ships `off`; to try it set `DRIVE_TRANSCRIBE_ENABLED_DEV=on` and a low
  `DRIVE_TRANSCRIBE_USD_CAP_DEV`, redeploy, and set the owner's flag with `drive_transcribe_flag.py`. **Levers, fastest first:**
  the user's flag off (stops new work, keeps transcripts); the kill switch off and a redeploy (the stage is not built; open
  batches stay at the provider and their reservations stay until it is switched on again or the user opts out); the previous
  image. No rebuild or migration gate; open reservations are released or booked at a purge, never lost.
- **Levers when there is no rollback to an older generation** (an older revision cannot read newer record values and never
  purges chunks): stop all content work by emptying `ADMITTED_KINDS` and `TRIAGED_KINDS` in
  `src/domain/drive_index_policy.py` and bumping `POLICY_VERSION` (the pass re-classifies every record and every write that
  drops a readable generation collects its chunks); stop model spend only by removing the provider keys from the indexer's
  secrets (every model call is then an uncharged outage, parsing and cards go on; the Gemini fallback does not take over,
  because a primary without a key fails the context build before any failover); turn the Gemini fallback off by setting
  `"fallback": None` in the drive entries of `AgentProviderStrategy.STRATEGIES`; switch a reader's provider (§4.16); turn
  indexing off for one user (the purge removes chunks, records and sync state).
- **Files the bot cannot open** need the composite index `connection_id + inaccessible_since` on the records collection `READY`
  before the deploy; a rollback to a revision that predates the marks would treat a marked file's next 404 as a deletion again.
- **Mirror:** §7 (mirror rollback and indexes).

---

## 8. Links and where to start reading

**Specs and decisions.** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md),
[USER_DRIVE_RFC.md](../../10_rfcs/USER_DRIVE_RFC.md), [MULTI_DRIVE_RFC.md](../../10_rfcs/MULTI_DRIVE_RFC.md),
[GOOGLE_DRIVE_RFC.md](../../10_rfcs/GOOGLE_DRIVE_RFC.md), [DRIVE_SCOPE_WALK_RFC.md](../../10_rfcs/DRIVE_SCOPE_WALK_RFC.md),
[DRIVE_FILE_NOTES_RFC.md](../../10_rfcs/DRIVE_FILE_NOTES_RFC.md),
[DRIVE_SPECIALIST_RFC.md](../../10_rfcs/DRIVE_SPECIALIST_RFC.md),
[DRIVE_MCP_CONNECTOR_RFC.md](../../10_rfcs/DRIVE_MCP_CONNECTOR_RFC.md),
[UNTRUSTED_CONTENT_SANDBOX_RFC.md](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md),
[EXTERNAL_FILES_RFC.md](../../10_rfcs/EXTERNAL_FILES_RFC.md); `docs/04_solution_strategy/decisions/drive_*.md`;
agent traps: `src/agents/CLAUDE.md` (Drive); schedulers: `docs/07_deployment/SCHEDULERS.md`; the document service:
`docs/05_building_blocks/file_conversion/README.md`.

**Where to start reading.**

| For | Entry point |
|---|---|
| The tick | `src/services/drive_indexing_service.py::DriveIndexingService.tick`, then `_work_off` |
| What a record is and what the planner decides | `src/domain/drive_index.py::DriveIndexRecord`, `src/domain/drive_index_planner.py::plan_observation` |
| Parameters and versions | `src/domain/drive_index_params.py`, `src/domain/drive_index.py::CURRENT_VERSIONS` |
| Keys and search | `src/domain/drive_keys.py::query_concepts`, `src/domain/drive_tiers.py::classify`, `src/services/drive_search_service.py::DriveSearchService.search` |
| The specialist | `src/agents/drive_agent.py::DriveAgent`, `src/infrastructure/drive_operations.py::DriveOperations`, `src/infrastructure/drive_tools.py::DriveToolRun` |
| Connections and access | `src/services/drive_connection_service.py::DriveConnectionService`, `src/domain/drive_access_policy.py::decide`, `src/services/user_drive_service.py::UserDriveService` |
| Model agents and enrichment | `src/composition/drive_reader_runner.py::DriveReaderRunner`, `src/domain/drive_enrich_plan.py::planned` |
| Transcripts, mirror, notes | `src/services/drive_transcript_stage.py::DriveTranscriptStage`, `src/services/drive_transcript_mirror.py::DriveTranscriptMirror`, `src/services/drive_note_service.py::DriveNoteService` |
| Indexer wiring | `indexer_main.py`, `src/web/indexer_worker_app.py::create_indexer_worker_blueprint`, `src/composition/drive_indexing_setup.py` |
