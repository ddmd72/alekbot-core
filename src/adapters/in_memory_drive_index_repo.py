"""
InMemoryDriveIndexRepository — DriveIndexRepository in process memory. The second implementation of
the port: the eval (scripts/validation/drive_eval) and service tests run the real tick on it. kNN is
brute-force cosine; ordering and lease semantics match the Firestore adapter (Search v2's are pinned by
tests/unit/adapters/test_drive_index_v2_parity.py against it).
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from ..domain.drive_cap_alert import CapCounts
from ..domain.drive_enrichment import DriveHintDoc, EnrichJob, InitialBuild, JobState, ModelUsageMonth
from ..domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, LeaseLost, Reason, RecordState
from ..domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean, lean_of
from ..domain.drive_index_params import CONCEPT_KEYS_MAX, KNN_ITEM_IDS_MAX
from ..domain.drive_search import is_not_read_yet, is_unread
from ..domain.drive_transcript import (
    StoredTranscript, TranscriptBatch, TranscriptJob, WindowTranscript, batch_doc_id, batch_url_fields,
    job_url_fields, normalized_batch, transcript_doc_id,
)
from ..domain.drive_usage import (
    added_usage, closed_initial, new_initial_build, reserved_field, reserved_usage, spent_initial,
)
from ..ports.drive_index_repository import DriveIndexRepository
from ..utils.logger import logger


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)


# The fan-out's flags: only update_sync_flags writes them (the Firestore adapter's _FLAGS)
_FLAG_FIELDS = ("stale_alerted", "reconnect_notified", "migration_alerted", "stall_alerted", "fallback_alerted_day",
                "card_cap_alerted", "chunk_cap_alerted")


def _fence(current: Optional[DriveSyncState], owner: Optional[str], user_id: str) -> None:
    """The port's fence: the stored lease holder (None for a missing state) must be `owner`."""
    held = current.lease_owner if current is not None else None
    if held != owner:
        raise LeaseLost(f"sync state of {user_id[:8]} is held by {held!r}, not {owner!r}")


