"""
FirestoreDriveIndexRepository — DriveIndexRepository on Firestore (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5).

Collections (env-prefixed through EnvironmentConfig):
    {prefix}drive_index_records   doc id record_key(user_id, item_id); every DriveIndexRecord field under its own
                                  name, plus `card_vector` (Vector, 768) and `has_card_vector` (bool, mirrors it)
    {prefix}drive_index_chunks    doc id chunk_key(user_id, item_id, generation, index); every DriveChunk field
                                  under its own name, plus `vector` (Vector, 768) (§3.3, §24.6)
    {prefix}drive_sync_state      doc id user_id; every DriveSyncState field under its own name

Reference semantics: InMemoryDriveIndexRepository. The deliberate differences are Firestore's own:
    - `due_records` reads one query per priority class (0..4) and stops when the page is full, so every query
      orders by its own inequality field first. A record due without a priority would never be returned, so
      `upsert_records` refuses it (ValueError), as the in-memory repository does.
    - `lowest_ranked_carded` has no `state` filter: a `deleting` record carries no card (the planner drops it),
      so it never has `has_card_vector == True`.
    - ties the port does not order (`filter_records`, `card_capped`, `records_seen_before`) break on the doc id.

Writes:
    - A record write is `set(doc, merge=[every record field])`: each listed top-level field is replaced whole (a
      nested map such as `facets` loses stale keys), and the fields not listed — `card_vector` and its mirror — are
      kept. A given vector adds both to the merge list; a record with has_card=False lists them with DELETE_FIELD.
    - `create_sync_state_if_absent` is a document `create()`: AlreadyExists means a state is there, nothing written.
    - `save_sync_state` merges every state field except `stale_alerted` and `reconnect_notified`; only
      `update_sync_flags` writes those two, with `update`, so a missing state stays missing.
    - `save_sync_state` never writes `lease_owner`/`lease_until` either: the lease is taken and released in
      transactions by `acquire_lease`/`release_lease` only (a creating `acquire_lease` writes it with the new
      state). A tick that outlived its lease would otherwise write it back and then release the new holder's.
    - `save_sync_state` and `delete_sync_state` (a purge, the whole document, the lease with it) run in a
      transaction that reads `lease_owner` first and writes only when it is the caller's `owner` (None for a missing
      document); otherwise LeaseLost and nothing is written (§7.1 fencing).

Vectors: record queries, `get_records` and `find_nearest_cards` project the record fields, so they never load
`card_vector`; only `card_vectors` reads it (its purpose). The kNN projection lists `card_distance` as well. Chunk
reads likewise project the chunk fields (`chunk_distance` with the kNN); only `get_chunks_with_vectors` reads `vector`.
The two search streams (`find_nearest_chunks`, `find_chunks_by_term`) leave out `text` and `content_terms` as well:
a widened page is up to FETCH_MAX chunks, and windows are read by id with `get_chunks`.
Chunk reads are never fenced: the search service compares each chunk's generation with its record (§7.4).
Datetimes come back as plain tz-aware UTC datetimes, never DatetimeWithNanoseconds (see
firestore_oauth_credentials_adapter.py: `.replace()` on one breaks write-back).
"""
from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import datetime, timezone
from enum import Enum
from typing import AbstractSet, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.field_path import FieldPath
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.drive_index import (
    ContentClass, DocumentBrief, DriveChunk, DriveIndexRecord, DriveSyncState, EpochPhase, ErrorCategory, LeaseLost,
    Priority, Reason, RecordState, Scope, TriageVerdict, record_key,
)
from ..domain.drive_index_params import NAME_QUERY_KEYS_MAX, RECORD_WRITE_BATCH
from ..ports.drive_index_repository import DriveIndexRepository
from ..utils.logger import logger

