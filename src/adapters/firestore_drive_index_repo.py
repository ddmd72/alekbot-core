"""
FirestoreDriveIndexRepository — DriveIndexRepository on Firestore (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5).

Collections (env-prefixed through EnvironmentConfig):
    {prefix}drive_index_records   doc id record_key(user_id, item_id); every DriveIndexRecord field under its own
                                  name, plus `card_vector` (Vector, 768) and `has_card_vector` (bool, mirrors it).
                                  `inaccessible_notified_since` is the fan-out's: RECORD_FIELDS (the tick's writes and
                                  reads) leave it out, `set_inaccessible_notified` writes it with a field update and
                                  `inaccessible_records` reads it (§26.2, Δ54)
    {prefix}drive_index_chunks    doc id chunk_key(user_id, item_id, generation, index); every DriveChunk field
                                  under its own name, plus `vector` (Vector, 768) (§3.3, §24.6)
    {prefix}drive_sync_state      doc id user_id; every DriveSyncState field under its own name
    {prefix}drive_index_hints     doc id chunk_key(...) of the chunk; every DriveHintDoc field (items as maps), plus
                                  `hint_vector` (Vector, 768) (Search v2, §25.2.2)
    {prefix}drive_model_usage     `{user_id}:{YYYY-MM}` (per-class dollars, fallback, calls, daily map, `expires_at` for
                                  the TTL) and `{user_id}:initial` (the initial build); no purge deletes them (§25.4.5)

Search v2 (§25.13.1): stream reads project the lean fields only (RECORD_LEAN_FIELDS, CHUNK_LEAN_FIELDS,
HINT_LEAN_FIELDS); a key list is cut at 30 values and a kind is one `==` (a family is the caller's post-filter); every
document write goes in batches of at most RECORD_WRITE_BATCH documents and WRITE_BATCH_BYTES estimated bytes.

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
The two search streams (`find_nearest_chunks`, `find_chunks_by_term`) leave out `text`, `content_terms` and the v2
chunk keys (`word_keys`, `keys_version`) as well: a widened page is up to FETCH_MAX chunks, and windows are read by id
with `get_chunks`.
Chunk reads are never fenced: the search service compares each chunk's generation with its record (§7.4).
Errors: Firestore's transient errors (DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted,
ResourceExhausted) and FailedPrecondition (a composite index missing or still building, logged ERROR) leave every
public method as DriveIndexStoreUnavailable, which the tick treats as systemic (§8.2); any other error keeps its type.
Datetimes come back as plain tz-aware UTC datetimes, never DatetimeWithNanoseconds (see
firestore_oauth_credentials_adapter.py: `.replace()` on one breaks write-back).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from typing import AbstractSet, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from google.api_core.exceptions import (
    Aborted, AlreadyExists, DeadlineExceeded, FailedPrecondition, InternalServerError, NotFound, ResourceExhausted,
    ServiceUnavailable,
)
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.field_path import FieldPath
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.drive_enrichment import (
    DriveHintDoc, DriveProfile, EnrichJob, GradedValue, HintItem, InitialBuild, JobState, KeyValue, ModelUsageMonth,
    PhotoObject, ProfileDate, ProfileEntity, UsageClass,
)
from ..domain.drive_index import (
    ContentClass, DocumentBrief, DriveChunk, DriveIndexRecord, DriveIndexStoreUnavailable, DriveSyncState, EpochPhase,
    ErrorCategory, LeaseLost, Priority, Reason, RecordState, Scope, TriageVerdict, record_key,
)
from ..domain.drive_index_lean import (
    RECORD_LEAN_FIELDS, ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean, lean_of,
)
from ..domain.drive_index_params import (
    CONCEPT_KEYS_MAX, NAME_QUERY_KEYS_MAX, RECORD_WRITE_BATCH, WRITE_BATCH_BYTES,
)
from ..domain.drive_usage import (
    closed_initial, initial_doc_id, new_initial_build, spent_initial, usage_doc_id, usage_expires_at,
)
from ..ports.drive_index_repository import DriveIndexRepository
from ..utils.logger import logger

CARD_VECTOR = "card_vector"
HAS_CARD_VECTOR = "has_card_vector"
CARD_DISTANCE = "card_distance"
CHUNK_VECTOR = "vector"
CHUNK_DISTANCE = "chunk_distance"
_TS_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)   # Firestore's smallest timestamp: `>=` it excludes nulls
_FLAGS = ("stale_alerted", "reconnect_notified", "migration_alerted", "stall_alerted", "fallback_alerted_day")
_LEASE = ("lease_owner", "lease_until")
IN_VALUES_MAX = 30                                  # Firestore `in` limit (and the disjunction limit per query)
# metadata_only reasons that leave an admitted document unread (drive_search.is_unread, §10.2)
UNREAD_REASONS: Tuple[Reason, ...] = (Reason.TOO_LARGE, Reason.NO_TEXT, Reason.MACHINE_TEXT, Reason.USER_CHUNK_CAP)
# The fan-out's own record field (§26.2, Δ54): the tick merges whole records, so a field in its writes would be cleared by
# any stale snapshot and the notice repeated. Like the sync state's fan-out flags, it is outside RECORD_FIELDS.
RECORD_FANOUT_FIELDS: Tuple[str, ...] = ("inaccessible_notified_since",)
RECORD_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveIndexRecord) if f.name not in RECORD_FANOUT_FIELDS)
_ALL_RECORD_FIELDS: Tuple[str, ...] = RECORD_FIELDS + RECORD_FANOUT_FIELDS
# What the tick and the fan-out read of a marked record (§26.2, reviews of S1): the tick's counters
# (drive_access.access_counts, which reads drive_search.is_not_read_yet) and parent map; the fan-out's notice
# (drive_access.notice_batch, and notice_paths, whose resolve_chain needs scope and state to see the record)
ACCESS_MARK_FIELDS: Tuple[str, ...] = (
    "user_id", "item_id", "parent_id", "name", "is_folder", "kind", "scope", "state", "reason", "indexed_generation",
    "card_capped", "triage_verdict", "inaccessible_since", "inaccessible_notified_since")
CHUNK_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveChunk))
# search streams (port): no text (windows read it by id) and no content_terms (up to 256 per chunk; the term stream
# knows its term from its own query, review D-M3)
# Search v2 chunk keys (§25.2.2) are not read by the v1 streams either (§25.13.3: no keys): up to 900 per chunk
CHUNK_KEY_FIELDS: Tuple[str, ...] = ("word_keys", "keys_version")
STREAM_FIELDS: Tuple[str, ...] = tuple(name for name in CHUNK_FIELDS
                                       if name not in ("text", "content_terms") + CHUNK_KEY_FIELDS)
_DOC_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveSyncState) if f.name not in _FLAGS)
# Search v2 lean projections (§25.13.3): streams read no text, no keys, no vector, no hint items
CHUNK_LEAN_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveChunkLean))
HINT_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveHintDoc))
HINT_LEAN_FIELDS: Tuple[str, ...] = tuple(f.name for f in fields(DriveHintLean))
HINT_VECTOR = "hint_vector"
HINT_DISTANCE = "hint_distance"
USAGE_EXPIRES_AT = "expires_at"                     # the TTL field of a month's usage document (§25.4.5)
USAGE_USD_FIELDS: Tuple[str, ...] = tuple(f"{c.value}_usd" for c in UsageClass)
USAGE_INITIAL_FIELDS: Tuple[str, ...] = tuple(f"{c.value}_initial_usd" for c in UsageClass)
STATE_FIELDS: Tuple[str, ...] = tuple(name for name in _DOC_FIELDS if name not in _LEASE)   # save_sync_state's


# -- serialization ------------------------------------------------------------------------------

def _plain(value: Any) -> Any:
    """Firestore-encodable copy: dataclasses (a brief, a profile, photo objects) as maps, enums as values, mappings as
    dicts, tuples as lists (recursively), a `date` (a profile date, Firestore has no date type) as "YYYY-MM-DD"."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
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
                         amount=value.get("amount", ""), date_grade=value.get("date_grade", ""))


