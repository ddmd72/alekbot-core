"""
Drive note merge — the owner's note copied into the index record of its item (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md
§5.1-§5.5; D7, D9, D15). Pure: stdlib and domain modules only; no I/O, no clock (callers pass `now`).

The note store (`{prefix}drive_notes`) is the record; the index record carries a denormalized copy (`owner_*` fields),
rebuilt from the store by the indexer's tick through ONE function, `with_owner_note(rec, view)`. A view exists only for
an `active` note bound on the index's connection (D15; MULTI_DRIVE_RFC §7.2, §9: a record finds its note by the item
identity's `i:` key, and the note's `r:` key names the connection); anything else (cleared, archived, an erase stub,
another connection) clears the copy. The record then goes through
`drive_enrich_plan.planned` (its about keys carry the note's keys) and `schedule` (a changed card hash makes card work
due in class 0); this module decides nothing about scheduling.

`NOTE_MERGE_VERSION` versions the card segment and the note's keys. It is NOT in `CURRENT_VERSIONS` (N9): a bump resets
the notes step's watermark (`DriveSyncState.notes_merge_version`), which re-reads every note and re-merges exactly the
noted records.

Public interface:
    NoteView                                     (note_id, version, text, doc_type, life_areas) of a merged note
    note_view(note, *, ref_tag) -> Optional[NoteView]
    with_owner_note(rec, view) -> DriveIndexRecord        idempotent; None clears the copy
    merged(rec, view) -> bool                    the record already carries exactly this view (no write needed)
    effective_doc_type(rec), effective_life_areas(rec)    the owner's classifiers over the profile's (re-exported)
    note_store_keys(content, name) -> (keys, keys_version)   the note document's `keys` (§3.1) and their version
    NOTE_STORE_KEYS_VERSION                       that version ("{KEYS_VERSION}.{NOTE_KEYS_VERSION}")
    notes_query_start(state) -> (after, after_note_id)       where the notes step's next `changed_since` starts
    NOTE_MERGE_VERSION and the tick constants (§15): NOTE_PAGE, NOTE_WATERMARK_SKEW_S, NOTE_EMBED_PER_TICK,
    NOTE_CHECKS_MAX, NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS, NOTE_STEP_S, NOTE_STUB_HOLD_MAX, NOTE_LOOKUP_BATCH
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .drive_index import KEYS_VERSION, DriveIndexRecord, DriveSyncState
from .drive_key_sources import effective_doc_type, effective_life_areas
from .drive_keys import name_keys_v2, note_keys
from .drive_note import NOTE_KEYS_VERSION, DriveNote, NoteContent, NoteState, on_connection

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
# A stub whose delete keeps failing holds the watermark (the notes after it wait); past this age it is logged and passed
# (re-review item 5): it holds no content, and the notes more than a page behind it must not wait for good.
NOTE_STUB_HOLD_MAX = timedelta(hours=24)
NOTE_LOOKUP_BATCH = 30                      # §5.1 a: binding keys per `array-contains-any` (the repository batches too)
# §3.1: the layout of a note document's stored `keys` (the keyer's and the note keyer's versions): a note whose
# `keys_version` differs has its keys recomputed with `note_store_keys` (part C's deleted-files search)
NOTE_STORE_KEYS_VERSION = f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"


@dataclass(frozen=True)
class NoteView:
    """The part of an `active` note an index record carries (§5.1): which note, which version, and the triple."""
    note_id: str
    version: int
    text: str
    doc_type: str
    life_areas: Tuple[str, ...]


def note_view(note: Optional[DriveNote], *, ref_tag: str) -> Optional[NoteView]:
    """The view to merge for `note`, or None to clear the copy: only an `active` note (not an erase stub) bound on the
    connection whose ref tag is `ref_tag` gives one (D9: an archived note is never merged; D15, D16: a note bound on
    another connection is dormant here). A note whose triple is empty is never `active`, but is cleared all the same.
    The record's note is found by `item_binding_key(record.identity)` (drive_note)."""
    if note is None or note.erase_pending or note.state != NoteState.ACTIVE.value:
        return None
    if not on_connection(note, ref_tag):
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
    memory at search time). An empty triple has none and no name keys (§3.1: `[]` when cleared)."""
    if content.is_empty:
        return [], ""
    keys = list(dict.fromkeys([*note_keys(content.text, content.doc_type, content.life_areas),
                               *(name_keys_v2(name) if name else ())]))
    return keys, NOTE_STORE_KEYS_VERSION


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
    "NOTE_MERGE_VERSION", "NOTE_PAGE", "NOTE_STEP_S", "NOTE_STORE_KEYS_VERSION", "NOTE_STUB_HOLD_MAX",
    "NOTE_WATERMARK_SKEW_S", "NoteView", "effective_doc_type", "effective_life_areas", "merged", "note_store_keys",
    "note_view", "notes_query_start", "with_owner_note",
]
