"""
DriveNoteDeletedSearch — the owner-notes source of the deleted-files search (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4,
D9, D16, D21; plan part C). A `DeletedSource` of DriveSearchService (`add_deleted_source`): the deleted-files
search of the drive specialist, one section, one result list with the archived transcripts (§0.3). It reads the note
store only, never the index, so it needs neither the opt-in flag nor a connection (it runs before the index gate).

- Archived notes: `archived(user_id, ARCHIVE_SCAN_MAX)`, newest first.
- Dormant notes (D16): active notes bound to a drive that is not the connected one, labelled. While disconnected there
  is nothing to compare with: none is shown, and the result says so.
- Strict AND over the note's keys and its last known name's keys (`drive_note_render.note_matches`), no allowances.
- Nothing exact: the meaning fallback — one `find_nearest` on `note_vector`, prefiltered on `state = archived` and the
  current embedding space (a vector of another space is never compared), at most MEANING_MAX lines labelled "closest
  by meaning, unverified". Dormant notes have no meaning fallback (keys only).
- An empty query reads the newest NEWEST_MAX and shows the newest DELETED_SHOW_MAX (5), the rest counted.
- Every entry carries the note's `drive_note:` ref, which works only with the note tools (D21: read, change, undo or
  remove the note of a gone file); the note is data inside the wrapper (§6.3). Logs carry counts, never text.

Public interface:
    DriveNoteDeletedSearch(notes, embeddings, *, embedding_space, store_keys, provider_key, nonce)
    await (user_id, concepts, connection) -> (hits, coverage lines)       # the DeletedSource signature
"""
from __future__ import annotations

import secrets
from typing import Callable, List, Sequence, Tuple

from ..domain.drive_deleted_search import DeletedHit
from ..domain.drive_index import DriveIndexStoreUnavailable
from ..domain.drive_index_params import DELETED_SHOW_MAX
from ..domain.drive_keys import name_keys_v2
from ..domain.drive_match import Concept
from ..domain.drive_note import (
    ARCHIVE_SCAN_MAX, NOTES_PER_USER_MAX, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteState, binding_parts_of,
)
from ..domain.drive_note_merge import note_store_keys
from ..domain.drive_note_render import (
    ARCHIVE_SCAN_CUT_LINE, DORMANT_HIDDEN_LINE, MEANING_LINE, MORE_NOTES_LINE, StoreKeys, matched_line, note_hit,
    note_matches, search_keys,
)
from ..domain.user_drive import DriveConnection
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import exc_brief, logger

MEANING_MAX = 2          # §6.4 step 3
NEWEST_MAX = 10          # §6.4 step 4
UNAVAILABLE_LINE = "Notes on deleted files could not be searched right now."


def _nonce() -> str:
    return secrets.token_hex(8)


class DriveNoteDeletedSearch:
    def __init__(self, notes: DriveNoteRepository, embeddings: EmbeddingService, *, embedding_space: str,
                 store_keys: StoreKeys = note_store_keys, provider_key: str = ONEDRIVE_PROVIDER_KEY,
                 nonce: Callable[[], str] = _nonce) -> None:
        """`embedding_space` is the space query vectors are embedded in (the adapter's EMBEDDING_SPACE): the meaning
        fallback compares only note vectors in it. `store_keys` is part B's `drive_note_merge.note_store_keys`, used when
        a note's stored keys are of another layout; injectable for tests only."""
        self._notes = notes
        self._embeddings = embeddings
        self._space = embedding_space
        self._store_keys = store_keys
        self._provider = provider_key
        self._nonce = nonce

    async def __call__(self, user_id: str, concepts: Sequence[Concept],
                       connection: DriveConnection) -> Tuple[List[DeletedHit], List[str]]:
        drive_id = connection.drive_id if connection.connected else ""
        try:
            archived = await self._notes.archived(user_id, ARCHIVE_SCAN_MAX)
            active = await self._notes.active(user_id, NOTES_PER_USER_MAX)
        except DriveIndexStoreUnavailable as exc:
            logger.warning(f"[DriveNote] deleted-files search for {user_id[:8]}: note store unavailable: "
                           f"{exc_brief(exc)}")
            return [], [UNAVAILABLE_LINE]
        coverage: List[str] = []
        dormant = [n for n in active if self._elsewhere(n, drive_id)] if drive_id else []
        if not drive_id and active:
            coverage.append(DORMANT_HIDDEN_LINE)
        if len(archived) >= ARCHIVE_SCAN_MAX:
            coverage.append(ARCHIVE_SCAN_CUT_LINE.format(limit=ARCHIVE_SCAN_MAX))
        pool = [(n, False) for n in archived] + [(n, True) for n in dormant]
        found: List[Tuple[DriveNote, bool, str]] = []
        if not concepts:
            found = [(n, d, "") for n, d in pool[:NEWEST_MAX]]
        else:
            for note, is_dormant in pool:
                keys = search_keys(note, self._store_keys)
                if note_matches(concepts, keys):
                    name_keys = frozenset(name_keys_v2(note.snapshot.name)) if note.snapshot and note.snapshot.name \
                        else frozenset()
                    found.append((note, is_dormant, matched_line(concepts, keys - name_keys, name_keys)))
        meaning = 0
        if concepts and not found and archived:
            nearest = await self._nearest(user_id, concepts)
            found = [(n, False, MEANING_LINE) for n in nearest]
            meaning = len(found)
        shown = found[:DELETED_SHOW_MAX]
        if len(found) > len(shown):
            coverage.append(MORE_NOTES_LINE.format(count=len(found) - len(shown)))
        hits = [note_hit(n, dormant=d, nonce=self._nonce(), matched=m, day=_gone_day(n) if not d else "")
                for n, d, m in shown]
        logger.info(f"[DriveNote] deleted-files search user={user_id[:8]} archived={len(archived)} "
                    f"dormant={len(dormant)} matched={len(found)} meaning={meaning} shown={len(hits)} "
                    f"connected={bool(drive_id)}")
        return hits, coverage

    def _elsewhere(self, note: DriveNote, drive_id: str) -> bool:
        """An active note bound to a drive that is not the connected one (D16), erase stubs excepted."""
        if note.erase_pending or note.state != NoteState.ACTIVE.value:
            return False
        parts = binding_parts_of(note)
        return parts is not None and (parts.provider_key != self._provider or parts.drive_id != drive_id)

    async def _nearest(self, user_id: str, concepts: Sequence[Concept]) -> List[DriveNote]:
        """The meaning fallback (§6.4 step 3): archived notes only, in the current space; a failure costs only this
        fallback."""
        query = " ".join(c.word for c in concepts)
        try:
            vector = await self._embeddings.get_embedding(query, task_type="RETRIEVAL_QUERY")
            near = await self._notes.find_nearest(user_id, vector, state=NoteState.ARCHIVED.value,
                                                  embedding_space=self._space, limit=MEANING_MAX)
        except Exception as exc:   # an embedding or store failure: the exact search already said nothing matched
            logger.warning(f"[DriveNote] deleted-files meaning fallback for {user_id[:8]} failed: {exc_brief(exc)}")
            return []
        return [n for n, _ in near]


def _gone_day(note: DriveNote) -> str:
    """The date a deleted entry shows (§6.4): when the file was last seen, else (an older note) when it was archived."""
    when = note.last_seen_at or note.archived_at
    return when.date().isoformat() if when else ""