def _strs(value: Any) -> Tuple[str, ...]:
    return tuple(str(v) for v in (value or ()))


def _photo_objects(value: Any) -> Tuple[PhotoObject, ...]:
    return tuple(PhotoObject(name=v.get("name", ""), forms=_strs(v.get("forms"))) for v in (value or ()))


def _profile(value: Any) -> Optional[DriveProfile]:
    """A stored profile (§25.2.1); missing lists read as empty, so a later field added to the profile reads back."""
    if not value:
        return None
    return DriveProfile(
        title=value.get("title", ""), what_it_is=value.get("what_it_is", ""), language=value.get("language", ""),
        doc_type=value.get("doc_type", ""), life_domains=_strs(value.get("life_domains")),
        dates=tuple(ProfileDate(label=d.get("label", ""), iso=date.fromisoformat(d["iso"]),
                                  printed=d.get("printed", ""))
                    for d in value.get("dates") or ()),
        entities=tuple(ProfileEntity(name=e.get("name", ""), type=e.get("type", ""), printed=e.get("printed", ""),
                                     forms=_strs(e.get("forms")), found=_strs(e.get("found")))
                       for e in value.get("entities") or ()),
        key_values=tuple(KeyValue(label=k.get("label", ""), printed=k.get("printed", ""),
                                  canonical=k.get("canonical", "")) for k in value.get("key_values") or ()),
        topics=_strs(value.get("topics")), topics_native=_strs(value.get("topics_native")),
        topic_words=_strs(value.get("topic_words")), label_words=_strs(value.get("label_words")),
        domain_words=_strs(value.get("domain_words")), validity_words=_strs(value.get("validity_words")),
        graded=tuple(GradedValue(field=g.get("field", ""), value=g.get("value", ""), grade=g.get("grade", ""),
                                 seen_as=g.get("seen_as", "")) for g in value.get("graded") or ()))


def _dt_map(value: Any) -> Dict[str, datetime]:
    return {str(k): v for k, v in ((k, _dt(v)) for k, v in (value or {}).items()) if v is not None}


_RECORD_READERS: Dict[str, Callable[[Any], Any]] = {
    "facets": lambda v: dict(v or {}),
    "content_class": ContentClass,
    "scope": Scope,
    "state": RecordState,
    "reason": _optional(Reason),
    "modified_at": _dt, "fs_modified_at": _dt, "effective_date": _dt,
    "dirty_since": _dt, "due_at": _dt, "attempt_started_at": _dt, "defer_until": _dt, "model_read_at": _dt,
    "name_tokens": lambda v: tuple(v or ()),
    "priority": _optional(int), "defer_priority": _optional(int),
    "brief": _brief,
    "triage_verdict": _optional(TriageVerdict),
    # Search v2 (§25.2.1)
    "profile": _profile, "photo_objects": _photo_objects,
    "valid_until": _dt, "profile_defer_until": _dt, "passages_defer_until": _dt, "passages_done_at": _dt,
    "enrich_wake_at": _dt,
    "about_keys": _strs, "place_forms": _strs, "photo_scene_labels": _strs,
    "passages_generation": _optional(int),
    # §26.2 (D33)
    "inaccessible_since": _dt, "inaccessible_checked_at": _dt, "inaccessible_notified_since": _dt,
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
    "model_backoff_by_agent": _dt_map,
    "described_count": int, "described_grew_at": _dt,
    "migration_alerted": bool, "stall_alerted": bool, "fallback_alerted_day": str,
}


