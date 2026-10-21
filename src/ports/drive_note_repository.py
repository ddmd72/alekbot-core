"""
DriveNoteRepository — the owner's notes on drive items (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.1, §3.2, §3.5, §4.4,
§4.8, §5.1, §6.4, §7).

`{prefix}drive_notes/{user_id}:{note_id}` with SCD2 versions under `versions/v{n}`. Durable (D3): no index rebuild, opt-out,
disconnect or provider switch touches it; only the owner's explicit erase and account deletion delete notes. **When an
account-deletion flow is built, it must delete this collection.** Never given to the consolidator, the biographical
cache, SearchEnrichmentService or the remote MCP server (§1.4).

Every method takes `user_id` and touches only that user's documents (multi-tenant rule: the document id carries the
user, and every query filters `user_id ==`). `updated_at` is set by the repository on every parent write — a server
timestamp in Firestore, a strictly increasing clock in memory — never by the caller: it is the indexer's watermark
(§5.1 b). `revision` is bumped by the repository on EVERY parent write too (owner writes, lifecycle writes, erase): it
is what `write_version` compares, so an owner write prepared before a lifecycle write fails instead of overwriting it.
Lifecycle writes stamp `last_writer` with origin `indexer`. A missing composite index or a backend error leaves a method
as DriveIndexStoreUnavailable; contention that outlasts the backend's own transaction retries is DriveNoteConflict.

Public interface:
    get / get_many / find_bound / find_by_match_keys / note_ids / count     (reads by id and by key; §4.4, §5.1 a)
    get_version / list_versions                                             (history; §4.4 undo, §4.8 erase)
    write_version                                                           (the one owner-write transaction; §4.4 step 5)
    changed_since / embedding_pending / archived / active / find_nearest    (the tick and the deleted-files search)
    archive / reactivate / refresh_snapshot / set_vector                    (lifecycle: the parent in place, no version)
    erase / delete_stub                                                     (§4.8)
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.drive_note import DriveNote, DriveNoteVersion, EraseResult, NoteSnapshot


class DriveNoteRepository(ABC):

    # -- reads by id and by key ---------------------------------------------------------------------------------------

    @abstractmethod
    async def get(self, user_id: str, note_id: str) -> Optional[DriveNote]:
        """The parent document, or None (an erase stub is returned: `erase_pending` tells it apart)."""

    @abstractmethod
    async def get_many(self, user_id: str, note_ids: Sequence[str]) -> Dict[str, DriveNote]:
        """The existing notes among `note_ids`, keyed by note id; a missing id is absent from the result."""

    @abstractmethod
    async def find_bound(self, user_id: str, binding_key: str) -> Optional[DriveNote]:
        """The note whose `match_keys` hold the binding key `i:{provider}:{drive_id}:{item_id}` (one `array-contains`
        query), stubs included. One lineage per item (N4); were there several, the lowest note id is returned."""

    @abstractmethod
    async def find_by_match_keys(self, user_id: str, keys: Sequence[str]) -> List[DriveNote]:
        """Every note whose `match_keys` hold any of `keys` (`i:` binding keys or `r:` ref keys), stubs included,
        deduplicated, ordered by note id. Any number of keys: the adapter batches them 30 per `array-contains-any`."""

    @abstractmethod
    async def note_ids(self, user_id: str) -> List[str]:
        """The ids of every note of the user that is not an erase stub, ordered (erase-all, §4.8)."""

    @abstractmethod
    async def count(self, user_id: str) -> int:
        """Every note document of the user, stubs included (one aggregation; NOTES_PER_USER_MAX, the tick's gate)."""

    # -- history --------------------------------------------------------------------------------------------------------

    @abstractmethod
    async def get_version(self, user_id: str, note_id: str, version: int) -> Optional[DriveNoteVersion]:
        """`versions/v{version}` of the note, or None."""

    @abstractmethod
    async def list_versions(self, user_id: str, note_id: str) -> List[DriveNoteVersion]:
        """Every stored version of the note, by version ascending."""

    # -- the owner-write transaction ----------------------------------------------------------------------------------

    @abstractmethod
    async def write_version(self, note: DriveNote, version: DriveNoteVersion, *, expected_version: int) -> None:
        """One transaction (§4.4 step 5). Preconditions, else DriveNoteConflict and nothing is written: the stored
        `current_version` (0 for a missing document or an erase stub) equals `expected_version`, AND the stored
        `revision` (0 for a missing document) equals `note.revision` — the revision the caller read, so any parent
        write since that read (another owner write, archive, re-activation, snapshot refresh, vector repair, erase)
        fails the write. A first write (`expected_version` 0, `note.revision` 0) therefore needs the document not to
        exist (or to be the stub the caller read). It then closes v(expected_version) when it exists
        (`valid_to = version.valid_from`, `is_current = False`; a missing one is tolerated; nothing to close at 0), adds
        `version` (`version.version` must be `expected_version + 1`, else ValueError) and replaces the parent with
        `note` (`note.current_version` must equal `version.version`), `revision = note.revision + 1`, `updated_at` set
        by the repository."""

    # -- the tick (§5.1) and the deleted-files search (§6.4) -----------------------------------------------------------

    @abstractmethod
    async def changed_since(self, user_id: str, after: Optional[datetime], limit: int, *,
                            after_note_id: str = "") -> List[DriveNote]:
        """Notes ordered by (`updated_at`, `note_id`) ascending, at most `limit`, stubs included, vectors left out.
        `after` None reads from the start. With `after_note_id` "" the result starts strictly after `after`; with it,
        strictly after the pair (`after`, `after_note_id`): the page cursor of a re-read, so a page of notes that share
        one timestamp still advances (index 2)."""

    @abstractmethod
    async def embedding_pending(self, user_id: str, limit: int) -> List[DriveNote]:
        """Notes with `embedding_pending` (the vector repair, §5.1 b step 5; index 5), at most `limit`, by note id."""

    @abstractmethod
    async def archived(self, user_id: str, limit: int) -> List[DriveNote]:
        """`archived` notes, newest `archived_at` first, at most `limit`, vectors left out (index 3)."""

    @abstractmethod
    async def active(self, user_id: str, limit: int) -> List[DriveNote]:
        """`active` notes, latest `updated_at` first, at most `limit`, vectors left out (index 4): the deleted-files
        search filters them for dormant ones and the audit for record-less ones, both in memory."""

    @abstractmethod
    async def find_nearest(self, user_id: str, vector: Sequence[float], *, state: str, embedding_space: str,
                           limit: int) -> List[Tuple[DriveNote, float]]:
        """The nearest notes by cosine distance on `note_vector`, prefiltered on `user_id`, `state` and
        `embedding_space` (equality only; the vector index of §3.5 item 6), with their distance, nearest first."""

    # -- lifecycle: the parent in place, never a version (N5) -------------------------------------------------------------

    @abstractmethod
    async def archive(self, user_id: str, note_id: str, *, archived_at: datetime, last_seen_at: Optional[datetime],
                      snapshot: Optional[NoteSnapshot] = None) -> bool:
        """The item is gone (§7.1). An `active` note → `archived` with `archived_at`, `last_seen_at` and the snapshot
        when given. A `cleared` note (not a stub) that carries no `archived_at` keeps `cleared` and is stamped
        `archived_at` / `last_seen_at` (and the snapshot), so an undo later brings it back archived. One transaction;
        `revision`, `updated_at` bumped, `last_writer` = indexer. Anything else (archived already, a stamped cleared
        note, a stub, a missing note) is left as it is: False."""

    @abstractmethod
    async def reactivate(self, user_id: str, note_id: str, *, bound_at: datetime) -> bool:
        """The item is back (§7.2). An `archived` note → `active`: `binding.bound_by = restore`, `binding.bound_at`,
        `archived_at` cleared (`last_seen_at` kept). A `cleared` note with `archived_at` set keeps `cleared` and loses
        `archived_at` (an undo later brings it back active). One transaction; `revision`, `updated_at` bumped,
        `last_writer` = indexer. Anything else: False."""

    @abstractmethod
    async def refresh_snapshot(self, user_id: str, note_id: str, snapshot: NoteSnapshot) -> bool:
        """Replace the snapshot of a note that is not a stub (§7.5); `revision`, `updated_at` bumped, `last_writer` =
        indexer. False for a stub or none."""

    @abstractmethod
    async def set_vector(self, user_id: str, note_id: str, vector: Sequence[float], embedding_space: str, *,
                         expected_version: int) -> bool:
        """Store a repaired vector (§5.1 b step 5): `note_vector`, `embedding_space`, `embedding_pending = False`, on
        an `active` or `archived` note whose stored `current_version` is still `expected_version` — the version whose
        text was embedded. A newer version (its own embedding pending or done), a cleared note or a stub stores nothing:
        False. One transaction; `revision`, `updated_at` bumped, `last_writer` = indexer."""

    # -- erase (§4.8) -----------------------------------------------------------------------------------------------------

    @abstractmethod
    async def erase(self, user_id: str, note_ids: Sequence[str]) -> EraseResult:
        """For each existing note: delete every version (they are `v1`..`v{current_version}`) and replace the parent
        with its contentless stub (`drive_note.erase_stub`, `revision` and `updated_at` bumped), in one transaction per
        note, so a
        concurrent write either lands before (and is erased) or after (on top of the stub). A history over one
        transaction's writes loses its oldest versions first, and the stub is written last: an interrupted erase
        leaves the note as it was, minus old versions, never a stub with history under it; when a write lands between
        those batches and the stub (the version count moved), the batches are redone. Idempotent.

        **Never stops part-way:** a note that cannot be erased — a conflict that outlasts the retries
        (DriveNoteConflict), or a store error on that note (DriveIndexStoreUnavailable) — is logged and its id
        reported in `EraseResult.left`; the remaining notes are still erased. Returns `EraseResult(erased, left)`:
        `erased` counts the notes erased (stubs included, missing ids not)."""

    @abstractmethod
    async def delete_stub(self, user_id: str, note_id: str) -> bool:
        """Delete an erase stub once its record copy is cleared, in a transaction that re-reads it: a note that is not
        (or no longer) a stub — a new note written on top of it — is never deleted: False."""
