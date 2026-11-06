# RFC: Drive scope by folder walk — a scope change without re-reading the account

**Status:** Accepted (owner, 2026-11-06) — **step 1 built** (see "Built (step 1)" at the end: ten plan deltas and two review rules); step 2 not built.
**Owner:** AI Engineering
**Builds on:** [MULTI_DRIVE_RFC.md](MULTI_DRIVE_RFC.md) (§11.3 scope and access policy; E70, E71, E87),
[GOOGLE_DRIVE_RFC.md](GOOGLE_DRIVE_RFC.md) (§3.3 the feed; GE-14, GE-35, GE-36),
[USER_DRIVE_INDEXING_RFC.md](USER_DRIVE_INDEXING_RFC.md) (the tick, §5.2 re-listing, §7.5 sweep, §7.6 cascade, Δ9,
§27.9 transcript mirror), [DRIVE_FILE_NOTES_RFC.md](DRIVE_FILE_NOTES_RFC.md) (C1).
**Supersedes in part:** MULTI_DRIVE_RFC **E71** and **E87** (for folder scopes) and the parent-chain paragraph of §11.3;
GOOGLE_DRIVE_RFC **GE-14** (for folder scopes); USER_DRIVE_INDEXING_RFC **Δ9** (re-listing pages and parent lookups
leave the item-call budget). §6 says exactly what each becomes.
**Owner's map:** [docs/05_building_blocks/user_drive/README.md](../05_building_blocks/user_drive/README.md) (it describes
the built code; it changes when a slice of this RFC lands).
**Execution plan:** written from this RFC (private ops repo).

Notation: **D-n** owner decisions (§3), **W-n** engineering decisions (§13, each with its reason and the rejected
alternative), **P-n** provider facts (§4). Code cites are `main` @ `5b914643`. Provider behaviour is tagged **[DOC]**
(an official Google or Microsoft page, quoted in §4) or **[INF]** (inference, not seen in a document or in code).
Folder names are invented ("Projects", "Projects/2026", "Archive").

---

## 0. In one paragraph

Today the index keeps only a hash of the scope it was built under, so any Cabinet scope change restarts the
connection's feed and re-reads the whole account (E71); and on an account-wide feed each changed item is judged by
walking its parent chain at the provider, twenty item calls a tick (Δ9), which is why a narrow scope builds slowly and
needed the E87 drain. This RFC stores the folder set the index was built under and changes it by **diff**: an added
folder is walked (only its subtree is listed), a removed folder's index is deleted at once (only its subtree), and the
rest of the index is untouched (**step 1**). Then a folder scope's index is **built by walking** the chosen folders
instead of enumerating the account, so every in-scope folder is a stored record and a feed item is judged **locally**:
its parent is a stored in-scope folder or it is out, with no provider call (**step 2**). "Whole drive" keeps the full
enumeration. The change feed itself stays one account-wide (Google) or area-wide (OneDrive) cursor; only the judging
becomes local.

## 1. Problem

1. **A scope change re-reads the account.** Adding one folder, removing one, or nesting one under another restarts the
   feed: cursor cleared, epoch + 1, a full enumeration of every item the account sees, then the sweep. Unchanged
   records keep their content (no model money), but the provider calls and the time are paid for the whole account,
   for a change that concerns one subtree. The owner rates this as critical: a scope change must cost what changed.
2. **A folder scope builds slowly and keeps an out-of-scope backlog.** On Google the initial build of a folder scope
   lists the whole account, and every item whose parent chain is not yet known costs `get_change` reads from a shared
   budget of 20 item calls per tick. A narrow scope over a busy account therefore stores `unresolved` records for
   out-of-scope items and works them off for many ticks (E87 records the consequence and its drain).
3. **The parent-chain verdict is memoised per tick only**, so the same out-of-scope folders are read again tick after
   tick.

Nothing here is a visibility leak: the access policy reads the connection fresh on every drive operation (E70) and
search re-checks scope over the index's own chain (§2). It is cost, time and tidiness.

## 2. Current mechanism (verified at `5b914643`)

- **The connection holds the set; the index holds a hash.** `DriveConnection.scope_folder_ids` / `scope_whole_drive`
  (`src/domain/drive_connection.py:75-76`); `scope_key` is `""` (default), `"whole"`, or the first 16 hex digits of the
  sha256 of the sorted ids (`src/domain/drive_access_policy.py:104-112`, `drive_connection.py:111-114`). The sync state
  stores only that key, `applied_scope: str` (`src/domain/drive_index.py:588`). No diff is possible.
- **Any change restarts the feed.** A tick whose `applied_scope != connection.scope_key` with a cursor calls
  `scope_restart` (`src/services/drive_indexing_service.py:879-886`): cursor None, epoch + 1, `ENUMERATING`
  (`src/domain/drive_index_planner.py:206-210`). The re-enumeration re-observes every item, the filter deletes what
  left, the epoch sweep deletes what was not seen (`drive_indexing_service.py:2336-2362`); an unchanged record only
  gets its epoch re-stamped (`drive_index_planner.py:613-622`), and GE-36 skips the revision read when the modified time
  is unchanged.
- **The initial build lists the whole account** on Google: `files.list corpora=user`, then one pass per member shared
  drive, `orderBy=folder`, 1000 per page (GE-14; `src/adapters/google/google_drive_adapter.py:507-533`, `PAGE_SIZE`
  `:97`). The feed is account-wide (`feed_scope=ACCOUNT`, `:180`). OneDrive's feed is the App Folder's delta
  (`src/adapters/microsoft/onedrive_adapter.py:107`, `feed_scope=AREA` `:342`).