CARD_VECTOR = "card_vector"
HAS_CARD_VECTOR = "has_card_vector"
CARD_DISTANCE = "card_distance"
CHUNK_VECTOR = "vector"
CHUNK_DISTANCE = "chunk_distance"
_TS_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)   # Firestore's smallest timestamp: `>=` it excludes nulls
_FLAGS = ("stale_alerted", "reconnect_notified")
_LEASE = ("lease_owner", "lease_until")
IN_VALUES_MAX = 30                                  # Firestore `in` limit (and the disjunction limit per query)
# metadata_only reasons that leave an admitted document unread (drive_search.is_unread, §10.2)
UNREAD_REASONS: Tuple[Reason, ...] = (Reason.TOO_LARGE, Reason.NO_TEXT, Reason.MACHINE_TEXT, Reason.USER_CHUNK_CAP)
RECORD_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveIndexRecord))
CHUNK_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveChunk))
# search streams (port): no text (windows read it by id) and no content_terms (up to 256 per chunk; the term stream
# knows its term from its own query, review D-M3)
STREAM_FIELDS: Tuple[str, ...] = tuple(name for name in CHUNK_FIELDS if name not in ("text", "content_terms"))
_DOC_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveSyncState) if f.name not in _FLAGS)
STATE_FIELDS: Tuple[str, ...] = tuple(name for name in _DOC_FIELDS if name not in _LEASE)   # save_sync_state's


# -- serialization ------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """Firestore-encodable copy: enums as values, mappings as dicts, tuples as lists (recursively)."""
    if isinstance(value, DocumentBrief):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(DocumentBrief)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _dt(value: Any) -> Optional[datetime]:
    """A stored timestamp as a plain tz-aware UTC datetime (never DatetimeWithNanoseconds)."""
    if value is None:
        return None
    plain = datetime(value.year, value.month, value.day, value.hour, value.minute, value.second,
                     value.microsecond, tzinfo=value.tzinfo or timezone.utc)
    return plain.astimezone(timezone.utc)


