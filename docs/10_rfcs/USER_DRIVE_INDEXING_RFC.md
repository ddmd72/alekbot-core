# RFC: User Drive Step 2 — Indexing and Search

**Status:** Draft, revision 3.15 (2026-10-13) — **Slice 1 live** (PR #58, deployed and accepted 2026-10-12); where
the build differs from this text, §23 says how and why. **Slices 2 and 4 are one slice** (D6), designed in §24 and
built (not live until its deploy); §24 overrides the text above where they disagree and was brought in line with the
code in rev 3.15. Slice 3 not built. Phase 0 hardening merged (PR #57, §16).
**Builds on:** [USER_DRIVE_RFC.md](USER_DRIVE_RFC.md) (Step 1, live: CRUD over the user's OneDrive App Folder).
Replaces that RFC's §5 "Step 2" placeholder.
**Precedents reused:** email index ([GMAIL_EMAIL_INDEXING_RFC.md](GMAIL_EMAIL_INDEXING_RFC.md), `IndexedEmail`),
To Do mirror (`TaskSearchIndex`), `src/domain/rrf.py`, `TaskQueue.enqueue_worker_task(dedup_key=…)`, the Gmail
`needs_reconnect` pattern, scheduler fan-out (`start_daily_email_review`).

**Revision history**
- Rev 1 (2026-10-11, external draft): a broad catalogue of risks, written without checking the code. Its factual
  errors are listed in §19 so they are not reintroduced.
- Rev 2 (2026-10-11): rewritten as one spec after two review rounds (five lens analyzers + six-hats synthesis;
  paradigm advocates + problem hunters + a judge panel) and owner decisions.
- Rev 3 (2026-10-11): after a skeptic panel (product, architecture, QA, operations; each finding put to three
  independent refuters, 38 survived) and a completeness critic (7 more). Main changes: tree-scoped policy with opaque
  subtrees (§5.2), a per-user lease and explicit task keys (§7.1), chunk generations (§3.3), a durable folder-delete
  cascade (§7.6), version upgrades that actually take effect (§12.3), failure classes (§8.2), a search coverage
  envelope (§10.2), an escaped untrusted wrapper (§10.4), provisioning (§12.5), acceptance per slice (§16), a runnable
  eval (§17.8).
- Rev 3.1 (2026-10-11): after an audit of rev 3 (11 partial resolutions, 14 new defects): the App Folder root as an
  implicit parent, generation persisted before chunk writes, epoch stamped on every upsert, purge keyed on state not on
  exceptions, chunk-cap release by headroom, upgrade completion and embedding switch only when nothing is left, edits
  ahead of onboarding in priority, staleness alert on class-0 work only, read-time hiding under opaque ancestors,
  subtree re-listing instead of drive-wide epochs, an `unread` count limited to admitted kinds.
- Rev 3.2 (2026-10-11): owner decisions D2, D4, D5 (separate indexer service) and proposals D1 (images, §5.3) and D3
  (disconnect grace); `DriveChange`-returning port reads, cascades in their own priority class, upgrade completion by a
  clean pass, dual-field writes during an embedding switch, folder search bounded by chunks, `observed_as_of`.
- Rev 3.3 (2026-10-11): D1 decided with rationale — problem-content classes (§5.5) and image triage (§5.3); D3 decided —
  access loss keeps the index under a 30-day TTL; indexer service provisioned and wired (§4, §12.5, §13).
- Rev 3.4 (2026-10-11): TTL stamped once, late and at a fixed instant, with a safe resume rule; density decided once from
  a probe, over content class, with hysteresis; the text-quality gate measured after timestamps; `building` limited to
  incompleteness; image triage on every image (camera facet as a prior) and thumbnails as spike S0f; place names stored
  as a derived facet; `drive_id` sourced from the credentials document; bounded folder walks.
- Rev 3.5 (2026-10-11): regain before the TTL margin continues from the cursor (no epoch); VTT/SRT admitted and
  normalised before the text gate, which ignores timestamp-only lines and short replies; `building` derived by a query,
  not a counter; probes on threshold crossing with a budget; triage verdicts stored; geocoding versioned in the upgrade
  step and injected by composition.
- Rev 3.6 (2026-10-11), after an implementer's blocker pass: a 401 after a successful refresh is an operation error,
  not a lost grant; regain checks the earliest stamp still written; per-item provider errors never stall a user;
  first-seen cards wait for the round; cap priority by content class; date filters on `effective_date`; filter-only
  queries; `drive_id` carried through token refreshes.
- Rev 3.7 (2026-10-11), after an external review: a lexical name stream in the base (Slice 1); duplicates collapsed by
  content hash; the content exact-token stream eval-gated within Slice 2; reranking, contextual headers, better PDF
  extraction, extraction reuse and version clustering named as extensions; extraction quality and walk mode stated as
  known limits; maintenance hotspots named.
- Rev 3.8 (2026-10-11): quality decisions taken in the RFC instead of left to the eval — no cosine thresholds (rank +
  `K` + Smart's judgement), the content exact-token stream decided into Slice 2; the eval is a regression guard only.
- Rev 3.9 (2026-10-11): Slice 4 becomes one model extractor — a multimodal model reading PDFs and images natively,
  behind a parser-first router (no text, low density, garbage text, parser error) — covering OCR, hybrid and hard
  PDFs (§9).
- Rev 3.10 (2026-10-11), final external review: the access-loss TTL stamping replaced by a tick-side purge after a
  30-day grace period (same behaviour, far less machinery); full-text lexical search named as the known ceiling with
  its seam; a per-user monthly page budget for the model extractor; a deterministic name-key selection rule.
- Rev 3.11 (2026-10-11): spikes S0a–S0f run on the owner's App Folder (§18). Delta works under
  `Files.ReadWrite.AppFolder`, so walk mode is not built. Results folded into §3.1, §5.3, §7.2, §7.3, §8.5, §15 and
  §17.3. Unchanged items are re-emitted often, and a delete and a restore can share one round. A move out of the App
  Folder arrives as a plain deletion. The `photo` facet can be an empty object, and audio files carry no `audio` facet.
- Rev 3.12 (2026-10-12): Slice 1 built and reviewed; its deltas in §23 (Δ1–Δ45), false statements corrected in place.
- Rev 3.13 (2026-10-12): Δ46 — a 410 `Location` is followed only to the App Folder's own feed.
- Rev 3.14 (2026-10-13): owner decisions D6–D13 (§21). Slices 2 and 4 merged into one slice and designed in §24: the
  model extractor is Claude Haiku 5.5 or a GPT model behind a config switch (the "Gemini, decided" of §9 is
  withdrawn), it returns a structured brief next to the verbatim text, image triage also captions photographs (moved
  in from §14), the document's own date becomes `effective_date`, both model calls are agents on the indexer, the
  drive intents stay as they are.
- Rev 3.15 (2026-10-13): §24 matched to the built slice after its adversarial reviews: lazy page windows and the
  halving rules (a half that times out again fails the file; a provider refusal is terminal), maximal query terms,
  digit groups and the `.00` integer form, term selectivity by distinct files with a probe past the page, the window
  budget, partial usage before an outage that does not end it, re-embedding readable chunks in any content state, a
  missing thumbnail retried before it reads as a photograph, the parameters as built, and the known limits (sandbox
  isolation, `LeaseLost` usage, text-gate calibration).

**Owner decisions (2026-10-11)**
- The App Folder is meant to become a real document store. It is empty only because the feature is being built.
  **We build the full target system**, not a usage-gated minimum. "Unjustified complexity" means future maintenance
  cost and fragility, not implementation effort.
- Several users exist. Nobody can be told what to put in their folder: **it may contain anything**, a git project
  included.
- **Anything not built now must arrive later as an extension** (new adapter, new stage, new stream, new policy kind),
  never as a paradigm change (new unit of knowledge, new ID scheme, new data model, new concurrency model).
- All three query types are equally likely: *find a file*, *answer from content*, *Smart uses the documents as a
  knowledge base*.
- The user's own edits in Office Online (autosave) are handled from day one. **Editing by the bot** is a separate RFC.
- The drive is an **orthogonal knowledge base**, not validated facts about the user. It does not feed
  `SearchEnrichmentService` (§10.3).
- This RFC adds **no path that feeds document content to consolidation**. Where the line runs (a scanned receipt
  *is* a fact about the user; a contract on the drive is not) needs its own design: OQ-1.

**Owner decisions on the open points** (D1–D5) are in §21.

---

## 1. Problem and goals

Step 1 gives the bot a long-term file area it can list, open, save, move and delete. It cannot find anything by
meaning or content: Graph search inside the App Folder returned 401 (spike A6, Step 1 §7), and listing does not scale
past a few folders. Files with uninformative names (`Договор_ред3.docx`, `notes (2).md`) are lost to the
conversation.

**Goals**
- **G1 Find a file or folder** by name, type, date, folder, photo facets and place, and — for files with a text layer — by
  what it is about. Screenshots and scans are found by their text from Slice 4; camera photos by date, place and
  camera (§5.3).
- **G2 Answer from content**: a clause, an amount or a date inside a document, with the file it came from.
- **G3 Knowledge base**: Smart searches the drive on its own when the answer may be in the user's documents, and does
  not when it is not.
- **G4 Correctness under arbitrary content** (repositories, archives, dumps, books, scans, photos) and under Office
  Online autosave churn, without owner intervention for months.
- **G5 Extensibility**: every deferred capability (§14) plugs into a named seam.

**Non-goals (this RFC)**
- Editing files by the bot (separate RFC; Step 1 §4.9).
- A path that feeds drive content to consolidation (OQ-1), and any use of `SearchEnrichmentService` (§10.3).
- A second storage provider. The port stays provider-neutral, but the design is not bent around a hypothetical Google
  Drive adapter: Google has no user-visible app area like the App Folder, so "the user drops files in, the indexer
  picks them up" does not port as is. Step 1 §2 is corrected accordingly.
- LLM summaries of every document, code indexing, audio/video (all §14). Text recognition for screenshots and scans,
  a brief for model-read documents and short captions for photographs are in the merged Slice 2+4 (§24).

---

## 2. Concept

1. **The drive is the source of truth; the index is a disposable mirror.** Everything indexed can be recomputed from
   the drive. Version changes migrate in place (§12.3); purge-and-rebuild (§12.2) is the recovery tool when the index
   is suspect. Strictness goes where data is *not* disposable: memory, prompts, history refs, telemetry copies (§11).
2. **One record per drive item is the canonical unit.** Chunks are children of a record, keyed by the record's index
   generation. Search fuses results at the record level. A later stage (LLM summary, OCR text, a new vector) is a new
   field or child of the same record, never a new unit.
3. **Reconciliation is level-triggered.** A periodic tick per user observes the drive, writes what changed into the
   records, then works off whatever is *due*. A record's state alone says what work remains: a crash, a missed tick,
   an expired delta token and a reconnect all run the same code path. Long cascades (folder delete, policy change in a
   subtree) are themselves record states, so they survive crashes.
4. **Content admission is a default-deny domain policy over the tree.** Documents are read; everything else is
   registered by metadata only, with a reason. Machine-generated subtrees (dot-folders, repositories) are collapsed
   into one record. The policy is pure code: a new kind of content is a policy change plus a version bump.
5. **Untrusted content is handled at two boundaries.** Extraction runs in a resource-limited child process behind a
   port. Retrieved text and file names reach the model only inside a nonce-tagged wrapper that text inside it
   cannot close (§10.4).

**Why not the alternatives** (each argued in full by an advocate and scored by three judges):
- *A generic, source-agnostic document corpus*: pays for generality before a second source exists; the email index is
  a different unit (one triaged message, not a file tree). If a second document source arrives, the card contract
  (§10.2) is the seam.
- *No content index, read on demand with long-context models*: fails G2/G3 across many files and puts untrusted
  extraction on the chat path.
- *An LLM summary per file as the core unit*: an LLM call over every hostile file at index time, and still no pinpoint
  answers in long documents. It stays a later stage (§14).
- *An internal UUID per file*: Step 1 refs are `drive:<provider item_id>`; a provider switch rebuilds the index anyway.
  An alias resolver behind `parse_drive_ref` is the seam if ever needed.

---

## 3. Domain model (`src/domain/drive_index.py` and siblings, pure, `mypy --strict`)

### 3.1 `DriveChange` — what the adapter observes

`item_id`, `parent_id`, `name`, `is_folder`, `size_bytes`, `mime_type`, `content_version` (`cTag`), `modified_at`
(the item's server-set `lastModifiedDateTime`, not `fileSystemInfo` — it drives scheduling only), `web_url`, `facets`,
`deleted`. The record also stores `effective_date`, the date a user means: `photo.takenDateTime`, else
`fileSystemInfo.lastModifiedDateTime`, else server time. Search date filters and cards use `effective_date`; an
August holiday photo uploaded in October is an August photo. For a deletion
only `item_id` and `deleted` are relied on. In S0a a file's tombstone carried `cTag` and `size` but no name or path, and a
folder's carried neither; other providers and OneDrive for Business promise less. **No path is stored**: OneDrive
personal does return `parentReference.path` on live delta items (S0a), but OneDrive for Business does not, and paths
derived from the parent chain do not go stale on a folder rename. `DriveChange` is a new type; Step 1's `DriveItem` keeps its resolved `path` and is built for cards only
after a path is derived (§10.2), so the Step 1 invariant "`path == ''` means the root" is never violated.

### 3.2 `DriveIndexRecord` — one per drive item the sync keeps

| Field | Meaning |
|---|---|
| `user_id`, `item_id` | Key. `item_id` is the provider id (same as Step 1 refs). |
| `parent_id`, `name`, `is_folder`, `size_bytes`, `mime_type`, `web_url`, `facets` | From the last observation. Facets are stored raw (photo, audio, video, location, remoteItem, package, malware, hashes). |
| `kind`, `content_class`, `card_rank` | Kind from the policy (§5), from the extension and mime type (magic bytes from Slice 2); its content class (document, media, other, folder) and the card-cap rank derived from it (§5.4). |
| `scope` | `normal`, `opaque_root`, `inside_opaque` (folders only), `unresolved` (parent not yet known). Inherited from the parent (§5.2). |
| `content_version`, `modified_at` | Provider `cTag` and server `lastModifiedDateTime`. |
| `state`, `reason` | §3.4. |
| `index_generation` | Monotonic integer; incremented **and persisted** before an index run writes its first chunk (§8.1 step 2). |
| `indexed_generation` | Generation whose chunks are readable; `None` if none (§7.4). |
| `indexed_version` | `content_version` that `indexed_generation` was built from. `stale` = a readable generation exists **and** `content_version != indexed_version` (records without content are never stale). |
| `policy_version`, `extraction_version`, `card_version`, `embedding_space`, `name_keys_version`, `geo_table_version` | Versions under which the record's current state, chunks, card, name keys and place name were produced (§12.3). The policy and name-keys versions are stamped on every record; the card version and embedding space when a card is embedded or refused; the geo-table version on a located photo. |
| `card_hash`, `has_card`, `card_vector` | Hash of the card text built by `card_text(record)`, whether a vector is stored, and the vector (§8.5). |
| `card_capped`, `card_refused` | The card cap withholds this record's card (§5.4, RFC Δ23); the provider refused the card text `card_hash` names (§8.2, RFC Δ24). Neither changes `state` or `reason`. |
| `name_tokens` | Lexical keys of the name for the name stream (§10.1): NFC + casefold tokens split on separators, case and digit boundaries, their accent-stripped forms, Cyrillic↔Latin transliterations (Russian-style and Ukrainian national), and character trigrams (RFC Δ38). Recomputed on rename. Empty for OS artefacts (RFC Δ32). |
| `place_name`, `effective_date`, `fs_modified_at` | Derived on observation: the offline place name of a located photo (§5.3), and the date a user means (§3.1) with the file-system time it falls back to. |
| `probe_pending`, `rescope_pending`, `cascade_cursor`, `reobserve_pending`, `reobserve_cursor`, `probed_child_count` | Pending structural work that makes the record due (§3.6, §5.2, §7.6; RFC Δ18), and the child count the density decision was taken at (hysteresis). |
| `content_hash` | `quickXorHash` + size from the facets, when present; collapses duplicates in search (§10.2). |
| `dirty_since`, `due_at`, `priority` | Scheduling (§7.3, §7.8). |
| `attempts`, `attempt_started_at` | Failure accounting (§8.2). `attempt_started_at` is persisted before download; a record found with a stale one is a crashed attempt. |
| `extracted_by` | `extractor_id@version` that produced the current text (§9); compared per kind by the upgrade step. |
| `text_chars`, `pages_total`, `chunk_count`, `truncated` | What the last extraction produced. `text_chars / pages_total` is the text density that finds hybrid PDFs (some pages scanned) for Slice 4. `chunk_count` is kept on refusal too, so cap release can test headroom (§5.4). |
| `seen_epoch` | Epoch number current when the record was last upserted (§7.5). |

### 3.3 `DriveChunk` — child of a record

Key `(user_id, item_id, generation, index)`. Fields: `text`, `vector`, `char_start`, `char_end`, `kind` (copied from
the record; immutable for a generation, so it is a free pre-filter). No file name, path or date: rename and move cost
zero chunk writes. Neighbours are `index ± 1` of the same generation.

### 3.4 States and reasons

`state ∈ {unresolved, pending, indexed, metadata_only, failed, deleting}`.

- `unresolved` — observed before its parent; no work, no card, until the parent arrives (§5.2). Transient: when a
  round completes with the parent still unknown, the planner resolves it with one `get_change(parent_id)` (bounded per
  tick); a parent that is definitely gone (a deleted change, or not found) means the record is an orphan and is
  deleted, a folder through the cascade. A lookup that keeps failing transiently never deletes it: it is retried
  every `RETRY_CAP` (§8.2, RFC Δ29). A record whose parent is itself unresolved is parked (no due time, no card)
  until the parent's resolution re-classifies its children.
- `pending` — content admitted, not yet indexed at the current version and versions.
- `indexed` — readable chunks exist for `indexed_version == content_version` at the current extraction version.
- `metadata_only` — findable by its card (unless the reason says otherwise), content deliberately not read.
  `reason ∈ {folder, kind_not_admitted, too_large, opaque_root, dot_name, secret_name, os_artifact, remote_item,
  package, malware, no_text, machine_text, dense_tree, encrypted, user_chunk_cap, photograph}` (`photograph`: an
  image triaged as a photograph, §24.5). Entering `metadata_only` clears `indexed_generation`
  and collects the chunks. `os_artifact` is the only reason that also withholds the card (and the name keys, RFC
  Δ32). The card cap is not a reason but the flag `card_capped` (§5.4, RFC Δ23), and a terminally refused card the
  flag `card_refused` (§8.2, RFC Δ24); `user_card_cap` is unused.
- `failed` — admitted, failed at this version after the per-file retry budget (§8.2). `reason ∈ {extract_timeout,
  extract_memory, extract_error, download_error, embed_timeout}`. **Keeps** the previous `indexed_generation`: older
  chunks stay readable, with `stale` and the reason on the card.
- `deleting` — a folder whose subtree is being removed (§7.6). Never returned by search, nor is anything under it.
  Leaves the state only by removal, or by being observed again as not deleted (§7.6).

**Content readability** is independent of state: a record's chunks are readable iff `indexed_generation` is set
(states `indexed`, `pending` with a previous generation, `failed` with a previous generation). Search returns a card
for every carded record and content only for readable generations (§10).

`no_text` (a scan with no text layer) is deterministic, so it is `metadata_only`, and it is exactly the set the
Slice 4 OCR extractor picks up (§5.3).

### 3.5 `DriveSyncState` — one per user

`drive_id`, `root_item_id` (the App Folder root: an implicit parent of scope `normal` that never becomes a record),
`cursor` (opaque), `round_complete` (the last `get_changes` call returned `complete`), `round_started_at`, `epoch`,
`epoch_phase ∈ {idle, enumerating, sweeping}`, `initial_enumeration_done`, `purging` (a purge started and finishes
before anything else, §7.6), `created_at` (the staleness baseline before any success, §12.1),
`lease_owner`, `lease_until` (§7.1), `continuation_enqueued_at`, `applied_versions`, `target_versions`,
`upgrade_cursor` and `upgrade_found_stale` (§12.3), `applied_space` and `target_space` (the embedding space the cards
are in, and the one a running pass moves them to, §12.3), `observed_as_of` (§12.1), `access_lost_at` (§7.6),
`last_success_at`, `last_error` (a category:
`reconnect_needed`, `throttled`, `provider_error`, `internal`; detail stays in logs), `systemic_failures` (consecutive
ticks ended by a systemic error, for the backoff, §8.2), `stale_alerted` and `reconnect_notified` (written only by the
fan-out, §12.1, §7.7), `counters` (records per state and `carded`; readable chunks from Slice 2). There is no
`pending_cascades` (RFC Δ14) and no `pending_reobserve` (a record flag instead, RFC Δ18).

### 3.6 The planner and the policy

As built (`src/domain/drive_index_planner.py`, `drive_index_policy.py`; the policy is module code, not a parameter):
- `classify(change, parent_scope, folder_probe | None, previous_record | None) -> Classification` (stored?, scope,
  state, reason, kind, content class, `probe_pending`, the density count) — pure.
  `folder_probe` is the result of probing a folder's first `PROBE_CHILDREN` children with `list_children_changes`
  (marker children such as `.git` and `pyvenv.cfg`, and content-class counts); the service supplies it for a folder
  whose `childCount` exceeds `DENSE_FOLDER_CHILDREN` when no earlier density decision still stands (first sight, a
  count that doubled or halved, a `policy_version` bump); at most `ITEM_CALLS_PER_TICK` item calls per tick (RFC Δ9;
  the rest wait as `probe_pending`, their children held as `unresolved`).
- `plan_observation(change, existing, parent, probe, ctx, previous_parent=None, *, observed=True) -> ObservationPlan`
  — pure, one feed entry or a re-planned stored record: the record to upsert (with its due time and priority from
  `schedule`), the item to delete, and updates of the old or new parent (a marker child closing or re-opening a
  folder). Pending work is carried on the record itself — `probe_pending`, `rescope_pending`, `reobserve_pending`,
  state `unresolved` or `deleting`, a card due or to drop — and the tick's due loop dispatches on it. The other pure
  helpers: `access_action` (the gate), `stale_versions` and `reembed_in_space` (§12.3), `capped`, `released`,
  `card_refused`, `with_card`, `with_retry`, `is_stale` (§12.1).
- A parent equal to `root_item_id` is a known parent of scope `normal`.
- **All decision logic lives here** and is tested without Firestore, Graph or a clock (§17).

---

## 4. Ports

| Port | Change | Why it is a port |
|---|---|---|
| `UserDrivePort` (exists) | `get_changes(user_id, cursor) -> DriveChanges` (`changes: list[DriveChange]` in feed order, next opaque `cursor`, `complete`, `full_enumeration`, `drive_id`, `root_item_id`; raises `DriveResyncRequired` on a 410, or a 404 on a stored cursor (RFC Δ40), and `DriveThrottled` with a `retry_after_s`). New error `DriveReauthRequired(DriveNotConnectedError)` **only** when the token refresh itself is rejected (`invalid_grant`, and `interaction_required` / `consent_required`, RFC Δ41). A 401 that survives a *successful* forced refresh becomes a new `DriveAccessDenied` for that operation — spike A6 showed the App Folder answering 401 to a valid token for an unsupported call — and is handled as a per-item or drive-wide error (§8.2), never as a lost grant. Two new reads returning `DriveChange` (with `cTag`, `parent_id`, facets): `get_change(user_id, item_id)` (nudged items, unresolved parents; an item that no longer exists comes back as a `deleted` change) and `list_children_changes(user_id, folder_id, limit, cursor)` (density probe, paged subtree re-observation; RFC Δ10). Also `connection(user_id)` (connected, `needs_reconnect`, the connected `drive_id`) and `flag_reconnect(user_id)` (True on the False→True transition). Step 1's `DriveItem`-returning methods are not used to build observations. | Provider boundary. Delta, confirmed by S0a; a bounded walk only for a future provider without a change feed. |
| `DriveIndexRepository` (new) | Records and sync state (chunks join in Slice 2). Sync state: `get_sync_state`, `save_sync_state(state, owner)` and `delete_sync_state(user_id, owner)` fenced by the lease (§7.1), `create_sync_state_if_absent` (the fan-out's minimal state, RFC Δ28), `update_sync_flags` (the fan-out's two flags), `acquire_lease`/`release_lease` (transactional), `list_sync_user_ids`. Records: `get_records`, `upsert_records` (a card vector travels with its record write), `delete_records`, `due_records(user_id, now, limit)` ordered by `(priority, due_at, item_id)`, `oldest_due_at`, `has_record_in(user_id, state, priority)`, `children(user_id, parent_id, after_item_id, limit)`, `records_page(user_id, after_item_id, limit)`, `records_seen_before` (the sweep), `count_carded`, `lowest_ranked_carded`, `card_capped`, `count_by_state`, `delete_user_records(user_id, limit)` (a purge's batches). Search: `find_nearest_cards` (pre-filter `user_id`, optional `kind`), `find_by_name_keys`, `filter_records`, `card_vectors`. Batching and transactions are the adapter's business. | Storage boundary (Firestore). One repository: records and chunks change together. |
| `ContentExtractorPort` (new, Slice 2) | `extract(path, kind, limits) -> ExtractionOutcome` (`text`, `no_text`, `unsupported`, `too_large`, `timeout`, `memory`, `error`). | Sandbox and process boundary; several adapters expected (local child process, separate service, OCR provider). |
| `EmbeddingService` (exists) | Unchanged; the space is the adapter's class attribute `EMBEDDING_SPACE` (e.g. `gemini-embedding-2/768`), injected by composition (RFC Δ1). | Already a port. |
| `TaskQueue` (exists) | Port unchanged. Composition injects a **second instance bound to the drive queue and to the indexer service's `/worker` URL and OIDC audience** (§12.5), used by the fan-out and by `UserDriveService` nudges on the main service; its `create_queue_if_not_exists` is never called (it would create the queue with concurrency 5). | — |

No port for the policy, the planner, the chunker or the card builder: they are pure domain code.

---

## 5. Content policy (`src/domain/drive_index_policy.py`)

### 5.1 Per item

Decided from metadata first; a download happens only if the item is admitted.

- **Never downloaded:** names starting with `.`; secret-shaped names (`id_rsa*`, `id_ed25519*`, `*.pem`, `*.p12`,
  `*.pfx`, `*.kdbx`, `*.keychain*`, and `*.key` unless its mime type is Keynote's); OS and lock artefacts (`~$*`,
  `.~lock.*#`, `Thumbs.db`, `desktop.ini`, `.DS_Store`, `*.tmp`), checked first, so `.DS_Store` is an artefact
  before it is a dot-name, and kept as records with no card and no name keys (RFC Δ32); items with a `remoteItem`,
  `package` or `malware` facet.
- **Admitted kinds (content read):** PDF, DOCX, PPTX, XLSX/XLS (row-capped), ODT/ODS/ODP, EPUB, HTML, Markdown, plain
  text, CSV, RTF, MSG/EML, VTT/SRT subtitles and transcripts. **HTML/XHTML is always converted** (markitdown's HTML
  converter: scripts and styles removed, headings and tables to Markdown, inline `data:` images truncated), never read
  raw: today's `text/*` fast path in `convert_file_to_text` would put markup, CSS and base64 into chunks — and the bot
  itself saves HTML reports to the drive. MHTML (saved web pages) is metadata-only for now.
  **Every admitted kind has a named extractor** — a kind without one is not admitted (checked against today's code
  2026-10-11):
  - plain text, Markdown, CSV, JSON, VTT/SRT: read with **charset detection** (BOM first, then a detector such as
    `charset-normalizer`, already a markitdown dependency) — today's `text/*` path decodes as UTF-8 only, so a CP1251
    or UTF-16 `.txt` becomes replacement characters; VTT/SRT then normalised to cue text;
  - RTF: a dedicated RTF-to-text step (e.g. `striprtf`) — markitdown has no RTF converter and passes raw control words;
  - EML: Python's `email` parser, text/plain part preferred, else the HTML part through the HTML converter, decoded
    from base64/quoted-printable — markitdown has no EML converter;
  - ODT/ODS/ODP: an ODF text extractor (content.xml of the ZIP package) — markitdown has none;
  - PDF, DOCX, PPTX, XLSX/XLS, EPUB, MSG, HTML: markitdown's allowlisted converters (Phase 0), PDF behind the §9 router.
  Password-protected PDF/Office/EPUB → `metadata_only / encrypted` (deterministic: no retries, no model call). Apple
  iWork (`.pages`, `.numbers`, `.key`) has no extractor → `kind_not_admitted`. Kind comes from magic bytes on the downloaded head plus the mime type: a `.txt` that is a
  ZIP is a ZIP.
- **Metadata only by default:** source code, logs and dumps, archives, disk images, databases, executables, images,
  audio, video. Screenshots and scans are admitted to text recognition in Slice 4 (§5.3); the rest are candidate
  extensions (§14). Why each class gets the treatment it gets: §5.5.

### 5.2 Over the tree: scope and opaque subtrees

- An **opaque root** is a dot-named folder, a folder with a child `.git` or `pyvenv.cfg`, a `node_modules`,
  `__pycache__` or `site-packages` folder anywhere (RFC Δ16; `vendor`, `target`, `build` and `dist` need no rule of
  their own: inside a repository they are already inside its opaque root), or a **dense tree** (§5.5, class 1): a
  folder whose
  `childCount` exceeds `DENSE_FOLDER_CHILDREN` and whose probed children are mostly of content class *other* — not
  *document* (text, office, PDF, e-book, mail) and not *media* (images, audio, video). The class is about what a file
  is, not whether this slice admits it, so a folder of 600 PDFs or 1 000 holiday photos is never dense. Density is
  decided **once, before the folder's children are applied** (from the probe), and re-decided only when `childCount`
  doubles or halves, or on a `policy_version` bump — no flapping. It gets one `metadata_only` record
  (`reason = opaque_root`, `dot_name` or `dense_tree`) with a card: `name | repository` (a marker child),
  `name | hidden folder` (a dot-name), `name | generated files` (a generated directory) or
  `name | generated files (500+ items)` (a dense tree, its child count bucketed so the card does not change with
  every child; RFC Δ33). No `descendant_count` is kept.
  From Slice 2, a repository root's card also carries the lead text of its root `README*` (extracted like any
  document); the README gets no record of its own. One card per repository.
- **Inside an opaque root, files get no record and folders get a lightweight record** (`scope = inside_opaque`, no
  card, not counted toward caps) so their children can be recognised and dropped. A 30k-file repository costs one card
  embedding and a few hundred folder writes.
- `scope` is inherited: `classify` needs only the parent record, never the chain. A change whose parent is unknown is
  stored as `unresolved` (no work, no card); when the parent is observed, the planner re-classifies its children.
- **Re-scoping is durable.** When a folder's effective scope changes — renamed to or from a dot-name, moved into or
  out of an opaque subtree, a `.git` child appears or disappears — the planner sets the folder's `rescope_pending`:
  the folder is due (class 1, §7.8), its children are re-classified page by page; each child folder whose scope
  changed gets its own rescope. Records that become opaque are deleted (files) or reduced to lightweight folder
  records. A folder that leaves an opaque subtree gets the re-listing flag `reobserve_pending` (RFC Δ18): its
  subtree, never stored, is re-listed with `list_children_changes` (one page per item call, resumable by a page
  cursor) and every listed item is applied as an observation — no drive-wide epoch. A subtree whose effective policy is unchanged costs metadata reads only.
- **Read-time check.** Until a cascade finishes, search drops any card whose ancestor chain (already walked to build
  the path, §10.2) reaches an `opaque_root`, `inside_opaque` or `deleting` record. Search is correct at once; the
  cascade does the physical work.

### 5.3 Images

An image is either **information-bearing** (a scan, a screenshot, a photographed receipt, a whiteboard) or a
**photograph** (a holiday, people, places). The first needs its text; the second needs where and when. Reading every
image with a vision model is the "photo gallery trap" — 100 holiday photos would cost 100 full vision calls for nothing
— so images are triaged by signals the provider gives for free, and only the information-bearing ones are read.

- **Every image (Slice 1):** a card from provider metadata only — name, date taken, camera, dimensions and a place name
  derived **offline** from the `location` facet. The indexer service resolves the place once with a bundled
  reverse-geocoding table (no API call, no download) and stores it on the record as a derived `place_name` facet
  (English name plus the local name), stamped with `geo_table_version`; `card_text` stays pure over record fields.
  Composition loads the table and injects a pure lookup into the indexing service — no port, no adapter import.
  "Photos from Rome last August" works for 100 holiday photos at the cost of 100 card embeddings. S0e confirmed that
  `photo` (with `takenDateTime`, camera make and model) and `location` arrive in delta for a phone HEIC.
- **Triage (Slice 4), on every image:** the provider's small thumbnails (no full download) are sent in batches of
  `TRIAGE_BATCH` to an ECO vision model with one question — "which of these show a document, a receipt, a screen or a
  board with text?". 100 photos are about 7 cheap calls. The camera facet is only a prior (passed with each thumbnail),
  never a verdict: photos saved from WhatsApp or Telegram, downloads and edited exports carry no EXIF, and sending them
  all to full OCR would rebuild the gallery trap. The prior is `photo.takenDateTime` or `photo.cameraMake` being
  present, never the `photo` key alone: S0e saw an empty `photo: {}` on EXIF-less JPEGs and on a video. S0f confirmed
  thumbnails under `Files.ReadWrite.AppFolder` for JPEG, PNG, HEIC (served as JPEG) and SVG (served as PNG), in
  bounding boxes of 96, 176 and 800 px, so the indexer never downscales an image itself. The verdict is stored on the record (`triage_verdict`,
  `triage_version`), so a `policy_version` bump does not re-triage images; only a `triage_version` change does.
  - Information-bearing images and PDFs with `no_text` (or a low text density, hybrid PDFs) go to the **model
     extractor** (§9); the recognised text enters the ordinary chunk pipeline. Photographs stay metadata-only.
- **Rejected signals:** file-name and folder patterns (`Scan_*`, `Screenshot_*`, `Docs/`) — the no-convention principle,
  many languages, users rename; Shannon entropy — meaningless on compressed image formats.
- **Captions (D9, §24.4):** the same triage call returns a caption of at most ten words for each photograph ("a
  cathedral at night"), stored on the record and put into its card. Every image passes triage anyway, so burst
  sampling is not needed and is dropped.

### 5.4 Caps

- **Per file:** download size by kind; ZIP container caps (declared inflated size, member count); extraction limits
  (§9); `MAX_CHUNKS_PER_FILE`. A file beyond the chunk cap is indexed up to the cap with `truncated = true`.
- **Per user:** `MAX_CARD_RECORDS_PER_USER` counts records that carry a card vector; `MAX_CHUNKS_PER_USER` counts
  readable chunks (`Σ chunk_count` over records with an `indexed_generation`).
- **Card cap, admission order** by content class (document, then media, then other — §5.2), then folders. In Slice 1,
  where no kind is admitted yet, a PDF still outranks a `.py` file. A strictly higher-priority
  item evicts the lowest-priority carded record (the oldest `modified_at` among equals), which gets the flag
  `card_capped` and loses its card while its state and reason stay structural (RFC Δ23); an equal-rank newcomer is
  capped itself. So a document never loses its card while a non-document holds one.
- **Chunk cap:** a file whose chunks would exceed `MAX_CHUNKS_PER_USER` is refused whole after extraction, never
  partly indexed: `metadata_only / user_chunk_cap`, card kept, `chunk_count` recorded.
- **Release:** a `card_capped` record becomes due when there is card headroom (the flag is lifted, class 3); a `user_chunk_cap` record becomes due
  only when chunk headroom ≥ its recorded `chunk_count`. Bounded per tick, best rank first, then newest `modified_at`. A refused file
  is therefore never re-extracted until it can fit.

### 5.5 Problem-content classes — why each rule exists

Rev 1 grouped hostile content by the **harm it causes**, not by extension. That framing is kept, with one design
principle on top: **safety never depends on classifying correctly.** Two layers:

- **Layer 0 — bounds that hold for any input:** the per-user caps with document-first admission (§5.4), the per-file
  caps, max-pooling one hit per file before fusion (§10.1), extraction in a killed-on-limit child process on a separate
  service (§9), the quiet period (§7.3). A misclassified file can cost quality, never stability or another user's
  service.
- **Layer 1 — classification for quality and cost:** each class below needs (a) a harm, (b) an objective signal
  available at the stage where it is evaluated (metadata, the first bytes, or extracted text), and (c) a visible
  `reason`, so a wrong rule is fixed by a policy bump, not a migration.

| Class (rev 1) | Harm | Signal we use | Stage | Outcome | Rev 1 signal not used, and why |
|---|---|---|---|---|---|
| 1. Machine-generated trees (repos, `node_modules`, venvs, app caches, site mirrors) | Record/embedding blow-up, search pollution | Markers: dot-folders, a `.git` child, a `pyvenv.cfg` child, build/vendor dirs inside a repository; **density**: a folder whose `childCount` (free provider facet) exceeds `DENSE_FOLDER_CHILDREN` and whose probed children are mostly content class *other* (neither document nor media), decided once before its children are applied | metadata + a bounded probe | opaque root (§5.2), reason `opaque_root` / `dense_tree` | Timestamp clustering: copying, syncing or unzipping gives human folders identical timestamps too (300 scanned invoices uploaded at once). Density alone: an Obsidian vault is hundreds of tiny Markdown notes, a photo folder thousands of images — the content-class condition keeps both. |
| 2. Opaque containers (archives, disk images, encrypted DBs, password vaults) | Decompression bombs, OOM, unreadable | Magic bytes of the downloaded head; ZIP central directory for OOXML/EPUB | first bytes | metadata-only, `kind_not_admitted`; bombs → `too_large` | Shannon entropy: every DOCX/XLSX/PPTX/EPUB is a ZIP at ~7.9 bits/byte, so an entropy gate bars all Office documents and passes dumps. |
| 3. Machine text disguised as text (logs, SQL/HAR dumps, telemetry saved as `.txt`) | Thousands of near-identical chunks that win every query on common tokens | Extension/kind first (`.log`, `.sql`, `.har`, `.jsonl` are not admitted); then a **text-quality gate** on extracted text: repeated-line ratio, share of lines starting with a timestamp/IP, distinct-token ratio over a sample | extracted text | `metadata_only / machine_text`, card keeps the lead text | — (rev 1's lexical-variety idea is used, but after extraction, where it is measurable, not as a guess before download). Tables are exempt: they are row-capped and treated as tables (§8.3). |
| 4. Book-length monoliths | Search gravity over short notes; embedding cost | Chunk count | chunking | Max-pooling per file removes the gravity; `MAX_CHUNKS_PER_FILE` bounds cost; `truncated` is visible | Head + summary + tail sampling: loses the middle anyway and needs an LLM; a range read from stored chunks (§14) serves "chapter 12". |
| 5. Image-only and hybrid scans | Silent "indexed" with no text | Zero yield (`no_text`); text density `text_chars / pages_total` | extracted text | `metadata_only / no_text`; OCR in Slice 4 (§5.3) | Per-page routing inside one PDF: deferred; the density field makes it a later refinement, not a migration. |
| 6. Flapping files (Office Online autosave, live vaults) | Re-download/re-embed per save | `modified_at` vs. now | metadata | Quiet period + `MAX_DEFER` (§7.3) | — (rev 1's hysteresis, adopted as is). |
| 7. Information-bearing vs. photographic images | Blind to scans, or a vision bill for every photo | Batched thumbnail triage on every image, camera facet as a prior | thumbnails | §5.3 | File-name and folder patterns (no-convention principle); the camera facet as a verdict (messenger-saved photos have no EXIF). |

---

## 6. Opt-in, disclosure, Cabinet

- **Indexing starts only when the user turns it on** (Cabinet toggle; `UserBotConfig.drive_indexing_enabled`).
  Connecting the drive for Step 1 copies nothing.
- The toggle text says what is copied (names, extracted text, vectors) into our database, that excerpts reach the
  model and stay in operational telemetry for up to 30 days, that scans, images and hard PDFs are read by a model
  provider during indexing (Slice 4), that turning it off deletes the index, and that after a
  disconnect or an expired connection the index is kept for about 30 days in case the
  same drive comes back. Excerpts already shown in chat stay in the conversation history.
- **Turning indexing off purges the index** (an explicit request).
- **Losing access does not purge** (D3, §21). After a disconnect or an expired/revoked grant the index is kept for
  `ACCESS_LOSS_TTL` (30 days) and then purged by the tick (§7.6); reconnecting the same drive in time resumes
  indexing. Tokens drop often and a disconnect can be accidental, while a full
  re-index is slow (the Gmail indexing experience). Connecting a **different** drive purges at once.
- While access is lost: after a deliberate disconnect search returns `disconnected` (the index is hidden); with an
  expired grant search keeps answering from the index, marked `reconnect_needed` with its `index_as_of`.
- Cabinet card: on/off, every non-zero counter (records per state and carded records; readable chunks from Slice 2),
  "index full", last successful sync, last error category, and **Reconnect** when the drive needs it (§7.7). The
  switch is offered only where indexing can run (a drive queue is wired), unless search is already on; the "kept
  about 30 days" and "search still answers from the index" hints show only while an index is kept (no purge running,
  grace period not over). `/api/drive/status` returns `available`, `access_lost_at` and `index_kept` for that (RFC
  Δ44). All texts are `LANG_DICT` keys in en/uk/fr/es; `last_error` is shown as its localised category, never as a
  raw message.
- No manual "sync now", "reindex" or "skipped files" endpoints in the first cut (§14).

---

## 7. Change capture and reconciliation

### 7.1 The tick, its lease and its task keys

**Trigger.** A Cloud Scheduler job fans out every `TICK_INTERVAL` to each user with indexing on, **or with a
`DriveSyncState` but indexing off or no credentials** (so an orphaned index always gets purged). The fan-out skips a
user whose `continuation_enqueued_at` is recent (a continuation is already queued), which bounds the queue at about
one task per user plus nudges and busy retries. For a user with indexing on, a working grant and no sync state, it
also creates a minimal one (`created_at = now`), so the staleness check covers a user whose ticks never land (RFC
Δ28). Payload: `{user_id}` plus an optional nudge (RFC Δ4).

**Task keys** (`dedup_key` families; a name, once used, is reserved by Cloud Tasks for up to a day):
- scheduler: `{user_id}:tick:{bucket}`;
- continuation and `Retry-After`: `{user_id}:cont:{id of the finishing tick}` (unique), with `delay_seconds` for
  `Retry-After` and backoff;
- busy retry: `{user_id}:cont:busy-{lease holder}` — a tick without a nudge that finds a live lease comes back once
  per holder after `NUDGE_RETRY_DELAY_S` (RFC Δ27);
- nudge: `{user_id}:nudge:{mutation id}`, one per mutation, the payload carrying every item id it touched (RFC Δ3).
  A nudge that finds a live lease is not dropped: it re-enqueues itself with a delay under `…:{mutation id}:r{n}`;
- toggle: `{user_id}:toggle:{timestamp}`, from the Cabinet switch in both directions (RFC Δ2).
A dropped continuation (`ALREADY_EXISTS`) is logged at WARNING.

**Lease (single writer).** A tick starts by acquiring `DriveSyncState.lease_until` in a transaction, for
`TICK_DEADLINE + LEASE_MARGIN`. A tick that finds a live lease returns 200 `busy`. This covers concurrent redelivery
after a deadline overrun and duplicate delivery (both seen in this repo, `decisions/worker_task_dedup.md`). A holder
can still outlive its lease (Cloud Run answers 504 at its timeout without cancelling the coroutine), so its writes are
**fenced** (RFC Δ26): the tick writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and
`save_sync_state` and `delete_sync_state` are transactions that apply only while the tick still holds the stored
lease, else `LeaseLost`. A tick that meets `LeaseLost` stops quietly (INFO, status `lease_lost`) and writes nothing
more; the holder carries on from the last saved state. The dedicated queue's `maxConcurrentDispatches = 1` is a CPU
and memory knob for the instance, not the correctness mechanism; raising it later is an extension (§14).

**Deadline.** Every `drive_sync` enqueue passes `deadline_seconds = TICK_DEADLINE`. A new file is started only if
`elapsed + DOWNLOAD_TIMEOUT + EXTRACT_WALL_TIMEOUT + EMBED_FILE_BUDGET + margin < TICK_DEADLINE`; a test asserts this
holds for the configured constants (Slice 2; in Slice 1 no work and no item call starts after `TICK_START_BUDGET`).

**Steps.**
0. **Gate.** Read `drive_indexing_enabled` from the user repository (not the agent cache) and the connection (the
   credentials document exists, `needs_reconnect`, its `drive_id`). Indexing off and anything stored → purge (the
   user's records in batches, then the sync state) and stop. No credentials or `needs_reconnect` → the access-loss
   path of §7.6, and stop. Indexing off and nothing stored → `disabled` before the lease: nothing is read from the
   drive and nothing written. Tasks for such a user still arrive — a nudge after a bot write (nudges do not read
   the flag), a toggle — and end there; a purge enqueues its own continuation until it has finished, and an opt-out
   met mid-tick enqueues one whose gate purges. Purge is keyed on these facts only, never on an exception type: a
   `DriveNotConnectedError` or `DriveReauthRequired` raised during a tick ends the tick without purging (§7.7). The
   gate is decided again under the lease, since the facts may have moved.
1. **Observe.** First the item ids carried by a nudge: each is read with `get_change` and applied as an observation
   with `QUIET = 0` and its card due in class 0, so a bot write is indexed even if delta has not caught up (S0d). Then
   pages from `get_changes(cursor)` until the start budget or `complete`. Compare `drive_id` with the stored one before
   applying any page; on a mismatch, purge and start fresh. Apply each page, then save the cursor (a crash replays the
   page; applying is idempotent). In walk mode, a new walk does not start while the previous epoch is sweeping.
2. **Work off what is due**, after a complete round only (RFC Δ15): the sweep of a finished enumeration, then due
   records in priority order (§7.8), within the budget.
3. **Upgrade.** If no class stopped on its budget and `applied_versions` or the embedding space differ from the
   current ones, run a bounded upgrade step (§12.3), then release capped cards into any headroom (§5.4). Fresh work
   comes first (RFC Δ30).
4. **Continue.** Release the lease; then, if changes or due work remain, enqueue a continuation (after the release,
   so it can never meet its own parent's lease, RFC Δ27).

The gate is re-checked before each write batch (at most every 5 s, RFC Δ19) and before committing an index result,
so an opt-out during a tick drops the in-flight result.

### 7.2 Delta or walk

`get_changes` is the adapter's job. **S0a confirmed (2026-10-11) that delta works on the App Folder under
`Files.ReadWrite.AppFolder`**, so the OneDrive adapter uses it: `full_enumeration = true` only at onboarding and after a
410. **Walk mode is not built.** The rest of this paragraph specifies it as the fallback for a future provider without
a change feed, an adapter extension (§14). In walk mode the adapter walks the tree
within a per-tick budget, never descends into an opaque root (§5.2: dot-folders, folders whose listing contains a
marker such as `.git` or `pyvenv.cfg`, dense trees already recorded as such; it still reports the root and its marker
child), and reports `full_enumeration = true` on every walk; it does not diff. **Walk mode is a degraded mode, not
an equivalent:** every walk is an epoch with a sweep, Graph calls scale with the drive's size instead of its changes,
and freshness for edits made outside the bot drops to one walk's duration. If S0a fails, choosing between living with
walk mode and widening the scope to `Files.Read` (a new consent, the whole drive visible) is an owner decision, taken
with S0a's numbers. In both cases the planner diffs against stored records (`content_version` equal → nothing due), and
every full enumeration is an epoch (§7.5).

**Delta semantics the planner relies on** (each observed in S0a):
- Changes are applied in feed order, so the last occurrence of an item wins. A delete and a recycle-bin restore of the
  same item arrived in one round: the tombstone first, then the live item with the same id.
- A deletion is decided by `item_id` alone, before any name or kind policy.
- The App Folder root never becomes a record. It appears in every round, with a `specialFolder` facet and without a
  `root` facet, so it is recognised by `root_item_id`, never by a facet.
- **A move out of the App Folder is a deletion.** The tombstone carries the item's id, and a GET by that id returns
  404 `itemNotFound`, the same as a real delete. **A move in is a first sighting** of an item with its existing id.
  A move made in the web UI reached the feed within a few minutes, not instantly.
- **Unchanged items are re-emitted often.** One round with two real moves carried 41 entries, the same item up to
  three times. The `content_version` comparison keeps such an entry to a record read and nothing more. The records a
  page touches are read in one batched read.
- Deleting a non-empty folder produced a tombstone for every descendant on OneDrive personal. The cascade (§7.6) stays,
  because the feed does not promise this.

### 7.3 Office Online autosave: the quiet period

A content change on an admitted record sets `dirty_since = now` if it is unset, and
`due_at = min(modified_at + QUIET, dirty_since + MAX_DEFER)`: due when the file has been untouched for `QUIET` or
dirty for `MAX_DEFER`, whichever comes first. `dirty_since` is cleared when the record reaches a terminal state
(`indexed`, `failed`, `metadata_only`) or is deleted. Each autosave only moves `modified_at`; churn costs one metadata
write per observed change. A first-seen file and a file named in a nudge (the bot's own atomic write) use `QUIET = 0`.
`modified_at` is the item's server-set `lastModifiedDateTime`; `min(…, dirty_since + MAX_DEFER)` bounds any skew.
S0b measured it: during 11 minutes of continuous typing in Word for the web, `cTag` moved every 1.2–3.3 minutes, and
intermediate versions were skipped. A rename or a move leaves `cTag` unchanged and moves only `eTag`.

### 7.4 The read fence

An index run takes `generation = index_generation + 1`, writes its chunks under that generation, then sets
`indexed_generation`, `indexed_version` and `extraction_version` in one record write, then deletes chunks of every
other generation. Search counts a chunk only if its record exists, is not `deleting`, and
`record.indexed_generation == chunk.generation`. Consequences:
- a re-index at the same `cTag` (chunker change, fewer chunks) never serves a mix of generations;
- a crash mid-write leaves the previous generation readable and the partial one invisible;
- a deleted or demoted record's chunks are invisible at once, before collection.
Before the record write, the tick re-reads the record and drops its result if `content_version` moved on meanwhile;
with the lease this plain check is sufficient.

### 7.5 Full enumeration (onboarding, expired cursor, walk)

`epoch_phase`: `idle → enumerating(E) → sweeping(E) → idle`. **Every observation, in any phase, stamps
`seen_epoch = sync_state.epoch`** (a feed item, a nudge read, a re-listed child), so a record created during sweeping
(a bot save, a nudge) carries E and survives. A re-plan of a stored record (rescope, parent resolution, probe
completion, upgrade, cap release) is not an observation and keeps its own stamp, so a record the drive no longer has
cannot escape a sweep that spans ticks by being re-planned.
When the enumeration completes, `sweeping(E)` deletes records with `seen_epoch < E` in bounded, resumable batches,
and also deletes chunks whose record is missing or whose generation is not the record's `indexed_generation` (the
backstop for every crash window). The phase returns to `idle` only after the last sweep batch. A 410 in any phase
abandons epoch E without sweeping and starts `enumerating(E+1)`. Onboarding is epoch 1;
`initial_enumeration_done` is set when it reaches `idle`. In walk mode an item moved into an already-walked folder
during a walk can be swept and is re-indexed by the next walk; on a disposable index that cost is accepted.

### 7.6 Deletion, folder moves, purge

- **File deleted** → `delete_record`: delete its chunks (all generations), then the record; the cursor is saved after
  both.
- **Folder deleted** → the folder record becomes `deleting` (the durable marker; no `pending_cascades`, RFC Δ14). The
  cascade runs only when the delta round reports `complete`, following Graph's advice to delete a folder locally only once it is empty
  after all changes are synced: children that a later change re-parented are spared. It then deletes descendants
  post-order (files first; subfolders become `deleting` before their children are removed) within the budget, and
  deletes the folder last. A crash or a spent budget leaves `deleting` records due, so the next tick resumes. A
  folder whose children are all deleting folders outside the page being worked steps aside one second behind them,
  and a step that deletes nothing charges no budget (RFC Δ29).
  A child whose parent is `deleting` is hidden (§5.2 read-time check) and is removed by the cascade unless it is
  re-parented first.
- **A `deleting` folder observed again as not deleted** (a later page undoes the delete, or a recycle-bin restore that
  keeps the id, S0c) leaves `deleting`, is re-classified, and gets the re-listing flag: descendants the cascade
  already removed come back through the subtree re-listing (§5.2).
- **Folder renamed or moved** → only its record changes, unless its scope changes (§5.2). Cards carry no path;
  paths are derived at read time.
- **Purge** (opt-out, or a reconnect to a different `drive_id`) runs only inside a tick, under the lease. It first
  writes `purging = True`, so a started purge always finishes before anything else, then deletes the records in
  batches within the tick's budget (continuing in-chain) and the sync state last. Toggle-off flips the flag and
  enqueues a `toggle` task (RFC Δ2); the queue runs it after any tick already in flight. Search reads the flag
  uncached (§10.1), so the index is invisible at once.
- **Access lost** (no credentials document, or `needs_reconnect`): the gate sets `access_lost_at` once (a new loss
  while one is recorded keeps the earlier value) and stops. Nothing else is written, so a token blip costs nothing.
  The fan-out keeps visiting the user (§7.1); once `now ≥ access_lost_at + ACCESS_LOSS_TTL` the tick purges the index
  in bounded batches under the lease. No Firestore TTL and no per-document stamps: the only case they would add is a
  scheduler dead for 30 days, in which the whole system is down and the scheduler alert has long fired.
- **Access regained** (same `drive_id`): before `access_lost_at + ACCESS_LOSS_TTL`, the tick clears `access_lost_at`
  and **continues from the stored cursor** (nothing was deleted; a cursor that really expired still arrives as a 410
  and gets its epoch). At or after it, the purge has started or finished, so the tick completes the purge and starts
  epoch 1.

### 7.7 Revoked access and throttling

- `DriveReauthRequired` (the refresh was rejected: `invalid_grant`, or `interaction_required` / `consent_required`,
  RFC Δ41) ends the tick without purging and sets `needs_reconnect` on the `microsoft_onedrive` credentials record
  (Gmail precedent, `decisions/gmail_grant_revoked_reconnect.md`); `/api/drive/status` returns it and the Cabinet
  shows **Reconnect**. A successful save of credentials clears it. **This flag is new work**: Step 1 has none.
- The user is notified by the fan-out on the main service, which has chat delivery (the indexer has none, RFC Δ6):
  `reconnect_notified` goes up before the send and stays up once a send was attempted, delivered or not, so a notice
  cut off after it left is never repeated; when no send was attempted (no channel, a formatter failure, `notify`
  raising) it comes down and the next fan-out retries. A good grant again clears it.
- A deliberate disconnect removes the credentials document; the next tick's gate sees `is_connected() == false` and
  starts the access-loss grace period (§7.6). No exception type ever purges; an access loss purges only after
  `ACCESS_LOSS_TTL`.
- Throttling and systemic errors end the tick and enqueue a continuation with a delay. A Graph 429/503
  (`DriveThrottled`) waits the provider's `Retry-After` (capped at `TICK_DEADLINE`; 60 s when absent or for a failed
  token refresh) the first time, and `max(Retry-After, backoff)` when it repeats; an embedding 429/503 or any other
  systemic error waits the backoff, `min(60 · 2^n, 1800)` s over `n` consecutive systemic failures (§8.2).

### 7.8 Priorities

`due_records` orders by `(priority, due_at, item_id)`:
0. **edits and nudges**: content changed on a known record, a card that changed on a known record, items named in a
   nudge;
1. **cascades and structure**: folder delete and rescope steps (metadata operations only, no download), up to
   `CASCADE_OPS_PER_TICK` records; density probes, parent resolutions and card drops share the class (probes and
   lookups spend `ITEM_CALLS_PER_TICK`, RFC Δ9); search is already correct through the read-time check (§5.2), so
   cascades only do the physical work;
2. **first seen**: onboarding, re-observed subtrees (the folder's re-listing flag, RFC Δ18), bulk drops. A
   first-seen record's card waits until the round that delivered it has completed (RFC Δ15: all work-off does), so a
   `.git` or `pyvenv.cfg` that arrives later in the same round makes the folder opaque before any of its files is
   carded;
3. **maintenance**: version upgrades and newly admitted kinds (§12.3), cap releases;
4. **retries** with backoff.
A tick works class 0 first, then 1, then 2, and gives classes 3–4 at most `MAINTENANCE_SHARE` of its start budget's
time. When a class runs out of its budget, lower classes wait for the next tick (RFC Δ19). An edit or a bot write
therefore never waits behind a cascade, onboarding or an upgrade once its round is complete; its worst-case freshness
is about *(users with class-0 work) × tick budget*, and `STALE_ALERT_AFTER` stays above it.

---

## 8. Indexing a record

### 8.1 Steps

For each due `pending` record, within the budget:
1. Policy check on metadata (§5).
2. Persist, in one record write, `index_generation += 1` and `attempt_started_at = now`. Every chunk of this run is
   written under that generation, so a retry never reuses a crashed run's generation.
3. Download (per-kind size cap checked on metadata first; the task carries `item_id`, never the short-lived download
   URL). The parent drops its in-memory copy once the temp file is written; the temp file is deleted in `finally`.
4. Sniff kind from magic bytes; a mismatch goes back through the policy.
5. Extract through `ContentExtractorPort` (§9).
6. Text-quality gate (§5.5, class 3) on non-table text. Subtitle formats (VTT, SRT) are first normalised to cue text
   (cue numbers, timing lines and blank separators removed — also before chunking). The gate then strips a leading
   timestamp, speaker tag or IP from each line, **ignores lines that are only a timestamp or a number**, and measures
   the remaining non-empty residue: repetition counted over lines of at least 20 characters (short replies like "ok"
   do not count), share of near-empty residue lines, token variety. Machine-like residue →
   `metadata_only / machine_text`, card keeps the lead text. Meeting transcripts (VTT/SRT, `[00:01:23] Speaker:`), chat
   exports and dated journals leave prose after the timestamp and stay indexed. Deterministic (§8.2).
7. Chunk (§8.4) up to `MAX_CHUNKS_PER_FILE`; set `truncated` if cut.
8. Embed chunks with at most `EMBED_IN_FLIGHT` concurrent calls, and the card (§8.5).
9. Commit through the fence (§7.4); clear `attempts` and `attempt_started_at`; collect older generations.

### 8.2 Outcome classes

| Class | Outcomes | Effect |
|---|---|---|
| Deterministic | `no_text`, `machine_text`, `encrypted`, `unsupported`, `too_large`, ZIP caps exceeded | Terminal at once: `metadata_only` with the reason. No retry, no re-download. |
| Per-file transient | `download_error` (not 429/503), `extract_error`, `timeout`, `memory`, `embed_timeout` (the file's embeddings exceeded `EMBED_FILE_BUDGET`), and a **crashed attempt** (a record found with `attempt_started_at` older than `TICK_DEADLINE` and no outcome: the instance died during it) | `attempts++`, `attempt_started_at` cleared, `due_at = now + min(RETRY_BASE · 2^attempts, RETRY_CAP)`, class 4; at `MAX_ATTEMPTS` → `failed`. |
| Per-item provider call | 5xx, `DriveAccessDenied`, a transport error or timeout on a call about **one item** — a density probe, `get_change` (a nudge read, a parent lookup), a subtree re-listing page, a download | The page is still applied; the item's own work is deferred with backoff (its children held `unresolved`, the record kept `pending`). After `MAX_ATTEMPTS`: a probe → classify without a probe (not dense); a parent lookup → keep retrying every `RETRY_CAP`, never delete (only a definite not-found removes an orphan, RFC Δ29); a re-listing → the folder's re-listing flag is dropped; a nudge read → left to the delta feed; a download → `failed / download_error`. A change whose observation fails inside a page is skipped with an ERROR line. One bad item never stalls the user's index. |
| Per-record provider refusal | The embedding provider refuses one card text (HTTP 400, `LLMClientError`) | That record backs off alone (`attempts++`, class 4); the rest of the batch is stored. At `MAX_ATTEMPTS` the refusal is terminal: flag `card_refused`, no card, `card_hash` names the refused text and `card_version` is stamped, the state stays structural; a new card text or a card-builder bump gets a fresh try (RFC Δ24). |
| Systemic | Graph 429, or 5xx/`DriveAccessDenied` on the **drive-wide** feed (`get_changes`); a failed or timed-out token refresh; any embedding error but a 400 (quota, 5xx, timeout, network); repository errors | End the tick, after committing the vectors and refusals the card batch already obtained; `attempts` untouched and `attempt_started_at` cleared; continuation with the backoff of §7.7; `last_error` set; `last_success_at` not advanced. |

`attempts` is incremented only by the per-file class, never before the outcome is known, so a sequence of systemic
errors of any length never moves a record to `failed` (property test, §17.1). It resets when `content_version` or
`extraction_version` changes, and after each successful step of a record's structural work (a parent resolved, a
probe, a re-listing page), so the next step counts its own `MAX_ATTEMPTS`.

### 8.3 Tables

XLSX/XLS/CSV/ODS are read row-capped (`MAX_SHEET_ROWS` per sheet); continued table chunks repeat the header row.
Search returns them as evidence of *where* data lives, not as data to sum (§10.4).

### 8.4 Chunker (`src/domain/drive_chunker.py`)

Split extracted text into chunks of about `CHUNK_CHARS` at paragraph boundaries, falling back to sentence and then
hard boundaries, with `CHUNK_OVERLAP`; carry the header row into continued Markdown tables. A chunker change bumps
`extraction_version` (§12.3). Heading breadcrumbs are a later improvement of the same function (§14).

### 8.5 Card

`card_text(record)` is pure over record fields: `name | kind | key facets` plus, for indexed and `machine_text`
records, the first `CARD_LEAD_CHARS` of text; folders `name | folder`; opaque roots `name | repository`,
`name | hidden folder`, `name | generated files` or `name | generated files (500+ items)` (§5.2, RFC Δ33). OS
artefacts have no card (RFC Δ32). Key facets are a fixed subset of stored facets (photo
`takenDateTime`, camera make and model, the derived `place_name` (§5.3), image dimensions, video dimensions and
duration), finalised by S0e. The `audio` facet is not used: S0e saw none on M4A or WAV files. A new geocoding table is a `geo_table_version` bump in the upgrade step (§12.3): it
re-resolves `place_name`, and since the card hash covers the card text, only cards whose place name actually changed
are re-embedded. **No path**, so a folder rename does not touch descendants. The planner makes
the card due whenever `hash(card_text(record), card_version)` differs from `card_hash`: on rename, on facet change, on
first sight, when indexing adds lead text, when a demotion removes it, and when the card builder changes. A rename
therefore costs one card embedding and zero chunk writes.

### 8.6 Cost bounds

Embedding (`gemini-embedding-2`, 768-dim): a 100-page document is about 100 chunks and 100 calls (the adapter's
one-call-per-text quirk), cents at most. The dominating recurring cost is Firestore kNN (flat index, billed 1 read per
100 index entries scanned under the user filter), linear in the user's chunk count; hence the per-user caps (S3
measures it).

---

## 9. Extraction isolation

- **Why a child process.** Phase 0 measurements on markitdown 0.1.4: a 66 KB DOCX peaked at ~950 MiB RSS, a 5 KB XLSX
  at ~1.75 GB, an 8.7 KB legacy XLS at ~3 GB, a 408 KB PDF at ~955 MiB. Parsers amplify, so no size check on the
  input bounds memory. Only a process with a memory limit and a kill does.
- **Extraction quality is a known limit.** markitdown's PDF path (pdfminer) loses table structure and mixes two-column
  layouts; for G2 over contracts and invoices this is the largest quality factor after retrieval. Phase 0 made
  extraction safe, not good. The eval includes table-heavy and two-column PDFs; a better PDF extractor is another
  `ContentExtractorPort` adapter plus an `extraction_version` bump (§14).
- **Extractor chains per kind.** Which extractor reads which kind is a pure domain table,
  `EXTRACTOR_CHAINS: kind → [extractor_id, …]` (e.g. PDF `[markitdown, model]`, DOCX `[markitdown]`, RTF `[rtf]`);
  the next link runs only on the "parser failed" signals below. Each `ContentExtractorPort` adapter has an id and a
  version; composition builds the registry `{id: adapter}`. Trying another parser for one kind (e.g. `pymupdf4llm`
  or `docling` for PDF) is a new adapter plus one table edit. The record stores `extracted_by` (`id@version`), and the
  upgrade step (§12.3) compares it with the current chain **for that record's kind**, so changing the PDF extractor
  re-extracts PDFs only. Per-user overrides are a later extension, not needed now.
- **The model extractor (Slice 2+4; model, agent shape, windows, brief, budget and failures in §24 — that section
  overrides this bullet).** The second `ContentExtractorPort` implementation sends a PDF or an image to a multimodal
  model that reads it natively and returns its text verbatim, tables as Markdown tables. It serves scans and screenshots (the OCR of §5.3), hybrid PDFs and PDFs whose parser yield is
  poor. **Router, parser first:** the free deterministic parser runs first; "the parser failed", and the model is
  called, means exactly one of:
  1. `no_text` — no text layer (a scan);
  2. low density — `text_chars / pages_total` below `MODEL_EXTRACT_DENSITY` (a hybrid PDF, or only headers and page
     numbers);
  3. garbage text — the share of `(cid:N)` sequences, `U+FFFD` replacement characters and mojibake patterns above
     `MODEL_EXTRACT_GARBAGE` (fonts without a Unicode map, common in Cyrillic PDFs: plenty of "text", none readable);
  4. a parser `error` or `unsupported` on a PDF (a damaged or unusual file) — one model attempt before the per-file
     retry budget of §8.2; `timeout` and `memory` are not routed (they are bombs, and stay failures).
  Plus triaged information-bearing images. Not detectable and therefore not routed: readable text whose tables or
  columns the parser scrambled — that is the §14 extension. Rules: a "transcribe verbatim, do not summarise or correct" instruction with deterministic settings; no
  tools, text output only, so a hostile document can at worst corrupt its own text, which is served inside the
  untrusted wrapper anyway; concurrency bounded like embeddings (`EMBED_IN_FLIGHT`, shared Gemini quota); pages per
  file capped by `MODEL_EXTRACT_MAX_PAGES`; **pages per user per month capped by `MODEL_EXTRACT_PAGES_PER_MONTH`**
  (a layer-0 bound, §5.5: a drive of 25 000 scans cannot burn vision calls for months) — over the cap a file waits as
  `pending` in class 3 until the next month, and the pages sent to the model are a counter in the tick summary line
  and in `DriveSyncState.counters`; disclosed in the opt-in text (§6).
- `ContentExtractorPort`, first adapter `SubprocessContentExtractor`: the converter runs in a child Python process with
  `RLIMIT_AS`, `RLIMIT_CPU` and a wall-clock kill (`EXTRACT_WALL_TIMEOUT`); a kill maps to `memory` or `timeout`.
  Converters: markitdown with builtins off and a local-document allowlist, ZIP central-directory caps, XLSX row caps
  (Phase 0).
- **Host: a separate indexer service** (owner decision D5). A second Cloud Run service from the same image runs the
  whole `drive_sync` tick — the drive queue targets its URL — with about 2 GiB, `concurrency = 1`, scale to zero and
  `--execution-environment=gen2`. The main service (1 GiB, already peaking at 74–90 % memory) never indexes. Precedent:
  the voice relay as a second deploy unit. S1 sets the memory and the child limits on this service; its results hold
  only for the pinned environment. Same deploy discipline as the relay (own env, no reuse of dual-purpose `.env` keys).
- The chat path, Step 1 `open_file_from_drive` and email attachments move to the same port in Slice 3: the indexer
  service exposes an OIDC-protected `/extract`, and the main service's adapter calls it — one hardened way to turn a
  user file into text.

---

## 10. Search and the agent surface

### 10.1 `DriveSearchService`

Input: `query`, optional `folder` (a `drive:` ref or a path), `kind` (a format, an extension or a family, RFC Δ20),
`date_from`, `date_to` (on `effective_date`; a date-only bound is a whole UTC day). **Filter-only requests** ("files
I saved this week", "all my spreadsheets") have no name words in `query` (empty, or punctuation only): the service then
runs a plain Firestore query on `user_id` + `kind` / `effective_date` (one per member kind of a family), ordered by
`effective_date` descending, limit `K`, with no kNN and no threshold, and says how many matched (a lower bound when
the fetch hit its cap). A request with no name words and no filter is **refused** with a correction, never answered
with the newest files (RFC Δ36). The manifest field description tells Smart to put name words, a place or a subject
in `drive_query` (the delegate's `query` is not read for search), never type words or dates (Slice 2+4: dates
written inside a document do go in it, §24.10).
0. Read the opt-in flag, `DriveSyncState` and the credentials state from Firestore, never from the agent cache. Off →
   `disabled`. No credentials → `disconnected`. `needs_reconnect` with `now ≥ access_lost_at + ACCESS_LOSS_TTL`
   → `disconnected` (the purge has begun). A purge in progress, or the TTL passed on a connected drive → no results,
   `building`. A connected `drive_id` (a field of the
   credentials record written when the OAuth grant is saved; credentials are rebuilt on every refresh, so the refresh
   carries the field over; absent for a user connected earlier until that refresh, in which case the comparison is
   skipped) different from the one in `DriveSyncState` → `disconnected`, saying a different drive is connected and is
   being indexed, until the purge runs. `needs_reconnect` → search proceeds and the envelope carries `reconnect_needed`.
1. Embed the query once.
2. **Card stream:** kNN over records, pre-filter `user_id` (+ `kind`; a family runs one kNN per member kind, merged by
   distance, RFC Δ37), fetch `CARD_FETCH`. Skipped while the index's cards are in another embedding space than the
   query's; the envelope then says `refreshing` (§12.3, RFC Δ25).
3. **Chunk stream** (Slice 2): kNN over chunks, pre-filter `user_id` (+ `kind`), fetch `CHUNK_FETCH` (≤ 1000). Drop
   chunks failing the fence (§7.4); max-pool to one hit per record (best chunk).
3a. **Name stream (lexical, from Slice 1):** the query's name keys (the same normalisation as `name_tokens`, with a
   small set of function words dropped from the query, RFC Δ38). Each whole-token key and each trigram key gets its
   own `array-contains-any` query, at most `NAME_KEY_QUERIES` of each in query order, plus one query with the
   remaining keys (at most 30), in parallel, merged by id, ranked by how many keys each record shares (RFC Δ17).
   Catches exact and partial names (`IMG_4912`, `ред3`), transliteration and typos, where a cosine over the card
   vector is weak. "Find a file" is Slice 1's core, so this stream is part of the base, not a later extension.
4. **No cosine thresholds** in the design: each stream contributes its top results by rank, RRF and `K` bound the
   output, and Smart judges relevance from the cards (the honest envelope covers "not found"). The name stream keeps
   only records sharing at least one whole token or `NAME_MIN_TRIGRAMS` trigrams. Then read-time visibility (§5.2),
   post-filters (kind, dates, folder), then RRF
   (`apply_rrf_ranking`, `k = 60`, key `item_id`). Pooling before fusion is required: `rrf.py` keeps the first object
   seen per key. After RRF, records whose name covers every query token rank first, RRF order kept within each group
   (RFC Δ17).
5. If fewer than `K` records survive and a stream page was full, widen once (fetch at the cap). When the filters
   then dropped candidates from a full page, the result is `capped`: the answer says the filters saw only the best N
   matches, and with no survivor never says "no matching files" but how to search all files (RFC Δ37).
6. Collapse duplicates: cards with the same `content_hash` (files with content only: every empty file shares one
   hash) become one card with `also_at` paths.
7. Return the top `K` cards plus the coverage envelope (§10.2).

**Folder** means the subtree of one folder record, resolved by ref, or by path matched after NFC + casefold
(root-anchored first, then folders named like the last segment anywhere). If several
folders match, return them as candidates instead of guessing; a single suffix match from a full name page is
"could not resolve", not a guess. A folder that hides its contents (an opaque root, inside one, or deleting) answers
that its contents are not indexed and points to `list_files_in_drive`, instead of "no match". A folder-scoped query
walks the subtree via `children`
with projection reads (no vectors), never entering opaque roots, **stopping after `FOLDER_INMEM_CARDS` records
read**. If the walk completed, the
subtree's cards are ranked in memory, and its chunks too when `Σ chunk_count ≤ FOLDER_INMEM_CHUNKS`. Otherwise the
user-wide streams are used and each candidate is kept only if its ancestor chain — already walked to build its path
(§10.2) — passes through the folder, with the widening of step 5.

One summary log line per search (`[DriveSearch]`): counts after each stage (card and name fetched, name queries,
name kept, visible, filtered, fused, covered, widened, collapsed, returned), `building`, and the elapsed `ms`.

### 10.2 Result contract

**Envelope:** `enabled`; `content_search` (false in Slice 1); `building` — the set of existing items may still be
incomplete: no sync state or root yet (onboarding), a purge running, an epoch not `idle` (onboarding or after a 410),
any class-2 record due (first-seen cards and folder re-listings, RFC Δ18), or an `unresolved` record at class 1 (its
parent lookup or probe in progress) — each derived with one `limit 1` query, never a counter that could drift. Edits
(class 0), other cascades, and unresolved records waiting on a `RETRY`-class lookup or parked do not set it, or a
parent that is never found would keep it true for good. Routine walks do not count. `refreshing` — the cards are in
another embedding space, so only names and filters were searched (RFC Δ25). `reconnect_needed`; `index_as_of`
(`observed_as_of`, §12.1 — never the last tick); `unread` — documents of an **admitted** kind whose
content is not searchable: `pending` without a readable generation, `failed`, and `metadata_only` for `too_large`,
`no_text`, `machine_text`, `user_chunk_cap` (photos, code, archives and other non-admitted kinds are not counted; 0 in
Slice 1); `index_full`.

**Card:** `ref` (`drive:<item_id>`), `name`, `path` (derived from the `parent_id` chain at read time, ending at
`root_item_id`; an ancestor not yet recorded ends the path at the last known one and keeps the card; a `deleting`,
`opaque_root` or `inside_opaque` ancestor drops the card, §5.2), `kind`, `effective_date`, `modified_at`, `web_url`,
`state`/`reason`, `stale`, `truncated`, `also_at`, `matched` (`card`, `content` or both), and for content hits a
window: the best chunk and its neighbours `index ± 1` from stored text. What Smart reads per card, inside its
wrapper: the card text (`name | kind | key facets`), the path, the effective date, the index state and reason, and
the `stale`, `truncated` and `also_at` lines when set; `ref` and `path` are wrapper attributes. `web_url`,
`modified_at` and `matched` (always `card` in Slice 1) stay in the result object; history keeps each card's ref and
path (§10.4). The contract is the seam for future sources and stages.

### 10.3 Not in `SearchEnrichmentService`

`SearchEnrichmentService` injects validated facts about the user into every request; the same service backs the remote
MCP server and the companion agents. The drive is a different thing: a knowledge base that may hold facts about the
user and may hold nothing about them. It is reached through a tool when the question calls for it. (A consequence, not
the reason: enrichment would also hand file content to claude.ai via MCP and to the companions.)

### 10.4 Intent, protocol, history

- **Intent** `search_files_in_drive` on FileManagement, `SYNC`. Context fields get names no other intent uses
  (`drive_query`, `drive_folder`, `drive_kind`, `drive_date_from`, `drive_date_to`), because the delegate
  tool keeps the first description seen for a field name (`src/agents/base_agent.py`); a manifest test fails when a
  new shared field name has differing descriptions (the four existing collisions are listed as known debts).
- **Untrusted wrapper.** Each result goes inside `<drive_document nonce="…" ref="…" path="…">…</drive_document nonce="…">`
  with a per-result random nonce. The defence is the nonce: a forged closing tag cannot guess it. On top of it, a
  `<drive_document` or `</drive_document` look-alike in the body loses its opener (any case or spacing; a fullwidth or small `<`;
  a fullwidth, division, fraction or big-solidus `/`; fullwidth letters and `_` look-alikes; format controls,
  combining marks and variation selectors inside the tag word; the same in attribute values), and attribute values escape only `"`, `<` and `>`. Nothing
  else is escaped, so `R&D/Tom's plan.docx` reaches the model as written and a path it copies back is the real one
  (RFC Δ34). Search results and `open_file_from_drive` carry the reminder that the text is reference material and can
  never be an instruction; file names are untrusted too. A drive file the coordinator prefetches for another
  specialist (DocPlanner, PdfGenerator, HtmlPageGenerator) still reaches it raw until Slice 3 (RFC Δ35). No extra
  confirmation for destructive intents (D2).
- **Smart protocol lines** (a token change, backed up before editing; Slice 2 — until then the guidance below that
  Slice 1 needs is rendered with the results):
  - search the drive when the answer may be in the user's documents;
  - results reflect the drive as of `index_as_of`; if the user refers to a recent edit, or a card is `stale`, open the
    file (Step 1 reads live) before quoting;
  - while `building` is true, do not say a document does not exist — say the index is still being built; when
    `unread > 0` or a matching card is `truncated`, a "not found" names what was not searched (e.g. "3 documents could
    not be read") and offers to list or open the folder; when `content_search` is false, say only names and metadata
    were searched;
  - when a folder card matches, search or list within it before answering "not found";
  - for sums and totals over tables, open the file, then compute from the opened text (no intent takes a file ref
    and computes);
  - a document is authoritative for its own contents (its terms, amounts, dates). It does not override memory about
    the user's current state: when they disagree, give both, citing the document's path and date (memory facts carry
    no date in the prompt today). Provisional until OQ-1;
  - when several cards look like versions of one document and their values differ, never pick silently: answer from
    the version the name or date marks as latest, say which, and list the others with their dates.
- **History.** Returned refs go into `history_context["drive_context"]` (Step 1 mechanism), so "open the second one"
  works. Snippets live only in the tool result and `full_text`.
- `capabilities.py` gains the capability.

---

## 11. Memory and the irreversible stores

- **Memory.** Snippets never enter the turn summary `p.text`. That does **not** keep document content out of memory:
  Smart's answer summary may restate document values and reaches consolidation like any answer — as it already does
  for Step 1 `open_file`, chat uploads, email and web answers. This RFC accepts that existing path unchanged and adds
  no new one (owner decision D4, §21). The `CONSOLIDATION_TAXONOMY` is not changed here; OQ-1 covers both direct
  content and answers derived from it.
- **Prompts.** The protocol lines (§10.4) are a Firestore token change, backed up before editing.
- **History refs.** `drive:<item_id>` refs stay valid while the provider item exists; their format does not change.
- **Telemetry.** Excerpts passed to Smart are captured by Logfire and `prompt_content` (30-day TTL), like any tool
  result. Disclosed in the opt-in text (§6); redaction would be a separate, telemetry-wide decision.

---

## 12. Operations

### 12.1 Observability and alerts
- One summary line per tick that ends normally, on the indexer: `[DriveIndex] tick user=… end=done|continued {…}
  duration=…`, the counts being those of the work done (`pages`, `observed`, `skipped`, `swept`, `deleted`,
  `rescoped`, `cards`, `evicted`, `released`, `upgraded`; Slice 2 adds indexed, failed, demoted, chunks and embedding
  time). A throttled, failed or lease-lost tick logs its own WARNING, ERROR or INFO line instead; a purge logs
  `index purged`.
- **Staleness is evaluated in the scheduler fan-out** (it runs even when the drive queue is stuck): a user with
  indexing on and access (no `access_lost_at`) is stale when no tick has ended without a systemic error for
  `STALE_ALERT_AFTER` since the last success, or since the sync state was created (the fan-out creates a minimal one
  for a user whose ticks never land, RFC Δ28), or a **class-0**
  record has been due for longer than it. Cascades, onboarding and upgrades (classes 1–4) never trip it. The check posts to
  `AlertSinkPort` on the False→True transition of `stale_alerted` and clears the flag only when the criterion itself is
  false again (one alert per incident, no flapping).
- `observed_as_of` is the start time of the last **complete** round — a delta round, or a walk whose sweep has
  finished — set when that round ends; it is what search shows as `index_as_of`.
- `last_success_at` advances on every tick that ends without a systemic error and either made progress or found
  nothing to do. It drives the staleness alert only; search never shows it.
- `DriveSyncState.counters` hold records per state and carded records (readable chunks from Slice 2), refreshed when
  a tick finds nothing left to do; embedding usage stays in the log line.
  Embeddings are unbilled system-wide today; pricing them is a separate decision.

### 12.2 Rebuild
`scripts/infrastructure/drive_index_rebuild.py --user <id> [--apply]`: under the user's lease (refused while a tick
runs), it marks the sync state `purging`, so the next tick takes the purge-then-restart path — purge, then epoch 1
(§7.6). Dry run by default. Tested on the in-memory repository through to the rebuilt index; the documented answer to
"the index looks wrong". A drive switch purges on its own (§7.6). It is not the version-migration path.

### 12.3 Versions and rolling upgrades
- `policy_version` (admission, scope), `extraction_version` (chunker and shared extraction rules; the extractor
  itself is versioned per kind through `extracted_by`, §9), `card_version` (card builder),
  `geo_table_version` (place names), `name_keys_version` (the name-stream keyer, RFC Δ31), `triage_version` (image
  triage, Slice 4) are domain constants; every record stamps the versions its state was decided under. The Slice 1
  version map is `{policy, card, geo_table, name_keys}`. The embedding space (model, dimension) is tracked beside it:
  each card stamps the space it was embedded in, and the sync state the space all its cards are in (RFC Δ25).
- When `DriveSyncState.applied_versions` differ from the constants, or `applied_space` from the service's space, each
  tick whose due work is done runs a bounded **upgrade step** (§7.1 step 3): it pages through the user's records by
  `item_id` (`upgrade_cursor`) and hands stale ones to the planner, which picks the
  cheapest work — re-classification only, card re-embed, re-extraction, or re-embedding stored chunk text for a model
  change — and schedules it in class 3 (maintenance, §7.8). `failed` and `metadata_only` records are included, so a
  fixed converter or a newly admitted kind takes effect; a record parked under an unresolved parent is left to its
  parent's rescope. A further bump during a pass sets new `target_versions` (or `target_space`) and restarts the pass
  from the first record (it is idempotent). `applied_versions` (and `applied_space`) are updated when a **full pass
  finds no stale record** — scheduling is not completion, and no count query is needed. A pass that left stale
  records waits until due work has run or the target changes before verifying again, so a record stuck in backoff
  never costs a full read of the index per tick. A new sync state starts applied: onboarding builds every record under
  the current constants (RFC Δ30). Records whose work is blocked by a cap (`user_chunk_cap`, `card_capped`) are
  re-stamped without work: their outcome is decided again when they are released (§5.4).
- Old chunks stay readable through the fence until the new generation commits. **An embedding-space change**
  (Slice 1, RFC Δ25): a card embedded in another space is version-stale, so the pass clears its hash and the card is
  re-embedded like any changed card, its old vector serving until the new one replaces it; while `applied_space`
  differs from the query's space, search skips the card stream and answers from names and filters, saying so
  (`refreshing`). **Not built:** a second vector field written next to the old one during the transition (Firestore
  vector indexes are per field), with search switching fields when a full pass finds none left; it is an extension for
  the day a model change must not degrade search for the length of a pass
  (`decisions/drive_embedding_dual_field_deferred.md`).
- Expected duration of a full re-extraction ≈ records ÷ files per tick × tick duration, shared with fresh work by
  `MAINTENANCE_SHARE`; a rollback costs the same as the bump it reverses.

### 12.4 Maintenance hotspots
The density probe (§3.6, §5.2) is correct and isolated but is where future maintenance cost concentrates (the
access-loss machinery was simplified to a grace period in rev 3.10 for the same reason). Changes there get property tests first;
incidents there are the first signal to simplify.

### 12.4a Configuration
Parameters (§15) are domain constants. The opt-in flag is per-user config. No new `.env` keys pass through Cloud Build
substitutions (deploy-substitution trap).

### 12.5 Provisioning (reproducible, in the repo)
- **Queue** `drive-sync-{suffix}`: created and updated in `cloudbuild-dev.yaml` with the describe → update || create
  pattern used for scheduler jobs, pinning `max-concurrent-dispatches=1`, the retry config and log sampling.
- **Scheduler job** for the fan-out: in `cloudbuild-dev.yaml` and `docs/07_deployment/SCHEDULERS.md`.
- **Firestore indexes** in `config/firestore.indexes.json`, for `development_drive_index_records` only (RFC Δ22):
  card vector indexes with `user_id` and `user_id + kind` pre-filters; `user_id + name_tokens (array) + item_id` and
  `user_id + kind + name_tokens + item_id` for the name stream; `user_id + effective_date` and
  `user_id + kind + effective_date` for filter-only queries; `user_id + priority + due_at + item_id` for
  `due_records`; `user_id + parent_id + item_id` for `children`; `user_id + item_id` for `records_page`;
  `user_id + seen_epoch + item_id` for the sweep; `user_id + has_card_vector + card_rank (+ modified_at)` for card-cap
  eviction; `user_id + card_capped + card_rank + modified_at` for cap release (RFC Δ23). Eleven composites and two
  vector entries; the equality-only queries (`count_carded`, `count_by_state`, `has_record_in`, a purge's batches)
  are served by single-field indexes, and the sync-state collection needs none. Chunk indexes come with Slice 2.
  `make deploy-indexes` creates the composites; the vector entries are created by
  `scripts/infrastructure/create_drive_vector_indexes.sh` (the deploy script skips vector indexes), and
  `scripts/infrastructure/drive_index_smoke.py` runs every repository read once against live Firestore.
- **Indexer service** `alek-bot-indexer-{suffix}` (D5): same image, entry point `indexer_main.py`, about 2 GiB,
  `concurrency = 1`, `min-instances = 0`, `max-instances = 1`, `--execution-environment=gen2`, `--ingress=internal`
  (RFC Δ21), its own env (no reuse of dual-purpose `.env` keys), OIDC-protected `/worker` (and `/extract` from
  Slice 3). Deployed in `cloudbuild-dev.yaml` with the drive queue and its IAM step **after** the main service, so a
  failing drive step never blocks a fix of the bot; `make deploy` refuses an empty indexer URL outside the first
  pass of the two-pass first deploy (`DRIVE_FIRST_PASS=1`, RFC Δ43).

---

## 13. Where the logic lives

| Piece | Layer | File |
|---|---|---|
| `DriveChange`, records, chunks, states, planner, policy, chunker, card builder | domain | `drive_index.py`, `drive_index_policy.py`, `drive_chunker.py`, `drive_card.py` |
| Delta / walk, `DriveReauthRequired` | adapter | `adapters/microsoft/onedrive_adapter.py` |
| Records, chunks, sync state, vector queries, lease | adapter | `adapters/firestore_drive_index_repo.py` |
| Child-process extraction | adapter | `adapters/subprocess_content_extractor.py` |
| Tick (gate → observe → upgrade → work off → continue), purge | service | `services/drive_indexing_service.py` |
| Search | service | `services/drive_search_service.py` |
| Nudge after a bot mutation | service | `UserDriveService`, with an injected `TaskQueue`; sent inside the shielded mutation after the provider call succeeds, carrying the item ids |
| `drive_sync` tick entry point | indexer service | `indexer_main.py` (relay-style minimal app: OIDC `/worker` for `drive_sync` only; `/extract` from Slice 3) |
| Fan-out, staleness check, reconnect notice (scheduler task on the main service) | service, delegated to by the handler | `services/drive_sync_scheduler_service.py`; `handlers/worker_handler.py` only delegates (RFC Δ5) |
| Intent, protocol, capability | agents / manifest | `file_management_agent.py`, `agent_manifest.py`, Smart protocol token |
| Opt-in toggle, status, Reconnect | web | Cabinet app, `/api/drive/indexing`, `/api/drive/status` |
| Place-name resolution (bundled reverse-geocoding table → `place_name` facet) | domain + adapter | `domain/place_lookup.py` (pure lookup), `adapters/geo_place_table.py` (loads `src/assets/geo/places_v1.tsv`), injected into the tick by composition; `geo_table_version` stamped |
| Wiring (second `TaskQueue`, repository, place lookup, fan-out) | composition | `composition/drive_indexing_setup.py`, `service_container.py`; the indexer builds its own in `indexer_main.py` |
| Rebuild one user's index | ops script | `scripts/infrastructure/drive_index_rebuild.py` (§12.2) |

---

## 14. Extension seams

| Later capability | Seam | Already stored for it |
|---|---|---|
| Longer descriptions of photo content (beyond the triage caption, §24.4) | A vision stage over the stored triage verdicts; new card text; `card_version` bump | `facets`, `caption` |
| Audio and video transcripts (User Drive step 3, not Slice 2+4 by owner decision 2026-10-13) | A transcriber implementing `ContentExtractorPort` over `AudioTranscriptionPort`; `EXTRACTOR_CHAINS` gains `audio → [transcriber]`, `video → [transcriber]` (audio track via the existing video probe tooling); its own monthly minutes budget and cost line (§24.7, §24.9). What it needs that this slice does not build: ranged download past the 45 MB cap and a transcription that resumes across ticks for long recordings. The transcript then takes the ordinary path (text gate, chunks, both content streams) | `kind`, facets (`video.duration`) |
| A brief for parsed documents (type, date, parties), ≈ $0.0003 per document (D10) | A third agent over stored chunk text writing the same brief fields; `card_version` bump; closes the `effective_date` asymmetry of D13 | chunk text, brief fields |
| LLM summary per document | New stage writing summary + vector on the record; new search stream | chunk text |
| Heading breadcrumbs, smarter chunking | Chunker change + `extraction_version` bump | — |
| Full-text lexical search over content (BM25 class) — the **known ceiling** of a Firestore-only index, which has no inverted index | A lexical stream on a separate engine (e.g. SQLite FTS5 or a small search service) fed from stored chunk text — rebuildable like the rest of the index — joined into RRF. The cure if rare terms and proper names inside documents miss, not a tweak | chunk text |
| Cosine thresholds per stream | A filter step before RRF, only if the regression set shows noise that `K` and Smart's judgement do not absorb | — |
| Reranking of the fused top-K (cross-encoder or LLM) | A step after RRF; makes the cosine thresholds less load-bearing | cards, windows |
| Contextual chunk headers / breadcrumbs | Chunker change + `extraction_version` bump; until then, lower recall on long documents is expected (§17.8) | — |
| Model extraction for born-digital PDFs whose parser output is structurally poor (tables, columns) | Widen the §9 router beyond text density; same model extractor | `text_chars`, `pages_total` |
| Reuse of extraction for duplicate files | Copy chunks between records with the same `content_hash` instead of re-extracting | `content_hash` |
| Near-duplicate / version clustering (ред1..3) | Grouping step after RRF; today a protocol line only (§10.4) | names, `effective_date` |
| Code, logs, archive tables of contents | Policy change (opaque root → walkable, new admitted kinds) + `policy_version` bump; a new epoch picks up subtrees that were never stored | `reason`, `kind`, opaque root records |
| Push instead of polling (Graph webhooks) | Another trigger enqueuing the same `drive_sync` | — |
| Recency preference | Re-rank after RRF | `modified_at` |
| Range read of long files | `open_file_from_drive` option served from stored chunks; past the indexed span it downloads | chunks, offsets |
| Second document source | Same card contract; new source adapter | — |
| Facts from documents (OQ-1) | A consolidation pass over stored chunk text (email Stage-3 precedent) | chunk text |
| Manual sync / reindex / skipped-file views | Cabinet endpoints over existing state and the rebuild path (§12.2) | state, reasons |
| Faster or isolated extraction host | New `ContentExtractorPort` host | — |
| Per-user parallel ticks | Raise the queue's `maxConcurrentDispatches`; the lease already guarantees one writer per user | lease |

---

## 15. Parameters (initial values; revisit after S1, S3 and the eval)

| Name | Initial | Basis |
|---|---|---|
| `TICK_INTERVAL` | 10 min | Freshness vs. Graph calls; nudges cover the bot's own writes |
| `TICK_DEADLINE` (`deadline_seconds`) | 1800 s | Cloud Tasks HTTP maximum = service timeout |
| Tick start budget | 8 min, 50 files | No new file starts after it (§7.1 invariant) |
| `MAINTENANCE_SHARE` | 30 % of the file budget | Fresh work first (§7.8) |
| `QUIET` / `MAX_DEFER` | 10 min / 60 min | S0b: Word for the web saved every 1.2–3.3 min while typing; 10 min is three times the longest gap |
| `MAX_ATTEMPTS`, `RETRY_BASE`, `RETRY_CAP` | 3, 5 min, 6 h | Per-file, per-item and per-record failures (§8.2) |
| `DOWNLOAD_TIMEOUT` | 120 s | Step 1 drive timeout |
| `EXTRACT_WALL_TIMEOUT` | 120 s | S1 |
| `RLIMIT_AS` / `RLIMIT_CPU` | placeholders | Set by S1 for the chosen host |
| `EMBED_IN_FLIGHT` / `EMBED_FILE_BUDGET` | 4 / 180 s | Caps the indexer's share of the shared Gemini quota; a 429 ends the tick |
| Download cap | 45 MB (Step 1), XLSX 10 MB | Step 1 §4.6 |
| ZIP container caps | 100 MiB inflated, 10 000 members | Phase 0 pre-filter, not a memory bound |
| `MAX_TEXT_CHARS` | 2 000 000 | Extractor-output memory bound |
| `CHUNK_CHARS` / `CHUNK_OVERLAP` | 2 400 / 240 | ≈ 600 tokens, far below the 8 192-token embedding limit |
| `MAX_CHUNKS_PER_FILE` | 400 | The binding content cap: ≈ 864k characters indexed |
| `MAX_CARD_RECORDS_PER_USER` / `MAX_CHUNKS_PER_USER` | 25 000 / 50 000 | kNN cost — S3 |
| `MAX_SHEET_ROWS` | 5 000 | — |
| `CARD_LEAD_CHARS` | 2 000 | — |
| `K`, `CARD_FETCH`, `CHUNK_FETCH`, `NAME_FETCH` | 8, 50, 300, 100 | Widened once when fewer than K survive |
| `DENSE_FOLDER_CHILDREN` | 500 | Class 1 density threshold (with the kind condition) |
| `MODEL_EXTRACT_DENSITY` / `MODEL_EXTRACT_GARBAGE` / `MODEL_EXTRACT_MAX_PAGES` / `MODEL_EXTRACT_PAGES_PER_MONTH` | 200 chars per page / 5 % / 200 / 5 000 | Parser-first router to the model extractor (§9) |
| `TRIAGE_BATCH` | 16 thumbnails | Slice 4 image triage, one ECO vision call per batch |
| `PROBE_CHILDREN` / `PROBES_PER_TICK` | 200 / 20 | Density probe sample and per-tick budget (§3.6); as built one budget, `ITEM_CALLS_PER_TICK` = 20, for probes, parent lookups and re-listing pages (`RELIST_PAGE` = 200 children each; RFC Δ9, Δ18) |
| `NAME_KEY_QUERIES` | 8 | Whole-token keys, and trigram keys, each queried one by one before one query with the rest (RFC Δ17) |
| `LEASE_MARGIN` / `NUDGE_RETRY_DELAY_S` | 1 min / 60 s | Lease = start + `TICK_DEADLINE` + margin, and the write fence at `lease_until` − margin (RFC Δ26); delay of a nudge or busy retry (RFC Δ27) |
| `SYSTEMIC_BACKOFF_BASE_S` / `SYSTEMIC_BACKOFF_CAP_S` | 60 s / 1800 s | Continuation delay after a systemic error, doubled per consecutive failure (§7.7) |
| `PLACE_MAX_DISTANCE_KM` / `PLACE_PREFER_RADIUS_KM` / `PLACE_PREFER_WEIGHT_FACTOR` / `PLACE_FOOTPRINT_K` | 50 / 15 / 3 / 0.0015 | Offline place naming (RFC Δ7, Δ39), measured by `scripts/geo/measure_place_preference.py` |
| `FOLDER_INMEM_CARDS` | 1 000 | Folder-scoped card ranking in memory below it |
| `FOLDER_INMEM_CHUNKS` | 2 000 | Folder-scoped chunk ranking in memory below it (reads and memory bounded) |
| `CASCADE_OPS_PER_TICK` | 2 000 | Metadata deletes/rescopes per tick (§7.8) |
| `NAME_MIN_TRIGRAMS` | 3 | Name stream match floor (a whole shared token also qualifies) |
| `STALE_ALERT_AFTER` | 2 h | Above the worst-case freshness bound (§7.8) |
| `ACCESS_LOSS_TTL` | 30 days | Grace period after a disconnect or an expired grant, then the tick purges (D3) |

---

## 16. Rollout and acceptance

**Phase 0 — hardening, independent of this RFC** (merged 2026-10-11, PR #57): markitdown builtins off
with a local-document allowlist (no ZIP recursion, no upload of audio to Google's speech API); ZIP central-directory
caps; every audio type routed to `AudioTranscriptionPort`. Not covered and documented: parser memory amplification —
closed by the child-process extractor (Slice 2).

**Slice 1 — find a file by metadata (G1 without content).** `DriveChange`, records, policy with opaque subtrees and
scope, planner; `get_changes`; repository; tick with gate, lease, task keys, epochs, cascades, purge; nudges; cards;
`search_files_in_drive` with the envelope and the nonce-tagged wrapper; opt-in, disclosure, Reconnect; the access-loss grace period;
the indexer service and the rest of provisioning; offline place names in image cards; staleness alert. The policy admits **no** content kinds yet: every file is `metadata_only / kind_not_admitted` with a
metadata card.
*Done when:* the record-level parts of §17 are green — §17.1 (a)–(d) and the record properties, §17.2, §17.3, §17.4
(lease, purge, `due_records` ordering), §17.5, §17.6 (envelope, wrapper, folder scope, warm-cache opt-out), §17.7;
the G1 metadata subset of the eval — including exact, partial, transliterated and misspelled names through the name
stream — passes; duplicates are collapsed by content hash; a Step 1 save is found by name on the
tick after its nudge; a 410 at an arbitrary page ends with the record set equal to the drive; opt-out during a tick
leaves zero records and sync docs after the purge, and search returns `disabled` at once; a 30k-file repository costs
at most one card embedding when its marker arrives in the same round as its files (files carded in earlier completed
rounds are removed by the rescope cascade), and displaces no document. Chunk, fence and pooling tests belong to Slice 2.

**Slices 2 and 4 are built and shipped as one slice (D6).** The two paragraphs below stay as the content list; the
merged design and its acceptance are §24.12.

**Slice 2 — answer from content (G2, G3).** `ContentExtractorPort` + child-process adapter on the host S1 chose;
chunker; chunks with generations and the fence; content stream with pooling and windows; the **content exact-token
stream** (normalised amounts, numbers, ids and dates from each chunk as `content_terms`, matched with
`array-contains-any`; decided, not eval-gated — embeddings are weak exactly on G2's numbers); protocol lines. Admitting the
§5 document kinds is a `policy_version` bump, so the upgrade step makes the Slice 1 records due — the extension path
exercised once before it is needed.
*Done when:* §17 is green; the full eval meets its recorded bars (G1, G2, G3 positives; the G3 negative set's search
rate is reported); the Office Online edit-then-ask scenario either answers from the edit or states the index time.

**Slice 4 — the model extractor (D1).** The second `ContentExtractorPort` adapter (§9: a multimodal model reading
images and PDFs natively, behind the parser-first router); a `policy_version` bump admits screenshots, scans,
`no_text` and low-density PDFs (§5.3), with its own cost line in the tick summary.
*Done when:* the eval's OCR-labelled G1/G2 queries meet their bars; 100 holiday photos — camera originals and
messenger-saved copies without EXIF — cause only batched thumbnail-triage calls (≈ ⌈100 / TRIAGE_BATCH⌉) and no
full-image vision call.

**Slice 3 — one extraction path.** Includes the chat decision for HTML: today chat uploads and
Step 1 `open_file_from_drive` pass raw markup; the default becomes converted text, with the raw source available when
the user works on the markup itself. Chat uploads, Step 1 `open_file_from_drive` and email attachments move to
`ContentExtractorPort`; correction of Step 1 §2.
*Done when:* the port produces the same text as the current path for each caller's fixtures.

**Documentation per slice.** Slice 1: CLAUDE.md (User Drive paragraph, WorkerHandler task list), `src/agents/CLAUDE.md`
roster line, `docs/07_deployment/SCHEDULERS.md` (job, queue, its concurrency), a `docs/05_building_blocks/user_drive/`
README, `capabilities.py`, `docs/07_deployment/README.md` (indexer service), decision records for
"drive not in `SearchEnrichmentService`", "opt-in; access loss keeps the index under a 30-day TTL", "lease + dedicated
queue + indexer service" and "problem-content classes (§5.5)". Slice 2: file storage README §2.2 (the extractor
port) and the Smart protocol token change with its backup reference.

---

## 17. Testing

1. **Planner properties** over a model state with a fake executor (outcomes fixed per item and version) and a fake
   clock; generators include renames and moves of folders, `.git` appearing late, out-of-order parents, duplicate
   feed items, folder delete followed by a re-parented child on a later page:
   (a) *Terminal states*: with the clock past every `due_at` and an unlimited budget, every record ends at its latest
   version in `indexed`, `failed` or `metadata_only` with a reason consistent with the policy.
   (b) *Convergence*: at a fixed `now`, applying the full plan leaves no record due at or before `now`.
   (c) *Idempotence*: observing the same page twice, or with duplicate items, gives the same state; the last
   occurrence wins.
   (d) *Visibility*: a card is returned iff its record exists, is not itself `deleting`, carries a card, and no ancestor is `deleting`,
   `opaque_root` or `inside_opaque`; content is returned iff, in addition, the record has a readable generation
   (§3.4). An item whose last observation was a deletion is never returned.
   (e) *Fence*: no chunk with a generation other than its record's `indexed_generation` is ever returned; none is left
   at convergence; a `metadata_only` record never has readable chunks; a retry after a crashed run never makes the
   crashed run's chunks readable.
   Plus: a systemic error sequence never moves a record to `failed`; class-0 work is never starved by class 1–3 work;
   after a version bump every stale record becomes due within ⌈records / page⌉ ticks and `applied_versions` changes
   only when none is left; an access loss and regain within `ACCESS_LOSS_TTL` writes nothing but `access_lost_at` and
   its clearing, and never starts an epoch; a user with indexing off gets no record or chunk written, whatever the task sequence; a
   record created during `sweeping` survives the sweep; every top-level item resolves (its parent is the root); a
   `user_chunk_cap` record is not re-extracted while headroom is below its `chunk_count`.
2. **Policy and scope**: a hostile tree fixture with expected `(state, reason)` per path — a git repository with
   `node_modules`, dot-files, key files, `~$` locks, a nested zip, a ZIP bomb, a DOCX bomb, a 20 MiB PDF, a scan
   without text, an XLSX with 100k rows, a Python venv without a dot-name, an extracted repository zip without
   `.git`, an Obsidian-style vault of 800 small Markdown notes (must stay admitted), a server log saved as `.txt`
   (must become `machine_text`), a VTT meeting transcript, a chat export and a dated journal (must stay indexed), a
   folder of 600 PDFs and one of 1 000 photos (must not become `dense_tree`), a folder that grows past the threshold
   over several ticks (probed when it crosses); plus rename into a dot-name, move out of a dot-folder, `.git` after its siblings.
3. **Adapter wire tests** on Graph responses recorded during S0a: delta pages with `nextLink` and `deltaLink`,
   deletions without a name, moves in and out, the approot item, re-emitted unchanged items, and a delete and a restore
   in one round. They follow `docs/how_to/ADAPTER_WIRE_TESTING.md`. The raw recordings hold names and ids, so they stay
   in `scripts/memory/`. Slice 1 builds sanitised fixtures from them. No 410 was seen, so its fixture follows the
   documented shape: 410 Gone with a `resyncChanges…` error code and a `Location` header. The first live 410
   (2026-10-12) named another feed as its `Location`; its wire test pins that URL (Δ46).
4. **Repository**: the fence across generations (a re-index at the same `cTag` to fewer chunks returns no old chunk; a
   crash after N chunk writes returns only the previous generation); lease exclusion; purge; `due_records` ordering.
5. **Tick and queue**: a fake `TaskQueue` with `ALREADY_EXISTS` and name reservation — a budget-exhausted tick enqueues
   exactly one continuation and it runs; a nudge after the bucket's tick is not lost; a duplicate delivery returns
   `busy` and comes back once per holder; a continuation racing its parent finds the lease released; a file that
   cannot finish before the deadline never starts (Slice 2). The queue bound (about one task per user, plus nudges and
   busy retries) holds by construction — the fan-out skips a user with a fresh continuation marker, a tick enqueues
   at most one continuation, a busy retry is keyed per holder — and is not a test of its own.
6. **Search**: pooling before RRF; one 400-chunk document does not starve other records under a filter; folder scope
   finds a weakly named file deep in the subtree; the envelope (`disabled`, `building`, `unread`); `index_as_of`;
   the wrapper cannot be closed by a chunk containing `</drive_document …>`; opt-out with a warm agent cache returns
   `disabled`; disconnect returns `disconnected`, an expired grant returns results with `reconnect_needed` until
   `access_lost_at + ACCESS_LOSS_TTL`, then `disconnected`; a regain before it continues from the cursor with no epoch;
   a regain after it completes the purge and starts epoch 1.
7. **Manifest**: context-field description collisions; all new `LANG_DICT` keys present in en/uk/fr/es.
8. **Eval** (`scripts/validation/drive_eval/`, run as `python -m scripts.validation.drive_eval`; Slice 1 calls
   `DriveSearchService` directly, the Smart dry-run scaffolding of `ab_agent_models.py` joins with G3 in Slice 2, RFC
   Δ13): a committed, PII-free, multilingual fixture folder (RU/UK/ES/EN/FR; names like `Scan_0012.pdf`,
   `Договор_ред1..3.docx`; a nested folder; a repository) behind an in-memory `UserDrivePort` fake, and a gold file
   (Slice 1: `tests/fixtures/drive_eval/gold_g1.json`, cases `{id, case, query, filters, lang, goal, expected_paths,
   k?, min_hits?, known_hard?}`; Slice 2 adds `expected_substring`). Metrics: G1 and G2 recall@K, a substring hit for G2, the
   G3 search-initiated rate on positives and on a negative set (chit-chat, memory and web questions). Cases also cover
   a draft family whose dates disagree with its version names, and memory-vs-document conflicts in both directions.
   Name cases: exact, partial, transliterated (`dogovor` → `Договор`) and misspelled names. G2 cases include
   table-heavy and two-column PDFs and numeric questions (amounts, invoice numbers). Expected and recorded, not a
   surprise: recall on long documents stays below what contextual chunk headers would give.
   **The eval is a regression guard, not a decision tool.** Every quality choice is decided in this RFC with a default;
   the eval decides nothing about scope. It is automated over a synthetic fixture folder (no live drive, no manual
   testing), its first run records the bars, and later chunker, card, embedding or extraction changes must not fall
   below them; any case that hit when the bars were recorded and misses now fails the run. A case is shaped as Smart would call (a place goes in the query with `drive_kind=photo`); a
   case with more right answers than `K` may set `min_hits`; a `known_hard` case (with its reason) is run and reported
   but is in no bar; `semantic` cases reach their file only through the card stream (expected misses with offline
   fake embeddings). G1 queries that need OCR are labelled expected misses until Slice 4 ships (RFC Δ45).

---

## 18. Spikes

| # | Question | Decides |
|---|---|---|
| S0a | Does `GET /me/drive/special/approot/delta` work under `Files.ReadWrite.AppFolder` (pagination, 410 + `Location`, folder deletion as one item or each descendant, moves into and out of the App Folder, whether the approot item appears)? Record the responses as wire fixtures. | Delta or walk adapter |
| S0b | Is `cTag` stable across rename and move? How often do `cTag` and `lastModifiedDateTime` change during 10 minutes of editing in Word for the web? | `QUIET`, `MAX_DEFER` |
| S0c | Does a file restored from the recycle bin keep its `item_id`? | Restore = ordinary re-observation or not |
| S0d | How soon does the bot's own upload appear in delta, and with which `cTag`? | Nudge timing |
| S0e | Are `photo`, `location`, `remoteItem`, `package` facets and `folder.childCount` populated in delta pages? | Key facets, density probe, policy without extra calls |
| S0f | Do `/items/{id}/thumbnails` work for App Folder items under `Files.ReadWrite.AppFolder`, at what sizes, and for which image formats (incl. HEIC)? | Slice 4 triage, or a fallback (downscale on the indexer) |
| S0g | Do Graph custom thumbnail sizes (`c{w}x{h}`) work for App Folder items, up to which size, and is receipt and A4 text legible in them (HEIC included)? | The reader's image input (§24.5) |
| S1 | On the indexer service (gen2): child-process `RLIMIT_AS`/`RLIMIT_CPU` hold for the Phase 0 bomb set; the service's peak RSS with a 45 MB download and a child at its limit; cold-start time; whether a backfill's embedding calls raise chat-side 429s on the shared quota. | Indexer memory, child limits, `EMBED_IN_FLIGHT` |
| S3 | Cost and latency of `find_nearest` at 10k and 50k chunks under a `user_id` filter. | Per-user caps |

S0a–S0e and the eval fixture come before Slice 1 code; S1 before Slice 2; S3 before Slice 2 ships; S0f before
Slice 4.

**Results (2026-10-11).** These come from `scripts/onedrive/probe_delta.py` on the owner's App Folder. The raw
reports are in `scripts/memory/onedrive_s0_*.json`, which is gitignored because they contain PII.

| # | Result | Consequence |
|---|---|---|
| S0a | Delta works under `Files.ReadWrite.AppFolder`. `$top` pagination gives `nextLink` pages, and the last page carries a `deltaLink`. The approot appears in every round, without a `root` facet. Deleting a non-empty folder gave a tombstone for each descendant. A move out of the App Folder is a tombstone, after which a GET by the item's id returns 404. A move in shows up as the item, with its id. Unchanged items are re-emitted often. A delete and a restore came in one round. No 410 was seen. | Delta adapter, no walk mode (§7.2). The 410 path is tested on the documented shape (§17.3). |
| S0b | A rename or a move keeps `cTag` and changes `eTag`. In Word for the web, `cTag` moved every 1.2–3.3 min of typing and intermediate versions were skipped. | `QUIET` 10 min and `MAX_DEFER` 60 min kept (§15). |
| S0c | `POST /items/{id}/restore` works under the App Folder scope and keeps `item_id`. | A restore is an ordinary re-observation (§7.6). |
| S0d | The bot's own upload was in delta on the first poll (0.3 s), with the `cTag` the PUT returned. | Nudges stay as a guarantee, not a workaround for lag. |
| S0e | A phone HEIC carries `photo` (taken time, camera), `location` and `image`. Messenger JPEGs carry `image` and an empty `photo: {}`, and video carries `video` (duration, dimensions). Audio files have no `audio` facet. `folder.childCount` is present. `quickXorHash` is always present, and SHA-1 and SHA-256 only on some files. `remoteItem` and `package` were not seen. | Camera prior on `photo.takenDateTime` or `photo.cameraMake` (§5.3), key facets (§8.5). The density probe uses `childCount`. Duplicates are collapsed on `quickXorHash`. |
| S0f | Thumbnails work for JPEG, PNG, HEIC (as JPEG) and SVG (as PNG), at 96, 176 and 800 px. | Slice 4 triage on provider thumbnails, with no downscale on the indexer (§5.3). |

---

## 19. Rev 1 claims that were wrong (do not reintroduce)

- `FileConversionPort.can_extract` does not exist; conversion is `src/utils/file_conversion.py`.
- Email search is not hybrid keyword + vector with LLM key extraction; it is multi-vector kNN + RRF over one record per
  email.
- Firestore takes compound pre-filters with vector search (in production for email) and sets dimension per field; the
  500-writes-per-batch limit is gone from the current quotas.
- `sha256Hash` is "not supported, don't use" on OneDrive; only `quickXorHash` is guaranteed.
- Shannon entropy > 7.7 does not mean "container": every DOCX/XLSX/PPTX/EPUB is a ZIP.
- An RRF threshold of 0.025 rejects every hit found by a single stream (rank 1 → 1/61 ≈ 0.016).
- The "Anthropic Contextual Retrieval" of rev 1 was a fixed prefix; Anthropic's technique has an LLM write per-chunk
  context.
- There is no Redis in the stack.
- Check-then-write "generation fencing" without a single writer is a race; see §7.1 and §7.4.
- A size cap on the input does not bound a parser's memory (§9).
- Synchronous delta before each search, write-through indexing from bot mutations, a `DriveIndexingJob` state machine,
  per-model collections and per-user subcollections, keyword routing of queries to sources, and in-turn conversion
  ("JIT") are not part of this design.

---

## 20. Open questions

- **OQ-1 Document content and memory.** Document content is not fed to consolidation by this RFC, but some documents
  *are* facts about the user (a scanned receipt, a ticket), and answers derived from documents already reach
  consolidation (§11). Whether consolidation may draw on documents, how it would tell a fact about the user from a
  third-party text, with what provenance on facts and turns, needs its own design and a measured dry run. The memory-
  vs-document protocol line (§10.4) is revisited with it.
- **OQ-2 Full text on open.** Step 1 deliberately returns a file's full converted text to Smart (file storage README
  §2.2; truncation removed in `2ee8cb3`). With a growing drive, opening a 300-page document puts all of it in context.
  Options: keep as is; a cap with an alert; range reads from stored chunks (§14).
- **OQ-3 Caps.** Initial values in §15 are reasoned, not measured; S3 and the first real folders set them.

---

## 21. Owner decisions on the open points (2026-10-11)

- **D1 Problem content and images — decided with rationale in §5.5 and §5.3.** Rev 1's harm-based classes are kept as
  the frame; safety rests on bounds that need no classification (layer 0), classification only buys quality and cost
  (layer 1). Adopted: markers + density over content class (decided once from a probe), magic bytes, a
  post-extraction text-quality gate measured after timestamps, zero yield and text density, the quiet period, and
  batched thumbnail triage on every image with the camera facet as a prior. Rejected:
  timestamp clustering, entropy, name and folder conventions, head/summary/tail sampling.
- **D2 Prompt injection — decided: no extra confirmation.** Every delivery of file text carries the
  reference-material reminder inside a nonce-tagged wrapper (§10.4; as built, RFC Δ34, and the prefetch path in
  Slice 3, RFC Δ35); deletes remain recoverable from the recycle bin and post a receipt.
- **D3 Access loss — decided: no immediate purge; a 30-day grace period** after a disconnect or an expired/revoked
  grant, after which the tick purges; reconnecting the same drive in time resumes (§6, §7.6). Rev 3.10 replaced the
  Firestore-TTL stamping with this tick-side rule — same behaviour for the user, far less machinery. Opt-out still purges at
  once; a different drive purges at once. Basis: tokens drop often, disconnects can be accidental, and re-indexing is
  slow in practice (Gmail indexing experience), even though its embedding cost is small.
- **D4 Answers derived from documents reach memory as today — decided: accepted** as a trade-off, to be refined later
  in the consolidator's prompt (OQ-1).
- **D5 Extraction host — decided: a separate indexer service** running the whole tick (§9).

**Owner decisions for Slice 2+4 (2026-10-12 and 2026-10-13)**
- **D6 Slices 2 and 4 are one slice.** One design of `ContentExtractorPort`, the parser-first router, the extraction
  outcomes, policy admission, both extractors, OCR-labelled eval cases from day one, triage and its cost line.
  Building the parser path alone would mean reshaping it when the model path arrives.
- **D7 Extractor model: Claude Haiku 5.5 or a GPT model, no spike.** The provider is a configuration switch, and the
  final pick is checked live on the owner's real files before deploy (§24.3). §9's "Gemini, decided" had no basis and
  is withdrawn.
- **D8 The extractor call also returns a brief**, in structured fields next to the verbatim text: the document type,
  one line on what it is, the document's own date, the parties and the amount when present. The brief goes into the
  card, so a scan with a neutral name is found by subject and Smart can describe it without opening it. The verbatim
  text stays verbatim.
- **D9 Triage also captions photographs** (at most ten words), into the card. Photo descriptions move from §14 into
  this slice; burst sampling is dropped.
- **D10 No fact-style model metadata for every file.** Exact values come from `content_terms`, meaning from chunks,
  a brief only where the model is already called. A brief for parsed documents stays a §14 extension.
- **D11 The drive intents stay as they are** (no single commissioning intent now; a later refactor is small). The
  §10.4 protocol lines go into Smart's token in this slice, and the G3 eval with them.
- **D12 Both model calls are agents** on the indexer — a document reader and an image triage agent, not an extension
  of `FileManagementAgent` (§24.2).
- **D13 The document's own date becomes `effective_date`** when the brief has one, the §3.1 principle ("the date a
  user means"). Parsed documents keep the file date until the §14 brief for parsed documents exists; the asymmetry is
  accepted and stated in the search guidance.

---

## 22. Where to continue (handoff, 2026-10-13)

**Slice 1 is live** (PR #58, deployed and accepted with the owner 2026-10-12); its deltas are §23. Owner decisions
D1–D13 are taken (§21). **Slice 2+4 is designed in §24**; its implementation plan is written next, against a frozen,
verified core, then reviewed adversarially before any code lands on a feature branch. Order inside the slice:

1. Spikes before code that depends on them: S1 (child-process limits on the indexer), S0g (Graph renditions for model
   input, §24.5); S3 (`find_nearest` cost on chunks) before the slice ships.
2. Live checks before deploy, with the owner: the model choice (Haiku 5.5 or a GPT model) and the thumbnail size
   (176 or 800 px) on the owner's real files; the real repository under a `zz-synthetic-…` user, then purged.
3. Owner-run steps: the agents' prompts (tokens, blueprints, profiles) and the Smart protocol token (`--apply` is
   human-only), each backed up first.
4. Deferred by decision, each with its trigger: `docs/04_solution_strategy/decisions/drive_*.md`.

---

## 23. Slice 1 implementation deltas

Where Slice 1 as built differs from the text above, and why. Δ1–Δ22 were decided before the build (CLAUDE.md
Gate 4, by the arbiter under the owner's delegation); Δ23–Δ45 come from the review of the built slice (owner
decisions and fixes). The body text above was corrected where a delta made it false; a row stays here as the record
of the decision. Code and tests cite these rows as `RFC Δn`.

| # | The RFC said | Slice 1 does | Why |
|---|---|---|---|
| Δ1 | `EmbeddingService` (port) exposes `embedding_space` (§4) | The port is unchanged. `GeminiEmbeddingAdapter.EMBEDDING_SPACE = "gemini-embedding-2/768"` is a class attribute; composition reads it and injects it as a `str` into the tick and, required, into `DriveSearchService` | The port contract pins its abstract methods; no caller switches on the value. A wiring that forgets the space fails at construction instead of skipping the space check |
| Δ2 | Task-key families scheduler / cont / nudge / **purge** (§7.1) | `purge` is renamed `toggle` (`{user_id}:toggle:{ts}`) and is also used when indexing is turned **on** | Onboarding starts at once instead of after up to `TICK_INTERVAL`; the tick's gate decides purge or onboarding |
| Δ3 | Nudge key `{user_id}:nudge:{item_id}:{mutation id}`, one per item | One nudge per mutation: `{user_id}:nudge:{mutation_id}`, payload `item_ids: [...]` | A save that creates folders touches several items; the mutation id is already unique |
| Δ4 | Tick payload `{user_id, account_id}` | `{user_id}` plus an optional `nudge` | Nothing in the indexer uses `account_id` (embeddings are unbilled, §12.1) |
| Δ5 | Fan-out and staleness in the handler (§13) | `DriveSyncSchedulerService` (services/) holds them; `WorkerHandler` only delegates | `WorkerHandler` owns no business logic |
| Δ6 | "Notifies the user once" on a lost grant (§7.7), host unspecified | The indexer sets `needs_reconnect`. The **fan-out on the main service** posts the notice on the transition, tracked by `DriveSyncState.reconnect_notified`, set before the send. It comes down again when no send was attempted (no channel, a formatter failure, `notify` raising; `NotifyResult.sent`), so the next fan-out retries; once a send was attempted it stays up, delivered or not | The indexer has no chat delivery. A notice cut off after it left must not repeat every 10 minutes; one that never left must not be lost |
| Δ7 | Place names "English plus local" from a bundled table (§5.3), no radius | GeoNames `cities5000` + `alternateNamesV2` reduced to `src/assets/geo/places_v1.tsv`, with a weight (population, tripled for capitals and regional seats). The nearest place within `PLACE_MAX_DISTANCE_KM = 50` names the photo, unless a heavier place within `PLACE_PREFER_RADIUS_KM = 15` qualifies (Δ39); else `None` | Pure nearest-point named districts ("Esquilino", "Paris 04") and missed villages. CC BY 4.0, attributed in the asset README |
| Δ8 | Property tests (§17.1), library unspecified | Seeded `random` generators over 200 seeds | `hypothesis` is not installed; seeds make failures reproducible |
| Δ9 | Per-item provider calls "bounded per tick", budgets unnamed except `PROBES_PER_TICK` | One shared budget, `ITEM_CALLS_PER_TICK = 20`, for probes, parent lookups and re-listing pages; no item call starts after the tick's start budget | One knob instead of three; a slow provider must not push the tick past its deadline. Each kind resumes on the next tick |
| Δ10 | `list_children_changes(user_id, folder_id)` | `list_children_changes(user_id, folder_id, limit, cursor=None) -> DriveChildrenPage` | The probe reads only the first `PROBE_CHILDREN`; a re-listing is paged and resumable (Δ18) |
| Δ11 | `open_file_from_drive` text "carries the reminder" (§10.4, D2), no slice named | Done in Slice 1 together with the search wrapper | One wrapper function, one review |
| Δ12 | Quiet period (§7.3), index runs (§8), chunks, extractor, chunk cap | Not built; `ADMITTED_KINDS` is empty, so `pending` is unreachable | All of them act on content; Slice 2 builds them with the extractor |
| Δ13 | Eval (§17.8) "reuses the Smart dry-run scaffolding" | Slice 1's eval measures G1 by calling `DriveSearchService` directly with each gold case's filters; the G3/Smart part arrives with Slice 2 | §16: Slice 1 is done on the G1 metadata subset; G3 bars are Slice 2's |
| Δ14 | `DriveSyncState.pending_cascades` (§3.5, §7.6) | Not kept: the folder record's `deleting` state is the durable marker, and work-off runs only after a complete round (Δ15) | Two sources of truth for one fact |
| Δ15 | First-seen cards and cascades wait for a complete round; edits do not (§7.6, §7.8) | **All** work-off waits for a complete round | One rule. Edits wait only while a round spans ticks (onboarding, 410), and continuations chain at once |
| Δ16 | Opaque roots: generated dirs only "inside a repository" (§5.2) | `node_modules`, `__pycache__` and `site-packages` are opaque roots **anywhere**; their card reads `generated files` (Δ33) | Tools create them, so the no-convention principle does not apply; an extracted repository without `.git` would otherwise fill the card cap |
| Δ17 | Name stream: one `array-contains-any` query, ranked by shared keys, then RRF (§10.1 3a) | One query per whole-token key **and** one per trigram key, at most `NAME_KEY_QUERIES = 8` of each in query order, plus one query with the remaining keys (≤ 30), in parallel, merged by id and cut by the `NAME_MIN_TRIGRAMS` floor. **After RRF, records whose name covers every query token rank first**, RRF order kept within each group | A shared query returns its first 100 matches by id: `IMG_4912` among 1 000 `IMG_*` files, or a fully misspelled name reachable only by trigrams, was never seen. Plain RRF ranked an exact name below two-stream noise. The tie-break is the §14 "step after RRF" seam, not a cosine threshold |
| Δ18 | Re-listing a never-stored subtree (§5.2): "bounded, resumable"; `pending_reobserve` in the sync state | A record flag, `reobserve_pending` + `reobserve_cursor`, due in class 2: one page (`RELIST_PAGE = 200`) per item call, retried with backoff, dropped after `MAX_ATTEMPTS`. A folder that becomes opaque clears it, and the step skips an opaque folder without a call | A list in the sync state was lost on any systemic error, and a failing folder looped the user's ticks; an opaque folder's subtree is never stored, so listing it is wasted |
| Δ19 | §7.8 class order; the gate re-checked "before each write batch"; toggle and nudge on a busy lease | (a) When a class runs out of budget, lower classes wait. (b) The gate is re-read at most every 5 s. (c) A nudge on a busy lease re-enqueues itself under `…:r{n}`; a toggle or a continuation comes back once per holder (Δ27) | (a) keeps a half-rescoped repository's files from being carded. (b) one Firestore read per write batch is noise |
| Δ20 | `kind` filter, a single value (§10.1) | Families: spreadsheet → {xlsx, xls, ods, csv}, document → {docx, doc, odt, rtf, pdf}, word → {docx, doc}, presentation → {pptx, ppt, odp}, photo → {image}; a format, an extension or a plural ("jpgs") is accepted; an unknown value is refused | §10.1's own example ("all my spreadsheets") needs a family; an unknown word silently matched nothing |
| Δ21 | The indexer has an OIDC-protected `/worker`; `.env` keys are not substitutions (§12.4a); audience unspecified | `--ingress=internal` + in-app OIDC with a pinned audience, failing closed on Cloud Run. One new substitution, `_INDEXER_URL`, from its own key `INDEXER_URL_DEV` | One caller (Cloud Tasks, same project). The key is deploy-only, which is exactly the deploy-substitution rule |
| Δ22 | Indexes for both prefixed and unprefixed collections | `development_` collections only; the old unprefixed index entries elsewhere in the file are kept (`decisions/drive_index_pruning_deferred.md`) | The unprefixed collections were deleted 2026-05-31 |
| Δ23 | The card cap marks a record `metadata_only / user_card_cap` (§3.4, §5.4) | A flag, `card_capped`: it withholds the card and nothing else; scope, state and reason stay structural. Eligibility, eviction and release read the flag; the release query is `user_id + card_capped + card_rank + modified_at`. `Reason.USER_CARD_CAP` is unused | Overwriting the reason re-opened a capped repository on every re-emission, kept a repository opaque after its `.git` left, and carded a released dense folder as a repository |
| Δ24 | A card the provider refuses is retried like any per-file failure (§8.2) | After `MAX_ATTEMPTS` refusals (HTTP 400) the refusal is terminal: flag `card_refused`, no card, `card_hash` names the refused text and `card_version` is stamped; the state stays structural. A new card text or a card-builder bump gets a fresh try | Retried forever, a refused card stayed version-stale and restarted a full upgrade pass every tick. `failed` is reserved for content outcomes (Slice 2) |
| Δ25 | An embedding change writes both vector fields; `query_embedding_space` switches when a full pass finds none left (§12.3) | The sync state records the space its cards are in (`applied_space`, and `target_space` for a running pass); a card embedded in another space is version-stale, so the upgrade pass re-embeds it (the old vector serves until replaced) and `applied_space` moves only when a full pass finds none left. While it differs from the query space, search skips the card stream (names and filters only) and the envelope says `refreshing`. The dual-field write is not built (`decisions/drive_embedding_dual_field_deferred.md`) | One space per index until a model change is real; a mixed-space kNN would rank by meaningless distances |
| Δ26 | "`lease_until` exceeds the task's dispatch deadline, so a holder never outlives its lease" (§7.1) | Fenced: the tick writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and `save_sync_state(state, owner)` / `delete_sync_state(user_id, owner)` are transactions that apply only while `owner` holds the stored lease, else `LeaseLost`. The tick treats `LeaseLost` as a quiet stop (INFO, status `lease_lost`) | Cloud Run can answer 504 at its timeout without cancelling the coroutine; a stale purge could delete the new holder's state and lease |
| Δ27 | The tick enqueues its continuation, then releases the lease; a busy tick returns `busy` (§7.1) | The continuation is enqueued after `release_lease`, on every path. A tick without a nudge that finds the lease busy re-enqueues itself once per holder, after `NUDGE_RETRY_DELAY_S`, under `{user_id}:cont:busy-{holder}` | A continuation dispatched at once found its own parent busy and was dropped, while the parent's continuation marker made the fan-out skip the user for up to 30 min |
| Δ28 | Staleness is evaluated for users with a sync state (§12.1) | The fan-out creates a minimal sync state (`created_at = now`, Firestore `create()`) for a user with indexing on, a working grant and no state; the tick onboards from it as from none | A user whose ticks never land (indexer unreachable, 401/404/503, a crash before the lease) was never alerted, and the Cabinet said "building" forever. Not under a lost grant: a state then would restart the grace period and its notice |
| Δ29 | After `MAX_ATTEMPTS` a parent lookup → orphan, deleted (§8.2) | Only a definite not-found (a deleted change, `DriveItemNotFoundError`) removes the orphan, a folder through the cascade. Transient failures past `MAX_ATTEMPTS` keep the record `unresolved`, retried every `RETRY_CAP` at priority `RETRY`. A record parked under an unresolved parent drops its card. A deleting folder whose children are all deleting folders not in the current page steps aside one second behind them; a cascade step that writes nothing charges no budget | Transient failures prove nothing about the parent, and in delta mode no later enumeration would settle it. Without the step-aside, a page full of such folders stopped the cascade class every tick |
| Δ30 | Tick step 2 is the upgrade, before work-off (§7.1); a pass restarts until clean (§12.3) | The upgrade step runs after work-off, only when no class stopped on its budget, then the card-cap release. A new sync state starts with `applied_versions` = the current constants. A finished pass that left stale records waits until due work has run or the target changes | Fresh work first (§7.8). Onboarding builds every record under the current constants, so a verification read of the whole index found nothing; a record stuck in backoff must not cost a full read per tick |
| Δ31 | Name keys are recomputed on rename (§3.2) | The keyer has its own version, `NAME_KEYS_VERSION`, in the version map; a record stamps `name_keys_version`, and an older one is version-stale | A key change re-keys through the upgrade pass instead of a policy bump |
| Δ32 | OS and lock artefacts are never downloaded (§5.1) and keep a card with their reason | `~$*`, `.~lock.*#`, `Thumbs.db`, `desktop.ini`, `.DS_Store` and `*.tmp` are reason `os_artifact`, decided before the dot-name rule; the record is kept with no card and no name keys | A lock file ranked above the document it locks ("ред3" returned `~$Договор_ред3.docx`). §3.4 lets a reason withhold the card |
| Δ33 | Opaque-root cards `name \| repository`, `name \| hidden folder` or `name \| generated files (N items)`, plus a `descendant_count` facet (§5.2, §8.5) | Dot-folder `hidden folder`; generated directory (Δ16) `generated files`; dense tree `generated files (500+ items)` with the child count bucketed (100+, 500+, 1000+, 5000+, …); a marker folder `repository`. No `descendant_count` | A live `childCount` re-embedded a dense folder's card on every child change; a `node_modules` is not a repository |
| Δ34 | Content and names XML-escaped (`&`, `<`, `>`, quotes) in the wrapper (§10.4) | Body text: only a `<drive_document` / `</drive_document` look-alike loses its opener, including a fullwidth `＜` or small `﹤` opener, a fullwidth, division, fraction or big-solidus slash, fullwidth letters and `_` look-alikes, and format controls (zero-width, bidi, tag characters), combining marks and variation selectors inside the tag word; attribute values get the same neutralisation. Attribute values escape only `" < >`. The attached-file label for vision and video is not escaped. Letter look-alikes are out of scope: the per-result nonce is the defence | Full escaping turned `R&D` into `R&amp;D` in paths the model copied back, naming a folder that does not exist or creating one through save/move. A forged closing tag cannot guess the nonce |
| Δ35 | Every delivery of file text carries the wrapper and reminder (§10.4, D2) | Search results and `open_file_from_drive` do. A drive file prefetched by the coordinator for another specialist (DocPlanner, PdfGenerator, HtmlPageGenerator) reaches it raw until Slice 3's one extraction path (`decisions/drive_prefetch_wrapper_slice3.md`) | That path is the coordinator's file-ref resolution shared with chat uploads; Slice 3 rebuilds it |
| Δ36 | An empty query is filter-only (§10.1) | A request with no name keys (empty, or punctuation only) is filter-only; with no filter either it is refused with a correction (put the words in `drive_query`, or set a kind, date or folder filter). The `drive_query` description says it is required unless the request is filter-only, that the delegate's `query` is not read, and that it takes name words or a place or subject, never type words or dates | Smart's words sat in the required `query` field, which search never reads: the empty search answered with the 8 newest files as if they matched. A query of punctuation ran a kNN over a meaningless vector |
| Δ37 | Filters are post-filters on the streams' pages; widen once (§10.1 steps 4–5) | When a stream page came back full (after the widening) and the filters dropped candidates, the result is `capped`: the answer says the filters saw only the best N name (and meaning) matches, and with no survivor never says "No matching files" but how to search all files. A kind family runs one card kNN per member kind, merged by distance | A query plus a date or folder filter answered a flat "no match" for files that exist past the page; a family filter dropped card candidates of the other kinds |
| Δ38 | Name keys: NFC + casefold tokens, Cyrillic↔Latin transliterations, trigrams (§3.2) | Each token also gets its Latin accent-stripped form (œ æ ø ł ß đ ð þ ı ħ folded), and Cyrillic tokens the Ukrainian national transliteration (resolution No. 55, 2010) next to the Russian-style one; any Unicode punctuation and apostrophe look-alikes separate words. Variant keys sit after the originals; the Ukrainian form gets no trigrams. A small en/ru/uk function-word set is dropped from queries only | "resume" did not find `Résumé.pdf`, "kyiv" did not find `Київ.jpg`, `Report—final’s` was one token; a third script's trigrams lifted unrelated names over the floor; "photos from my trip" blocked the cover tie-break |
| Δ39 | (Δ7) The heaviest place within 15 km names a photo | The nearest place names it, unless a place within 15 km weighs at least `PLACE_PREFER_WEIGHT_FACTOR = 3` times as much and is in the nearest place's country, or holds the photo inside its own footprint (`PLACE_FOOTPRINT_K = 0.0015 · √weight` km) while the nearest place's footprint does not | Across borders the old rule named Monaco "Nice" and Vaduz "Feldkirch" (603 places at their own centre). Measured over the bundled table by `scripts/geo/measure_place_preference.py`: all expected cases hold, self-name 62.7 %, residual cross-border cases listed in `src/assets/geo/README.md` (`decisions/drive_place_name_residuals.md`) |
| Δ40 | A 410 on the delta link restarts the enumeration (§7.5) | A 404 on a **stored** cursor is treated the same: `DriveResyncRequired` from the delta start. A 404 on a fresh enumeration or on the App Folder read keeps its class | A delta link Graph no longer knows is as dead as a 410 one; it failed every tick as an internal error |
| Δ41 | Only `invalid_grant` is a lost grant (§4, §7.7) | `interaction_required` and `consent_required` from the token endpoint are lost grants too (`GraphReauthRequired`, so `DriveReauthRequired`); To Do still sees a `ValueError` subclass | Only a new sign-in helps (MFA, Conditional Access, consent withdrawn); as a plain error they became a 60 s throttle retried forever |
| Δ42 | Timeouts: per-call Graph bound only | The token POST has its own 30 s `ClientTimeout` (shared with To Do; a timeout keeps its throttle class), and the Gemini embedding client a 30 s HTTP timeout mapped to `LLMUnavailableError` (systemic). The connect-time drive-id read has 10 s | aiohttp's default is 300 s and google-genai sends with none: a hung connection held a tick indefinitely |
| Δ43 | Provisioning in `cloudbuild-dev.yaml` next to the relay (§12.5) | `make deploy` refuses an empty `SERVICE_URL_DEV`, and an empty `INDEXER_URL_DEV` unless `DRIVE_FIRST_PASS=1` (the first pass of the two-pass first deploy). Cloud Build deploys the drive queue, the indexer and its IAM step **after** the main service | An empty indexer URL silently disabled indexing in the live environment; a failing drive step blocked every hotfix of the bot |
| Δ44 | Cabinet card: on/off, records per state, … (§6) | `/api/drive/status` also returns `available` (a drive queue is wired; the switch is hidden when false unless search is already on), `access_lost_at` and `index_kept` (an index exists, no purge runs, and the grace period has not ended). The "kept about 30 days" and "search still answers" hints show only while `index_kept`; the card lists every non-zero counter, labelled in en/uk/fr/es | Turning search on where no queue exists showed "building" forever; the card promised a kept index past its grace period or during a purge |
| Δ45 | The eval records bars and later runs must not fall below them (§17.8) | Place cases call as the manifest says (the place in `drive_query`, `drive_kind=photo`); per case `min_hits` for a case with more right answers than K (the type-word place twins: 5), `known_hard` cases (run and reported, in no bar), a `semantic` case type (card stream only; expected misses offline with fake embeddings), and **any** case that hit when the bars were recorded and misses now fails the run | The real-embedding run found the right photos for every place case but scored them as misses; a single lost case hid inside an unchanged scope recall |
| Δ46 | On a 410, restart from the `Location` header (§7.5, §17.3) | `Location` is followed only when it is the App Folder's own feed (`/me/drive/special/approot/delta`, absolute or relative); otherwise the restart is the delta start. A stored cursor on any other feed restarts the same way without a call. The 410 body is logged (200 chars) | The first live 410 (2026-10-12, after the owner moved the App Folder to the drive root) named `/drive/root/view.delta`, the whole drive's legacy feed. Following it failed every tick with an unparsable body until the staleness alert fired; the synthetic fixture had assumed an approot link |

---

## 24. Slice 2+4 design (rev 3.15)

One slice (D6): content extraction with the parser first and the model second, chunks, the content streams, image
triage with captions, the brief, the protocol lines, the full eval. Where this section and the text above disagree,
this section wins; the text above was corrected where it was plainly false. Slice 3 (one extraction path for chat,
`open_file_from_drive` and email attachments) is not part of it, and neither are audio and video transcripts (owner,
2026-10-13): they stay metadata-only, and their seam is the §14 row.

### 24.1 What the slice builds, by RFC section
- §5.1 admission (`POLICY_VERSION` 2): the document kinds with a named extractor each (PDF, DOCX, PPTX, XLSX/XLS,
  ODT/ODS/ODP, EPUB, HTML, Markdown, text, CSV, JSON, RTF, MSG, EML, VTT/SRT); every image enters triage. New
  constants `EXTRACTION_VERSION` (2 since the maximal query forms of §24.6) and `TRIAGE_VERSION` (1) join
  `CURRENT_VERSIONS`; `CARD_VERSION` 2 (brief, caption, lead text). Each wired extractor's version joins the target
  map as `extractor:<id>`, so a new parser or reader re-reads only what it decided (`extracted_by`, §12.3). The
  upgrade pass (§12.3) makes the Slice 1 records due in class 3 — the extension path exercised on real data, bounded
  by `MAINTENANCE_SHARE`.
- §7.3 quiet period, §8.1 index run, §8.2 content outcome classes, §8.3 tables, §8.4 chunker, §7.4 fence, §5.4 chunk
  cap and release, §5.2 the repository root's `README` lead text (read by the parser only), §12.1 the counters this
  slice adds (all listed as not built in Δ12). A record due for other work (a card, a re-embed) still keeps its
  content's quiet period. <!-- r2: T-M1 -->
- §9 `ContentExtractorPort`, the child-process parser adapter, the router (§24.3) and the reader agent (§24.2).
- §5.3 triage with captions (§24.4), §10.1 step 3 chunk stream plus the content exact-token stream (§24.6), §10.2
  windows and `matched`, §10.4 protocol lines in Smart's token (D11), the G2/G3/OCR eval (§24.11).

### 24.2 Two agents on the indexer (D12)
- **`DriveDocumentReaderAgent`** (agent type `drive_reader`): a PDF window or an image in, verbatim text plus — for the
  first window only — the brief (§24.4) out, as structured output with an `OUTPUT_FORMAT_DRIVE_READER` token. No
  tools, no conversation history, no biographical context, no user prompt overrides. **`DriveImageTriageAgent`**
  (agent type `drive_triage`): a batch of thumbnails in, one verdict and, for photographs, one caption per thumbnail
  out (`OUTPUT_FORMAT_DRIVE_TRIAGE`).
- Both are non-delegation agents (no manifest descriptor, `can_handle` false), like `EmailClassificationAgent`, but
  built **per call with the user's own configuration**, like `CompanionExtractorRunner`
  (`src/composition/companion_extractor_runner.py`): composition holds a runner per agent type that reads the user's
  `UserBotConfig`, builds the execution context through `AgentContextBuilder`, sets the quota service and runs the agent
  inside its billing scope. The reader's runner (`DriveReaderRunner`) implements `ContentExtractorPort`; the triage
  runner (`DriveTriageRunner`) implements `ImageTriagePort`. The indexing service sees only ports.
  `src/composition/drive_model_setup.py` builds both for `indexer_main.py` (and the eval) on one context builder.
- **Provider is configuration (D7).** `STRATEGIES` entries for both types allow `claude` and `openai`, default
  `claude`, no fallback; `_DEFAULT_AGENT_TIERS` sets both to ECO (Claude ECO = `claude-haiku-5-5`, OpenAI ECO =
  `gpt-6-luna`). A user's `agent_providers` switches one or both. The indexer builds a `ProviderRegistry` with the
  Claude and OpenAI adapters, each only when its key is set (their API keys are indexer secrets, never Cloud Build
  substitutions); a user whose configuration names a provider without a key gets a model outage. Gemini is not
  offered: its adapter takes no inline file bytes. Calls run with `allow_provider_storage=False` (OpenAI `store`
  off): the content is the user's files. Per-call limits (`DRIVE_READER` / `DRIVE_TRIAGE` in `agent_config.py`):
  reader 180 s and 16 000 output tokens, triage 120 s and 4 096, one parse retry, thinking `low` (a user's
  `agent_thinking` wins).
- **OpenAI file input is fixed in this slice** (adapter wire tests, `docs/how_to/ADAPTER_WIRE_TESTING.md`): a PDF goes
  as an inline `input_file` with a file name instead of a Files API upload that is never deleted, and a part that
  cannot be built raises instead of being dropped silently. The Claude adapter sends no PDF citations when a response
  schema is set.
- **Prompts live in Firestore** (tokens, blueprints, profiles per `docs/how_to/NEW_AGENT_PLAYBOOK.md`), uploaded by the
  owner and assembled by `build_drive_prompt` (no biography, no directives, no user overrides). A missing prompt, or an
  assembly without its `cognitive_process` and `output_format` sections, fails the call (no fallback prompt), is
  evicted from the prompt cache and is handled as a model outage (§24.8), logged ERROR.
- **Telemetry:** the indexer has no Logfire and no `prompt_content` capture; verbatim scans are not copied into
  BigQuery. The tick summary line carries the model counters and cost (§24.9); `[DriveReader]` logs one line per
  file read.

### 24.3 Extraction: the port, the router, page windows
- `ContentExtractorPort.extract(request) -> ExtractionOutcome`. The request carries the temp file path, name, sniffed
  kind, mime type, the caller (`user_id`, `account_id` — the parser ignores them; the reader bills and configures by
  them) and `max_model_pages`, the pages the reader may still send this month. The outcome status is one of `text`,
  `no_text`, `unsupported`, `too_large`, `encrypted`, `timeout`, `memory`, `error`, `budget_exhausted`, with `text`,
  `pages_total`, `pages_sent`, `truncated`, the brief when present, `extracted_by` (`id@version`), `model_calls` and
  `model_cost`. A model outage is not a status: the reader raises `DriveModelUnavailable` (§24.8), carrying what the
  file spent before it. `extract()` never raises otherwise.
- `EXTRACTOR_CHAINS` (pure domain table, §9): PDF `[parser, reader]`; image `[reader]` (after a `text` triage
  verdict); every other admitted kind `[parser]`. The router (`model_route`) calls the next link only on the §9
  signals: no text, low density (fewer than `MODEL_EXTRACT_DENSITY` characters per page), garbage text (more than
  `MODEL_EXTRACT_GARBAGE` of the visible characters in `(cid:N)` runs, replacement characters, or UTF-8 read as
  Latin-1 or CP1251), or a parser `error` / `unsupported`. A parser `timeout` or `memory` kill is a bomb and is never
  routed; `encrypted` is deterministic and never routed. `extracted_by` records the link that decided.
- **Sniffing before the chain.** The kind the chain runs on is the one the downloaded head shows (`sniff_kind`, magic
  bytes over the name); a sniffed kind the policy does not admit is `metadata_only / kind_not_admitted`. A file whose
  bytes show a kind that may reach the reader, when this tick has no time left for the reader's worst case, waits for
  the next tick.
- **Parser adapter** `SubprocessContentExtractor`: one child Python process per file (`python -B -E -s -m
  src.adapters.drive_parser.child`), one at a time, with `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_CORE` 0
  applied before any parser loads, a wall-clock kill of its process group, an allowlisted environment and a private
  temp directory; the result is a JSON file, never stdout. Inside it: pdfminer.six for PDF text, laid out page by page
  and stopped at `MAX_TEXT_CHARS` <!-- r2: P-m6 -->; pypdf for the page count (walked from the page tree, never from
  `/Count`), encryption and page windows; markitdown 0.1.8's allowlisted converters (Phase 0) for DOCX, PPTX, EPUB,
  MSG and HTML; openpyxl for XLSX (at most 1 025 columns, rendered per sheet, stopping at `MAX_TEXT_CHARS`;
  `truncated` when a cell past the column bound holds a value <!-- r2: P-m3 -->) and xlrd for XLS, both as Markdown
  tables capped at `MAX_SHEET_ROWS`; charset detection for text kinds (`charset-normalizer`, BOM first, a legacy
  code-page tie-break for uk/ru/en/es/fr); `striprtf` for RTF; the stdlib `email` parser for EML; ODF `content.xml`
  read with `defusedxml`; VTT/SRT reduced to cue text. Password-protected PDF, Office, EPUB and ODF files are
  `encrypted`; a ZIP container over the Phase 0 limits is `too_large`. Every library on this path is pinned in
  `requirements.txt`; a changed output for the same file is a `SubprocessContentExtractor.VERSION` bump. The memory,
  CPU and wall limits (1 GiB, 90 s, 120 s) are placeholders until spike S1.
- **Page windows.** One call cannot transcribe 200 pages, and a Haiku 5.5 prompt above 100k tokens costs five times
  as much on input and output. The child process counts a routed PDF's pages, then cuts one window of
  `MODEL_PAGES_PER_CALL` pages at a time, on demand, from the walked page tree <!-- r2: P-M1 --> (a temp PDF the
  indexer process never parses, read into memory and removed before the call), so at most `MODEL_IN_FLIGHT` windows
  exist on disk even when every page shares one large image; the windows run with at most `MODEL_IN_FLIGHT`
  concurrent calls and their texts are joined in page order. The parser's own status (`encrypted`, `timeout`,
  `memory`, …) when it cannot count or cut the PDF is the file's outcome.
- **Halving rules** (`DriveReaderRunner._piece`):
  - a window answered at the output limit is split in half (cut from the source) and read once more; a half or a
    single page still at the limit adds no text and sets `truncated`;
  - a window whose call timed out is halved once; a half that times out again fails the file (`error`, a counted
    per-file retry — the model answers, these pages are too dense for it); a single-page window that times out is a
    model outage <!-- r2: T-M2b -->;
  - a window the provider refuses as too large (HTTP 413) is halved down to one page; a single page still too large
    is `too_large`; a 400/422 refusal of any window is `unsupported`. Both demote: the same bytes get the same answer,
    and a retry would only bill the windows already read again <!-- r2: P-m5 -->. Output that stays invalid after the
    parse retry, or a refusal raised before any HTTP exchange, is `error`.
- **File budget.** When the file's model time reaches `MODEL_FILE_BUDGET`, the windows done so far are indexed and the
  record is `truncated` — no file restarts forever. A file whose windows were all cut by the budget with no text read
  is `error` (retried), never `no_text` <!-- r2: P-m4 -->. Pages past `MODEL_EXTRACT_MAX_PAGES` are not sent
  (`truncated`). An outage in any window cancels the file and carries what it spent before it (pages answered, calls,
  cost); the tick counts that before it records the outage.
- **Tick deadline (§7.1):** a file that may be routed starts only if
  `elapsed + DOWNLOAD_TIMEOUT + EXTRACT_WALL_TIMEOUT + MODEL_FILE_BUDGET + EMBED_FILE_BUDGET + margin < TICK_DEADLINE`;
  a test asserts it for the configured constants.

### 24.4 The brief and the caption (D8, D9, D13)
- **Brief** (reader, first window): `doc_type` (a short English label: invoice, receipt, contract, statement, letter,
  ticket, certificate, form, other; anything else reads as `other`), `description` (one line in the document's
  language, at most 300 characters), `doc_date` (ISO date or null; anything else is dropped, never fails the
  transcription), `parties` (at most 5 names), `amount` (value + currency as one display string, or empty). The prompt
  keeps the verbatim field verbatim; the brief fields are the only place for interpretation. Stored on the record as
  `brief` (a `DocumentBrief`).
- **Caption** (triage, photographs only): at most ten words, in English, stored as `caption`. It lives with the `photo`
  verdict: a rename away and back keeps both. <!-- r2: T-m3 -->
- **Card text** (`card_text`, `CARD_VERSION` 2): `name | kind | brief | caption | key facets | lead text`, each content
  segment only when present, the brief as `doc_type | description | doc_date | parties: … | amount`. The lead text is
  the first `CARD_LEAD_CHARS` of every indexed document's text, parsed or read (a `machine_text` demotion keeps it
  too); a photograph's card is `name | image | caption | key facets`. Brief, caption and lead text are model output or
  file text from untrusted content: they reach Smart only inside the nonce-tagged wrapper, like names and chunks.
- **`effective_date`** = the brief's `doc_date` when present, else the §3.1 order (D13). Only the reader writes a
  brief, so parsed documents keep the file date. The model is not told which kind matches which date: the guidance is
  one distinction (§24.10).

### 24.5 Image triage and image reading
- Every image record without a verdict at the current `TRIAGE_VERSION` on its content version is triage-due (class 2
  on first sight, class 3 after an upgrade). The tick gathers due images `TRIAGE_BATCH` per call; a partial last batch
  waits for the next page of the same class only while the pass progresses anyway, so N images cost ⌈N /
  `TRIAGE_BATCH`⌉ calls. Thumbnails come from `UserDrivePort.get_thumbnail(user_id, item_id, size) -> bytes | None`
  (Graph, served as JPEG or PNG, S0f), at `TRIAGE_THUMBNAIL_SIZE` (`medium`, 176 px; `large`, 800 px, is the live
  check's alternative). Each thumbnail goes with an opaque index and the camera prior (§5.3) as text.
- Verdict `text` → the record's content is due for the reader, in the class the triage had. Verdict `photo` →
  `metadata_only / photograph` (new reason) with its caption. **No thumbnail** may be one the provider has not made
  yet: it is retried with backoff, and only at `MAX_ATTEMPTS` is it `metadata_only / photograph` without a caption.
  A batch the provider refuses (400) is halved until the refused image is alone; that image, one left unanswered after
  the agent's one correction turn, and a thumbnail over the vision cap are photographs without a caption
  (deterministic). The verdict, `triage_version`, `triaged_content_version` and the caption are stored.
- The reader gets an information-bearing image as the provider's `READER_RENDITION_SIZE` rendition
  (`c2048x2048`, JPEG; HEIC is never decoded by us). **Spike S0g** confirms custom rendition sizes under
  `Files.ReadWrite.AppFolder` and that receipt text stays legible. With no rendition, the original is sent only when it
  is JPEG/PNG/WebP/GIF within the vision cap (3.75 MiB raw: 5 MiB once base64-encoded, Anthropic's per-image limit; to
  be confirmed in the live model check); other formats are `metadata_only / no_text`. A 413 on an image is
  `too_large`, a 400/422 `unsupported`. <!-- r2: P-m5 -->
- Done-when of §16 Slice 4 holds: 100 holiday photos cost ⌈100 / `TRIAGE_BATCH`⌉ triage calls and no reader call.

### 24.6 Chunks and the content streams
- Collection `{prefix}drive_index_chunks`, doc id `{user_id}:{item_id}:{generation}:{index}`, fields of §3.3 plus
  `content_terms`. Vector indexes under `user_id` and `user_id + kind`; composites for the term stream (with and
  without `kind`) and for generation collection; single-field exemptions for `text` and `vector`. Stream reads project
  away `text` and `content_terms` (they may come back empty); windows read chunks by id. The repository port gains the
  chunk methods; records and chunks stay in one repository.
- **Chunk stream** (§10.1 step 3): kNN (`CHUNK_FETCH`), fence, max-pool per record before fusion, window
  `index ± 1`. Off while the index's vectors are in another embedding space (`refreshing`), like the card stream.
- **Content terms** (`drive_content_terms.py`, decided in rev 3.8): one pure normaliser shared by indexing and the
  query. Index side, every form, at most 256 per chunk: amounts in one canonical form whatever the separators
  (`1 250,00`, `1.250,00`, `1,250.00` → `1250.00`) plus their integer part; numeric dates in ISO form (both readings
  when day and month are ambiguous); alphanumeric ids with their separators stripped plus their digit runs of three
  or more; whitespace-separated digit groups of 9+ digits joined (IBANs, phone numbers). A token holding `.` or `,`
  never yields its digits joined (`1250.00` is not `125000`). A normaliser change bumps `EXTRACTION_VERSION`.
- **Query side: maximal forms only** (review D-M1). `query_terms` takes each number-like span's maximal form — the ISO
  date, the canonical amount, the joined digit group, the joined id, the first kind claiming a stretch of text winning
  in that order — never a fragment (a year, an amount's integer part, a run inside an id), because a fragment as a
  query matches documents that share only that part. An amount whose cents are `.00` also queries its integer form, so
  `1,250.00` finds a document that says `1250` <!-- r2: S-m2 -->. The terms come from at most `QUERY_TERM_CHARS` of
  the query, cut at a word boundary, and the span claiming is linear <!-- r2: S-m1 -->; at most
  `CONTENT_TERM_QUERIES` terms, longest first, each matched by its own `array-contains` query ordered by item id (not
  one `array-contains-any`, which returns the first 100 by id and lets a common term hide a rare one).
- **Selectivity by distinct files** (review D-M2, arbiter R1-2 and R2 S-M1). A term page that was not full is
  complete and selects. A full page is an id-ordered slice of the term's chunks: the term is **non-selective** when
  the page spans more than `TERM_NONSELECTIVE_FILES` distinct files. A full page from at most that many files is
  probed past its last file, one new file per probe (`find_next_file_with_term`: the same query with a cursor on
  `item_id`, limit 1), until one file too many is seen or the term runs out; only then is it selective
  <!-- r2: S-M1 -->. A non-selective term takes no part in the fusion, in anchoring a window, in trimming a window
  around a term, or in the widening (it is never read again). A full page concentrated in few files — an IBAN in every
  chunk of one contract — still selects. The folder in-memory path reads every chunk of the subtree, so its pages are
  complete and need no probe.
- **Pooling and fusion.** Term hits are fenced and pooled per record: more shared query terms first, then the more
  specific term, then item id; the anchor is the record's chunk holding the most terms. The card, name, chunk and term
  streams are fused by RRF. A content hit counts only for a carded, visible record (§17.1 d).
- **Windows** (§10.2). The anchor (a term anchor wins over a kNN anchor) and its neighbours are read by id from stored
  text. All windows of one result set share `WINDOW_CHARS_TOTAL`: each hit keeps its best chunk — trimmed around its
  query term only when the best chunks alone exceed the budget, and then only down to the share the shorter ones leave
  — and neighbours are added in rank order, each whole, until one does not fit; after that no lower hit gets one.
  A trimmed or cut window is marked `shortened`, and Smart opens the file before quoting from it.
- Envelope `content_search = true`; `unread` filled by the tick; the `capped` hedge names all streams.

### 24.7 Budgets and bounds (layer 0, §5.5)
- `MODEL_EXTRACT_PAGES_PER_MONTH` per user (pages sent to the reader) and `TRIAGE_IMAGES_PER_MONTH` per user (images
  sent to triage). Before the reader is called, the file must fit what is left of the month: an image needs one page,
  a PDF its page count capped at `MODEL_EXTRACT_MAX_PAGES` (that cap when the parser could not count). A file that does
  not fit waits as `pending` in class 3 until the next month (UTC); it is never cut to what is left of this one, and
  only a file needing more than the whole monthly budget would be cut to it (review B-M4). Images over the month's
  triage budget wait the same way. Both are counters on `DriveSyncState` keyed by month, carried by every save of the
  tick (error paths included), shown in the Cabinet card and the tick summary.
- `MODEL_IN_FLIGHT` bounds concurrent model calls per file, and the tick hands files to the reader one at a time;
  separate from `EMBED_IN_FLIGHT` (D7): the model quota is not the Gemini embedding quota.
- Per-file time: download `DOWNLOAD_TIMEOUT`, parser `EXTRACT_WALL_TIMEOUT`, model `MODEL_FILE_BUDGET`, embeddings
  `EMBED_FILE_BUDGET`; per tick `TICK_FILE_BUDGET` runs within the start budget and the deadline rule of §24.3.

### 24.8 Model failures
`src/infrastructure/drive_model_call.py` sorts every failed call by what it says about the input:

| Outcome | Effect |
|---|---|
| Provider unavailable: 429, 5xx, a timeout of a call that cannot be halved, network, an open breaker, an account-level 4xx (key, permission, model id), a provider without a key, a missing prompt, anything unexpected | **Model outage, not a systemic error** (`DriveModelUnavailable`): `model_backoff_until` on the sync state (`MODEL_BACKOFF_BASE_S` doubling to `MODEL_BACKOFF_CAP_S`), records that need the model stay `pending` and are skipped until then, attempts untouched; parser work, cards and cascades go on. What the file or batch spent before the outage is counted against the month and the summary without ending the outage: the backoff keeps doubling. <!-- r2: T-M2a --> Successful model work ends it. |
| The provider refuses one input: 413 on a single page or an image → `too_large`; 400/422 on a window or an image → `unsupported` | Deterministic: `metadata_only`, no retry, no re-bill. <!-- r2: P-m5 --> In triage, the refused image is a photograph without a caption. |
| A window halved after a timeout times out again | Per-file transient (`error`, §8.2); at `MAX_ATTEMPTS` → `failed / extract_error`. <!-- r2: T-M2b --> |
| Output that is not valid JSON after the parse retry, a refusal before any HTTP exchange | Per-file transient. |
| The reader finds no text | `metadata_only / no_text` (deterministic). |
| Monthly budget spent | `budget_exhausted` / the pre-call hold: `pending`, class 3, due at the month boundary. |

### 24.9 Cost and billing
- Every reader and triage call is billed to the user's account through the agents' billing scope (`account_id` read
  from the user record on the indexer; the task payload stays `{user_id}`, Δ4); failed calls and parse retries are
  counted from the agent's ledger too.
- Prices (checked at the provider 2026-10-13): Claude Haiku 5.5 $0.10 / $0.50 per million input / output tokens for
  prompts up to 100k tokens, five times that above; GPT-6 Luna $0.10 / $0.50. A page costs about $0.0006, so the
  default page budget bounds a user at about $3 a month. The tick summary adds `indexed`, `failed`, `demoted`,
  `retried`, `deferred`, `chunks`, `embed_ms`, `model_pages`, `model_calls`, `triage_calls`, `triage_images` and
  `model_cost`.

### 24.10 Agent surface (D11)
- The intents stay as they are. `search_files_in_drive`'s description and `drive_query` field description say that
  content questions put the subject, a phrase, a number or an amount in `drive_query`.
- Dates are one distinction, the same sentence in the `drive_query` and `drive_date_from` descriptions: dates written
  inside the document go in `drive_query`; the date filters only when the user limits when the file is dated. The
  token keeps only the retry rule (a date filter that finds nothing is retried without it before "not found").
- The §10.4 protocol lines go into Smart's protocol token (`drive_specialist` in `PROTOCOL_AGENT_SELECTION`, backed up
  first; `--apply` is run by the owner): search when the answer may be in a document, freshness, open a shortened
  passage before quoting, honest not-found, open the file for sums (a window is not a table), document vs memory,
  versions. `render_results` keeps the per-result facts and drops the standing guidance that moves to the token, so
  the render change and the token upload ship together.
- `capabilities.py` says the drive answers from content.

### 24.11 Eval and tests
- §17 in full, plus: the router's signals; page windows (join order, output-limit split, timeout and 413 halving,
  file budget, truncation); triage batching, verdict mapping and the photo invariant; model outage keeping parser work
  going; budgets across a month boundary; `effective_date` from the brief; term selectivity; the window budget; the
  OpenAI adapter's file input (wire tests). `tests/integration/test_drive_g2_end_to_end.py` runs every text-layer G2
  case through the real parser with scripted models.
- The eval fixture folder gains file bytes: documents generated by a committed script (DOCX, PDF with and without a
  text layer, broken-font Cyrillic PDF, XLSX, CSV, RTF, EML, ODT, VTT) and rendered scans and screenshots (UA, RU, ES,
  EN), PII-free. `gold_g2.json` adds `expected_substring` cases (clauses, amounts, invoice numbers, table-heavy and
  two-column PDFs); OCR-labelled G1/G2 cases lose their expected-miss label. Bars are recorded with the real reader and
  real embeddings and pin the reader's provider; unit tests use fakes.
- G3: a Smart dry run (the `ab_agent_models.py` scaffolding) in which the drive read intents run for real against the
  fixture index; positives and a negative set; the search-initiated rate is recorded. Each case also says where its
  date belongs (`expect`: `date_in_query`, `date_filter` or `none`), scored from the fields of Smart's search calls as
  its own rate, barred over the date cases (`date_in_query`, `date_filter`) only, with synthetic date cases added so
  the bar has enough of them <!-- r2: S-m3 -->. The bars are the Wilson lower bounds (z = 1.96) of the recorded rates,
  judged only at equal repeats.

### 24.12 Acceptance
§17 and §24.11 green; the eval meets its recorded bars (G1, G2, OCR-labelled, G3 positives; the G3 negative rate
reported); the §16 Slice 4 photo invariant; the Office Online edit-then-ask scenario answers from the edit or states
the index time; live checks with the owner on real files (`scripts/validation/drive_model_check.py`, then
`drive_eval.live_check` with the drive queue paused): the model pick (Haiku 5.5 or GPT) and the per-window timeout,
the thumbnail size, a scan, a broken-font Cyrillic PDF, a screenshot and a photo folder, each found by subject and
answered from content.

### 24.13 Parameters (initial)
Values in `src/domain/drive_index_params.py` unless named otherwise.

| Name | Initial | Basis |
|---|---|---|
| `QUIET` / `MAX_DEFER` | 10 min / 60 min | S0b: Word for the web saves every 1.2–3.3 min while typing |
| `DOWNLOAD_TIMEOUT_S` / `EXTRACT_WALL_TIMEOUT_S` / `EMBED_FILE_BUDGET_S` | 120 / 120 / 180 s | §7.1 deadline sum; S1 may revise the parser's |
| `MODEL_FILE_BUDGET_S` | 600 s | Fits the §7.1 deadline sum with the other per-file budgets |
| `TICK_FILE_BUDGET` | 50 | Index runs per tick |
| `MAX_DOWNLOAD_BYTES` / `MAX_SHEET_DOWNLOAD_BYTES` | 45 MiB / 10 MiB | Step 1 cap; sheets amplify most (Phase 0) |
| `MAX_TEXT_CHARS` / `MAX_SHEET_ROWS` | 2 000 000 / 5 000 | Text and table bounds |
| `CHUNK_CHARS` / `CHUNK_OVERLAP` / `MAX_CHUNKS_PER_FILE` / `MAX_CHUNKS_PER_USER` | 2 400 / 240 / 400 / 50 000 | §8.4, §5.4 |
| `CARD_LEAD_CHARS` | 2 000 | Lead text in the card |
| `MODEL_EXTRACT_DENSITY` / `MODEL_EXTRACT_GARBAGE` | 200 chars per page / 0.05 | §9 router |
| `MODEL_EXTRACT_MAX_PAGES` | 200 | Pages read per file |
| `MODEL_PAGES_PER_CALL` | 10 | ≈ 15–40k prompt tokens on Claude, far below the 100k price step; ≈ 8k output tokens |
| `MODEL_IN_FLIGHT` | 4 | A 200-page PDF in ≈ 5 rounds of ≈ 60 s |
| `MODEL_EXTRACT_PAGES_PER_MONTH` | 5 000 | ≈ $3 per user per month |
| `TRIAGE_IMAGES_PER_MONTH` | 20 000 | ≈ 1 250 triage calls |
| `TRIAGE_BATCH` | 16 | §15 |
| `TRIAGE_THUMBNAIL_SIZE` / `READER_RENDITION_SIZE` | `medium` (176 px) / `c2048x2048` | Live check / spike S0g |
| `MODEL_BACKOFF_BASE_S` / `MODEL_BACKOFF_CAP_S` | 60 s / 3 600 s | §7.7 doubling backoff |
| Reader / triage call timeout and output cap (`agent_config.py`) | 180 s, 16 000 tokens / 120 s, 4 096 tokens | Calibrated by the live model check |
| Vision cap (`MAX_VISION_IMAGE_BYTES`, `drive_image_mime.py`) | 3.75 MiB raw | 5 MiB base64, Anthropic's per-image limit |
| Parser `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_FSIZE` (`subprocess_content_extractor.py`) | 1 GiB / 90 s / 64 MiB | Placeholders until S1 |
| `CHUNK_FETCH` / `FOLDER_INMEM_CHUNKS` | 300 / 2 000 | Chunk kNN page; folder chunks ranked in memory |
| `CONTENT_TERM_QUERIES` / `CONTENT_TERM_FETCH` | 8 / 100 | Term queries per search; term page size |
| `TERM_NONSELECTIVE_FILES` | 8 | Distinct files a full term page may span and still select |
| `QUERY_TERM_CHARS` | 1 000 | Query characters terms are read from <!-- r2: S-m1 --> |
| `WINDOW_CHARS_TOTAL` | 12 000 | All windows of one result set (≈ 3–4k tokens) |

### 24.14 Known limits
- **The parser sandbox is resource isolation, not code-execution containment.** The child process bounds memory, CPU,
  file size and wall time, and starts with an allowlisted environment, but it runs as the same user on the same host:
  a parser exploit with code execution could read the parent's environment through `/proc/<ppid>/environ` and reach
  the metadata server for the service account's token. Accepted for a single-owner deployment parsing the owner's own
  files. *Trigger to revisit:* a code-execution CVE in a parser the child loads (markitdown and its converters,
  pdfminer/pdfplumber, openpyxl, python-pptx, mammoth, lxml), or indexing files of untrusted users (multi-tenant
  growth); the answer then is a separate parser service with its own identity (§9's "separate service" adapter).
  Decision: `decisions/drive_parser_sandbox_resource_isolation.md`.
- **A tick that ends with `LeaseLost` loses its model usage.** The month's page and image counters live on the sync
  state, and a tick without its lease cannot write it, so the model calls of that one tick are not counted against
  the monthly budgets (they are still billed to the account per call). Bounded to one tick's work
  (`MODEL_IN_FLIGHT` windows and the triage batches of one tick); the next tick counts from the last saved state.
- **The text gate's variety bar is calibrated on synthetic text.** `MIN_VARIETY` 0.04 separates the measured dumps
  (≤ 0.015) from a 1 500-line statement over 200 merchants (≥ 0.10); a real statement with far fewer merchants may
  still read as machine text. *Trigger:* a statement or ledger demoted `machine_text` in the live check. Decision:
  `decisions/drive_text_gate_financial_documents.md`.
