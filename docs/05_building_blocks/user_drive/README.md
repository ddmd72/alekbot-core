# User Drive (Building Block)

## 1. Overview

The user's long-term file area: their OneDrive App Folder, behind the provider-neutral `UserDrivePort`.
- **Step 1, live** ([USER_DRIVE_RFC.md](../../10_rfcs/USER_DRIVE_RFC.md)): the bot lists, opens, saves, moves,
  creates folders in, appends to and deletes files through seven `*_drive` intents on FileManagement. The model and
  history see only `drive:<item_id>` refs and `[Drive: …]` labels, never a provider name.
- **Step 2, Slice 1** ([USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md)): an opt-in index
  that finds a file or folder by name (exact, partial, transliterated, misspelled), kind, date, folder, photo facets
  and place.
- **Step 2, Slice 2+4** (RFC §24, one slice by owner decision D6): the index reads what the files say. Documents are
  parsed in a sandboxed child process; scans, screenshots, images with text and PDFs the parser cannot read go to a
  model (Claude Haiku 5.5 or GPT); every image is triaged first, and photographs get a short caption. Smart answers a
  clause, an amount or a date from the documents and searches the drive on its own when the answer may be there.
  One extraction path for chat uploads and `open_file_from_drive` (Slice 3) and audio/video transcripts are not built
  ([decision](../../04_solution_strategy/decisions/drive_audio_video_deferred.md)).

The drive is the source of truth; the index is a disposable mirror. Everything in it can be rebuilt from the drive.

---

## 2. Architecture

```
Cabinet  PUT /api/drive/indexing {"enabled": true|false}
           → UserBotConfig.drive_indexing_enabled + drive_sync task (toggle key)
Cloud Scheduler every 10 min (minutes 3, 13, …) → main /worker start_drive_sync
           → DriveSyncSchedulerService: one drive_sync task per user (tick key), staleness alert, reconnect notice
Step 1 mutation (save, move, delete, …) → UserDriveService → drive_sync task (nudge key, item ids)

Cloud Tasks queue drive-sync-dev (max-concurrent-dispatches=1)
  → alek-bot-indexer-dev /worker (indexer_main.py; ingress internal + OIDC, RFC Δ21)
      → DriveIndexingService.tick(user_id, nudge)
          gate → lease → observe (nudged items, then delta pages) → work off due records → continue
          → UserDrivePort (get_changes / get_change / list_children_changes; download_to_file, get_thumbnail)
          → ContentExtractorPort: SubprocessContentExtractor (parser, child process) → DriveReaderRunner
              (DriveDocumentReaderAgent, only when the §9 router sends a file on)
          → ImageTriagePort: DriveTriageRunner (DriveImageTriageAgent, batches of thumbnails)
          → GeminiEmbeddingAdapter (card and chunk vectors)
          → FirestoreDriveIndexRepository ({prefix}drive_index_records, {prefix}drive_index_chunks,
              {prefix}drive_sync_state)

Smart → delegate_to_specialist(search_files_in_drive) → FileManagementAgent
  → DriveSearchService → FirestoreDriveIndexRepository (card kNN, name keys, chunk kNN, content terms, filters)
```

Deploy units, queue, scheduler job and the two-pass first deploy: [docs/07_deployment/README.md](../../07_deployment/README.md)
→ Drive Indexer Service and [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md).

### 2.1 Ports and adapters

