# User Drive (Building Block)

## 0. How to answer questions about the drive index

Find the kind of question, then read the section named. The code paths in each section are the ground truth; the
RFC ([USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md)) holds the reasoning, §23 the Slice 1
deltas and §24 the Slice 2+4 design.

| Question | Read |
|---|---|
| What is in the index, what a record or a card holds, why a folder is one card | §3 |
| Why a file was or was not read; what happens to it from the moment it lands on the drive | §4 (walk-through, steps 1–9) |
| Why a scan, a screenshot or a PDF went to the model, and what the model returns | §4 steps 4–6 |
| Why a photo has no caption, or an image was or was not read | §4 step 5 |
| What a record's `state` / `reason` means, which outcomes retry and which are final | §4.10 (outcome table) |
| Why a file is waiting: quiet period, monthly budget, model outage, tick budget | §4 step 2, §4.11 |
| A new parser, reader, prompt rule or embedding model: what gets redone | §4.12 (upgrade pass) |
| How the tick schedules its work, leases, errors and continuations | §5 |
| How a search ranks; why a passage is shown or shortened; why a number did or did not match | §6, §6.1 |
| What Smart reads and how it is told to use it; dates in queries vs date filters | §6.2 |
| How quality is measured (G1, G2, OCR, G3), the live checks, deploy steps, logs, indexes | §7 |
| Where a piece of code lives | §8 |

---

## 1. Overview

The user's long-term file area: their OneDrive App Folder, behind the provider-neutral `UserDrivePort`.
- **Step 1, live** ([USER_DRIVE_RFC.md](../../10_rfcs/USER_DRIVE_RFC.md)): the bot lists, opens, saves, moves,
  creates folders in, appends to and deletes files through seven `*_drive` intents on FileManagement. The model and
  history see only `drive:<item_id>` refs and `[Drive: …]` labels, never a provider name.
- **Step 2, Slice 1, live** ([USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md)): an opt-in
  index that finds a file or folder by name (exact, partial, transliterated, misspelled), kind, date, folder, photo
  facets and place.
- **Step 2, Slice 2+4, built, not live until its deploy** (RFC §24, one slice by owner decision D6): the index reads
  what the files say. Documents are parsed in a sandboxed child process; scans, screenshots, images with text and PDFs
  the parser cannot read go to a model (Claude Haiku 5.5 or GPT); every image is triaged first, and photographs get a
  short caption. Smart answers a clause, an amount or a date from the documents and searches the drive on its own when
  the answer may be there. One extraction path for chat uploads and `open_file_from_drive` (Slice 3) and audio/video
  transcripts are not built ([decision](../../04_solution_strategy/decisions/drive_audio_video_deferred.md)).

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

| Port | Adapter(s) | Role |
|---|---|---|
| `UserDrivePort` | `OneDriveAdapter` | Step 1 CRUD, plus `get_changes` (delta), `get_change`, paged `list_children_changes(limit, cursor)`, `connection`, `flag_reconnect`; from Slice 2+4 `download_to_file` (streamed, cut at a byte cap) and `get_thumbnail` (a provider rendition, never decoded by us) |
| `DriveIndexRepository` | `FirestoreDriveIndexRepository`, `InMemoryDriveIndexRepository` | records, chunks, sync state, lease, card and chunk kNN, name keys, content terms, filter queries. The in-memory one runs the tests and the eval |
| `EmbeddingService` | `GeminiEmbeddingAdapter` (`gemini-embedding-2/768`) | card and chunk vectors, query vectors |
| `ContentExtractorPort` (Slice 2+4) | `SubprocessContentExtractor` (`parser`), `DriveReaderRunner` (`reader`) | one downloaded file to text; chained per kind by `EXTRACTOR_CHAINS` |
| `ImageTriagePort` (Slice 2+4) | `DriveTriageRunner` | text or photograph per thumbnail, with a caption |
| `TaskQueue` | `GcpTaskQueue` (second instance) | bound to `drive-sync-dev` and the indexer's `/worker`; built only when `DRIVE_INDEXER_URL` is set |
| `AlertSinkPort` | `SlackWebhookAdapter` (the ops sink) | the fan-out's staleness alert |

No port for the policy, the planner, the card builder, the run rules, the text gate, the chunker, the content terms or
the place lookup: they are pure domain code (`src/domain/drive_*.py`, `place_lookup.py`) and decide everything the
tick and the search do, tested without Firestore, Graph, a model or a clock. The two model agents are reached through
runners in `src/composition/` because services may not import agents.

---

## 3. The index

- **One record per drive item** (`DriveIndexRecord`, doc id `{user_id}:{item_id}`). It stores the last observation
  (name, parent id, kind, size, raw facets), `scope` and `state`/`reason`, scheduling fields (`due_at`, `priority`),
  `name_tokens` and the card (`card_hash`, `has_card`, plus the `card_vector` field in Firestore). Two flags sit
  beside the structural state and never change it: `card_capped` (no card headroom, RFC Δ23) and `card_refused` (the
  embedding provider refused this card text `MAX_ATTEMPTS` times, RFC Δ24). **No path is stored**: paths are derived
  from the `parent_id` chain at read time, so a folder rename touches no descendant.
- **Content fields (Slice 2+4)** on the same record: `indexed_generation` (the readable chunk generation, or none),
  `index_generation` (the last generation a run started), `indexed_version` (the content version it was read at),
  `chunk_count`, `chunk_space` (the embedding space of its chunks), `extraction_version`, `extracted_by`
  (`parser@1` / `reader@1`), `truncated`, `text_chars`, `pages_total`, `lead_text` (the first 2 000 characters),
  `brief` (from the reader), the triage `triage_verdict` / `triage_version` / `triaged_content_version` and `caption`,
  and the run bookkeeping `attempts`, `attempt_started_at`, `dirty_since`, `defer_until` / `defer_priority`.
- **One chunk document per chunk** (`DriveChunk`, `{prefix}drive_index_chunks`, doc id
  `{user_id}:{item_id}:{generation}:{index}`): `text`, `char_start`/`char_end` in the extracted text, `kind`,
  `content_terms`, a 768-dim `vector`. The text and the vector are exempt from single-field indexing.
- **One sync state per user** (`DriveSyncState`, doc id `user_id`): delta cursor, drive and root ids, epoch and
  phase, lease, `observed_as_of`, `access_lost_at`, `purging`, `systemic_failures`, applied versions and
  `applied_space` (the embedding space the cards are in), counters. From Slice 2+4 also the model fields:
  `usage_month`, `model_pages_used`, `triage_images_used`, `model_backoff_until`, `model_failures`. The fan-out creates
  a minimal one for a user with indexing on, a working grant and none yet, so staleness covers ticks that never land
  (RFC Δ28).
- **Lease fence (RFC Δ26).** `save_sync_state` never writes the lease fields, and `release_lease` clears them only
  for the owner. A tick writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and `save_sync_state` /
  `delete_sync_state` apply only while it still holds the stored lease (`LeaseLost` otherwise: a quiet stop, INFO).
