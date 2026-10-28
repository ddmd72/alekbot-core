"""
FirestoreDriveNoteRepository — DriveNoteRepository on Firestore (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.1, §3.2, §3.5,
§4.4, §4.8).

Collection (env-prefixed through EnvironmentConfig.drive_notes_collection):
    {prefix}drive_notes/{user_id}:{note_id}             every DriveNote field under its own name (maps for `binding`,
                                                        `snapshot`, `last_writer`); `note_vector` a Vector, absent
                                                        when None; `updated_at` always SERVER_TIMESTAMP
    {prefix}drive_notes/{user_id}:{note_id}/versions/v{n}   every DriveNoteVersion field under its own name

The layout copies the skills store: a note's current state is one document read, history one subcollection read, and
only parent documents are queried (the versions need no composite). Durable (D3): no purge touches this collection.

Writes: `write_version` is one transaction (§4.4 step 5) that reads the parent first and raises DriveNoteConflict unless
its `current_version` (0 when missing or a stub) is the expected one AND its `revision` is the one the caller read — a
first write therefore needs the document not to exist, and any parent write in between (a lifecycle write included)
fails it; the precondition and the writes see the same snapshot, and Firestore retries the function on contention,
which re-reads. Every parent write sets `revision` to the stored one + 1. Lifecycle updates (archive, re-activation,
snapshot, vector) are transactions on the parent alone; erase is one
transaction per note that deletes `v1`..`v{current_version}` by name and writes the stub last.

Queries (composites in config/firestore.indexes.json, §3.5): `match_keys` array-contains[-any] (1); (`updated_at`,
`note_id`) ascending (2); `state` + `archived_at` desc (3); `state` + `updated_at` desc (4); `embedding_pending` (5);
the kNN on `note_vector` needs the vector index of §3.5 item 6 (S3's script). Reads that list notes project every field
but `note_vector`, which only the vector repair reads.

Errors: Firestore's transient errors and a missing composite index leave every public method as
DriveIndexStoreUnavailable (`_store_errors`); DriveNoteConflict and ValueError keep their type, and the client's
ValueError when contention outlasts its transaction retries becomes DriveNoteConflict (`_run`). Datetimes come back as
plain tz-aware UTC datetimes, never DatetimeWithNanoseconds.
"""
from __future__ import annotations

import functools
import inspect
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from google.api_core.exceptions import (
    Aborted, DeadlineExceeded, FailedPrecondition, InternalServerError, ResourceExhausted, ServiceUnavailable,
)
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.drive_index import DriveIndexStoreUnavailable
from ..domain.drive_note import (
    VERSIONS_SUBCOLLECTION, BoundBy, DriveNote, DriveNoteConflict, DriveNoteVersion, NoteSnapshot, NoteState,
    EraseResult, erase_stub, indexer_writer, note_doc_id,
)
from ..ports.drive_note_repository import DriveNoteRepository
from ..utils.logger import logger

IN_VALUES_MAX = 30                     # Firestore's limit on the values of one array-contains-any
TRANSACTION_WRITES_MAX = 500           # Firestore's limit on the writes of one transaction or batch
ERASE_ATTEMPTS = 5                     # passes of one note's erase while concurrent writes keep moving its history
_GET_BATCH = 300                       # documents per get_all
NOTE_VECTOR = "note_vector"
NOTE_DISTANCE = "note_distance"
LEAN_FIELDS: Tuple[str, ...] = tuple(f for f in DriveNote.model_fields if f != NOTE_VECTOR)


