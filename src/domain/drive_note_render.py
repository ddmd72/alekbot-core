"""
The owner notes' side of the deleted-files search (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4, D9, D16, D21): the note
store's keys, the strict-AND match over a note's keys and its last known name's keys, and the lines of a note entry.
Pure: no I/O, no clock, no logging. The section, its heading and the wrapper are the shared ones
(`drive_deleted_search`, one surface with the transcripts mirror, §0.3); this module fills a `DeletedHit`.

Every entry is data, never instructions: it is rendered inside the nonce-tagged wrapper, the note's text included (§6.3).

Public interface:
    NOTE_STORE_KEYS_VERSION                           # "<KEYS_VERSION>.<NOTE_KEYS_VERSION>": the stored `keys` layout
    NoteKeyer                                         # (text, doc_type, life_areas) -> the note's keys (§5.4)
    note_store_keys(content, name, note_keys) -> Tuple[str, ...]   # what a note's `keys` hold (§3.1)
    search_keys(note, note_keys) -> FrozenSet[str]    # the stored keys, or recomputed on a layout mismatch
    note_matches(concepts, keys) -> bool              # strict AND, no allowances
    owner_note_line(text, doc_type, life_areas) -> str
    matched_line(concepts, note_keys_of_note) -> str
    note_ref_line(note_id) -> str
    note_hit(note, *, dormant, nonce, matched, day) -> DeletedHit
    coverage lines: ARCHIVE_SCAN_CUT_LINE, MORE_NOTES_LINE, DORMANT_HIDDEN_LINE
"""
from __future__ import annotations

from typing import Callable, Collection, FrozenSet, List, Sequence, Tuple

from .drive_deleted_search import DeletedHit
from .drive_index import KEYS_VERSION
from .drive_keys import name_keys_v2
from .drive_match import Concept
from .drive_note import NOTE_KEYS_VERSION, DriveNote, NoteContent, binding_parts_of, note_ref

NOTE_STORE_KEYS_VERSION = f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"

# B's `drive_keys.note_keys(text, doc_type, life_areas)` (§5.4): the note's exact keys, classifiers first
NoteKeyer = Callable[[str, str, Sequence[str]], Sequence[str]]

OWNER_NOTE_PREFIX = "noted by the owner: "
MEANING_LINE = "closest by meaning, unverified"
REF_LINE = ("{ref} — the file is gone: this ref works only for annotate_drive_file (to change, undo or remove the "
            "note)")
DORMANT_REF_LINE = ("{ref} — the file is on a drive that is not connected: this ref works only for annotate_drive_file "
                    "(to change, undo or remove the note)")
ARCHIVE_SCAN_CUT_LINE = "Only the newest {limit} notes on deleted files were searched; older ones were not."
MORE_NOTES_LINE = "{count} more notes on deleted files matched."
DORMANT_HIDDEN_LINE = "The drive is not connected, so notes on files of other drives are not shown."


def note_store_keys(content: NoteContent, name: str, note_keys: NoteKeyer) -> Tuple[str, ...]:
    """A note document's `keys` (§3.1): the note's own keys (`note_keys`, part B's keyer) and the name keys v2 of the
    item's last known name, unique, in that order."""
    own = note_keys(content.text, content.doc_type, content.life_areas) if not content.is_empty else ()
    return tuple(dict.fromkeys((*own, *(name_keys_v2(name) if name else ()))))


def search_keys(note: DriveNote, note_keys: NoteKeyer) -> FrozenSet[str]:
    """The keys the deleted-files search matches a note on: the stored ones under the current layout, otherwise
    recomputed in memory (§3.1 `keys_version`): a note written before its keys existed (S1 stores none) still
    matches."""
    if note.keys_version == NOTE_STORE_KEYS_VERSION and note.keys:
        return frozenset(note.keys)
    name = note.snapshot.name if note.snapshot is not None else ""
    return frozenset(note_store_keys(note.content, name, note_keys))


def note_matches(concepts: Sequence[Concept], keys: Collection[str]) -> bool:
    """Strict AND, no allowances (the set is small, §6.4 step 2): every concept meets the keys through its about keys
    (the note's words, numbers and classifiers) or its word keys (the name's). An empty query matches everything."""
    held = keys if isinstance(keys, (set, frozenset)) else set(keys)
    return all(any(k in held for k in (*c.about_keys, *c.word_keys)) for c in concepts)


def owner_note_line(text: str, doc_type: str = "", life_areas: Sequence[str] = ()) -> str:
    """The note as a result line (§6.2's form): `noted by the owner: «<text>» (type contract; life areas home)`, or
    `noted by the owner: type contract` for a classifier-only note; "" for an empty triple."""
    parts: List[str] = []
    if doc_type:
        parts.append(f"type {doc_type.replace('_', ' ')}")
    if life_areas:
        parts.append("life areas " + ", ".join(a.replace("_", " ") for a in life_areas))
    if text:
        return OWNER_NOTE_PREFIX + f"«{text}»" + (f" ({'; '.join(parts)})" if parts else "")
    return OWNER_NOTE_PREFIX + "; ".join(parts) if parts else ""


def matched_line(concepts: Sequence[Concept], own_keys: Collection[str], name_keys: Collection[str]) -> str:
    """`matched: your note "kitchen", name "estimate"`: per concept, where it met (the note first). "" with no
    concept (a listing of the newest)."""
    found: List[str] = []
    for c in concepts:
        if any(k in own_keys for k in c.about_keys):
            found.append(f'your note "{c.word}"')
        elif any(k in name_keys for k in c.word_keys):
            found.append(f'name "{c.word}"')
    return "matched: " + ", ".join(found) if found else ""


def note_ref_line(note_id: str, *, dormant: bool = False) -> str:
    return (DORMANT_REF_LINE if dormant else REF_LINE).format(ref=note_ref(note_id))


def note_hit(note: DriveNote, *, dormant: bool, nonce: str, matched: str, day: str = "") -> DeletedHit:
    """One note entry: the item's last known name and path head it (`gone from the drive …` or, `dormant`, `on a drive
    that is not connected`), then the ref line and the note line with how it matched (`matched` is the matched line,
    or MEANING_LINE for the meaning fallback). The wrapper's ref is the note's `drive_note:` ref; there is no drive
    file to open (`item` None). `item_key` (`<drive id>:<item id>`) lets an archived transcript of the same item
    share the entry."""
    snapshot = note.snapshot
    name = snapshot.name if snapshot is not None else ""
    path = (snapshot.path or name) if snapshot is not None else ""
    parts = binding_parts_of(note)
    line = owner_note_line(note.text, note.doc_type, note.life_areas)
    return DeletedHit(
        name=name or note.ref, path=path, day=day,
        refs=(note_ref_line(note.note_id, dormant=dormant),),
        note_line="\n".join(x for x in (line, matched) if x),
        on_connected_drive=not dormant, nonce=nonce, ref=note.ref,
        item_key=f"{parts.drive_id}:{parts.item_id}" if parts is not None else "")