- A subtree that must be listed again (a folder leaving an opaque root, or restored after a delete) is a flag on the
  folder's own record (`reobserve_pending`, `reobserve_cursor`), worked page by page in class 2 (RFC Δ18).
- **Policy (§5).** Since `POLICY_VERSION` 2 the §5.1 document kinds are admitted for content (`admission()` →
  `CONTENT`): PDF, DOCX, PPTX, XLSX/XLS, ODT/ODS/ODP, EPUB, HTML, Markdown, text, CSV, JSON, RTF, MSG, EML and VTT/SRT
  subtitles. Every image is admitted for triage (`TRIAGE`). Everything else (`doc`, `ppt`, `mhtml`, iWork, audio,
  video, archives, code, …) is `metadata_only` (reason `kind_not_admitted`) with a card. A content kind over its
  download cap (45 MiB; 10 MiB for XLSX/XLS) is `metadata_only / too_large` before any download. Dot-names,
  secret-shaped names (`id_rsa*`, `*.kdbx`, a non-Keynote `*.key`, …) and remote/package/malware items keep a card
  with their reason and are never read. OS and lock artefacts (`~$*`, `.~lock.*#`, `Thumbs.db`, `desktop.ini`,
  `.DS_Store`, `*.tmp`) keep a record but get no card and no name keys (`os_artifact`, RFC Δ32). A dot-folder, a
  folder with a `.git` or `pyvenv.cfg` child, a `node_modules`, `__pycache__` or `site-packages` folder anywhere
  (RFC Δ16), or a dense tree (more than 500 children, mostly neither document nor media, judged from a probe of the
  first 200 and kept until the child count doubles or halves) becomes one `opaque_root` card. The files inside get no
  record; its subfolders and the marker are kept without a card, so a deleted `.git` re-opens the folder. A
  repository root's card carries its README's lead text (read by the parser, never the reader). A repository whose
  files arrive in rounds that complete before its `.git` is carded file by file, then uncarded by the rescope
  ([decisions/drive_repo_uploaded_across_rounds.md](../../04_solution_strategy/decisions/drive_repo_uploaded_across_rounds.md)).
  Why each rule exists:
  [decisions/drive_problem_content_classes.md](../../04_solution_strategy/decisions/drive_problem_content_classes.md).
- **Card** (`card_text`, `CARD_VERSION` 2): `name | kind | brief | caption | key facets | lead text`, each content
  segment only when present. The brief is `doc_type | description | doc_date | parties: … | amount` (reader-read
  documents only); the caption is a photograph's; the key facets are the photo's taken date, camera, `place_name`,
  image dimensions, and video duration and dimensions; the lead text is the first 2 000 characters of an indexed
  document's text (also kept on a `machine_text` demotion). Opaque roots read `repository` (plus the README lead text),
  `hidden folder`, `generated files`, or `generated files (500+ items)` for a dense tree, the count bucketed so a
  child coming or going does not re-embed it (RFC Δ33). The place name is resolved offline from
  `src/assets/geo/places_v1.tsv` (GeoNames): the nearest place within 50 km, unless a place within 15 km weighs at
  least 3× as much and is in the same country or holds the photo inside its own footprint (RFC Δ39;
  [residuals](../../04_solution_strategy/decisions/drive_place_name_residuals.md)). A card is re-embedded only when
  its hash changes.
- **Name keys** (`drive_name_keys.py`): NFC + casefold tokens split on any Unicode punctuation (curly quotes, dashes,
  `%` included), with variant keys for the accent-stripped Latin spelling, the Russian-style and the Ukrainian national
  transliterations, and trigrams (RFC Δ38). `resume` finds `Résumé.pdf`, `kyiv` finds `Київ.jpg`.
- **Versions** (`CURRENT_VERSIONS` in `src/domain/drive_index.py`: `policy` 2, `card` 2, `geo_table` 1, `name_keys`
  1, `extraction` 2, `triage` 1, plus `extractor:parser` and `extractor:reader` from the wired extractors) and the
  embedding space are stamped on each record; when one changes, the upgrade pass redoes exactly the stale part (§4.12).

---

## 4. Content: from a file on the drive to a searchable passage (Slice 2+4)

Design: RFC §24 (it overrides older RFC text where they disagree). Decisions:
[model extractor and its two agents](../../04_solution_strategy/decisions/drive_model_extractor_agents.md),
[model outages](../../04_solution_strategy/decisions/drive_model_outage_defers_model_work.md),
[the document date](../../04_solution_strategy/decisions/drive_document_date_effective_date.md),
[term selectivity](../../04_solution_strategy/decisions/drive_term_selectivity_distinct_files.md),
[the parser sandbox](../../04_solution_strategy/decisions/drive_parser_sandbox_resource_isolation.md),
[the text gate](../../04_solution_strategy/decisions/drive_text_gate_financial_documents.md).

```
 file lands in the App Folder (user upload, or the bot's own save = nudge)
   │  delta page / get_change
   ▼
 1 OBSERVE   plan_observation → classify: admission, size cap, item rules ──► metadata_only + card (no content)
   │         content kind → pending (content_due)        image → pending (triage_due)
   ▼
 2 SCHEDULE  quiet period (QUIET 10 min, MAX_DEFER 60 min; a nudge is due at once) → due_at, class 0-4
   │
   ├─ image ─► 5 TRIAGE  thumbnails (176 px) in batches of 16 → DriveTriageRunner → photo: caption ──► card
   │                                                                   text: content_due ──┐
   ▼                                                                                       ▼
 3 RUN START can_start_file (deadline) → generation_started (persisted before the download)
   ▼
 4 FETCH     download_to_file (45 MiB / 10 MiB cap) or, for an image, the 2048-px rendition → sniff_kind
   ▼
 5 EXTRACT   chain_for(kind): parser (child process) ──model_route?──► reader (10-page windows, 4 in flight)
   │         outcome_step: TEXT │ DEMOTE │ RETRY │ WAIT_MONTH            (budgets and outage checked first)
   ▼
 6 BRIEF     reader's first window → brief → card segments, effective_date (D13)
   ▼
 7 TEXT      text gate (tables exempt) → chunker → chunk cap → embed chunks (≤ 4 in flight, 180 s)
   ▼                                   → content_terms
 8 COMMIT    write chunks under the new generation → read fence → indexed() (one record write) → collect debris
   ▼
 9 CARD      name | kind | brief | caption | facets | lead text → card_vector  (same pass, after the content)
```

Every step below names the function that decides it. The tick runs on the indexer only, one user at a time
(`DriveIndexingService.tick` in `src/services/drive_indexing_service.py`).

### Step 1 — observe and classify

