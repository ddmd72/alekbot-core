"""
The rules of an owner note's lifecycle in the indexer (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §7.1, §7.4, §7.5, §8): which
deleted records report a check, how checks fold into the sync state, which notes the daily audit reads, and the snapshot
a confirmed observation leaves. Pure: no I/O, no clock, no logging. The tick and `DriveNoteLifecycle` (services/) call
these and do the reads and writes.

The round end's and the audit's period constants are part B's (`drive_note_merge`: NOTE_CHECKS_MAX, NOTE_AUDIT_EVERY,
NOTE_AUDIT_SKIP_DAYS, RFC §15); the two caps below are this module's.

Public interface:
    NOTE_CHECKS_STORED_MAX, NOTE_ROUND_END_CALL_SHARE, NOTE_AUDIT_CALL_SHARE
    call_share(calls_left, share) -> int                               # the item calls a step may take
    checks_of(records, why, last_seen_at) -> Tuple[NoteCheck, ...]     # the deleted records that carried a note
    fold_checks(stored, new, cap=NOTE_CHECKS_STORED_MAX) -> Tuple[Tuple[NoteCheck, ...], int]
    without_checks(checks, done) -> Tuple[NoteCheck, ...]
    audit_due(audited_at, now) -> bool
    audit_key(note) -> AuditKey                                        # the audit's order and its pass cursor
    audit_candidates(notes, *, ref_tag, now, after=None) -> List[DriveNote]   (the connection's notes, by ref tag)
    audit_cursor(state) -> Optional[AuditKey]
    refreshed_snapshot(old, change, *, path, now) -> NoteSnapshot      # the item was found: what the bot saw now
    moved_snapshot(old, path) -> Optional[NoteSnapshot]                # the item is gone: only its last known path
    record_moved(before, after) -> bool                                # §7.5: a noted record's name, parent or content
    snapshot_stale(snapshot, rec) -> bool                              # §7.5: the snapshot no longer says the record
    snapshot_from_record(old, rec, *, path, now) -> NoteSnapshot       # §7.5: refreshed from the record, no item call
    chain_path(name, names_up) -> str
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Collection, Iterable, List, Optional, Sequence, Tuple

from .drive_content_identity import ContentIdentity
from .drive_index import DriveChange, DriveIndexRecord, DriveSyncState
from .drive_note import DriveNote, NoteSnapshot, NoteState, on_connection
from .drive_note_check import NoteCheck
from .drive_note_merge import NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS

# The sync state is one Firestore document (1 MiB): past this many stored checks the newest are dropped, logged; the
# daily audit (§7.4) is the backstop, as for a check lost with a failed save (§3.4)
NOTE_CHECKS_STORED_MAX = 2_000
# §7.4: the audit takes at most this share of the item calls left when it starts (the round end's are spent first), so
# probes, parent lookups, re-listings and access re-checks of the same tick always keep the rest; with the default 20
# calls it reads 13, so a pass over 25 notes that all fail ends in two ticks
NOTE_AUDIT_CALL_SHARE = 0.65
# §7.1, D-C20: the round end, which runs first, takes at most this share of the item calls left, and the audit then takes
# NOTE_AUDIT_CALL_SHARE of what remains: with the default 20 calls, 13 for the round end, then 4 for the audit, 3 kept
NOTE_ROUND_END_CALL_SHARE = 0.65
# final review D-M1: a check whose read keeps saying nothing (unknown) is dropped once the delete it reports is this old
# (its `last_seen_at`): it no longer costs an item call on every complete round, and the daily audit — which reads an
# active, record-less note once it is NOTE_AUDIT_SKIP_DAYS unseen — is the backstop that archives it
NOTE_CHECK_UNKNOWN_MAX_AGE = timedelta(days=NOTE_AUDIT_SKIP_DAYS)

AuditKey = Tuple[datetime, str]      # (snapshot_at, note_id); a note never snapshotted sorts first
_NEVER = datetime.min.replace(tzinfo=timezone.utc)


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


def call_share(calls_left: int, share: float) -> int:
    """The item calls a notes step may take of `calls_left` (D-C18, D-C20): `share` of them, at least one while any is
    left (a tick with a single call still makes progress), none when none is left."""
    if calls_left <= 0:
        return 0
    return max(1, int(calls_left * share))


def audit_due(audited_at: Optional[datetime], now: datetime) -> bool:
    """§7.4: once a day; a state that never audited is due."""
    return audited_at is None or now - audited_at >= NOTE_AUDIT_EVERY


def audit_key(note: DriveNote) -> AuditKey:
    """The audit's order (§7.4): the longest unseen first (`snapshot.snapshot_at`; never snapshotted first), then the
    note id. The pass cursor is the key of the last note a pass tried."""
    seen = note.snapshot.snapshot_at if note.snapshot is not None else None
    return (seen or _NEVER, note.note_id)


def audit_cursor(state: DriveSyncState) -> Optional[AuditKey]:
    """The running pass's cursor (`notes_audit_after_id` set), else None (no pass running)."""
    if not state.notes_audit_after_id:
        return None
    return (state.notes_audit_after_at or _NEVER, state.notes_audit_after_id)


