"""
STUB (part C's worktree only). Part B owns this module (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §5.1-§5.5, §15): the merge
of a note into its record, the tick constants and the note store's keys. Only the names part C imports are stubbed
here, with B's signatures and values; the assembly keeps B's module whole and drops this file.

Part C imports: NOTE_CHECKS_MAX, NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS, note_store_keys, NOTE_STORE_KEYS_VERSION (the
last is the one name B adds for C: B's `note_store_keys` builds the same string inline).
"""
from __future__ import annotations

from datetime import timedelta
from typing import List, Tuple

from .drive_index import KEYS_VERSION
from .drive_keys import name_keys_v2, note_keys
from .drive_note import NOTE_KEYS_VERSION, NoteContent

NOTE_CHECKS_MAX = 500                       # §7.1 (S3): archive checks handled per round end
NOTE_AUDIT_EVERY = timedelta(hours=24)      # §7.4 (S3)
NOTE_AUDIT_SKIP_DAYS = 7                    # §7.4 (S3)
NOTE_STORE_KEYS_VERSION = f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"   # the note document's `keys_version` (§3.1)


def note_store_keys(content: NoteContent, name: str) -> Tuple[List[str], str]:
    """The note document's `keys` (§3.1): the note's own keys and the snapshot name's name keys v2, used only by the
    deleted-files search; and `keys_version`. An empty triple has none."""
    if content.is_empty:
        return [], ""
    keys = list(dict.fromkeys([*note_keys(content.text, content.doc_type, content.life_areas),
                               *(name_keys_v2(name) if name else ())]))
    return keys, NOTE_STORE_KEYS_VERSION