| Port | Adapter(s) | Slice 1 role |
|---|---|---|
| `UserDrivePort` | `OneDriveAdapter` | Step 1 CRUD, plus `get_changes` (delta), `get_change`, paged `list_children_changes(limit, cursor)`, `connection`, `flag_reconnect` |
| `DriveIndexRepository` | `FirestoreDriveIndexRepository`, `InMemoryDriveIndexRepository` | records, chunks, sync state, lease, card and chunk kNN, name keys, content terms, filter queries. The in-memory one runs the tests and the eval |
| `EmbeddingService` | `GeminiEmbeddingAdapter` (`gemini-embedding-2/768`) | card and chunk vectors, query vectors |
| `ContentExtractorPort` (Slice 2+4) | `SubprocessContentExtractor` (`parser`), `DriveReaderRunner` (`reader`) | one downloaded file to text; chained per kind by `EXTRACTOR_CHAINS` |
| `ImageTriagePort` (Slice 2+4) | `DriveTriageRunner` | text or photograph per thumbnail, with a caption |
| `TaskQueue` | `GcpTaskQueue` (second instance) | bound to `drive-sync-dev` and the indexer's `/worker`; built only when `DRIVE_INDEXER_URL` is set |
| `AlertSinkPort` | `SlackWebhookAdapter` (the ops sink) | the fan-out's staleness alert |

No port for the policy, the planner, the card builder or the place lookup: they are pure domain code
(`src/domain/drive_index*.py`, `drive_card.py`, `drive_name_keys.py`, `place_lookup.py`) and decide everything the
tick does, tested without Firestore, Graph or a clock.

---

## 3. The index

- **One record per drive item** (`DriveIndexRecord`, doc id `{user_id}:{item_id}`). It stores the last observation
  (name, parent id, kind, size, raw facets), `scope` and `state`/`reason`, scheduling fields (`due_at`, `priority`),
  `name_tokens` and the card (`card_hash`, `has_card`, plus the `card_vector` field in Firestore). Two flags sit
  beside the structural state and never change it: `card_capped` (no card headroom, RFC Δ23) and `card_refused` (the
  embedding provider refused this card text `MAX_ATTEMPTS` times, RFC Δ24). **No path is stored**: paths are derived
  from the `parent_id` chain at read time, so a folder rename touches no descendant.
- **One sync state per user** (`DriveSyncState`, doc id `user_id`): delta cursor, drive and root ids, epoch and
  phase, lease, `observed_as_of`, `access_lost_at`, `purging`, `systemic_failures`, applied versions and
  `applied_space` (the embedding space the cards are in), counters. The fan-out creates a minimal one for a user with
  indexing on, a working grant and none yet, so staleness covers ticks that never land (RFC Δ28).
- **Lease fence (RFC Δ26).** `save_sync_state` never writes the lease fields, and `release_lease` clears them only
  for the owner. A tick writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and `save_sync_state` /
  `delete_sync_state` apply only while it still holds the stored lease (`LeaseLost` otherwise: a quiet stop, INFO).
- A subtree that must be listed again (a folder leaving an opaque root, or restored after a delete) is a flag on the
  folder's own record (`reobserve_pending`, `reobserve_cursor`), worked page by page in class 2 (RFC Δ18).
- **Policy (§5).** Slice 1 admits no content kind: every file is `metadata_only` (reason `kind_not_admitted`, or a
  never-download rule) with a card. Dot-names, secret-shaped names (`id_rsa*`, `*.kdbx`, a non-Keynote `*.key`, …) and
  remote/package/malware items keep a card with their reason. OS and lock artefacts (`~$*`, `.~lock.*#`, `Thumbs.db`,
  `desktop.ini`, `.DS_Store`, `*.tmp`) keep a record but get no card and no name keys (`os_artifact`, RFC Δ32). A
  dot-folder, a folder with a `.git` or `pyvenv.cfg` child, a `node_modules`, `__pycache__` or `site-packages` folder
  anywhere (RFC Δ16), or a dense tree (more than 500 children, mostly neither document nor media, judged from a probe
  of the first 200 and kept until the child count doubles or halves) becomes one `opaque_root` card. The files inside
  get no record; its subfolders and the marker are kept without a card, so a deleted `.git` re-opens the folder. A
  repository whose files arrive in rounds that complete before its `.git` is carded file by file, then uncarded by the
  rescope ([decisions/drive_repo_uploaded_across_rounds.md](../../04_solution_strategy/decisions/drive_repo_uploaded_across_rounds.md)).
  Why each rule exists:
  [decisions/drive_problem_content_classes.md](../../04_solution_strategy/decisions/drive_problem_content_classes.md).