def audit_candidates(notes: Iterable[DriveNote], *, ref_tag: str, now: datetime,
                     after: Optional[AuditKey] = None) -> List[DriveNote]:
    """The active notes the audit may read by id (§7.4): bound on the index's connection (`ref_tag`, MULTI_DRIVE_RFC §9;
    a note of another connection is dormant here, and is never read), not an erase stub, not seen by the bot in the last NOTE_AUDIT_SKIP_DAYS, and —
    in a pass that resumes — after the pass cursor `after`, so a note that failed is not tried twice in one pass. In
    `audit_key` order."""
    horizon = now - timedelta(days=NOTE_AUDIT_SKIP_DAYS)
    found: List[DriveNote] = []
    for note in notes:
        if note.state != NoteState.ACTIVE.value or note.erase_pending:
            continue
        if not on_connection(note, ref_tag):
            continue
        seen = note.snapshot.snapshot_at if note.snapshot is not None else None
        if seen is not None and seen > horizon:
            continue
        if after is not None and audit_key(note) <= after:
            continue
        found.append(note)
    return sorted(found, key=audit_key)


def chain_path(name: str, names_up: Sequence[str]) -> str:
    """The path relative to the area root from the item's name and its ancestors' names, nearest first."""
    return "/".join([*reversed(names_up), name])


def refreshed_snapshot(old: Optional[NoteSnapshot], change: DriveChange, *, path: Optional[str],
                       now: datetime) -> NoteSnapshot:
    """The note's snapshot after the bot saw the item again (§7.1 "found", §7.4, §7.5): name, parent, size, dates and
    the content key from the observation; `path` from the parent chain, or the last known one when the chain could not
    be read (None); kind and effective date kept (the index decides those; a note does not re-derive them)."""
    base = old or NoteSnapshot()
    content_key, family = _content_key(change.content_identity)
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
        "content_key_family": family,
        "snapshot_at": now,
    })


def moved_snapshot(old: Optional[NoteSnapshot], path: Optional[str]) -> Optional[NoteSnapshot]:
    """The snapshot an archive stores (§7.1): only the last known path refreshed from the parent chain, everything else
    as last seen (`snapshot_at` too: the item was not seen). None when nothing changes (the archive keeps the stored
    one)."""
    if old is None or path is None or path == old.path:
        return None
    return old.model_copy(update={"path": path})


def record_moved(before: DriveIndexRecord, after: DriveIndexRecord) -> bool:
    """§7.5: an observation changed what a note's snapshot holds of its item — the name, the parent or the content."""
    return ((before.name, before.parent_id, before.content_identity)
            != (after.name, after.parent_id, after.content_identity))


def snapshot_stale(snapshot: Optional[NoteSnapshot], rec: DriveIndexRecord) -> bool:
    """§7.5: the note's snapshot differs from its record in name, parent or content key (part B's merge entries ask
    this when they merge a note into a record)."""
    if snapshot is None:
        return True
    return (snapshot.name, snapshot.parent_item_id, snapshot.content_key) != (
        rec.name, rec.parent_id, _content_key(rec.content_identity)[0])


def snapshot_from_record(old: Optional[NoteSnapshot], rec: DriveIndexRecord, *, path: Optional[str],
                         now: datetime) -> NoteSnapshot:
    """The snapshot as the index holds the item now (§7.5): no item call. `path` from the parent chain, or the last
    known one (None)."""
    base = old or NoteSnapshot()
    content_key, family = _content_key(rec.content_identity)
    return base.model_copy(update={
        "name": rec.name or base.name,
        "path": path if path is not None else base.path,
        "parent_item_id": rec.parent_id or base.parent_item_id,
        "is_folder": rec.is_folder,
        "kind": rec.kind or base.kind,
        "mime_type": rec.mime_type or base.mime_type,
        "size_bytes": rec.size_bytes,
        "fs_modified_at": rec.fs_modified_at or base.fs_modified_at,
        "effective_date": rec.effective_date or base.effective_date,
        "content_key": content_key,
        "content_key_family": family,
        "snapshot_at": now,
    })


def _content_key(identity: Optional[ContentIdentity]) -> Tuple[str, str]:
    """The snapshot's content key and family (MULTI_DRIVE_RFC §7.2): `{family}:{digest}:{size}` and the adapter's family;
    ("", "") when the provider gave no hash (no family is implied by absence)."""
    if identity is None:
        return "", ""
    return identity.render(), identity.family