- **Parent-chain verdicts.** On a filtered scope each change is judged by `verdict`
  (`src/domain/drive_index_scope.py:44-71`); unknown links are read with `get_change` (`drive_indexing_service.py:1404-
  1427`, `_scope_verdict` `:1448-1466`), memoised per tick in `_ScopeView` (`:562`). Out of budget → stored `UNRESOLVED`
  and judged again in `_resolve_step` (`:2613`); a False chain drains its unresolved siblings (`_drain_out_of_scope`,
  `:2719-2760`, E87). Probes, parent lookups and re-listing pages share `ITEM_CALLS_PER_TICK = 20`
  (`src/domain/drive_index_params.py:26`, Δ9). Feed pages are bounded by time only (`_observe`, `:1143-1173`).
- **Pieces this RFC reuses:**
  - *Subtree walk* — a folder's `reobserve_pending` + `reobserve_cursor`; `_reobserve_step` lists one page
    (`list_children_changes`, `RELIST_PAGE` = 200) and applies it with `relisting=True`; a child folder **new to the
    index** gets `reobserve_pending`, so the walk recurses, resumes per folder and never descends into a folder already
    stored (`drive_indexing_service.py:2799-2831`, `_adjusted` `:1310-1313`). Google lists one folder as
    `'<id>' in parents and trashed=false` (`google_drive_adapter.py:410-436`). An opaque folder is not listed
    (`:2802-2804`).
  - *Moved-in folder* — a folder new to a filtered index during a delta round is listed once (`_adjusted`,
    `:1299-1309`).
  - *Moved-out folder* — an out-of-scope verdict becomes a deletion for the planner (`_observe_one`, `:1286-1293`); a
    folder becomes `DELETING` and `_cascade_step` removes its subtree post-order, collecting chunks (`:2560-2586`).
  - *Rescope* — a folder whose classification changes gets `rescope_pending`; `_rescope_step` re-classifies its stored
    children page by page (`drive_index_planner.py:602-605`, `drive_indexing_service.py:2770-2797`).
  - *Skeleton ancestors* — the folders from the root to each scope folder are stored as `SCOPE_ANCESTOR` records
    (`drive_index_planner.py:197-203`), loaded per tick by `_load_scope_ancestors` (`drive_indexing_service.py:
    1429-1446`). Today they are only ever added.
  - *Keep guard* — E87's drain never removes scope folders, ancestors, skeleton records or mirror roots
    (`drive_indexing_service.py:2739`).
  - *Normalised save* — `set_scope` drops a folder whose ancestor is also selected (`_outermost`,
    `src/services/drive_connection_service.py:137`, applied `:426`); the Cabinet nudges the indexer after a save
    (`src/web/user_cabinet_app.py:1767`).
- **Defaults differ by feed.** `IndexScope.covers_everything` = whole drive, or an `AREA` feed with no folders
  (`drive_index_scope.py:33-36`); `in_scope` with no folders admits everything on `AREA` and nothing on `ACCOUNT`
  (`drive_access_policy.py:126-135`).
- **Read-time gates.** The access policy reads the connection on every operation (E70); search re-checks scope as a
  second line (`src/services/drive_search_service.py:491-537`).
- **The mid-tick race.** The tick reads the connection once (`drive_indexing_service.py:819`) and builds its
  `_ScopeView` from it (`:994-995`); a save after that point is seen by the next tick.

## 3. Owner decisions (2026-11-06)

| # | Decision |
|---|---|
| D1 | **The model.** Step 1: the index stores the folder set it was built under and a scope change is applied as a diff — walk only added subtrees, delete only what removed folders covered, guarded by what is still in scope. Step 2: a folder scope's index is built by walking the chosen folders, and a feed item is judged locally (its parent is in the stored in-scope folder set). Switching from folders to the whole drive keeps a full enumeration. Scope per file: not now; possible later only as "also these files" next to folders. |
| D2 | **A folder removed from the scope**: its index (records, cards, passages) is deleted at once. No soft-hide period. |
| D3 | **Transcripts of recordings that leave the scope**: as today — kept while their drive copy (the mirror file) exists, otherwise they age out under the existing orphan TTL (`TRANSCRIPT_ORPHAN_TTL`, `drive_index_params.py:253`). |
| D4 | **Cabinet.** One progress line per drive (not per folder). Going from the whole drive to folders shows a warning of what leaves the index and that adding it back costs reading again. A scope folder deleted on the drive shows "folder missing" until the owner removes it from the scope. |
| D5 | **No migration machinery.** The first tick under the new code adopts the stored hash if it equals the connection's current `scope_key`; otherwise it runs one last old-style restart. No wipe, no rebuild. |

## 4. Provider facts

| # | Fact | Source |
|---|---|---|
| P1 | Google `changes.list` has no folder parameter: the feed is per user (or per shared drive). The feed stays account-wide in this RFC. | GOOGLE_DRIVE_RFC §3.3 [DOC changes.list] |
| P2 | Google's search guide lists `or` among the query's logical operators and shows `'<id>' in parents` for **one** folder; it has no example combining several parents. Neither the guide nor the `files.list` reference states any limit on the length of `q` or the number of clauses. `pageSize` maximum is 1000. | [DOC search-files], [DOC files.list]: "The maximum value is 1000; values above 1000 will be coerced to 1000." |
| P3 | A community report (not official) saw an `or` chain of `in parents` clauses fail with "The query is too complex" at about 600 clauses / 30 000 characters. Treated as unverified. | Stack Overflow question 29738020 (community) |
| P4 | Microsoft Graph `delta`: the v1.0 reference lists only `…/root/delta` request paths for a drive, while its SDK snippets address `drives/{id}/items/{id}/delta`. The App Folder page documents `GET /drive/special/approot:/{path}:/delta` ("View changes on an Item"). No current official page says that OneDrive for Business or SharePoint supports `delta` only on the root; the old OneDrive API page (`driveitem_delta.md` in `onedrive-api-docs`) is now an empty redirect to the Graph page. The claim stays **unresolved**, and this RFC never calls `delta` on a scope folder, so it does not matter (W16). Delta on the App Folder works for personal OneDrive (spike S0a, `onedrive_adapter.py:20`). | [DOC driveitem-delta], [DOC special-folders-appfolder] |
| P5 | Graph `delta?token=latest` "returns empty response with latest delta token", i.e. a cursor at "now" without enumerating. Its use on the App Folder path is [INF]. | [DOC driveitem-delta], Example 3 |
| P6 | Graph: paging `children` "is not guaranteed to return every single item if any writes take place during the enumeration. Using `delta` is the only way to guarantee that you've read all of the data you need to." | [DOC driveitem-delta], Example 3 note |
| P7 | Graph: "renaming a folder doesn't result in any descendants of the folder being returned from **delta**. When using delta you should always track items by id." Google: a folder's trash and untrash each emit a change for every descendant [PROBE P-a, GOOGLE_DRIVE_RFC §3.3]; a parent change of a folder emits the folder only [INF], consistent with the moved-in rule (`:1299-1309`). | [DOC driveitem-delta] Remarks |