- **Card** (`card_text`): `name | kind | key facets`, where the facets are the photo's taken date, camera,
  `place_name`, image dimensions, and video duration and dimensions. Opaque roots read `repository`, `hidden folder`,
  `generated files`, or `generated files (500+ items)` for a dense tree, the count bucketed so a child coming or
  going does not re-embed it (RFC Δ33). The place name is resolved offline from `src/assets/geo/places_v1.tsv`
  (GeoNames): the nearest place within 50 km, unless a place within 15 km weighs at least 3× as much and is in the
  same country or holds the photo inside its own footprint (RFC Δ39;
  [residuals](../../04_solution_strategy/decisions/drive_place_name_residuals.md)). A card is re-embedded only when its
  hash changes.
- **Name keys** (`drive_name_keys.py`): NFC + casefold tokens split on any Unicode punctuation (curly quotes, dashes,
  `%` included), with variant keys for the accent-stripped Latin spelling, the Russian-style and the Ukrainian national
  transliterations, and trigrams (RFC Δ38). `resume` finds `Résumé.pdf`, `kyiv` finds `Київ.jpg`.
- **Versions** (`CURRENT_VERSIONS`: policy, card, geo table, name keys, and from Slice 2+4 extraction and triage) and
  the embedding space are stamped on each
  record; when one changes, the upgrade pass re-plans stale records in class 3 after the due work, and the space
  change re-embeds cards while search answers from names and filters only (RFC Δ25, Δ30, Δ31).

---

## 3a. Content (Slice 2+4)

Design: RFC §24 (it overrides older RFC text where they disagree). Decisions: the model extractor and its two agents
([decision](../../04_solution_strategy/decisions/drive_model_extractor_agents.md)), model outages
([decision](../../04_solution_strategy/decisions/drive_model_outage_defers_model_work.md)), the document date
([decision](../../04_solution_strategy/decisions/drive_document_date_effective_date.md)).

- **Admission.** `POLICY_VERSION` 2 admits the §5.1 document kinds with a named extractor (PDF, DOCX, PPTX, XLSX/XLS,
  ODF, EPUB, HTML, Markdown, text, CSV, JSON, RTF, MSG, EML, VTT/SRT); `doc`, `ppt`, `mhtml` and iWork stay
  metadata-only, and so do audio and video. Every image enters triage first. The upgrade pass makes the Slice 1
  records due in class 3, bounded by the maintenance share.
- **Extraction.** A due record is downloaded (streamed, capped by kind) and read by the parser in a child process with
  memory, CPU and wall-clock limits. The §9 router sends a PDF on to the reader when the parser found no text, too
  little text per page, garbage text (e.g. `(cid:N)` from a font without a Unicode map) or failed; a timeout or a
  memory kill is a bomb and is never sent on. The reader gets the PDF in page windows of `MODEL_PAGES_PER_CALL` and
  returns verbatim text; the first window also returns the brief (type, one line, the document's date, parties,
  amount). The brief and the lead text go into the card; the brief's date becomes `effective_date` (D13).
- **Images.** Triage reads provider thumbnails in batches of `TRIAGE_BATCH`, with the camera facet as a prior: `text`
  sends the image to the reader (as the provider's large rendition), `photo` makes it `metadata_only / photograph`
  with a caption of at most ten words; no thumbnail is a photograph without a caption. A folder of holiday photos
  costs ⌈n / `TRIAGE_BATCH`⌉ triage calls and no reader call.
- **Chunks.** Text is chunked (`drive_chunker.py`) into `{prefix}drive_index_chunks`, keyed by the record's
  generation: search returns a chunk only when its generation is the record's `indexed_generation` (the read fence,
  §7.4). Each chunk carries `content_terms` (`drive_content_terms.py`): amounts in one canonical form, digit runs and
  ids without separators, dates in ISO form, so `1 250,00 €` in a scan and `1250` in a question meet.
