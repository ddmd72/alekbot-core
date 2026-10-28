"""
InMemoryDriveNoteRepository — DriveNoteRepository in process memory: the second implementation of the port, the reference
the Firestore adapter is held against (tests/unit/adapters/test_drive_note_repo_contract.py runs one case list on both).

`updated_at` comes from `clock`, made strictly increasing (a tie moves one microsecond on), so the watermark order is
total, as Firestore's server timestamps are for commits. Every parent write bumps `revision` (the stored one + 1).
Results are copies: a caller mutating one changes nothing here.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.drive_note import (
    BoundBy, DriveNote, DriveNoteConflict, DriveNoteVersion, EraseResult, NoteSnapshot, NoteState, closed_version,
    erase_stub, indexer_writer,
)
from ..ports.drive_note_repository import DriveNoteRepository

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lean(note: DriveNote) -> DriveNote:
    return note.model_copy(update={"note_vector": None}, deep=True)


class InMemoryDriveNoteRepository(DriveNoteRepository):
    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._last: Optional[datetime] = None
        self.notes: Dict[Tuple[str, str], DriveNote] = {}
        self.versions: Dict[Tuple[str, str], Dict[int, DriveNoteVersion]] = {}

    def _stamp(self) -> datetime:
        now = self._clock()
        if self._last is not None and now <= self._last:
            now = self._last + timedelta(microseconds=1)
        self._last = now
        return now

    def _user(self, user_id: str) -> List[DriveNote]:
        return sorted((n for (u, _), n in self.notes.items() if u == user_id), key=lambda n: n.note_id)

    def _put(self, note: DriveNote) -> None:
        """Store `note` as the parent, `revision` one past the STORED one (0 when there is none), `updated_at` now."""
        stored = self.notes.get((note.user_id, note.note_id))
        revision = (stored.revision if stored is not None else 0) + 1
        self.notes[(note.user_id, note.note_id)] = note.model_copy(
            update={"updated_at": self._stamp(), "revision": revision}, deep=True)

    def _lifecycle(self, note: DriveNote, update: Dict[str, object]) -> None:
        self._put(note.model_copy(update={**update, "last_writer": indexer_writer()}))

    # -- reads by id and by key ----------------------------------------------------------------------------------------

    async def get(self, user_id: str, note_id: str) -> Optional[DriveNote]:
        note = self.notes.get((user_id, note_id))
        return note.model_copy(deep=True) if note is not None else None

    async def get_many(self, user_id: str, note_ids: Sequence[str]) -> Dict[str, DriveNote]:
        return {i: self.notes[(user_id, i)].model_copy(deep=True) for i in dict.fromkeys(note_ids)
                if (user_id, i) in self.notes}

    async def find_bound(self, user_id: str, binding_key: str) -> Optional[DriveNote]:
        found = [n for n in self._user(user_id) if binding_key in n.match_keys]
        return found[0].model_copy(deep=True) if found else None

    async def find_by_match_keys(self, user_id: str, keys: Sequence[str]) -> List[DriveNote]:
        wanted = set(keys)
        if not wanted:
            return []
        return [n.model_copy(deep=True) for n in self._user(user_id) if wanted.intersection(n.match_keys)]

    async def note_ids(self, user_id: str) -> List[str]:
        return [n.note_id for n in self._user(user_id) if not n.erase_pending]

    async def count(self, user_id: str) -> int:
        return len(self._user(user_id))

    # -- history -----------------------------------------------------------------------------------------------------------

    async def get_version(self, user_id: str, note_id: str, version: int) -> Optional[DriveNoteVersion]:
        found = self.versions.get((user_id, note_id), {}).get(version)
        return found.model_copy(deep=True) if found is not None else None

    async def list_versions(self, user_id: str, note_id: str) -> List[DriveNoteVersion]:
        chain = self.versions.get((user_id, note_id), {})
        return [chain[v].model_copy(deep=True) for v in sorted(chain)]

    # -- the owner-write transaction -----------------------------------------------------------------------------------

    async def write_version(self, note: DriveNote, version: DriveNoteVersion, *, expected_version: int) -> None:
        if version.version != expected_version + 1 or note.current_version != version.version:
            raise ValueError(f"write_version: version {version.version} / note {note.current_version} after "
                             f"{expected_version}")
        key = (note.user_id, note.note_id)
        stored = self.notes.get(key)
        current = stored.current_version if stored is not None else 0
        revision = stored.revision if stored is not None else 0
        if current != expected_version:
            raise DriveNoteConflict(f"note {note.note_id}: stored version {current}, expected {expected_version}")
        if revision != note.revision:
            raise DriveNoteConflict(f"note {note.note_id}: stored revision {revision}, read {note.revision}")
        chain = self.versions.setdefault(key, {})
        if expected_version and expected_version in chain:
            chain[expected_version] = closed_version(chain[expected_version], version.valid_from)
        chain[version.version] = version.model_copy(deep=True)
        self._put(note)

    # -- the tick and the deleted-files search ------------------------------------------------------------------------

    async def changed_since(self, user_id: str, after: Optional[datetime], limit: int, *,
                            after_note_id: str = "") -> List[DriveNote]:
        notes = sorted((n for n in self._user(user_id) if n.updated_at is not None),
                       key=lambda n: (n.updated_at or _OLDEST, n.note_id))
        if after is not None:
            if after_note_id:
                notes = [n for n in notes if (n.updated_at or _OLDEST, n.note_id) > (after, after_note_id)]
            else:
                notes = [n for n in notes if (n.updated_at or _OLDEST) > after]
        return [_lean(n) for n in notes[:limit]]

    async def embedding_pending(self, user_id: str, limit: int) -> List[DriveNote]:
        return [n.model_copy(deep=True) for n in self._user(user_id) if n.embedding_pending][:limit]

    async def archived(self, user_id: str, limit: int) -> List[DriveNote]:
        found = [n for n in self._user(user_id) if n.state == NoteState.ARCHIVED.value]
        # Firestore orders a null below every timestamp; ties break on the document id, descending with the order
        found.sort(key=lambda n: (n.archived_at or _OLDEST, n.note_id), reverse=True)
        return [_lean(n) for n in found[:limit]]

    async def active(self, user_id: str, limit: int) -> List[DriveNote]:
        found = [n for n in self._user(user_id) if n.state == NoteState.ACTIVE.value]
        found.sort(key=lambda n: (n.updated_at or _OLDEST, n.note_id), reverse=True)
        return [_lean(n) for n in found[:limit]]

    async def find_nearest(self, user_id: str, vector: Sequence[float], *, state: str, embedding_space: str,
                           limit: int) -> List[Tuple[DriveNote, float]]:
        query = [float(x) for x in vector]
        scored: List[Tuple[float, str, DriveNote]] = []
        for note in self._user(user_id):
            if note.state != state or note.embedding_space != embedding_space or not note.note_vector:
                continue
            v = note.note_vector
            dot = sum(a * b for a, b in zip(v, query))
            na, nb = math.sqrt(sum(a * a for a in v)), math.sqrt(sum(b * b for b in query))
            scored.append((1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb), note.note_id, note))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [(_lean(n), d) for d, _, n in scored[:limit]]

    # -- lifecycle --------------------------------------------------------------------------------------------------------

    async def archive(self, user_id: str, note_id: str, *, archived_at: datetime, last_seen_at: Optional[datetime],
                      snapshot: Optional[NoteSnapshot] = None) -> bool:
        note = self.notes.get((user_id, note_id))
        if note is None or note.erase_pending:
            return False
        if note.state == NoteState.ACTIVE.value:
            update: Dict[str, object] = {"state": NoteState.ARCHIVED.value}
        elif note.state == NoteState.CLEARED.value and note.archived_at is None:
            update = {}
        else:
            return False
        update.update({"archived_at": archived_at, "last_seen_at": last_seen_at})
        if snapshot is not None:
            update["snapshot"] = snapshot
        self._lifecycle(note, update)
        return True

    async def reactivate(self, user_id: str, note_id: str, *, bound_at: datetime) -> bool:
        note = self.notes.get((user_id, note_id))
        if note is None or note.erase_pending:
            return False
        if note.state == NoteState.ARCHIVED.value and note.binding is not None:
            binding = note.binding.model_copy(update={"bound_by": BoundBy.RESTORE.value, "bound_at": bound_at})
            self._lifecycle(note, {"state": NoteState.ACTIVE.value, "archived_at": None, "binding": binding})
            return True
        if note.state == NoteState.CLEARED.value and note.archived_at is not None:
            self._lifecycle(note, {"archived_at": None})
            return True
        return False

    async def refresh_snapshot(self, user_id: str, note_id: str, snapshot: NoteSnapshot) -> bool:
        note = self.notes.get((user_id, note_id))
        if note is None or note.erase_pending:
            return False
        self._lifecycle(note, {"snapshot": snapshot})
        return True

    async def set_vector(self, user_id: str, note_id: str, vector: Sequence[float], embedding_space: str, *,
                         expected_version: int) -> bool:
        note = self.notes.get((user_id, note_id))
        if (note is None or note.erase_pending or note.state == NoteState.CLEARED.value
                or note.current_version != expected_version):
            return False
        self._lifecycle(note, {"note_vector": [float(x) for x in vector], "embedding_space": embedding_space,
                               "embedding_pending": False})
        return True

    # -- erase ------------------------------------------------------------------------------------------------------------

    async def erase(self, user_id: str, note_ids: Sequence[str]) -> EraseResult:
        erased = 0
        left: List[str] = []
        for note_id in dict.fromkeys(note_ids):
            try:
                done = await self._erase_one(user_id, note_id)
            except DriveNoteConflict:
                left.append(note_id)
                continue
            erased += int(done)
        return EraseResult(erased=erased, left=tuple(left))

    async def _erase_one(self, user_id: str, note_id: str) -> bool:
        """One note's erase (the seam a test makes conflict, as Firestore's can)."""
        note = self.notes.get((user_id, note_id))
        if note is None:
            return False
        self.versions.pop((user_id, note_id), None)
        self._put(erase_stub(note))
        return True

    async def delete_stub(self, user_id: str, note_id: str) -> bool:
        note = self.notes.get((user_id, note_id))
        if note is None or not note.erase_pending:
            return False
        self.versions.pop((user_id, note_id), None)
        del self.notes[(user_id, note_id)]
        return True
