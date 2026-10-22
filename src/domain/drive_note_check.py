"""
One owner note's archive check (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.4, §7.1): a record that carried a note left the
index, and the round end decides what that means for the note. Pure: stdlib only, no I/O, no clock.

Kept in its own module because `DriveSyncState` (drive_index.py) stores a tuple of them and the lifecycle rules
(drive_note_lifecycle.py) import drive_index: this module imports neither.

Public interface:
    NoteCheck(note_id, item_id, why, last_seen_at)
    WHY_GONE, WHY_UNSTORED       # the same words as the transcript mirror's notices (drive_mirror.GONE / UNSTORED)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

WHY_GONE = "gone"            # a tombstone, a cascade under a deleted folder, the epoch sweep, an orphan: confirm by id
WHY_UNSTORED = "unstored"    # the item is no longer stored (a repository, a dense tree, an OS artefact): never archive


@dataclass(frozen=True)
class NoteCheck:
    """`note_id` is the note the deleted record carried (`owner_note_id`), `item_id` the record's item, `why` one of
    WHY_GONE / WHY_UNSTORED, `last_seen_at` the sync state's `observed_as_of` when the delete was seen: what the
    archive stamps as `last_seen_at`."""
    note_id: str
    item_id: str
    why: str
    last_seen_at: Optional[datetime] = None