- **Budgets and failures.** Per user and calendar month: `MODEL_EXTRACT_PAGES_PER_MONTH` pages to the reader and
  `TRIAGE_IMAGES_PER_MONTH` images to triage; over either, the work waits as `pending` for the next month. A model
  outage (429, 5xx, timeout, a missing prompt) sets `model_backoff_until` and defers model work only. Calls are
  billed to the user's account; the tick summary carries `model_pages`, `model_calls`, `triage_images`, `model_cost`.

---

## 4. The tick

`DriveIndexingService.tick(user_id, nudge)` runs only on the indexer service, one user at a time.
1. **Gate**, from Firestore, never from the agent cache: indexing off with anything stored → purge; indexing off with
   nothing stored → `disabled`, nothing read or written; no credentials or `needs_reconnect` → stamp `access_lost_at`
   once and stop; past 30 days → purge; a different `drive_id` → purge. The delta cursor carries the drive id and App
   Folder id it came from: a mismatch with a fresh App Folder read restarts the enumeration before the old cursor is
   followed, and a page from another drive purges and re-onboards. A 404 on a stored cursor restarts the enumeration
   like a 410 (RFC Δ40).
2. **Lease** in a transaction. A live lease answers `busy`: a nudge re-enqueues itself under a new key, any other
   tick comes back once per holder after 60 s (`{user}:cont:busy-{holder}`, RFC Δ27).
3. **Observe**: items named in a nudge first (`get_change`, their cards due in class 0), then delta pages until the
   round completes or the 8-minute start budget is spent. A full enumeration (onboarding, an expired cursor) opens an
   epoch; the sweep at its end deletes what it did not see.
4. **Work off** due records after a complete round, by class: 0 edits and nudges, 1 cascades (folder deletes,
   rescopes, probes, parent resolution, card drops), 2 first-seen cards and subtree re-listings, 3 maintenance
   (version upgrades, card-cap release), 4 retries. When one class runs out of budget, lower classes wait (RFC Δ19).
   A deleting folder whose children are all deleting folders outside the page steps aside one second behind them.
5. **Upgrade** (only if no class stopped on its budget): one bounded pass over the records when the versions or the
   embedding space changed; a pass that left stale records waits for due work before verifying again (RFC Δ30).
6. **Continue**: release the lease, then enqueue a continuation if anything is left (RFC Δ27).

Purge is keyed on facts only, never on an exception: a rejected refresh (`invalid_grant`, `interaction_required`,
`consent_required`, RFC Δ41) sets `needs_reconnect` and stops. Errors are classified by what the call is about (§8.2):
- **Per item.** A probe, a parent lookup or a re-listing page that fails defers that one item (retried with
  backoff); a successful step resets the item's attempts. After `MAX_ATTEMPTS` a probe or a re-listing is settled
  without it (not dense; the subtree not re-listed). A parent lookup that keeps failing transiently keeps the record
  `unresolved` and retries every 6 h; only a definite not-found removes it as an orphan (a folder with its stored
  subtree, through the cascade; RFC Δ29). A failed nudge read is left to the delta feed. Item calls share one budget
  (`ITEM_CALLS_PER_TICK` = 20, RFC Δ9) and also stop once the tick's time is spent. A change that fails inside a page
  is skipped and logged at ERROR, and the page still applies.
- **Per record.** A card the embedding provider refuses (HTTP 400) backs off alone; after `MAX_ATTEMPTS` it is
  `card_refused`, with no card until its text changes or the card builder is bumped (RFC Δ24).
- **Systemic.** The delta feed, the token endpoint (30 s timeout, RFC Δ42), any other embedding error (30 s HTTP
  timeout) and the repository end the tick, after the card batch's finished vectors and refusals are committed. A
  throttled drive waits the provider's Retry-After the first time (60 s when absent or for a failed token refresh),
  then `max(Retry-After, backoff)`; anything else backs off `min(60·2^n, 1800)` s. The error is written onto the
  state as last saved, so progress saved page by page is kept.

Indexing turned off mid-tick drops the in-flight result and enqueues a continuation, whose gate purges.
Opt-in and access loss: [decisions/drive_index_opt_in_access_loss_grace.md](../../04_solution_strategy/decisions/drive_index_opt_in_access_loss_grace.md).
Lease, queue and service: [decisions/drive_indexer_lease_queue_service.md](../../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md).

