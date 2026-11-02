"""
DriveNoteDeletedSearch — the owner-notes source of the deleted-files search (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §6.4,
D9, D16, D21; plan part C). A `DeletedSource` of DriveSearchService (`add_deleted_source`): the deleted-files
search of the drive specialist, one section, one result list with the archived transcripts (§0.3). It reads the note
store only, never the index, so it needs neither the opt-in flag nor a connection (it runs before the index gate).

- Archived notes: `archived(user_id, ARCHIVE_SCAN_MAX)`, newest first.
- Dormant notes (D16): active notes bound on a connection that is not the searched one (MULTI_DRIVE_RFC §9: by the ref
  tag of their `r:` key), labelled. With no connection there is nothing to compare with: none is shown, and the result
  says so.
- Strict AND over the note's keys and its last known name's keys (`drive_note_render.note_matches`), no allowances.
- Nothing exact: the meaning fallback — one `find_nearest` on `note_vector`, prefiltered on `state = archived` and the
  current embedding space (a vector of another space is never compared), at most MEANING_MAX lines labelled "closest
  by meaning, unverified". Dormant notes have no meaning fallback (keys only).
- An empty query reads the newest NEWEST_MAX and shows the newest DELETED_SHOW_MAX (5), the rest counted.
- Every entry carries the note's `drive_note:` ref, which works only with the note tools (D21: read, change, undo or
  remove the note of a gone file); the note is data inside the wrapper (§6.3). Logs carry counts, never text.

Public interface:
    DriveNoteDeletedSearch(notes, embeddings, *, embedding_space, store_keys, nonce, mount_name_of)
    await (user_id, concepts, connection) -> (hits, coverage lines)       # the DeletedSource signature (connection: the
                                                                          # searched one, or None)
"""
from __future__ import annotations

import secrets
from dataclasses import replace
from typing import AbstractSet, Awaitable, Callable, List, Optional, Sequence, Tuple

from ..domain.drive_deleted_search import DeletedHit
from ..domain.drive_index import DriveIndexStoreUnavailable
from ..domain.drive_index_params import DELETED_SHOW_MAX
from ..domain.drive_keys import name_keys_v2
from ..domain.drive_match import Concept
from ..domain.drive_connection import DriveConnection
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_mounts import canonical_path
from ..domain.drive_note import (
    ARCHIVE_SCAN_MAX, NOTES_PER_USER_MAX, DriveNote, NoteState, is_drive_note, note_ref_tag, on_connection,
)
from ..domain.drive_note_merge import note_store_keys
from ..domain.drive_note_render import (
    ARCHIVE_SCAN_CUT_LINE, DORMANT_HIDDEN_LINE, MEANING_LINE, MORE_NOTES_LINE, StoreKeys, matched_line, note_hit,
    note_matches, search_keys,
)
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import exc_brief, logger

MountNameOf = Callable[[str, str], Awaitable[Optional[str]]]   # (user_id, ref tag) -> the connection's mount name

LiveTags = Callable[[str], Awaitable[AbstractSet[str]]]          # user_id -> the ref tags of the live connections

MEANING_MAX = 2          # §6.4 step 3
NEWEST_MAX = 10          # §6.4 step 4
UNAVAILABLE_LINE = "Notes on deleted files could not be searched right now."


def _nonce() -> str:
    return secrets.token_hex(8)


