# Drive file notes — plan part B: S2, the merge of notes into cards and search

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. This part is one ordered group of tasks on the
> feature branch `feat/drive-notes`, after part A's tasks (it reads A's composition, §2) and before part C's (C builds on
> B's record fields, entry (a) and the tick accumulator, §2).

**Status: PLAN ONLY, verified.** Every code block below is the diff of commits on `plan/drive-notes-b` (worktree
`alekbot-core-npb`, base `cef8f147`), where it compiles, passes its new tests and `make check` (§7). Executing a task
means applying its hunks (or cherry-picking its commit) and running its done-criteria; nothing is designed anew.
- Agents write code and tests only: no cloud, no gcloud, no Firestore write, no paid model call, no deploy. Owner steps
  are **[OWNER]**.
- **Reviewer rule** (CLAUDE.md, Tests): the implementer never edits an existing test. Each task lists its
  expected-risk tests (they failed in the worktree exactly as listed, for the reason given); the task reviewer decides
  each one. Anything failing outside that list is a code bug.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (Sonnet implementers: their own line).

**Spec:** `docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md` rev 3.2, §3.3, §3.4, §5.1-§5.6, §6.1-§6.3, §11 "S2"; the frozen core
`.superpowers/sdd/drive-notes/core-contract.md`, its §5 "Requirements the review sets for S2" and §6 "S2" (all met,
§1.1). Base code: the T2 mirror tick (`_work_off`, `_mirror_ready`, `_Budget.mirror_stop`).

---

## 1. Scope

RFC §11 S2:
- `DriveIndexRecord` owner fields and `note_lookup_due` (§3.3); `DriveSyncState` notes-step fields (§3.4, with the pair
  cursor the core review requires);
- `src/domain/drive_note_merge.py`: `NoteView`, `note_view`, `with_owner_note`, `effective_doc_type` /
  `effective_life_areas`, `NOTE_MERGE_VERSION`, the tick constants (§5.1, §5.5, §15);
- the card segment `drive_card.owner_segment`, `card_text(owner=)`, and the rendered line `owner_note_line` /
  `note_line` (§5.3, §6.2);
- keys: `drive_keys.note_keys`, `AboutField.OWNER_NOTE`, `note_keys_digest`, the `l:type` / `l:domain` overrides and the
  D31 drop (§5.4); the note store's `keys` filled (`note_store_keys`, the core's S1 left them `[]`);
- the tick: entry (a) `_lookup_notes` in `_commit` (with re-activation), entry (b) `_notes_step` (merge, clear, erase
  stubs, watermark pair, paging, merge-version reset), the count gate, the vector repair and the space pass, the notes
  step behind the closed access gate (`_notes_gated`), the sync-state fold (`_TickNotes.onto`);
- search: `drive_tier_explain` (`your note …`), `DriveSearchCard.owner_note`, `make_card`, `card_body`, the listing's
  `_card_body`, `_related_body`, all four `make_card` call sites, `_collapse` on the lean `owner_note_id`, the
  FileManagement open header, the `drive-search` skill text (§6.1-§6.3);
