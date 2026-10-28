"""
DriveNoteLifecycle — the indexer's side of an owner note's life (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §5.1 c, §7.1,
§7.4, §7.5, §10): the round-end archive checks and the daily audit. Called by the tick (DriveIndexingService), which
lends it its item-call budget and its lease fence; injected, never imported at run time (REQ-ARCH-22). Built only with
the notes kill switch on (DRIVE_NOTES_ENABLED, read in composition).

An archive is never decided inside `_apply`: a record that left the index reports a check, and only here, at the round
end, does one `get_change` decide. Only a not-found (or a port still reporting a deletion) archives, and only for a
check whose delete said `gone`; a record dropped because it is no longer stored (`unstored`) never archives (review C1).
A check whose record is present again (a restore, a move back in) is dropped. Any other error keeps the check for the
next round end. A note-store write that fails is logged at ERROR and keeps its check: note lifecycle is never systemic
(§10). A drive error the tick treats as systemic (not connected, throttled) ends the tick as it would anywhere else.

The audit (§7.4) is the backstop for every check that never ran: once a day, the active notes bound to the index's
drive whose item has no record and that the bot has not seen for NOTE_AUDIT_SKIP_DAYS are read by id, within the same
budget; a not-found archives the note, a found item refreshes its snapshot.

Note-store writes are the repository's own transactions on the note's parent (archive, refresh_snapshot), each behind
the tick's fence. Nothing here writes the index.

Public interface:
    DriveNoteLifecycle(notes, index, drive, *, provider_key=ONEDRIVE_PROVIDER_KEY, clock)
    await round_end(user_id, state, *, take_item_call, fence) -> (state, done note ids, counters)
    await audit(user_id, state, *, take_item_call, fence) -> (state, counters)
    await refresh_moved(user_id, state, moved, *, fence) -> counters      # §7.5, no item call
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from ..domain.drive_index import DriveChange, DriveIndexStoreUnavailable, DriveSyncState
from ..domain.drive_index_lean import DriveRecordLean
from ..domain.drive_note import NOTES_PER_USER_MAX, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteState, binding_parts_of
from ..domain.drive_note_check import WHY_GONE
from ..domain.drive_note_lifecycle import (
    NOTE_CHECK_UNKNOWN_MAX_AGE, audit_candidates, audit_cursor, audit_due, audit_key, chain_path, moved_snapshot, refreshed_snapshot,
    snapshot_from_record, snapshot_stale,
)
from ..domain.drive_note_merge import NOTE_CHECKS_MAX
from ..domain.drive_search import MAX_CHAIN_DEPTH
from ..domain.user_drive import DriveAccessDenied, DriveItemNotFoundError
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import exc_brief, logger

# An item read that says nothing about the item: the check stays for the next round end (the index RFC's §8.2 per-item
# class). DriveItemNotFoundError is caught before these (it is an OSError).
_PER_ITEM_ERRORS = (DriveAccessDenied, asyncio.TimeoutError, ValueError, OSError)
_GONE, _FOUND, _UNKNOWN = "gone", "found", "unknown"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DriveNoteLifecycle:
    def __init__(self, notes: DriveNoteRepository, index: DriveIndexRepository, drive: UserDrivePort, *,
                 provider_key: str = ONEDRIVE_PROVIDER_KEY, clock: Callable[[], datetime] = _utcnow) -> None:
        """`provider_key` is the provider part of the notes' binding keys: the domain's one constant, as the main
        service writes them. `index` is read only (is the record back? the parent chain of a path)."""
        self._notes = notes
        self._index = index
        self._drive = drive
        self._provider = provider_key
        self._clock = clock

    # ------------------------------------------------------------------------------------------------------------------
    # The round end (§5.1 c, §7.1)
    # ------------------------------------------------------------------------------------------------------------------

    async def round_end(self, user_id: str, state: DriveSyncState, *, take_item_call: Callable[[], bool],
                        fence: Callable[[], None]) -> Tuple[DriveSyncState, FrozenSet[str], Dict[str, int]]:
        """Work the first NOTE_CHECKS_MAX of `state.note_checks` (the caller folds the tick's new checks in first).
        Returns the state without the checks that are done, their note ids (the caller drops them from its
        accumulator too) and counters for the tick line. `take_item_call` spends one item call of the tick's budget
        (False: none left, the check waits; the tick gives the round end NOTE_ROUND_END_CALL_SHARE of its calls left);
        `fence` raises LeaseLost once the lease is ending. A check whose read says nothing (unknown) moves to the back
        of the list, so a run of checks that keep failing rotates instead of starving the rest (D-C20)."""
        checks = state.note_checks[:NOTE_CHECKS_MAX]
        if not checks:
            return state, frozenset(), {}
        counts: Dict[str, int] = {}
        now = self._clock()
        try:
            notes = await self._notes.get_many(user_id, list(dict.fromkeys(c.note_id for c in checks)))
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveNote] round end for {user_id[:8]}: note store unavailable: {exc_brief(exc)}; "
                         f"{len(checks)} checks kept")
            return state, frozenset(), {"note_checks_kept": len(checks)}
        cache: Dict[str, Optional[DriveRecordLean]] = dict(
            await self._index.get_records_lean(user_id, [c.item_id for c in checks]))
        done: Set[str] = set()
        rotated: Set[str] = set()                # unknown this round end: to the back of the list (D-C20)
        for check in checks:
            note = notes.get(check.note_id)
            if note is None or cache.get(check.item_id) is not None or not self._bound_to(note, check.item_id,
                                                                                            state.drive_id):
                done.add(check.note_id)          # the record is back, or the note no longer names this item
                _inc(counts, "note_checks_dropped")
                continue
            if note.state == NoteState.ARCHIVED.value:
                done.add(check.note_id)
                continue
            if not take_item_call():
                continue                         # past the item-call budget: the next round end
            verdict, change = await self._observe(user_id, check.item_id)
            if verdict == _UNKNOWN:
                if check.last_seen_at is not None and now - check.last_seen_at > NOTE_CHECK_UNKNOWN_MAX_AGE:
                    done.add(check.note_id)      # D-M1: aged out; the daily audit is the backstop
                    _inc(counts, "note_checks_aged_out")
                    continue
                rotated.add(check.note_id)
                _inc(counts, "note_checks_unknown")
                continue
            if change is not None:                # found
                if await self._refresh(user_id, note, change, state.root_item_id, cache, fence):
                    done.add(check.note_id)
                    _inc(counts, "note_snapshots_refreshed")
                continue
            if check.why != WHY_GONE:            # `unstored` never archives (C1): the audit watches it from now on
                done.add(check.note_id)
                _inc(counts, "note_checks_unstored_absent")
                continue
            if await self._archive(user_id, note, check.last_seen_at, state.root_item_id, cache, fence):
                done.add(check.note_id)
                _inc(counts, "notes_archived")
        # D-C20: a check whose read said nothing goes to the back, so checks that keep failing cannot hold the front of
        # the list (and every item call of the round end) for ever; the others keep their order
        kept = (tuple(c for c in state.note_checks if c.note_id not in done and c.note_id not in rotated)
                + tuple(c for c in state.note_checks if c.note_id in rotated))
        if counts:
            logger.info(f"[DriveNote] round end for {user_id[:8]}: checks={len(checks)} done={len(done)} "
                        f"kept={len(kept)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return replace(state, note_checks=kept), frozenset(done), counts

    # ------------------------------------------------------------------------------------------------------------------
    # The daily audit (§7.4)
    # ------------------------------------------------------------------------------------------------------------------

    async def audit(self, user_id: str, state: DriveSyncState, *, take_item_call: Callable[[], bool],
                    fence: Callable[[], None]) -> Tuple[DriveSyncState, Dict[str, int]]:
        """Once a day (`notes_audited_at`): every active note on the index's drive whose item has no record, unseen for
        NOTE_AUDIT_SKIP_DAYS, read by id. A pass moves a cursor (`notes_audit_after_*`, the `audit_key` of the last note
        it tried, whatever the outcome), so a pass cut by its item calls resumes after it on the next tick and a note
        that keeps failing is tried once per pass, never at the front of every tick; `notes_audited_at` is stamped,
        and the cursor cleared, when the pass ends. `take_item_call` is the tick's audit share (NOTE_AUDIT_CALL_SHARE)."""
        now = self._clock()
        after = audit_cursor(state)
        if not state.drive_id or (after is None and not audit_due(state.notes_audited_at, now)):
            return state, {}
        try:
            active = await self._notes.active(user_id, NOTES_PER_USER_MAX)
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveNote] audit for {user_id[:8]}: note store unavailable: {exc_brief(exc)}")
            return state, {}
        candidates = audit_candidates(active, provider_key=self._provider, drive_id=state.drive_id, now=now,
                                      after=after)
        counts: Dict[str, int] = {}
        finished = True
        tried: Optional[DriveNote] = None
        if candidates:
            items = [self._item_of(n) for n in candidates]
            cache: Dict[str, Optional[DriveRecordLean]] = dict(await self._index.get_records_lean(user_id, items))
            for note, item_id in zip(candidates, items):
                if cache.get(item_id) is not None:
                    continue                     # indexed: its record's deletes report checks
                if not take_item_call():
                    finished = False
                    break
                tried = note
                _inc(counts, "notes_audited")
                verdict, change = await self._observe(user_id, item_id)
                if verdict == _GONE:
                    seen = note.snapshot.snapshot_at if note.snapshot is not None else None
                    if await self._archive(user_id, note, seen, state.root_item_id, cache, fence):
                        _inc(counts, "notes_archived_by_audit")
                elif change is not None:         # found
                    if await self._refresh(user_id, note, change, state.root_item_id, cache, fence):
                        _inc(counts, "note_snapshots_refreshed")
        logger.info(f"[DriveNote] audit for {user_id[:8]}: active={len(active)} candidates={len(candidates)} "
                    f"resumed={after is not None} finished={finished} "
                    + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if finished:
            return replace(state, notes_audited_at=now, notes_audit_after_at=None, notes_audit_after_id=""), counts
        if tried is not None:
            at, note_id = audit_key(tried)
            state = replace(state, notes_audit_after_at=None if tried.snapshot is None or tried.snapshot.snapshot_at
                            is None else at, notes_audit_after_id=note_id)
        return state, counts

    # ------------------------------------------------------------------------------------------------------------------
    # Snapshot refresh (§7.5)
    # ------------------------------------------------------------------------------------------------------------------

    async def refresh_moved(self, user_id: str, state: DriveSyncState, moved: Mapping[str, str], *,
                            fence: Callable[[], None]) -> Dict[str, int]:
        """The notes whose record this tick renamed, moved or saw with new content (note id -> item id), refreshed
        from the record — no item call — when the snapshot no longer says it; path from the parent chain. A store
        failure is logged and costs only the refresh (the snapshot is "last known": the next observation retries)."""
        if not moved:
            return {}
        try:
            notes = await self._notes.get_many(user_id, list(moved))
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveNote] snapshot refresh for {user_id[:8]}: note store unavailable: {exc_brief(exc)}")
            return {}
        records = await self._index.get_records(user_id, list(dict.fromkeys(moved.values())))
        cache: Dict[str, Optional[DriveRecordLean]] = {}
        counts: Dict[str, int] = {}
        for note_id, item_id in moved.items():
            note, rec = notes.get(note_id), records.get(item_id)
            if note is None or rec is None or not self._bound_to(note, item_id, state.drive_id):
                continue
            if not snapshot_stale(note.snapshot, rec):
                continue
            path = await self._path(user_id, rec.name, rec.parent_id, state.root_item_id, cache)
            fence()
            try:
                await self._notes.refresh_snapshot(
                    user_id, note_id, snapshot_from_record(note.snapshot, rec, path=path, now=self._clock()))
            except DriveIndexStoreUnavailable as exc:
                logger.error(f"[DriveNote] refreshing note {note_id} for {user_id[:8]} failed: {exc_brief(exc)}")
                continue
            _inc(counts, "note_snapshots_refreshed")
        return counts

    # ------------------------------------------------------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------------------------------------------------------

    def _bound_to(self, note: DriveNote, item_id: str, drive_id: str) -> bool:
        """The note still names this item on the index's drive (a drive switch purges the state, so another drive's
        note never reaches here; checked anyway). An erase stub names nothing."""
        if note.erase_pending:
            return False
        parts = binding_parts_of(note)
        return (parts is not None and parts.provider_key == self._provider and parts.item_id == item_id
                and (not drive_id or parts.drive_id == drive_id))

    @staticmethod
    def _item_of(note: DriveNote) -> str:
        parts = binding_parts_of(note)
        return parts.item_id if parts is not None else ""

    async def _observe(self, user_id: str, item_id: str) -> Tuple[str, Optional[DriveChange]]:
        """One `get_change`: gone (not found), found, or unknown (a per-item error, or a port answering with a
        deletion: delta decides existence, as the tick's re-check step reads it; §7.1 archives on not-found only). A
        systemic error (not connected, throttled) propagates: the tick ends as it would on any item call."""
        try:
            change = await self._drive.get_change(user_id, item_id)
        except DriveItemNotFoundError:
            return _GONE, None
        except _PER_ITEM_ERRORS as exc:
            logger.warning(f"[DriveNote] reading item {item_id} for {user_id[:8]} failed: {exc_brief(exc)}; kept")
            return _UNKNOWN, None
        if change.deleted:
            logger.warning(f"[DriveNote] reading item {item_id} for {user_id[:8]} answered a deletion; kept")
            return _UNKNOWN, None
        return _FOUND, change

    async def _archive(self, user_id: str, note: DriveNote, last_seen_at: Optional[datetime], root_item_id: str,
                       cache: Dict[str, Optional[DriveRecordLean]], fence: Callable[[], None]) -> bool:
        """Archive the note (§7.1): its last known path refreshed from the parent chain when its parent's record still
        exists. True when the note is archived now or already was (the check is done); False on a store failure."""
        old = note.snapshot
        path = (await self._path(user_id, old.name, old.parent_item_id, root_item_id, cache)
                if old is not None and old.name else None)
        fence()
        try:
            changed = await self._notes.archive(user_id, note.note_id, archived_at=self._clock(),
                                                last_seen_at=last_seen_at, snapshot=moved_snapshot(old, path))
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveNote] archiving note {note.note_id} for {user_id[:8]} failed: {exc_brief(exc)}; "
                         f"the check stays")
            return False
        logger.info(f"[DriveNote] note {note.note_id} of {user_id[:8]} archived={changed}: its item is gone")
        return True

    async def _refresh(self, user_id: str, note: DriveNote, change: DriveChange, root_item_id: str,
                       cache: Dict[str, Optional[DriveRecordLean]], fence: Callable[[], None]) -> bool:
        """The item exists outside the index's view: the note stays as it is and its snapshot is what the bot saw now
        (§7.1 found, §7.5). False on a store failure (the check stays)."""
        path = await self._path(user_id, change.name, change.parent_id, root_item_id, cache)
        snapshot = refreshed_snapshot(note.snapshot, change, path=path, now=self._clock())
        fence()
        try:
            await self._notes.refresh_snapshot(user_id, note.note_id, snapshot)
        except DriveIndexStoreUnavailable as exc:
            logger.error(f"[DriveNote] refreshing note {note.note_id} for {user_id[:8]} failed: {exc_brief(exc)}; "
                         f"the check stays")
            return False
        return True

    async def _path(self, user_id: str, name: str, parent_id: str, root_item_id: str,
                    cache: Dict[str, Optional[DriveRecordLean]]) -> Optional[str]:
        """The path relative to the area root from the parent chain of records (§7.1), read through `cache`. None when
        the parent itself has no record (the caller keeps the last known path); a chain that breaks higher up ends at
        the last known ancestor; a cycle or a chain deeper than MAX_CHAIN_DEPTH is None."""
        if not name:
            return None
        names: List[str] = []
        seen: Set[str] = set()
        current = parent_id
        while current and current != root_item_id:
            if current in seen or len(names) >= MAX_CHAIN_DEPTH:
                return None
            seen.add(current)
            if current not in cache:
                cache.update(await self._index.get_records_lean(user_id, [current]))
                cache.setdefault(current, None)
            parent = cache[current]
            if parent is None:
                if not names:
                    return None
                break
            names.append(parent.name)
            current = parent.parent_id
        return chain_path(name, names)


def _inc(counts: Dict[str, int], key: str, by: int = 1) -> None:
    counts[key] = counts.get(key, 0) + by
