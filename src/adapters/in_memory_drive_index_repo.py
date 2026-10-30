"""
InMemoryDriveIndexRepository — DriveIndexRepository in process memory. The second implementation of
the port: the eval (scripts/validation/drive_eval) and service tests run the real tick on it. kNN is
brute-force cosine; ordering and lease semantics match the Firestore adapter (Search v2's are pinned by
tests/unit/adapters/test_drive_index_v2_parity.py against it).

Connection scope (MULTI_DRIVE_RFC §8, I3): the sync states, records, vectors, chunks, hints, transcript jobs, window
results and batches are keyed by the connection id (as the Firestore documents are); a stored object whose `user_id`
differs from the scope's is treated as absent and logged at ERROR. The usage months, the initial builds (by
`initial_build_doc_id`) and the transcript store are keyed by the user.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import AbstractSet, Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar, Union

from ..domain.connection_scope import ConnectionScope
from ..domain.drive_cap_alert import CapCounts
from ..domain.drive_enrichment import DriveHintDoc, EnrichJob, InitialBuild, JobState, ModelUsageMonth
from ..domain.reconnect_policy import Cleared
from ..domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, LeaseLost, Reason, RecordState
from ..domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean, lean_of
from ..domain.drive_index_params import CONCEPT_KEYS_MAX, KNN_ITEM_IDS_MAX
from ..domain.drive_search import is_not_read_yet, is_unread
from ..domain.drive_transcript import (
    StoredTranscript, TranscriptBatch, TranscriptJob, WindowTranscript, batch_url_fields, job_url_fields,
    normalized_batch, transcript_batch_doc_id, transcript_doc_id,
)
from ..domain.drive_usage import (
    added_usage, closed_initial, initial_build_doc_id, new_initial_build, reserved_field, reserved_usage, spent_initial,
)
from ..ports.drive_index_repository import DriveIndexRepository
from ..utils.logger import logger

_T = TypeVar("_T")


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)


# The fan-out's flags: only update_sync_flags writes them (the Firestore adapter's _FLAGS)
_FLAG_FIELDS = ("stale_alerted", "reconnect_notified", "migration_alerted", "stall_alerted", "fallback_alerted_day",
                "card_cap_alerted", "chunk_cap_alerted", "disconnected_by_user_at")


def _fence(current: Optional[DriveSyncState], owner: Optional[str], connection_id: str) -> None:
    """The port's fence: the stored lease holder (None for a missing state) must be `owner`."""
    held = current.lease_owner if current is not None else None
    if held != owner:
        raise LeaseLost(f"sync state of connection {connection_id[:8]} is held by {held!r}, not {owner!r}")


def _connection_of(kind: str, connection_id: str) -> str:
    """The connection id a write is keyed by; ValueError before any write when it is empty (§8)."""
    if not connection_id:
        logger.error(f"[DriveIndex] refused {kind} write: no connection id")
        raise ValueError(f"a {kind} write needs its connection id")
    return connection_id


def _owned(scope: ConnectionScope, found: Optional[_T], what: str) -> Optional[_T]:
    """I3: a stored object of another user under the scope's connection is absent (and an ERROR)."""
    if found is None:
        return None
    if getattr(found, "user_id") != scope.user_id:
        logger.error(f"[DriveIndex] {what} of connection {scope.connection_id[:8]} belongs to another user: "
                     f"treated as absent")
        return None
    return found


