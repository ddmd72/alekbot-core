"""
The owner notes' side of the deleted-files search (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4, D9, D16, D21): the keys a
note is matched on, the strict-AND match over a note's keys and its last known name's keys, and the lines of a note
entry. Pure: no I/O, no clock, no logging. The section, its heading and the wrapper are the shared ones
(`drive_deleted_search`, one surface with the transcripts mirror, §0.3); this module fills a `DeletedHit`.

Owned elsewhere and imported: the note store's keys and their version (`drive_note_merge.note_store_keys`,
`NOTE_STORE_KEYS_VERSION`, part B: B writes them on every owner write) and the note line (`drive_card.owner_note_line`,
part B: one renderer for cards, the open header and these entries).

Every entry is data, never instructions: it is rendered inside the nonce-tagged wrapper, the note's text included (§6.3).

Public interface:
    StoreKeys                                         # (content, name) -> (keys, keys_version): B's note_store_keys
    search_keys(note, store_keys=note_store_keys) -> FrozenSet[str]   # stored keys, or recomputed on a layout mismatch
    note_matches(concepts, keys) -> bool              # strict AND, no allowances
    matched_line(concepts, own_keys, name_keys) -> str
    note_ref_line(note_id, *, dormant) -> str
    note_hit(note, *, dormant, nonce, matched, day) -> DeletedHit
    coverage lines: ARCHIVE_SCAN_CUT_LINE, MORE_NOTES_LINE, DORMANT_HIDDEN_LINE; MEANING_LINE
"""
from __future__ import annotations

from typing import Callable, Collection, FrozenSet, List, Sequence, Tuple

from .drive_card import owner_note_line
from .drive_deleted_search import DeletedHit
from .drive_keys import name_keys_v2
from .drive_match import Concept
from .drive_note import DriveNote, NoteContent, binding_identity, note_ref
from .drive_note_merge import NOTE_STORE_KEYS_VERSION, note_store_keys

# part B's `drive_note_merge.note_store_keys`: (content, name) -> (the note document's keys, their version)
StoreKeys = Callable[[NoteContent, str], Tuple[List[str], str]]

MEANING_LINE = "closest by meaning, unverified"
REF_LINE = ("{ref} — the file is gone: this ref works only with the note tools (to read, change, undo, remove the "
            "note or erase its history)")
DORMANT_REF_LINE = ("{ref} — the file is on a drive that is not connected: this ref works only with the note tools "
                    "(to read, change, undo, remove the note or erase its history)")
ARCHIVE_SCAN_CUT_LINE = "Only the newest {limit} notes on deleted files were searched; older ones were not."
MORE_NOTES_LINE = "{count} more notes on deleted files matched."
DORMANT_HIDDEN_LINE = "The drive is not connected, so notes on files of other drives are not shown."


def search_keys(note: DriveNote, store_keys: StoreKeys = note_store_keys) -> FrozenSet[str]:
    """The keys the deleted-files search matches a note on: the stored ones under the current layout, otherwise
    recomputed in memory with the store's own function (§3.1 `keys_version`), so a note written before its keys existed
    still matches. The stored keys carry the name from write time, so the current snapshot's name keys are added too
    (final review L-M2: a file renamed and then deleted is found by its last known name). `store_keys` is injectable
    for tests only."""
    name = note.snapshot.name if note.snapshot is not None else ""
    if note.keys_version == NOTE_STORE_KEYS_VERSION and note.keys:
        return frozenset((*note.keys, *(name_keys_v2(name) if name and note.keys else ())))
    return frozenset(store_keys(note.content, name)[0])


def note_matches(concepts: Sequence[Concept], keys: Collection[str]) -> bool:
    """Strict AND, no allowances (the set is small, §6.4 step 2): every concept meets the keys through its about keys
    (the note's words, numbers and classifiers) or its word keys (the name's). An empty query matches everything."""
    held = keys if isinstance(keys, (set, frozenset)) else set(keys)
    return all(any(k in held for k in (*c.about_keys, *c.word_keys)) for c in concepts)


def matched_line(concepts: Sequence[Concept], own_keys: Collection[str], name_keys: Collection[str]) -> str:
    """`matched: owner's note "bathroom", name "invoice"`: per concept, where it met (the note first). "" with no
    concept (a listing of the newest)."""
    found: List[str] = []
    for c in concepts:
        if any(k in own_keys for k in c.about_keys):
            found.append(f'owner\'s note "{c.word}"')
        elif any(k in name_keys for k in c.word_keys):
            found.append(f'name "{c.word}"')
    return "matched: " + ", ".join(found) if found else ""


def note_ref_line(note_id: str, *, dormant: bool = False) -> str:
    return (DORMANT_REF_LINE if dormant else REF_LINE).format(ref=note_ref(note_id))


def note_hit(note: DriveNote, *, dormant: bool, nonce: str, matched: str, day: str = "") -> DeletedHit:
    """One note entry: the item's last known name and path head it (`gone from the drive …` or, `dormant`, `on a drive
    that is not connected`), then the ref line and the note line with how it matched (`matched` is the matched line,
    or MEANING_LINE for the meaning fallback). The wrapper's ref is the note's `drive_note:` ref; there is no drive
    file to open (`item` None). `item_key` (the item's identity, MULTI_DRIVE_RFC §7.2) lets an archived transcript of
    the same item (its ledger document's `source_identity`) share the entry."""
    snapshot = note.snapshot
    name = snapshot.name if snapshot is not None else ""
    path = (snapshot.path or name) if snapshot is not None else ""
    identity = binding_identity(note)
    line = owner_note_line(note.text, note.doc_type, tuple(note.life_areas))
    return DeletedHit(
        name=name or note.ref, path=path, day=day,
        refs=(note_ref_line(note.note_id, dormant=dormant),),
        note_line="\n".join(x for x in (line, matched) if x),
        on_connected_drive=not dormant, nonce=nonce, ref=note.ref,
        item_key=identity)