---

## 5. Search

`search_files_in_drive` (SYNC, FileManagement; context fields `drive_query`, `drive_folder`, `drive_kind`,
`drive_date_from`, `drive_date_to`) calls `DriveSearchService.search`. Search reads only `drive_query`, never the
delegate's `query`; the field takes name words, a place or subject, or what to find inside the documents (a phrase, a
number, an amount), no type words or date filters.
- Step 0 reads the opt-in flag, sync state and connection uncached: `disabled`, `disconnected` (a different drive
  connected says so and that it is being indexed), or results marked `reconnect_needed`. While a purge runs, a
  connected drive gets no results and `building`.
- With name words: card kNN and a lexical name stream, fused with RRF (k = 60). The name stream gives each whole-token
  key (transliterations and accent-stripped spellings included) and each trigram key its own query, at most
  `NAME_KEY_QUERIES` = 8 of each in query order, plus one query for the remaining keys; they run in parallel, are
  merged by id and cut by the `name_match` floor (one whole token or three trigrams). Firestore returns the first 100
  matches by id, so a shared query would let a common key crowd out the target: `4912` among 1 000 `IMG_…` names, or
  a misspelled `contarto`. A small en/ru/uk set of function words is dropped from the query. After RRF, records whose
  name covers every query token rank first (RFC Δ17). No cosine threshold; `K = 8` and Smart's judgement bound the
  output. Fewer than K results with a full page widens the fetch once, to 1 000: at most 18 parallel queries per
  round (17 name, 1 kNN; a kind family runs one kNN per member kind), 36 with the widening.
- **Content streams (Slice 2+4).** A chunk kNN (fenced, max-pooled to one hit per record, before fusion) and the
  content exact-token stream (one `array-contains` query per maximal query term — ISO date, canonical amount, joined
  id; a term whose first page is full AND spans more than 8 distinct files is not selective and is left out, while a
  full page from few files — an IBAN in every chunk of one contract — still selects) join the card and name streams
  in RRF. A
  content hit's card shows a window: the best chunk and its neighbours. The envelope says `content_search` and counts
  `unread` documents (admitted kinds, and images triaged as text, whose content is not searchable).
- **Capped hedge (RFC Δ37).** When a stream page was full and the kind, date or folder filters dropped candidates,
  the answer says the filters saw only the best N matches; with no survivor it never says "No matching files" but
  how to search all files.
- **No name words** (empty or punctuation only) with a filter is filter-only: a plain query on kind and
  `effective_date`, newest first, with a count (a lower bound when the fetch hit its cap). With no filter either the
  request is refused with a correction (RFC Δ36). `drive_kind` takes a format (`pdf`, `xlsx`, …), an extension, a
  plural, or a family: `spreadsheet`, `document` (incl. PDF), `word`, `presentation`, `photo` (RFC Δ20); an unknown
  value is refused. Date-only bounds are UTC days ([decision](../../04_solution_strategy/decisions/drive_date_filters_utc_days.md)).
  While building, no "0 match" count is given.
- **Embedding space (RFC Δ25).** While the cards are in another space than the query's, the card stream is skipped
  and the envelope says `refreshing`: names and filters only.
- `building` is true while there is no root yet, a purge runs, an epoch is not idle, a first-seen or re-listing record
  (class 2) is due, or an unresolved record is being resolved at class 1. Parked or 6-hour-retry records do not count.
- A folder filter is a subtree, resolved by ref or by path; several matching folders come back as candidates, never
  guessed (a single suffix match from a full name page is refused, a single exact path is used). A folder that hides
  its contents (a repository, generated files, a dense or deleting folder, or a path through one) answers that its
  contents are not indexed and points to `list_files_in_drive`. A subtree of up to 1 000 records is ranked in memory,
  a larger one is a post-filter.