A file arrives in a delta page (or, for the bot's own write, in a nudge read with `get_change`). `plan_observation` →
`classify` (`src/domain/drive_index_planner.py`) decides what the record is:
- not admitted, an item rule (dot-name, secret name, OS artefact, package, …) or inside an opaque subtree → no
  content, ever; a card or not as in §3;
- a content kind over `size_cap(kind)` → `metadata_only / too_large`, never downloaded;
- an image: a stored triage verdict for this content version decides (`valid_verdict`): `photo` → `metadata_only /
  photograph`; `text` → content; none → `pending`, triage due;
- an admitted content kind → `pending`, content due;
- **a content outcome that still stands is kept** (`_outcome_stands`): the same content version read under the
  current `EXTRACTION_VERSION` keeps its `indexed` / `failed` / content-demotion state, so a rename or a move never
  re-reads the file (the card is re-embedded because the name changed).

A new content version of an indexed file makes it `pending`; its old generation keeps serving, marked `stale` in
results, until the new one commits (§7.4 fence). A kind change (`.jpg` renamed to `.pdf`) is another extractor chain:
attempts, backoff and deferrals restart (`_kind_changed`). A photograph's caption lives with its `photo` verdict: a
rename away and back keeps both <!-- r2: T-m3 -->.

### Step 2 — when it runs (quiet period, classes)

`schedule` / `_content_due_at`: an edit of a pending record sets `dirty_since`; its content waits until the file has
been untouched for `QUIET` (10 min) or dirty for `MAX_DEFER` (60 min) — Word for the web saves every 1–3 minutes while
someone types (spike S0b). First sight, an upgrade, a release and a nudge (the bot's own atomic write) are due at once.
A record due for other work (a card, a re-embed) still keeps its content's quiet period (`quiet_over`) <!-- r2: T-M1 -->.

The due loop (`_due_loop` → `_work_class`) works records in class order: 0 edits, 1 cascades, 2 first-seen,
3 maintenance (upgrades, releases, month-held work), 4 retries. Within a record, content and triage run before its
card, so a file is carded once, with its lead text, brief or caption.

### Step 3 — starting a run

`_index_step`:
- a run that started a whole tick deadline ago and never ended is a crashed attempt (`attempt_crashed` →
  `content_retry(extract_error)`);
- an image (its chain starts at the reader) is first checked against the model hold (`_model_hold`: outage backoff,
  pages left this month);
- `_Budget.file_allowed`: at most `TICK_FILE_BUDGET` (50) runs per tick, none after the 8-minute start budget, and
  only if the file's worst case still ends before the 30-minute tick deadline (`can_start_file`: download 120 s +
  parser 120 s + model 600 s if the kind may reach the reader + embeddings 180 s + 60 s margin). A file that does not
  fit stays due for the next tick;
- `generation_started` persists a new `index_generation` and `attempt_started_at` **before** the download, so a retry
  never reuses a crashed run's generation and the sweep can collect its partial chunks.

### Step 4 — fetch and sniff

`_fetch`: a document is streamed to a temp file by `download_to_file`, cut at `size_cap(kind)` (`too_large` if the
stream passes it — metadata can understate a size). An image triaged as text is fetched as the provider's
`c2048x2048` rendition (JPEG/PNG; HEIC is never decoded by us); with no rendition, the original is used only when it
is JPEG/PNG/WebP/GIF within the 3.75 MiB vision cap (`original_fallback`), else the record is `metadata_only /
no_text`. `sniff_kind` (`src/domain/drive_sniff.py`) reads the first 8 KiB: magic bytes win over the name (a `.txt`
that is a ZIP is an archive); a sniffed kind the policy does not admit is `metadata_only / kind_not_admitted`; a file
whose bytes show a kind that may reach the reader, with no time left in this tick for the reader's worst case, waits
one second for the next tick. A gone item (404/410) is re-observed at once instead of being read as "no content"
(`_item_gone`).

### Step 5 — extract: parser first, model second

`_extract` walks `chain_for(kind)` (`EXTRACTOR_CHAINS` in `src/domain/drive_extraction.py`): PDF `(parser, reader)`,
image `(reader)`, every other admitted kind `(parser)`.

- **Parser** (`SubprocessContentExtractor`, `src/adapters/subprocess_content_extractor.py`): a fresh child Python
  process per file (`src/adapters/drive_parser/child.py`) with `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 90 s, `RLIMIT_FSIZE`
  64 MiB, no core dumps, a 120 s wall-clock kill of its process group, an allowlisted environment and a private temp
  directory; one child at a time. Readers (`src/adapters/drive_parser/readers.py`): pdfminer.six for PDF text, laid
  out page by page and stopped once `MAX_TEXT_CHARS` is reached <!-- r2: P-m6 --> (pypdf for the page count and
  encryption);
  markitdown's allowlisted converters for DOCX, PPTX, EPUB, MSG and HTML; openpyxl (XLSX, at most 1 025 columns) and
  xlrd (XLS) as Markdown tables of at most 5 000 rows per sheet, `truncated` when a cell past the column bound holds a
  value <!-- r2: P-m3 -->; charset detection for text kinds; cue text for VTT/SRT; `striprtf`; the stdlib `email`
  parser; ODF `content.xml` through `defusedxml`. A password-protected file is `encrypted`. Text is capped at
  `MAX_TEXT_CHARS` (2 M). The parser libraries are pinned in `requirements.txt`: a changed output for the same file
  is a `SubprocessContentExtractor.VERSION` bump. This is resource isolation, not code-execution containment
  ([decision](../../04_solution_strategy/decisions/drive_parser_sandbox_resource_isolation.md)).
- **Router** (`model_route`): a PDF goes on to the reader when the parser found no text, fewer than
  `MODEL_EXTRACT_DENSITY` (200) characters per page, more than 5 % unreadable characters (`garbage_share`: `(cid:N)`
  runs, replacement characters, UTF-8 read as Latin-1 or CP1251), or failed (`error`, `unsupported`). A parser
  `timeout` or `memory` kill is a bomb and is never sent on; `encrypted` is deterministic and never sent on. Other
  kinds stay with the parser's outcome.
- **Reader** (`DriveReaderRunner`, `src/composition/drive_reader_runner.py`, driving `DriveDocumentReaderAgent`):
  before it is called, the file must fit this month's reader pages (`reader_pages`: an image is 1 page, a PDF its
  page count capped at `MODEL_EXTRACT_MAX_PAGES` = 200, or 200 when the parser could not count) and the model must
  not be in an outage backoff; otherwise the record waits (`deferred`). The runner builds the agent with the user's
  own provider, tier and thinking (`AgentContextBuilder`, D7) and bills every call to the user's account.
  - An image is one call on the rendition.
  - A PDF's pages are counted by the sandboxed child, then read in windows of `MODEL_PAGES_PER_CALL` (10) pages, at
    most `MODEL_IN_FLIGHT` (4) at a time, each window cut by the child only when its turn comes (lazy windows: at most
    four window files on disk, cut from the walked page tree, never from `/Count` <!-- r2: P-M1 -->), joined in page
    order.
  - A window answered at the output limit is halved once (cut from the source); a half or a single page still at
    the limit adds no text and sets `truncated`.
  - A window whose call times out (180 s) is halved once; a half that times out again fails the file (`error`, a
    counted per-file retry); a single-page window that times out is a model outage <!-- r2: T-M2b -->.
  - HTTP 413, 400 or 422 halves a window down to one page; a single page still refused is skipped, the rest of the
    file is read and indexed `truncated`, with `pages_refused` on the outcome and in the log line <!-- r2: R3-P1 -->.
    Only a PDF whose every page was refused, or a refused image, is terminal: `too_large` when a refusal was 413,
    else `unsupported` — both demote, because the same bytes always get the same answer and a retry would only bill
    the read windows again <!-- r2: P-m5 -->. Output that stays invalid after the parse retry is `error`.
  - At `MODEL_FILE_BUDGET_S` (600 s) the windows done are kept and the rest cancelled (`truncated`); a file whose
    every window was cut by the budget and that found no text is `error` (retried), never `no_text`
    <!-- r2: P-m4 -->. Pages past 200 are not sent (`truncated`).
  - An outage in any window raises `DriveModelUnavailable` carrying the pages, calls and cost spent before it.
- **Outcome** (`outcome_step` in `src/domain/drive_index_run.py`): `text` goes on; `no_text`, `unsupported`,
  `too_large`, `encrypted` demote; `timeout`, `memory`, `error` retry; `budget_exhausted` waits for next month. See
  the table in §4.10. `extracted_by` records the link that decided (`parser@1` or `reader@1`), so a new version of
  that link re-reads exactly the files it decided (§4.12).

### Step 5 (images) — triage

`_triage_step` / `_triage_batch`: due images are gathered `TRIAGE_BATCH` (16) per call; a partial last batch waits
for the next page of the same class only when the pass is progressing anyway, so N photos cost ⌈N/16⌉ calls. Each
image's `medium` (176 px) thumbnail goes with an opaque index and the camera prior as text (`photo.takenDateTime` or
`cameraMake` present: a hint, never a verdict). `DriveTriageRunner` returns one result per image: `photo` + a caption
of at most ten words, or `text`. A batch the provider refuses (400) is halved until the refused image is alone; that
image, an image left unanswered after one correction turn, and a thumbnail over the vision cap are photographs
without a caption. Verdicts (`triaged`): `photo` → `metadata_only / photograph` with the caption, carded; `text` →
content due for the reader (steps 3–8), in the class the triage had. No thumbnail yet is retried with backoff
(`no_rendition`); only at `MAX_ATTEMPTS` is it a photograph without a caption. A model outage defers the batch; the
images left over this month's `TRIAGE_IMAGES_PER_MONTH` (20 000) wait in class 3 for the next month.

### Step 6 — the brief and the document date

The reader's first window also returns a brief (`DocumentBrief`): `doc_type` (invoice, receipt, contract, statement,
letter, ticket, certificate, form, other), a one-line `description` in the document's language, `doc_date` (ISO or
none), up to five `parties`, an `amount` (value and currency). Field lengths are bounded, never rewritten. The brief
goes into the card; its `doc_date` becomes the record's `effective_date` (D13), so "the March bill" finds a scan
uploaded in April. Parsed documents get no brief and keep the photo-taken / file date. Brief and caption are model
output from untrusted content and reach Smart only inside the untrusted wrapper.

### Step 7 — text gate, chunks, terms, embeddings

`_read_text`, all CPU work in worker threads:
- **Text gate** (`is_machine_text`, `src/domain/drive_text_gate.py`; tables — CSV, XLSX, XLS, ODS — are exempt): a
  server log, SQL dump or telemetry saved as text is `metadata_only / machine_text` (its card keeps the lead text).
  Machine text is repetition **and** low token variety together, or mostly near-empty lines; a short text is judged
  on its lines without amounts or dates. A bank statement or an invoice repeats a template but keeps variety, and its
  lines carry amounts and dates, so it passes ([decision](../../04_solution_strategy/decisions/drive_text_gate_financial_documents.md)).
- **Chunker** (`chunk_text`, `src/domain/drive_chunker.py`): about `CHUNK_CHARS` (2 400) characters, ending at a
  paragraph, else a sentence or line, else a word; the next chunk repeats up to `CHUNK_OVERLAP` (240) characters of
  trailing sentences; a Markdown table is never overlapped, its continued chunks start with the header row. At most
  `MAX_CHUNKS_PER_FILE` (400) chunks (`truncated` beyond). No chunk → `no_text`.
- **Chunk cap** (`chunk_cap_refuses`): the user's readable chunks may not pass `MAX_CHUNKS_PER_USER` (50 000); a file
  that would is `metadata_only / user_chunk_cap` with its would-be `chunk_count`, released (best card rank, then
  newest) when the headroom holds it, never re-read before.
- **Embeddings**: one `RETRIEVAL_DOCUMENT` vector per chunk, at most `EMBED_IN_FLIGHT` (4) calls at once, within
  `EMBED_FILE_BUDGET_S` (180 s; past it → `embed_timeout`, retried). A 400 on a chunk is this file's (`extract_error`,
  retried); any other embedding error is systemic and ends the tick.
- **Content terms** (`content_terms`, `src/domain/drive_content_terms.py`), at most 256 per chunk: amounts in one
  canonical form (`1 250,00`, `1.250,00`, `1,250.00` → `1250.00`) plus their integer part, numeric dates in ISO form
  (both readings when day and month are ambiguous), ids with separators stripped (`INV-2024/0012` → `inv20240012`)
  plus their digit runs, and whitespace-separated digit groups of 9+ digits joined (an IBAN, a phone number). A token
  holding `.` or `,` never has its digits joined. One normaliser serves the index and the query; a change bumps
  `EXTRACTION_VERSION`.

### Step 8 — commit under the fence

The chunks are written under the run's generation, then the record is read again: the result commits only if the
record still has the content version and generation the run started from (the read fence, §7.4); otherwise the run's
chunks are collected and the result dropped (`dropped`). `indexed()` is the run's one record write:
`indexed_generation` becomes the new generation, with `chunk_space`, `extracted_by`, `truncated`, the lead text and
the brief; then every other generation's chunks are deleted. Search only ever returns a chunk whose generation is its
record's `indexed_generation`, so a crash mid-write, a re-index and a demotion are invisible at once. The sweep at the
end of an epoch also removes chunks with no record or a dead generation.

### Step 9 — the card

Back in the same `_work_class` pass, the record's card is due because its text changed (brief, caption or lead text):
`card_text` → `RETRIEVAL_DOCUMENT` vector → `card_vector`, as in Slice 1.

### 4.10 Outcomes and statuses

| Where | Outcome | Record | Retried? |
|---|---|---|---|
| any run | text, chunks committed | `indexed` | — |
| parser / reader | `no_text` (reader found none, PDF with no pages, no chunk) | `metadata_only / no_text` | no, until the content or the extractor changes |
| parser / reader | `unsupported` (no reader for the kind, a deterministic parser failure, a provider 400/422 refusal of an image or of every page <!-- r2: P-m5, R3-P1 -->) | `metadata_only / kind_not_admitted` | no |
| fetch / parser / reader | `too_large` (download cap, ZIP container bomb, 413 on an image or among refusals of every page <!-- r2: P-m5, R3-P1 -->) | `metadata_only / too_large` | no |
| parser | `encrypted` (user password) | `metadata_only / encrypted` | no |
| sniff | bytes show a kind the policy does not admit | `metadata_only / kind_not_admitted` | no |
| text gate | machine text | `metadata_only / machine_text` | no |
| chunk cap | over `MAX_CHUNKS_PER_USER` | `metadata_only / user_chunk_cap` | released when headroom holds it |
| triage | photograph | `metadata_only / photograph` (+ caption) | no |
| parser | `timeout`, `memory` (a bomb) | `pending`, backoff 10 min, 20 min, then `failed / extract_timeout` or `extract_memory` | yes, `MAX_ATTEMPTS` 3 |
| parser / reader / run | `error` (parser error, invalid model output, a half that timed out again <!-- r2: T-M2b -->, a budget that cut every window), crashed run, chunk refused (400) | `pending` → `failed / extract_error` | yes, 3 |
| fetch / triage | download or thumbnail failed; no rendition yet | `pending` → `failed / download_error` (a missing rendition ends as a photograph without caption) | yes, 3 |
| embeddings | per-file 180 s passed | `pending` → `failed / embed_timeout` | yes, 3 |
| reader / triage | month's pages or images spent (`budget_exhausted`, or the hold before the call) | `pending`, class 3, due at the UTC month start | waits |
| reader / triage | **model outage**: 429, 5xx, timeout of an unhalved single page, network, open breaker, account-level 4xx, missing prompt, anything unexpected | `pending`, due at `model_backoff_until` (60 s doubling to 1 h); attempts untouched | waits |
| run | **systemic**: drive disconnected or throttled, a rejected refresh, an embedding error other than a chunk's 400, transient Firestore errors <!-- r2: T-m4 -->, a content port not wired | the run is abandoned (attempt not counted, content due again); the tick ends with its §5 backoff | the tick |

A `failed` record keeps its previous readable generation (served as stale). Retry backoff is `5 min · 2^attempts`,
capped at 6 h. A new content version restarts attempts and deferrals. `failed` and content demotions stand until the
content version or the deciding extractor changes.

### 4.11 Budgets and bounds

| Bound | Value | What happens at it |
|---|---|---|
| Runs per tick (`TICK_FILE_BUDGET`) | 50 | the rest wait for the next tick |
| Start budget / tick deadline | 8 min / 30 min | no run starts whose worst case would pass the deadline |
| Per file: download, parser, model, embeddings | 120 s, 120 s, 600 s, 180 s | `too_large`/retry; `timeout` kill; `truncated`; `embed_timeout` |
| Reader pages per user per month | 5 000 | a file waits until a month has the pages it needs; never cut to what is left |
| Triage images per user per month | 20 000 | the rest wait in class 3 for the next month |
| Pages read per file | 200 | `truncated` |
| Concurrent model calls | 4 per file (files go to the reader one at a time) | — |
| Readable chunks per user | 50 000 | `user_chunk_cap` |
| Maintenance share of the start budget | 30 % | upgrades and month-held work yield to edits and first-seen work |

Monthly counters are on the sync state, keyed by UTC month (`usage_now`, `with_usage`); every save of the tick,
error paths included, carries them. Usage spent before an outage is counted without ending it: the backoff keeps
doubling <!-- r2: T-M2a -->. A tick that ends `lease_lost` cannot save its usage (RFC §24.14 known limit).

### 4.12 Upgrades: what a version change redoes

The upgrade pass (`_upgrade_step` / `_upgrade_page`) runs after the due loop, pages through the records in class 3
within the maintenance share, and re-plans each stale record (`stale_versions`, `extractor_stale`):
- `POLICY_VERSION` (admission) → re-classified; Slice 1 records became due this way when Slice 2+4 was turned on.
- `CARD_VERSION`, `NAME_KEYS_VERSION`, `GEO_TABLE_VERSION` → re-carded / re-keyed / re-placed; no content read.
- `EXTRACTION_VERSION` (chunker, text gate, content terms) → every file whose outcome was decided under the old
  version is read again (it stays readable from its old generation meanwhile).
- An extractor's version (`extractor:parser`, `extractor:reader` in the target map) → only the files that link
  decided (`extracted_by`) are re-read (`reextraction_due`).
- `TRIAGE_VERSION` → images are triaged again.
- **Embedding space** → cards are re-embedded (search runs on names, filters and exact terms meanwhile,
  `refreshing`), and readable chunks are re-embedded from their stored text into a new generation with no download and
  no extraction (`_reembed_step`, under the same fence). This applies to any record with readable chunks in the old
  space, whatever its content state (an indexed, failed or still-waiting record), without touching its attempts,
  deferral or quiet period <!-- r2: T-M1 -->. Chunks that cannot be read back as stored send the file to a full run.
- A pass that leaves stale records (backing off, held) does not repeat until due work has run; `applied_versions` and
  `applied_space` are written only after a clean pass.

---

## 5. The tick

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
   epoch; the sweep at its end deletes what it did not see, then sweeps orphan chunks.
4. **Work off** due records after a complete round, by class: 0 edits and nudges, 1 cascades (folder deletes,
   rescopes, probes, parent resolution, card drops), 2 first-seen cards, triage and index runs, and subtree
   re-listings, 3 maintenance (version upgrades, card-cap release, README reads, re-embeds, month-held model work),
   4 retries. When one class runs out of budget, lower classes wait (RFC Δ19). A deleting folder whose children are
   all deleting folders outside the page steps aside one second behind them.
5. **Upgrade** (only if no class stopped on its budget): one bounded pass over the records when the versions or the
   embedding space changed; a pass that left stale records waits for due work before verifying again (RFC Δ30). Then
   the card-cap and chunk-cap releases.
6. **Continue**: release the lease, then enqueue a continuation if anything is left (RFC Δ27).

Purge is keyed on facts only, never on an exception: a rejected refresh (`invalid_grant`, `interaction_required`,
`consent_required`, RFC Δ41) sets `needs_reconnect` and stops. A purge deletes chunks first, then records, then the
sync state. Errors are classified by what the call is about (§8.2):
- **Per item.** A probe, a parent lookup or a re-listing page that fails defers that one item (retried with
  backoff); a successful step resets the item's attempts. After `MAX_ATTEMPTS` a probe or a re-listing is settled
  without it (not dense; the subtree not re-listed). A parent lookup that keeps failing transiently keeps the record
  `unresolved` and retries every 6 h; only a definite not-found removes it as an orphan (a folder with its stored
  subtree, through the cascade; RFC Δ29). A failed nudge read is left to the delta feed. Item calls share one budget
  (`ITEM_CALLS_PER_TICK` = 20, RFC Δ9) and also stop once the tick's time is spent. A change that fails inside a page
  is skipped and logged at ERROR, and the page still applies.
- **Per record.** A card the embedding provider refuses (HTTP 400) backs off alone; after `MAX_ATTEMPTS` it is
  `card_refused`, with no card until its text changes or the card builder is bumped (RFC Δ24). Content outcomes: §4.10.
- **Model outage** (Slice 2+4): defers model work only (§4.10); parsing, cards and cascades go on.
- **Systemic.** The delta feed, the token endpoint (30 s timeout, RFC Δ42), any other embedding error (30 s HTTP
  timeout) and the repository end the tick, after the card batch's finished vectors and refusals are committed. A
  throttled drive waits the provider's Retry-After the first time (60 s when absent or for a failed token refresh),
  then `max(Retry-After, backoff)`; anything else backs off `min(60·2^n, 1800)` s. The error is written onto the
  state as last saved, so progress saved page by page is kept. An index run interrupted by a systemic error is
  abandoned without counting an attempt; one that already committed its outcome keeps it.

Indexing turned off mid-tick drops the in-flight result and enqueues a continuation, whose gate purges.
Opt-in and access loss: [decisions/drive_index_opt_in_access_loss_grace.md](../../04_solution_strategy/decisions/drive_index_opt_in_access_loss_grace.md).
Lease, queue and service: [decisions/drive_indexer_lease_queue_service.md](../../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md).

---

## 6. Search

`search_files_in_drive` (SYNC, FileManagement, zero-LLM; context fields `drive_query`, `drive_folder`, `drive_kind`,
`drive_date_from`, `drive_date_to`) calls `DriveSearchService.search` (`src/services/drive_search_service.py`).
Search reads only `drive_query`, never the delegate's `query`; the field takes name words, a place or subject, or what
to find inside the documents (a phrase, a number, an amount, a date as written in the document), no type words.
- Step 0 reads the opt-in flag, sync state and connection uncached: `disabled`, `disconnected` (a different drive
  connected says so and that it is being indexed), or results marked `reconnect_needed`. While a purge runs, a
  connected drive gets no results and `building`.
- With name words, four streams run in parallel and are fused with RRF (k = 60): **card kNN**, the lexical **name
  stream**, the **chunk kNN** and the **content exact-term stream** (§6.1). The name stream gives each whole-token key
  (transliterations and accent-stripped spellings included) and each trigram key its own query, at most
  `NAME_KEY_QUERIES` = 8 of each in query order, plus one query for the remaining keys; they are merged by id and cut
  by the `name_match` floor (one whole token or three trigrams). Firestore returns the first 100 matches by id, so a
  shared query would let a common key crowd out the target: `4912` among 1 000 `IMG_…` names, or a misspelled
  `contarto`. A small en/ru/uk set of function words is dropped from the query. Each stream is filtered (read-time
  visibility, kind, date, folder) and pooled to one hit per record before RRF. After RRF, records whose name covers
  every query token rank first (RFC Δ17). No cosine threshold; `K = 8` and Smart's judgement bound the output. Fewer
  than K results with any full stream page widens every fetch once, to 1 000.
- **Capped hedge (RFC Δ37).** When a stream page was full and the kind, date or folder filters dropped candidates,
  the answer says the filters saw only the best N matches; with no survivor it never says "No matching files" but
  how to search all files.
- **No name words** (empty or punctuation only) with a filter is filter-only: a plain query on kind and
  `effective_date`, newest first, with a count (a lower bound when the fetch hit its cap). With no filter either the
  request is refused with a correction (RFC Δ36). `drive_kind` takes a format (`pdf`, `xlsx`, …), an extension, a
  plural, or a family: `spreadsheet`, `document` (incl. PDF), `word`, `presentation`, `photo` (RFC Δ20); an unknown
  value is refused. Date-only bounds are UTC days ([decision](../../04_solution_strategy/decisions/drive_date_filters_utc_days.md)).
  While building, no "0 match" count is given.
- **Embedding space (RFC Δ25).** While the index's vectors are in another space than the query's (`applied_space`
  not yet the current one), card kNN and chunk kNN are skipped and the envelope says `refreshing`: names, filters and
  exact terms only.
- `building` is true while there is no root yet, a purge runs, an epoch is not idle, a class-2 record (first-seen
  card, triage or index run, or a re-listing) is due, or an unresolved record is being resolved at class 1. Parked
  or 6-hour-retry records do not count.
- A folder filter is a subtree, resolved by ref or by path; several matching folders come back as candidates, never
  guessed (a single suffix match from a full name page is refused, a single exact path is used). A folder that hides
  its contents (a repository, generated files, a dense or deleting folder, or a path through one) answers that its
  contents are not indexed and points to `list_files_in_drive`. A subtree of up to 1 000 records is ranked in memory
  (its chunks too, up to `FOLDER_INMEM_CHUNKS` = 2 000, read by id at each record's readable generation); a larger
  one runs the user-wide streams under a folder post-filter.
- Duplicates (same `quickXorHash` and size, files with content only) collapse into one card with `also_at` paths.
- One `[DriveSearch]` line per search, with the counts after each stage (`card`, `name`, `chunk`, `chunk_kept`,
  `term_queries`, `term_probes` <!-- r2: S-M1 -->, `term`, `term_kept`, `fused`, `covered`, `widened`, `windows`,
  `window_chars`, …) and `ms=`.

The drive is not part of `SearchEnrichmentService`:
[decisions/drive_index_not_in_search_enrichment.md](../../04_solution_strategy/decisions/drive_index_not_in_search_enrichment.md).

### 6.1 The content streams and the passage window

The content streams run while the policy admits any content (`content_admitted`); the envelope then says
`content_search`.
- **Chunk kNN** (`find_nearest_chunks`, `CHUNK_FETCH` = 300, one per member kind): nearest chunks, fenced against the
  records read now (`chunk_readable`) and max-pooled to one hit per record, its nearest chunk (`pool_nearest`).
- **Content exact-term stream.** `query_terms` takes the **maximal form** of each number-like span of `drive_query`
  — an ISO date, a canonical amount, a joined id, a joined digit group — never a fragment (a year, an amount's integer
  part, a run inside an id), because a fragment matches documents that share only that part. A whole amount with
  `.00` cents also queries its integer form, so "1,250.00" finds a document that says "1250" <!-- r2: S-m2 -->. Terms
  are read from at most `QUERY_TERM_CHARS` (1 000) characters of the query, cut at a word boundary
  <!-- r2: S-m1 -->, at most `CONTENT_TERM_QUERIES` (8) of them, longest first. Each runs as its own `array-contains`
  query (`CONTENT_TERM_FETCH` = 100, ordered by item id), never one `array-contains-any`, which would let a common term
  hide a rare one.
- **Selectivity** (`selective_term_hits` / `term_selective`, `src/domain/drive_content_search.py`;
  [decision](../../04_solution_strategy/decisions/drive_term_selectivity_distinct_files.md)). A page that was not full
  is complete and selects. A full page is an id-ordered slice: the term is **non-selective** when it spans more than
  `TERM_NONSELECTIVE_FILES` (8) distinct files. A full page from 8 files or fewer is probed past its last file, one new
  file per probe (`find_next_file_with_term`), until a ninth file is seen or the term runs out; only then is it
  selective <!-- r2: S-M1 -->. A non-selective term takes no part in fusion, anchoring, window trimming or the
  widening. A term concentrated in few files — an IBAN in every chunk of one contract — still selects. The folder
  in-memory path has every chunk, so its page is complete.
- **Pooling** (`pool_terms`): records sharing more query terms first, then the more specific term, then item id; the
  anchor is the record's chunk holding the most terms.
- **Visibility.** A content hit counts only for a record that is carded and visible (§17.1 d): nothing inside an
  opaque subtree or under a deleting folder.
- **The window** (`_with_content`, `budget_windows`). For each content hit, in rank order, the anchor chunk and its
  neighbours `index ± 1` are read by id from stored text (a term anchor wins over a kNN anchor: it holds the number
  asked for). All windows of one result set share `WINDOW_CHARS_TOTAL` (12 000 characters): every hit keeps its best
  chunk — trimmed around its query term only when the best chunks alone do not fit, and then only down to the share
  the shorter ones leave — and neighbours are added in rank order until one does not fit, after which no lower hit
  gets one. Overlapping text between neighbours is cut by offsets; a missing neighbour leaves a `[…]` gap. A trimmed
  or cut window is rendered `matching passage (shortened):`.
- **`matched`**: `card` (card kNN or name), `content` (chunk kNN or terms), or `card+content`.
- **`unread`** in the envelope: documents of an admitted kind, and images triaged as text, whose content is not
  searchable (pending without a readable generation, `failed`, or demoted for `too_large`, `no_text`,
  `machine_text`, `user_chunk_cap`), counted by the tick when it settles.

### 6.2 What Smart sees

`render_results` (`src/domain/drive_search.py`) gives Smart per-result facts only: a reference reminder, the envelope
lines (index time, `building`, `refreshing`, "only names were searched" when content is off, how many documents could
not be read, index full, reconnect), then one nonce-tagged `<drive_document>` wrapper per card holding the card text,
`path`, `date` (the effective date), `index: <state> (<reason>)`, `stale`, `truncated`, `identical copies also at`,
and for a content hit `matching passage:` with the window. The nonce is the defence: only a wrapper-tag look-alike in
the text loses its `<`, attributes escape `" < >`, and `R&D/Tom's plan.docx` reads as written (RFC Δ34). The same
wrapper carries `open_file_from_drive` text; a drive file prefetched for DocPlanner, PdfGenerator or
HtmlPageGenerator gets it in Slice 3 ([decision](../../04_solution_strategy/decisions/drive_prefetch_wrapper_slice3.md)).

The standing guidance is in Smart's prompt, the `drive_specialist` block of `PROTOCOL_AGENT_SELECTION`
(`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`, D11): search the drive when the answer may be
written in a document even if no file was asked for; open a stale or recently edited file before quoting; open the
file before quoting a shortened passage; never say a document does not exist while building, and say what was not
searched; open the file for sums and totals (a window is not a table); a document is authoritative for its own
contents, not for the user's current situation; never pick silently between versions.

**The one date rule.** The `drive_query` and `drive_date_from` field descriptions in `agent_manifest.py` carry the
same sentence: dates written inside the document go in `drive_query`; the date filters only when the user limits when
the file is dated. The token's `dates` line keeps only the retry rule: a date filter that finds nothing is retried
without it before saying not found. The filter matches `effective_date` — the photo's taken date, a reader-read
document's own date, otherwise the file date — but the model is told only the one distinction
([decision](../../04_solution_strategy/decisions/drive_document_date_effective_date.md)).

---

## 7. Operations

- **Logs.** On the indexer service, one `[DriveIndex] tick user=… end=done|continued {…} model_cost=$… duration=…`
  line per tick that completes, with counts such as `observed`, `pages`, `skipped`, `cards`, `swept`, `deleted`,
  `rescoped`, `upgraded`, `released`; from Slice 2+4 `indexed`, `demoted`, `failed`, `retried`, `deferred`, `chunks`,
  `embed_ms`, `dropped`, `gone`, `reembedded`, `released_chunks`, `swept_chunks`, `triaged`, `triage_calls`,
  `triage_images`, `model_calls`, `model_pages`. `[DriveReader]` logs one line per read file (status, pages, calls,
  cost, windows, halves); `[DriveExtract]` the parser's failures; `[drive_reader]` / `[drive_triage]` each failed
  model call and how it was classified. A throttled or failed tick logs its own WARNING or ERROR line instead, a tick
  that lost its lease an INFO line, and a purge logs `index purged`. An idle tick still observes a few items, since
  the root and re-emitted unchanged items arrive in every delta round (S0a), but it adds no `cards`. The indexer has
  no Logfire telemetry and no `prompt_content` capture (verbatim scans stay out of BigQuery): Cloud Logging only.
  Read it with `make fetch-logs-indexer K=300 && grep '\[DriveIndex\]' alek_debug_indexer.log`; `make fetch-logs`
  reads the main service, which holds the `[DriveSync]` fan-out and `[DriveSearch]` search lines.
- **Alerts.** The fan-out posts to the ops sink when a user's index is stale (no clean tick since the last success or
  since the sync state was created, or an overdue edit, for `STALE_ALERT_AFTER` = 2 h), once per incident. On a lost
  grant it posts one reconnect notice to the user: the flag is set before sending and lowered only when no send was
  attempted (`NotifyResult.sent`), so the next fan-out retries a notice that never left, and one cut off after it left
  is lost, never repeated. A missing drive prompt or a model outage has no alert of its own: ERROR lines only.