class InMemoryDriveIndexRepository(DriveIndexRepository):
    def __init__(self) -> None:
        # (connection_id, item_id) -> record / card vector
        self.records: Dict[Tuple[str, str], DriveIndexRecord] = {}
        self.vectors: Dict[Tuple[str, str], List[float]] = {}
        self.states: Dict[str, DriveSyncState] = {}   # by connection id
        # (connection_id, item_id, generation, index) -> (chunk, vector)
        self.chunks: Dict[Tuple[str, str, int, int], Tuple[DriveChunk, List[float]]] = {}
        # Search v2 (§25.2.2, §25.4.5): hint documents by (connection_id, item_id, generation, index); usage documents by
        # (user_id, "YYYY-MM") and the initial builds by `initial_build_doc_id`, which a purge keeps (no delete method
        # touches them)
        self.hints: Dict[Tuple[str, str, int, int], Tuple[DriveHintDoc, List[float]]] = {}
        self.usage: Dict[Tuple[str, str], ModelUsageMonth] = {}
        self.initial: Dict[str, InitialBuild] = {}
        # Transcripts (§27.3-§27.5, §27.8 b): jobs by (connection_id, item_id), their window results by (connection_id,
        # item_id, index), batches by (connection_id, provider name) and the store by (user_id, content_key)
        self.transcript_jobs: Dict[Tuple[str, str], TranscriptJob] = {}
        self.window_results: Dict[Tuple[str, str, int], Tuple[str, WindowTranscript]] = {}   # value: (user_id, result)
        self.batches: Dict[Tuple[str, str], TranscriptBatch] = {}
        self.transcripts: Dict[Tuple[str, str], StoredTranscript] = {}
        self._lock = asyncio.Lock()

    # -- sync state --------------------------------------------------------------------------

    async def get_sync_state(self, scope: ConnectionScope) -> Optional[DriveSyncState]:
        return _owned(scope, self.states.get(scope.connection_id), "sync state")

    async def save_sync_state(self, state: DriveSyncState, owner: Optional[str] = None) -> None:
        """Keeps the stored fan-out flags and the stored lease: only their own methods write them."""
        cid = _connection_of("sync state", state.connection_id)
        async with self._lock:
            current = self.states.get(cid)
            _fence(current, owner, cid)
            base = current or DriveSyncState(user_id=state.user_id, connection_id=cid)
            flags = {name: getattr(base, name) for name in _FLAG_FIELDS}
            lease = (current.lease_owner, current.lease_until) if current else (None, None)
            self.states[cid] = replace(state, **flags, lease_owner=lease[0], lease_until=lease[1])

    async def create_sync_state_if_absent(self, state: DriveSyncState) -> bool:
        cid = _connection_of("sync state", state.connection_id)
        async with self._lock:
            if cid in self.states:
                return False
            self.states[cid] = state
            return True

    async def update_sync_flags(self, scope: ConnectionScope, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None, migration_alerted: Optional[bool] = None,
                                stall_alerted: Optional[bool] = None,
                                fallback_alerted_day: Optional[str] = None,
                                card_cap_alerted: Optional[bool] = None,
                                chunk_cap_alerted: Optional[bool] = None,
                                disconnected_by_user_at: Union[datetime, Cleared, None] = None) -> None:
        current = _owned(scope, self.states.get(scope.connection_id), "sync state")
        if current is None:
            return
        given = {"stale_alerted": stale_alerted, "reconnect_notified": reconnect_notified,
                 "migration_alerted": migration_alerted, "stall_alerted": stall_alerted,
                 "fallback_alerted_day": fallback_alerted_day, "card_cap_alerted": card_cap_alerted,
                 "chunk_cap_alerted": chunk_cap_alerted}
        changes: Dict[str, Any] = {k: v for k, v in given.items() if v is not None}
        if disconnected_by_user_at is not None:
            changes["disconnected_by_user_at"] = None if disconnected_by_user_at is Cleared.MARK \
                else disconnected_by_user_at
        self.states[scope.connection_id] = replace(current, **changes)

    async def delete_sync_state(self, scope: ConnectionScope, owner: Optional[str] = None) -> None:
        async with self._lock:
            current = _owned(scope, self.states.get(scope.connection_id), "sync state")
            if current is None:
                return
            _fence(current, owner, scope.connection_id)
            del self.states[scope.connection_id]

    async def acquire_lease(self, scope: ConnectionScope, owner: str, now: datetime, until: datetime, *,
                            create: bool) -> Optional[DriveSyncState]:
        async with self._lock:
            stored = self.states.get(scope.connection_id)
            state = _owned(scope, stored, "sync state")
            if stored is not None and state is None:
                return None   # another user's state under this id: never taken, never overwritten (I3)
            if state is None:
                if not create:
                    return None
                state = DriveSyncState(user_id=scope.user_id, connection_id=scope.connection_id)
            if state.lease_owner and state.lease_until and state.lease_until > now and state.lease_owner != owner:
                return None
            state = replace(state, lease_owner=owner, lease_until=until)
            self.states[scope.connection_id] = state
            return state

    async def release_lease(self, scope: ConnectionScope, owner: str) -> None:
        async with self._lock:
            state = self.states.get(scope.connection_id)
            if state is not None and state.user_id == scope.user_id and state.lease_owner == owner:
                self.states[scope.connection_id] = replace(state, lease_owner=None, lease_until=None)

    async def list_sync_scopes(self) -> List[ConnectionScope]:
        return [ConnectionScope(user_id=s.user_id, connection_id=cid) for cid, s in sorted(self.states.items())]

    # -- records -----------------------------------------------------------------------------

    def _conn(self, scope: ConnectionScope) -> List[DriveIndexRecord]:
        found = [r for (c, _), r in self.records.items() if c == scope.connection_id]
        return [r for r in found if _owned(scope, r, f"record {r.item_id[:8]}") is not None]

    def _record(self, scope: ConnectionScope, item_id: str) -> Optional[DriveIndexRecord]:
        return _owned(scope, self.records.get((scope.connection_id, item_id)), f"record {item_id[:8]}")

    async def get_records(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        found = {i: self._record(scope, i) for i in dict.fromkeys(item_ids)}
        return {i: r for i, r in found.items() if r is not None}

    async def records_by_identity(self, scope: ConnectionScope,
                                  identities: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        wanted = set(identities)
        return {r.identity: r for r in sorted(self._conn(scope), key=lambda r: r.item_id)
                if r.identity and r.identity in wanted}

    async def upsert_records(self, records: Sequence[DriveIndexRecord],
                             card_vectors: Optional[Mapping[str, Sequence[float]]] = None) -> None:
        stuck = [rec.item_id for rec in records if rec.due_at is not None and rec.priority is None]
        if stuck:   # never ordered by due_records; refused whole, before any write (as the Firestore adapter)
            raise ValueError(f"records due without a priority: {stuck[:5]}")
        for rec in records:
            _connection_of("record", rec.connection_id)
        for rec in records:
            key = (rec.connection_id, rec.item_id)
            stored = self.records.get(key)
            # the fan-out's field is never part of a record write (§26.2, Δ54): the stored value stays
            self.records[key] = replace(rec, inaccessible_notified_since=stored.inaccessible_notified_since
                                        if stored is not None else None)
            if card_vectors and rec.item_id in card_vectors:
                self.vectors[key] = list(card_vectors[rec.item_id])
            elif not rec.has_card:
                self.vectors.pop(key, None)

    async def delete_records(self, scope: ConnectionScope, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            if self._record(scope, item_id) is None:
                continue
            self.records.pop((scope.connection_id, item_id), None)
            self.vectors.pop((scope.connection_id, item_id), None)

    async def due_records(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._conn(scope) if r.due_at is not None and r.due_at <= now]
        due.sort(key=lambda r: (r.priority if r.priority is not None else 99, r.due_at, r.item_id))
        return due[:limit]

    async def oldest_due_at(self, scope: ConnectionScope, priority: int) -> Optional[datetime]:
        dates = [r.due_at for r in self._conn(scope) if r.priority == priority and r.due_at is not None]
        return min(dates) if dates else None

    async def has_record_in(self, scope: ConnectionScope, state: RecordState, priority: int) -> bool:
        return any(r.state is state and r.priority == priority for r in self._conn(scope))

    async def children(self, scope: ConnectionScope, parent_id: str, after_item_id: Optional[str],
                       limit: int) -> List[DriveIndexRecord]:
        kids = sorted((r for r in self._conn(scope) if r.parent_id == parent_id), key=lambda r: r.item_id)
        return [r for r in kids if after_item_id is None or r.item_id > after_item_id][:limit]

    async def records_page(self, scope: ConnectionScope, after_item_id: Optional[str],
                           limit: int) -> List[DriveIndexRecord]:
        recs = sorted(self._conn(scope), key=lambda r: r.item_id)
        return [r for r in recs if after_item_id is None or r.item_id > after_item_id][:limit]

    async def records_seen_before(self, scope: ConnectionScope, epoch: int, limit: int) -> List[DriveIndexRecord]:
        return sorted((r for r in self._conn(scope) if r.seen_epoch < epoch), key=lambda r: r.item_id)[:limit]

    def _carded_keys(self, scope: ConnectionScope) -> List[Tuple[str, str]]:
        return [k for k in self.vectors if k[0] == scope.connection_id and self._record(scope, k[1]) is not None]

    async def count_carded(self, scope: ConnectionScope) -> int:
        return len(self._carded_keys(scope))

    async def lowest_ranked_carded(self, scope: ConnectionScope) -> Optional[DriveIndexRecord]:
        carded = [self.records[k] for k in self._carded_keys(scope)
                  if self.records[k].state is not RecordState.DELETING]
        if not carded:
            return None
        return sorted(carded, key=lambda r: (-r.card_rank, r.modified_at.timestamp() if r.modified_at else math.inf,
                                             r.item_id))[0]

    async def card_capped(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        capped = [r for r in self._conn(scope) if r.card_capped]
        capped.sort(key=lambda r: (r.card_rank, -(r.modified_at.timestamp() if r.modified_at else 0.0)))
        return capped[:limit]

    async def chunk_capped(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        refused = [r for r in self._conn(scope)
                   if r.state is RecordState.METADATA_ONLY and r.reason is Reason.USER_CHUNK_CAP]
        refused.sort(key=lambda r: (r.card_rank, -(r.modified_at.timestamp() if r.modified_at else 0.0), r.item_id))
        return refused[:limit]

    async def count_capped(self, scope: ConnectionScope) -> CapCounts:
        recs = self._conn(scope)
        return CapCounts(card=sum(1 for r in recs if r.card_capped),
                         chunk=sum(1 for r in recs
                                   if r.state is RecordState.METADATA_ONLY and r.reason is Reason.USER_CHUNK_CAP))

    async def inaccessible_records(self, scope: ConnectionScope, limit: int, *,
                                   lean: bool = False) -> List[DriveIndexRecord]:
        marked = [r for r in self._conn(scope) if r.inaccessible_since is not None]
        marked.sort(key=lambda r: (r.inaccessible_since, r.item_id))
        return marked[:limit]

    async def set_inaccessible_notified(self, scope: ConnectionScope,
                                        values: Mapping[str, Optional[datetime]]) -> None:
        for item_id, since in values.items():
            rec = self._record(scope, item_id)
            if rec is not None:
                self.records[(scope.connection_id, item_id)] = replace(rec, inaccessible_notified_since=since)

    async def count_unread(self, scope: ConnectionScope, kinds: AbstractSet[str],
                           triaged: AbstractSet[str] = frozenset(), *, pending: Optional[bool] = None,
                           transcribed: AbstractSet[str] = frozenset()) -> int:
        admitted, images, media = frozenset(kinds), frozenset(triaged), frozenset(transcribed) - frozenset(kinds)
        if pending:
            return sum(1 for r in self._conn(scope) if is_not_read_yet(r, admitted, images, media))
        if pending is False:
            return sum(1 for r in self._conn(scope)
                       if is_unread(r, admitted, images, media) and r.state is not RecordState.PENDING)
        return sum(1 for r in self._conn(scope) if is_unread(r, admitted, images, media))

    async def count_by_state(self, scope: ConnectionScope) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rec in self._conn(scope):
            counts[rec.state.value] = counts.get(rec.state.value, 0) + 1
        counts["carded"] = await self.count_carded(scope)
        return counts

    async def delete_user_records(self, scope: ConnectionScope, limit: int) -> int:
        keys = [(scope.connection_id, r.item_id) for r in self._conn(scope)][:limit]
        for key in keys:
            self.records.pop(key, None)
            self.vectors.pop(key, None)
        return len(keys)

    # -- search ------------------------------------------------------------------------------

    async def find_nearest_cards(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveIndexRecord, float]]:
        scored = [(self.records[k], _cosine_distance(vector, self.vectors[k])) for k in self._carded_keys(scope)
                  if kind is None or self.records[k].kind == kind]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_by_name_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                kind: Optional[str] = None) -> List[DriveIndexRecord]:
        wanted = set(keys)
        hits = [r for r in self._conn(scope)
                if wanted & set(r.name_tokens) and (kind is None or r.kind == kind)]
        return sorted(hits, key=lambda r: r.item_id)[:limit]

    async def filter_records(self, scope: ConnectionScope, kind: Optional[str], date_from: Optional[datetime],
                             date_to: Optional[datetime], limit: int) -> List[DriveIndexRecord]:
        recs = [r for r in self._conn(scope)
                if (kind is None or r.kind == kind) and r.effective_date is not None
                and (date_from is None or r.effective_date >= date_from)
                and (date_to is None or r.effective_date <= date_to)]
        recs.sort(key=lambda r: r.effective_date or datetime.min, reverse=True)
        return recs[:limit]

    async def card_vectors(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, List[float]]:
        keys = set(self._carded_keys(scope))
        return {i: self.vectors[(scope.connection_id, i)] for i in item_ids if (scope.connection_id, i) in keys}

    # -- chunks ------------------------------------------------------------------------------

    def _conn_chunks(self, scope: ConnectionScope,
                     kind: Optional[str] = None) -> List[Tuple[DriveChunk, List[float]]]:
        rows = [(c, v) for k, (c, v) in sorted(self.chunks.items()) if k[0] == scope.connection_id]
        return [(c, v) for c, v in rows
                if (kind is None or c.kind == kind) and _owned(scope, c, f"chunk {c.item_id[:8]}") is not None]

    async def write_chunks(self, chunks: Sequence[DriveChunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        for chunk in chunks:
            _connection_of("chunk", chunk.connection_id)
        for chunk, vector in zip(chunks, vectors):
            self.chunks[(chunk.connection_id, chunk.item_id, chunk.generation, chunk.index)] = (chunk, list(vector))

    async def delete_chunks_except(self, scope: ConnectionScope, item_id: str,
                                   keep_generation: Optional[int]) -> int:
        doomed = [(scope.connection_id, c.item_id, c.generation, c.index) for c, _ in self._conn_chunks(scope)
                  if c.item_id == item_id and c.generation != keep_generation]
        for key in doomed:
            del self.chunks[key]
        return len(doomed)

    async def find_nearest_chunks(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                  kind: Optional[str] = None) -> List[Tuple[DriveChunk, float]]:
        scored = [(c, _cosine_distance(vector, v)) for c, v in self._conn_chunks(scope, kind)]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_nearest_chunks_in(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                     item_ids: Sequence[str]) -> List[Tuple[DriveChunk, float]]:
        wanted = set(_knn_item_ids(item_ids, "find_nearest_chunks_in"))
        scored = [(c, _cosine_distance(vector, v)) for c, v in self._conn_chunks(scope) if c.item_id in wanted]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_chunks_by_term(self, scope: ConnectionScope, term: str, limit: int,
                                  kind: Optional[str] = None) -> List[DriveChunk]:
        return [c for c, _ in self._conn_chunks(scope, kind) if term in c.content_terms][:limit]

    async def find_next_file_with_term(self, scope: ConnectionScope, term: str, after_item_id: str,
                                       kind: Optional[str] = None) -> Optional[DriveChunk]:
        later = [c for c, _ in self._conn_chunks(scope, kind)
                 if c.item_id > after_item_id and term in c.content_terms]
        return min(later, key=lambda c: (c.item_id, c.generation, c.index), default=None)

    def _chunk_rows(self, scope: ConnectionScope, item_id: str, generation: int,
                    indexes: Iterable[int]) -> List[Tuple[DriveChunk, List[float]]]:
        rows = [self.chunks.get((scope.connection_id, item_id, generation, i)) for i in sorted(set(indexes))]
        return [row for row in rows if row is not None and _owned(scope, row[0], f"chunk {item_id[:8]}") is not None]

    async def get_chunks(self, scope: ConnectionScope, item_id: str, generation: int,
                         indexes: Sequence[int]) -> List[DriveChunk]:
        return [c for c, _ in self._chunk_rows(scope, item_id, generation, indexes)]

    async def get_chunks_with_vectors(self, scope: ConnectionScope, item_id: str, generation: int,
                                      indexes: Sequence[int], *, text: bool = True, terms: bool = True,
                                      vectors: bool = True) -> List[Tuple[DriveChunk, List[float]]]:
        rows = self._chunk_rows(scope, item_id, generation, indexes)
        return [(replace(c, text=c.text if text else "", content_terms=c.content_terms if terms else (),
                         word_keys=c.word_keys if terms else ()),
                 list(v) if vectors else []) for c, v in rows]

    async def readable_chunk_total(self, scope: ConnectionScope) -> int:
        return sum(r.chunk_count for r in self._conn(scope) if r.indexed_generation is not None)

    async def chunks_page(self, scope: ConnectionScope, after: Optional[Tuple[str, int, int]],
                          limit: int) -> List[DriveChunk]:
        rows = [c for c, _ in self._conn_chunks(scope)]
        return [c for c in rows if after is None or (c.item_id, c.generation, c.index) > after][:limit]

    async def delete_user_chunks(self, scope: ConnectionScope, limit: int) -> int:
        keys = [(scope.connection_id, c.item_id, c.generation, c.index) for c, _ in self._conn_chunks(scope)][:limit]
        for key in keys:
            del self.chunks[key]
        return len(keys)

    # -- Search v2 (§25.13.1) ---------------------------------------------------------------------------------------

    def _about_hits(self, scope: ConnectionScope, keys: Sequence[str], method: str) -> List[DriveIndexRecord]:
        wanted = set(_query_keys(keys, method))
        return [r for r in self._conn(scope) if wanted & set(r.about_keys)] if wanted else []

    async def find_by_about_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int, *,
                                 kind: Optional[str] = None, date_from: Optional[datetime] = None,
                                 date_to: Optional[datetime] = None,
                                 after: Optional[str] = None) -> List[DriveRecordLean]:
        dated = date_from is not None or date_to is not None
        hits = [r for r in self._about_hits(scope, keys, "find_by_about_keys")
                if (kind is None or r.kind == kind)
                and (not dated or (r.effective_date is not None
                                   and (date_from is None or r.effective_date >= date_from)
                                   and (date_to is None or r.effective_date <= date_to)))]
        hits.sort(key=_newest_first, reverse=True)
        if after is not None:
            cursor = self._record(scope, after)
            if cursor is None:
                return []   # the cursor record is gone: the paging ends (port)
            hits = [r for r in hits if _newest_first(r) < _newest_first(cursor)]
        return [lean_of(r) for r in hits[:limit]]

    async def find_expiring(self, scope: ConnectionScope, keys: Sequence[str], after_date: date,
                            limit: int) -> List[DriveRecordLean]:
        start = _day_start(after_date)
        hits = [r for r in self._about_hits(scope, keys, "find_expiring")
                if r.valid_until is not None and r.valid_until >= start]
        hits.sort(key=lambda r: (r.valid_until, r.item_id))
        return [lean_of(r) for r in hits[:limit]]

    async def find_chunks_by_word_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                       after_item_id: Optional[str] = None) -> List[DriveChunkLean]:
        wanted = set(_query_keys(keys, "find_chunks_by_word_keys"))
        hits = [c for c, _ in self._conn_chunks(scope) if wanted & set(c.word_keys)
                and (after_item_id is None or c.item_id > after_item_id)]
        return [DriveChunkLean(c.user_id, c.item_id, c.generation, c.index, c.kind) for c in hits[:limit]]

    async def get_chunk_keys(self, scope: ConnectionScope,
                             ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        found = {i: self._chunk_rows(scope, i[0], i[1], (i[2],)) for i in ids}
        return {i: rows[0][0].word_keys for i, rows in found.items() if rows}

    def _conn_hints(self, scope: ConnectionScope,
                    kind: Optional[str] = None) -> List[Tuple[DriveHintDoc, List[float]]]:
        rows = [row for k, row in sorted(self.hints.items()) if k[0] == scope.connection_id]
        return [(h, v) for h, v in rows
                if (kind is None or h.kind == kind) and _owned(scope, h, f"hint {h.item_id[:8]}") is not None]

    async def find_hints_by_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                 after_item_id: Optional[str] = None) -> List[DriveHintLean]:
        wanted = set(_query_keys(keys, "find_hints_by_keys"))
        hits = [h for h, _ in self._conn_hints(scope) if wanted & set(h.hint_keys)
                and (after_item_id is None or h.item_id > after_item_id)]
        return [_hint_lean(h) for h in hits[:limit]]

    async def get_hint_keys(self, scope: ConnectionScope,
                            ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        by_id = {(h.item_id, h.generation, h.index): h for h, _ in self._conn_hints(scope)}
        return {i: by_id[i].hint_keys for i in ids if i in by_id}

    async def find_nearest_hints(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveHintLean, float]]:
        scored = [(h, _cosine_distance(vector, v)) for h, v in self._conn_hints(scope, kind)]
        scored.sort(key=lambda pair: pair[1])   # stable: equal distances keep the key order
        return [(_hint_lean(h), d) for h, d in scored[:limit]]

    async def find_nearest_hints_in(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                    item_ids: Sequence[str]) -> List[Tuple[DriveHintLean, float]]:
        wanted = set(_knn_item_ids(item_ids, "find_nearest_hints_in"))
        scored = [(h, _cosine_distance(vector, v)) for h, v in self._conn_hints(scope) if h.item_id in wanted]
        scored.sort(key=lambda pair: pair[1])   # stable: equal distances keep the key order
        return [(_hint_lean(h), d) for h, d in scored[:limit]]

    async def get_hints(self, scope: ConnectionScope, item_id: str, generation: int) -> List[DriveHintDoc]:
        return [h for h, _ in await self.get_hints_with_vectors(scope, item_id, generation)]

    async def get_hints_with_vectors(self, scope: ConnectionScope, item_id: str,
                                     generation: int) -> List[Tuple[DriveHintDoc, List[float]]]:
        return [(h, list(v)) for h, v in self._conn_hints(scope)
                if (h.item_id, h.generation) == (item_id, generation)]

    async def write_hints(self, docs: Sequence[DriveHintDoc], vectors: Sequence[Sequence[float]]) -> None:
        if len(docs) != len(vectors):
            raise ValueError(f"{len(docs)} hint documents but {len(vectors)} vectors")
        for doc in docs:
            _connection_of("hint", doc.connection_id)
        for doc, vector in zip(docs, vectors):
            self.hints[(doc.connection_id, doc.item_id, doc.generation, doc.index)] = (doc, list(vector))

    async def delete_hints_except(self, scope: ConnectionScope, item_id: str,
                                  keep_generation: Optional[int]) -> int:
        doomed = [(scope.connection_id, h.item_id, h.generation, h.index) for h, _ in self._conn_hints(scope)
                  if h.item_id == item_id and h.generation != keep_generation]
        for key in doomed:
            del self.hints[key]
        return len(doomed)

    async def delete_user_hints(self, scope: ConnectionScope, limit: int) -> int:
        keys = [(scope.connection_id, h.item_id, h.generation, h.index) for h, _ in self._conn_hints(scope)][:limit]
        for key in keys:
            del self.hints[key]
        return len(keys)

    async def hints_page(self, scope: ConnectionScope, after: Optional[ChunkId], limit: int) -> List[DriveHintLean]:
        rows = [h for h, _ in self._conn_hints(scope)]
        return [_hint_lean(h) for h in rows if after is None or (h.item_id, h.generation, h.index) > after][:limit]

    async def enrich_queue(self, scope: ConnectionScope, limit: int,
                           job: Optional[str] = None) -> List[DriveIndexRecord]:
        field = None if job is None else f"{EnrichJob(job).value}_state"   # ValueError for an unknown job

        def queued(r: DriveIndexRecord) -> bool:
            return r.enrich_ready if field is None else getattr(r, field) == JobState.READY.value
        ready = sorted((r for r in self._conn(scope) if queued(r)), key=lambda r: (r.parent_id, r.item_id))
        ready.sort(key=lambda r: r.effective_day, reverse=True)   # stable: (parent_id, item_id) within a day
        return ready[:limit]

    async def enrich_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._conn(scope) if r.enrich_wake_at is not None and r.enrich_wake_at <= now]
        return sorted(due, key=lambda r: (r.enrich_wake_at, r.item_id))[:limit]

    async def count_describe_gaps(self, scope: ConnectionScope) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rec in self._conn(scope):
            if rec.describe_gap:
                counts[rec.describe_gap] = counts.get(rec.describe_gap, 0) + 1
        return counts

    async def count_by_job_state(self, scope: ConnectionScope,
                                 kinds: AbstractSet[str]) -> Dict[str, Dict[str, int]]:
        states = {s.value for s in JobState}
        counts: Dict[str, Dict[str, int]] = {job.value: {} for job in EnrichJob}
        for rec in self._conn(scope):
            if rec.kind not in kinds:
                continue
            for job, state in ((EnrichJob.PROFILE.value, rec.profile_state),
                               (EnrichJob.PASSAGES.value, rec.passages_state)):
                if state in states:
                    counts[job][state] = counts[job].get(state, 0) + 1
        return counts

    async def keys_pending_page(self, scope: ConnectionScope, after: Optional[str],
                                limit: int) -> List[DriveIndexRecord]:
        recs = sorted((r for r in self._conn(scope) if r.keys_pending), key=lambda r: r.item_id)
        return [r for r in recs if after is None or r.item_id > after][:limit]

    async def get_records_lean(self, scope: ConnectionScope,
                               item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        return {i: lean_of(r) for i, r in (await self.get_records(scope, item_ids)).items()}

    # -- user-scoped: model usage and the initial build (§25.4.5, D8, MULTI_DRIVE_RFC §10) ---------------------------

    async def get_model_usage(self, user_id: str, month: str) -> ModelUsageMonth:
        return self.usage.get((user_id, month), ModelUsageMonth())

    async def add_model_usage(self, user_id: str, month: str, day: str, cls: str, usd: float, fallback_usd: float,
                              calls: int, covered_usd: float = 0.0) -> None:
        async with self._lock:
            current = self.usage.get((user_id, month), ModelUsageMonth())
            self.usage[(user_id, month)] = added_usage(current, day, cls, usd, fallback_usd, calls, covered_usd)

    async def reserve_model_usd(self, user_id: str, month: str, cls: str, delta_usd: float) -> None:
        reserved_field(cls)   # ValueError for a class with no reservations, before any write
        async with self._lock:
            current = self.usage.get((user_id, month), ModelUsageMonth())
            self.usage[(user_id, month)] = reserved_usage(current, cls, delta_usd)

    async def get_initial_build(self, user_id: str, realm: str, subject: str) -> InitialBuild:
        return self.initial.get(initial_build_doc_id(user_id, realm, subject)) or new_initial_build()

    async def spend_initial_build(self, user_id: str, realm: str, subject: str, usd: float,
                                  now: datetime) -> Tuple[InitialBuild, float]:
        key = initial_build_doc_id(user_id, realm, subject)
        async with self._lock:
            before = self.initial.get(key) or new_initial_build()
            after, covered = spent_initial(before, usd, now)
            if after != before:
                self.initial[key] = after
            return after, covered

    async def close_initial_build(self, user_id: str, realm: str, subject: str, now: datetime) -> None:
        key = initial_build_doc_id(user_id, realm, subject)
        async with self._lock:
            self.initial[key] = closed_initial(self.initial.get(key) or new_initial_build(), now)

    # -- transcripts: jobs, window results, batches, the store (§27.3-§27.5, §27.8 b) ---------------------------------

    async def get_transcript_job(self, scope: ConnectionScope, item_id: str) -> Optional[TranscriptJob]:
        return _owned(scope, self.transcript_jobs.get((scope.connection_id, item_id)), "transcript job")

    async def save_transcript_job(self, job: TranscriptJob) -> None:
        _refuse_urls("job", job_url_fields(job))
        self.transcript_jobs[(_connection_of("transcript job", job.connection_id), job.item_id)] = job

    async def delete_transcript_job(self, scope: ConnectionScope, item_id: str) -> None:
        if await self.get_transcript_job(scope, item_id) is not None:
            self.transcript_jobs.pop((scope.connection_id, item_id), None)
        for key in [k for k, (u, _) in self.window_results.items()
                    if k[:2] == (scope.connection_id, item_id) and u == scope.user_id]:
            del self.window_results[key]

    async def save_window_result(self, scope: ConnectionScope, item_id: str, index: int,
                                 result: WindowTranscript) -> None:
        self.window_results[(scope.connection_id, item_id, index)] = (scope.user_id, result)

    async def get_window_results(self, scope: ConnectionScope, item_id: str) -> Dict[int, WindowTranscript]:
        return {k[2]: v for k, (u, v) in sorted(self.window_results.items())
                if k[:2] == (scope.connection_id, item_id) and u == scope.user_id}

    async def create_batch(self, batch: TranscriptBatch) -> bool:
        _refuse_urls("batch", batch_url_fields(batch))
        batch = normalized_batch(batch)
        key = (_connection_of("transcript batch", batch.connection_id), batch.name)
        async with self._lock:
            if key in self.batches:
                return False
            self.batches[key] = batch
            return True

    async def save_batch(self, batch: TranscriptBatch) -> None:
        _refuse_urls("batch", batch_url_fields(batch))
        batch = normalized_batch(batch)
        self.batches[(_connection_of("transcript batch", batch.connection_id), batch.name)] = batch

    async def delete_batch(self, scope: ConnectionScope, name: str) -> None:
        if _owned(scope, self.batches.get((scope.connection_id, name)), "transcript batch") is not None:
            del self.batches[(scope.connection_id, name)]

    def _conn_batches(self, scope: ConnectionScope) -> List[TranscriptBatch]:
        return [b for (c, _), b in sorted(self.batches.items())
                if c == scope.connection_id and _owned(scope, b, "transcript batch") is not None]

    async def get_open_batches(self, scope: ConnectionScope, now: datetime, limit: int) -> List[TranscriptBatch]:
        due = [b for b in self._conn_batches(scope) if b.next_poll_at is not None and b.next_poll_at <= now]
        return sorted(due, key=lambda b: (b.next_poll_at, transcript_batch_doc_id(scope, b.name)))[:limit]

    async def get_transcript(self, user_id: str, content_key: str) -> Optional[StoredTranscript]:
        return self.transcripts.get((user_id, content_key))

    async def put_transcript(self, user_id: str, stored: StoredTranscript) -> None:
        self.transcripts[(user_id, stored.content_key)] = stored

    async def touch_transcript(self, user_id: str, content_key: str, last_used_at: datetime) -> None:
        current = self.transcripts.get((user_id, content_key))
        if current is not None:
            self.transcripts[(user_id, content_key)] = replace(current, last_used_at=last_used_at)

    async def retain_transcript(self, user_id: str, content_key: str, at: datetime, reason: str) -> bool:
        current = self.transcripts.get((user_id, content_key))
        if current is None:
            return False
        if current.retained_at is None:
            self.transcripts[(user_id, content_key)] = replace(current, retained_at=at, retained_reason=reason)
        return True

    async def transcript_retained(self, user_id: str, content_key: str) -> bool:
        current = self.transcripts.get((user_id, content_key))
        return current is not None and current.retained_at is not None

    async def delete_transcript(self, user_id: str, content_key: str) -> None:
        self.transcripts.pop((user_id, content_key), None)

    # -- transcripts: queues on the records, copies in flight, orphans, purge (§27.3, §27.8 b, G12) ----------------------

    async def transcript_queue(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        ready = sorted((r for r in self._conn(scope) if r.transcript_state == JobState.READY.value),
                       key=lambda r: (r.parent_id, r.item_id))
        ready.sort(key=lambda r: r.effective_day, reverse=True)   # stable: (parent_id, item_id) within a day
        return ready[:limit]

    async def transcript_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._conn(scope) if r.transcript_wake_at is not None and r.transcript_wake_at <= now]
        return sorted(due, key=lambda r: (r.transcript_wake_at, r.item_id))[:limit]

    async def transcript_records(self, scope: ConnectionScope, state: str, limit: int,
                                 after: Optional[str] = None) -> List[DriveIndexRecord]:
        found = (r for r in self._conn(scope) if r.transcript_state == state and (after is None or r.item_id > after))
        return sorted(found, key=lambda r: r.item_id)[:limit]

    async def mirror_queue(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        due = sorted((r for r in self._conn(scope) if r.mirror_due), key=lambda r: r.item_id)
        due.sort(key=lambda r: r.effective_day, reverse=True)   # stable: item_id ascending within a day
        return due[:limit]

    async def mirror_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        ripe = [r for r in self._conn(scope) if r.mirror_retry_at is not None and r.mirror_retry_at <= now]
        return sorted(ripe, key=lambda r: (r.mirror_retry_at, r.item_id))[:limit]

    async def jobs_by_content_key(self, scope: ConnectionScope, content_key: str) -> List[TranscriptJob]:
        return sorted((j for (c, _), j in self.transcript_jobs.items()
                       if c == scope.connection_id and j.user_id == scope.user_id and j.content_key == content_key),
                      key=lambda j: j.item_id)

    async def orphan_transcripts(self, user_id: str, before: datetime, limit: int) -> List[str]:
        old = [s for (u, _), s in self.transcripts.items()
               if u == user_id and s.last_used_at is not None and s.last_used_at < before]
        return [s.content_key for s in sorted(old, key=lambda s: (s.last_used_at, s.content_key))[:limit]]

    async def transcript_key_in_use(self, user_id: str, content_key: str) -> bool:
        """Every connection of the user: the store is user-scoped (§10)."""
        return any(r.user_id == user_id and r.transcript_key == content_key for r in self.records.values())

    async def delete_user_transcript_jobs(self, scope: ConnectionScope, limit: int) -> int:
        keys = sorted(k for k, j in self.transcript_jobs.items()
                      if k[0] == scope.connection_id and j.user_id == scope.user_id)[:limit]
        for _, item_id in keys:
            await self.delete_transcript_job(scope, item_id)
        return len(keys)

    async def delete_user_transcript_orphans(self, scope: ConnectionScope, limit: int, *,
                                             keep_transcripts: bool) -> int:
        """The connection's window results, all of them: called after the helpers returned 0, so none has a job (parts
        are not separate documents here, so `keep_transcripts` changes nothing), as on Firestore where the sweep cannot
        tell."""
        keys = sorted(k for k, (u, _) in self.window_results.items()
                      if k[0] == scope.connection_id and u == scope.user_id)[:limit]
        for key in keys:
            del self.window_results[key]
        return len(keys)

    async def delete_user_batches(self, scope: ConnectionScope, limit: int) -> int:
        keys = [(scope.connection_id, b.name) for b in self._conn_batches(scope)][:limit]
        for key in keys:
            del self.batches[key]
        return len(keys)

    async def delete_user_transcripts(self, user_id: str, limit: int) -> int:
        keys = sorted(k for k in self.transcripts if k[0] == user_id)[:limit]
        for key in keys:
            del self.transcripts[key]
        return len(keys)

    async def delete_user_transcripts_except(self, user_id: str, limit: int, keep: FrozenSet[str],
                                             after: Optional[str] = None) -> Tuple[int, Optional[str]]:
        heads = sorted((transcript_doc_id(u, ck), ck) for (u, ck) in self.transcripts if u == user_id)
        page = [(doc_id, ck) for doc_id, ck in heads if after is None or doc_id > after][:limit]
        deleted = 0
        for _, ck in page:
            if ck not in keep:
                del self.transcripts[(user_id, ck)]
                deleted += 1
        return deleted, (page[-1][0] if len(page) == limit and page else None)


def _refuse_urls(kind: str, fields: Sequence[str]) -> None:
    """§27.4: no URL is persisted (as the Firestore adapter): logs the field names and refuses the write."""
    if fields:
        logger.error(f"[DriveIndex] transcript {kind} refused: URL-like value in {', '.join(fields)}")
        raise ValueError(f"transcript {kind} holds a URL in {', '.join(fields)}")


def _query_keys(keys: Sequence[str], method: str) -> List[str]:
    """The distinct keys of one query, in order; more than CONCEPT_KEYS_MAX is refused and logged, as the Firestore
    adapter does (Firestore's limit; never a silent cut)."""
    wanted = list(dict.fromkeys(keys))
    if len(wanted) > CONCEPT_KEYS_MAX:
        logger.error(f"[DriveIndex] {method} refused: {len(wanted)} distinct keys, at most {CONCEPT_KEYS_MAX}")
        raise ValueError(f"{method}: {len(wanted)} distinct keys, at most {CONCEPT_KEYS_MAX}")
    return wanted


def _knn_item_ids(item_ids: Sequence[str], method: str) -> List[str]:
    """The distinct item ids of one scoped kNN, in order; more than KNN_ITEM_IDS_MAX is refused and logged, as the
    Firestore adapter does (Firestore's `in` limit; never a silent cut)."""
    wanted = list(dict.fromkeys(item_ids))
    if len(wanted) > KNN_ITEM_IDS_MAX:
        logger.error(f"[DriveIndex] {method} refused: {len(wanted)} distinct item ids, at most {KNN_ITEM_IDS_MAX}")
        raise ValueError(f"{method}: {len(wanted)} distinct item ids, at most {KNN_ITEM_IDS_MAX}")
    return wanted


def _day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _newest_first(rec: DriveIndexRecord) -> Tuple[bool, datetime, str]:
    """Sorted with reverse=True: dated records newest first, then the undated, item_id descending within a date
    (Firestore orders null below every timestamp)."""
    dated = rec.effective_date is not None
    return dated, rec.effective_date if dated else _NO_DATE, rec.item_id


_NO_DATE = datetime.min.replace(tzinfo=timezone.utc)


def _hint_lean(doc: DriveHintDoc) -> DriveHintLean:
    return DriveHintLean(doc.user_id, doc.item_id, doc.generation, doc.index, doc.kind, doc.hint_keys)