def record_to_doc(rec: DriveIndexRecord) -> Dict[str, Any]:
    """Every record field the tick writes, under its own name; the vector is not part of the record, and neither is the
    fan-out's `inaccessible_notified_since` (RECORD_FANOUT_FIELDS, Δ54)."""
    return {name: _plain(getattr(rec, name)) for name in RECORD_FIELDS}


def record_from_doc(data: Mapping[str, Any]) -> DriveIndexRecord:
    """Missing fields take the dataclass default (schema growth); vector and extra fields are ignored."""
    kwargs = {name: _RECORD_READERS.get(name, lambda v: v)(data[name]) for name in _ALL_RECORD_FIELDS if name in data}
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
                      content_terms=tuple(data.get("content_terms") or ()),
                      word_keys=tuple(data.get("word_keys") or ()), keys_version=int(data.get("keys_version", 0)))


def chunk_lean_from_doc(data: Mapping[str, Any]) -> DriveChunkLean:
    return DriveChunkLean(user_id=data["user_id"], item_id=data["item_id"], generation=int(data["generation"]),
                          index=int(data["index"]), kind=data.get("kind", ""))


def lean_record_from_doc(data: Mapping[str, Any]) -> DriveRecordLean:
    """A record read with RECORD_LEAN_FIELDS (or whole): the same readers as `record_from_doc` (the projection has no
    `user_id`; the lean record has none either)."""
    return lean_of(record_from_doc({"user_id": "", **data}))


def hint_to_doc(doc: DriveHintDoc) -> Dict[str, Any]:
    """Every hint-document field under its own name (`items` as maps); the vector is not part of the document."""
    return {name: _plain(getattr(doc, name)) for name in HINT_FIELDS}


def _hint_items(value: Any) -> Tuple[HintItem, ...]:
    return tuple(HintItem(text=v.get("text", ""), type=v.get("type", ""), evidence=v.get("evidence", ""),
                          evidence_start=int(v.get("evidence_start", -1)), evidence_end=int(v.get("evidence_end", -1)))
                 for v in (value or ()))


def hint_from_doc(data: Mapping[str, Any]) -> DriveHintDoc:
    """Vector, distance and extra fields are ignored."""
    return DriveHintDoc(user_id=data["user_id"], item_id=data["item_id"], generation=int(data["generation"]),
                        index=int(data["index"]), kind=data.get("kind", ""),
                        chunk_text_hash=data.get("chunk_text_hash", ""), items=_hint_items(data.get("items")),
                        hint_keys=_strs(data.get("hint_keys")), passages_version=int(data.get("passages_version", 0)))


def hint_lean_from_doc(data: Mapping[str, Any]) -> DriveHintLean:
    return DriveHintLean(user_id=data["user_id"], item_id=data["item_id"], generation=int(data["generation"]),
                         index=int(data["index"]), kind=data.get("kind", ""), hint_keys=_strs(data.get("hint_keys")))


def usage_from_doc(data: Mapping[str, Any]) -> ModelUsageMonth:
    """A month's usage document; a class or a day never written reads as zero."""
    daily = {str(day): (float((v or {}).get("total_usd", 0.0)), float((v or {}).get("fallback_usd", 0.0)))
             for day, v in (data.get("daily") or {}).items()}
    return ModelUsageMonth(**{name: float(data.get(name, 0.0)) for name in (*USAGE_USD_FIELDS, *USAGE_INITIAL_FIELDS)},
                           fallback_usd=float(data.get("fallback_usd", 0.0)), calls=int(data.get("calls", 0)),
                           daily=daily)


def initial_to_doc(user_id: str, build: InitialBuild) -> Dict[str, Any]:
    return {"user_id": user_id, "allowance_usd": build.allowance_usd, "spent_usd": build.spent_usd,
            "opened_at": build.opened_at, "closed_at": build.closed_at}


def initial_from_doc(data: Mapping[str, Any]) -> InitialBuild:
    allowance = data.get("allowance_usd")
    return InitialBuild(allowance_usd=new_initial_build().allowance_usd if allowance is None else float(allowance),
                        spent_usd=float(data.get("spent_usd", 0.0)), opened_at=_dt(data.get("opened_at")),
                        closed_at=_dt(data.get("closed_at")))


def _query_keys(keys: Sequence[str], method: str) -> List[str]:
    """The distinct keys of one `array_contains_any`, in order. More than CONCEPT_KEYS_MAX (Firestore's limit of 30
    values and 30 disjunctions per query) is refused, logged, never cut: a cut key is a file lost without a trace.
    The caller splits its lists (§25.5.1)."""
    wanted = list(dict.fromkeys(keys))
    if len(wanted) > CONCEPT_KEYS_MAX:
        logger.error(f"[DriveIndex] {method} refused: {len(wanted)} distinct keys, at most {CONCEPT_KEYS_MAX}")
        raise ValueError(f"{method}: {len(wanted)} distinct keys, at most {CONCEPT_KEYS_MAX}")
    return wanted


def _chunks(items: Sequence[Any], size: int = RECORD_WRITE_BATCH) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# -- write batches by bytes (§25.2.6) ---------------------------------------------------------------------------------

_DOC_OVERHEAD_BYTES = 1_024     # the document name, write metadata and the merge mask, generously