Sources:
- [DOC search-files] https://developers.google.com/workspace/drive/api/guides/search-files
- [DOC files.list] https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
- [DOC driveitem-delta] https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0
- [DOC special-folders-appfolder] https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/special-folders-appfolder
- Community (P3 only): https://stackoverflow.com/questions/29738020

**Consequence.** We never poll folders. There is one feed per connection, and on Google it is account-wide by API
design; it costs one call per page whatever the scope. The cost was never the feed but the **judging** of each feed
item, which step 2 makes local.

## 5. The model

### 5.1 Normal form of a scope

The **applied scope** is `WHOLE` or `FOLDERS(roots)`. An `AREA` feed with no folders chosen is `FOLDERS({area root})`;
an `ACCOUNT` feed with no folders chosen is `FOLDERS(∅)`. With it the provider default is not a special case: OneDrive
"default → Projects" is a narrowing, Google "default → Projects" is a widening (case 5c). A scope **covers everything**
when it is `WHOLE` or the `AREA` default (today's `covers_everything`): such a scope is built by enumeration and its
feed items need no verdict. Every other scope is a **folder scope**.

**Coverage.** A stored record is *covered* by a set of roots when its stored parent chain reaches one of them. Coverage
is computed over stored records (no provider call), never by comparing ids.

### 5.2 Stored state

On `DriveSyncState` (no new collection):

| Field | Meaning |
|---|---|
| `applied_scope: str` | kept: the `scope_key` of the applied scope; the cheap change detector and the D5 upgrade check |
| `applied_whole: bool`, `applied_roots: tuple[str]` | the applied scope itself (W1); small: what the Cabinet picker saved |
| `applied_shape: bool` | the two fields above are written (an old state has neither; D5) |
| `removing_roots: tuple[str]` | removed roots whose guarded removal (§5.6) is still running; read by the Cabinet line |
| `ancestry_dirty: bool` | the skeleton paths must be re-derived (§5.9); durable so a cut tick continues |
| walk counters | folders listed since the current walk began, folders the walk could not list (§5.5); for the Cabinet line only, never in `state.counters` |

The walk cursor stays on each folder record (`reobserve_cursor`), as today. **The in-scope folder set is the index
itself**: every stored folder record that is not a `SCOPE_ANCESTOR` skeleton and not `DELETING`. "A walk is running"
means: some folder of the connection has a listing pending. The build chooses how the tick learns that (a maintained
counter, or a query); a query on a field that has no composite index needs the index `READY` before the code deploys
(repo trap).

### 5.3 The tick

1. Read the connection (as today). If `applied_scope != connection.scope_key`, compute and apply the diff (§5.4): it
   only writes marks (walk starts, removal starts, `removing_roots`), never removes records inline.
2. Read the feed (time-bounded pages, as today) and judge each item (step 1: the existing chain verdict; step 2: §5.7).
3. Work off: removals (§5.6, cascade budget), walk pages (§5.5, walk time share), skeleton re-derivation (§5.9),
   content (class order as today). Before each paid batch, re-read the scope (§5.10).

### 5.4 The diff (step 1)

Old applied scope → new connection scope, both in normal form:

| Transition | What the tick does |
|---|---|
| `FOLDERS(A)` → `FOLDERS(B)` | **Added** = roots of B not covered by A's stored records: each is walked (§5.5). A root of B that A covers is already stored: nothing to read (case 3b). **Removed** = roots of A not covered by B: each gets a guarded removal (§5.6) whose keep set holds every root of B inside it and the path to it (case 3a). |
| `WHOLE` → `FOLDERS(B)` | A narrowing: a guarded removal over every stored top-level record (children of the drive root and parentless items) with the keep set B; zero provider calls except the skeleton paths of B (§5.9). Parentless shared items leave: they are in scope only under the whole drive (§11.3). Cabinet warning first (D4, §8). |
| `FOLDERS(A)` → `WHOLE` | A widening to "the rest of the account": that **is** a full enumeration. Keep `scope_restart` for this one transition (D1). Unchanged records keep their content. |
| any change while the **initial enumeration** of a scope that covers everything has not finished | `scope_restart`, as today: the enumeration's partial pass is all that is lost (W13). |

The diff is applied in the tick, never in the Cabinet request (the answer to E71's rejected "per-folder cascade from the
Cabinet": the Cabinet only saves the set). Then `applied_*` are stamped with the new scope.

### 5.5 The walk

- **What it is.** A root to walk gets a record (one `get_change` for a root the index does not hold, from Δ9's budget)
  with `reobserve_pending`; the existing re-listing recursion does the rest. A skeleton record that becomes in scope
  (a root added above it, or moved into scope) is re-classified as a normal folder and gets `reobserve_pending`: its
  children beyond the path were never stored (W4).
- **Descend only into what is new.** A diff walk descends only into child folders new to the index (`_adjusted`'s
  existing rule), so a walk of "Projects" skips the already-stored "Projects/2026" subtree (case 3a) and a root
  re-added mid-removal salvages what is not yet removed (case 8c). A **resync walk** (§5.8) descends into every folder,
  stored ones included, because it must re-stamp what it sees for the sweep (W4).
- **Budget.** Re-listing pages leave `ITEM_CALLS_PER_TICK` (Δ9). They spend a time share of the tick:
  `WALK_SHARE` (initial 0.5 of `TICK_START_BUDGET`), measured like feed pages. Once spent, re-listing steps wait for the
  next tick and count as waits of E87 (a)'s kind, so the due loop goes on with content; a large walk therefore cards
  what it has listed while it continues (the GE-36 lesson: a build that indexes nothing until it ends) (W3). Page size
  `WALK_PAGE` (initial 1000 on Google, its documented maximum, P2; OneDrive keeps its own page size if lower).
- **One folder per call.** On Google the walk lists one folder per `files.list` call with today's per-folder cursor.
  Listing several folders per call (`'a' in parents or 'b' in parents …`) is not documented and has no documented limit
  (P2, P3): it is an optional later slice behind a read-only probe (§9 S0, S3) (W15).
- **A folder that cannot be listed.** A not-found answer marks it inaccessible as today (`marked_inaccessible`, the
  `_recheck_step` re-read). A transient failure retries with back-off; after `MAX_ATTEMPTS` the folder is **not**
  marked done (today `_reobserve_step` commits `_relisted(folder, None)`, `:2820-2822`, which silently ends a walk with
  a hole): it stays pending at the `RETRY_CAP` back-off and is counted as "could not be read". The walk is *settled*
  without it, so its unlisted subtree is out until a retry succeeds and lists it (W5).
- **The feed covers the walk.** The feed cursor always predates the walk (a diff walk runs under a running cursor;
  an initial walk starts the cursor first, §5.8). Any write during the walk therefore reaches the index later through
  the feed: an item moved out of a listed folder arrives with an out-of-scope parent, an item moved into a folder
  after its listing arrives with an in-scope parent. So Microsoft's "children paging is incomplete under writes" (P6)
  does not bite: the walk is a snapshot, the feed is the correction [INF; pinned by a fake-provider test, §10].
- **Opaque subtrees.** As in today's re-listing, the walk does not list an opaque folder. A marker child (`.git`, …)
  is stored when its parent is listed, so a folder that closes on it still re-opens when the marker is deleted. The
  sub-folders deeper inside an opaque subtree, which a full enumeration stores as `inside_opaque` metadata records, are
  not stored by a walk. Their readers tolerate absence: search hides them (`src/domain/drive_search.py:350`,
  `drive_search_service.py:2119`) and the mirror's inside-check treats a missing record as inside
  (`src/services/drive_transcript_mirror.py:1200-1202`). The build confirms the reader map before step 2 (W18).

### 5.6 The guarded removal

- A removed root's stored subtree is removed with `unstored` (the file stays on the drive), post-order, on the cascade
  budget (`CASCADE_OPS_PER_TICK`), never in the Cabinet request.
- **Keep set**: every root of the new scope, the folders on the path from a removed root down to such a root (they turn
  into `SCOPE_ANCESTOR` skeletons: card, chunks and hints dropped, the record kept), mirror roots, and skeleton records
  of other roots. A kept root stops the descent: its subtree is untouched. This is the E87 keep set applied to the
  cascade.
- **Guard**: a removal step runs only once the tick's skeleton paths are loaded, and not in a tick whose feed applied a
  folder change (E87's rule: the chains are read once per tick, and a folder moved in the same tick makes them stale);
  it then waits for the next tick.
- Data on removal (D2, D3, unchanged mechanisms):

| Kind | Fate |
|---|---|
| record, card | deleted with the record; adding the folder back re-reads the content (model money again: the Cabinet says so, D4) |
| chunks (passages), hints | collected with the record (`_ever_read`, `drive_indexing_service.py:2582`); the sweep's chunk and hint backstops stay |
| owner notes | never archived on `unstored` (DRIVE_FILE_NOTES_RFC C1); the note stays bound to the item and merges back if the file returns [INF] |
| stored transcripts | user-scoped store; `unstored` detaches the mirror file (USER_DRIVE_INDEXING_RFC §27.9), whose ledger entry pins the transcript; an unmirrored one ages out under `TRANSCRIPT_ORPHAN_TTL` (D3) |
| mirror `.md` on the drive | detached; stays where it is |

### 5.7 The feed-item verdict (step 2, folder scopes)

For an item that is not the drive root, a mirror root or inside an opaque subtree (today's `_scope_applies`):

| Item | Verdict |
|---|---|
| a root of the applied scope | in |
| its parent is a root, or a stored in-scope folder (not skeleton, not `DELETING`), or an in-scope folder earlier in the same page | in |
| a folder on a root's path (a stored skeleton, or its parent is one and it is itself on a chain) | skeleton (`ANCESTOR`) |
| parent `""` (no reachable parent) | out (in only under the whole drive, which needs no verdict) |
| parent unknown, **no walk running** | out — nothing is stored for it, no provider call |
| parent unknown, a walk running, the item has **no** record | out — if it is in scope, the running walk lists it, because its parent is not stored yet |
| parent unknown, a walk running, the item **has** an in-scope record | held: `UNRESOLVED` with today's scope hold (content fields kept), no provider call; judged locally once the walk settles |
| parent is `DELETING` | out |

This rests on one invariant: **when no walk is running, every in-scope folder the index stores folders for is
stored** (a walk-blocked folder's subtree excepted, §5.5). No record is ever stored for an out-of-scope item, so the
`unresolved` backlog of E87 cannot form; holds exist only for records that were in scope and moved during a walk, and
they are resolved by a local test when it settles (W7).

### 5.8 The initial build and a lost cursor (step 2)

- **Folder scope, first build.** Take the feed's cursor at "now" first, then walk the roots. Port: one new operation
  that returns a cursor at the current position without enumerating — Google `changes.getStartPageToken` in the
  cursor's feed phase; OneDrive `delta?token=latest` on the App Folder (P5), falling back to paging the area's delta to
  its `deltaLink` without applying it if the App Folder path refuses `token=latest` (W8, W16). The walk-built index
  sets the same "initial build done" fields the enumeration sets when it ends (`initial_enumeration_done`, phase
  `IDLE`); every reader of them is mapped before the change (W8).
- **A scope that covers everything** keeps today's enumeration (GE-14 for Google, the App Folder delta for OneDrive).
- **Lost cursor** (`DriveResyncRequired`) under a folder scope: epoch + 1, a cursor at "now", a **resync walk** of every
  root (descending into stored folders too, re-stamping the epoch), then today's sweep removes what the walks did not
  see — E71's "the sweep removes what a restart does not see", scoped to the roots (W19).
- **Step-2 deploy with an enumeration in progress** under a folder scope: the tick takes the lost-cursor path instead
  of finishing the enumeration (W17). An index whose enumeration had finished already satisfies the invariant of §5.7.

### 5.9 Scope folders moved, renamed or deleted on the drive

**Rule.** The skeleton set is derived state: the union of the paths from the drive root to each current root. It is
re-derived (`ancestry_dirty`) whenever a root or a stored skeleton changes its parent or is deleted, and after every
diff. Re-derivation loads the paths with today's loader (stored records first, `get_change` for a missing link from
Δ9's budget); only after a **complete** load are skeleton records that lie on no current path removed (metadata only,
no content). The stored root set itself never changes on a provider event: it is the owner's setting, and the indexer
never writes the connection (W9, W10).

| Event | Outcome |
|---|---|
| a root renamed | nothing: ids, not names. Its record takes the new name; printed paths and the Cabinet (which reads the path live) show it. |
| a root moved elsewhere, outside every other root | its subtree stays (id-based parent links). Re-derivation stores the new path's skeletons and removes the old path's. |
| a root moved under another root | nesting after the fact: harmless (one record per item id). Old path skeletons removed. The connection keeps both ids; the next Cabinet save normalises them (`_outermost`). A later removal of the outer root keeps the inner one (coverage diff). |
| a skeleton ancestor renamed | its record takes the new name (today's `ANCESTOR` verdict). |
| a skeleton ancestor moved | the paths of the roots under it change: re-derivation as above. |
| a skeleton ancestor moved **into** another root's subtree | it is now in scope: re-classified as a normal folder and walked (its other children were never stored); the root under it is covered and skipped by the walk. |
| a root trashed or deleted, or access lost | tombstones cascade its subtree (`gone`; Google emits a change per descendant, P7). The id stays in the connection and in `applied_roots`; the Cabinet shows "folder missing" (D4, §8). Its path skeletons are removed. |
| a missing root restored (untrash) | its change arrives with an in-scope verdict (it is a root): new to the index, so it is walked; "folder missing" clears by itself. |
| OneDrive: a root moved out of the App Folder | the App Folder feed no longer sees it [INF: reported as deleted]; it is missing as above. |

### 5.10 The scope re-read before paid work; a save that lands mid-tick

The tick still reads the connection once, so a save after that point is applied by the next tick (the Cabinet's nudge
makes it immediate). Before each paid batch (content runs, transcription submits, embeddings) the tick re-reads the
connection's `scope_key` (one document read). If it differs from the tick's, the tick starts no more paid work, lets
runs already started finish, and continues at once; the next tick applies the diff first, and anything the finishing
runs stored for a removed folder goes with its removal. The access policy and search already refuse or hide such a
file meanwhile (E70) (W11).

### 5.11 Upgrade (D5)

On the first tick under the step-1 code, a state with `applied_shape` False: if `applied_scope == connection.scope_key`,
the connection's set **is** the applied set — write it and go on. Otherwise a change is pending that the old code never
applied: run one last `scope_restart`, stamping the new set when the restart starts (as `scope_restart` stamps the key
today). No rebuild, so no paid content is lost (W12). The step-2 deploy needs no upgrade beyond §5.8's last bullet.

## 6. What the superseded decisions become

| Decision | Becomes |
|---|---|
| MULTI_DRIVE E71 (a scope change restarts the feed) | Kept only for folders → whole drive and for a change during an unfinished enumeration of a scope that covers everything (§5.4). Every other change is a diff (step 1). The sweep stays the backstop for a lost cursor, scoped to the roots (§5.8). |
| MULTI_DRIVE §11.3, parent-chain paragraph | Step 1: unchanged. Step 2: a folder scope judges feed items locally (§5.7); `get_change` is used only for a root's own record and the skeleton paths (§5.5, §5.9). |
| MULTI_DRIVE E87 | Step 1: unchanged. Step 2: retired for folder scopes, its only users (no `unresolved` backlog forms, §5.7); its keep set and its "no removal in a tick that applied a folder change" rule live on in the guarded removal (§5.6). Its drain code is deleted with step 2, with its tests. |
| GOOGLE_DRIVE GE-14 (enumeration) | Kept for scopes that cover everything (the whole drive). A folder scope is built by walking (step 2). |
| USER_DRIVE_INDEXING Δ9 (`ITEM_CALLS_PER_TICK` for probes, parent lookups, re-listing pages) | Step 1: re-listing pages leave it for the walk time share (§5.5). Step 2: feed parent lookups end. It keeps probes, a root's own read, skeleton path reads and access rechecks. |

## 7. Cases

| # | Case | Outcome |
|---|---|---|
| 1 | Add "Projects" | Walk "Projects" only. Other records untouched; the feed keeps its cursor. |
| 2 | Remove "Projects" | Guarded removal of its stored subtree (`unstored`), at once (D2). Nothing else is read. |
| 3a | "Projects/2026" in scope, then "Projects" added | `_outermost` saves `{Projects}`. "Projects/2026" is covered → not removed; the walk of "Projects" skips it. No double walk; one record per item id. |
| 3b | "Projects" in scope, then "Projects" replaced by "Projects/2026" | "Projects/2026" is covered by stored records → no walk. Removal of "Projects" keeps "Projects/2026" and its subtree; "Projects" becomes a skeleton. |
| 4a | A folder moved **into** a scope folder on the drive | Feed: parent in scope → in → new to the index → walked (exists today). |
| 4b | A folder moved **out** | Feed: parent not in scope (step 2: unknown, no walk running) → out → `DELETING` cascade (`unstored`). |
| 4c | A folder renamed | Parent unchanged → verdict unchanged; the record is updated. A renamed root: §5.9. |
| 4d | A folder deleted | Tombstone → cascade (`gone`). A deleted root: §5.9, "folder missing". |
| 4e | A new sub-folder inside a scope folder | Parent stored in scope → in; stored; its later children are judged against it. |
| 4f | A root moved elsewhere / under another root | §5.9. |
| 5a | Whole drive → "Projects" | Guarded removal of everything outside "Projects" and its path; parentless shared items go too; zero provider calls beyond the path. Cabinet warning first. |
| 5b | "Projects" → whole drive | Full enumeration (`scope_restart`); no model money for unchanged records. |
| 5c | Provider default | OneDrive default = `FOLDERS({App Folder})`: "→ Projects" is case 5a inside the area. Google default = `FOLDERS(∅)`: "→ Projects" is case 1, and with step 2 it is the initial build. |
| 6 | "Why poll an out-of-scope folder?" | Nothing polls folders: one feed per connection; step 2 judges each item against the stored set with no call. |
| 7 | A single file in the scope | Not now (D1). If ever: "also these files" next to folders, each pinned file a root whose walk is one read and whose verdict is the same local id test. |
| 8a | Scope change while a walk runs | Added roots start their own walks; removed roots' removal also removes their half-walked folders (`reobserve_pending` dies with the record). No restart. |
| 8b | A save lands mid-tick | §5.10: no more paid work this tick; the next tick applies the diff. |
| 8c | A folder removed, then added back before its removal ends | `removing_roots` drops it; folders still `DELETING` revive and are re-listed (today's revive rule, `drive_index_planner.py:606-608`); files not yet removed keep their content; removed ones are read again. |
| 8d | A walk folder cannot be listed | §5.5: retried at back-off, counted "could not be read" in the Cabinet line; its subtree is out until it is listed. |

## 8. Cabinet

- **One progress line per drive** (D4), in place of today's `unresolved` line ("Checking which files are in your
  folders", which step 2 makes obsolete for folder scopes). At most one state shows, in this order: removing ("Removing
  N folder(s) from the index", from `removing_roots`); walking ("Reading the chosen folders: N folders read", from the
  walk counters, plus "M folders could not be read yet" when non-zero); building the whole drive (today's `building`).
  No line when idle. The row's `indexing` JSON gains the fields; no per-folder state is shown.
- **Whole drive → folders warning** (D4), in the folder-picker dialog before Save, only when the saved scope is the
  whole drive and the new one is folders: what leaves the index (everything outside the chosen folders, including
  items shared with the account that sit in no folder) and that adding it back later means reading it again. No item
  count: it would need an index scan inside the request (W20).
  The warning also covers a OneDrive connection leaving its default (the App Folder) for chosen folders: the default
  folder's files leave the index (owner decision 2026-11-06; `drive.scope_narrow_area_warning`, a `confirm()` on Save).
- **Folder missing** (D4). Today `_item_path` (`src/web/user_cabinet_app.py:1610-1618`) turns any read failure into an
  empty path. A not-found or trashed answer becomes `missing: true` on that folder, shown as "Folder missing on the
  drive" with a Remove action; a transient failure keeps today's empty path. A save that keeps a missing folder is
  accepted: `set_scope` admits a selected folder whose read answers not-found without its ancestry (it cannot nest
  and cannot contain the write folder), so the owner is never forced to remove it to change something else, and a
  restored folder comes back on its own (W21).

## 9. Delivery slices

Vertical; step 1 ships alone and ends the re-read on every change.

| Slice | Content | Done when |
|---|---|---|
| **S0** probe (optional, gates S3 only) | A read-only script against a Google connection: `files.list` with `'a' in parents or 'b' in parents …` for growing batch sizes; records which batch sizes answer, whether paging works across them, and the refusal. **Needs the owner's OK to run against his connection.** Counts and statuses only go to the ops plan. | A documented safe batch size, or "do not batch". |
| **S1** step 1 — stored set and diff | §5.2 fields; the D5 upgrade; the diff (§5.4); walks of added roots on the walk time share with the blocked-folder rule (§5.5); guarded removal (§5.6); skeleton re-derivation and the moved/renamed/deleted root rules (§5.9); the scope re-read before paid work (§5.10); Cabinet line, warning and "folder missing" (§8). Δ9 loses re-listing pages. MULTI_DRIVE_RFC E71 and the owner's map updated. | Adding or removing a folder on the live connection lists only that subtree (tick summary: listing calls of the walk, no enumeration pages); the Cabinet shows the line and the warning. |
| **S2** step 2 — walk-built index, local verdicts | The "cursor at now" port operation (both adapters); folder scopes built by walking (§5.8); local verdicts (§5.7) with holds; resync walk; step-2 deploy rule; the chain verdict, `_resolve_step`'s chain walk and the E87 drain deleted with their tests; GE-14, §11.3, E87 updated. | A folder scope on a fresh connection builds with no enumeration pages and no feed parent lookups; feed items outside the scope make no provider call. |
| **S3** multi-folder listing (optional) | After S0, only if walk duration is a measured problem: batch several folders per `files.list` call; a batch cursor in place of per-folder cursors. | Measured fewer calls per walk with the same stored result. |

## 10. Test strategy (behaviour to pin)

All against the fake providers (one `AREA`, one `ACCOUNT`), counting provider calls by kind:

- **Diff cost.** Adding a folder lists only its subtree (listing calls = its folders' pages; no enumeration page, no
  restart); removing one makes no provider call; untouched records keep `seen_epoch`, content and cards.
- **Coverage.** Cases 3a and 3b: no walk of a covered folder, no removal of a covered folder; one record per item id.
- **Guarded removal.** Kept roots and their subtrees survive; path folders become skeletons without card or chunks;
  mirror roots survive; nothing is removed in a tick whose feed applied a folder change or before the paths are loaded.
- **Whole → folders / folders → whole.** 5a removes everything outside, parentless items included, with zero listing
  calls; 5b restarts and keeps content.
- **Walk correctness under writes.** Writes during a walk (move in, move out, create, delete, rename) end, after the
  feed catches up, in exactly the index a fresh walk would build.
- **Blocked folder.** A listing that fails `MAX_ATTEMPTS` times is never marked done; it retries; the counter shows it.
- **Local verdicts (S2).** An out-of-scope feed item makes no provider call and stores nothing; an unknown-parent item
  with no record is out during a walk and is stored by the walk if in scope; a stored record moved under a not-yet-walked
  folder keeps its content and is in after the walk settles.
- **Moved roots.** Rename, move, move under another root, delete, restore: §5.9's outcomes, and no stale skeletons
  after a complete re-derivation.
- **Resync walk.** A lost cursor under a folder scope re-walks stored folders too, and the sweep removes exactly what
  the drive no longer has.
- **Upgrade.** Matching hash → adopt with no restart; differing hash → one restart; a step-2 deploy mid-enumeration →
  resync walk.
- **Mid-tick save.** A scope change seen at the re-read starts no further paid work in that tick.
- **Opaque parity.** A folder reached by a walk closes on its marker and re-opens when the marker is deleted.
- **Transcripts.** A removed folder's mirrored transcript stays pinned; an unmirrored one is swept only after the TTL.

## 11. Risks

| Risk | Mitigation |
|---|---|
| **Missed files**: a folder not stored after a settled walk hides its subtree from step 2's verdict | the blocked-folder rule (§5.5) instead of a silent "done"; the walk-under-writes test; the resync walk on a lost cursor |
| **Stale-ancestor removal** (E87's lesson): a removal on chains a same-tick move made stale | the guard of §5.6; skeleton removal only after a complete re-derivation |
| A folder scope covering most of a many-folder account walks with more calls than a 1000-per-page enumeration | expected and bounded by the time share; the owner can pick the whole drive; S3 if measured |
| Microsoft children paging under writes (P6) | the feed predates every walk and corrects it (§5.5) |
| Deleting the chain machinery (S2) breaks a path that still uses it | no remaining user after S2 (covers-everything scopes need no verdict); the build maps callers before deleting |
| A new "walk running" query without its composite index | index deployed and `READY` before the code (repo trap) |
| `token=latest` refused on the App Folder path (P5 is [INF] there) | the drain-to-`deltaLink` fallback (§5.8) |

## 12. Triggers to revisit

- A user asks for single files in the scope → "also these files" (D1, case 7).
- Walks of a large scope measured as too slow → S0 then S3.
- A provider adds a folder-scoped change feed (Google `changes.list` with a folder filter; Graph `delta` on a folder
  confirmed for every account type) → per-root feeds could replace local judging.
- The owner asks for an undo window on removal → a soft-hide period (D2 rejected it for now).

## 13. Engineering decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| W1 | The applied scope is stored as a structure (`applied_whole`, `applied_roots`) beside the kept `applied_scope` key. | A diff needs the set; the key stays the cheap detector and the D5 check. | Replacing the key (the upgrade loses its test); a new collection (the roots are few). |
| W2 | The diff works on coverage over stored records, not on id sets. | Nesting (cases 3a, 3b) makes id diffs wrong: a removed id may still be covered. | Id diff (re-walks or removes covered subtrees). |
| W3 | Re-listing pages spend a time share `WALK_SHARE` (initial 0.5 of the start budget), not `ITEM_CALLS_PER_TICK`; out of share is an E87 (a) wait. | At 20 calls a tick a large scope takes very many ticks; an unbounded walk would card nothing until it ends. | Δ9's count; a separate call count (a slow provider would still overrun). |
| W4 | Diff walks descend only into folders new to the index; a resync walk descends into all; a skeleton that becomes in scope is walked. | Reuses the existing relisting rule and makes covered subtrees free; a resync must re-stamp everything for the sweep. | Always descend (re-lists covered subtrees); never descend into stored folders (a resync would sweep them). |
| W5 | A walk listing that fails `MAX_ATTEMPTS` times stays pending at `RETRY_CAP` and is counted, instead of being marked done. | Step 2's verdict trusts a settled walk; a silent hole would hide a subtree for good. | Today's `_relisted(folder, None)`; holding every unknown-parent item until the folder lists (an unbounded backlog). |
| W6 | Guarded removal: E87's keep set and same-tick folder-change guard, on the cascade budget, in the tick. | Proven guards; never inside the Cabinet request. | Removal from the Cabinet (E71's rejected path); removal by epoch sweep (needs a restart). |
| W7 | Step 2 holds only records that were in scope; an unknown-parent item with no record is out even during a walk. | The walk lists any in-scope item whose parent it has not stored yet, so storing out-of-scope items is never needed; the E87 backlog cannot form. | The concept's "hold every unknown-parent item during a walk" (stores out-of-scope records again). |
| W8 | A folder scope starts its feed at "now" (new port operation), then walks; the walk-built index sets the enumeration's "done" fields. | The feed must predate the walk; readers of "initial build done" keep working. | Enumerate and judge (folders do not arrive in tree order, so a local verdict would misjudge). |
| W9 | The skeleton set is derived from the current roots' paths; stale skeletons go after a complete re-load. | Today skeletons are only added, so a moved root leaves a stale path. | Removing a skeleton when its child moves (it may be on another root's path). |
| W10 | Provider events never change the stored root set; a missing root stays until the owner removes it. | The set is the owner's setting (D4); a restored folder comes back by itself. | Dropping a missing root (D4 rejected it). |
| W11 | Re-read `scope_key` before each paid batch; on a change, no more paid work this tick. | One document read bounds paying for a removed folder to runs already started. | Skipping per record (a chain test in the hot path); no re-read (a whole tick of paid work on a removed folder). |
| W12 | The D5 restart stamps the new set when it starts. | Records enumerated from then on are under it, as `scope_restart` stamps the key today. | Stamping at its end (a second change during it would be diffed against the wrong set). |
| W13 | A change during an unfinished enumeration of a scope that covers everything keeps `scope_restart`. | The partial pass is all that is lost; a diff against a half-built index is undefined. | Diffing it. |
| W14 | S2 deletes the feed parent-chain verdict and the E87 drain; `_load_scope_ancestors` stays. | No user remains; dead paths cost tests and reading. | Keeping them as a fallback (untested code that never runs). |
| W15 | Google walks list one folder per call; multi-folder `or` queries only after a probe (S0, S3). | P2: no documented example or limit; P3 is anecdotal. | Batching blind. |
| W16 | The change feeds stay as they are (Google account-wide, OneDrive the App Folder's delta); no `delta` on a scope folder. | P4 is unresolved for Business, and local judging makes per-folder feeds unnecessary. | Per-root delta feeds. |
| W17 | At the step-2 deploy an unfinished enumeration under a folder scope takes the lost-cursor path. | The invariant of §5.7 holds only for a finished build; a resync walk restores it at the cost of the scope only. | Keeping the old enumeration path alive until it finishes. |
| W18 | A walk does not store sub-folders deep inside opaque subtrees. | Today's re-listing already behaves so; their readers tolerate absence; storing them means walking `node_modules`-sized trees. | Walking opaque subtrees for parity with the enumeration. |
| W19 | A lost cursor under a folder scope: epoch + 1, cursor at now, resync walk of the roots, sweep. | E71's backstop, scoped to the roots. | A full account enumeration. |
| W20 | The whole → folders warning names what leaves, without a count. | A count needs an index scan in the request. | A counted warning. |
| W21 | `set_scope` keeps a missing selected folder without its ancestry; the Cabinet shows it as missing. | D4: missing until the owner removes it, and a save of another change must not be refused for it. | Refusing any save that still holds a missing folder. |

---

## Built (step 1)

Step 1 (§5.2-§5.6, §5.9-§5.11, §8, the walk share of §5.5) is built as written, except for the following; where this
list and the text above differ, this list wins. Code: `src/domain/drive_scope_diff.py` (the pure diff),
`src/services/drive_indexing_service.py` (`_apply_scope_change`, `_removal_open`, `_cascade_step`, `_trim_step`,
`_scope_step`, `_reobserve_step`, `_settle_walk`), `src/web/user_cabinet_app.py` (`scope_progress`, `missing`).

1. **No `applied_shape` field (§5.2).** The stored set (`applied_whole`, `applied_roots`) is valid when
   `scope_key(applied_roots, applied_whole) == applied_scope` (`shape_valid`); the key stays the detector.
2. **Class (§5.5, §6).** Folders waiting to be listed are class 1 with the other structural work, not class 2.
3. **Unfinished enumeration (§5.4, W13).** In step 1 a change during any unfinished enumeration restarts, folder scopes
   included (W13 named only a scope that covers everything).
4. **W5 applies to every re-listing**, not only a scope walk, and supersedes Δ18's "dropped after `MAX_ATTEMPTS`": the
   folder stays pending at `RETRY_CAP` and is counted (`walk_blocked`).
5. **The cascade of a folder that left the scope notices `UNSTORED` (§5.6).** The old cascade noticed `GONE`; a folder
   deleted on the drive still does (`cascade_unstored` tells them apart).
6. **W11 rides the existing `_recheck_gate`**, forced for a removal's guard, not a new read.
7. **Walk progress is state fields** (`walking`, `walk_listed`, `walk_blocked`), settled once per tick while a walk runs.
8. **The Cabinet "checking" (`unresolved`) line stays in step 1**, ranked last after the scope line (§8).
9. **Skeleton re-derivation stores the path folders it reads** (§5.9), and marks stale ones for the cascade.
10. **OneDrive walks keep 200 per page** (`_CHILDREN_TOP`); Google lists `WALK_PAGE` = 1 000.

Review-driven rules: a switch to whole drive while a removal runs restarts at once (no parent reads first), and the
cascade under whole drive waits until the restart has run; a cascade left by the code before step 1
(`cascade_unstored` false) takes the guarded path after one provider read when the verdict or a kept child calls for it.