class InMemoryDriveIndexRepository(DriveIndexRepository):
    def __init__(self) -> None:
        self.records: Dict[Tuple[str, str], DriveIndexRecord] = {}
        self.vectors: Dict[Tuple[str, str], List[float]] = {}
        self.states: Dict[str, DriveSyncState] = {}
        # (user_id, item_id, generation, index) -> (chunk, vector)
        self.chunks: Dict[Tuple[str, str, int, int], Tuple[DriveChunk, List[float]]] = {}
        # Search v2 (§25.2.2, §25.4.5): hint documents by (user_id, item_id, generation, index); usage documents by
        # (user_id, "YYYY-MM") and the initial build by user, which a purge keeps (no delete method touches them)
        self.hints: Dict[Tuple[str, str, int, int], Tuple[DriveHintDoc, List[float]]] = {}
        self.usage: Dict[Tuple[str, str], ModelUsageMonth] = {}
        self.initial: Dict[str, InitialBuild] = {}
        # Transcripts (§27.3-§27.5, §27.8 b): jobs by (user_id, item_id), their window results by (user_id, item_id,
        # index), batches by (user_id, provider name) and the store by (user_id, content_key)
        self.transcript_jobs: Dict[Tuple[str, str], TranscriptJob] = {}
        self.window_results: Dict[Tuple[str, str, int], WindowTranscript] = {}
        self.batches: Dict[Tuple[str, str], TranscriptBatch] = {}
        self.transcripts: Dict[Tuple[str, str], StoredTranscript] = {}
        self._lock = asyncio.Lock()

    # -- sync state --------------------------------------------------------------------------

    async def get_sync_state(self, user_id: str) -> Optional[DriveSyncState]:
        return self.states.get(user_id)

    async def save_sync_state(self, state: DriveSyncState, owner: Optional[str] = None) -> None:
        """Keeps the stored fan-out flags and the stored lease: only their own methods write them."""
        async with self._lock:
            current = self.states.get(state.user_id)
            _fence(current, owner, state.user_id)
            base = current or DriveSyncState(user_id=state.user_id)
            flags = {name: getattr(base, name) for name in _FLAG_FIELDS}
            lease = (current.lease_owner, current.lease_until) if current else (None, None)
            self.states[state.user_id] = replace(state, **flags, lease_owner=lease[0], lease_until=lease[1])

    async def create_sync_state_if_absent(self, state: DriveSyncState) -> bool:
        async with self._lock:
            if state.user_id in self.states:
                return False
            self.states[state.user_id] = state
            return True

    async def update_sync_flags(self, user_id: str, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None, migration_alerted: Optional[bool] = None,
                                stall_alerted: Optional[bool] = None,
                                fallback_alerted_day: Optional[str] = None,
                                card_cap_alerted: Optional[bool] = None,
                                chunk_cap_alerted: Optional[bool] = None) -> None:
        current = self.states.get(user_id)
        if current is None:
            return
        given = {"stale_alerted": stale_alerted, "reconnect_notified": reconnect_notified,
                 "migration_alerted": migration_alerted, "stall_alerted": stall_alerted,
                 "fallback_alerted_day": fallback_alerted_day, "card_cap_alerted": card_cap_alerted,
                 "chunk_cap_alerted": chunk_cap_alerted}
        self.states[user_id] = replace(current, **{k: v for k, v in given.items() if v is not None})

    async def delete_sync_state(self, user_id: str, owner: Optional[str] = None) -> None:
        async with self._lock:
            current = self.states.get(user_id)
            if current is None:
                return
            _fence(current, owner, user_id)
            del self.states[user_id]

    async def acquire_lease(self, user_id: str, owner: str, now: datetime, until: datetime, *,
                            create: bool) -> Optional[DriveSyncState]:
        async with self._lock:
            state = self.states.get(user_id)
            if state is None:
                if not create:
                    return None
                state = DriveSyncState(user_id=user_id)
            if state.lease_owner and state.lease_until and state.lease_until > now and state.lease_owner != owner:
                return None
            state = replace(state, lease_owner=owner, lease_until=until)
            self.states[user_id] = state
            return state

    async def release_lease(self, user_id: str, owner: str) -> None:
        async with self._lock:
            state = self.states.get(user_id)
            if state is not None and state.lease_owner == owner:
                self.states[user_id] = replace(state, lease_owner=None, lease_until=None)

    async def list_sync_user_ids(self) -> List[str]:
        return sorted(self.states)

    # -- records -----------------------------------------------------------------------------

    def _user(self, user_id: str) -> List[DriveIndexRecord]:
        return [r for (u, _), r in self.records.items() if u == user_id]

    async def get_records(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        return {i: self.records[(user_id, i)] for i in item_ids if (user_id, i) in self.records}

    async def upsert_records(self, records: Sequence[DriveIndexRecord],
                             card_vectors: Optional[Mapping[str, Sequence[float]]] = None) -> None:
        stuck = [rec.item_id for rec in records if rec.due_at is not None and rec.priority is None]
        if stuck:   # never ordered by due_records; refused whole, before any write (as the Firestore adapter)
            raise ValueError(f"records due without a priority: {stuck[:5]}")
        for rec in records:
            key = (rec.user_id, rec.item_id)
            stored = self.records.get(key)
            # the fan-out's field is never part of a record write (§26.2, Δ54): the stored value stays
            self.records[key] = replace(rec, inaccessible_notified_since=stored.inaccessible_notified_since
                                        if stored is not None else None)
            if card_vectors and rec.item_id in card_vectors:
                self.vectors[key] = list(card_vectors[rec.item_id])
            elif not rec.has_card:
                self.vectors.pop(key, None)

    async def delete_records(self, user_id: str, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            self.records.pop((user_id, item_id), None)
            self.vectors.pop((user_id, item_id), None)

    async def due_records(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._user(user_id) if r.due_at is not None and r.due_at <= now]
        due.sort(key=lambda r: (r.priority if r.priority is not None else 99, r.due_at, r.item_id))
        return due[:limit]

    async def oldest_due_at(self, user_id: str, priority: int) -> Optional[datetime]:
        dates = [r.due_at for r in self._user(user_id) if r.priority == priority and r.due_at is not None]
        return min(dates) if dates else None

    async def has_record_in(self, user_id: str, state: RecordState, priority: int) -> bool:
        return any(r.state is state and r.priority == priority for r in self._user(user_id))

    async def children(self, user_id: str, parent_id: str, after_item_id: Optional[str],
                       limit: int) -> List[DriveIndexRecord]:
        kids = sorted((r for r in self._user(user_id) if r.parent_id == parent_id), key=lambda r: r.item_id)
        return [r for r in kids if after_item_id is None or r.item_id > after_item_id][:limit]

    async def records_page(self, user_id: str, after_item_id: Optional[str], limit: int) -> List[DriveIndexRecord]:
        recs = sorted(self._user(user_id), key=lambda r: r.item_id)
        return [r for r in recs if after_item_id is None or r.item_id > after_item_id][:limit]

    async def records_seen_before(self, user_id: str, epoch: int, limit: int) -> List[DriveIndexRecord]:
        return sorted((r for r in self._user(user_id) if r.seen_epoch < epoch), key=lambda r: r.item_id)[:limit]

    async def count_carded(self, user_id: str) -> int:
        return sum(1 for (u, _) in self.vectors if u == user_id)

    async def lowest_ranked_carded(self, user_id: str) -> Optional[DriveIndexRecord]:
        carded = [self.records[k] for k in self.vectors
                  if k[0] == user_id and k in self.records and self.records[k].state is not RecordState.DELETING]
        if not carded:
            return None
        return sorted(carded, key=lambda r: (-r.card_rank, r.modified_at.timestamp() if r.modified_at else math.inf,
                                             r.item_id))[0]

    async def card_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        capped = [r for r in self._user(user_id) if r.card_capped]
        capped.sort(key=lambda r: (r.card_rank, -(r.modified_at.timestamp() if r.modified_at else 0.0)))
        return capped[:limit]

    async def chunk_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        refused = [r for r in self._user(user_id)
                   if r.state is RecordState.METADATA_ONLY and r.reason is Reason.USER_CHUNK_CAP]
        refused.sort(key=lambda r: (r.card_rank, -(r.modified_at.timestamp() if r.modified_at else 0.0), r.item_id))
        return refused[:limit]

    async def count_capped(self, user_id: str) -> CapCounts:
        recs = self._user(user_id)
        return CapCounts(card=sum(1 for r in recs if r.card_capped),
                         chunk=sum(1 for r in recs
                                   if r.state is RecordState.METADATA_ONLY and r.reason is Reason.USER_CHUNK_CAP))

    async def inaccessible_records(self, user_id: str, limit: int, *, lean: bool = False) -> List[DriveIndexRecord]:
        marked = [r for r in self._user(user_id) if r.inaccessible_since is not None]
        marked.sort(key=lambda r: (r.inaccessible_since, r.item_id))
        return marked[:limit]

    async def set_inaccessible_notified(self, user_id: str, values: Mapping[str, Optional[datetime]]) -> None:
        for item_id, since in values.items():
            key = (user_id, item_id)
            if key in self.records:
                self.records[key] = replace(self.records[key], inaccessible_notified_since=since)

    async def count_unread(self, user_id: str, kinds: AbstractSet[str],
                           triaged: AbstractSet[str] = frozenset(), *, pending: Optional[bool] = None,
                           transcribed: AbstractSet[str] = frozenset()) -> int:
        admitted, images, media = frozenset(kinds), frozenset(triaged), frozenset(transcribed) - frozenset(kinds)
        if pending:
            return sum(1 for r in self._user(user_id) if is_not_read_yet(r, admitted, images, media))
        if pending is False:
            return sum(1 for r in self._user(user_id)
                       if is_unread(r, admitted, images, media) and r.state is not RecordState.PENDING)
        return sum(1 for r in self._user(user_id) if is_unread(r, admitted, images, media))

    async def count_by_state(self, user_id: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rec in self._user(user_id):
            counts[rec.state.value] = counts.get(rec.state.value, 0) + 1
        counts["carded"] = await self.count_carded(user_id)
        return counts

    async def delete_user_records(self, user_id: str, limit: int) -> int:
        keys = [k for k in self.records if k[0] == user_id][:limit]
        for key in keys:
            self.records.pop(key, None)
            self.vectors.pop(key, None)
        return len(keys)

    # -- search ------------------------------------------------------------------------------

    async def find_nearest_cards(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveIndexRecord, float]]:
        scored = [(self.records[k], _cosine_distance(vector, v)) for k, v in self.vectors.items()
                  if k[0] == user_id and k in self.records and (kind is None or self.records[k].kind == kind)]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_by_name_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                kind: Optional[str] = None) -> List[DriveIndexRecord]:
        wanted = set(keys)
        hits = [r for r in self._user(user_id)
                if wanted & set(r.name_tokens) and (kind is None or r.kind == kind)]
        return sorted(hits, key=lambda r: r.item_id)[:limit]

    async def filter_records(self, user_id: str, kind: Optional[str], date_from: Optional[datetime],
                             date_to: Optional[datetime], limit: int) -> List[DriveIndexRecord]:
        recs = [r for r in self._user(user_id)
                if (kind is None or r.kind == kind) and r.effective_date is not None
                and (date_from is None or r.effective_date >= date_from)
                and (date_to is None or r.effective_date <= date_to)]
        recs.sort(key=lambda r: r.effective_date or datetime.min, reverse=True)
        return recs[:limit]

    async def card_vectors(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, List[float]]:
        return {i: self.vectors[(user_id, i)] for i in item_ids if (user_id, i) in self.vectors}

    # -- chunks ------------------------------------------------------------------------------

    def _user_chunks(self, user_id: str, kind: Optional[str] = None) -> List[Tuple[DriveChunk, List[float]]]:
        rows = [(c, v) for k, (c, v) in sorted(self.chunks.items()) if k[0] == user_id]
        return [(c, v) for c, v in rows if kind is None or c.kind == kind]

    async def write_chunks(self, chunks: Sequence[DriveChunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        for chunk, vector in zip(chunks, vectors):
            self.chunks[(chunk.user_id, chunk.item_id, chunk.generation, chunk.index)] = (chunk, list(vector))

    async def delete_chunks_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        doomed = [k for k in self.chunks if k[0] == user_id and k[1] == item_id and k[2] != keep_generation]
        for key in doomed:
            del self.chunks[key]
        return len(doomed)

    async def find_nearest_chunks(self, user_id: str, vector: Sequence[float], limit: int,
                                  kind: Optional[str] = None) -> List[Tuple[DriveChunk, float]]:
        scored = [(c, _cosine_distance(vector, v)) for c, v in self._user_chunks(user_id, kind)]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_nearest_chunks_in(self, user_id: str, vector: Sequence[float], limit: int,
                                     item_ids: Sequence[str]) -> List[Tuple[DriveChunk, float]]:
        wanted = set(_knn_item_ids(item_ids, "find_nearest_chunks_in"))
        scored = [(c, _cosine_distance(vector, v)) for c, v in self._user_chunks(user_id) if c.item_id in wanted]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_chunks_by_term(self, user_id: str, term: str, limit: int,
                                  kind: Optional[str] = None) -> List[DriveChunk]:
        return [c for c, _ in self._user_chunks(user_id, kind) if term in c.content_terms][:limit]

    async def find_next_file_with_term(self, user_id: str, term: str, after_item_id: str,
                                       kind: Optional[str] = None) -> Optional[DriveChunk]:
        later = [c for c, _ in self._user_chunks(user_id, kind)
                 if c.item_id > after_item_id and term in c.content_terms]
        return min(later, key=lambda c: (c.item_id, c.generation, c.index), default=None)

    async def get_chunks(self, user_id: str, item_id: str, generation: int,
                         indexes: Sequence[int]) -> List[DriveChunk]:
        return [self.chunks[(user_id, item_id, generation, i)][0] for i in sorted(set(indexes))
                if (user_id, item_id, generation, i) in self.chunks]

    async def get_chunks_with_vectors(self, user_id: str, item_id: str, generation: int, indexes: Sequence[int], *,
                                      text: bool = True, terms: bool = True,
                                      vectors: bool = True) -> List[Tuple[DriveChunk, List[float]]]:
        rows = [self.chunks[(user_id, item_id, generation, i)] for i in sorted(set(indexes))
                if (user_id, item_id, generation, i) in self.chunks]
        return [(replace(c, text=c.text if text else "", content_terms=c.content_terms if terms else (),
                         word_keys=c.word_keys if terms else ()),
                 list(v) if vectors else []) for c, v in rows]

    async def readable_chunk_total(self, user_id: str) -> int:
        return sum(r.chunk_count for r in self._user(user_id) if r.indexed_generation is not None)

    async def chunks_page(self, user_id: str, after: Optional[Tuple[str, int, int]],
                          limit: int) -> List[DriveChunk]:
        rows = [c for c, _ in self._user_chunks(user_id)]
        return [c for c in rows if after is None or (c.item_id, c.generation, c.index) > after][:limit]

    async def delete_user_chunks(self, user_id: str, limit: int) -> int:
        keys = sorted(k for k in self.chunks if k[0] == user_id)[:limit]
        for key in keys:
            del self.chunks[key]
        return len(keys)

    # -- Search v2 (§25.13.1) ---------------------------------------------------------------------------------------

    def _about_hits(self, user_id: str, keys: Sequence[str], method: str) -> List[DriveIndexRecord]:
        wanted = set(_query_keys(keys, method))
        return [r for r in self._user(user_id) if wanted & set(r.about_keys)] if wanted else []

    async def find_by_about_keys(self, user_id: str, keys: Sequence[str], limit: int, *, kind: Optional[str] = None,
                                 date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                                 after: Optional[str] = None) -> List[DriveRecordLean]:
        dated = date_from is not None or date_to is not None
        hits = [r for r in self._about_hits(user_id, keys, "find_by_about_keys")
                if (kind is None or r.kind == kind)
                and (not dated or (r.effective_date is not None
                                   and (date_from is None or r.effective_date >= date_from)
                                   and (date_to is None or r.effective_date <= date_to)))]
        hits.sort(key=_newest_first, reverse=True)
        if after is not None:
            cursor = self.records.get((user_id, after))
            if cursor is None:
                return []   # the cursor record is gone: the paging ends (port)
            hits = [r for r in hits if _newest_first(r) < _newest_first(cursor)]
        return [lean_of(r) for r in hits[:limit]]

    async def find_expiring(self, user_id: str, keys: Sequence[str], after_date: date,
                            limit: int) -> List[DriveRecordLean]:
        start = _day_start(after_date)
        hits = [r for r in self._about_hits(user_id, keys, "find_expiring") if r.valid_until is not None and r.valid_until >= start]
        hits.sort(key=lambda r: (r.valid_until, r.item_id))
        return [lean_of(r) for r in hits[:limit]]

    async def find_chunks_by_word_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                       after_item_id: Optional[str] = None) -> List[DriveChunkLean]:
        wanted = set(_query_keys(keys, "find_chunks_by_word_keys"))
        hits = [c for c, _ in self._user_chunks(user_id) if wanted & set(c.word_keys)
                and (after_item_id is None or c.item_id > after_item_id)]
        return [DriveChunkLean(c.user_id, c.item_id, c.generation, c.index, c.kind) for c in hits[:limit]]

    async def get_chunk_keys(self, user_id: str, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        return {i: self.chunks[(user_id, *i)][0].word_keys for i in ids if (user_id, *i) in self.chunks}

    def _user_hints(self, user_id: str, kind: Optional[str] = None) -> List[Tuple[DriveHintDoc, List[float]]]:
        rows = [row for k, row in sorted(self.hints.items()) if k[0] == user_id]
        return [(h, v) for h, v in rows if kind is None or h.kind == kind]

    async def find_hints_by_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                 after_item_id: Optional[str] = None) -> List[DriveHintLean]:
        wanted = set(_query_keys(keys, "find_hints_by_keys"))
        hits = [h for h, _ in self._user_hints(user_id) if wanted & set(h.hint_keys)
                and (after_item_id is None or h.item_id > after_item_id)]
        return [_hint_lean(h) for h in hits[:limit]]

    async def get_hint_keys(self, user_id: str, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        return {i: self.hints[(user_id, *i)][0].hint_keys for i in ids if (user_id, *i) in self.hints}

    async def find_nearest_hints(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveHintLean, float]]:
        scored = [(h, _cosine_distance(vector, v)) for h, v in self._user_hints(user_id, kind)]
        scored.sort(key=lambda pair: pair[1])   # stable: equal distances keep the key order
        return [(_hint_lean(h), d) for h, d in scored[:limit]]

    async def find_nearest_hints_in(self, user_id: str, vector: Sequence[float], limit: int,
                                    item_ids: Sequence[str]) -> List[Tuple[DriveHintLean, float]]:
        wanted = set(_knn_item_ids(item_ids, "find_nearest_hints_in"))
        scored = [(h, _cosine_distance(vector, v)) for h, v in self._user_hints(user_id) if h.item_id in wanted]
        scored.sort(key=lambda pair: pair[1])   # stable: equal distances keep the key order
        return [(_hint_lean(h), d) for h, d in scored[:limit]]

    async def get_hints(self, user_id: str, item_id: str, generation: int) -> List[DriveHintDoc]:
        return [h for h, _ in await self.get_hints_with_vectors(user_id, item_id, generation)]

    async def get_hints_with_vectors(self, user_id: str, item_id: str,
                                     generation: int) -> List[Tuple[DriveHintDoc, List[float]]]:
        return [(h, list(v)) for k, (h, v) in sorted(self.hints.items())
                if k[:3] == (user_id, item_id, generation)]

    async def write_hints(self, docs: Sequence[DriveHintDoc], vectors: Sequence[Sequence[float]]) -> None:
        if len(docs) != len(vectors):
            raise ValueError(f"{len(docs)} hint documents but {len(vectors)} vectors")
        for doc, vector in zip(docs, vectors):
            self.hints[(doc.user_id, doc.item_id, doc.generation, doc.index)] = (doc, list(vector))

    async def delete_hints_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        doomed = [k for k in self.hints if k[0] == user_id and k[1] == item_id and k[2] != keep_generation]
        for key in doomed:
            del self.hints[key]
        return len(doomed)

    async def delete_user_hints(self, user_id: str, limit: int) -> int:
        keys = sorted(k for k in self.hints if k[0] == user_id)[:limit]
        for key in keys:
            del self.hints[key]
        return len(keys)

    async def hints_page(self, user_id: str, after: Optional[ChunkId], limit: int) -> List[DriveHintLean]:
        rows = [h for h, _ in self._user_hints(user_id)]
        return [_hint_lean(h) for h in rows if after is None or (h.item_id, h.generation, h.index) > after][:limit]

    async def enrich_queue(self, user_id: str, limit: int, job: Optional[str] = None) -> List[DriveIndexRecord]:
        field = None if job is None else f"{EnrichJob(job).value}_state"   # ValueError for an unknown job

        def queued(r: DriveIndexRecord) -> bool:
            return r.enrich_ready if field is None else getattr(r, field) == JobState.READY.value
        ready = sorted((r for r in self._user(user_id) if queued(r)), key=lambda r: (r.parent_id, r.item_id))
        ready.sort(key=lambda r: r.effective_day, reverse=True)   # stable: (parent_id, item_id) within a day
        return ready[:limit]

    async def enrich_wakeups(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._user(user_id) if r.enrich_wake_at is not None and r.enrich_wake_at <= now]
        return sorted(due, key=lambda r: (r.enrich_wake_at, r.item_id))[:limit]

    async def count_describe_gaps(self, user_id: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rec in self._user(user_id):
            if rec.describe_gap:
                counts[rec.describe_gap] = counts.get(rec.describe_gap, 0) + 1
        return counts

    async def count_by_job_state(self, user_id: str, kinds: AbstractSet[str]) -> Dict[str, Dict[str, int]]:
        states = {s.value for s in JobState}
        counts: Dict[str, Dict[str, int]] = {job.value: {} for job in EnrichJob}
        for rec in self._user(user_id):
            if rec.kind not in kinds:
                continue
            for job, state in ((EnrichJob.PROFILE.value, rec.profile_state),
                               (EnrichJob.PASSAGES.value, rec.passages_state)):
                if state in states:
                    counts[job][state] = counts[job].get(state, 0) + 1
        return counts

    async def keys_pending_page(self, user_id: str, after: Optional[str], limit: int) -> List[DriveIndexRecord]:
        recs = sorted((r for r in self._user(user_id) if r.keys_pending), key=lambda r: r.item_id)
        return [r for r in recs if after is None or r.item_id > after][:limit]

    async def get_records_lean(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        return {i: lean_of(self.records[(user_id, i)]) for i in item_ids if (user_id, i) in self.records}

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

    async def get_initial_build(self, user_id: str) -> InitialBuild:
        return self.initial.get(user_id) or new_initial_build()

    async def spend_initial_build(self, user_id: str, usd: float, now: datetime) -> Tuple[InitialBuild, float]:
        async with self._lock:
            before = self.initial.get(user_id) or new_initial_build()
            after, covered = spent_initial(before, usd, now)
            if after != before:
                self.initial[user_id] = after
            return after, covered

    async def close_initial_build(self, user_id: str, now: datetime) -> None:
        async with self._lock:
            self.initial[user_id] = closed_initial(self.initial.get(user_id) or new_initial_build(), now)

    # -- transcripts: jobs, window results, batches, the store (§27.3-§27.5, §27.8 b) ---------------------------------

    async def get_transcript_job(self, user_id: str, item_id: str) -> Optional[TranscriptJob]:
        return self.transcript_jobs.get((user_id, item_id))

    async def save_transcript_job(self, job: TranscriptJob) -> None:
        _refuse_urls("job", job_url_fields(job))
        self.transcript_jobs[(job.user_id, job.item_id)] = job

    async def delete_transcript_job(self, user_id: str, item_id: str) -> None:
        self.transcript_jobs.pop((user_id, item_id), None)
        for key in [k for k in self.window_results if k[:2] == (user_id, item_id)]:
            del self.window_results[key]

    async def save_window_result(self, user_id: str, item_id: str, index: int, result: WindowTranscript) -> None:
        self.window_results[(user_id, item_id, index)] = result

    async def get_window_results(self, user_id: str, item_id: str) -> Dict[int, WindowTranscript]:
        return {k[2]: v for k, v in sorted(self.window_results.items()) if k[:2] == (user_id, item_id)}

    async def create_batch(self, batch: TranscriptBatch) -> bool:
        _refuse_urls("batch", batch_url_fields(batch))
        batch = normalized_batch(batch)
        async with self._lock:
            if (batch.user_id, batch.name) in self.batches:
                return False
            self.batches[(batch.user_id, batch.name)] = batch
            return True

    async def save_batch(self, batch: TranscriptBatch) -> None:
        _refuse_urls("batch", batch_url_fields(batch))
        batch = normalized_batch(batch)
        self.batches[(batch.user_id, batch.name)] = batch

    async def delete_batch(self, user_id: str, name: str) -> None:
        self.batches.pop((user_id, name), None)

    async def get_open_batches(self, user_id: str, now: datetime, limit: int) -> List[TranscriptBatch]:
        due = [b for b in self.batches.values()
               if b.user_id == user_id and b.next_poll_at is not None and b.next_poll_at <= now]
        return sorted(due, key=lambda b: (b.next_poll_at, batch_doc_id(b.user_id, b.name)))[:limit]

    async def get_transcript(self, user_id: str, content_key: str) -> Optional[StoredTranscript]:
        return self.transcripts.get((user_id, content_key))

    async def put_transcript(self, user_id: str, stored: StoredTranscript) -> None:
        self.transcripts[(user_id, stored.content_key)] = stored

    async def touch_transcript(self, user_id: str, content_key: str, last_used_at: datetime) -> None:
        current = self.transcripts.get((user_id, content_key))
        if current is not None:
            self.transcripts[(user_id, content_key)] = replace(current, last_used_at=last_used_at)

    async def delete_transcript(self, user_id: str, content_key: str) -> None:
        self.transcripts.pop((user_id, content_key), None)

    # -- transcripts: queues on the records, copies in flight, orphans, purge (§27.3, §27.8 b, G12) ----------------------

    async def transcript_queue(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        ready = sorted((r for r in self._user(user_id) if r.transcript_state == JobState.READY.value),
                       key=lambda r: (r.parent_id, r.item_id))
        ready.sort(key=lambda r: r.effective_day, reverse=True)   # stable: (parent_id, item_id) within a day
        return ready[:limit]

    async def transcript_wakeups(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        due = [r for r in self._user(user_id) if r.transcript_wake_at is not None and r.transcript_wake_at <= now]
        return sorted(due, key=lambda r: (r.transcript_wake_at, r.item_id))[:limit]

    async def transcript_records(self, user_id: str, state: str, limit: int,
                                 after: Optional[str] = None) -> List[DriveIndexRecord]:
        found = (r for r in self._user(user_id) if r.transcript_state == state and (after is None or r.item_id > after))
        return sorted(found, key=lambda r: r.item_id)[:limit]

    async def mirror_queue(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        due = sorted((r for r in self._user(user_id) if r.mirror_due), key=lambda r: r.item_id)
        due.sort(key=lambda r: r.effective_day, reverse=True)   # stable: item_id ascending within a day
        return due[:limit]

    async def mirror_wakeups(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        ripe = [r for r in self._user(user_id) if r.mirror_retry_at is not None and r.mirror_retry_at <= now]
        return sorted(ripe, key=lambda r: (r.mirror_retry_at, r.item_id))[:limit]

    async def jobs_by_content_key(self, user_id: str, content_key: str) -> List[TranscriptJob]:
        return sorted((j for (u, _), j in self.transcript_jobs.items() if u == user_id and j.content_key == content_key),
                      key=lambda j: j.item_id)

    async def orphan_transcripts(self, user_id: str, before: datetime, limit: int) -> List[str]:
        old = [s for (u, _), s in self.transcripts.items() if u == user_id and s.last_used_at is not None and s.last_used_at < before]
        return [s.content_key for s in sorted(old, key=lambda s: (s.last_used_at, s.content_key))[:limit]]

    async def transcript_key_in_use(self, user_id: str, content_key: str) -> bool:
        return any(r.transcript_key == content_key for r in self._user(user_id))

    async def delete_user_transcript_jobs(self, user_id: str, limit: int) -> int:
        keys = sorted(k for k in self.transcript_jobs if k[0] == user_id)[:limit]
        for key in keys:
            await self.delete_transcript_job(*key)
        return len(keys)

    async def delete_user_transcript_orphans(self, user_id: str, limit: int, *, keep_transcripts: bool) -> int:
        """The user's window results, all of them: called after the helpers returned 0, so none has a job (parts are not
        separate documents here, so `keep_transcripts` changes nothing), as on Firestore where the sweep cannot tell."""
        keys = sorted(k for k in self.window_results if k[0] == user_id)[:limit]
        for key in keys:
            del self.window_results[key]
        return len(keys)

    async def delete_user_batches(self, user_id: str, limit: int) -> int:
        keys = sorted(k for k in self.batches if k[0] == user_id)[:limit]
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