def stored_bytes(value: Any) -> int:
    """An upper estimate of a value's size in the write request: protobuf bytes, each value wrapped in its `Value`
    message and each array element or map entry in its own (a string its UTF-8 bytes + 6, a number 12, a timestamp 16,
    a vector element 12). Nothing else counts the bytes of a write, so the estimate leans high
    (tests/unit/adapters/test_firestore_drive_index_v2_wire.py checks it against the SDK's own encoding)."""
    if value is None or isinstance(value, bool):
        return 4
    if isinstance(value, (int, float)):
        return 12
    if isinstance(value, datetime):
        return 16
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 6
    if isinstance(value, Vector):
        return 12 * len(value) + 32
    if isinstance(value, Mapping):
        return sum(len(str(k).encode("utf-8")) + 6 + stored_bytes(v) for k, v in value.items()) + 6
    if isinstance(value, (list, tuple)):
        return sum(stored_bytes(v) for v in value) + 6
    return 64   # a sentinel such as DELETE_FIELD, or a transform


def byte_batches(writes: Sequence[Any], size_of: Callable[[Any], int]) -> List[List[Any]]:
    """Consecutive batches of at most RECORD_WRITE_BATCH writes and WRITE_BATCH_BYTES estimated bytes (Firestore
    refuses a request over 10 MiB; a v2 chunk is about 25-35 KB, so 400 of them would pass it). A single write over
    the byte budget goes alone."""
    batches: List[List[Any]] = []
    current: List[Any] = []
    used = 0
    for write in writes:
        size = size_of(write) + _DOC_OVERHEAD_BYTES
        if current and (len(current) >= RECORD_WRITE_BATCH or used + size > WRITE_BATCH_BYTES):
            batches.append(current)
            current, used = [], 0
        current.append(write)
        used += size
    if current:
        batches.append(current)
    return batches


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
        self._hints_name = env_config.drive_index_hints_collection     # (Search v2, §25.2.2)
        self._usage_name = env_config.drive_model_usage_collection     # (Search v2, §25.4.5)
        # Serialises this process's initial-build spends (review m1): enrichment spends concurrently on one document,
        # and a transaction that loses its retries is an Aborted after the money was spent. The transaction stays for
        # other processes (the indexer runs max-instances 1, concurrency 1).
        self._initial_lock = asyncio.Lock()
        logger.info(f"📂 DriveIndex repo initialized (records={env_config.drive_index_records_collection}, "
                    f"chunks={self._chunks_name}, hints={self._hints_name}, usage={self._usage_name}, "
                    f"state={env_config.drive_sync_state_collection})")

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
                                reconnect_notified: Optional[bool] = None, migration_alerted: Optional[bool] = None,
                                stall_alerted: Optional[bool] = None,
                                fallback_alerted_day: Optional[str] = None) -> None:
        given = {"stale_alerted": stale_alerted, "reconnect_notified": reconnect_notified,
                 "migration_alerted": migration_alerted, "stall_alerted": stall_alerted,
                 "fallback_alerted_day": fallback_alerted_day}
        changes: Dict[str, Any] = {name: value for name, value in given.items() if value is not None}
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
        writes = []
        for rec in records:
            data = record_to_doc(rec)
            if card_vectors and rec.item_id in card_vectors:
                data[CARD_VECTOR] = Vector([float(x) for x in card_vectors[rec.item_id]])
                data[HAS_CARD_VECTOR] = True
            elif not rec.has_card:
                data[CARD_VECTOR] = firestore.DELETE_FIELD
                data[HAS_CARD_VECTOR] = False
            writes.append((self._ref(rec.user_id, rec.item_id), data))
        await self._set_in_batches(writes, merge=True)   # by bytes too: a v2 record carries a profile and keys

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
                             triaged: AbstractSet[str] = frozenset(), pending: Optional[bool] = None) -> List[Any]:
        """The count queries of `count_unread`: one per unread class — failed, pending without a readable generation,
        and one per metadata_only reason — each with one `kind in [...]` filter of at most IN_VALUES_MAX kinds, so a
        query never exceeds Firestore's disjunction limit; the triaged kinds (not also in `kinds`) get the same
        classes with `triage_verdict == text` (§24.5, review D-M4). Folders are excluded by kind ("folder" is no
        content kind). Not a port method: the smoke script runs them directly. `pending=True`: only the not-read-yet
        class (pending without a readable generation, not `card_capped`); `pending=False`: only the could-not-be-read
        classes (final review F5-2)."""
        documents = sorted(set(kinds))
        images = sorted(set(triaged) - set(kinds))
        verdict = (("triage_verdict", "==", TriageVerdict.TEXT.value),)
        waiting = (("state", "==", RecordState.PENDING.value), ("indexed_generation", "==", None))
        unreadable = ([(("state", "==", RecordState.FAILED.value),)]
                      + [(("state", "==", RecordState.METADATA_ONLY.value), ("reason", "==", reason.value))
                         for reason in UNREAD_REASONS])
        if pending is None:
            classes = unreadable[:1] + [waiting] + unreadable[1:]
        elif pending:
            classes = [(*waiting, ("card_capped", "==", False))]   # held by the card cap is not waiting
        else:
            classes = unreadable
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
                           triaged: AbstractSet[str] = frozenset(), *, pending: Optional[bool] = None) -> int:
        """Count aggregations over the unread classes (six per 30 kinds, for the documents and for the text images),
        summed; never a scan. The classes are disjoint (by state, then by reason; a kind in one list only), so the
        sum counts each record once."""
        queries = self.unread_count_queries(user_id, kinds, triaged, pending)
        if not queries:
            return 0
        return sum(await asyncio.gather(*(self._count(q) for q in queries)))

    def inaccessible_query(self, user_id: str, limit: int) -> Any:
        """`inaccessible_records`' query (composite index user_id + inaccessible_since, §26.9 step 3). Not a port
        method: the smoke script runs it."""
        return (self._user_query(user_id).where(filter=FieldFilter("inaccessible_since", ">=", _TS_MIN))
                .order_by("inaccessible_since").limit(limit))

    async def inaccessible_records(self, user_id: str, limit: int, *, lean: bool = False) -> List[DriveIndexRecord]:
        """Ties on `inaccessible_since` are ordered by the document name, `{user_id}:{item_id}`: by item id. `lean`
        projects ACCESS_MARK_FIELDS."""
        fields_read = ACCESS_MARK_FIELDS if lean else _ALL_RECORD_FIELDS
        query = self.inaccessible_query(user_id, limit).select(list(fields_read))
        return [record_from_doc(snap.to_dict()) for snap in await query.get()]

    async def set_inaccessible_notified(self, user_id: str, values: Mapping[str, Optional[datetime]]) -> None:
        """One field `update` per record: never a merge-set, which would re-create a record deleted meanwhile as a stub."""
        for item_id, since in sorted(values.items()):
            try:
                await self._ref(user_id, item_id).update({RECORD_FANOUT_FIELDS[0]: since})
            except NotFound:
                logger.info(f"[DriveIndex] record {item_id} of {user_id[:8]} is gone; its notice flag is not written")

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
        writes = []
        for chunk, vector in zip(chunks, vectors):
            data = chunk_to_doc(chunk)
            data[CHUNK_VECTOR] = Vector([float(x) for x in vector])
            writes.append((self._chunk_ref(chunk.user_id, chunk.item_id, chunk.generation, chunk.index), data))
        await self._set_in_batches(writes)   # by bytes too: a v2 chunk carries up to 900 keys (§25.2.6)

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

    async def find_next_file_with_term(self, user_id: str, term: str, after_item_id: str,
                                       kind: Optional[str] = None) -> Optional[DriveChunk]:
        """The term page's query with a cursor on item_id alone: a cursor shorter than the ordering positions past
        every document sharing its prefix, so past all of the item's chunks (arbiter R2 S-M1). Same composite index
        as `find_chunks_by_term`."""
        query = self._user_chunks(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = (query.where(filter=FieldFilter("content_terms", "array_contains", term))
                 .order_by("item_id").order_by("generation").order_by("index")
                 .start_after({"item_id": after_item_id}).limit(1))
        snaps = await query.select(list(STREAM_FIELDS)).get()
        return chunk_from_doc(snaps[0].to_dict()) if snaps else None

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

    async def get_chunks_with_vectors(self, user_id: str, item_id: str, generation: int, indexes: Sequence[int], *,
                                      text: bool = True, terms: bool = True,
                                      vectors: bool = True) -> List[Tuple[DriveChunk, List[float]]]:
        skipped = {name for name, wanted in (("text", text), ("content_terms", terms), ("word_keys", terms))
                   if not wanted}
        paths = [name for name in CHUNK_FIELDS if name not in skipped] + ([CHUNK_VECTOR] if vectors else [])
        docs = await self._get_chunk_docs(user_id, item_id, generation, indexes, paths)
        if not vectors:
            return [(chunk_from_doc(d), []) for d in docs]
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
        # ids only (review m3): the sweep needs (item_id, generation, index), and a v2 chunk carries up to 900 keys
        snaps = await query.select(list(CHUNK_LEAN_FIELDS)).limit(limit).get()
        return [chunk_from_doc(snap.to_dict()) for snap in snaps]

    async def delete_user_chunks(self, user_id: str, limit: int) -> int:
        snaps = await self._user_chunks(user_id).select([FieldPath.document_id()]).limit(limit).get()
        await self._delete_refs([snap.reference for snap in snaps])
        return len(snaps)

    async def _set_in_batches(self, writes: Sequence[Tuple[Any, Dict[str, Any]]], *,
                              merge: bool = False) -> None:
        """(reference, data) pairs as whole-document sets (or merges of the listed fields), in byte-sized batches."""
        for part in byte_batches(writes, lambda w: stored_bytes(w[1])):
            batch = self._db.batch()
            for ref, data in part:
                if merge:
                    batch.set(ref, data, merge=list(data))
                else:
                    batch.set(ref, data)
            await batch.commit()

    async def _delete_refs(self, refs: Sequence[Any]) -> None:
        for part in _chunks(refs):
            batch = self._db.batch()
            for ref in part:
                batch.delete(ref)
            await batch.commit()

    # -- Search v2: record streams and enrichment reads (§25.13.1) ---------------------------------------------------

    async def _read_lean(self, query: Any) -> List[DriveRecordLean]:
        return [lean_record_from_doc(snap.to_dict()) for snap in await query.select(list(RECORD_LEAN_FIELDS)).get()]

    def about_query(self, user_id: str, keys: Sequence[str], *, kind: Optional[str] = None,
                    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None) -> Any:
        """The query of `find_by_about_keys` before its cursor and limit, None without keys. Not a port method: the
        smoke script runs it with the most disjunctions one query may carry. One `array_contains_any` of at most
        CONCEPT_KEYS_MAX keys and one `kind ==` (a family is post-filtered by the caller, §25.5.5), so a query never
        passes Firestore's 30 disjunctions."""
        wanted = _query_keys(keys, "find_by_about_keys")
        if not wanted:
            return None
        query = self._user_query(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = query.where(filter=FieldFilter("about_keys", "array_contains_any", wanted))
        if date_from is not None:
            query = query.where(filter=FieldFilter("effective_date", ">=", date_from))
        if date_to is not None:
            query = query.where(filter=FieldFilter("effective_date", "<=", date_to))   # a range excludes nulls
        return (query.order_by("effective_date", direction=firestore.Query.DESCENDING)
                .order_by("item_id", direction=firestore.Query.DESCENDING))

    async def find_by_about_keys(self, user_id: str, keys: Sequence[str], limit: int, *, kind: Optional[str] = None,
                                 date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                                 after: Optional[str] = None) -> List[DriveRecordLean]:
        """Without a date filter the ordering alone keeps the undated records, last: every record stores
        `effective_date` (null included) and Firestore orders null below every timestamp. The cursor is the stored
        (effective_date, item_id) of the record `after`, read by id first."""
        query = self.about_query(user_id, keys, kind=kind, date_from=date_from, date_to=date_to)
        if query is None:
            return []
        if after is not None:
            snap = await self._ref(user_id, after).get(field_paths=["effective_date", "item_id"])
            if not snap.exists:
                return []   # the cursor record is gone: the paging ends (port)
            data = snap.to_dict() or {}
            query = query.start_after({"effective_date": data.get("effective_date"), "item_id": after})
        return await self._read_lean(query.limit(limit))

    async def find_expiring(self, user_id: str, keys: Sequence[str], after_date: date,
                            limit: int) -> List[DriveRecordLean]:
        wanted = _query_keys(keys, "find_expiring")
        if not wanted:
            return []
        start = datetime(after_date.year, after_date.month, after_date.day, tzinfo=timezone.utc)
        query = (self._user_query(user_id).where(filter=FieldFilter("about_keys", "array_contains_any", wanted))
                 .where(filter=FieldFilter("valid_until", ">=", start))
                 .order_by("valid_until").order_by("item_id").limit(limit))
        return await self._read_lean(query)

    async def enrich_queue(self, user_id: str, limit: int, job: Optional[str] = None) -> List[DriveIndexRecord]:
        queued = (FieldFilter("enrich_ready", "==", True) if job is None
                  else FieldFilter(f"{EnrichJob(job).value}_state", "==", JobState.READY.value))
        query = (self._user_query(user_id).where(filter=queued)
                 .order_by("effective_day", direction=firestore.Query.DESCENDING)
                 .order_by("parent_id").order_by("item_id").limit(limit))
        return await self._read(query)

    async def enrich_wakeups(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        query = (self._user_query(user_id).where(filter=FieldFilter("enrich_wake_at", "<=", now))
                 .order_by("enrich_wake_at").order_by("item_id").limit(limit))
        return await self._read(query)

    def job_state_count_queries(self, user_id: str, kinds: AbstractSet[str]) -> List[Tuple[str, str, Any]]:
        """(job, state, query) for `count_by_job_state`: one count per job, state and batch of at most IN_VALUES_MAX
        kinds (`kind in [...]`, the only disjunction in the query). Not a port method: the smoke script runs them."""
        queries = []
        for part in _chunks(sorted(set(kinds)), IN_VALUES_MAX):
            for job in EnrichJob:
                for state in JobState:
                    query = (self._user_query(user_id)
                             .where(filter=FieldFilter(f"{job.value}_state", "==", state.value))
                             .where(filter=FieldFilter("kind", "in", list(part))))
                    queries.append((job.value, state.value, query))
        return queries

    DESCRIBE_GAPS = ("waiting", "held", "no_passages")   # drive_enrich_plan.GAP_* (the derived `describe_gap`)

    def describe_gap_count_queries(self, user_id: str) -> List[Tuple[str, Any]]:
        """(gap, query) for `count_describe_gaps`: equality filters only (user_id, describe_gap), served by the
        single-field indexes. Not a port method: the smoke script runs them."""
        return [(gap, self._user_query(user_id).where(filter=FieldFilter("describe_gap", "==", gap)))
                for gap in self.DESCRIBE_GAPS]

    async def count_describe_gaps(self, user_id: str) -> Dict[str, int]:
        """Three count aggregations a tick, never a scan; a gap with no record is left out."""
        queries = self.describe_gap_count_queries(user_id)
        totals = await asyncio.gather(*(self._count(q) for _, q in queries))
        return {gap: n for (gap, _), n in zip(queries, totals) if n}

    async def count_by_job_state(self, user_id: str, kinds: AbstractSet[str]) -> Dict[str, Dict[str, int]]:
        """Count aggregations, never a scan; a state with no record is left out (as in `count_by_state`)."""
        queries = self.job_state_count_queries(user_id, kinds)
        totals = await asyncio.gather(*(self._count(q) for _, _, q in queries))
        counts: Dict[str, Dict[str, int]] = {job.value: {} for job in EnrichJob}
        for (job, state, _), n in zip(queries, totals):
            if n:
                counts[job][state] = counts[job].get(state, 0) + n
        return counts

    async def keys_pending_page(self, user_id: str, after: Optional[str], limit: int) -> List[DriveIndexRecord]:
        query = self._user_query(user_id).where(filter=FieldFilter("keys_pending", "==", True))
        if after is not None:
            query = query.where(filter=FieldFilter("item_id", ">", after))
        return await self._read(query.order_by("item_id").limit(limit))

    async def get_records_lean(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        found: Dict[str, DriveRecordLean] = {}
        for part in _chunks(list(dict.fromkeys(item_ids))):
            refs = [self._ref(user_id, i) for i in part]
            async for snap in self._db.get_all(refs, field_paths=list(RECORD_LEAN_FIELDS)):
                if snap.exists:
                    lean = lean_record_from_doc(snap.to_dict())
                    found[lean.item_id] = lean
        return found

    # -- Search v2: chunk keys (§25.2.2) ---------------------------------------------------------------------------

    def word_keys_query(self, user_id: str, keys: Sequence[str]) -> Any:
        """`find_chunks_by_word_keys`' query before its cursor and limit, None without keys (smoke script)."""
        wanted = _query_keys(keys, "find_chunks_by_word_keys")
        if not wanted:
            return None
        return (self._user_chunks(user_id).where(filter=FieldFilter("word_keys", "array_contains_any", wanted))
                .order_by("item_id").order_by("generation").order_by("index"))

    async def find_chunks_by_word_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                       after_item_id: Optional[str] = None) -> List[DriveChunkLean]:
        """A cursor on item_id alone positions past every chunk of that item (as `find_next_file_with_term`)."""
        query = self.word_keys_query(user_id, keys)
        if query is None:
            return []
        if after_item_id is not None:
            query = query.start_after({"item_id": after_item_id})
        snaps = await query.select(list(CHUNK_LEAN_FIELDS)).limit(limit).get()
        return [chunk_lean_from_doc(snap.to_dict()) for snap in snaps]

    async def _keys_by_id(self, refs_of: Callable[[ChunkId], Any], ids: Sequence[ChunkId],
                          field: str) -> Dict[ChunkId, Tuple[str, ...]]:
        found: Dict[ChunkId, Tuple[str, ...]] = {}
        wanted = list(dict.fromkeys(ids))
        for part in _chunks(wanted):
            async for snap in self._db.get_all([refs_of(i) for i in part],
                                               field_paths=["item_id", "generation", "index", field]):
                data = snap.to_dict() if snap.exists else None
                if data:
                    found[(data["item_id"], int(data["generation"]), int(data["index"]))] = _strs(data.get(field))
        return found

    async def get_chunk_keys(self, user_id: str, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        return await self._keys_by_id(lambda i: self._chunk_ref(user_id, *i), ids, "word_keys")

    # -- Search v2: hint documents (§25.2.2) -------------------------------------------------------------------------

    @property
    def _hint_col(self) -> Any:
        return self._db.collection(self._hints_name)

    def _hint_ref(self, user_id: str, item_id: str, generation: int, index: int) -> Any:
        return self._hint_col.document(chunk_key(user_id, item_id, generation, index))

    def _user_hints(self, user_id: str) -> Any:
        return self._hint_col.where(filter=FieldFilter("user_id", "==", user_id))

    def hint_keys_query(self, user_id: str, keys: Sequence[str]) -> Any:
        """`find_hints_by_keys`' query before its cursor and limit, None without keys (smoke script). Kind is never
        in it: the caller post-filters (§25.5.5, no `kind + hint_keys` composite)."""
        wanted = _query_keys(keys, "find_hints_by_keys")
        if not wanted:
            return None
        return (self._user_hints(user_id).where(filter=FieldFilter("hint_keys", "array_contains_any", wanted))
                .order_by("item_id").order_by("generation").order_by("index"))

    async def find_hints_by_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                 after_item_id: Optional[str] = None) -> List[DriveHintLean]:
        query = self.hint_keys_query(user_id, keys)
        if query is None:
            return []
        if after_item_id is not None:
            query = query.start_after({"item_id": after_item_id})
        snaps = await query.select(list(HINT_LEAN_FIELDS)).limit(limit).get()
        return [hint_lean_from_doc(snap.to_dict()) for snap in snaps]

    async def get_hint_keys(self, user_id: str, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        return await self._keys_by_id(lambda i: self._hint_ref(user_id, *i), ids, "hint_keys")

    async def find_nearest_hints(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveHintLean, float]]:
        query = self._user_hints(user_id)
        if kind is not None:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        # as find_nearest_cards: under a projection the distance comes back only when its name is selected too
        nearest = query.select([*HINT_LEAN_FIELDS, HINT_DISTANCE]).find_nearest(
            vector_field=HINT_VECTOR, query_vector=Vector([float(x) for x in vector]), limit=limit,
            distance_measure=DistanceMeasure.COSINE, distance_result_field=HINT_DISTANCE)
        hits: List[Tuple[DriveHintLean, float]] = []
        for snap in await nearest.get():
            data = snap.to_dict()
            hits.append((hint_lean_from_doc(data), float(data[HINT_DISTANCE])))
        return hits

    def _generation_hints(self, user_id: str, item_id: str, generation: int) -> Any:
        return (self._user_hints(user_id).where(filter=FieldFilter("item_id", "==", item_id))
                .where(filter=FieldFilter("generation", "==", generation)).order_by("index"))

    async def get_hints(self, user_id: str, item_id: str, generation: int) -> List[DriveHintDoc]:
        snaps = await self._generation_hints(user_id, item_id, generation).select(list(HINT_FIELDS)).get()
        return [hint_from_doc(snap.to_dict()) for snap in snaps]

    async def get_hints_with_vectors(self, user_id: str, item_id: str,
                                     generation: int) -> List[Tuple[DriveHintDoc, List[float]]]:
        query = self._generation_hints(user_id, item_id, generation).select([*HINT_FIELDS, HINT_VECTOR])
        rows: List[Tuple[DriveHintDoc, List[float]]] = []
        for snap in await query.get():
            data = snap.to_dict()
            rows.append((hint_from_doc(data), [float(x) for x in data.get(HINT_VECTOR) or ()]))
        return rows

    async def write_hints(self, docs: Sequence[DriveHintDoc], vectors: Sequence[Sequence[float]]) -> None:
        if len(docs) != len(vectors):
            logger.error(f"[DriveIndex] refused hint write: {len(docs)} documents but {len(vectors)} vectors")
            raise ValueError(f"{len(docs)} hint documents but {len(vectors)} vectors")
        writes = []
        for doc, vector in zip(docs, vectors):
            data = hint_to_doc(doc)
            data[HINT_VECTOR] = Vector([float(x) for x in vector])
            writes.append((self._hint_ref(doc.user_id, doc.item_id, doc.generation, doc.index), data))
        await self._set_in_batches(writes)

    async def delete_hints_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        """Equality filters only, the generation compared here (as `delete_chunks_except`)."""
        query = self._user_hints(user_id).where(filter=FieldFilter("item_id", "==", item_id)).select(["generation"])
        doomed = [snap.reference for snap in await query.get()
                  if (snap.to_dict() or {}).get("generation") != keep_generation]
        await self._delete_refs(doomed)
        return len(doomed)

    async def delete_user_hints(self, user_id: str, limit: int) -> int:
        snaps = await self._user_hints(user_id).select([FieldPath.document_id()]).limit(limit).get()
        await self._delete_refs([snap.reference for snap in snaps])
        return len(snaps)

    async def hints_page(self, user_id: str, after: Optional[ChunkId], limit: int) -> List[DriveHintLean]:
        query = self._user_hints(user_id).order_by("item_id").order_by("generation").order_by("index")
        if after is not None:
            query = query.start_after({"item_id": after[0], "generation": after[1], "index": after[2]})
        snaps = await query.select(list(HINT_LEAN_FIELDS)).limit(limit).get()
        return [hint_lean_from_doc(snap.to_dict()) for snap in snaps]

    # -- Search v2: model usage and the initial build (§25.4.5, D27) ----------------------------------------------------

    @property
    def _usage_col(self) -> Any:
        return self._db.collection(self._usage_name)

    async def get_model_usage(self, user_id: str, month: str) -> ModelUsageMonth:
        snap = await self._usage_col.document(usage_doc_id(user_id, month)).get()
        return usage_from_doc(snap.to_dict()) if snap.exists else ModelUsageMonth()

    async def add_model_usage(self, user_id: str, month: str, day: str, cls: str, usd: float, fallback_usd: float,
                              calls: int, covered_usd: float = 0.0) -> None:
        """One merge write with server-side increments: concurrent adds never lose one (no transaction). The initial
        build's share is written only when there is one."""
        name = f"{UsageClass(cls).value}_usd"   # ValueError for an unknown class, before any write
        data: Dict[str, Any] = {
            "user_id": user_id, "month": month, USAGE_EXPIRES_AT: usage_expires_at(month),
            name: firestore.Increment(float(usd)), "fallback_usd": firestore.Increment(float(fallback_usd)),
            "calls": firestore.Increment(int(calls)),
            "daily": {day: {"total_usd": firestore.Increment(float(usd)),
                            "fallback_usd": firestore.Increment(float(fallback_usd))}},
        }
        if covered_usd > 0:
            data[f"{UsageClass(cls).value}_initial_usd"] = firestore.Increment(float(covered_usd))
        await self._usage_col.document(usage_doc_id(user_id, month)).set(data, merge=True)

    async def get_initial_build(self, user_id: str) -> InitialBuild:
        snap = await self._usage_col.document(initial_doc_id(user_id)).get()
        return initial_from_doc(snap.to_dict()) if snap.exists else new_initial_build()

    async def spend_initial_build(self, user_id: str, usd: float, now: datetime) -> Tuple[InitialBuild, float]:
        ref = self._usage_col.document(initial_doc_id(user_id))

        @firestore.async_transactional
        async def _spend(transaction: Any) -> Tuple[InitialBuild, float]:
            snap = await ref.get(transaction=transaction)
            before = initial_from_doc(snap.to_dict()) if snap.exists else new_initial_build()
            after, covered = spent_initial(before, usd, now)
            if after != before:
                transaction.set(ref, initial_to_doc(user_id, after))
            return after, covered

        async with self._initial_lock:
            return await _spend(self._db.transaction())

    async def close_initial_build(self, user_id: str, now: datetime) -> None:
        ref = self._usage_col.document(initial_doc_id(user_id))

        @firestore.async_transactional
        async def _close(transaction: Any) -> None:
            snap = await ref.get(transaction=transaction)
            before = initial_from_doc(snap.to_dict()) if snap.exists else new_initial_build()
            after = closed_initial(before, now)
            if after != before:
                transaction.set(ref, initial_to_doc(user_id, after))

        async with self._initial_lock:
            await _close(self._db.transaction())


# Transient on Firestore's side: the same call may succeed later, so it is never one file's failure (§8.2).
_TRANSIENT = (DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted, ResourceExhausted)
# A query whose composite index is missing or still building (final review F4-2): no file's fault, and it does not heal
# by itself — logged as an error, and systemic for the tick like a transient error, so no file is counted for it.
_MISSING_INDEX = (FailedPrecondition,)



def _store_errors(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except _TRANSIENT as exc:
            logger.warning(f"[DriveIndex] index store {method.__name__} failed: {type(exc).__name__}: {exc}")
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}") from exc
        except _MISSING_INDEX as exc:
            logger.error(f"[DriveIndex] index store {method.__name__} failed, an index is missing or building: "
                         f"{type(exc).__name__}: {exc}")
            # the provider's message names the index to create (the smoke check and the deploy step print it)
            raise DriveIndexStoreUnavailable(f"{method.__name__}: {type(exc).__name__}: {exc}") from exc
    return run



for _name, _method in list(vars(FirestoreDriveIndexRepository).items()):
    if not _name.startswith("_") and inspect.iscoroutinefunction(_method):
        setattr(FirestoreDriveIndexRepository, _name, _store_errors(_method))
