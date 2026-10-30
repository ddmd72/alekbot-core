"""
DriveNoteService — the write path of the owner's notes on drive items (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §4.4,
§4.5, §4.6, §4.8; D3, D11-D17, D20).

Each note tool of the drive specialist calls one method: `get` reads a note from the store alone; `create` and `update`
write a note through the shared `_write` loop (`set` is the whole-triple write both are built beside); `delete_note` →
`clear`; `undo_note` → `undo`; `erase_note` → `request_erase`. `$drive-notes erase-all` → `request_erase_all`; pasting a
code → `erase`. **Only `set`, `create` and `update` (through `_write`) touch the drive**: get, clear, undo and the erase
methods resolve the note from the store alone (a `drive_note:<id>` ref, or a `drive:<tag>:<id>` ref through the ref key
`r:{tag}:{id}`), so they work on archived and dormant notes and while the drive is disconnected (D3, D17).

Connections (MULTI_DRIVE_RFC §7.2, §9, §11.6): the live path resolves a `drive:<tag>:<id>` ref to its connection
through the injected DriveConnectionService (an untagged ref, or one of a connection that is gone, is refused with the
code-built line, D13), reads the item through the connection's handle and binds the note by the item's identity
(`i:{identity}`) and the connection's tag (`r:{tag}:{id}`). A note belongs to the connection whose tag it carries: the
indexing switch, the index record and the nudge are that connection's.

Guards (§4.5) that live here: the security check before any drive I/O (layer 4) and the per-user lineage cap. The owner-
turn gate (layer 1), the per-turn write cap and the per-execution lock are the tool handlers'.
Every receipt carries the stored triple byte for byte (layer 5); every version its provenance (layer 6).

Writes go through `DriveNoteRepository.write_version` (one transaction, precondition on `current_version`); a conflict
is retried once from the lineage lookup, then refused. After a write the indexer is nudged with the item id, through the
same `drive_sync` task as UserDriveService's nudge; a nudge never raises (the next fan-out merges). Logs carry ids,
counts, versions and statuses, never the note's text.

Public interface:
    set(user_id, account_id, ref, note, doc_type="", life_areas=(), provenance=NoteProvenance()) -> NoteReceipt
    get(user_id, ref) -> Optional[NoteView]
    create(user_id, account_id, ref, content, provenance=NoteProvenance()) -> NoteReceipt
    update(user_id, account_id, ref, change, provenance=NoteProvenance(), *, expected_version=None) -> NoteReceipt
    clear(user_id, account_id, ref, provenance=NoteProvenance()) -> NoteReceipt
    undo(user_id, account_id, ref, provenance=NoteProvenance()) -> NoteReceipt
    request_erase(user_id, ref) -> EraseRequestReceipt
    request_erase_all(user_id) -> EraseRequestReceipt
    erase(user_id, code) -> int
    note_for_item(user_id, identity) -> Optional[DriveNote]
Refusals raise DriveNoteRefused (a NoteRefusal reason and its detail); nothing is stored when one is raised.
DriveIndexStoreUnavailable from the note store passes through every method unchanged (the caller reports "try again";
an index-store failure on the record read is absorbed, see `_record`).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from ..domain import drive_ref
from ..domain.drive_connection import DriveConnection
from ..domain.drive_index import DriveChange, DriveIndexRecord, Nudge, inaccessible
from ..domain.drive_index_params import ITEM_CALLS_PER_TICK, TICK_DEADLINE_S
from ..domain.drive_note import (
    ERASE_CODE_TTL, ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, NOTES_PER_USER_MAX, STATUS_ARCHIVED,
    STATUS_OTHER_DRIVE, DriveNote,
    DriveNoteConflict, DriveNoteVersion,
    DriveNoteRefused, EraseRequestReceipt, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteReceipt,
    NoteRefusal, NoteSnapshot, NoteState, NoteTarget, content_of, deterministic_note_id, item_binding_key, item_ref,
    next_version, note_after_write, note_content, note_embedding_text, note_ref_tag, note_word_count,
    parse_note_target, searchability, snapshot_of, state_after, tagged_ref_key, undo_source,
)
from ..domain.drive_ref_refusal import DriveRefRefusal, stale_ref_line
from ..domain.drive_note_change import NoteChange, apply_change
from ..domain.drive_note_change_kind import NoteChangeKind
from ..domain.drive_note_merge import note_store_keys
from ..domain.drive_note_view import NoteView
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_task_key, scope_payload
from ..domain.prompt_v3.security import TrustZone
from ..domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNotConnectedError
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.ephemeral_store import EphemeralStore
from ..ports.security_port import SecurityPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .drive_connection_service import DriveConnectionService

_ERASE_CODE_ATTEMPTS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_note_id() -> str:
    return uuid.uuid4().hex


def _new_code() -> str:
    return secrets.token_hex(3)


class _LiveItem(NamedTuple):
    """A `drive:<tag>:<id>` ref resolved on its connection (§4.4 step 2): what the live write path binds the note to.
    `key` is the binding key `i:{identity}`."""
    connection: DriveConnection
    item_id: str
    identity: str
    record: Optional[DriveIndexRecord]
    snapshot: NoteSnapshot
    key: str

    @property
    def ref(self) -> str:
        return drive_ref.format(self.connection.ref_tag, self.item_id)


class DriveNoteService:
    def __init__(self, notes: DriveNoteRepository, drive: UserDrivePort, index: DriveIndexRepository,
                 embeddings: EmbeddingService, security: SecurityPort, connections: "DriveConnectionService", *,
                 embedding_space: str,
                 nudge_queue: Optional[TaskQueue] = None, erase_requests: Optional[EphemeralStore] = None,
                 clock: Callable[[], datetime] = _utcnow,
                 new_note_id: Callable[[], str] = _new_note_id, new_code: Callable[[], str] = _new_code) -> None:
        """`drive` is the router over every drive kind, addressed by a connection's handle; `connections` resolves refs
        and gives each connection's indexing switch (MULTI_DRIVE_RFC §6, §9). `embedding_space` is the space
        `embeddings` writes (the adapter's EMBEDDING_SPACE). `index` is read only, per connection: the inaccessible
        check (D20) and the searchability status. `nudge_queue` is the drive queue (optional, as in UserDriveService);
        `erase_requests` the ephemeral store of `{prefix}drive_note_erase_requests` (without it the erase methods
        refuse)."""
        self._notes = notes
        self._drive = drive
        self._index = index
        self._embeddings = embeddings
        self._security = security
        self._connections = connections
        self._space = embedding_space
        self._nudge_queue = nudge_queue
        self._erase_requests = erase_requests
        self._clock = clock
        self._new_note_id = new_note_id
        self._new_code = new_code

    # ------------------------------------------------------------------------------------------------------------------
    # set (§4.4 steps 1-8)
    # ------------------------------------------------------------------------------------------------------------------

    async def set(self, user_id: str, account_id: str, ref: str, note: str, doc_type: str = "",
                  life_areas: Sequence[str] = (), provenance: NoteProvenance = NoteProvenance()) -> NoteReceipt:
        """Replace the note `ref` names with the triple (an empty triple is `clear`).

        A `drive:<tag>:<id>` ref resolves the live item on its connection (refused when the ref names no live
        connection, the drive is not reachable, the item is not on it or the bot cannot open it) and creates the item's note or replaces it. A
        `drive_note:<id>` ref edits that existing note from the store alone, with no drive call (I5, owner ruling
        2026-10-21): an archived note stays archived, a note bound to another drive stays bound there; the only thing
        never done without the live file is creating a NEW note. An identical triple writes nothing (a failover re-run
        never doubles versions)."""
        content = note_content(note, doc_type, life_areas)
        if content.is_empty:
            return await self.clear(user_id, account_id, ref, provenance)
        target = self._target(ref)
        if content.text:
            await self._check(user_id, content.text)                       # step 1: before any drive I/O
        return await self._write(user_id, account_id, target, lambda _current: content, provenance, allow_new=True)

    # ------------------------------------------------------------------------------------------------------------------
    # get, create, update (specialist RFC §9.2: one tool per edit kind)
    # ------------------------------------------------------------------------------------------------------------------

    async def get(self, user_id: str, ref: str) -> Optional[NoteView]:
        """The note `ref` names, from the store alone (no drive call): active, archived or cleared (a cleared note is
        returned as cleared), also while the drive is disconnected. None when there is no note or only an erase stub.
        A `drive:` ref of a note on a drive that is not connected, or of several notes, is refused as `_resolve_note`
        refuses it (with each note's own `drive_note:` ref)."""
        target = self._target(ref)
        if target.note_id:
            note = await self._notes.get(user_id, target.note_id)
        else:
            note = await self._resolve_note(user_id, target)
        if note is None or note.erase_pending:
            logger.info(f"[DriveNote] get user={user_id[:8]} note=- found=False")
            return None
        view = NoteView.of(note)
        logger.info(f"[DriveNote] get user={user_id[:8]} note={view.note_id} v={view.version} state={view.state}")
        return view

    async def create(self, user_id: str, account_id: str, ref: str, content: NoteContent,
                     provenance: NoteProvenance = NoteProvenance()) -> NoteReceipt:
        """Add a note where there is none. `content` is already validated (`note_content`). A cleared note reuses its
        lineage; content identical to the note's is a no-op receipt; a note holding something else is refused
        (NOTE_EXISTS: change it with the edit tools). `ref` follows `set`: a `drive:` ref needs the live file on the
        connected drive, a `drive_note:` ref only revives that note from the store (never a NEW note)."""
        if content.is_empty:
            raise DriveNoteRefused(NoteRefusal.EMPTY_CHANGE,
                                   "There is nothing to write: give the note's text, its type or its life areas.")
        target = self._target(ref)
        if content.text:
            await self._check(user_id, content.text)                       # before any drive I/O

        def compose(current: Optional[DriveNote]) -> NoteContent:
            if current is None or current.state == NoteState.CLEARED.value or current.content == content:
                return content
            raise DriveNoteRefused(NoteRefusal.NOTE_EXISTS,
                                   "The file already has a note: use add_to_note, replace_note_text or the type and "
                                   "area tools to change it.", note_ref=current.ref)

        return await self._write(user_id, account_id, target, compose, provenance, allow_new=True, op="create")

    async def update(self, user_id: str, account_id: str, ref: str, change: NoteChange,
                     provenance: NoteProvenance = NoteProvenance(), *,
                     expected_version: Optional[int] = None) -> NoteReceipt:
        """Apply one change to the note `ref` names (`apply_change`). With no note, or a cleared one, an additive
        change (append text, set type, add areas) creates the note holding only that change, under `create`'s rules;
        any other change is refused (NO_NOTE: use create_note). `expected_version` (the version `get` returned) refuses
        with CHANGED_SINCE_READ when the note's current version differs. The change is composed again on each write
        attempt, so a concurrent write is merged, never overwritten; a change that leaves the triple as it is (a
        re-run, removing an absent area) is a no-op receipt. An append is the one change a re-run would repeat: its
        re-run guard mirrors undo's (I3) — when the current version was written by this same session and non-empty
        turn id and the text already ends with the appended words, a provider-rotation re-run of the turn writes
        nothing and gets a no-op receipt."""
        target = self._target(ref)
        if change.text:
            await self._check(user_id, change.text)                        # the change's new words, before drive I/O

        def compose(current: Optional[DriveNote]) -> NoteContent:
            live = current is not None and current.state != NoteState.CLEARED.value
            return apply_change(current.content if current is not None and live else NoteContent(), change)

        rerun: Optional[Callable[[DriveNote, Optional[DriveNoteVersion]], bool]] = None
        if change.kind is NoteChangeKind.APPEND_TEXT:
            def rerun(note: DriveNote, written: Optional[DriveNoteVersion]) -> bool:
                return self._same_turn_append(written, provenance, note.text, change.text)

        return await self._write(user_id, account_id, target, compose, provenance, allow_new=change.additive,
                                 expected_version=expected_version, op="update", rerun=rerun)

    # ------------------------------------------------------------------------------------------------------------------
    # the one write loop (§4.4 steps 2-8) shared by set, create and update
    # ------------------------------------------------------------------------------------------------------------------

    async def _write(self, user_id: str, account_id: str, target: NoteTarget,
                     compose: Callable[[Optional[DriveNote]], NoteContent], provenance: NoteProvenance, *,
                     allow_new: bool, expected_version: Optional[int] = None, op: str = "set",
                     rerun: Optional[Callable[[DriveNote, Optional[DriveNoteVersion]], bool]] = None) -> NoteReceipt:
        """Resolve the note, compose its new triple and write it; the composition runs inside the two-attempt conflict
        loop, so a conflict re-reads the note and composes again on top of the other write.

        A `drive:` ref takes the live path (the connected drive, the D20 inaccessible check, the item resolved on the
        drive, the note found by its binding key): the note is written `active` and re-bound to the live item, and a
        missing or erased note gets a lineage (`allow_new`). A `drive_note:` ref takes the store alone (D21): the
        note keeps its binding, snapshot and state (a cleared note comes back as a restore says), and never starts.

        `compose(current)` gets the live note (None for no note or an erase stub; a cleared note as it is) and returns
        the triple, or raises DriveNoteRefused. Without `allow_new`, no note or a cleared note is refused (NO_NOTE).
        `expected_version` differing from the current version (0 for none) is refused (CHANGED_SINCE_READ). A composed
        triple equal to the current one, in the same state, is a no-op receipt; content differing from the content
        last embedded is embedded again. `rerun(note, its current version)` true is a re-run of a write already made:
        a no-op receipt, checked on each attempt before the composition."""
        place = await self._live_item(user_id, target) if not target.note_id else None
        embedded: Optional[Tuple[NoteContent, str]] = None
        vector: Optional[List[float]] = None
        pending = False
        for attempt in range(2):
            if place is not None:
                existing = await self._notes.find_bound(user_id, place.key)
                current = existing if existing is not None and not existing.erase_pending else None
                name = place.snapshot.name
            else:
                found = await self._notes.get(user_id, target.note_id)
                if found is None or found.erase_pending:
                    raise DriveNoteRefused(NoteRefusal.NO_NOTE, f"There is no note {target.note_id}.")
                existing = current = found
                name = found.snapshot.name if found.snapshot is not None else ""
            now_version = existing.current_version if existing is not None else 0
            if expected_version is not None and now_version != expected_version:
                logger.info(f"[DriveNote] {op} user={user_id[:8]} note={existing.note_id if existing else '-'}: "
                            f"changed since read (read v{expected_version}, now v{now_version})")
                raise DriveNoteRefused(NoteRefusal.CHANGED_SINCE_READ,
                                       "The note changed since you read it: read it again with get_note.",
                                       read_version=expected_version, current_version=now_version)
            if not allow_new and (current is None or current.state == NoteState.CLEARED.value):
                raise DriveNoteRefused(NoteRefusal.NO_NOTE, "There is no note on this file: use create_note.")
            if rerun is not None and current is not None and current.state != NoteState.CLEARED.value and rerun(
                    current, await self._notes.get_version(user_id, current.note_id, current.current_version)):
                logger.info(f"[DriveNote] {op} user={user_id[:8]} note={current.note_id}: a re-run of this turn's "
                            "write; nothing written")
                receipt = await self._noop_receipt(user_id, current, place)
                self._log(op, user_id, receipt)
                return receipt
            content = compose(current)
            state = self._state_after_write(current, content, live=place is not None)
            if (current is not None and current.state != NoteState.CLEARED.value and current.content == content
                    and current.state == state.value):
                receipt = await self._noop_receipt(user_id, current, place)
                self._log(op, user_id, receipt, words=note_word_count(content.text))
                return receipt
            if embedded != (content, name):                                 # step 4: only when the content changed
                vector, pending = await self._embed(user_id, content, name)
                embedded = (content, name)
            base = await self._base(user_id, account_id, existing, place.key if place is not None else "")
            expected = now_version
            now = self._clock()                                             # fresh on a retry
            binding: Optional[NoteBinding] = None
            if place is not None:
                same = (current is not None and current.binding is not None
                        and current.binding.identity == place.identity and current.binding.item_id == place.item_id
                        and current.binding.ref_tag == place.connection.ref_tag)
                binding = (current.binding if same and current is not None and current.binding is not None
                           else NoteBinding(identity=place.identity, item_id=place.item_id,
                                            ref_tag=place.connection.ref_tag, bound_at=now,
                                            bound_by=provenance.origin))
            version = next_version(expected, content, NoteAction.SET, provenance, now,
                                   not_before=await self._valid_from_of(user_id, existing))
            keys, keys_version = note_store_keys(content, name)            # §3.1: the deleted-files search's keys
            written = note_after_write(base, version, state, provenance, now, vector=vector,
                                       embedding_space=self._space, embedding_pending=pending, binding=binding,
                                       snapshot=place.snapshot if place is not None else None, keys=keys,
                                       keys_version=keys_version)
            try:
                await self._notes.write_version(written, version, expected_version=expected)
            except DriveNoteConflict:
                if attempt:
                    logger.warning(f"[DriveNote] {op} conflict user={user_id[:8]} note={base.note_id}: refused")
                    raise DriveNoteRefused(NoteRefusal.CONFLICT, "The note was changed at the same time: try again.")
                logger.info(f"[DriveNote] {op} conflict user={user_id[:8]} note={base.note_id}: retrying")
                continue
            receipt = await self._written_receipt(user_id, written, current, place)   # steps 6-7
            self._log(op, user_id, receipt, words=note_word_count(content.text))     # step 8
            return receipt
        raise AssertionError("unreachable")   # pragma: no cover

    async def _live_item(self, user_id: str, target: NoteTarget) -> _LiveItem:
        """§4.4 step 2 for a `drive:` ref: its connection (MULTI_DRIVE_RFC §9), the index's inaccessible mark first
        (D20), then the item on the drive, its identity and its snapshot."""
        connection = await self._connection_of_ref(user_id, target)
        item_id = target.item_id
        record = await self._record(connection, item_id)
        if record is not None and inaccessible(record):                     # step 2.1: the index first (D20)
            raise DriveNoteRefused(NoteRefusal.CANNOT_OPEN,
                                   "The bot cannot open this file, so it cannot hold a note: ask the owner to copy it "
                                   "into a new folder they create inside the bot's folder, and note the copy.", item_id=item_id)
        change, item = await self._resolve_item(connection, item_id)       # step 2.2-2.3
        identity = item.identity or change.identity
        if not identity:
            logger.error(f"[DriveNote] item without an identity user={user_id[:8]} item={item_id}: the adapter "
                         "built none")
            raise DriveNoteRefused(NoteRefusal.DRIVE_UNAVAILABLE, "The drive could not be reached: try again later.")
        snapshot = snapshot_of(change, item, kind=record.kind if record else "",
                               effective_date=record.effective_date if record else None, now=self._clock())
        return _LiveItem(connection=connection, item_id=item_id, identity=identity, record=record, snapshot=snapshot,
                         key=item_binding_key(identity))

    @staticmethod
    def _state_after_write(note: Optional[DriveNote], content: NoteContent, *, live: bool) -> NoteState:
        """The note's state after the write. The live path (`live`) binds it to the resolved item: active; so is a new
        lineage. A note edited by its `drive_note:` ref keeps its state (archived stays archived, a note on another
        drive stays active there); a cleared one comes back as a restore says (archived when it had been archived)."""
        if live or note is None:
            return NoteState.ACTIVE
        if note.state == NoteState.CLEARED.value:
            return state_after(note, content, NoteAction.RESTORE)
        return NoteState(note.state)

    async def _base(self, user_id: str, account_id: str, existing: Optional[DriveNote], key: str) -> DriveNote:
        """The parent the write starts from: the stored note; an erase stub restarts its lineage on its own id; no note
        at all starts a lineage named from the binding `key` (only the live path gets here: a note ref never starts
        one)."""
        if existing is None:
            return DriveNote(note_id=await self._new_lineage_id(user_id, key), user_id=user_id, account_id=account_id)
        if existing.erase_pending:                                          # a stub: a fresh lineage on its id
            return existing.model_copy(update={"account_id": existing.account_id or account_id, "created_at": None})
        return existing.model_copy(update={"account_id": existing.account_id or account_id})

    async def _noop_receipt(self, user_id: str, note: DriveNote, place: Optional[_LiveItem]) -> NoteReceipt:
        if place is not None:
            return self._receipt("set", note, path=place.snapshot.path, ref=place.ref, previous=note,
                                 status=searchability(place.connection.indexing_enabled, place.record), noop=True)
        return self._receipt("set", note, path=self._path(note), ref=item_ref(note), previous=note,
                             status=await self._status_of(user_id, note), noop=True)

    async def _written_receipt(self, user_id: str, written: DriveNote, previous: Optional[DriveNote],
                               place: Optional[_LiveItem]) -> NoteReceipt:
        """Nudge the indexer (step 6: a live item always; a note-ref edit only when the note is active on the connected
        drive) and build the receipt of the stored note (step 7)."""
        if place is not None:
            await self._nudge(place.connection, [place.item_id])
            return self._receipt("set", written, path=place.snapshot.path, ref=place.ref,
                                 previous=previous, status=searchability(place.connection.indexing_enabled,
                                                                         place.record))
        connection = await self._note_connection(user_id, written)
        if connection is not None and written.state == NoteState.ACTIVE.value:
            await self._nudge(connection, [written.binding.item_id] if written.binding is not None else [])
        return self._receipt("set", written, path=self._path(written), ref=item_ref(written), previous=previous,
                             status=await self._status_of(user_id, written))

    # ------------------------------------------------------------------------------------------------------------------
    # clear and undo: the store alone, no drive call (§4.4, I8)
    # ------------------------------------------------------------------------------------------------------------------

    async def clear(self, user_id: str, account_id: str, ref: str,
                    provenance: NoteProvenance = NoteProvenance()) -> NoteReceipt:
        """Delete the note: a `clear` version, `state = cleared`, every derived field emptied; history kept (undo
        restores, an erase removes). No note, a cleared note or an erase stub: a no-op success ("there was no note")."""
        target = self._target(ref)
        note = await self._resolve_note(user_id, target)
        for attempt in range(2):
            if note is None or note.erase_pending or note.state == NoteState.CLEARED.value:
                receipt = self._noop_clear(note, target)
                self._log("clear", user_id, receipt)
                return receipt
            now = self._clock()
            version = next_version(note.current_version, NoteContent(), NoteAction.CLEAR, provenance, now,
                                   not_before=await self._valid_from_of(user_id, note))
            written = note_after_write(note.model_copy(update={"account_id": note.account_id or account_id}), version,
                                       NoteState.CLEARED, provenance, now, vector=None, embedding_space="",
                                       embedding_pending=False)
            try:
                await self._notes.write_version(written, version, expected_version=note.current_version)
            except DriveNoteConflict:
                if attempt:
                    logger.warning(f"[DriveNote] clear conflict user={user_id[:8]} note={note.note_id}: refused")
                    raise DriveNoteRefused(NoteRefusal.CONFLICT, "The note was changed at the same time: try again.")
                note = await self._notes.get(user_id, note.note_id)
                continue
            await self._nudge_note(user_id, note)
            receipt = self._receipt("clear", written, path=self._path(note), ref=item_ref(note), previous=note)
            self._log("clear", user_id, receipt)
            return receipt
        raise AssertionError("unreachable")   # pragma: no cover

    async def undo(self, user_id: str, account_id: str, ref: str,
                   provenance: NoteProvenance = NoteProvenance()) -> NoteReceipt:
        """Put back the note as it was before its last change: a `restore` version copying v(n-1) (undo of a first
        note clears it; undo of an undo restores the undone version, `redo`). An archived note keeps `archived`; a
        cleared note comes back `active` (or `archived` when it had been archived). Re-run guard (I3): when the current
        version is a restore written by this same turn (`provenance.session_id` and a non-empty `provenance.turn_id`
        equal), nothing is written and a no-op receipt returns — a failover re-run of the turn never flips it back.
        The caller must pass a non-empty `turn_id`; without one the guard is off (logged)."""
        target = self._target(ref)
        if not provenance.turn_id:
            logger.warning(f"[DriveNote] undo user={user_id[:8]} without a turn id: the re-run guard is off")
        note = await self._resolve_note(user_id, target)
        for attempt in range(2):
            if note is None or note.erase_pending:
                raise DriveNoteRefused(NoteRefusal.NO_NOTE, "There is no note on this file to undo.")
            source_v = undo_source(note.current_version)
            source = await self._notes.get_version(user_id, note.note_id, source_v) if source_v else None
            if source_v and source is None:
                logger.error(f"[DriveNote] undo user={user_id[:8]} note={note.note_id}: version {source_v} missing")
                raise DriveNoteRefused(NoteRefusal.HISTORY_MISSING, "The note's previous version is missing.")
            current = await self._notes.get_version(user_id, note.note_id, note.current_version)
            if self._same_turn_restore(current, provenance):
                # I3: a failover re-run of this turn repeats its undo; the first one already did what the owner asked
                receipt = self._receipt("restore", note, path=self._path(note), ref=item_ref(note), previous=note,
                                        noop=True)
                self._log("undo", user_id, receipt)
                return receipt
            redo = current is not None and current.action == NoteAction.RESTORE.value
            content = content_of(source)
            state = state_after(note, content, NoteAction.RESTORE)
            name = note.snapshot.name if note.snapshot is not None else ""
            vector, pending = (None, False) if content.is_empty else await self._embed(user_id, content, name)
            now = self._clock()
            version = next_version(note.current_version, content, NoteAction.RESTORE, provenance, now,
                                   restored_from=source_v,
                                   not_before=current.valid_from if current is not None else None)
            keys, keys_version = note_store_keys(content, name)   # an empty restore (a cleared note) has none
            written = note_after_write(note.model_copy(update={"account_id": note.account_id or account_id}), version,
                                       state, provenance, now, vector=vector, embedding_space=self._space,
                                       embedding_pending=pending, keys=keys, keys_version=keys_version)
            try:
                await self._notes.write_version(written, version, expected_version=note.current_version)
            except DriveNoteConflict:
                if attempt:
                    logger.warning(f"[DriveNote] undo conflict user={user_id[:8]} note={note.note_id}: refused")
                    raise DriveNoteRefused(NoteRefusal.CONFLICT, "The note was changed at the same time: try again.")
                note = await self._notes.get(user_id, note.note_id)
                continue
            await self._nudge_note(user_id, note)
            receipt = self._receipt("restore", written, path=self._path(note), ref=item_ref(note), previous=note,
                                    redo=redo)
            self._log("undo", user_id, receipt)
            return receipt
        raise AssertionError("unreachable")   # pragma: no cover

    # ------------------------------------------------------------------------------------------------------------------
    # erase (§4.8, D13): a code first, the erase on its paste
    # ------------------------------------------------------------------------------------------------------------------

    async def request_erase(self, user_id: str, ref: str) -> EraseRequestReceipt:
        """Store an erase request for the one note `ref` names and return its code; nothing is erased here."""
        store = self._erase_store()
        note = await self._resolve_note(user_id, self._target(ref))
        if note is None or note.erase_pending:
            raise DriveNoteRefused(NoteRefusal.NO_NOTE, "There is no note on this file to erase.")
        code = await self._store_request(store, user_id, ERASE_SCOPE_ONE, [note.note_id])
        logger.info(f"[DriveNote] erase requested user={user_id[:8]} scope=one note={note.note_id} "
                    f"versions={note.current_version}")
        return EraseRequestReceipt(scope=ERASE_SCOPE_ONE, code=code, note_count=1, path=self._path(note),
                                   version_count=note.current_version)

    async def request_erase_all(self, user_id: str) -> EraseRequestReceipt:
        """Store an erase request for every note of the user (stubs excepted) and return its code and the count."""
        store = self._erase_store()
        ids = await self._notes.note_ids(user_id)
        if not ids:
            raise DriveNoteRefused(NoteRefusal.NO_NOTE, "There are no drive notes to erase.")
        code = await self._store_request(store, user_id, ERASE_SCOPE_ALL, ids)
        logger.info(f"[DriveNote] erase requested user={user_id[:8]} scope=all notes={len(ids)}")
        return EraseRequestReceipt(scope=ERASE_SCOPE_ALL, code=code, note_count=len(ids))

    async def erase(self, user_id: str, code: str) -> int:
        """Redeem an erase code (once: an atomic get-and-delete) and erase its notes: every version deleted, a
        contentless stub left for the indexer to clear the record copy — or, with indexing off (no copy exists), the
        stubs deleted at once. Returns the number erased. A wrong, expired, reused or someone else's code is refused
        and erases nothing. The code is spent on redemption, so a partial erase still finishes what it can: the notes
        the store could not erase (`EraseResult.left`) are reported AFTER the rest is done (a one-note erase nudges its item;
        an erase-all nudges nothing, its copies are cleared from the notes step's watermark; with indexing off a stub
        is deleted unless a record still holds its copy), as DriveNoteRefused(CONFLICT) with `erased`, `left` and
        `scope` — the owner asks again for those (after an erase-all, by sending the command again)."""
        store = self._erase_store()
        code = (code or "").strip()
        value = await store.get_and_delete(f"{user_id}:{code}") if code else None
        if not value:
            logger.info(f"[DriveNote] erase user={user_id[:8]}: no such request")
            raise DriveNoteRefused(NoteRefusal.NO_SUCH_ERASE_REQUEST, "No such erase request.")
        ids = [str(i) for i in value.get("note_ids") or []]
        notes = await self._notes.get_many(user_id, ids)
        result = await self._notes.erase(user_id, ids)
        left = set(result.left)
        done = [i for i in ids if i not in left]
        scope = str(value.get("scope", ""))
        live = await self._live_connections(user_id)
        if live is not None:
            by_tag = {c.ref_tag: c for c in live}
            erased = {i: n for i, n in notes.items() if i in done}
            # with indexing off on the note's connection (or no live connection: no index at all) no tick clears a
            # record copy: a stub is deleted now only when no record holds that note's copy (a copy left from before
            # indexing went off waits for the indexer, which clears it first)
            idle = {i: n for i, n in erased.items()
                    if (c := by_tag.get(note_ref_tag(n))) is None or not c.indexing_enabled}
            held = await self._copies_held(user_id, idle, by_tag)
            for note_id in idle:
                if note_id not in held:
                    await self._notes.delete_stub(user_id, note_id)
            if scope != ERASE_SCOPE_ALL:
                # one note: its item is nudged; an erase-all nudges nothing (its merges come from the watermark, and
                # a nudge of every item would cost the next tick one item call each)
                for note_id, note in erased.items():
                    connection = by_tag.get(note_ref_tag(note))
                    if note_id not in idle and connection is not None and note.binding is not None:
                        await self._nudge(connection, [note.binding.item_id])
        logger.info(f"[DriveNote] erased user={user_id[:8]} scope={value.get('scope', '')} notes={result.erased} "
                    f"left={len(left)}")
        if left:
            raise DriveNoteRefused(
                NoteRefusal.CONFLICT,
                f"{result.erased} notes were erased; {len(left)} could not be erased right now: ask to erase them "
                "again.", erased=result.erased, left=len(left), note_ids=list(result.left), scope=scope)
        return result.erased

    # ------------------------------------------------------------------------------------------------------------------
    # reads (S2: the open-file header, §6.2)
    # ------------------------------------------------------------------------------------------------------------------

    async def note_for_item(self, user_id: str, identity: str) -> Optional[DriveNote]:
        """The note bound to the item of this identity (`DriveItem.identity`, MULTI_DRIVE_RFC §7.2), when it says
        anything: an `active` or `archived` note (a cleared note and an erase stub are None). One `array-contains`
        read; None for an empty identity."""
        if not identity:
            return None
        note = await self._notes.find_bound(user_id, item_binding_key(identity))
        if note is None or note.erase_pending or note.state == NoteState.CLEARED.value:
            return None
        return note

    # ------------------------------------------------------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _target(ref: str) -> NoteTarget:
        target = parse_note_target(ref)
        if target is None:
            raise DriveNoteRefused(NoteRefusal.INVALID_REF,
                                   "file_ref must be a drive: ref from a search result, a listing or a [Drive: …] "
                                   "label, or a drive_note:<id> ref from a deleted-files result.")
        return target

    async def _check(self, user_id: str, text: str) -> None:
        """§4.5 layer 4: the same call MemoryFactService makes; blocked or flagged is refused (patterns logged, never
        the text)."""
        try:
            result = await self._security.validate(text, context=f"drive_note_user_{user_id}",
                                                   zone=TrustZone.UNTRUSTED)
        except ValueError as exc:   # the regex/composite adapters raise on HIGH/CRITICAL
            logger.warning(f"🛡️ [DriveNote] note rejected user={user_id[:8]}: blocked ({exc})")
            raise DriveNoteRefused(NoteRefusal.SECURITY_REJECTED, "The note was blocked by the security check.") from exc
        if result.action_taken != "passed":
            logger.warning(f"🛡️ [DriveNote] note rejected user={user_id[:8]}: flagged {result.patterns_detected}")
            raise DriveNoteRefused(NoteRefusal.SECURITY_REJECTED, "The note was flagged by the security check.")

    async def _connection_of_ref(self, user_id: str, target: NoteTarget) -> DriveConnection:
        """The live connection a `drive:` target names (§9): refused with the code-built line for an untagged ref or a
        tag of no live connection (D13), NOT_CONNECTED when it is not reachable, DRIVE_UNAVAILABLE when the
        connections cannot be read."""
        ref = (drive_ref.format(target.tag, target.item_id) if target.tag
               else f"{drive_ref.REF_PREFIX}{target.item_id}")
        try:
            found = await self._connections.resolve_ref(user_id, ref)
        except Exception as exc:
            logger.error(f"[DriveNote] connection read failed user={user_id[:8]}: {type(exc).__name__}: {exc}")
            raise DriveNoteRefused(NoteRefusal.DRIVE_UNAVAILABLE, "The drive could not be reached: try again later.")
        if found is None or isinstance(found, DriveRefRefusal):
            line = found.line if isinstance(found, DriveRefRefusal) else stale_ref_line(ref)
            raise DriveNoteRefused(NoteRefusal.INVALID_REF, line, ref=ref)
        return found.connection

    async def _record(self, connection: DriveConnection, item_id: str) -> Optional[DriveIndexRecord]:
        try:
            return (await self._index.get_records(connection.scope, [item_id])).get(item_id)
        except Exception as exc:   # the guard falls back to Graph's own 404 for a file the bot cannot open
            logger.warning(f"[DriveNote] index read failed user={connection.user_id[:8]}: {type(exc).__name__}: {exc}")
            return None

    async def _resolve_item(self, connection: DriveConnection, item_id: str) -> Tuple[DriveChange, DriveItem]:
        user_id = connection.user_id
        try:
            change = await self._drive.get_change(connection.handle, item_id)
            item = await self._drive.get_item(connection.handle, item_id)
        except DriveItemNotFoundError:
            raise DriveNoteRefused(NoteRefusal.NOT_ON_DRIVE,
                                   "The file is not on the drive. If it had a note, that note is archived: a "
                                   "deleted-files search shows its drive_note: ref, which can change it.",
                                   item_id=item_id)
        except DriveNotConnectedError:
            raise DriveNoteRefused(NoteRefusal.NOT_CONNECTED,
                                   "The drive is not connected: connect it first (a note needs the file verified).")
        except Exception as exc:
            logger.error(f"[DriveNote] item read failed user={user_id[:8]} item={item_id}: "
                         f"{type(exc).__name__}: {exc}")
            raise DriveNoteRefused(NoteRefusal.DRIVE_UNAVAILABLE, "The drive could not be reached: try again later.")
        return change, item

    async def _new_lineage_id(self, user_id: str, key: str) -> str:
        """The deterministic id `sha256(i-key)[:32]` (concurrent first writes collide on it), or a random one when
        that id is taken by a note bound elsewhere. Refused past NOTES_PER_USER_MAX lineages."""
        count = await self._notes.count(user_id)
        if count >= NOTES_PER_USER_MAX:
            logger.warning(f"[DriveNote] note limit user={user_id[:8]} notes={count}")
            raise DriveNoteRefused(NoteRefusal.NOTE_LIMIT,
                                   f"The owner already has {count} drive notes (at most {NOTES_PER_USER_MAX}).",
                                   count=count, max=NOTES_PER_USER_MAX)
        note_id = deterministic_note_id(key)
        taken = await self._notes.get(user_id, note_id)
        if taken is None or key in taken.match_keys:
            return note_id
        return self._new_note_id()

    async def _resolve_note(self, user_id: str, target: NoteTarget) -> Optional[DriveNote]:
        """The note a ref names, from the store alone (no connection read: it works while the drive is disconnected,
        D3, D17). `drive_note:<id>` → that note (refused when it does not exist). `drive:<tag>:<id>` → the note whose
        ref key is `r:{tag}:{id}` (None when there is none; several live ones are refused with each one's `drive_note:`
        ref). An untagged `drive:<id>` is refused with the "search again" line (MULTI_DRIVE_RFC §9, D13)."""
        if target.note_id:
            note = await self._notes.get(user_id, target.note_id)
            if note is None:
                raise DriveNoteRefused(NoteRefusal.NO_NOTE, f"There is no note {target.note_id}.")
            return note
        if not target.tag:
            ref = f"{drive_ref.REF_PREFIX}{target.item_id}"
            raise DriveNoteRefused(NoteRefusal.INVALID_REF, stale_ref_line(ref), ref=ref)
        found = await self._notes.find_by_match_keys(user_id, [tagged_ref_key(target.tag, target.item_id)])
        live = [n for n in found if not n.erase_pending]
        if not live:
            return found[0] if found else None
        if len(live) == 1:
            return live[0]
        refs = [n.ref for n in live]
        raise DriveNoteRefused(NoteRefusal.AMBIGUOUS_NOTE,
                               f"Several notes match this file: use one of {', '.join(refs)}.", refs=refs)

    async def _live_connections(self, user_id: str) -> Optional[List[DriveConnection]]:
        """The user's live connections (ACTIVE or NEEDS_RECONNECT); None when they could not be read (unknown)."""
        try:
            return await self._connections.list_for_user(user_id)
        except Exception as exc:
            logger.warning(f"[DriveNote] connection read failed user={user_id[:8]}: {type(exc).__name__}")
            return None

    async def _note_connection(self, user_id: str, note: DriveNote) -> Optional[DriveConnection]:
        """The live connection the note is bound on (by its ref tag); None when there is none or it is unknown."""
        tag = note_ref_tag(note)
        live = await self._live_connections(user_id) if tag else None
        return next((c for c in live or () if c.ref_tag == tag), None)

    async def _copies_held(self, user_id: str, notes: Mapping[str, DriveNote],
                           by_tag: Mapping[str, DriveConnection]) -> Set[str]:
        """The note ids whose bound item's index record (in the index of the note's connection) still carries that
        note's copy. A note with no live connection has no index, so nothing holds it. An index read that fails counts
        every note of that connection as held (the stub then waits for the indexer, which is the safe side)."""
        groups: Dict[str, Dict[str, str]] = {}
        for note_id, note in notes.items():
            tag = note_ref_tag(note)
            if note.binding is not None and tag in by_tag:
                groups.setdefault(tag, {})[note.binding.item_id] = note_id
        held: Set[str] = set()
        for tag, items in groups.items():
            try:
                records = await self._index.get_records(by_tag[tag].scope, list(items))
            except Exception as exc:
                logger.warning(f"[DriveNote] index read failed user={user_id[:8]}: {type(exc).__name__}")
                held.update(items.values())
                continue
            held.update(note_id for item_id, note_id in items.items()
                        if item_id in records and records[item_id].owner_note_id == note_id)
        return held

    async def _embed(self, user_id: str, content: NoteContent, name: str) -> Tuple[Optional[List[float]], bool]:
        """One embedding call (§4.4 step 4); a failure stores the note with `embedding_pending` (the indexer repairs)."""
        try:
            vector = await self._embeddings.get_embedding(note_embedding_text(content, name))
        except Exception as exc:
            logger.warning(f"[DriveNote] embedding failed user={user_id[:8]}: {type(exc).__name__}: {exc}")
            return None, True
        if not vector:
            logger.warning(f"[DriveNote] embedding empty user={user_id[:8]}")
            return None, True
        return [float(x) for x in vector], False

    async def _nudge_note(self, user_id: str, note: DriveNote) -> None:
        """Nudge the item of a note edited from the store, on its connection; nothing when it has none live."""
        if note.binding is None:
            return
        connection = await self._note_connection(user_id, note)
        if connection is not None:
            await self._nudge(connection, [note.binding.item_id])

    async def _nudge(self, connection: DriveConnection, item_ids: Sequence[str]) -> None:
        """Tell the connection's indexer which items changed (UserDriveService's nudge, RFC Δ3). Never raises."""
        # at most ITEM_CALLS_PER_TICK: the tick reads every nudged item by id, serially; the rest merge from the
        # notes step's watermark anyway
        ids = tuple(dict.fromkeys(i for i in item_ids if i))[:ITEM_CALLS_PER_TICK]
        if self._nudge_queue is None or not ids:
            return
        nudge = Nudge(mutation_id=uuid.uuid4().hex, item_ids=ids)
        try:
            await self._nudge_queue.enqueue_worker_task(
                DRIVE_SYNC_TASK, scope_payload(connection.scope, nudge), deadline_seconds=TICK_DEADLINE_S,
                dedup_key=nudge_task_key(connection.scope, nudge.mutation_id, 0))
        except Exception as exc:
            logger.warning(f"[DriveNote] nudge failed user={connection.user_id[:8]} items={len(ids)}: {exc}")

    def _erase_store(self) -> EphemeralStore:
        if self._erase_requests is None:
            raise DriveNoteRefused(NoteRefusal.ERASE_UNAVAILABLE, "Erasing notes is unavailable right now.")
        return self._erase_requests

    async def _store_request(self, store: EphemeralStore, user_id: str, scope: str, note_ids: Sequence[str]) -> str:
        for _ in range(_ERASE_CODE_ATTEMPTS):
            code = self._new_code()
            key = f"{user_id}:{code}"
            if await store.get(key) is None:
                await store.set(key, {"scope": scope, "note_ids": list(note_ids)},
                                ttl_s=int(ERASE_CODE_TTL.total_seconds()))
                return code
        logger.error(f"❌ [DriveNote] no unique erase code user={user_id[:8]} after {_ERASE_CODE_ATTEMPTS} attempts")
        raise RuntimeError(f"could not allocate an erase code after {_ERASE_CODE_ATTEMPTS} attempts")

    async def _status_of(self, user_id: str, note: DriveNote) -> str:
        """The searchability of an existing note edited by its `drive_note:` ref: archived → found only by a
        deleted-files search; bound on a connection that is not live → not searchable now; else as for `set`, by its
        connection's switch and index. Connections that cannot be read are not "another drive": status as for `set`
        with the switch on and no record."""
        if note.state == NoteState.ARCHIVED.value:
            return STATUS_ARCHIVED
        if note.binding is None:
            return STATUS_OTHER_DRIVE
        live = await self._live_connections(user_id)
        if live is None:
            return searchability(True, None)
        tag = note_ref_tag(note)
        connection = next((c for c in live if tag and c.ref_tag == tag), None)
        if connection is None:
            return STATUS_OTHER_DRIVE
        return searchability(connection.indexing_enabled, await self._record(connection, note.binding.item_id))

    async def _valid_from_of(self, user_id: str, note: Optional[DriveNote]) -> Optional[datetime]:
        """The current version's `valid_from` (the next one never opens before it); None for no note or a stub."""
        if note is None or note.erase_pending or note.current_version < 1:
            return None
        current = await self._notes.get_version(user_id, note.note_id, note.current_version)
        return current.valid_from if current is not None else None

    @staticmethod
    def _same_turn_append(written: Optional[DriveNoteVersion], provenance: NoteProvenance, text: str,
                          appended: str) -> bool:
        """The append's re-run guard: the current version is a set by this session and turn (non-empty turn id) and
        the note's text already ends with the words (normalized, as the change holds them)."""
        return (written is not None and written.action == NoteAction.SET.value and bool(provenance.turn_id)
                and written.turn_id == provenance.turn_id and written.session_id == provenance.session_id
                and bool(appended) and text.endswith(appended))

    @staticmethod
    def _same_turn_restore(current: Optional[DriveNoteVersion], provenance: NoteProvenance) -> bool:
        return (current is not None and current.action == NoteAction.RESTORE.value and bool(provenance.turn_id)
                and current.turn_id == provenance.turn_id and current.session_id == provenance.session_id)

    @staticmethod
    def _path(note: Optional[DriveNote]) -> str:
        return note.snapshot.path if note is not None and note.snapshot is not None else ""

    @staticmethod
    def _receipt(action: str, note: DriveNote, *, path: str, ref: str, previous: Optional[DriveNote],
                 status: str = "", noop: bool = False, redo: bool = False) -> NoteReceipt:
        """The receipt of what is now stored (`note` is the written parent, whose triple copies the stored version)."""
        before = previous.content if previous is not None else NoteContent()
        return NoteReceipt(action=action, note_id=note.note_id, ref=ref, path=path, version=note.current_version,
                           text=note.text, doc_type=note.doc_type, life_areas=tuple(note.life_areas),
                           previous_text=before.text, previous_doc_type=before.doc_type,
                           previous_life_areas=before.life_areas, status=status, state=str(note.state), noop=noop,
                           redo=redo)

    def _noop_clear(self, note: Optional[DriveNote], target: NoteTarget) -> NoteReceipt:
        if note is None:
            ref = drive_ref.format(target.tag, target.item_id) if target.item_id and target.tag else ""
            return NoteReceipt(action="clear", note_id="", ref=ref, path="", version=0, state="", noop=True)
        stub = note.erase_pending
        return NoteReceipt(action="clear", note_id=note.note_id, ref=item_ref(note), path=self._path(note),
                           version=0 if stub else note.current_version, state=str(note.state), noop=True)

    @staticmethod
    def _log(op: str, user_id: str, receipt: NoteReceipt, *, words: int = 0) -> None:
        """One line per call: ids, counts, the version and the status, never the text."""
        logger.info(f"[DriveNote] {op} user={user_id[:8]} note={receipt.note_id} v={receipt.version} words={words} "
                    f"status={receipt.status or '-'} state={receipt.state or '-'} noop={receipt.noop}")