- Duplicates (same `quickXorHash` and size, files with content only) collapse into one card with `also_at` paths.
- Each card comes back inside its own nonce-tagged `<drive_document>` wrapper, after a coverage envelope (`building`,
  `refreshing`, `index_as_of`, `reconnect_needed`, `index_full`). The nonce is the defence: only a wrapper-tag
  look-alike in the text loses its `<`, attributes escape `" < >`, and `R&D/Tom's plan.docx` reads as written (RFC
  Δ34). The same wrapper carries `open_file_from_drive` text; a drive file prefetched for DocPlanner, PdfGenerator or
  HtmlPageGenerator gets it in Slice 3 ([decision](../../04_solution_strategy/decisions/drive_prefetch_wrapper_slice3.md)).
- One `[DriveSearch]` line per search, with the counts after each stage and `ms=`.

The drive is not part of `SearchEnrichmentService`:
[decisions/drive_index_not_in_search_enrichment.md](../../04_solution_strategy/decisions/drive_index_not_in_search_enrichment.md).

---

## 6. Operations

- **Logs.** On the indexer service, one `[DriveIndex] tick user=… end=done|continued {…} duration=…` line per tick
  that completes, with counts such as `observed`, `pages`, `skipped`, `cards`, `swept`, `deleted`, `rescoped`,
  `upgraded`, `released`; a throttled or failed tick logs its own WARNING or ERROR line instead, a tick that lost its
  lease an INFO line, and a purge logs `index purged`. An idle tick still observes a few items, since the root and
  re-emitted unchanged items arrive in every delta round (S0a), but it adds no `cards`. A skipped item logs one ERROR
  line. The indexer has no Logfire telemetry: Cloud Logging only. Read it with
  `make fetch-logs-indexer K=300 && grep '\[DriveIndex\]' alek_debug_indexer.log`; `make fetch-logs` reads the main
  service, which holds the `[DriveSync]` fan-out and `[DriveSearch]` search lines.
- **Alerts.** The fan-out posts to the ops sink when a user's index is stale (no clean tick since the last success or
  since the sync state was created, or an overdue edit, for `STALE_ALERT_AFTER` = 2 h), once per incident. On a lost
  grant it posts one reconnect notice to the user: the flag is set before sending and lowered only when no send was
  attempted (`NotifyResult.sent`), so the next fan-out retries a notice that never left, and one cut off after it left
  is lost, never repeated.