class DriveNoteDeletedSearch:
    def __init__(self, notes: DriveNoteRepository, embeddings: EmbeddingService, *, embedding_space: str,
                 store_keys: StoreKeys = note_store_keys, nonce: Callable[[], str] = _nonce,
                 mount_name_of: Optional[MountNameOf] = None, live_tags: Optional[LiveTags] = None) -> None:
        """`embedding_space` is the space query vectors are embedded in (the adapter's EMBEDDING_SPACE): the meaning
        fallback compares only note vectors in it. `store_keys` is part B's `drive_note_merge.note_store_keys`, used when
        a note's stored keys are of another layout; injectable for tests only. `mount_name_of(user_id, tag)` is the
        current mount name of the connection a ref tag names (None: unknown): an entry's stored (connection-relative)
        path is printed as `<mount>/<path>` (multi-drive 2a, E-11); without it, or for an unknown tag, the stored path.
        `live_tags(user_id)` gives the ref tags of the user's live connections (multi-drive 2b, E-9): an active note is
        dormant when its connection is not among them, and a note on another live drive is not; without it the old
        rule holds (dormant = bound on a connection other than the searched one). A whole-drive note is never dormant."""
        self._notes = notes
        self._embeddings = embeddings
        self._space = embedding_space
        self._store_keys = store_keys
        self._nonce = nonce
        self._mount_name_of = mount_name_of
        self._live_tags = live_tags

    async def __call__(self, user_id: str, concepts: Sequence[Concept],
                       connection: Optional[DriveConnection]) -> Tuple[List[DeletedHit], List[str]]:
        tag = connection.ref_tag if connection is not None and DriveConnectionStatus.of(connection).connected else ""
        try:
            archived = await self._notes.archived(user_id, ARCHIVE_SCAN_MAX)
            active = await self._notes.active(user_id, NOTES_PER_USER_MAX)
        except DriveIndexStoreUnavailable as exc:
            logger.warning(f"[DriveNote] deleted-files search for {user_id[:8]}: note store unavailable: "
                           f"{exc_brief(exc)}")
            return [], [UNAVAILABLE_LINE]
        coverage: List[str] = []
        live = await self._live(user_id)
        if live is not None:
            dormant = [n for n in active if self._not_live(n, live)]
        else:
            dormant = [n for n in active if self._elsewhere(n, tag)] if tag else []
            if not tag and active:
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
        hits = [await self._mounted(user_id, n, note_hit(n, dormant=d, nonce=self._nonce(), matched=m,
                                                         day=_gone_day(n) if not d else "",
                                                         drive_name=await self._drive_name(user_id, n)))
                for n, d, m in shown]
        logger.info(f"[DriveNote] deleted-files search user={user_id[:8]} archived={len(archived)} "
                    f"dormant={len(dormant)} matched={len(found)} meaning={meaning} shown={len(hits)} "
                    f"connected={bool(tag)}")
        return hits, coverage

    async def _mounted(self, user_id: str, note: DriveNote, hit: DeletedHit) -> DeletedHit:
        """The hit with its stored path canonical, by the mount of the connection the note is bound on."""
        tag = note_ref_tag(note)
        if self._mount_name_of is None or not tag or not hit.path:
            return hit
        name = await self._mount_name_of(user_id, tag)
        return replace(hit, path=canonical_path(name, hit.path)) if name else hit

    async def _live(self, user_id: str) -> Optional[AbstractSet[str]]:
        """The ref tags of the live connections; None when not wired or the read failed (the old rule then applies)."""
        if self._live_tags is None:
            return None
        try:
            return await self._live_tags(user_id)
        except Exception as exc:
            logger.warning(f"[DriveNote] live connections read failed for {user_id[:8]}: {exc_brief(exc)}")
            return None

    async def _drive_name(self, user_id: str, note: DriveNote) -> str:
        """A whole-drive note's drive name (its last one, a disconnected drive's included); "" for any other note."""
        if self._mount_name_of is None or not is_drive_note(note):
            return ""
        return await self._mount_name_of(user_id, note_ref_tag(note)) or ""

    @staticmethod
    def _not_live(note: DriveNote, live: AbstractSet[str]) -> bool:
        """An active item note bound on a connection that is not live (E-9), erase stubs and drive notes excepted."""
        if note.erase_pending or note.state != NoteState.ACTIVE.value or is_drive_note(note):
            return False
        return note.binding is not None and note_ref_tag(note) not in live

    @staticmethod
    def _elsewhere(note: DriveNote, tag: str) -> bool:
        """An active note bound on a connection that is not the searched one (D16), erase stubs and drive notes
        excepted."""
        if note.erase_pending or note.state != NoteState.ACTIVE.value or is_drive_note(note):
            return False
        return note.binding is not None and not on_connection(note, tag)

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