- **Cabinet.** `/api/drive/status` reads the indexing block in its own try: on failure it logs ERROR and leaves the
  block out, so Connect, Disconnect and Reconnect always show. The block carries `available` (a drive queue is
  wired; the switch is hidden when false unless search is on), `access_lost_at` and `index_kept` (the "kept about 30
  days" hint shows only while an index is kept), and the card lists every non-zero counter (RFC Δ44). From Slice 2+4
  it also returns `readable_chunks` and this month's `model_pages_month` / `triage_images_month` with their limits;
  the disclosure names the model providers (Anthropic or OpenAI) that read scans, images and hard PDFs, and Google
  (Gemini) for embeddings.
- **Cost.** Every reader and triage call is billed to the user's account (it appears in `billing_daily_summary`).
  A page costs about $0.0006 on Haiku 5.5 or GPT-6 Luna; the page budget bounds a user at about $3 a month (RFC
  §24.9).
- **Eval** (offline regression guard). `venv/bin/python -m scripts.validation.drive_eval` indexes a synthetic drive
  (`tests/fixtures/drive_eval/tree.json`, with file bytes in `files/` made by `make_fixtures.py`: DOCX, PDFs with and
  without a text layer, a broken-font Cyrillic PDF, XLSX, CSV, RTF, EML, ODT, VTT, rendered UA/RU/ES/EN scans and
  screenshots, all PII-free) with the real tick, real embeddings (`GEMINI_API_KEY` from `.env`) and the real parser,
  reader and triage (`content_wiring.py`, on the indexer's own model registry, with the Firestore prompts;
  `DRIVE_EVAL_PROVIDER=openai` for the GPT leg), runs the gold cases (`gold_g1.json` finds a file, `gold_g2.json`
  answers from content: every expected substring must be in the card's window) through the real search, and fails
  when recall@8 falls below the recorded bars or when any case that hit when the bars were recorded misses now
  (`bars.json`, written by the first owner-reviewed run; it pins the tree, the gold, every served byte and the
  reader's provider). Scopes: per case type, `G1`, `G2` and `ocr`. A case may set `min_hits` (a trip's photos) or
  `known_hard` (reported, in no bar); `semantic` cases reach their file only through the card stream and miss
  offline with fake embeddings (RFC Δ45). `--record` re-records the bars; do that only for a deliberate change, and
  review the misses it prints first. `tests/integration/test_drive_g2_end_to_end.py` runs every text-layer G2 case
  through the real parser with scripted models and bag-of-words embeddings (`make test-integration`).
- **G3** (does Smart search the drive when it should; owner-run, cents per run).
  `python -m scripts.validation.drive_eval.smart_dry_run [--record] --repeats 3` runs the real Smart on
  `gold_g3.json` with the drive reads answered from the fixture index. It measures the search-initiated rate (barred
  on positives, reported on negatives: chit-chat, memory, web, general) and the date-expectation rate (did Smart put a
  document's date in `drive_query` and a "dated" limit in the filters), barred over the date cases only
  (`date_in_query`, `date_filter`) <!-- r2: S-m3 -->. A bar is the Wilson lower bound (z = 1.96) of the recorded
  rate, judged only against bars recorded with the same `--repeats` (`bars_g3.json`).
- **Live model check** (before deploy; owner-run, paid). `venv/bin/python -m scripts.validation.drive_model_check
  read <folder> [--providers claude,openai]` runs the production reader on real files side by side per provider and
  times every call against the 180 s per-window timeout; `triage <renditions dir> [--sizes medium,large]` answers the
  176 vs 800 px thumbnail question. Output (file text, briefs, captions) goes only to `scripts/memory/`.
- **Live check** (before a deploy that changes extraction or search). `python -m scripts.validation.drive_eval.live_check
  --folder scripts/memory/drive_live --user zz-synthetic-<date> --query …` indexes a local folder of real files with
  the real tick, parser, reader, triage and the live Firestore repository under the synthetic id, prints each query's
  result as Smart reads it, then turns indexing off so the production purge removes everything, and fails if anything
  is left. It refuses any id that does not start with `zz-synthetic-`, and refuses to start unless the drive queue
  (`drive-sync-dev`) is PAUSED: the owner pauses it before and resumes it after (the fan-out would otherwise tick the
  synthetic user live); the script prints the resume command when it ends.
- **Deploy (Slice 2+4), in order** (full commands: [docs/07_deployment/README.md](../../07_deployment/README.md) →
  Slice 2+4): spikes S1 (parser limits on the indexer), S0g (custom renditions), S3 (chunk kNN cost) recorded in RFC
  §18; `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` mounted on the indexer as secrets; the `drive_reader` and
  `drive_triage` prompts uploaded by the owner; the Firestore indexes **before** the code that queries them — the
  chunk composites (term stream, generation collection), the records' `unread` composites, the single-field
  exemptions for chunk `text` and `vector` (`make deploy-indexes`), and the card and chunk vector indexes under
  `user_id` and `user_id + kind` (`scripts/infrastructure/create_drive_vector_indexes.sh`), then the smoke check;
  Smart's `PROTOCOL_AGENT_SELECTION` token uploaded with `--apply` together with the deploy, because `render_results`
  dropped the standing guidance the token now carries; the live model check and the live check.
- **Rebuild.** `scripts/infrastructure/drive_index_rebuild.py --user <user_id>` (dry run) / `--apply`: takes the
  user's lease (refused while a tick runs: retry in a few minutes), marks the sync state `purging` and releases it;
  the next tick (the fan-out's within 10 minutes, or a queued continuation) purges the index and re-onboards from
  epoch 1. Project and credentials from the environment / ADC, collections from `APP_ENV`. The purge shows as the
  indexer's `index purged` line. Turning indexing off and on again also rebuilds, but a quick off/on can find the
  index still there and keep it.
- **Smoke.** `scripts/infrastructure/drive_index_smoke.py <user_id>` runs every repository read once against live
  Firestore (record and chunk queries) and names any missing index; it writes nothing.

---

## 8. Code references

| Piece | File |
|---|---|
| Domain model, parameters, versions | `src/domain/drive_index.py`, `src/domain/drive_index_params.py` |
| Policy, planner, card, name keys, task keys | `src/domain/drive_index_policy.py`, `drive_index_planner.py`, `drive_card.py`, `drive_name_keys.py`, `drive_task_keys.py` |
| Extraction contract, router, run rules (Slice 2+4) | `src/domain/drive_extraction.py`, `drive_index_run.py`, `drive_sniff.py`, `drive_image_mime.py` |
| Text gate, chunker, content terms, content search, windows | `src/domain/drive_text_gate.py`, `drive_chunker.py`, `drive_content_terms.py`, `drive_content_search.py` |
| Content ports | `src/ports/content_extractor_port.py`, `src/ports/image_triage_port.py` |
| Parser (sandboxed child) | `src/adapters/subprocess_content_extractor.py`, `src/adapters/drive_parser/child.py`, `readers.py`; `src/utils/document_hardening.py` |
| Model agents, runners, wiring | `src/agents/drive_document_reader_agent.py`, `drive_image_triage_agent.py`; `src/infrastructure/drive_model_call.py`; `src/composition/drive_reader_runner.py`, `drive_triage_runner.py`, `drive_model_setup.py`; agent configs `DRIVE_READER` / `DRIVE_TRIAGE` in `src/infrastructure/agent_config.py`, provider strategy in `src/services/agent_context_builder.py` |
| Smart's drive guidance | `prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy` (`drive_specialist`) |
| Place lookup | `src/domain/place_lookup.py`, `src/adapters/geo_place_table.py`, `src/assets/geo/` |
| Search result, wrapper, render; Cabinet status | `src/domain/drive_search.py`, `src/domain/drive_indexing_status.py` |
| Ports | `src/ports/user_drive_port.py`, `src/ports/drive_index_repository.py` |
| Adapters | `src/adapters/microsoft/onedrive_adapter.py`, `src/adapters/firestore_drive_index_repo.py`, `src/adapters/in_memory_drive_index_repo.py` |
| Services | `src/services/drive_indexing_service.py`, `drive_search_service.py`, `drive_sync_scheduler_service.py`, `user_drive_service.py` |
| Wiring, indexer routes | `src/composition/drive_indexing_setup.py`, `indexer_main.py`, `src/web/indexer_worker_app.py` |
| Agent, manifest | `src/agents/file_management_agent.py`, `src/infrastructure/agent_manifest.py` |
| Eval and checks | `scripts/validation/drive_eval/` (`runner.py`, `smart_dry_run.py`, `live_check.py`, `make_fixtures.py`), `scripts/validation/drive_model_check.py`, `tests/fixtures/drive_eval/`, `tests/integration/test_drive_g2_end_to_end.py` |
| Ops and spike scripts | `scripts/infrastructure/drive_index_rebuild.py`, `drive_index_smoke.py`, `create_drive_vector_indexes.sh`, `deploy_firestore_indexes.py`, `drive_extract_s1.py` (S1), `drive_chunk_knn_cost.py` (S3); `scripts/onedrive/probe_renditions.py` (S0g) |
