"""
DriveNoteService — the write path of the owner's notes on drive items (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §4.4,
§4.5, §4.6, §4.8; D3, D11-D17, D20).

The tool handler routes one `annotate_drive_file` call to one method: a non-empty triple → `set`; an empty triple →
`clear`; `undo` → `undo`; `erase` → `request_erase`. `$drive-notes erase-all` → `request_erase_all`; pasting a code →
`erase`. **Only `set` touches the drive**: clear, undo and the erase methods resolve the note from the store alone
(a `drive_note:<id>` ref, or a `drive:<id>` ref through the provider-free ref key `r:{id}`), so they work on archived and
dormant notes and while the drive is disconnected (D3, D17).

Guards (§4.5) that live here: the security check before any drive I/O (layer 4) and the per-user lineage cap. The owner-
turn gate (layer 1), the skill gate (layer 3), the per-turn write cap and the per-execution lock are the tool handler's.
Every receipt carries the stored triple byte for byte (layer 5); every version its provenance (layer 6).

Writes go through `DriveNoteRepository.write_version` (one transaction, precondition on `current_version`); a conflict
is retried once from the lineage lookup, then refused. After a write the indexer is nudged with the item id, through the
same `drive_sync` task as UserDriveService's nudge; a nudge never raises (the next fan-out merges). Logs carry ids,
counts, versions and statuses, never the note's text.

Public interface:
    set(user_id, account_id, ref, note, doc_type="", life_areas=(), provenance=NoteProvenance()) -> NoteReceipt
    clear(user_id, account_id, ref, provenance=NoteProvenance()) -> NoteReceipt
    undo(user_id, account_id, ref, provenance=NoteProvenance()) -> NoteReceipt
    request_erase(user_id, ref) -> EraseRequestReceipt
    request_erase_all(user_id) -> EraseRequestReceipt
    erase(user_id, code) -> int
Refusals raise DriveNoteRefused (a NoteRefusal reason and its detail); nothing is stored when one is raised.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from ..domain.drive_index import DriveChange, DriveIndexRecord, Nudge, inaccessible
from ..domain.drive_index_params import TICK_DEADLINE_S
from ..domain.drive_note import (
    ERASE_CODE_TTL, ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, NOTES_PER_USER_MAX, DriveNote, DriveNoteConflict,
    DriveNoteRefused, EraseRequestReceipt, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteReceipt,
    NoteRefusal, NoteState, NoteTarget, binding_key, content_of, deterministic_note_id, item_ref, next_version,
    note_after_write, note_content, note_embedding_text, note_word_count, parse_note_target, ref_key, searchability,
    snapshot_of, state_after, undo_source,
)
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_key, tick_payload
from ..domain.prompt_v3.security import TrustZone
from ..domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNotConnectedError, make_drive_ref
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_note_repository import DriveNoteRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.ephemeral_store import EphemeralStore
from ..ports.security_port import SecurityPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_ERASE_CODE_ATTEMPTS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_note_id() -> str:
    return uuid.uuid4().hex


def _new_code() -> str:
    return secrets.token_hex(3)


class DriveNoteService:
    def __init__(self, notes: DriveNoteRepository, drive: UserDrivePort, index: DriveIndexRepository,
                 embeddings: EmbeddingService, security: SecurityPort, users: UserRepository, *,
                 provider_key: str, embedding_space: str, nudge_queue: Optional[TaskQueue] = None,
                 erase_requests: Optional[EphemeralStore] = None, clock: Callable[[], datetime] = _utcnow,
                 new_note_id: Callable[[], str] = _new_note_id, new_code: Callable[[], str] = _new_code) -> None:
        """`provider_key` names the drive provider in the binding key (`onedrive`; composition knows it, the port
        does not). `embedding_space` is the space `embeddings` writes (the adapter's EMBEDDING_SPACE). `index` is read
        only: the inaccessible check (D20) and the searchability status. `users` gives the owner's indexing switch.
        `nudge_queue` is the drive queue (optional, as in UserDriveService); `erase_requests` the ephemeral store of
        `{prefix}drive_note_erase_requests` (without it the erase methods refuse)."""
        self._notes = notes
        self._drive = drive
        self._index = index
        self._embeddings = embeddings
        self._security = security
        self._users = users
        self._provider = provider_key
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
        """Replace the note of the item `ref` names with the triple (an empty triple is `clear`). Refused when the
        drive is not connected, the item is not on it or the bot cannot open it; an identical triple on an active note
        writes nothing (a failover re-run never doubles versions)."""
        content = note_content(note, doc_type, life_areas)
        if content.is_empty:
            return await self.clear(user_id, account_id, ref, provenance)
        target = self._target(ref)
        if content.text:
            await self._check(user_id, content.text)                       # step 1: before any drive I/O
        drive_id = await self._connected_drive(user_id)
        item_id = await self._item_of(user_id, target, drive_id)
        record = await self._record(user_id, item_id)
        if record is not None and inaccessible(record):                     # step 2.1: the index first (D20)
            raise DriveNoteRefused(NoteRefusal.CANNOT_OPEN,
                                   "The bot cannot open this file, so it cannot hold a note: ask the owner to copy it "
                                   "into the bot's folder and note the copy.", item_id=item_id)
        change, item = await self._resolve_item(user_id, item_id)          # step 2.2-2.3
        now = self._clock()
        snapshot = snapshot_of(change, item, kind=record.kind if record else "",
                               effective_date=record.effective_date if record else None, now=now)
        key = binding_key(self._provider, drive_id, item_id)
        vector, pending = await self._embed(user_id, content, snapshot.name)   # step 4: once, outside the retry

        for attempt in range(2):                                            # step 3 + 5, retried once on a conflict
            existing = await self._notes.find_bound(user_id, key)
            live = existing if existing is not None and not existing.erase_pending else None
            if live is not None and live.state == NoteState.ACTIVE.value and live.content == content:
                enabled = await self._indexing_enabled(user_id)
                receipt = self._receipt("set", live, path=snapshot.path, ref=make_drive_ref(item_id), previous=live,
                                        status=searchability(enabled is not False, record), noop=True)
                self._log("set", user_id, receipt, words=note_word_count(content.text))
                return receipt
            if existing is None:
                base = DriveNote(note_id=await self._new_lineage_id(user_id, key), user_id=user_id,
                                 account_id=account_id)
            elif existing.erase_pending:                                    # a stub: a fresh lineage on its id
                base = existing.model_copy(update={"account_id": existing.account_id or account_id,
                                                   "created_at": None})
            else:
                base = existing.model_copy(update={"account_id": existing.account_id or account_id})
            expected = 0 if existing is None else existing.current_version
            same = (live is not None and live.binding is not None and live.binding.drive_id == drive_id
                    and live.binding.item_id == item_id and live.binding.provider_key == self._provider)
            binding = (live.binding if same and live is not None and live.binding is not None
                       else NoteBinding(provider_key=self._provider, drive_id=drive_id, item_id=item_id, bound_at=now,
                                        bound_by=provenance.origin))
            version = next_version(expected, content, NoteAction.SET, provenance, now)
            written = note_after_write(base, version, NoteState.ACTIVE, provenance, now, vector=vector,
                                       embedding_space=self._space, embedding_pending=pending, binding=binding,
                                       snapshot=snapshot)
            try:
                await self._notes.write_version(written, version, expected_version=expected)
            except DriveNoteConflict:
                if attempt:
                    logger.warning(f"[DriveNote] set conflict user={user_id[:8]} note={base.note_id}: refused")
                    raise DriveNoteRefused(NoteRefusal.CONFLICT, "The note was changed at the same time: try again.")
                logger.info(f"[DriveNote] set conflict user={user_id[:8]} note={base.note_id}: retrying")
                continue
            await self._nudge(user_id, [item_id])                           # step 6
            enabled = await self._indexing_enabled(user_id)
            receipt = self._receipt("set", written, path=snapshot.path, ref=make_drive_ref(item_id), previous=live,
                                    status=searchability(enabled is not False, record))   # step 7
            self._log("set", user_id, receipt, words=note_word_count(content.text))      # step 8
            return receipt
        raise AssertionError("unreachable")   # pragma: no cover

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
            version = next_version(note.current_version, NoteContent(), NoteAction.CLEAR, provenance, now)
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
            await self._nudge(user_id, [note.binding.item_id] if note.binding is not None else [])
            receipt = self._receipt("clear", written, path=self._path(note), ref=item_ref(note), previous=note)
            self._log("clear", user_id, receipt)
            return receipt
        raise AssertionError("unreachable")   # pragma: no cover

    async def undo(self, user_id: str, account_id: str, ref: str,
                   provenance: NoteProvenance = NoteProvenance()) -> NoteReceipt:
        """Put back the note as it was before its last change: a `restore` version copying v(n-1) (undo of a first
        note clears it; undo of an undo restores the undone version, `redo`). An archived note keeps `archived`; a
        cleared note comes back `active` (or `archived` when it had been archived)."""
        target = self._target(ref)
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
            redo = current is not None and current.action == NoteAction.RESTORE.value
            content = content_of(source)
            state = state_after(note, content, NoteAction.RESTORE)
            name = note.snapshot.name if note.snapshot is not None else ""
            vector, pending = (None, False) if content.is_empty else await self._embed(user_id, content, name)
            now = self._clock()
            version = next_version(note.current_version, content, NoteAction.RESTORE, provenance, now,
                                   restored_from=source_v)
            written = note_after_write(note.model_copy(update={"account_id": note.account_id or account_id}), version,
                                       state, provenance, now, vector=vector, embedding_space=self._space,
                                       embedding_pending=pending)
            try:
                await self._notes.write_version(written, version, expected_version=note.current_version)
            except DriveNoteConflict:
                if attempt:
                    logger.warning(f"[DriveNote] undo conflict user={user_id[:8]} note={note.note_id}: refused")
                    raise DriveNoteRefused(NoteRefusal.CONFLICT, "The note was changed at the same time: try again.")
                note = await self._notes.get(user_id, note.note_id)
                continue
            await self._nudge(user_id, [note.binding.item_id] if note.binding is not None else [])
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
        and erases nothing."""
        store = self._erase_store()
        code = (code or "").strip()
        value = await store.get_and_delete(f"{user_id}:{code}") if code else None
        if not value:
            logger.info(f"[DriveNote] erase user={user_id[:8]}: no such request")
            raise DriveNoteRefused(NoteRefusal.NO_SUCH_ERASE_REQUEST, "No such erase request.")
        ids = [str(i) for i in value.get("note_ids") or []]
        notes = await self._notes.get_many(user_id, ids)
        items = [n.binding.item_id for n in notes.values() if n.binding is not None]
        erased = await self._notes.erase(user_id, ids)
        if await self._indexing_enabled(user_id) is False:
            for note_id in ids:
                await self._notes.delete_stub(user_id, note_id)
        else:
            await self._nudge(user_id, items)
        logger.info(f"[DriveNote] erased user={user_id[:8]} scope={value.get('scope', '')} notes={erased}")
        return erased

    # ------------------------------------------------------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _target(ref: str) -> NoteTarget:
        target = parse_note_target(ref)
        if target is None:
            raise DriveNoteRefused(NoteRefusal.INVALID_REF,
                                   "file_ref must be a drive:<id> ref from a search result, a listing or a [Drive: …] "
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

    async def _connected_drive(self, user_id: str) -> str:
        try:
            connection = await self._drive.connection(user_id)
        except DriveNotConnectedError:
            connection = None
        except Exception as exc:
            logger.error(f"[DriveNote] connection read failed user={user_id[:8]}: {type(exc).__name__}: {exc}")
            raise DriveNoteRefused(NoteRefusal.DRIVE_UNAVAILABLE, "The drive could not be reached: try again later.")
        if connection is None or not connection.connected or not connection.drive_id:
            raise DriveNoteRefused(NoteRefusal.NOT_CONNECTED,
                                   "The drive is not connected: connect it first (a note needs the file verified).")
        return connection.drive_id

    async def _item_of(self, user_id: str, target: NoteTarget, drive_id: str) -> str:
        """The item a `set` binds to: the `drive:` ref's id, or the bound item of a `drive_note:` ref on this drive."""
        if target.item_id:
            return target.item_id
        bound = await self._notes.get(user_id, target.note_id)
        if bound is None or bound.erase_pending or bound.binding is None:
            raise DriveNoteRefused(NoteRefusal.NO_NOTE, f"There is no note {target.note_id}.")
        if bound.binding.drive_id != drive_id:
            raise DriveNoteRefused(NoteRefusal.OTHER_DRIVE,
                                   "That note's file is on a drive that is not connected: it can be undone, deleted "
                                   "or erased, not changed.", note_ref=bound.ref)
        return bound.binding.item_id

    async def _record(self, user_id: str, item_id: str) -> Optional[DriveIndexRecord]:
        try:
            return (await self._index.get_records(user_id, [item_id])).get(item_id)
        except Exception as exc:   # the guard falls back to Graph's own 404 for a file the bot cannot open
            logger.warning(f"[DriveNote] index read failed user={user_id[:8]}: {type(exc).__name__}: {exc}")
            return None

    async def _resolve_item(self, user_id: str, item_id: str) -> Tuple[DriveChange, DriveItem]:
        try:
            change = await self._drive.get_change(user_id, item_id)
            item = await self._drive.get_item(user_id, item_id)
        except DriveItemNotFoundError:
            raise DriveNoteRefused(NoteRefusal.NOT_ON_DRIVE, "The file is not on the drive.", item_id=item_id)
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
        """The note a ref names, from the store alone. `drive_note:<id>` → that note (refused when it does not
        exist). `drive:<id>` → the note whose ref key is `r:{id}` (None when there is none); several (one item id on
        two drives) → the one on the connected drive, else refused with each one's `drive_note:` ref."""
        if target.note_id:
            note = await self._notes.get(user_id, target.note_id)
            if note is None:
                raise DriveNoteRefused(NoteRefusal.NO_NOTE, f"There is no note {target.note_id}.")
            return note
        found = await self._notes.find_by_match_keys(user_id, [ref_key(target.item_id)])
        live = [n for n in found if not n.erase_pending]
        if not live:
            return found[0] if found else None
        if len(live) == 1:
            return live[0]
        drive_id = await self._drive_id_or_empty(user_id)
        here = [n for n in live if drive_id and n.binding is not None and n.binding.drive_id == drive_id]
        if len(here) == 1:
            return here[0]
        refs = [n.ref for n in live]
        raise DriveNoteRefused(NoteRefusal.AMBIGUOUS_NOTE,
                               f"Several notes match this file (on different drives): use one of {', '.join(refs)}.",
                               refs=refs)

    async def _drive_id_or_empty(self, user_id: str) -> str:
        try:
            connection = await self._drive.connection(user_id)
        except Exception as exc:
            logger.warning(f"[DriveNote] connection read failed user={user_id[:8]}: {type(exc).__name__}")
            return ""
        return connection.drive_id if connection.connected else ""

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

    async def _indexing_enabled(self, user_id: str) -> Optional[bool]:
        """The owner's indexing switch; None when it could not be read."""
        try:
            profile = await self._users.get_user(user_id)
        except Exception as exc:
            logger.warning(f"[DriveNote] user read failed user={user_id[:8]}: {type(exc).__name__}: {exc}")
            return None
        return bool(profile is not None and profile.config.drive_indexing_enabled)

    async def _nudge(self, user_id: str, item_ids: Sequence[str]) -> None:
        """Tell the indexer which items changed (UserDriveService's nudge, RFC Δ3). Never raises."""
        ids = tuple(dict.fromkeys(i for i in item_ids if i))
        if self._nudge_queue is None or not ids:
            return
        nudge = Nudge(mutation_id=uuid.uuid4().hex, item_ids=ids)
        try:
            await self._nudge_queue.enqueue_worker_task(
                DRIVE_SYNC_TASK, tick_payload(user_id, nudge), deadline_seconds=TICK_DEADLINE_S,
                dedup_key=nudge_key(user_id, nudge.mutation_id, 0))
        except Exception as exc:
            logger.warning(f"[DriveNote] nudge failed user={user_id[:8]} items={len(ids)}: {exc}")

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
            ref = make_drive_ref(target.item_id) if target.item_id else ""
            return NoteReceipt(action="clear", note_id="", ref=ref, path="", version=0, state="", noop=True)
        stub = note.erase_pending
        return NoteReceipt(action="clear", note_id=note.note_id, ref=item_ref(note), path=self._path(note),
                           version=0 if stub else note.current_version, state=str(note.state), noop=True)

    @staticmethod
    def _log(op: str, user_id: str, receipt: NoteReceipt, *, words: int = 0) -> None:
        """One line per call: ids, counts, the version and the status, never the text."""
        logger.info(f"[DriveNote] {op} user={user_id[:8]} note={receipt.note_id} v={receipt.version} words={words} "
                    f"status={receipt.status or '-'} state={receipt.state or '-'} noop={receipt.noop}")