- composition: the indexer (`DRIVE_NOTES_ENABLED`, the store) and the main service (the open header's readers).

Not in B: the tool, skill `drive-file-notes`, receipt, Smart wiring and the DriveNoteService instance in the container
(A); `note_checks`, round-end archiving, the audit, `search_deleted`'s notes source, erase commands (C).

### 1.1 The binding obligations, where each is met

| Obligation (core contract §5/§6) | Met by | Proof |
|---|---|---|
| `ONEDRIVE_PROVIDER_KEY` for every `i:` key, never a literal | `drive_note_merge.record_binding_key` (the only `i:` builder in the indexer); `note_view` and `_merge_notes` compare `binding_parts_of(note).provider_key` with it | `test_drive_note_merge.py::test_the_record_binding_key_uses_the_one_provider_key`, `::test_only_an_active_note_on_the_current_drive_gives_a_view`; `grep -n '"microsoft_onedrive"' src/services src/domain/drive_note_merge.py` is empty |
| Stub bindings via `binding_parts_of` | `_lookup_notes`, `_merge_notes`, `note_view` | `test_drive_indexing_notes.py::test_an_erase_stub_clears_the_copy_then_goes` |
| Entry (a) re-activates an archived note, and a cleared note with `archived_at` | `_reactivated` | `::test_a_restore_reactivates_the_archived_note_and_merges_it`, `::test_a_cleared_archived_note_loses_its_stamp_and_stays_cleared` |
| Persist the `changed_since` pair cursor | `notes_applied_at` + `notes_applied_id` + `notes_paging` (+ `notes_scanned_at`), `notes_query_start` | `::test_a_full_page_continues_on_the_pair_cursor`, `test_drive_note_merge.py::test_notes_query_start`, the codec test |
| Notes step while the access gate is closed (access-loss grace, D30) | `tick()` calls `_notes_gated` in both branches; cards re-embedded there, dropped on an embedding failure | `::test_a_cleared_note_leaves_the_record_and_card_behind_a_closed_gate[access_lost/awaiting_migration]`, `::test_behind_a_closed_gate_a_card_the_embedder_refuses_is_dropped` |
| `set_vector` with the embedded version | `_embed_notes` passes `note.current_version` of the note it embedded | `::test_a_newer_version_keeps_its_own_vector` |
| Invariant pins unchanged (`index_tables_digest`, `golden_digest`, capture, scene) | no key family added; note constants outside the digests; `RecordKeyInputs` note fields default empty | `tests/unit/domain/test_drive_keys_tables.py` passes unchanged |

### 1.2 Where the notes step sits, and the deadline arithmetic

`_run_spending`: gate → lease → marks/mirror roots → nudge → **observe → notes step** → work-off (`_work_off`: the
mirror reserve decision via `_mirror_ready`, sweep, transcript cheap steps, due loop with enrichment, extraction,
upgrade pass, cap releases, **mirror step last**) → `_has_more` → save. The notes step:
- starts only while `budget.time_left()` (elapsed < `TICK_START_BUDGET` = 480 s);
- starts no Firestore or embedding call later than `NOTE_STEP_S` = 60 s after it began; one call in flight is covered by
  `FILE_START_MARGIN_S` = 60 s. Its overrun past the start budget is therefore at most 60 + 60 = **120 s**;
- runs before the work-off, so after an overrun nothing in the due loop starts (`time_left()` is false) and enrichment
  cannot start either (`enrichment_allowed` needs `time_left()`): the two overruns never add up, the larger counts.

The §7.1 sum asserted by `test_drive_indexing_mirror_tick.py::test_deadline_sum_includes_the_reserve` (unchanged):

```
TOTAL 480 + max(notes 60+60, enrichment ENRICH_FILE_BUDGET_S 300 + 60) + TRANSCRIBE_SHARE 0.25 × 480
     + extract_worst (TRANSCRIBE_EXTRACT_WALL_S 240 + TRANSCRIBE_UPLOAD_S 60 + FILE_START_MARGIN_S 60)
     + MIRROR_RESERVE_S 45 + FILE_START_MARGIN_S 60
= 480 + 360 + 120 + 360 + 45 + 60 = 1425 s < TICK_DEADLINE_S 1800 s
```

The extraction step's and the mirror's windows are capped against the deadline itself (`extraction_stop`'s `last_start`,
`mirror_stop`'s `TICK_DEADLINE_S − FILE_START_MARGIN_S`), so even a later start keeps them inside it. The new test
`test_drive_indexing_notes.py::test_the_notes_step_keeps_the_deadline_sum` pins `notes ≤ enrichment overrun` and the
sum; `::test_the_notes_step_does_not_start_past_the_start_budget` proves the start gate. Behind a closed gate the step is
the tick's only work (its own budget), so no sum applies.

---

## 2. Interfaces

### 2.1 B provides (to A and C)

**Domain** (`src/domain/drive_index.py`):
```python
# DriveIndexRecord (after the T2 mirror fields)
owner_note_id: str = ""; owner_note_version: int = 0; owner_note_text: str = ""; owner_doc_type: str = ""
owner_life_areas: Tuple[str, ...] = (); owner_note_merge_version: int = 0; note_lookup_due: bool = False
# DriveSyncState (after purge_keep_transcripts)
notes_applied_at: Optional[datetime] = None; notes_applied_id: str = ""; notes_paging: bool = False
notes_scanned_at: Optional[datetime] = None; notes_merge_version: int = 0; notes_space: str = ""
notes_space_at: Optional[datetime] = None; notes_space_id: str = ""
```
`DriveRecordLean.owner_note_id: str = ""` (and so `RECORD_LEAN_FIELDS`).

**`src/domain/drive_note_merge.py`** (pure): `NOTE_MERGE_VERSION = 1`, `NOTE_PAGE = 200`, `NOTE_WATERMARK_SKEW_S = 60`,
`NOTE_EMBED_PER_TICK = 20`, `NOTE_CHECKS_MAX = 500`, `NOTE_AUDIT_EVERY = timedelta(hours=24)`, `NOTE_AUDIT_SKIP_DAYS = 7`,
`NOTE_STEP_S = 60`, `NOTE_LOOKUP_BATCH = 30`;
```python
@dataclass(frozen=True) class NoteView: note_id: str; version: int; text: str; doc_type: str; life_areas: Tuple[str, ...]
def record_binding_key(drive_id: str, item_id: str) -> str
def note_view(note: Optional[DriveNote], *, drive_id: str, provider_key: str = ONEDRIVE_PROVIDER_KEY) -> Optional[NoteView]
def with_owner_note(rec: DriveIndexRecord, view: Optional[NoteView]) -> DriveIndexRecord
def merged(rec: DriveIndexRecord, view: Optional[NoteView]) -> bool
def note_store_keys(content: NoteContent, name: str) -> Tuple[List[str], str]       # (keys, keys_version)
def notes_query_start(state: DriveSyncState) -> Tuple[Optional[datetime], str]
effective_doc_type(rec), effective_life_areas(rec)    # re-exported from drive_key_sources
```
**Keys** (`drive_keys`): `note_keys(text, doc_type="", life_areas=()) -> Tuple[str, ...]`, `note_keys_digest() -> str`;
`AboutField.OWNER_NOTE` (details `type` / `area` / `number` / `date` / `word`); `RecordKeyInputs` / `KeyedRecord` gain
`owner_note_text`, `owner_doc_type`, `owner_life_areas`. C's deleted-files search recomputes a stale note `keys` with
`note_store_keys(note.content, note.snapshot.name)` when `note.keys_version != f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"`.

**Rendering** (`drive_card`): `owner_segment(rec) -> str`, `card_text(rec, *, owner=True)`,
`owner_note_line(text, doc_type, life_areas) -> str` (`noted by the owner: «…» (type …; life areas …)`, the one
renderer: C's `drive_note_render` uses it for the deleted-files note line), `note_line(rec)`.
`DriveSearchCard.owner_note: str = ""` (set by `make_card`).

**Tick hooks C extends** (`src/services/drive_indexing_service.py`):
- `_TickNotes` (a tick-scoped `ContextVar` `_TICK_NOTES`, set in `_run` and `_notes_gated`) with `drive_id`, `count`,
  `rescan`, `more` and `onto(state)`. **C adds** `checks: List[NoteCheck]` and folds them into `state.note_checks` in
  `onto` (cap `NOTE_CHECKS_MAX`). `onto` is already applied by `_notes_onto(...)` at the final save and at all three
  error-path saves of `_run_spending` (RFC §3.4); C may also fold in `_observe`'s per-page saves if it collects checks
  there.
- `_notes_count(user_id, tick)` (the per-tick count gate) and `self._notes` (`Optional[DriveNoteRepository]`, the
  constructor's new `notes=` keyword).
- Entry (a) `_lookup_notes` and `_reactivated` (C's round end calls `self._notes.archive`; entry (a) undoes it on a
  restore).
- `note_checks` placement: **B does not touch it** (no field, no reader). C adds `note_checks` and `notes_audited_at`
  to `DriveSyncState`, `_STATE_READERS` (`firestore_drive_index_repo.py`, after B's notes readers) and the codec fixture.
- The round end (RFC §5.1 c) goes at the start of `_work_off` when `state.round_complete` — B leaves `_work_off`
  unchanged.
- The delete sites C hooks (`_apply`'s tombstones, `_cascade_step`, `_sweep`, `_remove_orphan`, `_rescope_step`) read
  `owner_note_id` from the records they delete; B adds nothing there.

**Composition:** `drive_indexing_setup.drive_notes_enabled(value) -> bool`,
`build_drive_note_repository(db_client, env_config, *, enabled=True) -> Optional[DriveNoteRepository]`;
`DriveIndexingService(..., notes=None)`; `FileManagementAgent(..., drive_notes=None, drive_port=None)`;
`UserAgentFactory(..., drive_note_service=None, user_drive=None)`; `ServiceContainer.agent_services()["user_drive"]`.

### 2.2 B needs from A

- `ServiceContainer.drive_note_service: Optional[DriveNoteService]` built by A (with `DRIVE_NOTES_ENABLED` and the
  wiring A owns) and `agent_services()["drive_note_service"]`, so the factory parameter B declares
  (`drive_note_service`) receives it. **Shared parameter:** B's Task 9 adds `drive_note_service` and `user_drive` to
  `UserAgentFactory.__init__`; if A's task lands first with `drive_note_service`, B's hunk adds only `user_drive` (same
  name, same type, `Optional`, default None). Until A wires it, the open header shows no line (tested: no service → no
  line).
- Nothing else: B reads the note store through the frozen port and `DriveNoteService.note_for_item` only.

### 2.3 B needs from C

Nothing. Until C lands, an archived note is never merged (entry (b) clears a copy whose note is archived) and nothing
archives one; the D9 invariant holds.

---

## 3. Tasks

Order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10, then the docs tasks D1-D4. Each task: TDD (write the new tests, see
them fail, apply the hunks, see them pass), `make check` green apart from the task's expected-risk tests (handed to the
reviewer), commit. The hunks are `git diff cef8f147 855488f8 -- <file>` of the verified worktree (each file's whole change); a new module is shown
whole.

### Task 1: record and sync-state fields, the lean field, the planner flag, the codec readers

**Goal.** The copy of the note on the record (§3.3), the notes step's state (§3.4 plus the core review's pair cursor),
`owner_note_id` in the lean projection (I7), `_build` flags a record it creates (I2), Firestore readers for the new
non-string fields.

**Files.** Modify `src/domain/drive_index.py`, `src/domain/drive_index_lean.py`, `src/domain/drive_index_planner.py`,
`src/adapters/firestore_drive_index_repo.py`. (In-memory repository: nothing; it stores the dataclasses.)

```diff
diff --git a/src/adapters/firestore_drive_index_repo.py b/src/adapters/firestore_drive_index_repo.py
index 379b0965..3d0d365d 100644
--- a/src/adapters/firestore_drive_index_repo.py
+++ b/src/adapters/firestore_drive_index_repo.py
@@ -247,6 +247,7 @@ _RECORD_READERS: Dict[str, Callable[[Any], Any]] = {
     # §27 (transcripts)
     "transcript_wake_at": _dt, "transcript_languages": _strs, "transcript_hold": _optional(str),
     "mirror_retry_at": _dt,       # §27.9.4
+    "owner_life_areas": _strs,    # DRIVE_FILE_NOTES_RFC §3.3
 }
 
 _STATE_READERS: Dict[str, Callable[[Any], Any]] = {
@@ -262,6 +263,9 @@ _STATE_READERS: Dict[str, Callable[[Any], Any]] = {
     "described_count": int, "described_grew_at": _dt,
     "migration_alerted": bool, "stall_alerted": bool, "fallback_alerted_day": str,
     "purge_keep_transcripts": bool,
+    # DRIVE_FILE_NOTES_RFC §3.4: the notes step's watermark pair, pass end and space-pass cursor
+    "notes_applied_at": _dt, "notes_scanned_at": _dt, "notes_space_at": _dt, "notes_paging": bool,
+    "notes_merge_version": int,
 }
 
 
diff --git a/src/domain/drive_index.py b/src/domain/drive_index.py
index a61e1f96..7c43bcde 100644
--- a/src/domain/drive_index.py
+++ b/src/domain/drive_index.py
@@ -417,6 +417,19 @@ class DriveIndexRecord:
     mirrored_digest: str = ""
     mirror_retry_at: Optional[datetime] = None
     mirror_held_digest: str = ""
+    # -- owner notes (DRIVE_FILE_NOTES_RFC §3.3): a denormalized copy of the owner's note bound to this item, rebuilt
+    # from the note store by the tick (`drive_note_merge.with_owner_note`); the note store is the record. Plain fields,
+    # neutral defaults (Δ47's rollback invariant): a record without a note keeps a byte-identical card (G5). The copy
+    # carries the note's id and version and the NOTE_MERGE_VERSION it was merged under (0: no note).
+    owner_note_id: str = ""
+    owner_note_version: int = 0
+    owner_note_text: str = ""
+    owner_doc_type: str = ""                  # "" or one drive_labels.DOC_TYPES value
+    owner_life_areas: Tuple[str, ...] = ()    # 0-3 drive_labels.LIFE_DOMAINS values, the owner's order
+    owner_note_merge_version: int = 0
+    # set by the planner's `_build` on a record it creates; `_commit` looks the item's note up by its binding key in the
+    # same write and clears it, found or not (§5.1 a). Always False once written.
+    note_lookup_due: bool = False
 
     @property
     def stale(self) -> bool:
@@ -480,6 +493,23 @@ class DriveSyncState:
     stall_alerted: bool = False               # the rebuild stalled for DESCRIBED_STALL_AFTER: alerted once
     fallback_alerted_day: str = ""            # "YYYY-MM-DD" the fallback alert was posted for
     purge_keep_transcripts: bool = False      # §27.8 b: the running purge keeps the stored transcripts
+    # -- owner notes (DRIVE_FILE_NOTES_RFC §3.4, §5.1 b): the notes step's state, purged with the sync state (each is
+    # re-derivable: a purge re-reads every note). The watermark is the (updated_at, note_id) pair of the last note the
+    # step processed; `notes_paging`: the last page was full, so the next one starts strictly after that pair (the
+    # core's pair cursor); otherwise a new pass starts `NOTE_WATERMARK_SKEW_S` before the earlier of the watermark and
+    # `notes_scanned_at` (when the last pass ended), so a late-visible commit is caught and a full skew window is never
+    # re-read twice (`drive_note_merge.notes_query_start`). `notes_merge_version` != NOTE_MERGE_VERSION resets the
+    # watermark (§5.5). `notes_space` is the embedding space the user's note vectors were last moved to; while it
+    # differs from the service's space, a re-embed pass pages through the notes from (`notes_space_at`,
+    # `notes_space_id`).
+    notes_applied_at: Optional[datetime] = None
+    notes_applied_id: str = ""
+    notes_paging: bool = False
+    notes_scanned_at: Optional[datetime] = None
+    notes_merge_version: int = 0
+    notes_space: str = ""
+    notes_space_at: Optional[datetime] = None
+    notes_space_id: str = ""
 
     def cards_in_space(self, embedding_space: str) -> bool:
         """The card stream answers queries embedded in `embedding_space`: no card is left in another space."""
diff --git a/src/domain/drive_index_lean.py b/src/domain/drive_index_lean.py
index a2255bb9..865b3eb7 100644
--- a/src/domain/drive_index_lean.py
+++ b/src/domain/drive_index_lean.py
@@ -41,6 +41,9 @@ class DriveRecordLean:
     indexed_generation: Optional[int]
     content_hash: Optional[str]
     valid_until: Optional[datetime]
+    # DRIVE_FILE_NOTES_RFC §3.3, §6.1 (review I7): the note merged into this record ("" for none), so the duplicate
+    # collapse can show the noted copy of identical files. A record stored before it reads back "".
+    owner_note_id: str = ""
 
 
 @dataclass(frozen=True)
diff --git a/src/domain/drive_index_planner.py b/src/domain/drive_index_planner.py
index b4d81d01..9515d0a6 100644
--- a/src/domain/drive_index_planner.py
+++ b/src/domain/drive_index_planner.py
@@ -359,6 +359,9 @@ def _build(change: DriveChange, cls: Classification, existing: Optional[DriveInd
         name_tokens=name_keys_v2(change.name) if searchable(cls.reason) else (), name_keys_version=NAME_KEYS_VERSION,
         content_hash=content_hash_of(change), probe_pending=cls.probe_pending, probed_child_count=probed,
         seen_epoch=_stamp(existing, ctx, observed),
+        # DRIVE_FILE_NOTES_RFC §5.1 a (review I2): a record this observation creates looks its owner note up in the
+        # one write gate (`_commit`); an existing record carries its copy of the note across (`replace(existing)`)
+        note_lookup_due=True if existing is None else base.note_lookup_due,
     )
     # A capped record keeps its cap until a release decides again (§5.4); one that can hold no card drops it.
     rec = replace(rec, card_capped=rec.card_capped and structurally_card_eligible(rec))
```

**New tests.** `tests/unit/adapters/test_firestore_drive_index_notes_codec.py` (whole file in commit `8eb0ad7e`):
`test_the_record_copy_round_trips` (owner fields through `record_to_doc`/`record_from_doc` on stored types; the flag is
written False), `test_the_notes_state_round_trips_with_plain_datetimes` (every notes field is in `STATE_FIELDS` and
reads back as a plain UTC datetime), `test_old_documents_read_back_neutral_defaults`,
`test_the_lean_projection_carries_the_note_id`. `tests/unit/domain/test_drive_note_merge.py::
test_build_flags_a_created_record_and_carries_the_copy_across_an_observation` (runs after Task 3).

**Expected-risk existing tests** (failed in the worktree, for the reviewer; each encodes a field list the RFC changes on
purpose):
- `tests/unit/domain/test_drive_search_v2_frozen_core.py::TestTypes::test_fields[DriveRecordLean-expected11]` — the
  frozen `DriveRecordLean` field list; actual has one more item, `owner_note_id` (RFC §3.3 review I7 adds it).
- `tests/unit/adapters/test_firestore_drive_index_repo_serialization.py::TestRecordSerialization::test_fixture_sets_every_field`
  — `full_record()` leaves the 7 new record fields at their defaults (`owner_note_id`, …, `note_lookup_due`).
- `…::TestStateSerialization::test_fixture_sets_every_field` — `full_state()` leaves the 8 new state fields at defaults.
  For both fixtures the reviewer is expected to set the new fields (note `note_lookup_due=True` round-trips too).

**Done.** The four new codec tests pass; `pytest tests/unit/domain -k drive -n 4` and
`pytest tests/unit/adapters -k drive_index -n 4` fail only on the three listed tests. **Model:** Sonnet.

### Task 2: the note keys (`drive_keys`, `drive_key_sources`, the cap's home)

**Goal.** §5.4: `note_keys`, `AboutField.OWNER_NOTE`, the note's items first in `_about_items`, the `l:type` /
`l:domain` overrides (also without a profile), the D31 names dropped only for an overridden field, `note_keys_digest`;
`NOTE_KEYS_MAX` moved beside the keyer's caps (Core delta B1: `drive_note` imports `drive_keys`, so the keyer cannot
import `drive_note`).

**Files.** Modify `src/domain/drive_index_params.py`, `src/domain/drive_note.py` (re-export only),
`src/domain/drive_key_sources.py`, `src/domain/drive_keys.py`.

```diff
diff --git a/src/domain/drive_index_params.py b/src/domain/drive_index_params.py
index 71f89949..0d96f0bc 100644
--- a/src/domain/drive_index_params.py
+++ b/src/domain/drive_index_params.py
@@ -257,3 +257,7 @@ TRANSCRIBE_UPLOAD_S = 60                    # G16: worst case of uploading one w
 TRANSCRIBE_FILE_LIFE_AT_SUBMIT = timedelta(hours=24)   # §27.5: an uploaded window is submitted only with this much of its provider file's life left (the batch API's 24 h contract); less is re-extracted first
 TRANSCRIBE_MIN_RUN_WALL_S = 90              # Task 12: a further extraction run of a file within one step starts only with this much of the step's wall left (one 30-min window needs ~32 CPU-s plus its bytes)
 TRANSCRIPT_OPEN_MAX_CHARS = 60_000          # G19: not an RFC name; the most an open returns, the summary input bound
+# DRIVE_FILE_NOTES_RFC §5.4 (N1): the owner note's own key cap, classifiers first and never cut. Defined here so the
+# keyer (`drive_keys`) can read it without importing `drive_note` (which imports the keyer); `drive_note` re-exports it.
+# Deliberately NOT in `drive_keys.index_tables_digest`: it shapes only noted records, which NOTE_MERGE_VERSION re-merges.
+NOTE_KEYS_MAX = 320
diff --git a/src/domain/drive_key_sources.py b/src/domain/drive_key_sources.py
index fdc8b136..c08be45e 100644
--- a/src/domain/drive_key_sources.py
+++ b/src/domain/drive_key_sources.py
@@ -11,6 +11,8 @@ Public interface:
     AboutSource         (field, detail, printed, source_class) of one about key; recomputed at render time
     KeyedRecord         the record attributes the keyer reads (Protocol)
     RecordKeyInputs     a frozen carrier of those attributes
+    effective_doc_type(rec), effective_life_areas(rec)   the owner's classifiers over the profile's, read at key and
+                        render time (DRIVE_FILE_NOTES_RFC §5.4, D7); the stored profile is never changed
 """
 from __future__ import annotations
 
@@ -43,6 +45,9 @@ class AboutField(str, Enum):
     VISIBLE_TEXT_NUMBER = "visible_text_number"
     CAPTURE = "capture"                   # the capture's names (§26.3); `detail` is the capture value
     SCENE_LABEL = "scene_label"           # a scene label's names (D40); `detail` is the label
+    # the owner's note on the item (DRIVE_FILE_NOTES_RFC §5.4): `e:` / `v:` / `y:` / `m:` keys (About) and `ex:` / `exl:`
+    # skeletons (By translation); `detail` is "type" / "area" (a classifier name), "number", "date" or "word"
+    OWNER_NOTE = "owner_note"
 
 
 @dataclass(frozen=True)
@@ -99,6 +104,15 @@ class KeyedRecord(Protocol):
     @property
     def capture_source(self) -> str: ...
 
+    @property
+    def owner_note_text(self) -> str: ...
+
+    @property
+    def owner_doc_type(self) -> str: ...
+
+    @property
+    def owner_life_areas(self) -> Tuple[str, ...]: ...
+
 
 @dataclass(frozen=True)
 class RecordKeyInputs:
@@ -116,3 +130,23 @@ class RecordKeyInputs:
     visible_text: str = ""
     capture: str = ""                  # §26.3: a drive_capture.Capture value, or ""
     capture_source: str = ""           # a drive_capture.CaptureSource value, or ""
+    # DRIVE_FILE_NOTES_RFC §5.4: the record's copy of the owner's note; empty by default, so the golden record emits
+    # no note keys and `golden_digest` stays pinned
+    owner_note_text: str = ""
+    owner_doc_type: str = ""
+    owner_life_areas: Tuple[str, ...] = ()
+
+
+def effective_doc_type(rec: KeyedRecord) -> str:
+    """The doc type search groups and reads by (D7): the owner's when he gave one, else the profile's ("" without
+    either)."""
+    if rec.owner_doc_type:
+        return rec.owner_doc_type
+    return rec.profile.doc_type if rec.profile is not None else ""
+
+
+def effective_life_areas(rec: KeyedRecord) -> Tuple[str, ...]:
+    """The life areas search groups and reads by (D7): the owner's when he gave any, else the profile's."""
+    if rec.owner_life_areas:
+        return tuple(rec.owner_life_areas)
+    return tuple(rec.profile.life_domains) if rec.profile is not None else ()
diff --git a/src/domain/drive_keys.py b/src/domain/drive_keys.py
index 1476e7d0..536c5bc1 100644
--- a/src/domain/drive_keys.py
+++ b/src/domain/drive_keys.py
@@ -18,7 +18,10 @@ Public interface:
     match_spans(text, keys) -> ((start, end), ...)    where any of a concept's word keys occurs in `text`
     name_keys_v2(name), query_name_keys_v2(query)     name keys v2 (NAME_KEYS_V2) and the name stream's query keys
     name_word_concepts(query) -> (Concept, ...)       a query left with no concept, its words searched by name (xc F2)
+    note_keys(text, doc_type, life_areas) -> (key, ...)   the owner note's keys (DRIVE_FILE_NOTES_RFC §5.4), at most
+                                               NOTE_KEYS_MAX; a record's copy of the note leads its about keys
     index_tables_digest(), golden_digest()     version pins: the tables, and the rules' output on a fixed fixture
+    note_keys_digest()                         the note keyer's pin, per NOTE_MERGE_VERSION (never in the two above)
     FUNCTION_WORDS, GENERIC_WORDS, TIME_WORDS, NAME_KEYS_V2
 
 Families (§25.2.4, review M8): native-script forms and the spellings a file prints are `w:` (chunks, names) and `e:`
@@ -31,6 +34,10 @@ A photo object's phrases are `pe:` and a photo's visible-text numbers `pn:`: aut
 its prefix alone. The D31 tables name the profile's own doc type (`lw:`) and life areas (`dw:`) in four languages. A
 file's capture (§26.3, Δ58) is keyed by the CAPTURE_NAMES table as phrases, like the `k:` table: `cp:` when code
 detected it (verified), `cpa:` when a model said it (automatic); a query word emits both, as it emits `k:`.
+The owner's note (DRIVE_FILE_NOTES_RFC §5.4, D7) reuses the existing families under `AboutField.OWNER_NOTE` (no new
+family, so no KEYS_VERSION bump): its classifier names and words are `e:` (exact: the owner's own words), the words'
+skeletons `ex:` / `exl:`, its numbers and dates `v:` / `y:` / `m:` by the profile's own recipe. The owner's doc type
+and life areas override the profile's in the `l:` grouping keys, and the D31 names of an overridden field are left out.
 """
 from __future__ import annotations
 
@@ -50,6 +57,7 @@ from .drive_enrichment import DriveProfile, Grade, GradedField, KeyValue, PhotoO
 from .drive_index import TriageVerdict
 from .drive_index_params import (
     ABOUT_KEYS_PER_RECORD, CONCEPT_KEYS_MAX, ENTITY_FORMS_MAX, MAX_CONCEPTS, NAME_MIN_TRIGRAMS, NAME_QUERY_KEYS_MAX,
+    NOTE_KEYS_MAX,
     PHOTO_OBJECT_FORMS_MAX, PHOTO_OBJECTS_MAX, PHOTO_SCENE_LABELS_MAX, PHRASE_RUN_MAX, PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX,
     PROFILE_KEY_VALUES_MAX, PROFILE_WORD_LIST_MAX, QUERY_TERM_CHARS, WORD_KEYS_PER_CHUNK,
 )
@@ -57,7 +65,9 @@ from .drive_key_forms import (
     FORM_TABLES, SHORT_WORD_LETTERS, Token, is_short_function_word, primary, query_skeletons, scan, skeleton_key,
     token_forms, token_spans,
 )
-from .drive_key_sources import AboutField, AboutSource, KeyedRecord, RecordKeyInputs
+from .drive_key_sources import (
+    AboutField, AboutSource, KeyedRecord, RecordKeyInputs, effective_doc_type, effective_life_areas,
+)
 from .drive_labels import (
     CAPTURE_NAMES, DOC_TYPE_NAMES, KIND_WORDS, LIFE_DOMAIN_NAMES, PHOTO_KIND_WORDS, SCENE_NAMES,
     capture_names_digest, label_names_digest, scene_names_digest,
@@ -68,7 +78,7 @@ from .drive_name_keys import _is_cyrillic, _strip_accents, _trigrams
 __all__ = [
     "FUNCTION_WORDS", "GENERIC_WORDS", "NAME_KEYS_V2", "TIME_WORDS", "about_keys_capped", "about_keys_of", "about_sources",
     "all_forms", "family_keys", "fetch_batches", "golden_digest", "index_tables_digest", "match_spans",
-    "name_keys_v2", "name_match_v2", "name_word_concepts", "phrase_keys", "query_concepts", "query_name_keys_v2", "source_class_of_about_key",
+    "name_keys_v2", "name_match_v2", "name_word_concepts", "note_keys", "note_keys_digest", "phrase_keys", "query_concepts", "query_name_keys_v2", "source_class_of_about_key",
     "source_class_of_name_key", "source_class_of_word_key", "token_forms", "token_spans", "word_form_keys",
     "word_keys", "word_keys_capped",
 ]
@@ -379,12 +389,19 @@ def _about_items(rec: KeyedRecord) -> List[_Item]:
         native, skel = _phrase_items(text, last)
         add([prefix + s for s in native] + (_skel_keys(skel) if skeletons else []), field, detail, text, in_file)
 
+    # the owner's note first (DRIVE_FILE_NOTES_RFC §5.4): the record cap keeps the earliest keys, so it never cuts them
+    items.extend(_note_items(rec.owner_note_text, rec.owner_doc_type, rec.owner_life_areas)[0])
     profile = rec.profile
     lab = KeyFamily.LABEL.value
-    if profile is not None:
+    # D7: the owner's doc type and life areas override the profile's in the grouping keys, also on a record with no
+    # profile; without them the profile's are emitted exactly as before (a profile's "" type included)
+    if rec.owner_doc_type:
+        add([f"{lab}type:{rec.owner_doc_type}"], AboutField.LABEL, "type", rec.owner_doc_type)
+    elif profile is not None:
         add([f"{lab}type:{profile.doc_type}"], AboutField.LABEL, "type", profile.doc_type)
-        for domain in profile.life_domains:
-            add([f"{lab}domain:{domain}"], AboutField.LABEL, "domain", domain)
+    for domain in (rec.owner_life_areas if rec.owner_life_areas
+                   else profile.life_domains if profile is not None else ()):
+        add([f"{lab}domain:{domain}"], AboutField.LABEL, "domain", domain)
     if rec.valid_until is not None:
         add([f"{lab}valid"], AboutField.LABEL, "valid", rec.valid_until.isoformat()[:10])
 
@@ -436,9 +453,11 @@ def _about_items(rec: KeyedRecord) -> List[_Item]:
         items.extend(_date_items(eff.year, eff.month, AboutField.EFFECTIVE_DATE, "", eff.isoformat()[:10]))
 
     if profile is not None:
-        # D31: the fixed names of the profile's own doc type and life areas join the model's words (after them)
-        type_names = DOC_TYPE_NAMES.get(profile.doc_type, ())
-        domain_names = [n for d in profile.life_domains for n in LIFE_DOMAIN_NAMES.get(d, ())]
+        # D31: the fixed names of the profile's own doc type and life areas join the model's words (after them); a field
+        # the owner overrode has none (his names are `e:` keys already, DRIVE_FILE_NOTES_RFC §5.4)
+        type_names = () if rec.owner_doc_type else DOC_TYPE_NAMES.get(profile.doc_type, ())
+        domain_names = [] if rec.owner_life_areas else [n for d in profile.life_domains
+                                                        for n in LIFE_DOMAIN_NAMES.get(d, ())]
         lists = [(KeyFamily.LABEL_WORD.value, [*profile.label_words[:PROFILE_WORD_LIST_MAX], *type_names],
                   AboutField.LABEL_WORD),
                  (KeyFamily.TOPIC_WORD.value, list(profile.topic_words[:PROFILE_WORD_LIST_MAX]),
@@ -473,10 +492,107 @@ def _about_items(rec: KeyedRecord) -> List[_Item]:
     return items
 
 
+# -- the owner's note (DRIVE_FILE_NOTES_RFC §5.4) -------------------------------------------------------------------
+
+def _note_items(text: str, doc_type: str, life_areas: Sequence[str]) -> Tuple[List[_Item], bool]:
+    """The owner note's about keys with their sources, in the order its cap keeps, and whether NOTE_KEYS_MAX cut some
+    (§5.4, review I5, I6):
+    1. the owner's classifier names (DOC_TYPE_NAMES, LIFE_DOMAIN_NAMES) as `e:` phrases, native forms only, no
+       skeletons (as the D31 names), detail "type" / "area": at most ~85 keys, never cut;
+    2. numbers and dates by the profile's own recipe, found as the query finds them (`query_term_spans`): a value's
+       maximal forms as `v:` (never a fragment: "1 250,00" keys v:1250.00 and v:1250, not v:250) and an id's unspaced
+       form; a year 1900-2099 also `y:`; a date `v:` ISO plus `y:` and `m:`; a month-year span `y:` and `m:YYYY-MM`.
+       A short id with letters ("ред3") is left to the words, as the query leaves it (review m1);
+    3. each content word (function, generic and time words removed, as the query removes them; a hyphen compound is
+       one word) as its own phrase: `e:` native forms and its `ex:` / `exl:` skeleton.
+    Past NOTE_KEYS_MAX distinct keys the tail is cut: only the last words' keys, the words staying in the card text."""
+    items: List[_Item] = []
+    note = AboutField.OWNER_NOTE
+
+    def add(keys: Iterable[str], detail: str, printed: str) -> None:
+        for key in keys:
+            items.append((key, AboutSource(note, detail, printed, source_class_of_about_key(key))))
+
+    type_names = DOC_TYPE_NAMES.get(doc_type, ()) if doc_type else ()
+    area_names = [n for a in life_areas for n in LIFE_DOMAIN_NAMES.get(a, ())]
+    for detail, names in (("type", type_names), ("area", area_names)):
+        for name in names:
+            native, _ = _phrase_items(name)
+            add([_E + form for form in native], detail, name)
+    text = text or ""
+    if text:
+        spans = query_term_spans(text)
+        dates = [(s, e) for s, e, _, cat in spans if cat == "date"]
+        months = [(s, e, ym) for s, e, ym in _month_year_spans(text) if not any(_overlaps((s, e), d) for d in dates)]
+        consumed: List[Span] = []
+        for start, end, forms, category in spans:
+            printed = text[start:end].strip()
+            if not forms:
+                continue
+            if category == "date":
+                for iso in forms:
+                    add([_V + iso], "date", iso)
+                    try:
+                        year, month = int(iso[:4]), int(iso[5:7])
+                    except ValueError:
+                        continue
+                    items.extend(_date_items(year, month, note, "date", iso))
+                consumed.append((start, end))
+                continue
+            if any(_overlaps((start, end), (ms, me)) for ms, me, _ in months):
+                consumed.append((start, end))   # a year inside "March 2024": the month-year keys it
+                continue
+            if category == "id" and any(ch.isalpha() for ch in forms[0]) and len(forms[0]) <= _SHORT_ID_CHARS:
+                continue   # "ред3": words, as the query reads it (review m1)
+            values = dict.fromkeys([*forms, *(_unspaced_id_forms(printed) if category == "id" else [])])
+            add([_V + f for f in values], "number", printed)
+            add([_Y + f for f in forms if len(f) == 4 and f.isdigit() and _year_ok(int(f))], "number", printed)
+            consumed.append((start, end))
+        for start, end, ym in months:
+            items.extend(_date_items(int(ym[:4]), int(ym[5:]), note, "date", text[start:end].strip()))
+            consumed.append((start, end))
+        toks = scan(text)
+        i = 0
+        while i < len(toks):   # the query's own word loop (`query_concepts`), so a word keys as a query word reads
+            first, j = i, i
+            if toks[i].compound:
+                while j + 1 < len(toks) and toks[j].end < toks[i].compound_end:
+                    j += 1
+            if toks[i].compound_kind == "alnum" and all(token_forms(t.text) for t in toks[first:j + 1]):
+                j = first
+            word = toks[i].compound if j > first else toks[i].text
+            span = (toks[i].start, toks[j].end)
+            i = j + 1
+            if any(_overlaps(span, c) for c in consumed) or _is_removable(word) or not token_forms(word):
+                continue
+            native, skel = _phrase_items(word)
+            add([_E + form for form in native] + _skel_keys(skel), "word", text[span[0]:span[1]])   # as written
+    kept: Dict[str, None] = {}
+    out: List[_Item] = []
+    cut = False
+    for key, source in items:
+        if key not in kept:
+            if len(kept) >= NOTE_KEYS_MAX:
+                cut = True
+                continue
+            kept[key] = None
+        out.append((key, source))
+    return out, cut
+
+
+def note_keys(text: str, doc_type: str = "", life_areas: Sequence[str] = ()) -> Tuple[str, ...]:
+    """The owner note's keys (§5.4): classifier names first, then numbers and dates, then words, at most NOTE_KEYS_MAX.
+    The note store's `keys` and a noted record's leading about keys."""
+    return tuple(dict.fromkeys(key for key, _ in _note_items(text, doc_type, life_areas)[0]))
+
+
 def about_keys_capped(rec: KeyedRecord) -> Tuple[Tuple[str, ...], bool]:
-    """The record's about keys and whether ABOUT_KEYS_PER_RECORD cut some (the earliest entries are kept)."""
+    """The record's about keys and whether a cap cut some: ABOUT_KEYS_PER_RECORD (the earliest entries are kept), or
+    the note's own NOTE_KEYS_MAX (its last words, DRIVE_FILE_NOTES_RFC §5.4)."""
     keys = list(dict.fromkeys(key for key, _ in _about_items(rec)))
-    return tuple(keys[:ABOUT_KEYS_PER_RECORD]), len(keys) > ABOUT_KEYS_PER_RECORD
+    note_cut = bool(rec.owner_note_text) and _note_items(rec.owner_note_text, rec.owner_doc_type,
+                                                          rec.owner_life_areas)[1]
+    return tuple(keys[:ABOUT_KEYS_PER_RECORD]), len(keys) > ABOUT_KEYS_PER_RECORD or note_cut
 
 
 def about_keys_of(rec: KeyedRecord) -> Tuple[str, ...]:
@@ -785,3 +901,28 @@ def golden_digest() -> str:
     out.append(about_keys_of(rec))
     return hashlib.sha256(repr(out).encode("utf-8")).hexdigest()
 
+
+# Fixed owner notes in four languages (DRIVE_FILE_NOTES_RFC §5.4): their keys, and a noted record's about keys and
+# grouping overrides, are pinned per NOTE_MERGE_VERSION by their own test. Kept out of the two digests above on purpose:
+# a change here shapes only noted records, which a NOTE_MERGE_VERSION bump re-merges (§5.5, N9).
+_NOTE_GOLDEN: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
+    ("Смета Пети на ремонт кухни, 1 250,00 EUR, оплатить до 15.03.2024", "contract", ("home", "finance")),
+    ("Кошторис Петра на ремонт кухні, березень 2024, рахунок INV-2024/0012", "", ("home",)),
+    ("Petya's estimate for the kitchen renovation, March 2024; Wi-Fi router ред3", "invoice", ()),
+    ("Presupuesto de Petya para la cocina, pagado en 2023", "", ("finance", "home", "family")),
+    ("", "contract", ()),
+)
+
+
+def note_keys_digest() -> str:
+    """SHA-256 over the note keyer's output on `_NOTE_GOLDEN`, and over one noted record's about keys (a profile typed
+    `invoice` overridden by the owner's `contract`). A test pins it per NOTE_MERGE_VERSION."""
+    out: List[object] = [note_keys(text, doc_type, areas) for text, doc_type, areas in _NOTE_GOLDEN]
+    rec = RecordKeyInputs(kind="pdf", owner_note_text=_NOTE_GOLDEN[0][0], owner_doc_type="contract",
+                          owner_life_areas=("home",),
+                          profile=DriveProfile("t", "w", "ru", "invoice", life_domains=("finance",),
+                                               label_words=("invoice", "счёт")))
+    out.append(about_keys_of(rec))
+    out.append((effective_doc_type(rec), effective_life_areas(rec)))
+    return hashlib.sha256(repr(out).encode("utf-8")).hexdigest()
+
diff --git a/src/domain/drive_note.py b/src/domain/drive_note.py
index 4bb1830a..61d940da 100644
--- a/src/domain/drive_note.py
+++ b/src/domain/drive_note.py
@@ -44,6 +44,7 @@ from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tupl
 from pydantic import BaseModel, ConfigDict, Field
 
 from .drive_index import DriveChange, DriveIndexRecord, RecordState
+from .drive_index_params import NOTE_KEYS_MAX   # §5.4: the note's key cap; defined beside the keyer's caps (S2 Core delta B1)
 from .drive_card import content_hash_of
 from .drive_index_policy import kind_of
 from .drive_keys import phrase_keys, token_spans
@@ -55,7 +56,6 @@ from .user_drive import DriveItem, make_drive_ref, parse_drive_ref
 
 NOTE_WORDS_MAX = 80                         # N1: words, counted with the keyer's `token_spans` (script-independent)
 NOTE_CHARS_MAX = 600                        # N1: characters (code points) of the normalized text
-NOTE_KEYS_MAX = 320                         # §5.4: the note's own key cap (classifiers first, never cut); used by S2
 LIFE_AREAS_MAX = LIFE_DOMAINS_MAX           # 3, the profile's own cap (drive_profile.py)
 ERASE_CODE_TTL = timedelta(minutes=10)      # §4.8: an erase code works once, for ten minutes
 NOTE_WRITES_PER_TURN = 10                   # §4.2: per execution, then refused (a steered burst is bounded)
```

**New tests.** `tests/unit/domain/test_drive_keys_owner_note.py` (commit `fd79edb5`): the maximal note is valid (80
words, ≤ 600 chars); existing families only; order classifiers → numbers/dates → words; classifier names without
skeletons; the worst type + worst three areas < 100 keys, over every combination (table test); a maximal Cyrillic note
with them keeps every classifier key and the cap cuts only the last word (`about_keys_capped` reports the cut); no
fragment `v:250` for "1 250,00" (not covered by "250", covered by "1250" and "1 250,00"); a date keys `v:` ISO + `y:` +
`m:`; "March 2024" keys `m:2024-03`, a standalone year `y:`, 1850 none; sources are `OWNER_NOTE`, `e:` verified, the
skeleton a translation, classifier detail `type`; note keys lead the about keys; a full profile plus a maximal note cuts
the record (600) but never the note's keys; the overrides with and without a profile; D31 names dropped only for the
overridden field; "contract" / "договор" / "contrato" / "договір" cover through the owner's type in a verified family;
a record without a note emits no note source; `note_keys_digest` pinned per `NOTE_MERGE_VERSION`
(`{1: "4739cc0b5138"}`).

**Invariant pins, unchanged:** `tests/unit/domain/test_drive_keys_tables.py` (`index_tables_digest`, `golden_digest`,
capture and scene digests), `test_drive_keys_record.py`, `test_drive_capture_keys.py`, `test_drive_scene_labels.py`,
`tests/unit/domain/test_drive_note.py`. A failure there means the design is wrong, not the test.

**Expected-risk existing tests.** None (all key tests passed in the worktree).

**Done.** New file green; the pins green. **Model:** Sonnet.

### Task 3: `src/domain/drive_note_merge.py` (new)

**Goal.** §5.1-§5.5: the one merge function, the view rule (active, not a stub, bound on the current drive of the one
provider), the note document's keys, the watermark's next start, the constants. `effective_doc_type` /
`effective_life_areas` live in `drive_key_sources` (Task 2: the keyer needs them and must not import this module) and
are re-exported here.

**File.** Create `src/domain/drive_note_merge.py`, verbatim:

```python
"""
Drive note merge — the owner's note copied into the index record of its item (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md
§5.1-§5.5; D7, D9, D15). Pure: stdlib and domain modules only; no I/O, no clock (callers pass `now`).

The note store (`{prefix}drive_notes`) is the record; the index record carries a denormalized copy (`owner_*` fields),
rebuilt from the store by the indexer's tick through ONE function, `with_owner_note(rec, view)`. A view exists only for
an `active` note bound to this very item on the current drive of the current provider (D15: identity is the provider's
item id); anything else (cleared, archived, an erase stub, another drive) clears the copy. The record then goes through
`drive_enrich_plan.planned` (its about keys carry the note's keys) and `schedule` (a changed card hash makes card work
due in class 0); this module decides nothing about scheduling.

`NOTE_MERGE_VERSION` versions the card segment and the note's keys. It is NOT in `CURRENT_VERSIONS` (N9): a bump resets
the notes step's watermark (`DriveSyncState.notes_merge_version`), which re-reads every note and re-merges exactly the
noted records.

Public interface:
    NoteView                                     (note_id, version, text, doc_type, life_areas) of a merged note
    note_view(note, *, drive_id, provider_key) -> Optional[NoteView]
    with_owner_note(rec, view) -> DriveIndexRecord        idempotent; None clears the copy
    merged(rec, view) -> bool                    the record already carries exactly this view (no write needed)
    effective_doc_type(rec), effective_life_areas(rec)    the owner's classifiers over the profile's (re-exported)
    record_binding_key(drive_id, item_id) -> str  the `i:` key of an index record's item (ONEDRIVE_PROVIDER_KEY)
    note_store_keys(content, name) -> (keys, keys_version)   the note document's `keys` (§3.1) and their version
    notes_query_start(state) -> (after, after_note_id)       where the notes step's next `changed_since` starts
    NOTE_MERGE_VERSION and the tick constants (§15): NOTE_PAGE, NOTE_WATERMARK_SKEW_S, NOTE_EMBED_PER_TICK,
    NOTE_CHECKS_MAX, NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS, NOTE_STEP_S, NOTE_LOOKUP_BATCH
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .drive_index import KEYS_VERSION, DriveIndexRecord, DriveSyncState
from .drive_key_sources import effective_doc_type, effective_life_areas
from .drive_keys import name_keys_v2, note_keys
from .drive_note import (
    NOTE_KEYS_VERSION, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteContent, NoteState, binding_key, binding_parts_of,
)

# -- parameters (§15) -------------------------------------------------------------------------------------------------

NOTE_MERGE_VERSION = 1                      # §5.5: the card segment and the note's keys; outside CURRENT_VERSIONS (N9)
NOTE_PAGE = 200                             # §5.1 b: notes per `changed_since` page; a full page asks for a continuation
NOTE_WATERMARK_SKEW_S = 60                  # §5.1 b: a new pass starts this far before the watermark (late commits)
NOTE_EMBED_PER_TICK = 20                    # §5.1 b: note vectors re-embedded per tick (repair and space pass together)
NOTE_CHECKS_MAX = 500                       # §7.1 (S3): archive checks handled per round end
NOTE_AUDIT_EVERY = timedelta(hours=24)      # §7.4 (S3)
NOTE_AUDIT_SKIP_DAYS = 7                    # §7.4 (S3)
# The notes step's own start window (S2 plan): its Firestore calls start only within this many seconds of where the
# step began, and only while the tick's start budget lasts. Its worst overrun past the start budget is NOTE_STEP_S plus
# one call (FILE_START_MARGIN_S), below an enrichment file's (ENRICH_FILE_BUDGET_S plus the margin) that the §7.1
# deadline sum already holds, and nothing after it starts content work past the start budget: the sum is unchanged.
NOTE_STEP_S = 60
NOTE_LOOKUP_BATCH = 30                      # §5.1 a: binding keys per `array-contains-any` (the repository batches too)


@dataclass(frozen=True)
class NoteView:
    """The part of an `active` note an index record carries (§5.1): which note, which version, and the triple."""
    note_id: str
    version: int
    text: str
    doc_type: str
    life_areas: Tuple[str, ...]


def record_binding_key(drive_id: str, item_id: str) -> str:
    """The `i:` key of an index record's item on the current drive: the one provider key (never a literal), so the
    indexer and the main service build the same key (core contract §5)."""
    return binding_key(ONEDRIVE_PROVIDER_KEY, drive_id, item_id)


def note_view(note: Optional[DriveNote], *, drive_id: str, provider_key: str = ONEDRIVE_PROVIDER_KEY) -> Optional[NoteView]:
    """The view to merge for `note`, or None to clear the copy: only an `active` note (not an erase stub) whose binding
    is on `drive_id` of `provider_key` gives one (D9: an archived note is never merged; D15, D16: a note bound to
    another drive is dormant). A note whose triple is empty is never `active`, but is cleared all the same."""
    if note is None or note.erase_pending or note.state != NoteState.ACTIVE.value or not drive_id:
        return None
    parts = binding_parts_of(note)
    if parts is None or parts.provider_key != provider_key or parts.drive_id != drive_id:
        return None
    content = note.content
    if content.is_empty:
        return None
    return NoteView(note_id=note.note_id, version=note.current_version, text=content.text,
                    doc_type=content.doc_type, life_areas=tuple(content.life_areas))


def with_owner_note(rec: DriveIndexRecord, view: Optional[NoteView]) -> DriveIndexRecord:
    """The record with `view` as its copy of the owner's note, or with the copy cleared (None). Pure and idempotent;
    touches the owner fields only (never the profile, the accuracy, the effective date or any content state, §5.6)."""
    if view is None:
        return replace(rec, owner_note_id="", owner_note_version=0, owner_note_text="", owner_doc_type="",
                       owner_life_areas=(), owner_note_merge_version=0)
    return replace(rec, owner_note_id=view.note_id, owner_note_version=view.version, owner_note_text=view.text,
                   owner_doc_type=view.doc_type, owner_life_areas=tuple(view.life_areas),
                   owner_note_merge_version=NOTE_MERGE_VERSION)


def merged(rec: DriveIndexRecord, view: Optional[NoteView]) -> bool:
    """The record already carries exactly `view` under the current NOTE_MERGE_VERSION (or no note, for None): a
    re-merge would write nothing."""
    return with_owner_note(rec, view) == rec


def note_store_keys(content: NoteContent, name: str) -> Tuple[List[str], str]:
    """The note document's `keys` (§3.1): the note's own keys and the snapshot name's name keys v2, used only by the
    deleted-files search; and `keys_version` = "{KEYS_VERSION}.{NOTE_KEYS_VERSION}" (a mismatch recomputes them in
    memory at search time). An empty triple has none."""
    if content.is_empty:
        return [], ""
    keys = list(dict.fromkeys([*note_keys(content.text, content.doc_type, content.life_areas),
                               *(name_keys_v2(name) if name else ())]))
    return keys, f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"


def notes_query_start(state: DriveSyncState) -> Tuple[Optional[datetime], str]:
    """Where the notes step's next `changed_since` starts (§5.1 b, core review §5): from the beginning when there is no
    watermark; strictly after the watermark pair while a pass pages (the last page was full); otherwise a new pass, from
    NOTE_WATERMARK_SKEW_S before the earlier of the watermark and the end of the last pass — so a commit that became
    visible late is caught, and a skew window full of notes (an erase-all) is read once more at most, never on every
    tick (the window ends a minute after the last pass, not a minute before the newest note)."""
    if state.notes_applied_at is None:
        return None, ""
    if state.notes_paging:
        return state.notes_applied_at, state.notes_applied_id
    floor = (state.notes_scanned_at or state.notes_applied_at) - timedelta(seconds=NOTE_WATERMARK_SKEW_S)
    if state.notes_applied_at <= floor:
        return state.notes_applied_at, state.notes_applied_id
    return floor, ""


__all__ = [
    "NOTE_AUDIT_EVERY", "NOTE_AUDIT_SKIP_DAYS", "NOTE_CHECKS_MAX", "NOTE_EMBED_PER_TICK", "NOTE_LOOKUP_BATCH",
    "NOTE_MERGE_VERSION", "NOTE_PAGE", "NOTE_STEP_S", "NOTE_WATERMARK_SKEW_S", "NoteView", "effective_doc_type",
    "effective_life_areas", "merged", "note_store_keys", "note_view", "notes_query_start", "record_binding_key",
    "with_owner_note",
]
```

**New tests.** `tests/unit/domain/test_drive_note_merge.py` (commit `fd79edb5`): set/change/clear idempotent and
`merged`; the merge touches only the owner fields; the view rule (archived, cleared, other drive, other provider,
stub, no drive id, None); `record_binding_key` is the `ONEDRIVE_PROVIDER_KEY` key; `_build` flags a created record and
carries the copy across a rename; `planned()` recomputes the about keys from the copy; a carded noted record is due for
its card in class 0 (`Priority.EDIT`); `note_store_keys`; `notes_query_start` (no watermark, paging, a recent pass, an
old pass, no pass end).

**Expected-risk.** None. **Done.** File green; `mypy --strict` (`make typecheck`) clean. **Model:** Sonnet.

### Task 4: the card segment and the rendered line (`drive_card`)

**Goal.** §5.3: `owner_segment` right after `name | kind` in every branch (a collapsed folder: after its label; the
plain folder keeps its exact Slice 1 text without a note), the profile's doc type dropped when the owner gave one
(also with `owner=False`), `card_text(rec, *, owner=True)`; §6.2: `owner_note_line` / `note_line`.

**File.** Modify `src/domain/drive_card.py`.

```diff
diff --git a/src/domain/drive_card.py b/src/domain/drive_card.py
index 132811a6..f54b46f1 100644
--- a/src/domain/drive_card.py
+++ b/src/domain/drive_card.py
@@ -3,6 +3,10 @@ Card text and its hash (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.5, §25.2.7)
 `name | kind | ... | key facets`; no path, so a folder rename touches no descendant. CARD_VERSION 3 carries the
 profile and the photo fields; no lead text but for machine text, so card kNN and chunk kNN no longer vote on one signal.
 CARD_VERSION 4 adds the capture (§26.3) and drops the brief's doc type (Δ64), and a photograph's scene labels (D40).
+
+The owner's note (DRIVE_FILE_NOTES_RFC §5.3, D7) is a segment right after `name | kind` in every branch, with no
+CARD_VERSION bump (the G5 precedent): `_joined` drops an empty segment, so a record without a note keeps a byte-identical
+card text and hash. `note_line(rec)` is the same note as the search card renders it, on its own line.
 """
 from __future__ import annotations
 
@@ -159,6 +163,37 @@ def _joined(parts: List[str]) -> str:
     return " | ".join(p for p in parts if p)
 
 
+def _owner_parts(text: str, doc_type: str, life_areas: Tuple[str, ...]) -> List[str]:
+    out = [text] if text else []
+    if doc_type:
+        out.append(f"type {doc_type}")
+    if life_areas:
+        out.append("life areas " + ", ".join(life_areas))
+    return out
+
+
+def owner_segment(rec: DriveIndexRecord) -> str:
+    """DRIVE_FILE_NOTES_RFC §5.3: `noted by the owner: <text>; type <doc_type>; life areas <a>, <b>`, each part only when
+    present (a classifier-only note reads `noted by the owner: type contract`, never an empty quote); "" without a note.
+    The owner's words outrank the model's, so the segment sits right after `name | kind`."""
+    parts = _owner_parts(rec.owner_note_text, rec.owner_doc_type, tuple(rec.owner_life_areas))
+    return "noted by the owner: " + "; ".join(parts) if parts else ""
+
+
+def owner_note_line(text: str, doc_type: str, life_areas: Tuple[str, ...]) -> str:
+    """The rendered note (§6.2): `noted by the owner: «<text>» (type contract; life areas home)`, or `noted by the owner:
+    type contract` for a classifier-only note; "" for no note. One renderer for search cards and the open header."""
+    labels = _owner_parts("", doc_type, tuple(life_areas))
+    if text:
+        return f"noted by the owner: «{text}»" + (f" ({'; '.join(labels)})" if labels else "")
+    return "noted by the owner: " + "; ".join(labels) if labels else ""
+
+
+def note_line(rec: DriveIndexRecord) -> str:
+    """`owner_note_line` of the record's copy of the note ("" for a record without one)."""
+    return owner_note_line(rec.owner_note_text, rec.owner_doc_type, tuple(rec.owner_life_areas))
+
+
 def _transcript_segment(rec: DriveIndexRecord) -> str:
     """§27.8 (a): the transcript line of a transcribed media record; none for every other kind and for a media
     record without a transcript, so no existing card's text (and no `card_hash`) changes (G5)."""
@@ -167,8 +202,11 @@ def _transcript_segment(rec: DriveIndexRecord) -> str:
     return card_segment(rec) or ""
 
 
-def card_text(rec: DriveIndexRecord) -> str:
-    """§25.2.7 (CARD_VERSION 4: §26.3's capture), each segment only when present:
+def card_text(rec: DriveIndexRecord, *, owner: bool = True) -> str:
+    """§25.2.7 (CARD_VERSION 4: §26.3's capture), each segment only when present; the owner's note segment
+    (`owner_segment`, DRIVE_FILE_NOTES_RFC §5.3) right after `name | kind` (a collapsed folder: after its label) in every
+    branch, and a profiled file's doc type left out when the owner gave one (never shown twice). `owner=False` leaves
+    the segment out: the search card renders the note on its own line (§6.2), the card hash and embedding keep it.
     - profiled file: `name | kind | capture | doc_type | what_it_is | title | entities (as printed) | topics |
       topics_native | facets`;
     - text file without a profile: `name | kind | capture | brief (no doc type, Δ64) | facets`;
@@ -178,27 +216,31 @@ def card_text(rec: DriveIndexRecord) -> str:
       appended (§27.8 a, no CARD_VERSION bump, G5);
     - collapsed folder with a README profile: `name | label | what_it_is | title | topics`; without, its Slice 1
       card."""
+    seg = owner_segment(rec) if owner else ""
     if rec.is_folder:
         if rec.scope is Scope.OPAQUE_ROOT:
             p = rec.profile
             extra = [p.what_it_is, p.title, ", ".join(p.topics)] if p is not None else []
-            return _joined([rec.name, _opaque_label(rec), *extra])
-        return f"{rec.name} | folder"
+            return _joined([rec.name, _opaque_label(rec), seg, *extra])
+        # the plain folder: the exact Slice 1 text without a note (an empty name included), the segment after it
+        return f"{rec.name} | folder | {seg}" if seg else f"{rec.name} | folder"
     kind = _KIND_LABEL.get(rec.kind, "file")
     if rec.reason is Reason.MACHINE_TEXT:
-        return _joined([rec.name, kind, _MACHINE_TEXT, rec.lead_text, _transcript_segment(rec)])
+        return _joined([rec.name, kind, seg, _MACHINE_TEXT, rec.lead_text, _transcript_segment(rec)])
     if rec.triage_verdict is TriageVerdict.PHOTO and rec.state is not RecordState.PENDING:
-        return _joined([rec.name, kind, rec.photo_description, rec.photo_scene,
+        return _joined([rec.name, kind, seg, rec.photo_description, rec.photo_scene,
                         ", ".join(scene_label_text(label) for label in rec.photo_scene_labels),
                         ", ".join(o.name for o in rec.photo_objects if o.name), rec.visible_text, *_key_facets(rec)])
     p = rec.profile
     if p is not None:
-        # an entity as the file prints it, never only the profiler's spelling of it (xc F1)
-        return _joined([rec.name, kind, _capture_segment(rec), p.doc_type, p.what_it_is, p.title,
+        # an entity as the file prints it, never only the profiler's spelling of it (xc F1); the profile's doc type
+        # only when the owner gave none (D7: the owner's type is in the segment, and the stored profile is untouched)
+        return _joined([rec.name, kind, seg, _capture_segment(rec), "" if rec.owner_doc_type else p.doc_type,
+                        p.what_it_is, p.title,
                         ", ".join(e.printed or e.name for e in p.entities if e.printed or e.name),
                         ", ".join(p.topics),
                         ", ".join(p.topics_native), *_key_facets(rec), _transcript_segment(rec)])
-    return _joined([rec.name, kind, _capture_segment(rec), *_brief_segments(rec.brief), *_key_facets(rec),
+    return _joined([rec.name, kind, seg, _capture_segment(rec), *_brief_segments(rec.brief), *_key_facets(rec),
                     _transcript_segment(rec)])
```

**New tests.** `tests/unit/domain/test_drive_card_owner_note.py` (commit `fd79edb5`): **no note → byte-identical
`card_text` and `card_hash`** on a fixture of every branch (profiled, unprofiled, machine text, photograph, transcribed
recording, collapsed folder, plain folder; the expected texts are the pre-change cards, the G5 pin); an empty-name
folder keeps ` | folder`; the segment position in every branch; `owner=False`; the profile type shown when the owner
gave none and never twice; classifier-only segment and line with no empty quote; the rendered line's three shapes.

**Expected-risk.** None (every existing card test passed). **Model:** Sonnet.

### Task 5: search rendering and the search service

**Goal.** §6.1-§6.3: `your note "…"` / `your note: type "…"` / `your note: life area "…"` sources; the note line on
every card (`make_card` sets it from the record, so every call site renders it), after the summary in `card_body` (so
the 700-character cut never spends on it) and in the listing's `_card_body`, the first 120 characters on a Related card;
`card_text(rec, owner=False)` at the four `make_card` call sites; `_collapse` shows the first noted copy of identical
files (lean `owner_note_id`), else the first shown copy as before.

**Files.** Modify `src/domain/drive_tier_explain.py`, `src/domain/drive_search.py`, `src/domain/drive_tier_render.py`,
`src/services/drive_search_service.py`.

```diff
diff --git a/src/domain/drive_search.py b/src/domain/drive_search.py
index ebbfa224..b174280d 100644
--- a/src/domain/drive_search.py
+++ b/src/domain/drive_search.py
@@ -31,7 +31,7 @@ from datetime import date, datetime, time, timezone
 from typing import Callable, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple
 
 from .drive_accuracy import scorable
-from .drive_card import taken_at
+from .drive_card import note_line, taken_at
 from .drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState, Scope, TriageVerdict, inaccessible
 from .drive_index_params import ACCESS_LOSS_TTL, WRAPPER_TAG_GAP_MAX
 from .drive_index_policy import Admission, admission, kind_of
@@ -202,6 +202,9 @@ class DriveSearchCard:
     capture_source: str = ""
     scene_labels: Tuple[str, ...] = ()   # a photograph's scene labels (D40): its `scene:` line
     card_accuracy: Optional[int] = None  # 0-100 (§26.4): its `card accuracy: N%` line; None for a photo or an undescribed file
+    # DRIVE_FILE_NOTES_RFC §6.2: the owner's note as one rendered line (`drive_card.note_line`), "" for none; printed
+    # right after the summary, which is then `card_text(rec, owner=False)`, so the render cut never spends on the note
+    owner_note: str = ""
 
     def drive_item(self) -> DriveItem:
         """The Step 1 item, built only now that a path is derived (§3.1)."""
@@ -496,7 +499,9 @@ def is_not_read_yet(rec: DriveIndexRecord, admitted: Optional[FrozenSet[str]] =
 
 
 def make_card(rec: DriveIndexRecord, path: str, summary: str, nonce: str) -> DriveSearchCard:
-    """A record the bot cannot open (§26.2) keeps a readable generation it had, shown stale (Δ52)."""
+    """A record the bot cannot open (§26.2) keeps a readable generation it had, shown stale (Δ52). The owner's note
+    line comes from the record (DRIVE_FILE_NOTES_RFC §6.2), so every call site renders it; each passes
+    `card_text(rec, owner=False)` as the summary."""
     closed = inaccessible(rec)
     return DriveSearchCard(
         ref=make_drive_ref(rec.item_id), item_id=rec.item_id, name=rec.name, path=path, kind=rec.kind,
@@ -506,7 +511,7 @@ def make_card(rec: DriveIndexRecord, path: str, summary: str, nonce: str) -> Dri
         stale=rec.stale or (closed and rec.indexed_generation is not None), truncated=rec.truncated,
         content_hash=rec.content_hash, nonce=nonce, date_source=date_source(rec), valid_until=rec.valid_until,
         inaccessible=closed, capture=rec.capture, capture_source=rec.capture_source, card_accuracy=rec.card_accuracy if scorable(rec) else None,
-        scene_labels=rec.photo_scene_labels,
+        scene_labels=rec.photo_scene_labels, owner_note=note_line(rec),
     )
 
 
@@ -578,7 +583,8 @@ def _day(value: Optional[datetime]) -> str:
 def _card_body(card: DriveSearchCard) -> str:
     dated = f"{_day(card.effective_date)} ({card.date_source})" if card.effective_date and card.date_source \
         else _day(card.effective_date)
-    lines = [card.summary, f"path: {card.path or '/'}", f"date: {dated}"]
+    lines = [card.summary, *([card.owner_note] if card.owner_note else []), f"path: {card.path or '/'}",
+             f"date: {dated}"]
     if card.valid_until is not None:
         lines.append(VALID_UNTIL_LINE.format(iso=card.valid_until.date().isoformat()))
     captured = capture_line(card)
diff --git a/src/domain/drive_tier_explain.py b/src/domain/drive_tier_explain.py
index 237c166a..9bf23f3d 100644
--- a/src/domain/drive_tier_explain.py
+++ b/src/domain/drive_tier_explain.py
@@ -50,6 +50,10 @@ _MATCH_FILE_DATE = 'date "{word}"'
 _MATCH_KIND = 'kind "{word}"'
 _MATCH_CAPTURE = 'capture "{word}"'          # §26.3: detected by code (`cp:`); a model's (`cpa:`) is "(automatic)"
 _MATCH_AUTOMATIC = '{field} "{word}" (automatic)'
+# the owner's note (DRIVE_FILE_NOTES_RFC §6.1): his own words, exact; a skeleton match adds the script note
+_MATCH_NOTE_WORD = 'your note "{word}"'
+_MATCH_NOTE_TYPE = 'your note: type "{word}"'
+_MATCH_NOTE_AREA = 'your note: life area "{word}"'
 _NOT_MATCHED = "not matched"
 _AUTOMATIC_FIELD = {
     AboutField.TOPIC_WORD: "topic", AboutField.DOMAIN_WORD: "life area", AboutField.VALIDITY_WORD: "validity",
@@ -92,6 +96,9 @@ def _word_in(text: str, prefix: str, keys: Sequence[str]) -> str:
 
 def _about_text(source: AboutSource, keys: Sequence[str]) -> str:
     field, printed = source.field, source.printed
+    if field is AboutField.OWNER_NOTE:
+        template = {"type": _MATCH_NOTE_TYPE, "area": _MATCH_NOTE_AREA}.get(source.detail, _MATCH_NOTE_WORD)
+        return template.format(word=printed)
     if field is AboutField.ENTITY_FORM:
         kind = source.detail if source.detail and source.detail != "other" else "entity"
         return _MATCH_ENTITY_FORM.format(type=kind, word=printed, printed=source.in_file)
diff --git a/src/domain/drive_tier_render.py b/src/domain/drive_tier_render.py
index 8f605778..4f549642 100644
--- a/src/domain/drive_tier_render.py
+++ b/src/domain/drive_tier_render.py
@@ -39,6 +39,7 @@ _HEADINGS = {MatchTier.MENTIONED: MENTIONED_HEADING, MatchTier.TRANSLATED: TRANS
 _GROUP_WORD = {MatchTier.ABOUT: "matching", MatchTier.MENTIONED: "mentioning", MatchTier.TRANSLATED: "translated",
                MatchTier.LABELLED: "labelled"}
 _RELATED_CARD_CHARS = 300
+_RELATED_NOTE_CHARS = 120     # DRIVE_FILE_NOTES_RFC §6.2: a Related card's note line, cut
 _CUT_LINE = "Shortened to fit: fewer files are shown as cards; their counts and folders are above."
 _EXPIRED_LINE = "{count} more {files} with these words already expired."
 _NOTHING = "No matching files or folders."
@@ -160,6 +161,8 @@ def _no_passages_note(tiered: TieredResult) -> Optional[str]:
 
 def card_body(card: DriveSearchCard) -> str:
     lines = [_cut(card.summary, CARD_RENDER_MAX)]
+    if card.owner_note:
+        lines.append(card.owner_note)          # DRIVE_FILE_NOTES_RFC §6.2: after the summary, outside its cut
     if card.sources:
         lines.append(MATCHED_LINE.format(sources="; ".join(card.sources)))
     lines.extend(card.not_matched)
@@ -200,6 +203,7 @@ def _related_body(card: DriveSearchCard) -> str:
     day = _day(card)
     tail = f" | {day}" if day else ""
     body = _cut(card.summary, _RELATED_CARD_CHARS - len(tail)) + tail
+    body += f"\n{_cut(card.owner_note, _RELATED_NOTE_CHARS)}" if card.owner_note else ""
     body += f"\n{SIMILAR_NAME}" if card.similar_name else ""
     accuracy = accuracy_line(card)         # §26.4: a Related card is opened, and answers a value question, like any
     body += f"\n{accuracy}" if accuracy else ""
diff --git a/src/services/drive_search_service.py b/src/services/drive_search_service.py
index 1c6fc909..d6100479 100644
--- a/src/services/drive_search_service.py
+++ b/src/services/drive_search_service.py
@@ -1005,20 +1005,31 @@ class DriveSearchService:
 
     def _collapse(self, tiered: Mapping[MatchTier, List[str]], pool: _Pool,
                   scope: _Scope) -> Tuple[List[Tuple[MatchTier, str]], Dict[str, List[str]]]:
-        """The shown files in tier order, identical copies (same content hash, not empty) collapsed into the first
-        shown one's `also_at`."""
+        """The shown files in tier order, identical copies (same content hash, not empty) collapsed into one shown
+        copy's `also_at`: the first noted copy when any copy carries the owner's note (decided on the lean
+        `owner_note_id`, DRIVE_FILE_NOTES_RFC §6.1, review I7), else the first shown one."""
+        candidates = [(tier, item) for tier in _TIERS for item in tiered.get(tier, [])[:_SHOWN[tier]]]
+
+        def collapsible(item: str) -> Optional[str]:
+            digest = pool.leans[item].content_hash
+            return digest if digest and not digest.endswith(":0") else None
+
+        keeper: Dict[str, str] = {}
+        for _, item in candidates:
+            digest = collapsible(item)
+            if digest is None:
+                continue
+            noted = bool(pool.leans[item].owner_note_id)
+            if digest not in keeper or (noted and not pool.leans[keeper[digest]].owner_note_id):
+                keeper[digest] = item
         shown: List[Tuple[MatchTier, str]] = []
-        first: Dict[str, str] = {}
         also_at: Dict[str, List[str]] = {}
-        for tier in _TIERS:
-            for item in tiered.get(tier, [])[:_SHOWN[tier]]:
-                digest = pool.leans[item].content_hash
-                if digest and not digest.endswith(":0") and digest in first:
-                    also_at.setdefault(first[digest], []).append(scope.paths[item])
-                    continue
-                if digest:
-                    first[digest] = item
-                shown.append((tier, item))
+        for tier, item in candidates:
+            digest = collapsible(item)
+            if digest is not None and keeper[digest] != item:
+                also_at.setdefault(keeper[digest], []).append(scope.paths[item])
+                continue
+            shown.append((tier, item))
         return shown, also_at
 
     async def _build_cards(self, user_id: str, shown: Sequence[Tuple[MatchTier, str]],
@@ -1045,7 +1056,7 @@ class DriveSearchService:
             if tier in _PASSAGE_TIERS and text:
                 passage = passage_window(text, explained.spans, min(WINDOW_MAX, budget))
                 budget -= len(passage)
-            card = make_card(rec, scope.paths[item], card_text(rec), self._nonce())
+            card = make_card(rec, scope.paths[item], card_text(rec, owner=False), self._nonce())
             out.append(replace(card, tier=tier.value, sources=explained.sources, passage=passage,
                                not_matched=not_matched(match), keys_cut=rec.keys_capped,
                                also_at=tuple(also_at.get(item, ())), expiry_match=_expiry_match(match)))
@@ -1053,7 +1064,7 @@ class DriveSearchService:
 
     def _related_card(self, pool: _Pool, scope: _Scope, item: str, similar: bool) -> DriveSearchCard:
         rec = pool.fulls[item]
-        card = make_card(rec, scope.paths[item], card_text(rec), self._nonce())
+        card = make_card(rec, scope.paths[item], card_text(rec, owner=False), self._nonce())
         return replace(card, tier=MatchTier.RELATED.value, similar_name=similar)
 
     @staticmethod
@@ -1149,7 +1160,7 @@ class DriveSearchService:
                 continue   # keep scanning: a later duplicate still adds its path to a shown card
             if same_as:
                 by_hash[same_as] = len(cards)
-            cards.append(make_card(rec, path, card_text(rec), self._nonce()))
+            cards.append(make_card(rec, path, card_text(rec, owner=False), self._nonce()))
         return tuple(cards)
 
     # ------------------------------------------------------------------------------------------
@@ -1200,7 +1211,7 @@ class DriveSearchService:
             return matches[0]
         if not matches or guessed:
             return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND, folder_lookup_truncated=page_full)
-        cards = tuple(make_card(f.record, f.path, card_text(f.record), self._nonce())
+        cards = tuple(make_card(f.record, f.path, card_text(f.record, owner=False), self._nonce())
                       for f in sorted(matches, key=lambda f: f.path)[:K])
         return DriveSearchResult(status=STATUS_AMBIGUOUS_FOLDER, cards=cards)
```

**New tests.** `tests/unit/domain/test_drive_tier_explain_owner_note.py` (commit `fd79edb5`): a note word match is
About with `your note "Смета"` (the owner's spelling); the type and area sources; a two-word note-only match is About;
a Latin query reaching the note across scripts is By translation with the script note.
`tests/unit/services/test_drive_search_owner_note.py` (commit `98ca4eb1`): the line inside the wrapper right after the
summary, the summary without it; the owner's type finds the file and the summary drops the profile's type; a tag
look-alike in a note is escaped and no wrapper is forged; the summary cut (700) never spends on the note; the
filter-only listing, the ambiguous-folder candidates and a Related card (120 characters) render it; the accuracy line
unchanged; the noted copy is the collapse representative (and the lean carries the id); without a note the first copy
stays.

**Expected-risk.** None. **Model:** Sonnet.

### Task 6: the tick (entries a and b, the gate, the repair, the closed-gate step)

**Goal.** §5.1, §5.5 and the core review's S2 requirements (§1.1 table): `_TickNotes` and `_notes_onto`; `_lookup_notes`
in `_commit` (count gate; `record_binding_key`; `_reactivated` for an archived note and a cleared one with
`archived_at`; a store failure sets `rescan`, never ends the tick); `_notes_step` after observe (merge-version reset,
`notes_query_start`, one page, `_merge_notes`: a non-active note clears only its own copy, a noted record whose derived
fields lost the copy is rewritten, stubs deleted after the commit; watermark pair, paging, pass end; `more` →
continuation); `_repair_note_vectors` (pending first, then the space pass from its own cursor, stopping before a failed
embedding, stamping `notes_space`), `_embed_notes` (`set_vector` with the embedded version); `_notes_gated` from both
closed branches of `tick()` (its own budget, cards re-embedded by `_gated_cards`, dropped when the embedder fails);
the `notes=` constructor keyword; the drive id kept current by `_observe`; `_has_more` reads `more`.

**File.** Modify `src/services/drive_indexing_service.py`.

```diff
diff --git a/src/services/drive_indexing_service.py b/src/services/drive_indexing_service.py
index 7983cbaf..6eeb5565 100644
--- a/src/services/drive_indexing_service.py
+++ b/src/services/drive_indexing_service.py
@@ -67,6 +67,20 @@ the work-off (after a complete round), only for a user whose transcription is on
 `MirrorHost`, within its own budgets and window (`_Budget.mirror_stop`). While the user has mirror work it can do now
 (`_mirror_ready`, Δ209, Δ217) the content classes, enrichment and extraction stop MIRROR_RESERVE_S earlier. Mirror work
 never asks for a continuation; its counters join the tick line (`mirror={...}`).
+
+The owner's notes (DRIVE_FILE_NOTES_RFC §5.1, §5.5; plan part B): `notes` is the DriveNoteRepository (None: the
+DRIVE_NOTES_ENABLED kill switch is off, or nothing is wired; then none of this runs). The note store is the record; each
+index record carries a copy (`drive_note_merge.with_owner_note`), written by two entries, both through `_commit`:
+(a) `_lookup_notes`, in `_commit` itself, for every record the planner created (`note_lookup_due`): the note found by
+its binding key is merged in the same write, an archived one re-activated first; (b) `_notes_step`, every tick after
+observe and before the work-off: the notes changed since the watermark (`changed_since`, paged, the pair cursor kept in
+the sync state) reconcile their records, an erase stub is deleted once its record's copy is cleared, then the vector
+repair and the space pass re-embed note vectors. One note count per tick gates both (a user without notes pays one
+read). The notes step also runs while the access gate is closed (the access-loss grace and the D30 rebuild gate,
+`_notes_gated`), re-embedding the changed cards at once, so a cleared or erased note never outlives its owner's
+command in a record copy, its keys or a card vector. A note-store failure never ends the tick: the step is skipped, a
+lookup that failed resets the watermark (`_TickNotes.rescan`). The step starts only inside the start budget and stops
+starting calls NOTE_STEP_S after it began, so the §7.1 deadline sum is unchanged (`drive_note_merge.NOTE_STEP_S`).
 """
 from __future__ import annotations
 
@@ -132,11 +146,16 @@ from ..domain.drive_index_planner import (
     parent_scope, plan_observation, readme_read, reembed_in_space, reextraction_due, released, schedule,
     stale_versions, structure_stale, triage_due, triaged, with_card, with_retry, without_card,
 )
-from ..domain.drive_keys import word_form_keys, word_keys_capped
+from ..domain.drive_keys import about_keys_capped, word_form_keys, word_keys_capped
 from ..domain import drive_mirror_plan as mirror_versions
 from ..domain.drive_mirror import GONE, UNKNOWN, UNSTORED, DeletionNotice, MirrorRoot
 from ..domain.drive_mirror_plan import local_day, mirror_plan
 from ..domain.drive_mirror_render import MirrorStrings
+from ..domain.drive_note import ONEDRIVE_PROVIDER_KEY, DriveNote, NoteState, binding_parts_of, note_embedding_text
+from ..domain.drive_note_merge import (
+    NOTE_EMBED_PER_TICK, NOTE_MERGE_VERSION, NOTE_PAGE, NOTE_STEP_S, NoteView, note_view, notes_query_start,
+    record_binding_key, with_owner_note,
+)
 from ..domain.language import LanguageCode
 from ..domain.ui_messages import UIMessage
 from ..domain.drive_profile import (
@@ -173,6 +192,7 @@ from ..ports.content_extractor_port import ContentExtractorPort
 from ..ports.document_profiler_port import DocumentProfilerPort
 from ..ports.drive_index_repository import DriveIndexRepository
 from ..ports.drive_mirror_repository import DriveMirrorRepository
+from ..ports.drive_note_repository import DriveNoteRepository
 from ..ports.embedding_service import EmbeddingService
 from ..ports.image_triage_port import ImageTriagePort
 from ..ports.language_service_port import LanguageServicePort
@@ -511,6 +531,29 @@ class _Mirror:
 
 # Per tick: None outside a tick or with no mirror repository wired (no root ids, no marks).
 _TICK_MIRROR: ContextVar[Optional[_Mirror]] = ContextVar("drive_tick_mirror", default=None)
+
+
+@dataclass
+class _TickNotes:
+    """The tick's owner-notes work (DRIVE_FILE_NOTES_RFC §5.1): `drive_id` the binding keys are built for (the
+    connection's, else the sync state's; kept current by observe); `count` the user's notes, read once at its first use
+    (the per-tick gate: a user without notes pays that one read); `rescan`: a lookup by binding key failed, so records
+    were written without their notes — the next save resets the notes watermark and the notes step re-merges every
+    note; `more`: the notes step left a full page (a continuation). Part C folds its archive checks in `onto` too."""
+    drive_id: str
+    count: Optional[int] = None
+    rescan: bool = False
+    more: bool = False
+
+    def onto(self, state: DriveSyncState) -> DriveSyncState:
+        """What every sync-state save of the tick carries from here (the error paths' saves included)."""
+        if not self.rescan:
+            return state
+        return replace(state, notes_applied_at=None, notes_applied_id="", notes_paging=False)
+
+
+# Per tick: None outside a tick or with no note repository wired.
+_TICK_NOTES: ContextVar[Optional[_TickNotes]] = ContextVar("drive_tick_notes", default=None)
 # What makes the tick read the marked records again for its counters: it changed a marking itself (review of S1)
 _MARK_CHANGES = ("inaccessible", "inaccessible_folders", "inaccessible_rechecked", "access_regained")
 
@@ -570,6 +613,7 @@ class DriveIndexingService:
                  mirror: Optional["DriveTranscriptMirror"] = None,
                  localization: Optional["LocalizationService"] = None,
                  language: Optional[LanguageServicePort] = None,
+                 notes: Optional[DriveNoteRepository] = None,
                  clock: Callable[[], datetime] = _utcnow, monotonic: Callable[[], float] = time.monotonic) -> None:
         if mirror is not None and (mirror_repo is None or localization is None or language is None):
             raise ValueError("the transcript mirror step needs the mirror repository, the localization service and "
@@ -609,6 +653,9 @@ class DriveIndexingService:
         # the step could not start (its UI language or strings unreadable), per user: the same ladder, held off the
         # reserve only (`_note_step_unavailable`; process memory only)
         self._mirror_step_holds: Dict[str, Tuple[int, datetime]] = {}
+        # the owner-note store (DRIVE_FILE_NOTES_RFC §5.1): read and lifecycle-written by the tick, never the owner's
+        # words; None: the DRIVE_NOTES_ENABLED kill switch is off (composition), and no notes work runs
+        self._notes = notes
         self._clock = clock
         self._monotonic = monotonic
         self._gate_checked: Dict[str, float] = {}
@@ -649,8 +696,11 @@ class DriveIndexingService:
                 return await self._purge(user_id, state, action, enabled, summary, cont)
             if action is AccessAction.RECORD_LOSS:
                 if state.access_lost_at is None:
-                    await self._save_state(replace(state, access_lost_at=now))
+                    state = replace(state, access_lost_at=now)
+                    await self._save_state(state)
                     logger.warning(f"[DriveIndex] access lost for {user_id[:8]}; grace period started")
+                # the index keeps its cards for the grace: the owner's note commands still reach them (core review §5)
+                await self._notes_gated(user_id, state, connection, cont)
                 return TickResult("access_lost")
             if MIGRATION_GATE_KEY not in state.applied_versions:
                 # D30: built by v1, never upgraded. No tick work until the owner's rebuild purges it; the fan-out
@@ -663,6 +713,8 @@ class DriveIndexingService:
                 else:
                     self._migration_warned.add(user_id)
                     logger.warning(line)
+                # no index work, but a cleared or erased note must not outlive the command in a record (core review §5)
+                await self._notes_gated(user_id, state, connection, cont)
                 return TickResult("awaiting_migration")
             if action is AccessAction.RESUME_AFTER_REGAIN:
                 state = replace(state, access_lost_at=None)
@@ -691,6 +743,8 @@ class DriveIndexingService:
         spend_token = _TICK_SPEND.set(spend)
         closed_token = _TICK_CLOSED.set(None)   # read inside the tick's error handling (_run_spending)
         mirror_token = _TICK_MIRROR.set(None)   # read at the start of _run_spending, inside its error handling
+        notes_token = _TICK_NOTES.set(_TickNotes(drive_id=(connection.drive_id if connection is not None else "")
+                                                 or state.drive_id) if self._notes is not None else None)
         transcription = _Transcription(on=self._transcription_on(user_id))   # as the gate read it: kept for the tick
         if transcription.on:
             transcription.host = _TickHost(self, user_id, budget, models, summary, transcription)
@@ -699,6 +753,7 @@ class DriveIndexingService:
             return await self._run_spending(user_id, state, nudge, now, enabled, budget, summary, cont, saved,
                                             models, spend, connection)
         finally:
+            _TICK_NOTES.reset(notes_token)
             _TICK_MIRROR.reset(mirror_token)
             _TICK_TRANSCRIPTION.reset(transcription_token)
             _TICK_CLOSED.reset(closed_token)
@@ -765,6 +820,8 @@ class DriveIndexingService:
             if nudge is not None:
                 await self._observe_nudge(user_id, state, nudge, now, budget, summary)
             state = await self._observe(user_id, state, now, budget, summary, saved)
+            # entry (b) of the note merge (DRIVE_FILE_NOTES_RFC §5.1 b): after observe, before the work-off
+            state = await self._notes_step(user_id, state, budget, summary)
             if state.round_complete:
                 state = await self._work_off(user_id, state, now, budget, summary, models)
             more = await self._has_more(user_id, state, now, upgrade_wrote=summary.get("upgraded", 0) > 0,
@@ -789,9 +846,9 @@ class DriveIndexingService:
                                                     if not k.startswith(("profile_", "passages_", "coverage_"))},
                                                  **job_counts})
             progressed = any(v for k, v in summary.items() if k != "pages")
-            state = replace(models.onto(state), continuation_enqueued_at=now if more else None, last_error=None,
-                            systemic_failures=0,
-                            last_success_at=now if (progressed or not more) else state.last_success_at)
+            state = _notes_onto(replace(models.onto(state), continuation_enqueued_at=now if more else None,
+                                        last_error=None, systemic_failures=0,
+                                        last_success_at=now if (progressed or not more) else state.last_success_at))
             await self._save_state(state)
             transcription = _TICK_TRANSCRIPTION.get()
             self._log(user_id, "continued" if more else "done", summary, budget, models, spend,
@@ -811,7 +868,8 @@ class DriveIndexingService:
         except DriveReauthRequired:
             flagged = await self._drive.flag_reconnect(user_id)
             logger.warning(f"[DriveIndex] refresh rejected for {user_id[:8]} (flag set now: {flagged})")
-            await self._save_state(replace(models.onto(saved.state), last_error=ErrorCategory.RECONNECT_NEEDED))
+            await self._save_state(_notes_onto(replace(models.onto(saved.state),
+                                                       last_error=ErrorCategory.RECONNECT_NEEDED)))
             return TickResult("access_lost", summary, models.cost)
         except (DriveThrottled, LLMRateLimitError, LLMUnavailableError) as exc:
             delay = _backoff_s(state.systemic_failures)
@@ -820,9 +878,9 @@ class DriveIndexingService:
                 delay = retry_after if state.systemic_failures == 0 else max(retry_after, delay)
             logger.warning(f"[DriveIndex] throttled for {user_id[:8]}: {exc}; continuing in {delay}s")
             cont.request(delay)
-            await self._save_state(replace(models.onto(saved.state), last_error=ErrorCategory.THROTTLED,
-                                           continuation_enqueued_at=now,
-                                           systemic_failures=state.systemic_failures + 1))
+            await self._save_state(_notes_onto(replace(models.onto(saved.state), last_error=ErrorCategory.THROTTLED,
+                                                       continuation_enqueued_at=now,
+                                                       systemic_failures=state.systemic_failures + 1)))
             return TickResult("throttled", summary, models.cost)
         except Exception as exc:
             category = ErrorCategory.PROVIDER_ERROR if isinstance(exc, (DriveAccessDenied, ValueError)) \
@@ -830,9 +888,9 @@ class DriveIndexingService:
             delay = _backoff_s(state.systemic_failures)
             logger.error(f"[DriveIndex] tick failed for {user_id[:8]}: {exc}; continuing in {delay}s", exc_info=True)
             cont.request(delay)
-            await self._save_state(replace(models.onto(saved.state), last_error=category,
-                                           continuation_enqueued_at=now,
-                                           systemic_failures=state.systemic_failures + 1))
+            await self._save_state(_notes_onto(replace(models.onto(saved.state), last_error=category,
+                                                       continuation_enqueued_at=now,
+                                                       systemic_failures=state.systemic_failures + 1)))
             return TickResult("error", summary, models.cost)
 
     async def _counters(self, user_id: str, *, marked: Optional[Sequence[DriveIndexRecord]] = None,
@@ -922,6 +980,9 @@ class DriveIndexingService:
                 raise _DriveSwitched()
             state = self._enter_epoch(state, page)
             state = replace(state, drive_id=page.drive_id or state.drive_id, root_item_id=page.root_item_id)
+            notes = _TICK_NOTES.get()
+            if notes is not None and state.drive_id:
+                notes.drive_id = state.drive_id   # the drive the index is of: the one its binding keys name
             await self._apply(user_id, self._ctx(user_id, state, now), list(page.changes), budget, summary)
             state = replace(state, cursor=page.cursor)
             if page.complete:
@@ -1097,6 +1158,7 @@ class DriveIndexingService:
         await self._recheck_gate(user_id)
         for item_id in sorted(collects or ()):
             await self._collect(user_id, item_id, None)
+        writes = await self._lookup_notes(user_id, writes)   # entry (a) of the note merge (DRIVE_FILE_NOTES_RFC §5.1 a)
         ctx = self._enrich_ctx()
         tctx = self._transcript_ctx()
         # every write's plans: the transcript job's (§27.3), then the keys, enrichment jobs and queue fields (§25.4.9)
@@ -1162,6 +1224,305 @@ class DriveIndexingService:
                 logger.info(f"[DriveIndex] the transcript mirror root of {user_id[:8]} is back; root-loss mark cleared")
             mirror.gone_ids.discard(root_id)
 
+    # ------------------------------------------------------------------------------------------
+    # Owner notes: the copy on the record (DRIVE_FILE_NOTES_RFC §5.1, §5.5; plan part B)
+    # ------------------------------------------------------------------------------------------
+
+    async def _notes_count(self, user_id: str, tick: _TickNotes) -> int:
+        """The user's note documents (stubs included), read once per tick: the gate of both entries."""
+        if tick.count is None:
+            tick.count = await self._notes.count(user_id) if self._notes is not None else 0
+        return tick.count
+
+    async def _lookup_notes(self, user_id: str, writes: List[DriveIndexRecord]) -> List[DriveIndexRecord]:
+        """Entry (a) (§5.1 a, review I2): every record this commit creates (`note_lookup_due`, set by the planner's
+        `_build`) looks up the owner's note by its binding key, in the same write: every way a record comes into
+        existence (onboarding after a purge, a recycle-bin restore, a re-listing, an opened folder, a parent created by
+        `_resolve_step`) ends in `_commit`. An `active` note is merged; an `archived` one is re-activated first (§7.2: the
+        same item id on the same drive came back) and merged; a cleared note that had been archived only loses its stamp
+        (core contract §6), so a later undo returns it active. Every flagged record leaves with the flag cleared, found
+        or not. A user without notes pays the tick's one count. A note-store failure never ends the tick or holds the
+        write back: the records are written without their notes and the next save resets the watermark (`rescan`), so
+        the notes step re-merges every note."""
+        if not any(w.note_lookup_due for w in writes):
+            return writes
+        flagged = {w.item_id for w in writes if w.note_lookup_due}
+        cleared = [replace(w, note_lookup_due=False) if w.note_lookup_due else w for w in writes]
+        tick = _TICK_NOTES.get()
+        if self._notes is None or tick is None:
+            return cleared
+        if not tick.drive_id:
+            tick.rescan = True   # no drive id yet (no page seen, no connection): the notes step merges once it is known
+            return cleared
+        drive_id = tick.drive_id
+        try:
+            if not await self._notes_count(user_id, tick):
+                return cleared
+            keys = [record_binding_key(drive_id, item_id) for item_id in sorted(flagged)]
+            found = await self._notes.find_by_match_keys(user_id, keys)
+        except (LeaseLost, _OptedOut):
+            raise
+        except Exception as exc:
+            tick.rescan = True
+            logger.error(f"[DriveIndex] note lookup failed for {user_id[:8]} ({len(flagged)} new records): "
+                         f"{exc_brief(exc)}; written without their notes, the notes step re-reads every note")
+            return cleared
+        by_item: Dict[str, DriveNote] = {}
+        for note in found:   # lowest note id first, as `find_bound`; the key is exact, the parts are checked anyway
+            parts = binding_parts_of(note)
+            if (parts is not None and parts.provider_key == ONEDRIVE_PROVIDER_KEY and parts.drive_id == drive_id
+                    and parts.item_id in flagged):
+                by_item.setdefault(parts.item_id, note)
+        out: List[DriveIndexRecord] = []
+        for rec in cleared:
+            note = by_item.get(rec.item_id)
+            if note is not None:
+                view = note_view(await self._reactivated(user_id, note, tick), drive_id=drive_id)
+                if view is not None:
+                    rec = with_owner_note(rec, view)
+            out.append(rec)
+        return out
+
+    async def _reactivated(self, user_id: str, note: DriveNote, tick: _TickNotes) -> DriveNote:
+        """§7.2 and core contract §6: an archived note, or a cleared one stamped `archived_at`, whose item is indexed
+        again is re-activated (a cleared one stays cleared). The returned copy is the note as now stored (the merge's
+        input); a failed write leaves it as it was (logged; the next record write or the watermark catches up)."""
+        archived = note.state == NoteState.ARCHIVED.value
+        if note.erase_pending or not (archived or (note.state == NoteState.CLEARED.value
+                                                   and note.archived_at is not None)):
+            return note
+        if self._notes is None:
+            return note
+        _fence()
+        try:
+            done = await self._notes.reactivate(user_id, note.note_id, bound_at=self._clock())
+        except Exception as exc:
+            tick.rescan = True
+            logger.error(f"[DriveIndex] note re-activation failed for {user_id[:8]} note={note.note_id}: "
+                         f"{exc_brief(exc)}")
+            return note
+        if not done:
+            return note
+        logger.info(f"[DriveIndex] note re-activated for {user_id[:8]} note={note.note_id} "
+                    f"({'archived' if archived else 'cleared, unstamped'})")
+        return note.model_copy(update={"state": NoteState.ACTIVE.value if archived else note.state,
+                                       "archived_at": None})
+
+    async def _notes_step(self, user_id: str, state: DriveSyncState, budget: _Budget, summary: Dict[str, int], *,
+                          gated: bool = False) -> DriveSyncState:
+        """Entry (b) (§5.1 b): one page of the notes changed since the watermark reconciles the records of their items
+        (merged or cleared), erase stubs are deleted once their records' copies are gone, the watermark (a pair, with
+        its paging flag) advances to the last processed note, and the vector repair and the space pass re-embed up to
+        NOTE_EMBED_PER_TICK note vectors. A `NOTE_MERGE_VERSION` change resets the watermark first (§5.5). A full page
+        asks for a continuation. Starts only inside the start budget; no call starts NOTE_STEP_S after the step began.
+        `gated`: the access gate is closed, no due loop follows, so the changed cards are re-embedded here. A note-store
+        failure skips the rest of the step (logged); an index-store failure ends the tick like any record write."""
+        tick = _TICK_NOTES.get()
+        if self._notes is None or tick is None:
+            return state
+        if tick.rescan:
+            state, tick.rescan = tick.onto(state), False
+        if not budget.time_left() or not tick.drive_id:
+            return state
+        stop = budget.elapsed() + NOTE_STEP_S
+        try:
+            count = await self._notes_count(user_id, tick)
+        except (LeaseLost, _OptedOut):
+            raise
+        except Exception as exc:
+            logger.error(f"[DriveIndex] note count failed for {user_id[:8]}: {exc_brief(exc)}; notes step skipped")
+            return state
+        if not count:
+            # nothing to merge or move: the versions stand; a first note is read from the start of the store
+            return replace(state, notes_merge_version=NOTE_MERGE_VERSION, notes_space=self._space,
+                           notes_space_at=None, notes_space_id="")
+        if state.notes_merge_version != NOTE_MERGE_VERSION:
+            logger.info(f"[DriveIndex] note merge version {state.notes_merge_version} -> {NOTE_MERGE_VERSION} for "
+                        f"{user_id[:8]}: every note is merged again")
+            state = replace(state, notes_applied_at=None, notes_applied_id="", notes_paging=False,
+                            notes_scanned_at=None, notes_merge_version=NOTE_MERGE_VERSION)
+        after, after_id = notes_query_start(state)
+        try:
+            page = await self._notes.changed_since(user_id, after, NOTE_PAGE, after_note_id=after_id)
+        except (LeaseLost, _OptedOut):
+            raise
+        except Exception as exc:
+            logger.error(f"[DriveIndex] notes read failed for {user_id[:8]}: {exc_brief(exc)}; notes step skipped")
+            return state
+        if page:
+            await self._merge_notes(user_id, page, tick.drive_id, summary, gated=gated)
+            last = page[-1]
+            state = replace(state, notes_applied_at=last.updated_at, notes_applied_id=last.note_id)
+        full = len(page) >= NOTE_PAGE
+        state = replace(state, notes_paging=full, notes_scanned_at=None if full else self._clock())
+        tick.more = tick.more or full
+        if budget.elapsed() < stop:
+            state = await self._repair_note_vectors(user_id, state, budget, stop, summary)
+        return state
+
+    async def _merge_notes(self, user_id: str, page: Sequence[DriveNote], drive_id: str, summary: Dict[str, int], *,
+                           gated: bool) -> None:
+        """One page of changed notes onto the records of their items, in one record read and one commit. A note bound
+        to another drive or provider touches nothing here (dormant, D16). A note that is not `active` clears a copy
+        only if the copy is its own (another note's copy of the same item is left to that note). Records that do not
+        exist are skipped (entry a merges them when they appear). Each erase stub is deleted after the commit, once its
+        record's copy is cleared or it has no record on this drive."""
+        if self._notes is None:
+            return
+        per_item: Dict[str, List[Tuple[str, Optional[NoteView]]]] = {}
+        stubs: List[str] = []
+        for note in page:
+            if note.erase_pending:
+                stubs.append(note.note_id)
+            parts = binding_parts_of(note)
+            if parts is None or parts.provider_key != ONEDRIVE_PROVIDER_KEY or parts.drive_id != drive_id:
+                continue
+            per_item.setdefault(parts.item_id, []).append((note.note_id, note_view(note, drive_id=drive_id)))
+        recs = await self._repo.get_records(user_id, sorted(per_item)) if per_item else {}
+        now = self._clock()
+        writes: List[DriveIndexRecord] = []
+        for item_id, entries in per_item.items():
+            rec = recs.get(item_id)
+            if rec is None:
+                continue
+            new = rec
+            for note_id, view in entries:
+                if view is not None:
+                    new = with_owner_note(new, view)
+                elif new.owner_note_id in ("", note_id):
+                    new = with_owner_note(new, None)
+            if new != rec or _note_copy_stale(new):
+                writes.append(_card_rescheduled(new, now))
+        if writes:
+            await self._commit(user_id, writes, set())
+            _inc(summary, "notes_merged", len(writes))
+            if gated:
+                await self._gated_cards(user_id, [w.item_id for w in writes], now, summary)
+        for note_id in stubs:
+            _fence()
+            try:
+                if await self._notes.delete_stub(user_id, note_id):
+                    _inc(summary, "note_stubs_deleted")
+            except Exception as exc:   # the stub stays; the next pass over it deletes it
+                logger.error(f"[DriveIndex] erase stub {note_id} of {user_id[:8]} not deleted: {exc_brief(exc)}")
+
+    async def _gated_cards(self, user_id: str, item_ids: Sequence[str], now: datetime,
+                           summary: Dict[str, int]) -> None:
+        """The access gate is closed (no due loop runs): the cards whose text a merge changed are embedded at once, so a
+        cleared or erased note is gone from the card vector too (core review §5). A card the embedder cannot make now
+        is dropped instead (it comes back as card work when the gate opens): no vector of the old text survives."""
+        fresh = await self._repo.get_records(user_id, list(item_ids))
+        due = [r for r in fresh.values() if card_eligible(r) and r.card_hash != card_hash(r)]
+        if not due:
+            return
+        try:
+            await self._embed_cards(user_id, due, now, summary)
+        except (LeaseLost, _OptedOut):
+            raise
+        except Exception as exc:
+            left = await self._repo.get_records(user_id, [r.item_id for r in due])
+            dropped = [without_card(r, now) for r in left.values() if r.has_card and r.card_hash != card_hash(r)]
+            if dropped:
+                await self._commit(user_id, dropped, set())
+            logger.warning(f"[DriveIndex] card embedding failed for {user_id[:8]} behind the closed gate "
+                           f"({exc_brief(exc)}); {len(dropped)} stale cards dropped until it opens")
+
+    async def _repair_note_vectors(self, user_id: str, state: DriveSyncState, budget: _Budget, stop: float,
+                                   summary: Dict[str, int]) -> DriveSyncState:
+        """§5.1 b step 5: note vectors whose embedding failed at write (`embedding_pending`) are embedded, then, while
+        the user's note vectors are in another embedding space than the service's (`notes_space`), the space pass pages
+        through every note from its own cursor and moves them; both share NOTE_EMBED_PER_TICK. `set_vector` stores only
+        over the version that was embedded (a newer write keeps its own). The pass stops at a note whose embedding
+        failed, so none is left behind; when it reaches the end, `notes_space` is stamped."""
+        if self._notes is None:
+            return state
+        try:
+            pending = [n for n in await self._notes.embedding_pending(user_id, NOTE_EMBED_PER_TICK)
+                       if not n.erase_pending and not n.content.is_empty]
+            await self._embed_notes(user_id, pending, summary)
+            left = NOTE_EMBED_PER_TICK - len(pending)
+            if state.notes_space == self._space or left <= 0 or budget.elapsed() >= stop:
+                return state
+            page = await self._notes.changed_since(user_id, state.notes_space_at, NOTE_PAGE,
+                                                   after_note_id=state.notes_space_id)
+            todo: List[Tuple[int, DriveNote]] = []
+            end = len(page)   # the notes before `end` are done once their embeddings are stored
+            for i, note in enumerate(page):
+                if (not note.erase_pending and note.state != NoteState.CLEARED.value and not note.content.is_empty
+                        and note.embedding_space != self._space):
+                    if len(todo) >= left:
+                        end = i
+                        break
+                    todo.append((i, note))
+            moved = await self._embed_notes(user_id, [n for _, n in todo], summary)
+        except (LeaseLost, _OptedOut):
+            raise
+        except Exception as exc:
+            logger.error(f"[DriveIndex] note vector repair failed for {user_id[:8]}: {exc_brief(exc)}")
+            return state
+        if moved < len(todo):
+            end = todo[moved][0]   # an embedding failed: the cursor stops before that note, so it is never skipped
+        if end == len(page) and len(page) < NOTE_PAGE:
+            logger.info(f"[DriveIndex] note vectors of {user_id[:8]} are in {self._space}")
+            return replace(state, notes_space=self._space, notes_space_at=None, notes_space_id="")
+        if end == 0:
+            return state
+        last = page[end - 1]
+        return replace(state, notes_space_at=last.updated_at, notes_space_id=last.note_id)
+
+    async def _embed_notes(self, user_id: str, notes: Sequence[DriveNote], summary: Dict[str, int]) -> int:
+        """Embed each note's text (`note_embedding_text`, as the main service does) and store it under the version
+        that was embedded. Returns how many of `notes`, in order, were handled before the first embedding failure (a
+        stored or refused `set_vector` both count: a newer version keeps its own vector)."""
+        if self._notes is None:
+            return 0
+        handled = 0
+        for note in notes:
+            name = note.snapshot.name if note.snapshot is not None else ""
+            try:
+                vector = await self._embeddings.get_embedding(note_embedding_text(note.content, name),
+                                                              "RETRIEVAL_DOCUMENT")
+            except Exception as exc:
+                logger.warning(f"[DriveIndex] note embedding failed for {user_id[:8]} note={note.note_id}: "
+                               f"{exc_brief(exc)}; tried again next tick")
+                return handled
+            _fence()
+            if await self._notes.set_vector(user_id, note.note_id, [float(x) for x in vector], self._space,
+                                            expected_version=note.current_version):
+                _inc(summary, "note_vectors")
+            handled += 1
+        return handled
+
+    async def _notes_gated(self, user_id: str, state: DriveSyncState, connection: Optional[DriveConnection],
+                           cont: _Continuation) -> None:
+        """The notes step of a tick whose access gate is closed (the access-loss grace, the D30 rebuild gate; core review
+        §5): no other work runs, but the owner's clear, undo and erase must still reach the record copy, its keys and the
+        card vector (`gated`: cards re-embedded here). Its own start budget; the watermark is saved when it moved; a full
+        page asks for a continuation. Nothing here ends the tick but the lease."""
+        if self._notes is None:
+            return
+        budget = _Budget(started=self._monotonic(), monotonic=self._monotonic)
+        tick = _TickNotes(drive_id=(connection.drive_id if connection is not None else "") or state.drive_id)
+        token = _TICK_NOTES.set(tick)
+        summary: Dict[str, int] = {}
+        try:
+            after = tick.onto(await self._notes_step(user_id, state, budget, summary, gated=True))
+            if after != state:
+                await self._save_state(after)
+            if tick.more:
+                cont.request()
+            if summary:
+                logger.info(f"[DriveIndex] notes behind the closed gate for {user_id[:8]}: {summary}")
+        except LeaseLost:
+            raise
+        except _OptedOut:
+            logger.info(f"[DriveIndex] indexing turned off during the notes step of {user_id[:8]}; dropped")
+        except Exception as exc:
+            logger.error(f"[DriveIndex] notes step behind the closed gate failed for {user_id[:8]}: {exc_brief(exc)}",
+                         exc_info=True)
+        finally:
+            _TICK_NOTES.reset(token)
+
     # ------------------------------------------------------------------------------------------
     # Work off (only after a complete round, RFC Δ15)
     # ------------------------------------------------------------------------------------------
@@ -3864,6 +4225,9 @@ class DriveIndexingService:
     async def _has_more(self, user_id: str, state: DriveSyncState, now: datetime, *,
                         upgrade_wrote: bool = False, models: Optional[_Models] = None,
                         idle: Optional[Set[str]] = None) -> bool:
+        notes = _TICK_NOTES.get()
+        if notes is not None and notes.more:
+            return True   # the notes step left a full page (DRIVE_FILE_NOTES_RFC §5.1 b step 4)
         if not state.round_complete or state.epoch_phase is EpochPhase.SWEEPING:
             return True
         if models is not None and await self._enrichment_waiting(user_id, now, models, idle):
@@ -4198,6 +4562,24 @@ def _card_rescheduled(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
     return rec
 
 
+def _note_copy_stale(rec: DriveIndexRecord) -> bool:
+    """A noted record whose derived fields do not show its copy of the note: its about keys are not the keyer's over
+    it, or its card is not the card of its text and nothing has it due (a record written while the merge code was
+    rolled back keeps the copy but loses its keys and its card segment). A re-merge rewrites it (§5.5)."""
+    if not rec.owner_note_id:
+        return False
+    if about_keys_capped(rec)[0] != rec.about_keys:
+        return True
+    return card_eligible(rec) and rec.has_card and rec.card_hash != card_hash(rec) and rec.due_at is None
+
+
+def _notes_onto(state: DriveSyncState) -> DriveSyncState:
+    """The tick's notes accumulator folded into a sync-state save (DRIVE_FILE_NOTES_RFC §3.4): every save of the tick
+    that writes its final or error state carries it."""
+    notes = _TICK_NOTES.get()
+    return notes.onto(state) if notes is not None else state
+
+
 def _fence() -> None:
     """No write once this tick's lease is about to end (§7.1): a tick that outlives it stops instead."""
     lease = _TICK_LEASE.get()
```

**New tests.** `tests/unit/services/test_drive_indexing_notes.py` (commit `8eb0ad7e`), on the real service with the
in-memory index and note repositories:
- entry (a): onboarding merges in the creating write (card text, about keys, the card embedding carries the note, every
  flag cleared); a restore re-activates the archived note (`bound_by = restore`) and merges it; a cleared archived note
  loses its stamp and stays cleared; **a parent folder created by `_resolve_step` gets its note** (the folder never in
  delta, only its child's parent lookup creates it, I2); a note on another drive is never merged (D15);
- entry (b): merges then clears (the card re-embedded both times, the note keys gone); the watermark is the last note's
  pair, a new pass starts a skew before the pass end, an old one strictly after the pair; a full page continues on the
  pair cursor (`NOTE_PAGE` patched to 2, five notes); a `None` watermark re-reads every note in pages and restores lost
  copies; **a `NOTE_MERGE_VERSION` bump re-merges the noted record and leaves the plain one untouched** (I1); a record
  rewritten by rolled-back code (keys and card without the note) is repaired by the roll-forward bump; **the count gate
  costs exactly one read for a user without notes**; a nudge after a write merges in one tick; an erase stub clears the
  copy and is deleted; a cleared note never clears another note's copy; a note-store failure skips the step, the tick
  ends `done`, the watermark is reset, the next tick merges; a purge leaves the note store untouched and onboarding
  merges again; a re-key (`KEYS_VERSION`) keeps the note keys; without the store no notes work runs;
- repair: a pending vector repaired with the embedded version; **a newer version written during the embedding keeps
  its own vector**; the space pass moves every note at `NOTE_EMBED_PER_TICK` and stamps `notes_space`; it never skips a
  note whose embedding failed;
- closed gate: **a cleared note leaves the record copy, its keys and the card vector behind a closed gate**,
  parametrized over `access_lost` (needs-reconnect) and `awaiting_migration` (D30); behind a closed gate a card the
  embedder refuses is dropped (no vector of the old text left);
- lease and budget: the lease clock stops a re-activation (the tick ends `lease_lost`, the note stays archived); the
  deadline sum (§1.2); the notes step does not start past the start budget, and runs on the next tick.

**Expected-risk existing tests.** None beyond Task 1's (every `test_drive_indexing_*`, the mirror tick and the deadline
sum test `test_drive_indexing_mirror_tick.py::test_deadline_sum_includes_the_reserve` passed unchanged).

**Done.** New file green; `pytest tests/unit/services -k drive_indexing -n 4` green. **Model:** Opus (the tick's
ordering, lease and error classes are judgment; the hunks are verified, review them against §5.1 and §1.1).

### Task 7: the note document's keys on every owner write

**Goal.** §3.1: `DriveNoteService.set` (both ref kinds) and `undo` store `note_store_keys(content, name)`; a clear stores
none (unchanged: `note_after_write` empties them). Core delta B2 (behaviour only; no signature changes).

**File.** Modify `src/services/drive_note_service.py`.

```diff
diff --git a/src/services/drive_note_service.py b/src/services/drive_note_service.py
index 11409b8d..b23cef87 100644
--- a/src/services/drive_note_service.py
+++ b/src/services/drive_note_service.py
@@ -47,6 +47,7 @@ from ..domain.drive_note import (
     note_after_write, note_content, note_embedding_text, note_word_count, parse_note_target, ref_key, searchability,
     snapshot_of, state_after, undo_source,
 )
+from ..domain.drive_note_merge import note_store_keys
 from ..domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_key, tick_payload
 from ..domain.prompt_v3.security import TrustZone
 from ..domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNotConnectedError, make_drive_ref
@@ -162,9 +163,10 @@ class DriveNoteService:
                                         bound_by=provenance.origin))
             version = next_version(expected, content, NoteAction.SET, provenance, now,
                                    not_before=await self._valid_from_of(user_id, existing))
+            keys, keys_version = note_store_keys(content, snapshot.name)   # §3.1: the deleted-files search's keys (S2)
             written = note_after_write(base, version, NoteState.ACTIVE, provenance, now, vector=vector,
                                        embedding_space=self._space, embedding_pending=pending, binding=binding,
-                                       snapshot=snapshot)
+                                       snapshot=snapshot, keys=keys, keys_version=keys_version)
             try:
                 await self._notes.write_version(written, version, expected_version=expected)
             except DriveNoteConflict:
@@ -207,9 +209,10 @@ class DriveNoteService:
             now = self._clock()
             version = next_version(note.current_version, content, NoteAction.SET, provenance, now,
                                    not_before=await self._valid_from_of(user_id, note))
+            keys, keys_version = note_store_keys(content, name)
             written = note_after_write(note.model_copy(update={"account_id": note.account_id or account_id}), version,
                                        state, provenance, now, vector=vector, embedding_space=self._space,
-                                       embedding_pending=pending)
+                                       embedding_pending=pending, keys=keys, keys_version=keys_version)
             try:
                 await self._notes.write_version(written, version, expected_version=note.current_version)
             except DriveNoteConflict:
@@ -297,9 +300,10 @@ class DriveNoteService:
             version = next_version(note.current_version, content, NoteAction.RESTORE, provenance, now,
                                    restored_from=source_v,
                                    not_before=current.valid_from if current is not None else None)
+            keys, keys_version = note_store_keys(content, name)   # an empty restore (a cleared note) has none
             written = note_after_write(note.model_copy(update={"account_id": note.account_id or account_id}), version,
                                        state, provenance, now, vector=vector, embedding_space=self._space,
-                                       embedding_pending=pending)
+                                       embedding_pending=pending, keys=keys, keys_version=keys_version)
             try:
                 await self._notes.write_version(written, version, expected_version=note.current_version)
             except DriveNoteConflict:
```

**New tests.** `tests/unit/services/test_drive_note_service_keys.py` (commit `65060ba1`; it reuses the core test's `Env`):
set stores the note keys and the name keys with `keys_version` "4.1"; an edit by `drive_note:` ref re-keys; clear
empties, undo restores.

**Expected-risk.** None (the core's 131 tests passed). **Model:** Sonnet.

### Task 8: the open header (`FileManagementAgent`)

**Goal.** §6.2, §6.3, D20: `open_file_from_drive` reads the store's current note by binding on the connected drive
(`drive_port.connection` → `DriveNoteService.note_for_item`), renders it with `owner_note_line`, and shows it inside the
wrapper: after the label on a text open, in its own wrapper after a transcript, an attachment or a sent video, inside
the link body for `original`. No line without a note, the service or a connection, and none on a failed lookup (logged;
the open goes on). Listings unchanged.

**File.** Modify `src/agents/file_management_agent.py`.

```diff
diff --git a/src/agents/file_management_agent.py b/src/agents/file_management_agent.py
index 29ecf02d..a77405cd 100644
--- a/src/agents/file_management_agent.py
+++ b/src/agents/file_management_agent.py
@@ -22,6 +22,9 @@ Mutating intents refuse a ref from the other store and run shielded from cancell
 delete and replace post a receipt via notify_raw (USER_DRIVE_RFC §4.4, §4.10, §4.13).
 File text and search results reach the model only inside the escaped, nonce-tagged wrapper
 (USER_DRIVE_INDEXING_RFC §10.4, D2).
+open_file_from_drive shows the owner's current note on the file (DRIVE_FILE_NOTES_RFC §6.2, §6.3), read from the note
+store by binding, inside the wrapper, so Smart sees what a new note would replace; list_files_in_drive never shows it
+(D20).
 """
 
 import asyncio
@@ -36,6 +39,7 @@ from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Set
 import aiofiles
 
 from ..agents.base_agent import BaseAgent
+from ..domain.drive_card import owner_note_line
 from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus
 from ..domain.drive_search import (
     REFERENCE_REMINDER, REFERENCE_REMINDER_ATTACHED, render_results, request_from_fields, require_search_terms,
@@ -84,6 +88,8 @@ if TYPE_CHECKING:
     from ..services.user_drive_service import UserDriveService
     from ..services.file_conversion_service import FileConversionService
     from ..services.user_notification_service import UserNotificationService
+    from ..services.drive_note_service import DriveNoteService
+    from ..ports.user_drive_port import UserDrivePort
 
 
 def _flag(value: Any) -> bool:
@@ -136,8 +142,14 @@ class FileManagementAgent(BaseAgent):
         localization: Optional["LocalizationService"] = None,
         language_service: Optional["LanguageServicePort"] = None,
         drive_search: Optional["DriveSearchService"] = None,
+        drive_notes: Optional["DriveNoteService"] = None,
+        drive_port: Optional["UserDrivePort"] = None,
     ) -> None:
+        """`drive_notes` and `drive_port` give the open header its note (DRIVE_FILE_NOTES_RFC §6.2): the note service
+        reads by binding, the port gives the connected drive's id for it. Without either, no note line."""
         super().__init__(config)
+        self._drive_notes = drive_notes
+        self._drive_port = drive_port
         self._drive = drive_service
         self._drive_search = drive_search
         self._localization = localization
@@ -698,6 +710,32 @@ class FileManagementAgent(BaseAgent):
             return None
         return text or None
 
+    async def _note_line_of(self, user_id: str, item_id: str) -> str:
+        """The owner's note on the item as a search card renders it (DRIVE_FILE_NOTES_RFC §6.2): the store's current
+        note, by binding on the connected drive (one connection read, one note read). "" without one, without the note
+        service, or on any failure: the open goes on without the line."""
+        if self._drive_notes is None or self._drive_port is None:
+            return ""
+        try:
+            connection = await self._drive_port.connection(user_id)
+            if not connection.connected or not connection.drive_id:
+                return ""
+            note = await self._drive_notes.note_for_item(user_id, connection.drive_id, item_id)
+        except Exception as exc:
+            logger.warning("FileManagementAgent: note lookup failed: %s", exc_brief(exc))
+            return ""
+        if note is None:
+            return ""
+        return owner_note_line(note.text, note.doc_type, tuple(note.life_areas))
+
+    @staticmethod
+    def _with_note(text: str, item: Any, line: str) -> str:
+        """`text` followed by the note line in its own wrapper (the note is data, never instructions, §6.3)."""
+        if not line:
+            return text
+        return text + "\n" + wrap_untrusted(f"{drive_label(item)}\n{line}", nonce=secrets.token_hex(8), ref=item.ref,
+                                            path=item.path)
+
     async def _open_drive_item(self, message: AgentMessage, ref: str, user_id: str,
                                payload: Optional[dict] = None) -> AgentResponse:
         item = await self._conversion_service.get_drive_item(ref, user_id)
@@ -706,6 +744,7 @@ class FileManagementAgent(BaseAgent):
         archived = await self._archived_transcript_of(user_id, ref, item)
         if archived is not None:
             return self._ok(message, archived, [item])
+        note = await self._note_line_of(user_id, item.item_id)
         mime = item.mime_type or (mimetypes.guess_type(item.name)[0] or "application/octet-stream")
         # Vision caps and unsupported formats are decided on metadata, before any download (§4.6).
         if mime.startswith("image/") and mime not in VISION_IMAGE_MIME_TYPES:
@@ -718,13 +757,14 @@ class FileManagementAgent(BaseAgent):
         if recording and (payload or {}).get("original") is True:
             # The recording itself comes as a drive link, never as bytes into chat (§27.11, Δ121).
             link = item.web_url or "(no link available)"
-            body = wrap_untrusted(f"{drive_label(item)}\nLink to the original recording: {link}",
+            noted = f"\n{note}" if note else ""
+            body = wrap_untrusted(f"{drive_label(item)}{noted}\nLink to the original recording: {link}",
                                   nonce=secrets.token_hex(8), ref=item.ref, path=item.path)
             return self._ok(message, f"{REFERENCE_REMINDER}\n{body}", [item])
         if recording and self._drive_search is not None:
             transcript = await self._transcript_of(user_id, item)
             if transcript is not None:
-                return self._ok(message, transcript, [item])
+                return self._ok(message, self._with_note(transcript, item, note), [item])
         try:
             if vision_cap is not None:
                 if item.size_bytes > vision_cap:
@@ -735,8 +775,8 @@ class FileManagementAgent(BaseAgent):
                 async with aiofiles.open(tmp_path, "wb") as f:
                     await f.write(data)
                 # The model reads an attached PDF or image natively: the reminder travels with it (D2).
-                return self._ok(message, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} is "
-                                         f"attached. You can see and analyse it directly.",
+                return self._ok(message, self._with_note(f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} is "
+                                                         f"attached. You can see and analyse it directly.", item, note),
                                 [item], metadata={"file_data": {"path": tmp_path, "mime_type": mime}})
             if mime.startswith("video/"):
                 data = await self._conversion_service.resolve_bytes(ref, user_id)
@@ -748,14 +788,15 @@ class FileManagementAgent(BaseAgent):
                     channel_id_override=message.context.get("origin_channel_id"),
                     platform_override=message.context.get("origin_platform"),
                 )
-                return self._ok(message, f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} has "
-                                         f"been sent to the user as a file.", [item])
+                return self._ok(message, self._with_note(f"{REFERENCE_REMINDER_ATTACHED}\n{drive_label(item)} has "
+                                                         f"been sent to the user as a file.", item, note), [item])
         except DriveFileTooLargeError as e:
             return self._ok(message, f"'{item.path}' is {format_size(item.size_bytes)}; files above "
                                      f"{format_size(e.limit_bytes)} cannot be opened yet.", [item])
         content = await self._conversion_service.resolve_content(ref, user_id)
         # File text is untrusted: it reaches the model only inside the wrapper (RFC §10.4, RFC Δ11).
-        wrapped = wrap_untrusted(f"{drive_label(item)}\n{content}", nonce=secrets.token_hex(8),
+        noted = f"\n{note}" if note else ""   # the header: label, the owner's note (§6.2), then the file's text
+        wrapped = wrap_untrusted(f"{drive_label(item)}{noted}\n{content}", nonce=secrets.token_hex(8),
                                  ref=item.ref, path=item.path)
         return self._ok(message, f"{REFERENCE_REMINDER}\n{wrapped}", [item])
```

**New tests.** `tests/unit/agents/test_file_management_open_note_header.py` (commit `98ca4eb1`): the text open (label,
note line, file text, one read with the connected drive id); the transcript's wrapped note block; the original link;
no line and the open goes on (no note, no service, lookup fails, disconnected); a tag look-alike in the note escaped; a
listing never reads a note.

**Expected-risk.** None. **Model:** Sonnet.

### Task 9: composition (indexer and main service)

**Goal.** §3.6: `DRIVE_NOTES_ENABLED` (optional knob, `os.getenv` in `indexer_main`, default on) builds the note store
for the tick, or none; the open header's readers reach the agent through `UserAgentFactory` (`drive_note_service`
from part A's container entry, `user_drive` from the container's port).

**Files.** Modify `src/composition/drive_indexing_setup.py`, `indexer_main.py`, `src/composition/user_agent_factory.py`,
`src/composition/service_container.py`.

```diff
diff --git a/indexer_main.py b/indexer_main.py
index 2f4a52cc..45c0a720 100644
--- a/indexer_main.py
+++ b/indexer_main.py
@@ -72,7 +72,8 @@ from src.adapters.microsoft import OneDriveAdapter
 from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
 from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor
 from src.composition.drive_indexing_setup import (
-    build_drive_index_repository, build_drive_mirror_repository, build_drive_task_queue, build_place_lookup,
+    build_drive_index_repository, build_drive_mirror_repository, build_drive_note_repository, build_drive_task_queue,
+    build_place_lookup, drive_notes_enabled,
 )
 from src.composition.drive_model_setup import build_drive_mirror, build_drive_models, build_drive_transcription
 from src.composition.memory_telemetry_setup import build_memory_telemetry
@@ -155,6 +156,10 @@ def build_app() -> Quart:
         enabled=transcribe_on and transcripts is not None, repo=index_repo, mirror_repo=mirror_repo, drive=drive,
         users=users, account_repo=account_repo, profile_repo=profile_repo, assembly_service=assembly_service,
     )
+    # The owner's notes (DRIVE_FILE_NOTES_RFC §5.1): the tick merges them into the records; DRIVE_NOTES_ENABLED (an
+    # optional kill switch, os.getenv, default on) off builds no store, so the tick does no notes work.
+    notes = build_drive_note_repository(db, env_config,
+                                        enabled=drive_notes_enabled(os.environ.get("DRIVE_NOTES_ENABLED")))
     indexing = DriveIndexingService(
         drive=drive,
         repo=index_repo,
@@ -177,6 +182,7 @@ def build_app() -> Quart:
         mirror=mirror.mirror,
         localization=mirror.localization,
         language=mirror.language,
+        notes=notes,
     )
 
     app.register_blueprint(create_indexer_worker_blueprint(indexing, gate))
diff --git a/src/composition/drive_indexing_setup.py b/src/composition/drive_indexing_setup.py
index df7d5140..ab377c9b 100644
--- a/src/composition/drive_indexing_setup.py
+++ b/src/composition/drive_indexing_setup.py
@@ -16,6 +16,7 @@ from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Mapping, Optio
 
 from ..adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
 from ..adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
+from ..adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
 from ..adapters.gcp_task_queue import GcpTaskQueue
 from ..adapters.geo_place_table import load_place_rows
 from ..adapters.microsoft import ONEDRIVE_PROVIDER
@@ -25,6 +26,7 @@ from ..domain.place_lookup import PlaceIndex
 from ..ports.alert_sink import AlertSinkPort
 from ..ports.drive_index_repository import DriveIndexRepository
 from ..ports.drive_mirror_repository import DriveMirrorRepository
+from ..ports.drive_note_repository import DriveNoteRepository
 from ..ports.oauth_credentials_port import OAuthCredentialsPort
 from ..ports.task_queue import TaskQueue
 from ..ports.user_drive_port import UserDrivePort
@@ -55,6 +57,19 @@ def build_drive_mirror_repository(db_client: Any, env_config: EnvironmentConfig)
     return FirestoreDriveMirrorRepository(db_client, env_config)
 
 
+def drive_notes_enabled(value: Any) -> bool:
+    """DRIVE_NOTES_ENABLED (DRIVE_FILE_NOTES_RFC §3.6): an optional kill switch read with `os.getenv` by the caller, on
+    unless set to 0/off/false/no. Off: no lookup, no notes step, no lifecycle write in the indexer; merged copies stay
+    until the next merge."""
+    return str(value or "on").strip().lower() not in ("0", "off", "false", "no")
+
+
+def build_drive_note_repository(db_client: Any, env_config: EnvironmentConfig, *,
+                                enabled: bool = True) -> Optional[DriveNoteRepository]:
+    """The owner-note store `{prefix}drive_notes` (DRIVE_FILE_NOTES_RFC §3.1), or None with the kill switch off."""
+    return FirestoreDriveNoteRepository(db_client, env_config) if enabled else None
+
+
 def build_drive_task_queue(config: Mapping[str, Any], env_config: EnvironmentConfig) -> Optional[TaskQueue]:
     """The drive queue, targeting the indexer service; None when `DRIVE_INDEXER_URL` is unset."""
     indexer_url = (os.getenv("DRIVE_INDEXER_URL") or "").strip().rstrip("/")
diff --git a/src/composition/service_container.py b/src/composition/service_container.py
index 873f374c..eab023b2 100644
--- a/src/composition/service_container.py
+++ b/src/composition/service_container.py
@@ -450,6 +450,7 @@ class ServiceContainer:
             "file_storage": self.file_storage,
             "user_drive_service": self.user_drive_service,
             "drive_search_service": self.drive_search_service,
+            "user_drive": self.user_drive,   # the open header's drive id (DRIVE_FILE_NOTES_RFC §6.2)
             "prompt_content_store": self.prompt_content_store,
             "skill_service": self.skill_service,
             "memory_fact_service": self.memory_fact_service,
diff --git a/src/composition/user_agent_factory.py b/src/composition/user_agent_factory.py
index 854b1669..90e5fae4 100644
--- a/src/composition/user_agent_factory.py
+++ b/src/composition/user_agent_factory.py
@@ -118,6 +118,8 @@ if TYPE_CHECKING:
     from ..services.workspace_service import WorkspaceService
     from ..services.user_drive_service import UserDriveService
     from ..services.drive_search_service import DriveSearchService
+    from ..services.drive_note_service import DriveNoteService
+    from ..ports.user_drive_port import UserDrivePort
     from ..services.localization_service import LocalizationService
     from ..ports.language_service_port import LanguageServicePort
 
@@ -196,6 +198,10 @@ class UserAgentFactory(AgentFactoryPort):
         language_service: Optional["LanguageServicePort"] = None,
         drive_search_service: Optional["DriveSearchService"] = None,
         alert_sink: Optional[AlertSinkPort] = None,
+        # DRIVE_FILE_NOTES_RFC: the note service (part A builds it in the container and gives it to Smart; part B reads
+        # it for the open header) and the drive port (the connected drive's id for that read)
+        drive_note_service: Optional["DriveNoteService"] = None,
+        user_drive: Optional["UserDrivePort"] = None,
     ) -> None:
         # Resolved once: a bad SANDBOX_RENDER stops the start, and the sandbox runner is shared by all users.
         self._docx_runner_factory = docx_runner_factory()
@@ -242,6 +248,8 @@ class UserAgentFactory(AgentFactoryPort):
         self.long_turn_registry = long_turn_registry
         self.user_drive_service = user_drive_service
         self.drive_search_service = drive_search_service
+        self.drive_note_service = drive_note_service
+        self.user_drive = user_drive
         self.alert_sink = alert_sink
         self.localization = localization
         self.language_service = language_service
@@ -871,6 +879,8 @@ class UserAgentFactory(AgentFactoryPort):
             localization=self.localization,
             language_service=self.language_service,
             drive_search=self.drive_search_service,
+            drive_notes=self.drive_note_service,   # the open header's note (DRIVE_FILE_NOTES_RFC §6.2)
+            drive_port=self.user_drive,
         )
 
     def _build_domain_researcher(self, user_id: str, ctx: _UserContext) -> DomainResearcherAgent:
```

If part A already added `drive_note_service` to `UserAgentFactory.__init__`, apply only the `user_drive` half of that
hunk; `main.py` needs no change (`**container.agent_services()`), and A's container entry `"drive_note_service"`
completes the wiring.

**New tests.** `tests/unit/test_indexer_main_drive_notes.py` (commit `98ca4eb1`): the store is wired by default; the
kill switch (`off`, `0`, `false`, `NO`) builds none; `drive_notes_enabled` values; the factory hands the agent both
readers.

**Expected-risk.** None (`tests/unit/test_indexer_main.py` and the architecture tests passed). **Model:** Sonnet.

### Task 10: the `drive-search` skill text

**Goal.** §6.2: the reading line for `noted by the owner:` and the accuracy exception. The "Deleted files" section is
T2's and already says what §6.2 asks; unchanged.

**File.** Modify `src/skills/smart/drive-search/SKILL.md`.

```diff
diff --git a/src/skills/smart/drive-search/SKILL.md b/src/skills/smart/drive-search/SKILL.md
index a6d70f63..f1344d57 100644
--- a/src/skills/smart/drive-search/SKILL.md
+++ b/src/skills/smart/drive-search/SKILL.md
@@ -28,13 +28,14 @@ Put a time span in the date fields and a file format in `drive_kind`, not in the
 
 Drive results say how each file matched. 'About' and 'mentioned in' are exact: the file's own name, details, place or text holds the words, in their spellings and word forms. 'By translation' and 'labelled' are the index's automatic words — passage keywords with their source quote, type, topic and life-area words — and may be wrong or incomplete. 'Related by meaning only' lines are guesses. Other spellings and word forms were searched; other languages only where the index wrote them. The coverage line says what the search could not see. When a word is held by no file, the result counts the files that match the other words, so you can tell whether the word was phrasing or the subject.
 
+- A `noted by the owner:` line is what the user told you about the file. It outranks the card's own description and type, and it is not part of `card accuracy`.
 - Trust the exact tiers. When the answer depends on an automatic one, check its quoted source or open the file.
 - Mention "related by meaning only" lines only when they help, never as findings.
 - "Coverage: complete" makes a not-found reliable.
 
 ## Card accuracy
 
-A described file's result carries `card accuracy: N%`. 100% — everything in this card is confirmed by the file's own text; you can trust the card fully. The lower, the more likely the card's values contain errors.
+A described file's result carries `card accuracy: N%`. 100% — everything in this card *except the owner's note* is confirmed by the file's own text; you can trust the card fully. The lower, the more likely the card's values contain errors.
 
 - "Find", "which files", "do I have": answer from the cards at any accuracy. Never open files for this.
 - A question about a value in a file — a number, a date, an amount, an expiry: answer from the card when its accuracy is 90% or more. Below that, open the file and check the value before answering.
```

**Tests.** The system-skill bundle tests and `tests/unit/firestore_utils/test_protocol_drive_guidance*.py` passed
unchanged.

**Expected-risk existing test** (failed in the worktree's `make check`, for the reviewer):
- `tests/unit/skills/test_drive_search_skill_card_accuracy.py::test_the_section_is_there_with_its_rule_lines` — it pins
  the sentence "100% — everything in this card is confirmed by the file's own text; …"; RFC §6.2 changes it on purpose
  to "100% — everything in this card *except the owner's note* is confirmed by the file's own text; …" (the note is not
  part of the accuracy score, §5.6). The reviewer updates the pinned line.

**Model:** Sonnet.

### Docs tasks (after Task 10; English; no secrets)

- **D1 — `docs/05_building_blocks/user_drive/README.md` §4.24 "Owner notes on files" (B's part; A and C add theirs).**
  Write: the record carries a copy of the note (the seven fields), rebuilt from `{prefix}drive_notes` by two entries
  through `_commit` (created records; the notes step after observe, paged on a watermark pair); the card segment after
  `name | kind`, no `CARD_VERSION` bump, the profile type dropped when the owner gave one; the note's keys under
  `AboutField.OWNER_NOTE` in existing families (no `KEYS_VERSION` bump), classifiers first, cap 320; `l:type` /
  `l:domain` overrides; the search card's own `noted by the owner:` line, the open header; `NOTE_MERGE_VERSION`
  outside `CURRENT_VERSIONS`; the closed-gate step; `DRIVE_NOTES_ENABLED`. Add the `OWNER_NOTE` rows to §3.1's key table
  ("`e:` … or the owner's note") and the sync-state fields to §3's table.
- **D2 — `CLAUDE.md`, the User Drive paragraph** (one clause, merged with A's and C's line): "the tick copies the owner's
  notes into records (`drive_note_merge`; card segment and `e:` keys under `AboutField.OWNER_NOTE`, no version bump;
  `DRIVE_NOTES_ENABLED`)".
- **D3 — `docs/04_solution_strategy/decisions/drive_file_notes.md`** (shared record; B's section, 30-50 lines): the
  watermark pair and the pass-end skew (why not `updated_at − 60 s` alone: an erase-all inside one minute re-read every
  tick); the closed-gate step and why cards are re-embedded there; `NOTE_STEP_S` and the deadline sum; the
  re-merge of records whose derived fields lost the copy; **the rollback rule** (Default B12); the open header through
  the port, not a core change; `NOTE_KEYS_MAX`'s home.
- **D4 — `docs/07_deployment/README.md`.** No new index on the drive-index collections (B reads the note store through
  indexes 1, 2 and 5 that A's task declares). The indexer reads `DRIVE_NOTES_ENABLED` (default on; `off` stops lookups,
  the notes step and lifecycle writes). **Rollback:** the kill switch first; a code rollback of the indexer is followed by
  a roll-forward that ships `NOTE_MERGE_VERSION + 1` (Default B12), so every noted record is rewritten.

---

## 4. Defaults chosen

- Default B1: `effective_doc_type` / `effective_life_areas` live in `drive_key_sources` and are re-exported by
  `drive_note_merge` — the keyer needs them and `drive_note_merge` imports `drive_note`, which imports the keyer.
- Default B2: the record field is `note_lookup_due` as the RFC names it, but it is always `False` once written (cleared
  in `_commit` found or not, RFC §5.1 a). A failed lookup is repaired by a watermark reset (`_TickNotes.rescan`), not by
  a persisted flag — a persisted flag would need a query on the drive-index collection (RFC §3.5: no index changes there).
- Default B3: the sync state gains `notes_applied_id`, `notes_paging`, `notes_scanned_at` (the pair cursor the core
  review requires, and the pass end) and `notes_space_at` / `notes_space_id` (the space pass's own cursor), beside the
  RFC's `notes_applied_at`, `notes_merge_version`, `notes_space`.
- Default B4: a new pass starts `NOTE_WATERMARK_SKEW_S` before the **earlier of the watermark and the end of the last
  pass**, else strictly after the pair (`notes_query_start`) — with the RFC's "watermark − 60 s" alone, more than a page
  of notes inside one minute (an erase-all) would be re-read on every tick, forever.
- Default B5: the notes step runs on every tick right after observe, also when the round is not complete, only inside
  the start budget, and never asks for a continuation when it was skipped for the budget (the tick that ate the budget
  already continues).
- Default B6: the space pass compares `notes_space` with the service's own embedding space (`self._space`), not with
  `state.applied_space` (RFC §5.1 b) — the main service writes every new note vector in its current space, while
  `applied_space` lags until the index's upgrade pass ends. A first deploy reads every note once and embeds none
  (all already in the space).
- Default B7: with zero notes the step stamps `notes_merge_version` and `notes_space` directly (nothing to merge or move).
- Default B8: behind a closed gate the full notes step runs (merges as well as clears) and the changed cards are
  embedded inline; a card the embedder cannot make is dropped (`without_card`), so no vector of an old note survives;
  it comes back as card work when the gate opens. For a D30-gated (v1) index this writes v2-planned fields onto v1
  records: harmless, search is gated and the owner's rebuild purges them.
- Default B9: a non-active note clears a record's copy only when the copy is its own (`owner_note_id` equal or empty),
  so an old cleared lineage never removes another note's copy.
- Default B10: the note's numbers and dates are found as the query finds them, inside the note's own text
  (`query_term_spans` spans, their maximal forms; a year inside a month-year span is keyed by the month-year, which also
  keys `y:`); words are keyed by the query's own word loop (a hyphen compound is one word, a short id with letters is
  words), skipping tokens inside a number or date. A `matched:` source prints the word as the owner wrote it.
- Default B11: a note cut by its own cap sets the record's `keys_capped` (the card says "keys cut").
- Default B12 (**rollback rule**): the RFC §5.5 claim that a roll-forward's version mismatch re-merges after a rollback
  does not hold as written — the rolled-back code saves the sync state with `merge=list(STATE_FIELDS)` of its own, so
  `notes_merge_version` survives and no mismatch appears. So: rollback is the kill switch; after a code rollback of the
  indexer the roll-forward ships `NOTE_MERGE_VERSION + 1`, and the re-merge rewrites every noted record whose derived
  fields lost the copy (`_note_copy_stale`, tested). Documented in D3 and D4.
- Default B13: the plain-folder card keeps its exact Slice 1 text `f"{name} | folder"` without a note (the RFC's
  `_joined` would turn an empty-name folder's ` | folder` into `folder`); the segment is appended after it.
- Default B14: the profile's doc type is dropped from the card text whenever the owner gave a type, also with
  `owner=False` (the search summary), so the summary and the note line never disagree.
- Default B15: the open header reads the connected drive's id through the `UserDrivePort` given to the agent
  (`drive_port`), not through a new core method; it shows an `archived` note too (`note_for_item` returns one) — that
  can only be a moment before entry (a) re-activates it.
- Default B16: only the tier search's `_collapse` prefers the noted copy (RFC §6.1); the filter-only listing's own
  duplicate fold is unchanged.
- Default B17: `DRIVE_NOTES_ENABLED` is parsed by `drive_indexing_setup.drive_notes_enabled` (on unless 0/off/false/no),
  the convention of `DRIVE_UPGRADE_PASS`.
- Default B18: entry (a) with no drive id known yet (no page, no connection drive id) skips the lookup and requests the
  re-read, so the notes step merges once the id is known.

## 5. OWNER? items

None. (The rollback rule, B12, is an operations default with a stated reason, not a product question.)

## 6. Core deltas

- **B1** (`src/domain/drive_note.py`): `NOTE_KEYS_MAX = 320` is defined in `src/domain/drive_index_params.py` and
  re-exported by `drive_note` under the same name and value (still in its `__all__`). Reason: the keyer must apply the
  cap, and `drive_note` imports `drive_keys`, so `drive_keys` importing `drive_note` is a cycle. No caller changes.
- **B2** (`src/services/drive_note_service.py`, behaviour only, no signature): `set` (both ref kinds) and `undo` pass
  `keys` / `keys_version` from `drive_note_merge.note_store_keys` to `note_after_write`; S1 wrote `[]` (core contract
  §1: "the note keyer is S2's").

RFC text deltas for the build's doc pass (no code beyond this part): §3.4 gains the Default B3 fields; §5.1 b step 2's
start rule is Default B4; §5.1 b step 5 compares with the service's space (B6); `NOTE_STEP_S` (60) joins §15; §5.5's
rollback sentence is replaced by B12.

## 7. Verification

Worktree `/Users/dmytrodeleur/Github/alekbot-core-npb`, branch `plan/drive-notes-b`, base `cef8f147`:

| Commit | Content |
|---|---|
| `fd79edb5` | Tasks 1-5 domain: fields, lean, planner flag, keys, `drive_note_merge`, card, explain, search card rendering; domain tests |
| `8eb0ad7e` | Task 1 codec readers, Task 6 tick; codec and tick tests |
| `65060ba1` | Task 7 note document keys; its test |
| `98ca4eb1` | Tasks 5 (search service), 8, 9, 10; search, open-header and wiring tests |
| `855488f8` | Task 6 fix found by `make check`: guards instead of `assert` for the optional note store (REQ-ARCH-01) |

New tests: 122 in 10 files, all green. `mypy --strict` on `src/domain` + `src/ports`: clean. `ruff check src/`: clean.
`PYTEST_WORKERS=4 make check` (once, on `98ca4eb1`): ruff clean, `mypy --strict` clean, unit suite **17 593 passed,
5 failed, 70 skipped**. The five: the four expected-risk tests listed in Tasks 1 and 10 (field lists and a pinned skill
sentence the RFC changes on purpose, for the reviewer), and `tests/unit/test_req_arch_01_hexagonal_isolation.py::
test_no_assert_in_src` — a real defect (five `assert self._notes is not None` in the tick), fixed in `855488f8`; after
it, that test and the 29 tick tests pass (targeted run, `-n 4`). So on `855488f8` the suite fails only on the four
expected-risk tests.