- **Cabinet.** `/api/drive/status` reads the indexing block in its own try: on failure it logs ERROR and leaves the
  block out, so Connect, Disconnect and Reconnect always show. The block carries `available` (a drive queue is
  wired; the switch is hidden when false unless search is on), `access_lost_at` and `index_kept` (the "kept about 30
  days" hint shows only while an index is kept), and the card lists every non-zero counter (RFC Δ44). From Slice 2+4
  it also returns `readable_chunks` and this month's `model_pages_month` / `triage_images_month` with their limits;
  the disclosure names the model providers (Anthropic or OpenAI) that read scans, images and hard PDFs.
- **Eval.** `venv/bin/python -m scripts.validation.drive_eval` indexes a synthetic drive
  (`tests/fixtures/drive_eval/tree.json`, with file bytes in `files/` made by `make_fixtures.py`) with the real tick,
  real embeddings (`GEMINI_API_KEY` from `.env`) and the real parser, reader and triage (`content_wiring.py`: model
  keys and the Firestore prompts; `DRIVE_EVAL_PROVIDER=openai` for the GPT leg), runs the gold cases (`gold_g1.json`
  finds a file, `gold_g2.json` answers from content: the expected substring must be in the card's window) through the
  real search, and fails when recall@8 falls below the recorded bars or when any case that hit when the bars were
  recorded misses now (`bars.json`, written by the first owner-reviewed run; it pins the tree, the gold and every
  served byte). Scopes: per case type, `G1`, `G2` and `ocr`. **G3**
  (`python -m scripts.validation.drive_eval.smart_dry_run`, owner-run) runs the real Smart on `gold_g3.json` with the
  drive reads answered from the fixture index and measures how often it searches the drive: barred on positives,
  reported on negatives (chit-chat, memory, web, general). A case may set `min_hits` (a trip's photos) or `known_hard` (reported, in no bar); `semantic` cases reach their
  file only through the card stream and miss offline with fake embeddings (RFC Δ45). `--record` re-records the bars;
  do that only for a deliberate change, and review the misses it prints first.
- **Rebuild.** `scripts/infrastructure/drive_index_rebuild.py --user <user_id>` (dry run) / `--apply`: takes the
  user's lease (refused while a tick runs: retry in a few minutes), marks the sync state `purging` and releases it;
  the next tick (the fan-out's within 10 minutes, or a queued continuation) purges the index and re-onboards from
  epoch 1. Project and credentials from the environment / ADC, collections from `APP_ENV`. The purge shows as the
  indexer's `index purged` line. Turning indexing off and on again also rebuilds, but a quick off/on can find the
  index still there and keep it.
- **Smoke.** `scripts/infrastructure/drive_index_smoke.py <user_id>` runs every repository read once against live
  Firestore and names any missing index; it writes nothing.
- **Live check** (before a deploy that changes extraction or search). `python -m scripts.validation.drive_eval.live_check
  --folder scripts/memory/drive_live --user zz-synthetic-<date> --query …` indexes a local folder of real files with
  the real tick, parser, reader, triage and the live Firestore repository under the synthetic id, prints each query's
  result as Smart reads it, then turns indexing off so the production purge removes everything, and fails if anything
  is left. It refuses any id that does not start with `zz-synthetic-`, and refuses to start unless the drive queue
  (`drive-sync-dev`) is PAUSED: the owner pauses it before and resumes it after (the fan-out would otherwise tick the
  synthetic user live); the script prints the resume command when it ends.

---

## 7. Code references

| Piece | File |
|---|---|
| Domain model, parameters | `src/domain/drive_index.py`, `src/domain/drive_index_params.py` |
| Extraction contract, router, content terms, chunker, text gate (Slice 2+4) | `src/domain/drive_extraction.py`, `drive_content_terms.py`, `drive_chunker.py`, `drive_text_gate.py`, `drive_sniff.py` |
| Content ports, parser, model agents and runners (Slice 2+4) | `src/ports/content_extractor_port.py`, `image_triage_port.py`; `src/adapters/subprocess_content_extractor.py`; `src/agents/drive_document_reader_agent.py`, `drive_image_triage_agent.py`; `src/composition/drive_reader_runner.py`, `drive_triage_runner.py` |
| Smart's drive guidance | `prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy` (`drive_specialist`) |
| Policy, planner, card, name keys, task keys | `src/domain/drive_index_policy.py`, `drive_index_planner.py`, `drive_card.py`, `drive_name_keys.py`, `drive_task_keys.py` |
| Place lookup | `src/domain/place_lookup.py`, `src/adapters/geo_place_table.py`, `src/assets/geo/` |
| Search result, wrapper; Cabinet status | `src/domain/drive_search.py`, `src/domain/drive_indexing_status.py` |
| Ports | `src/ports/user_drive_port.py`, `src/ports/drive_index_repository.py` |
| Adapters | `src/adapters/microsoft/onedrive_adapter.py`, `src/adapters/firestore_drive_index_repo.py`, `src/adapters/in_memory_drive_index_repo.py` |
| Services | `src/services/drive_indexing_service.py`, `drive_search_service.py`, `drive_sync_scheduler_service.py`, `user_drive_service.py` |
| Wiring, indexer routes | `src/composition/drive_indexing_setup.py`, `indexer_main.py`, `src/web/indexer_worker_app.py` |
| Agent, manifest | `src/agents/file_management_agent.py`, `src/infrastructure/agent_manifest.py` |
| Eval | `scripts/validation/drive_eval/`, `tests/fixtures/drive_eval/` |
| Ops scripts | `scripts/infrastructure/drive_index_rebuild.py`, `drive_index_smoke.py`, `create_drive_vector_indexes.sh` |
