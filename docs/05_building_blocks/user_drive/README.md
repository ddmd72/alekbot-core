# User Drive (Building Block)

## 1. Overview

The user's long-term file area: their OneDrive App Folder, behind the provider-neutral `UserDrivePort`.
- **Step 1, live** ([USER_DRIVE_RFC.md](../../10_rfcs/USER_DRIVE_RFC.md)): the bot lists, opens, saves, moves,
  creates folders in, appends to and deletes files through seven `*_drive` intents on FileManagement. The model and
  history see only `drive:<item_id>` refs and `[Drive: …]` labels, never a provider name.
- **Step 2, Slice 1** ([USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md)): an opt-in index
  that finds a file or folder by name (exact, partial, transliterated, misspelled), kind, date, folder, photo facets
  and place. **No file content is read in Slice 1.** Content search (Slice 2), one extraction path (Slice 3) and the
  model extractor for scans and images (Slice 4) are not built.

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
          → UserDrivePort (get_changes / get_change / list_children_changes)
          → GeminiEmbeddingAdapter (card vectors)
          → FirestoreDriveIndexRepository ({prefix}drive_index_records, {prefix}drive_sync_state)

Smart → delegate_to_specialist(search_files_in_drive) → FileManagementAgent
  → DriveSearchService → FirestoreDriveIndexRepository (card kNN, name keys, filters)