# -- serialization ------------------------------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """A read value with every timestamp as a plain tz-aware UTC datetime (never DatetimeWithNanoseconds, whose
    `.replace()` breaks a write-back) and every Vector as a list, recursively."""
    if isinstance(value, datetime):
        plain = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                         value.microsecond, tzinfo=value.tzinfo or timezone.utc)
        return plain.astimezone(timezone.utc)
    if isinstance(value, Vector):
        return [float(x) for x in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def note_to_doc(note: DriveNote) -> Dict[str, Any]:
    """The parent document of `note`: `note_vector` as a Vector (absent when None), `updated_at` left to the writer."""
    data = note.model_dump()
    data.pop("updated_at", None)
    vector = data.pop(NOTE_VECTOR, None)
    if vector is not None:
        data[NOTE_VECTOR] = Vector([float(x) for x in vector])
    return data


STUB_FIELDS: Tuple[str, ...] = ("note_id", "user_id", "account_id", "state", "erase_pending", "match_keys", "revision")


def stub_to_doc(stub: DriveNote) -> Dict[str, Any]:
    """An erase stub as stored (§4.8, I9): the tenant fields, `state`, `erase_pending` and `match_keys` only (and
    `updated_at`, by the writer). Read back, every other field takes its default."""
    data = stub.model_dump()
    return {name: data[name] for name in STUB_FIELDS}


def note_from_doc(data: Mapping[str, Any]) -> DriveNote:
    return DriveNote.model_validate(_plain(dict(data)))


def version_to_doc(version: DriveNoteVersion) -> Dict[str, Any]:
    return version.model_dump()


def version_from_doc(data: Mapping[str, Any]) -> DriveNoteVersion:
    return DriveNoteVersion.model_validate(_plain(dict(data)))


def version_doc_id(version: int) -> str:
    return f"v{version}"


def _chunks(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class FirestoreDriveNoteRepository(DriveNoteRepository):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._notes = db_client.collection(env_config.drive_notes_collection)

    def _ref(self, user_id: str, note_id: str) -> Any:
        return self._notes.document(note_doc_id(user_id, note_id))

    def _version_ref(self, user_id: str, note_id: str, version: int) -> Any:
        return self._ref(user_id, note_id).collection(VERSIONS_SUBCOLLECTION).document(version_doc_id(version))

    def _user(self, user_id: str) -> Any:
        return self._notes.where(filter=FieldFilter("user_id", "==", user_id))

    @staticmethod
    def _own(user_id: str, data: Optional[Mapping[str, Any]]) -> bool:
        """Ownership by the stored document, not only the id text."""
        return data is not None and data.get("user_id") == user_id

    async def _read(self, query: Any) -> List[DriveNote]:
        return [note_from_doc(snap.to_dict()) for snap in await query.get()]

    # -- reads by id and by key ----------------------------------------------------------------------------------------

    async def get(self, user_id: str, note_id: str) -> Optional[DriveNote]:
        snap = await self._ref(user_id, note_id).get()
        data = snap.to_dict() if snap.exists else None
        return note_from_doc(data) if data is not None and self._own(user_id, data) else None

    async def get_many(self, user_id: str, note_ids: Sequence[str]) -> Dict[str, DriveNote]:
        found: Dict[str, DriveNote] = {}
        for part in _chunks(list(dict.fromkeys(note_ids)), _GET_BATCH):
            async for snap in self._db.get_all([self._ref(user_id, i) for i in part]):
                data = snap.to_dict() if snap.exists else None
                if data is not None and self._own(user_id, data):
                    note = note_from_doc(data)
                    found[note.note_id] = note
        return found

    async def find_bound(self, user_id: str, binding_key: str) -> Optional[DriveNote]:
        query = self._user(user_id).where(filter=FieldFilter("match_keys", "array_contains", binding_key))
        found = sorted(await self._read(query), key=lambda n: n.note_id)
        return found[0] if found else None

    async def find_by_match_keys(self, user_id: str, keys: Sequence[str]) -> List[DriveNote]:
        found: Dict[str, DriveNote] = {}
        for part in _chunks(list(dict.fromkeys(k for k in keys if k)), IN_VALUES_MAX):
            query = self._user(user_id).where(filter=FieldFilter("match_keys", "array_contains_any", list(part)))
            for note in await self._read(query):
                found[note.note_id] = note
        return [found[i] for i in sorted(found)]

    async def note_ids(self, user_id: str) -> List[str]:
        """One projected query over the user's notes (at most NOTES_PER_USER_MAX plus stubs: two small fields each), so
        it needs no composite beyond the `user_id` single-field index."""
        snaps = await self._user(user_id).select(["note_id", "erase_pending"]).get()
        rows = [snap.to_dict() or {} for snap in snaps]
        return sorted(str(d["note_id"]) for d in rows if d.get("note_id") and not d.get("erase_pending"))

    async def count(self, user_id: str) -> int:
        result = await self._user(user_id).count(alias="n").get()
        return int(result[0][0].value)

    # -- history -----------------------------------------------------------------------------------------------------------

    async def get_version(self, user_id: str, note_id: str, version: int) -> Optional[DriveNoteVersion]:
        if version < 1:
            return None
        snap = await self._version_ref(user_id, note_id, version).get()
        return version_from_doc(snap.to_dict()) if snap.exists else None

    async def list_versions(self, user_id: str, note_id: str) -> List[DriveNoteVersion]:
        versions = self._ref(user_id, note_id).collection(VERSIONS_SUBCOLLECTION)
        snaps = await versions.order_by("version").get()
        return [version_from_doc(snap.to_dict()) for snap in snaps]

    # -- the owner-write transaction -------------------------------------------------------------------------------------

    async def write_version(self, note: DriveNote, version: DriveNoteVersion, *, expected_version: int) -> None:
        if version.version != expected_version + 1 or note.current_version != version.version:
            raise ValueError(f"write_version: version {version.version} / note {note.current_version} after "
                             f"{expected_version}")
        ref = self._ref(note.user_id, note.note_id)
        new_ref = self._version_ref(note.user_id, note.note_id, version.version)
        closing = self._version_ref(note.user_id, note.note_id, expected_version) if expected_version else None
        data = {**note_to_doc(note), "revision": note.revision + 1, "updated_at": firestore.SERVER_TIMESTAMP}
        version_data = version_to_doc(version)

        @firestore.async_transactional
        async def _write(transaction: Any) -> None:
            # Reads first (Firestore's transaction rule): the parent and the version to close.
            snap = await ref.get(transaction=transaction)
            stored = snap.to_dict() if snap.exists else None
            previous = await closing.get(transaction=transaction) if closing is not None else None
            if stored is not None and not self._own(note.user_id, stored):
                raise DriveNoteConflict(f"note {note.note_id}: the document belongs to another user")
            current = int((stored or {}).get("current_version") or 0)
            if current != expected_version:
                raise DriveNoteConflict(f"note {note.note_id}: stored version {current}, expected {expected_version}")
            revision = int((stored or {}).get("revision") or 0)
            if revision != note.revision:
                raise DriveNoteConflict(f"note {note.note_id}: stored revision {revision}, read {note.revision}")
            if previous is not None and previous.exists:     # a missing version is tolerated, as in memory
                transaction.update(closing, {"valid_to": version.valid_from, "is_current": False})
            transaction.set(new_ref, version_data)
            transaction.set(ref, data)

        await _run(_write, self._db.transaction(), note.note_id)

    # -- the tick and the deleted-files search -----------------------------------------------------------------------

    async def changed_since(self, user_id: str, after: Optional[datetime], limit: int, *,
                            after_note_id: str = "") -> List[DriveNote]:
        query = self._user(user_id)
        if after is not None and not after_note_id:
            query = query.where(filter=FieldFilter("updated_at", ">", after))
        query = query.order_by("updated_at").order_by("note_id")
        if after is not None and after_note_id:
            query = query.start_after({"updated_at": after, "note_id": after_note_id})
        return await self._read(query.select(list(LEAN_FIELDS)).limit(limit))

    async def embedding_pending(self, user_id: str, limit: int) -> List[DriveNote]:
        query = self._user(user_id).where(filter=FieldFilter("embedding_pending", "==", True)).limit(limit)
        return sorted(await self._read(query), key=lambda n: n.note_id)

    async def archived(self, user_id: str, limit: int) -> List[DriveNote]:
        query = (self._user(user_id).where(filter=FieldFilter("state", "==", NoteState.ARCHIVED.value))
                 .order_by("archived_at", direction=firestore.Query.DESCENDING).select(list(LEAN_FIELDS)).limit(limit))
        return await self._read(query)

    async def active(self, user_id: str, limit: int) -> List[DriveNote]:
        query = (self._user(user_id).where(filter=FieldFilter("state", "==", NoteState.ACTIVE.value))
                 .order_by("updated_at", direction=firestore.Query.DESCENDING).select(list(LEAN_FIELDS)).limit(limit))
        return await self._read(query)

    async def find_nearest(self, user_id: str, vector: Sequence[float], *, state: str, embedding_space: str,
                           limit: int) -> List[Tuple[DriveNote, float]]:
        query = (self._user(user_id).where(filter=FieldFilter("state", "==", state))
                 .where(filter=FieldFilter("embedding_space", "==", embedding_space)))
        # the distance comes back under a projection only when its name is selected too (the drive index's finding)
        projected = query.select([*LEAN_FIELDS, NOTE_DISTANCE])
        nearest = projected.find_nearest(vector_field=NOTE_VECTOR, query_vector=Vector([float(x) for x in vector]),
                                         limit=limit, distance_measure=DistanceMeasure.COSINE,
                                         distance_result_field=NOTE_DISTANCE)
        hits: List[Tuple[DriveNote, float]] = []
        for snap in await nearest.get():
            data = dict(snap.to_dict() or {})
            distance = float(data.pop(NOTE_DISTANCE))
            hits.append((note_from_doc(data), distance))
        return hits

    # -- lifecycle ----------------------------------------------------------------------------------------------------------

    async def _update_parent(self, user_id: str, note_id: str,
                             change: Callable[[DriveNote], Optional[Dict[str, Any]]]) -> bool:
        """One transaction on the parent: `change(note)` gives the fields to update, or None to leave it. Every update
        bumps `revision` and `updated_at` and stamps `last_writer` = indexer (lifecycle writes are the tick's)."""
        ref = self._ref(user_id, note_id)

        @firestore.async_transactional
        async def _update(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            stored = snap.to_dict() if snap.exists else None
            if stored is None or not self._own(user_id, stored):
                return False
            fields = change(note_from_doc(stored))
            if fields is None:
                return False
            transaction.update(ref, {**fields, "last_writer": indexer_writer().model_dump(),
                                     "revision": int(stored.get("revision") or 0) + 1,
                                     "updated_at": firestore.SERVER_TIMESTAMP})
            return True

        return bool(await _run(_update, self._db.transaction(), note_id))

    async def archive(self, user_id: str, note_id: str, *, archived_at: datetime, last_seen_at: Optional[datetime],
                      snapshot: Optional[NoteSnapshot] = None) -> bool:
        def change(note: DriveNote) -> Optional[Dict[str, Any]]:
            if note.erase_pending:
                return None
            if note.state == NoteState.ACTIVE.value:
                fields: Dict[str, Any] = {"state": NoteState.ARCHIVED.value}
            elif note.state == NoteState.CLEARED.value and note.archived_at is None:
                fields = {}                                   # stamped, state kept (P4)
            else:
                return None
            fields.update({"archived_at": archived_at, "last_seen_at": last_seen_at})
            if snapshot is not None:
                fields["snapshot"] = snapshot.model_dump()
            return fields
        return await self._update_parent(user_id, note_id, change)

    async def reactivate(self, user_id: str, note_id: str, *, bound_at: datetime) -> bool:
        def change(note: DriveNote) -> Optional[Dict[str, Any]]:
            if note.erase_pending:
                return None
            if note.state == NoteState.ARCHIVED.value and note.binding is not None:
                binding = note.binding.model_copy(update={"bound_by": BoundBy.RESTORE.value, "bound_at": bound_at})
                return {"state": NoteState.ACTIVE.value, "archived_at": None, "binding": binding.model_dump()}
            if note.state == NoteState.CLEARED.value and note.archived_at is not None:
                return {"archived_at": None}
            return None
        return await self._update_parent(user_id, note_id, change)

    async def refresh_snapshot(self, user_id: str, note_id: str, snapshot: NoteSnapshot) -> bool:
        def change(note: DriveNote) -> Optional[Dict[str, Any]]:
            return None if note.erase_pending else {"snapshot": snapshot.model_dump()}
        return await self._update_parent(user_id, note_id, change)

    async def set_vector(self, user_id: str, note_id: str, vector: Sequence[float], embedding_space: str, *,
                         expected_version: int) -> bool:
        def change(note: DriveNote) -> Optional[Dict[str, Any]]:
            if (note.erase_pending or note.state == NoteState.CLEARED.value
                    or note.current_version != expected_version):
                return None
            return {NOTE_VECTOR: Vector([float(x) for x in vector]), "embedding_space": embedding_space,
                    "embedding_pending": False}
        return await self._update_parent(user_id, note_id, change)

    # -- erase ----------------------------------------------------------------------------------------------------------------

    async def _erase_one(self, user_id: str, note_id: str) -> bool:
        ref = self._ref(user_id, note_id)
        in_transaction = TRANSACTION_WRITES_MAX - 1                   # the stub takes one write
        for _ in range(ERASE_ATTEMPTS):
            snap = await ref.get()
            stored = snap.to_dict() if snap.exists else None
            if stored is None or not self._own(user_id, stored):
                return False
            current = int(stored.get("current_version") or 0)
            oldest = list(range(1, max(current - in_transaction, 0) + 1))
            for part in _chunks(oldest, TRANSACTION_WRITES_MAX):     # a long history: its oldest versions go first
                batch = self._db.batch()
                for v in part:
                    batch.delete(self._version_ref(user_id, note_id, v))
                await batch.commit()

            @firestore.async_transactional
            async def _stub(transaction: Any) -> Optional[bool]:
                fresh = await ref.get(transaction=transaction)
                data = fresh.to_dict() if fresh.exists else None
                if data is None or not self._own(user_id, data):
                    return False
                note = note_from_doc(data)
                if note.current_version > current and note.current_version > in_transaction:
                    return None                               # a write landed since the batches: redo them
                for v in range(1, note.current_version + 1)[-in_transaction:]:
                    transaction.delete(self._version_ref(user_id, note_id, v))
                transaction.set(ref, {**stub_to_doc(erase_stub(note)), "revision": note.revision + 1,
                                      "updated_at": firestore.SERVER_TIMESTAMP})
                return True

            done = await _run(_stub, self._db.transaction(), note_id)
            if done is not None:
                return bool(done)
        logger.warning(f"[DriveNote] erase of {note_id} kept moving under concurrent writes; refused")
        raise DriveNoteConflict(f"note {note_id}: its history kept changing during the erase")

    async def erase(self, user_id: str, note_ids: Sequence[str]) -> EraseResult:
        erased = 0
        left: List[str] = []
        for note_id in dict.fromkeys(note_ids):
            try:
                done = await self._erase_one(user_id, note_id)
            except DriveNoteConflict:
                left.append(note_id)            # logged where it was raised; the rest are still erased
                continue
            except _TRANSIENT as exc:
                logger.warning(f"[DriveNote] erase of {note_id} failed: {type(exc).__name__}: {exc}")
                left.append(note_id)
                continue
            except _MISSING_INDEX as exc:   # the same level `_store_errors` gives it: an operator must act
                logger.error(f"[DriveNote] erase of {note_id} failed, an index is missing or building: "
                             f"{type(exc).__name__}: {exc}")
                left.append(note_id)
                continue
            erased += int(done)
        return EraseResult(erased=erased, left=tuple(left))

    async def delete_stub(self, user_id: str, note_id: str) -> bool:
        ref = self._ref(user_id, note_id)

        @firestore.async_transactional
        async def _delete(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            data = snap.to_dict() if snap.exists else None
            if data is None or not self._own(user_id, data) or not data.get("erase_pending"):
                return False
            transaction.delete(ref)
            return True

        return bool(await _run(_delete, self._db.transaction(), note_id))


async def _run(function: Callable[[Any], Awaitable[Any]], transaction: Any, note_id: str) -> Any:
    """Run a transactional function. When contention outlasts Firestore's own retries the client raises a plain
    ValueError ("Failed to commit transaction in N attempts") from the last Aborted: that, and only that, is a conflict
    for the caller. Any other ValueError (a pydantic ValidationError from a corrupt document, a client misuse) keeps
    its own type."""
    try:
        return await function(transaction)
    except ValueError as exc:
        if not isinstance(exc.__cause__, Aborted):
            raise
        logger.warning(f"[DriveNote] transaction on {note_id} gave up under contention: {exc}")
        raise DriveNoteConflict(f"note {note_id}: contention outlasted the transaction's retries") from exc


# Transient on Firestore's side: the same call may succeed later. A query whose composite index is missing or still
# building does not heal by itself: logged as an error and, like a transient error, DriveIndexStoreUnavailable.
_TRANSIENT = (DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted, ResourceExhausted)
_MISSING_INDEX = (FailedPrecondition,)


def _store_errors(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except _TRANSIENT as exc:
            logger.warning(f"[DriveNote] note store {method.__name__} failed: {type(exc).__name__}: {exc}")
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}") from exc
        except _MISSING_INDEX as exc:
            logger.error(f"[DriveNote] note store {method.__name__} failed, an index is missing or building: "
                         f"{type(exc).__name__}: {exc}")
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}: {exc}") from exc
    return run


for _name, _method in list(vars(FirestoreDriveNoteRepository).items()):
    if not _name.startswith("_") and inspect.iscoroutinefunction(_method):
        setattr(FirestoreDriveNoteRepository, _name, _store_errors(_method))
