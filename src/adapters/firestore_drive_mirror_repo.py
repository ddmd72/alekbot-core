"""
FirestoreDriveMirrorRepository — DriveMirrorRepository on Firestore (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.2,
§27.9.3, §27.15 step 8c).

Collections (env-prefixed through EnvironmentConfig):
    {prefix}drive_mirror_roots        doc id `mirror_root_doc_id(user_id, drive_identity)`; every MirrorRoot field under
                                      its own name
    {prefix}drive_transcript_mirror   doc id MirrorDoc.key (`ledger_key(user_id, kind, drive_identity, source_identity)`,
                                      or the archive folder's hashed key); every MirrorDoc field under its own name

Both codecs are built from `fields(...)` of the domain class, so a field added there is carried without a second edit:
datetimes come back as plain tz-aware UTC datetimes (never DatetimeWithNanoseconds), tuples as tuples, a missing stored
field takes the dataclass default (schema growth).

Queries (the composite indexes are in config/firestore.indexes.json, §27.15 step 8c): every read of the mirror step
filters `user_id` and `drive_identity` (Δ206, MULTI_DRIVE_RFC §10); the cursor reads page on the document id (the implicit order, so `after_key`
is a range on `__name__`); the deleted-files search and the pins span drives (Δ217), and `pinned`/`pinned_keys` send
two queries each, one per pinning condition, never one OR filter. Writes: `save_*` replace the whole document; the mark
methods update only the mark fields of an existing document (never create one).

Errors: Firestore's transient errors and a missing composite index leave every public method as
DriveIndexStoreUnavailable, as in the drive index repository (`_store_errors`).
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import fields
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, get_type_hints

from google.api_core.exceptions import (
    Aborted, DeadlineExceeded, FailedPrecondition, InternalServerError, ResourceExhausted, ServiceUnavailable,
)
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.field_path import FieldPath

from ..config.environment import EnvironmentConfig
from ..domain.drive_index import DriveIndexStoreUnavailable
from ..domain.drive_index_params import RECORD_WRITE_BATCH
from ..domain.drive_mirror import (
    KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ARCHIVED, MIRROR_DETACHED, DeletionNotice, MirrorDoc,
    MirrorRoot, ledger_key, mirror_root_doc_id,
)
from ..domain.drive_mirror_plan import mark_held, marked, unmarked
from ..ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY, DriveMirrorRepository
from ..utils.logger import exc_brief, logger

IN_VALUES_MAX = 30                              # Firestore's limit on the values of one array-contains-any
PIN_PAGE = 500                                  # content keys read per page of the pins' two queries
_GET_BATCH = 300                                # documents per get_all
_PINNING_STATES = (MIRROR_ARCHIVED, MIRROR_DETACHED)
_FOLDER_KINDS = (KIND_FOLDER, KIND_ARCHIVE)
_MARK_FIELDS = ("gone_pending", "gone_why", "gone_at", "mark_ready_at")
_FAMILY_FIELDS = {ARCHIVE_FAMILY: "archive_keys", NAME_FAMILY: "name_keys"}


# -- serialization ------------------------------------------------------------------------------

def _dt(value: Any) -> Optional[datetime]:
    """A stored timestamp as a plain tz-aware UTC datetime (never DatetimeWithNanoseconds, whose `.replace()` breaks a
    write-back); None stays None."""
    if value is None:
        return None
    plain = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                     value.microsecond, tzinfo=value.tzinfo or timezone.utc)
    return plain.astimezone(timezone.utc)


def _strs(value: Any) -> Tuple[str, ...]:
    return tuple(str(v) for v in (value or ()))


def _plain(value: Any) -> Any:
    """Firestore-encodable copy of a field value: tuples as lists (the other field types are stored as they are)."""
    return [_plain(v) for v in value] if isinstance(value, (list, tuple)) else value


def _reader(annotation: Any) -> Callable[[Any], Any]:
    """The reader of one stored value, from the dataclass field's type: Optional[datetime] -> `_dt`, a tuple of strings
    -> `_strs`, anything else its own type (str, int, float, bool)."""
    text = str(annotation)
    if "datetime" in text:
        return _dt
    if "Tuple" in text or "tuple" in text:
        return _strs
    return annotation if annotation in (str, int, float, bool) else (lambda v: v)


def _readers(cls: type) -> Dict[str, Callable[[Any], Any]]:
    hints = get_type_hints(cls)
    return {f.name: _reader(hints[f.name]) for f in fields(cls)}   # type: ignore[arg-type]


_ROOT_READERS = _readers(MirrorRoot)
_DOC_READERS = _readers(MirrorDoc)


def root_to_doc(root: MirrorRoot) -> Dict[str, Any]:
    return {f.name: _plain(getattr(root, f.name)) for f in fields(root)}


def _kwargs(data: Mapping[str, Any], readers: Mapping[str, Callable[[Any], Any]]) -> Dict[str, Any]:
    """The stored fields that are present, read; a stored null stays null only for a timestamp (the Optional fields),
    anything else takes the dataclass default."""
    return {name: read(data[name]) for name, read in readers.items()
            if name in data and (data[name] is not None or read is _dt)}


def root_from_doc(data: Mapping[str, Any]) -> MirrorRoot:
    return MirrorRoot(**_kwargs(data, _ROOT_READERS))


def doc_to_doc(doc: MirrorDoc) -> Dict[str, Any]:
    return {f.name: _plain(getattr(doc, f.name)) for f in fields(doc)}


def doc_from_doc(data: Mapping[str, Any]) -> MirrorDoc:
    return MirrorDoc(**_kwargs(data, _DOC_READERS))


def _chunks(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class FirestoreDriveMirrorRepository(DriveMirrorRepository):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._roots = db_client.collection(env_config.drive_mirror_roots_collection)
        self._docs = db_client.collection(env_config.drive_transcript_mirror_collection)

    def _ledger(self, user_id: str, drive_identity: str) -> Any:
        return (self._docs.where(filter=FieldFilter("user_id", "==", user_id))
                .where(filter=FieldFilter("drive_identity", "==", drive_identity)))

    @staticmethod
    async def _read(query: Any) -> List[MirrorDoc]:
        return [doc_from_doc(snap.to_dict()) for snap in await query.get()]

    # -- roots -----------------------------------------------------------------------------------

    async def roots(self, user_id: str) -> List[MirrorRoot]:
        snaps = await self._roots.where(filter=FieldFilter("user_id", "==", user_id)).get()
        return sorted((root_from_doc(snap.to_dict()) for snap in snaps), key=lambda r: r.drive_identity)

    async def get_root(self, user_id: str, drive_identity: str) -> Optional[MirrorRoot]:
        snap = await self._roots.document(mirror_root_doc_id(user_id, drive_identity)).get()
        return root_from_doc(snap.to_dict()) if snap.exists else None

    async def save_root(self, root: MirrorRoot) -> None:
        await self._roots.document(root.key).set(root_to_doc(root))

    async def _roots_of_item(self, user_id: str, root_item_id: str) -> List[Any]:
        query = (self._roots.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("root_item_id", "==", root_item_id)))
        return list(await query.get())

    async def mark_root_gone(self, user_id: str, root_item_id: str, at: datetime) -> bool:
        snaps = await self._roots_of_item(user_id, root_item_id)
        for snap in snaps:
            if root_from_doc(snap.to_dict()).root_gone_at is None:   # the earlier mark stays
                await snap.reference.update({"root_gone_at": at})
        return bool(snaps)

    async def clear_root_gone(self, user_id: str, root_item_id: str) -> bool:
        snaps = await self._roots_of_item(user_id, root_item_id)
        for snap in snaps:
            await snap.reference.update({"root_gone_at": None})
        return bool(snaps)

    # -- ledger documents ---------------------------------------------------------------------

    def _ref(self, key: str) -> Any:
        return self._docs.document(key)

    async def get_docs(self, user_id: str, keys: Sequence[str]) -> Dict[str, MirrorDoc]:
        found: Dict[str, MirrorDoc] = {}
        own = [k for k in dict.fromkeys(keys) if k.startswith(f"{user_id}:")]
        for part in _chunks(own, _GET_BATCH):
            async for snap in self._db.get_all([self._ref(k) for k in part]):
                if snap.exists:
                    doc = doc_from_doc(snap.to_dict())
                    if doc.user_id == user_id:
                        found[doc.key] = doc
        return found

    async def save_doc(self, doc: MirrorDoc) -> None:
        await self._ref(doc.key).set(doc_to_doc(doc))

    async def delete_doc(self, user_id: str, key: str) -> None:
        ref = self._ref(key)
        snap = await ref.get()
        if snap.exists and (snap.to_dict() or {}).get("user_id") == user_id:   # ownership by the document, not the key text
            await ref.delete()

    async def by_mirror_item(self, user_id: str, mirror_item_id: str) -> Optional[MirrorDoc]:
        query = (self._docs.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("mirror_item_id", "==", mirror_item_id)).limit(1))
        found = await self._read(query)
        return found[0] if found else None

    # -- marks -------------------------------------------------------------------------------

    @staticmethod
    def _mark_update(doc: MirrorDoc) -> Dict[str, Any]:
        return {name: _plain(getattr(doc, name)) for name in _MARK_FIELDS}

    async def mark_gone(self, user_id: str, marks: Sequence[DeletionNotice], *, drive_identity: str) -> int:
        if not drive_identity:
            raise ValueError("mark_gone needs the mirror's drive identity")
        by_key: Dict[str, List[DeletionNotice]] = {}
        for notice in marks:
            if not notice.source_identity:
                continue   # names no document: a ledger key is built from the source identity (§10)
            for kind in (KIND_FILE, KIND_FOLDER):
                by_key.setdefault(ledger_key(user_id, kind, drive_identity, notice.source_identity), []).append(notice)
        existing = await self.get_docs(user_id, list(by_key))
        # the key carries the drive (§10); the stored field is checked too, as every drive-scoped read does
        existing = {k: d for k, d in existing.items() if d.drive_identity == drive_identity}
        updates = []
        for key, doc in existing.items():
            for notice in by_key[key]:
                doc = marked(doc, notice)
            updates.append((key, self._mark_update(doc)))
        for part in _chunks(updates, RECORD_WRITE_BATCH):   # a folder of recordings deleted at once: one batch is too big
            batch = self._db.batch()
            for key, data in part:
                batch.update(self._ref(key), data)
            await batch.commit()
        return len(existing)

    async def clear_mark(self, user_id: str, key: str) -> None:
        doc = (await self.get_docs(user_id, [key])).get(key)
        if doc is not None:
            await self._ref(key).update(self._mark_update(unmarked(doc)))

    async def hold_mark(self, user_id: str, key: str, until: datetime) -> None:
        doc = (await self.get_docs(user_id, [key])).get(key)
        if doc is not None:
            held = mark_held(doc, until)
            if held != doc:
                await self._ref(key).update({"mark_ready_at": held.mark_ready_at})

    async def ready_marks(self, user_id: str, drive_identity: str, now: datetime, limit: int) -> List[MirrorDoc]:
        query = (self._ledger(user_id, drive_identity).where(filter=FieldFilter("gone_pending", "==", True))
                 .where(filter=FieldFilter("mark_ready_at", "<=", now)).order_by("mark_ready_at").limit(limit))
        return await self._read(query)

    # -- the step's reads -----------------------------------------------------------------------

    async def children(self, user_id: str, drive_identity: str, mirror_parent_id: str, limit: int) -> List[MirrorDoc]:
        query = self._ledger(user_id, drive_identity).where(filter=FieldFilter("mirror_parent_id", "==", mirror_parent_id))
        return await self._read(query.limit(limit))

    def _by_key(self, query: Any, after_key: Optional[str], limit: int) -> Any:
        query = query.order_by(FieldPath.document_id())
        if after_key is not None:
            query = query.where(filter=FieldFilter(FieldPath.document_id(), ">", self._ref(after_key)))
        return query.limit(limit)

    async def folder_docs(self, user_id: str, drive_identity: str, after_key: Optional[str],
                          limit: int) -> List[MirrorDoc]:
        query = self._ledger(user_id, drive_identity).where(filter=FieldFilter("kind", "in", list(_FOLDER_KINDS)))
        return await self._read(self._by_key(query, after_key, limit))

    async def scan_page(self, user_id: str, drive_identity: str, after_key: Optional[str], limit: int) -> List[MirrorDoc]:
        query = self._ledger(user_id, drive_identity).where(filter=FieldFilter("kind", "==", KIND_FILE))
        return await self._read(self._by_key(query, after_key, limit))

    async def detached_due(self, user_id: str, drive_identity: str, before: datetime, limit: int) -> List[MirrorDoc]:
        base = self._ledger(user_id, drive_identity).where(filter=FieldFilter("state", "==", MIRROR_DETACHED))
        never = await self._read(base.where(filter=FieldFilter("checked_at", "==", None)).limit(limit))
        if len(never) >= limit:
            return never
        old = await self._read(base.where(filter=FieldFilter("checked_at", "<", before))
                               .order_by("checked_at").limit(limit - len(never)))
        return never + old

    # -- the deleted-files search ---------------------------------------------------------------

    async def archived_newest(self, user_id: str, limit: int) -> List[MirrorDoc]:
        query = (self._docs.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("state", "==", MIRROR_ARCHIVED))
                 .order_by("archived_at", direction=firestore.Query.DESCENDING).limit(limit))
        return await self._read(query)

    async def archived_by_keys(self, user_id: str, family: str, keys: Sequence[str], limit: int) -> List[MirrorDoc]:
        if family not in _FAMILY_FIELDS:
            raise ValueError(f"archived_by_keys: unknown family {family!r}")
        wanted = list(dict.fromkeys(keys))[:IN_VALUES_MAX]
        if not wanted:
            return []
        query = (self._docs.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("state", "==", MIRROR_ARCHIVED))
                 .where(filter=FieldFilter(_FAMILY_FIELDS[family], "array_contains_any", wanted)).limit(limit))
        return sorted(await self._read(query), key=lambda d: d.key)

    # -- pins -----------------------------------------------------------------------------------------

    async def pinned(self, user_id: str, content_key: str) -> bool:
        if not content_key:
            return False
        base = (self._docs.where(filter=FieldFilter("user_id", "==", user_id))
                .where(filter=FieldFilter("content_key", "==", content_key)))
        by_state = base.where(filter=FieldFilter("state", "in", list(_PINNING_STATES))).limit(1)
        if await by_state.get():
            return True
        return bool(await base.where(filter=FieldFilter("gone_pending", "==", True)).limit(1).get())

    async def pinned_keys(self, user_id: str) -> FrozenSet[str]:
        """Equality filters only, paged by document id (final review I4, Δ237): `user_id` and one pinning condition,
        no range and no ordering on any field but the document id, so the single-field indexes serve both queries (the
        `transcript_records` practice) and no composite index is needed. Every purge of every user reads this, and a
        privacy purge must not depend on a mirror-only index (T-R14). Firestore returns an equality query in
        document-id order; the next page starts after the last id."""
        keys: set = set()
        for condition in (FieldFilter("state", "in", list(_PINNING_STATES)),
                          FieldFilter("gone_pending", "==", True)):
            after: Optional[str] = None
            while True:
                query = self._docs.where(filter=FieldFilter("user_id", "==", user_id)).where(filter=condition)
                if after is not None:
                    query = query.where(filter=FieldFilter(FieldPath.document_id(), ">", self._ref(after)))
                snaps = list(await query.select(["content_key"]).limit(PIN_PAGE).get())
                keys.update(key for snap in snaps if (key := (snap.to_dict() or {}).get("content_key", "")))
                if len(snaps) < PIN_PAGE:
                    break
                after = snaps[-1].id
        return frozenset(keys)


# Transient on Firestore's side: the same call may succeed later. A query whose composite index is missing or still
# building does not heal by itself: logged as an error and, like a transient error, systemic for the tick (§8.2).
_TRANSIENT = (DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted, ResourceExhausted)
_MISSING_INDEX = (FailedPrecondition,)


def _store_errors(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except _TRANSIENT as exc:
            logger.warning(f"[DriveMirror] mirror store {method.__name__} failed: {exc_brief(exc)}")
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}") from exc
        except _MISSING_INDEX as exc:
            # the log line carries the type and a digest only (§27.12, final review M3): the provider's message holds the
            # index-creation console URL with the project id; the raised error keeps it for the smoke check and the
            # deploy step, which print it to the operator
            logger.error(f"[DriveMirror] mirror store {method.__name__} failed, an index is missing or building: "
                         f"{exc_brief(exc)}")
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}: {exc}") from exc
    return run


for _name, _method in list(vars(FirestoreDriveMirrorRepository).items()):
    if not _name.startswith("_") and inspect.iscoroutinefunction(_method):
        setattr(FirestoreDriveMirrorRepository, _name, _store_errors(_method))