```

Deploy units, queue, scheduler job and the two-pass first deploy: [docs/07_deployment/README.md](../../07_deployment/README.md)
→ Drive Indexer Service and [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md).

### 2.1 Ports and adapters

| Port | Adapter(s) | Slice 1 role |
|---|---|---|
| `UserDrivePort` | `OneDriveAdapter` | Step 1 CRUD, plus `get_changes` (delta), `get_change`, paged `list_children_changes(limit, cursor)`, `connection`, `flag_reconnect` |
| `DriveIndexRepository` | `FirestoreDriveIndexRepository`, `InMemoryDriveIndexRepository` | records, sync state, lease, card kNN, name keys, filter queries. The in-memory one runs the tests and the eval |
| `EmbeddingService` | `GeminiEmbeddingAdapter` (`gemini-embedding-2/768`) | card vectors, query vectors |
| `TaskQueue` | `GcpTaskQueue` (second instance) | bound to `drive-sync-dev` and the indexer's `/worker`; built only when `DRIVE_INDEXER_URL` is set |
| `AlertSinkPort` | `SlackWebhookAdapter` (the ops sink) | the fan-out's staleness alert |

No port for the policy, the planner, the card builder or the place lookup: they are pure domain code
(`src/domain/drive_index*.py`, `drive_card.py`, `drive_name_keys.py`, `place_lookup.py`) and decide everything the
tick does, tested without Firestore, Graph or a clock.

---

## 3. The index

- **One record per drive item** (`DriveIndexRecord`, doc id `{user_id}:{item_id}`). It stores the last observation
  (name, parent id, kind, size, raw facets), `scope` and `state`/`reason`, scheduling fields (`due_at`, `priority`),
  `name_tokens` and the card (`card_hash`, plus the `card_vector` field in Firestore). **No path is stored**: paths
  are derived from the `parent_id` chain at read time, so a folder rename touches no descendant.
- **One sync state per user** (`DriveSyncState`, doc id `user_id`): delta cursor, drive and root ids, epoch and
  phase, lease, `observed_as_of`, `access_lost_at`, `purging`, `systemic_failures`, applied versions, counters.
  `save_sync_state` never writes the lease fields, and `release_lease` clears them only for the owner. A tick
  writes nothing once its clock reaches `lease_until - LEASE_MARGIN`, and `save_sync_state` / `delete_sync_state`
  apply only while it still holds the stored lease (`LeaseLost` otherwise, a quiet stop; RFC Δ26).
  A subtree that must be listed again (a folder leaving an opaque root, or restored after a delete) is a flag on the
  folder's own record (`reobserve_pending`, `reobserve_cursor`), worked page by page in class 2 (RFC Δ18).
- **Policy (§5).** Slice 1 admits no content kind: every file is `metadata_only` (reason `kind_not_admitted`, or a never-download rule) with a card.
  Dot-names, secret-shaped names (`id_rsa*`, `*.kdbx`, …), OS artefacts and remote/package/malware items keep a card
  with their reason. A dot-folder, a folder with a `.git` or `pyvenv.cfg` child, a `node_modules`, `__pycache__` or
  `site-packages` folder anywhere (RFC Δ16), or a dense tree (more than 500 children, mostly neither document nor
  media, judged from a probe of the first 200 and kept until the child count doubles or halves) becomes one
  `opaque_root` card. The files inside get no record; its subfolders and the marker are kept without a card, so a
  deleted `.git` re-opens the folder. Why each rule exists:
  [decisions/drive_problem_content_classes.md](../../04_solution_strategy/decisions/drive_problem_content_classes.md).
- **Card** (`card_text`): `name | kind | key facets`, where the facets are the photo's taken date, camera,
  `place_name`, image dimensions, and video duration and dimensions. The place name is resolved offline from
  `src/assets/geo/places_v1.tsv` (GeoNames, RFC Δ7: the heaviest place within 15 km, else the nearest within
  50 km) and stored on the record. A card is re-embedded only when its hash changes.

---

## 4. The tick

`DriveIndexingService.tick(user_id, nudge)` runs only on the indexer service, one user at a time.
1. **Gate**, from Firestore, never from the agent cache: indexing off with anything stored → purge; no credentials or
   `needs_reconnect` → stamp `access_lost_at` once and stop; past 30 days → purge; a different `drive_id` → purge.
   The delta cursor carries the drive id and App Folder id it came from: a mismatch with a fresh App Folder read
   restarts the enumeration before the old cursor is followed, and a page from another drive purges and re-onboards.
2. **Lease** in a transaction. A live lease answers `busy`; a nudge then re-enqueues itself.
3. **Observe**: items named in a nudge first (`get_change`, their cards due in class 0), then delta pages until the
   round completes or the 8-minute start budget is spent. A full enumeration (onboarding, an expired cursor) opens an
   epoch; the sweep at its end deletes what it did not see.
4. **Work off** due records after a complete round, by class: 0 edits and nudges, 1 cascades (folder deletes,
   rescopes, probes), 2 first-seen cards and subtree re-listings, 3 maintenance (version upgrades, card-cap
   release), 4 retries. When one class runs out of budget, lower classes wait (RFC Δ19).
5. **Continue**: if anything is left, enqueue a continuation; release the lease.

Purge is keyed on facts only, never on an exception: a rejected refresh sets `needs_reconnect` and stops. Errors are
classified by what the call is about (§8.2):
- **Per item.** A probe, a parent lookup or a re-listing page that fails defers that one item (retried with
  backoff). After `MAX_ATTEMPTS` a probe or a re-listing is settled without it (not dense; the subtree not
  re-listed), and a record whose parent lookup keeps failing is removed as an orphan (a folder with its stored
  subtree); a failed nudge read is left to the delta feed. Item calls share
  one budget (`ITEM_CALLS_PER_TICK` = 20, RFC Δ9) and also stop once the tick's time is spent. A change that fails inside a page is skipped and logged at ERROR, and
  the page still applies. A card whose embedding is refused with HTTP 400 backs off alone.
- **Systemic.** The delta feed, the token endpoint, any other embedding error and the repository end the tick. A
  throttled drive waits the throttle hint (the provider's Retry-After; a flat 60 s for a failed token refresh);
  anything else backs off `min(60·2^n, 1800)` s. The error is written onto
  the state as last saved, so progress saved page by page is kept.

Indexing turned off mid-tick drops the in-flight result and enqueues a continuation, whose gate purges.
Opt-in and access loss: [decisions/drive_index_opt_in_access_loss_grace.md](../../04_solution_strategy/decisions/drive_index_opt_in_access_loss_grace.md).
Lease, queue and service: [decisions/drive_indexer_lease_queue_service.md](../../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md).

---

## 5. Search

`search_files_in_drive` (SYNC, FileManagement; context fields `drive_query`, `drive_folder`, `drive_kind`,
`drive_date_from`, `drive_date_to`) calls `DriveSearchService.search`.
- Step 0 reads the opt-in flag, sync state and connection uncached: `disabled`, `disconnected`, or results marked
  `reconnect_needed`. While a purge runs, a connected drive gets no results and `building`.
- With a query: card kNN and a lexical name stream, fused with RRF (k = 60). The name stream gives each whole-token
  key (transliterations included) and each trigram key its own query, at most `NAME_KEY_QUERIES` = 8 of each in
  query order, plus one query for the remaining keys; they run in parallel, are merged by id and cut by the
  `name_match` floor (one whole token or three trigrams). Firestore returns the first 100 matches by id, so a shared
  query would let a common key crowd out the target: `4912` among 1 000 `IMG_…` names, or a misspelled `contarto`.
  After RRF, records whose name covers every query token rank first (RFC Δ17). No cosine threshold; `K = 8` and
  Smart's judgement bound the output. Fewer than K results with a full page widens the fetch once, to 1 000: at
  most 18 parallel queries per round (17 name, 1 kNN), 36 with the widening.
- Without a query (filter-only, "all my spreadsheets"): a plain query on kind and `effective_date`, newest first.
  `drive_kind` takes a format (`pdf`, `xlsx`, …) or a family: `spreadsheet`, `document`, `presentation`, `photo`
  (RFC Δ20); an unknown value is refused. Date-only bounds are UTC days. When the fetch cap, not the data, empties
  the page, the answer says so and points to `list_files_in_drive`; a request with no words and no filters is
  answered as "the N newest files"; while building, no "0 match" count is given.
- `building` is true while an epoch is not idle or a first-seen record (class 2) is due.
- A folder filter is a subtree, resolved by ref or by path; several matching folders come back as candidates, never
  guessed (a single suffix match from a full name page is refused, a single exact path is used). A subtree of up to
  1 000 records is ranked in memory, a larger one is a post-filter.
- Duplicates (same `quickXorHash` and size) collapse into one card with `also_at` paths.
- Each card comes back inside its own nonce-tagged, escaped `<drive_document>` wrapper, after a coverage envelope:
  `building`, `index_as_of`, `reconnect_needed`, `index_full`. The same wrapper carries `open_file_from_drive` text.

The drive is not part of `SearchEnrichmentService`:
[decisions/drive_index_not_in_search_enrichment.md](../../04_solution_strategy/decisions/drive_index_not_in_search_enrichment.md).

---

## 6. Operations

- **Logs.** On the indexer service, one `[DriveIndex] tick user=… end=done|continued {…} duration=…` line per tick
  that completes, with counts such as `observed`, `pages`, `skipped`, `cards`, `swept`, `deleted`, `rescoped`; a
  throttled or failed tick logs its own WARNING or ERROR line instead, and a purge logs `index purged`. An idle tick
  still observes a few items, since the root and re-emitted unchanged items arrive in every delta round (S0a), but
  it adds no `cards`. A skipped item logs one ERROR line. The indexer has no Logfire telemetry: Cloud Logging only.
  Read it with `make fetch-logs-indexer K=300 && grep '\[DriveIndex\]' alek_debug_indexer.log`; `make fetch-logs`
  reads the main service, which holds the `[DriveSync]` fan-out and `[DriveSearch]` search lines.
- **Alerts.** The fan-out posts to the ops sink when a user's index is stale (no clean tick, or an overdue edit, for
  `STALE_ALERT_AFTER` = 2 h), once per incident. On a lost grant it posts one reconnect notice to the user; the flag
  is set before sending, so a cut-off notice is never repeated but may be lost.
- **Cabinet.** `/api/drive/status` reads the indexing block in its own try: on failure it logs ERROR and leaves the
  block out, so Connect, Disconnect and Reconnect always show.
- **Eval.** `venv/bin/python -m scripts.validation.drive_eval` indexes a synthetic drive
  (`tests/fixtures/drive_eval/tree.json`) with the real tick and real embeddings (`GEMINI_API_KEY` from `.env`), runs
  the gold cases (`gold_g1.json`) through the real search, and fails when recall@8 falls below the recorded bars
  (`bars.json`, written by the first owner-reviewed run). `--record` re-records them; do that only for a deliberate
  change, and review the misses it prints first.
- **Rebuild.** Turning indexing off, waiting until the purge has run, then on again purges and re-onboards. The
  purge shows only as the indexer's `index purged` line. Empty `indexing.counters` in `/api/drive/status` is not a
  sign of it (the counters stay empty until a round settles), and the Cabinet hides the counters as soon as indexing
  is off. A quick off/on can find the
  index still there and keep it: the toggle tick then sees indexing on with a state stored and proceeds.

---

## 7. Code references

| Piece | File |
|---|---|
| Domain model, parameters | `src/domain/drive_index.py`, `src/domain/drive_index_params.py` |
| Policy, planner, card, name keys, task keys | `src/domain/drive_index_policy.py`, `drive_index_planner.py`, `drive_card.py`, `drive_name_keys.py`, `drive_task_keys.py` |
| Place lookup | `src/domain/place_lookup.py`, `src/adapters/geo_place_table.py`, `src/assets/geo/` |
| Search result, wrapper; Cabinet status | `src/domain/drive_search.py`, `src/domain/drive_indexing_status.py` |
| Ports | `src/ports/user_drive_port.py`, `src/ports/drive_index_repository.py` |
| Adapters | `src/adapters/microsoft/onedrive_adapter.py`, `src/adapters/firestore_drive_index_repo.py`, `src/adapters/in_memory_drive_index_repo.py` |
| Services | `src/services/drive_indexing_service.py`, `drive_search_service.py`, `drive_sync_scheduler_service.py`, `user_drive_service.py` |
| Wiring, indexer routes | `src/composition/drive_indexing_setup.py`, `indexer_main.py`, `src/web/indexer_worker_app.py` |
| Agent, manifest | `src/agents/file_management_agent.py`, `src/infrastructure/agent_manifest.py` |
| Eval | `scripts/validation/drive_eval/`, `tests/fixtures/drive_eval/` |
