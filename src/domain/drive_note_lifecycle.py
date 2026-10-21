"""
The rules of an owner note's lifecycle in the indexer (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §7.1, §7.4, §7.5, §8): which
deleted records report a check, how checks fold into the sync state, which notes the daily audit reads, and the snapshot
a confirmed observation leaves. Pure: no I/O, no clock, no logging. The tick and `DriveNoteLifecycle` (services/) call
these and do the reads and writes.

Public interface:
    NOTE_CHECKS_MAX, NOTE_CHECKS_STORED_MAX, NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS
    checks_of(records, why, last_seen_at) -> Tuple[NoteCheck, ...]     # the deleted records that carried a note
    fold_checks(stored, new, cap=NOTE_CHECKS_STORED_MAX) -> Tuple[Tuple[NoteCheck, ...], int]
    without_checks(checks, done) -> Tuple[NoteCheck, ...]
    audit_due(audited_at, now) -> bool
    audit_candidates(notes, *, provider_key, drive_id, now) -> List[DriveNote]
    refreshed_snapshot(old, change, *, path, now) -> NoteSnapshot      # the item was found: what the bot saw now
    moved_snapshot(old, path) -> Optional[NoteSnapshot]                # the item is gone: only its last known path
    chain_path(name, names_up) -> str
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Collection, Iterable, List, Optional, Sequence, Tuple

from .drive_card import content_hash_of
from .drive_index import DriveChange, DriveIndexRecord
from .drive_note import CONTENT_KEY_FAMILY, DriveNote, NoteSnapshot, NoteState, binding_parts_of
from .drive_note_check import NoteCheck

# §7.1: a round end works through at most this many checks (each a record read; a `gone` one also an item call within
# ITEM_CALLS_PER_TICK); the rest wait for the next round end
NOTE_CHECKS_MAX = 500
# The sync state is one Firestore document (1 MiB): past this many stored checks the newest are dropped, logged; the
# daily audit (§7.4) is the backstop, as for a check lost with a failed save (§3.4)
NOTE_CHECKS_STORED_MAX = 2_000
NOTE_AUDIT_EVERY = timedelta(hours=24)       # §7.4: the audit's period (`notes_audited_at`)
NOTE_AUDIT_SKIP_DAYS = 7                     # §7.4: a note whose snapshot is younger is not read again


def checks_of(records: Iterable[DriveIndexRecord], why: str,
              last_seen_at: Optional[datetime]) -> Tuple[NoteCheck, ...]:
    """One check per deleted record that carried a note (`owner_note_id`), with the why its delete site decided
    (§7.1 table) and the state's `observed_as_of` as the note's `last_seen_at`."""
    return tuple(NoteCheck(note_id=r.owner_note_id, item_id=r.item_id, why=why, last_seen_at=last_seen_at)
                 for r in records if r.owner_note_id)


def fold_checks(stored: Sequence[NoteCheck], new: Sequence[NoteCheck],
                cap: int = NOTE_CHECKS_STORED_MAX) -> Tuple[Tuple[NoteCheck, ...], int]:
    """`stored` with `new` folded in, one check per note: a newer check for a note already listed replaces it in its
    place (the later delete decides the why), a new note goes to the end (the round end works from the front). Past
    `cap` the tail is dropped; returns (the checks, how many were dropped)."""
    merged = {c.note_id: c for c in stored}
    for check in new:
        merged[check.note_id] = check
    folded = tuple(merged.values())
    if len(folded) <= cap:
        return folded, 0
    return folded[:cap], len(folded) - cap


def without_checks(checks: Sequence[NoteCheck], done: Collection[str]) -> Tuple[NoteCheck, ...]:
    """The checks still to do: those whose note is not in `done`."""
    return tuple(c for c in checks if c.note_id not in done)


def audit_due(audited_at: Optional[datetime], now: datetime) -> bool:
    """§7.4: once a day; a state that never audited is due."""
    return audited_at is None or now - audited_at >= NOTE_AUDIT_EVERY


def audit_candidates(notes: Iterable[DriveNote], *, provider_key: str, drive_id: str,
                     now: datetime) -> List[DriveNote]:
    """The active notes the audit may read by id (§7.4): bound to the index's drive of this provider (a dormant note is
    another drive's, and is never read), not an erase stub, and not seen by the bot in the last NOTE_AUDIT_SKIP_DAYS
    (`snapshot.snapshot_at`). The longest unseen first; a note never snapshotted counts as the oldest."""
    horizon = now - timedelta(days=NOTE_AUDIT_SKIP_DAYS)
    found: List[DriveNote] = []
    for note in notes:
        if note.state != NoteState.ACTIVE.value or note.erase_pending:
            continue
        parts = binding_parts_of(note)
        if parts is None or parts.provider_key != provider_key or parts.drive_id != drive_id:
            continue
        seen = note.snapshot.snapshot_at if note.snapshot is not None else None
        if seen is not None and seen > horizon:
            continue
        found.append(note)
    return sorted(found, key=lambda n: (_seen(n) or datetime.min.replace(tzinfo=now.tzinfo), n.note_id))


def _seen(note: DriveNote) -> Optional[datetime]:
    return note.snapshot.snapshot_at if note.snapshot is not None else None


def chain_path(name: str, names_up: Sequence[str]) -> str:
    """The path relative to the area root from the item's name and its ancestors' names, nearest first."""
    return "/".join([*reversed(names_up), name])


def refreshed_snapshot(old: Optional[NoteSnapshot], change: DriveChange, *, path: Optional[str],
                       now: datetime) -> NoteSnapshot:
    """The note's snapshot after the bot saw the item again (§7.1 "found", §7.4, §7.5): name, parent, size, dates and
    the content key from the observation; `path` from the parent chain, or the last known one when the chain could not
    be read (None); kind and effective date kept (the index decides those; a note does not re-derive them)."""
    base = old or NoteSnapshot()
    content_key = content_hash_of(change) or ""
    return base.model_copy(update={
        "name": change.name or base.name,
        "path": path if path is not None else base.path,
        "parent_item_id": change.parent_id or base.parent_item_id,
        "is_folder": change.is_folder,
        "mime_type": change.mime_type or base.mime_type,
        "size_bytes": change.size_bytes,
        "fs_created_at": change.created_at or base.fs_created_at,
        "fs_modified_at": change.fs_modified_at or base.fs_modified_at,
        "content_key": content_key,
        "content_key_family": CONTENT_KEY_FAMILY if content_key else "",
        "snapshot_at": now,
    })


def moved_snapshot(old: Optional[NoteSnapshot], path: Optional[str]) -> Optional[NoteSnapshot]:
    """The snapshot an archive stores (§7.1): only the last known path refreshed from the parent chain, everything else
    as last seen (`snapshot_at` too: the item was not seen). None when nothing changes (the archive keeps the stored
    one)."""
    if old is None or path is None or path == old.path:
        return None
    return old.model_copy(update={"path": path})