def _optional(cast: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda v: None if v is None else cast(v)


def _int_map(value: Any) -> Dict[str, int]:
    return {str(k): int(v) for k, v in (value or {}).items()}


def _brief(value: Any) -> Optional[DocumentBrief]:
    if not value:
        return None
    return DocumentBrief(doc_type=value.get("doc_type", ""), description=value.get("description", ""),
                         doc_date=_dt(value.get("doc_date")), parties=tuple(value.get("parties") or ()),
                         amount=value.get("amount", ""))


_RECORD_READERS: Dict[str, Callable[[Any], Any]] = {
    "facets": lambda v: dict(v or {}),
    "content_class": ContentClass,
    "scope": Scope,
    "state": RecordState,
    "reason": _optional(Reason),
    "modified_at": _dt, "fs_modified_at": _dt, "effective_date": _dt,
    "dirty_since": _dt, "due_at": _dt, "attempt_started_at": _dt, "defer_until": _dt,
    "name_tokens": lambda v: tuple(v or ()),
    "priority": _optional(int), "defer_priority": _optional(int),
    "brief": _brief,
    "triage_verdict": _optional(TriageVerdict),
}

_STATE_READERS: Dict[str, Callable[[Any], Any]] = {
    "epoch_phase": EpochPhase,
    "last_error": _optional(ErrorCategory),
    "purging": bool, "systemic_failures": int, "created_at": _dt,
    "applied_versions": _int_map, "target_versions": _int_map, "counters": _int_map,
    "round_started_at": _dt, "lease_until": _dt, "continuation_enqueued_at": _dt,
    "observed_as_of": _dt, "access_lost_at": _dt, "last_success_at": _dt,
    "stale_alerted": bool, "reconnect_notified": bool,
    "model_backoff_until": _dt, "model_failures": int, "model_pages_used": int, "triage_images_used": int,
}


def record_to_doc(rec: DriveIndexRecord) -> Dict[str, Any]:
    """Every record field under its own name; the vector is not part of the record."""
    return {name: _plain(getattr(rec, name)) for name in RECORD_FIELDS}


def record_from_doc(data: Mapping[str, Any]) -> DriveIndexRecord:
    """Missing fields take the dataclass default (schema growth); vector and extra fields are ignored."""
    kwargs = {name: _RECORD_READERS.get(name, lambda v: v)(data[name]) for name in RECORD_FIELDS if name in data}
    return DriveIndexRecord(**kwargs)


def state_to_doc(state: DriveSyncState) -> Dict[str, Any]:
    """Every state field except the fan-out flags (their only writer is update_sync_flags). Includes the lease,
    which only a creating `acquire_lease` writes from here; `save_sync_state` sends just STATE_FIELDS."""
    return {name: _plain(getattr(state, name)) for name in _DOC_FIELDS}


def state_from_doc(user_id: str, data: Mapping[str, Any]) -> DriveSyncState:
    kwargs = {name: _STATE_READERS.get(name, lambda v: v)(data[name])
              for f in fields(DriveSyncState) if (name := f.name) in data and name != "user_id"}
    return DriveSyncState(user_id=user_id, **kwargs)


def chunk_key(user_id: str, item_id: str, generation: int, index: int) -> str:
    return f"{user_id}:{item_id}:{generation}:{index}"


def chunk_to_doc(chunk: DriveChunk) -> Dict[str, Any]:
    """Every chunk field under its own name; the vector is not part of the chunk."""
    return {name: _plain(getattr(chunk, name)) for name in CHUNK_FIELDS}


def chunk_from_doc(data: Mapping[str, Any]) -> DriveChunk:
    """Vector, distance and extra fields are ignored."""
    return DriveChunk(user_id=data["user_id"], item_id=data["item_id"], generation=int(data["generation"]),
                      index=int(data["index"]), text=data.get("text", ""), char_start=int(data.get("char_start", 0)),
                      char_end=int(data.get("char_end", 0)), kind=data.get("kind", ""),
                      content_terms=tuple(data.get("content_terms") or ()))


def _chunks(items: Sequence[Any], size: int = RECORD_WRITE_BATCH) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


async def _fence(ref: Any, transaction: Any, owner: Optional[str], user_id: str, *,
                 missing_ok: bool = False) -> bool:
    """Read the stored lease holder in the transaction; raise LeaseLost unless it is `owner` (None for a missing
    document, or any owner with `missing_ok`). Returns whether the document exists."""
    snap = await ref.get(transaction=transaction)
    if not snap.exists and missing_ok:
        return False
    held = (snap.to_dict() or {}).get("lease_owner") if snap.exists else None
    if held != owner:
        raise LeaseLost(f"sync state of {user_id[:8]} is held by {held!r}, not {owner!r}")
    return bool(snap.exists)


# -- the adapter ----------------------------------------------------------------------------------

class FirestoreDriveIndexRepository(DriveIndexRepository):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._records = db_client.collection(env_config.drive_index_records_collection)
        self._states = db_client.collection(env_config.drive_sync_state_collection)
        self._chunks_name = env_config.drive_index_chunks_collection   # resolved per call, like a reference
        logger.info(f"📂 DriveIndex repo initialized (records={env_config.drive_index_records_collection}, "
                    f"chunks={self._chunks_name}, state={env_config.drive_sync_state_collection})")

    # -- sync state --------------------------------------------------------------------------

    async def get_sync_state(self, user_id: str) -> Optional[DriveSyncState]:
        snap = await self._states.document(user_id).get()
        return state_from_doc(user_id, snap.to_dict()) if snap.exists else None

    async def save_sync_state(self, state: DriveSyncState, owner: Optional[str] = None) -> None:
        doc = state_to_doc(state)
        data = {name: doc[name] for name in STATE_FIELDS}
        ref = self._states.document(state.user_id)

        @firestore.async_transactional
        async def _save(transaction: Any) -> None:
            await _fence(ref, transaction, owner, state.user_id)
            transaction.set(ref, data, merge=list(STATE_FIELDS))

        await _save(self._db.transaction())

    async def create_sync_state_if_absent(self, state: DriveSyncState) -> bool:
        try:
            await self._states.document(state.user_id).create(state_to_doc(state))
        except AlreadyExists:
            return False
        return True

    async def update_sync_flags(self, user_id: str, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None) -> None:
        changes: Dict[str, bool] = {}
        if stale_alerted is not None:
            changes["stale_alerted"] = stale_alerted
        if reconnect_notified is not None:
            changes["reconnect_notified"] = reconnect_notified
        if not changes:
            return
        try:
            await self._states.document(user_id).update(changes)
        except NotFound:
            logger.debug(f"[DriveIndex] no sync state for {user_id[:8]}; flags {sorted(changes)} not written")

    async def delete_sync_state(self, user_id: str, owner: Optional[str] = None) -> None:
        ref = self._states.document(user_id)

        @firestore.async_transactional
        async def _delete(transaction: Any) -> None:
            if await _fence(ref, transaction, owner, user_id, missing_ok=True):
                transaction.delete(ref)

        await _delete(self._db.transaction())

    async def acquire_lease(self, user_id: str, owner: str, now: datetime, until: datetime, *,
                            create: bool) -> Optional[DriveSyncState]:
        ref = self._states.document(user_id)

        @firestore.async_transactional
        async def _take(transaction: Any) -> Optional[DriveSyncState]:
            snap = await ref.get(transaction=transaction)
            if not snap.exists and not create:
                return None
            state = state_from_doc(user_id, snap.to_dict()) if snap.exists else DriveSyncState(user_id=user_id)
            if state.lease_owner and state.lease_until and state.lease_until > now and state.lease_owner != owner:
                return None
            taken = replace(state, lease_owner=owner, lease_until=until)
            if snap.exists:
                transaction.update(ref, {"lease_owner": owner, "lease_until": until})
            else:
                transaction.set(ref, {**state_to_doc(taken), "lease_owner": owner, "lease_until": until})
            return taken

        return await _take(self._db.transaction())

    async def release_lease(self, user_id: str, owner: str) -> None:
        ref = self._states.document(user_id)

        @firestore.async_transactional
        async def _release(transaction: Any) -> None:
            snap = await ref.get(transaction=transaction)
            if snap.exists and (snap.to_dict() or {}).get("lease_owner") == owner:
                transaction.update(ref, {"lease_owner": None, "lease_until": None})

        await _release(self._db.transaction())

    async def list_sync_user_ids(self) -> List[str]:
        ids = [snap.id async for snap in self._states.select([FieldPath.document_id()]).stream()]
        return sorted(ids)

    # -- records -----------------------------------------------------------------------------

    def _ref(self, user_id: str, item_id: str) -> Any:
        return self._records.document(record_key(user_id, item_id))

    def _user_query(self, user_id: str) -> Any:
        return self._records.where(filter=FieldFilter("user_id", "==", user_id))

    async def _read(self, query: Any) -> List[DriveIndexRecord]:
        return [record_from_doc(snap.to_dict()) for snap in await query.select(list(RECORD_FIELDS)).get()]

    async def get_records(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        found: Dict[str, DriveIndexRecord] = {}
        unique = list(dict.fromkeys(item_ids))   # a feed page repeats items (S0a)
        for chunk in _chunks(unique):
            async for snap in self._db.get_all([self._ref(user_id, i) for i in chunk], field_paths=list(RECORD_FIELDS)):
                if snap.exists:
                    rec = record_from_doc(snap.to_dict())
                    found[rec.item_id] = rec
        return found

    async def upsert_records(self, records: Sequence[DriveIndexRecord],
                             card_vectors: Optional[Mapping[str, Sequence[float]]] = None) -> None:
        stuck = [rec.item_id for rec in records if rec.due_at is not None and rec.priority is None]
        if stuck:   # due_records queries per priority class: such a record would never be worked again
            logger.error(f"[DriveIndex] refused write: due without a priority: {stuck[:5]}")
            raise ValueError(f"records due without a priority: {stuck[:5]}")
        for chunk in _chunks(records):
            batch = self._db.batch()
            for rec in chunk:
                data = record_to_doc(rec)
                if card_vectors and rec.item_id in card_vectors:
                    data[CARD_VECTOR] = Vector([float(x) for x in card_vectors[rec.item_id]])
                    data[HAS_CARD_VECTOR] = True
                elif not rec.has_card:
                    data[CARD_VECTOR] = firestore.DELETE_FIELD
                    data[HAS_CARD_VECTOR] = False
                batch.set(self._ref(rec.user_id, rec.item_id), data, merge=list(data))
            await batch.commit()

    async def delete_records(self, user_id: str, item_ids: Sequence[str]) -> None:
        for chunk in _chunks(list(dict.fromkeys(item_ids))):
            batch = self._db.batch()
            for item_id in chunk:
                batch.delete(self._ref(user_id, item_id))
            await batch.commit()

    async def due_records(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """One equality query per class keeps due_at the first (and only range) ordering (§7.8)."""
        due: List[DriveIndexRecord] = []
        for priority in Priority:
            if len(due) >= limit:
                break
            query = (self._user_query(user_id)
                     .where(filter=FieldFilter("priority", "==", int(priority)))
                     .where(filter=FieldFilter("due_at", "<=", now))
                     .order_by("due_at").order_by("item_id").limit(limit - len(due)))
            due.extend(await self._read(query))
        return due

    async def oldest_due_at(self, user_id: str, priority: int) -> Optional[datetime]:
        query = (self._user_query(user_id)
                 .where(filter=FieldFilter("priority", "==", int(priority)))
                 .where(filter=FieldFilter("due_at", ">=", _TS_MIN))
                 .order_by("due_at").order_by("item_id").limit(1))
        recs = await self._read(query)
        return recs[0].due_at if recs else None

    async def has_record_in(self, user_id: str, state: RecordState, priority: int) -> bool:
        query = (self._user_query(user_id).where(filter=FieldFilter("state", "==", state.value))
                 .where(filter=FieldFilter("priority", "==", int(priority)))
                 .select([FieldPath.document_id()]).limit(1))
        return bool(await query.get())

    async def children(self, user_id: str, parent_id: str, after_item_id: Optional[str],
                       limit: int) -> List[DriveIndexRecord]:
        query = self._user_query(user_id).where(filter=FieldFilter("parent_id", "==", parent_id))
        if after_item_id is not None:
            query = query.where(filter=FieldFilter("item_id", ">", after_item_id))
        return await self._read(query.order_by("item_id").limit(limit))

    async def records_page(self, user_id: str, after_item_id: Optional[str], limit: int) -> List[DriveIndexRecord]:
        query = self._user_query(user_id)
        if after_item_id is not None:
            query = query.where(filter=FieldFilter("item_id", ">", after_item_id))
        return await self._read(query.order_by("item_id").limit(limit))

    async def records_seen_before(self, user_id: str, epoch: int, limit: int) -> List[DriveIndexRecord]:
        query = (self._user_query(user_id).where(filter=FieldFilter("seen_epoch", "<", epoch))
                 .order_by("seen_epoch").order_by("item_id").limit(limit))
        return await self._read(query)

    def _carded(self, user_id: str) -> Any:
        return self._user_query(user_id).where(filter=FieldFilter(HAS_CARD_VECTOR, "==", True))

    @staticmethod
    async def _count(query: Any) -> int:
        result = await query.count(alias="n").get()
        return int(result[0][0].value)

    async def count_carded(self, user_id: str) -> int:
        return await self._count(self._carded(user_id))

    async def lowest_ranked_carded(self, user_id: str) -> Optional[DriveIndexRecord]:
        """Highest card_rank, then oldest modified_at with a missing date last, then item_id (§5.4).

        No `state != deleting` filter: a deleting record carries no card (the planner drops it on entering the state),
        so its `has_card_vector` is False and `_carded` never returns it."""
        top = await self._read(self._carded(user_id).order_by("card_rank", direction=firestore.Query.DESCENDING)
                               .limit(1))
        if not top:
            return None
        dated_query, undated_query = self.eviction_rank_queries(user_id, top[0].card_rank)
        dated = await self._read(dated_query)
        if dated:
            return dated[0]
        undated = await self._read(undated_query)
        return undated[0] if undated else None

    def eviction_rank_queries(self, user_id: str, card_rank: int) -> Tuple[Any, Any]:
        """The two within-rank queries of `lowest_ranked_carded` (oldest dated, then undated). Not a port method:
        scripts/infrastructure/drive_index_smoke.py runs them directly, since an empty index never reaches them."""
        same_rank = self._carded(user_id).where(filter=FieldFilter("card_rank", "==", card_rank))
        dated = (same_rank.where(filter=FieldFilter("modified_at", ">=", _TS_MIN))
                 .order_by("modified_at").order_by("item_id").limit(1))
        undated = same_rank.where(filter=FieldFilter("modified_at", "==", None)).order_by("item_id").limit(1)
        return dated, undated

    async def card_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        """Best card_rank first, then newest modified_at; a missing date sorts last (null is lowest) (§5.4)."""
        query = (self._user_query(user_id).where(filter=FieldFilter("card_capped", "==", True))
                 .order_by("card_rank").order_by("modified_at", direction=firestore.Query.DESCENDING).limit(limit))
        return await self._read(query)

    async def chunk_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        """Best card_rank first, then newest modified_at (a missing date last: null is lowest), then item_id (§5.4)."""
        query = (self._user_query(user_id)
                 .where(filter=FieldFilter("state", "==", RecordState.METADATA_ONLY.value))
                 .where(filter=FieldFilter("reason", "==", Reason.USER_CHUNK_CAP.value))
                 .order_by("card_rank").order_by("modified_at", direction=firestore.Query.DESCENDING)
                 .order_by("item_id").limit(limit))
        return await self._read(query)

    def unread_count_queries(self, user_id: str, kinds: AbstractSet[str],
                             triaged: AbstractSet[str] = frozenset()) -> List[Any]:
        """The count queries of `count_unread`: one per unread class — failed, pending without a readable generation,
        and one per metadata_only reason — each with one `kind in [...]` filter of at most IN_VALUES_MAX kinds, so a
        query never exceeds Firestore's disjunction limit; the triaged kinds (not also in `kinds`) get the same
        classes with `triage_verdict == text` (§24.5, review D-M4). Folders are excluded by kind ("folder" is no
        content kind). Not a port method: the smoke script runs them directly."""
        documents = sorted(set(kinds))
        images = sorted(set(triaged) - set(kinds))
        verdict = (("triage_verdict", "==", TriageVerdict.TEXT.value),)
        classes = ([(("state", "==", RecordState.FAILED.value),),
                    (("state", "==", RecordState.PENDING.value), ("indexed_generation", "==", None))]
                   + [(("state", "==", RecordState.METADATA_ONLY.value), ("reason", "==", reason.value))
                      for reason in UNREAD_REASONS])
        queries = []
        for wanted, extra in ((documents, ()), (images, verdict)):
            for part in _chunks(wanted, IN_VALUES_MAX):
                for filters in classes:
                    query = self._user_query(user_id)
                    for field_path, op, value in (*filters, *extra):
                        query = query.where(filter=FieldFilter(field_path, op, value))
                    queries.append(query.where(filter=FieldFilter("kind", "in", list(part))))
        return queries

    async def count_unread(self, user_id: str, kinds: AbstractSet[str],
                           triaged: AbstractSet[str] = frozenset()) -> int:
        """Count aggregations over the unread classes (six per 30 kinds, for the documents and for the text images),
        summed; never a scan. The classes are disjoint (by state, then by reason; a kind in one list only), so the
        sum counts each record once."""
        queries = self.unread_count_queries(user_id, kinds, triaged)
        if not queries:
            return 0
        return sum(await asyncio.gather(*(self._count(q) for q in queries)))

    async def count_by_state(self, user_id: str) -> Dict[str, int]:
        states = list(RecordState)
        counts = await asyncio.gather(
            *(self._count(self._user_query(user_id).where(filter=FieldFilter("state", "==", s.value)))
              for s in states),
            self.count_carded(user_id))
        result = {s.value: n for s, n in zip(states, counts[:-1]) if n}
        result["carded"] = counts[-1]
        return result

    async def delete_user_records(self, user_id: str, limit: int) -> int:
        snaps = await self._user_query(user_id).select([FieldPath.document_id()]).limit(limit).get()
        for chunk in _chunks(list(snaps)):
            batch = self._db.batch()
            for snap in chunk:
                batch.delete(snap.reference)
            await batch.commit()
        return len(snaps)

    # -- search ------------------------------------------------------------------------------

    async def find_nearest_cards(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveIndexRecord, float]]:
        query = self._user_query(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        # the distance comes back under a projection only when its name is selected too (verified live 2026-10-12)
        projected = query.select([*RECORD_FIELDS, CARD_DISTANCE])
        nearest = projected.find_nearest(vector_field=CARD_VECTOR, query_vector=Vector([float(x) for x in vector]),
                                         limit=limit, distance_measure=DistanceMeasure.COSINE,
                                         distance_result_field=CARD_DISTANCE)
        hits: List[Tuple[DriveIndexRecord, float]] = []
        for snap in await nearest.get():
            data = snap.to_dict()
            hits.append((record_from_doc(data), float(data[CARD_DISTANCE])))
        return hits

    async def find_by_name_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                kind: Optional[str] = None) -> List[DriveIndexRecord]:
        """One `array_contains_any` query. The search service sends one whole-token or trigram key per call (at
        most NAME_KEY_QUERIES of each) plus one call with the remaining keys (RFC Δ17), so a common key cannot
        crowd out a rare one.
        Firestore takes at most NAME_QUERY_KEYS_MAX values: the distinct keys are cut there, in order."""
        wanted = list(dict.fromkeys(keys))[:NAME_QUERY_KEYS_MAX]
        if not wanted:
            return []
        query = self._user_query(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = query.where(filter=FieldFilter("name_tokens", "array_contains_any", wanted))
        return await self._read(query.order_by("item_id").limit(limit))

    async def filter_records(self, user_id: str, kind: Optional[str], date_from: Optional[datetime],
                             date_to: Optional[datetime], limit: int) -> List[DriveIndexRecord]:
        query = self._user_query(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = query.where(filter=FieldFilter("effective_date", ">=", date_from or _TS_MIN))   # no dateless record
        if date_to is not None:
            query = query.where(filter=FieldFilter("effective_date", "<=", date_to))
        return await self._read(query.order_by("effective_date", direction=firestore.Query.DESCENDING).limit(limit))

    async def card_vectors(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, List[float]]:
        vectors: Dict[str, List[float]] = {}
        for chunk in _chunks(list(dict.fromkeys(item_ids))):
            refs = [self._ref(user_id, i) for i in chunk]
            async for snap in self._db.get_all(refs, field_paths=["item_id", CARD_VECTOR]):
                data = snap.to_dict() if snap.exists else None
                if data and data.get(CARD_VECTOR) is not None:
                    vectors[data["item_id"]] = [float(x) for x in data[CARD_VECTOR]]
        return vectors

    # -- chunks (§3.3, §7.4, §24.6) -------------------------------------------------------------

    @property
    def _chunk_col(self) -> Any:
        return self._db.collection(self._chunks_name)

    def _chunk_ref(self, user_id: str, item_id: str, generation: int, index: int) -> Any:
        return self._chunk_col.document(chunk_key(user_id, item_id, generation, index))

    def _user_chunks(self, user_id: str) -> Any:
        return self._chunk_col.where(filter=FieldFilter("user_id", "==", user_id))

    async def write_chunks(self, chunks: Sequence[DriveChunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            logger.error(f"[DriveIndex] refused chunk write: {len(chunks)} chunks but {len(vectors)} vectors")
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        pairs = list(zip(chunks, vectors))
        for part in _chunks(pairs):
            batch = self._db.batch()
            for chunk, vector in part:
                data = chunk_to_doc(chunk)
                data[CHUNK_VECTOR] = Vector([float(x) for x in vector])
                batch.set(self._chunk_ref(chunk.user_id, chunk.item_id, chunk.generation, chunk.index), data)
            await batch.commit()

    async def delete_chunks_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        """Equality filters only (single-field indexes); the generation is compared here. An item holds at most a
        few generations of MAX_CHUNKS_PER_FILE chunks: collection runs after every commit (§7.4)."""
        query = self._user_chunks(user_id).where(filter=FieldFilter("item_id", "==", item_id)).select(["generation"])
        doomed = [snap.reference for snap in await query.get()
                  if (snap.to_dict() or {}).get("generation") != keep_generation]
        await self._delete_refs(doomed)
        return len(doomed)

    def nearest_chunks_query(self, user_id: str, vector: Sequence[float], limit: int,
                             kind: Optional[str] = None) -> Any:
        """The vector query `find_nearest_chunks` runs. Not a port method: the S3 cost script
        (scripts/infrastructure/drive_chunk_knn_cost.py) runs it with explain options to read its billed reads."""
        query = self._user_chunks(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        # as find_nearest_cards: under a projection the distance comes back only when its name is selected too
        projected = query.select([*STREAM_FIELDS, CHUNK_DISTANCE])
        return projected.find_nearest(vector_field=CHUNK_VECTOR, query_vector=Vector([float(x) for x in vector]),
                                      limit=limit, distance_measure=DistanceMeasure.COSINE,
                                      distance_result_field=CHUNK_DISTANCE)

    async def find_nearest_chunks(self, user_id: str, vector: Sequence[float], limit: int,
                                  kind: Optional[str] = None) -> List[Tuple[DriveChunk, float]]:
        hits: List[Tuple[DriveChunk, float]] = []
        for snap in await self.nearest_chunks_query(user_id, vector, limit, kind).get():
            data = snap.to_dict()
            hits.append((chunk_from_doc(data), float(data[CHUNK_DISTANCE])))
        return hits

    async def find_chunks_by_term(self, user_id: str, term: str, limit: int,
                                  kind: Optional[str] = None) -> List[DriveChunk]:
        """One `array_contains` per term, never `array_contains_any`: a shared query returns its first matches by
        id, so a common term would crowd out a rare one (§24.6, RFC Δ17)."""
        query = self._user_chunks(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = (query.where(filter=FieldFilter("content_terms", "array_contains", term))
                 .order_by("item_id").order_by("generation").order_by("index").limit(limit))
        return [chunk_from_doc(snap.to_dict()) for snap in await query.select(list(STREAM_FIELDS)).get()]

    async def _get_chunk_docs(self, user_id: str, item_id: str, generation: int, indexes: Sequence[int],
                              field_paths: List[str]) -> List[Mapping[str, Any]]:
        wanted = sorted(set(indexes))
        refs = [self._chunk_ref(user_id, item_id, generation, i) for i in wanted]
        found: Dict[int, Mapping[str, Any]] = {}
        for part in _chunks(refs):
            async for snap in self._db.get_all(part, field_paths=field_paths):
                data = snap.to_dict() if snap.exists else None
                if data:
                    found[int(data["index"])] = data
        return [found[i] for i in wanted if i in found]

    async def get_chunks(self, user_id: str, item_id: str, generation: int,
                         indexes: Sequence[int]) -> List[DriveChunk]:
        docs = await self._get_chunk_docs(user_id, item_id, generation, indexes, list(CHUNK_FIELDS))
        return [chunk_from_doc(d) for d in docs]

    async def get_chunks_with_vectors(self, user_id: str, item_id: str, generation: int,
                                      indexes: Sequence[int]) -> List[Tuple[DriveChunk, List[float]]]:
        docs = await self._get_chunk_docs(user_id, item_id, generation, indexes, [*CHUNK_FIELDS, CHUNK_VECTOR])
        return [(chunk_from_doc(d), [float(x) for x in d[CHUNK_VECTOR]]) for d in docs if d.get(CHUNK_VECTOR)]

    async def readable_chunk_total(self, user_id: str) -> int:
        """Σ chunk_count over records with a readable generation: one sum aggregation. An aggregation reads index
        entries only, so its composite index carries chunk_count (§12.5)."""
        query = self._user_query(user_id).where(filter=FieldFilter("indexed_generation", ">=", 0))
        result = await query.sum("chunk_count", alias="n").get()
        return int(result[0][0].value or 0)

    async def chunks_page(self, user_id: str, after: Optional[Tuple[str, int, int]],
                          limit: int) -> List[DriveChunk]:
        query = (self._user_chunks(user_id).order_by("item_id").order_by("generation").order_by("index"))
        if after is not None:
            query = query.start_after({"item_id": after[0], "generation": after[1], "index": after[2]})
        return [chunk_from_doc(snap.to_dict()) for snap in await query.select(list(CHUNK_FIELDS)).limit(limit).get()]

    async def delete_user_chunks(self, user_id: str, limit: int) -> int:
        snaps = await self._user_chunks(user_id).select([FieldPath.document_id()]).limit(limit).get()
        await self._delete_refs([snap.reference for snap in snaps])
        return len(snaps)

    async def _delete_refs(self, refs: Sequence[Any]) -> None:
        for part in _chunks(refs):
            batch = self._db.batch()
            for ref in part:
                batch.delete(ref)
            await batch.commit()
