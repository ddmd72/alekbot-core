"""
InMemoryDriveIndexRepository — DriveIndexRepository in process memory. The second implementation of
the port: the eval (scripts/validation/drive_eval) and service tests run the real tick on it. kNN is
brute-force cosine; ordering and lease semantics match the Firestore adapter.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState
from ..ports.drive_index_repository import DriveIndexRepository


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)


class InMemoryDriveIndexRepository(DriveIndexRepository):
    def __init__(self) -> None:
        self.records: Dict[Tuple[str, str], DriveIndexRecord] = {}
        self.vectors: Dict[Tuple[str, str], List[float]] = {}
        self.states: Dict[str, DriveSyncState] = {}
        self._lock = asyncio.Lock()

    # -- sync state --------------------------------------------------------------------------

    async def get_sync_state(self, user_id: str) -> Optional[DriveSyncState]:
        return self.states.get(user_id)

    async def save_sync_state(self, state: DriveSyncState) -> None:
        """Keeps the stored fan-out flags and the stored lease: only their own methods write them."""
        current = self.states.get(state.user_id)
        flags = (current.stale_alerted, current.reconnect_notified) if current else (False, False)
        lease = (current.lease_owner, current.lease_until) if current else (None, None)
        self.states[state.user_id] = replace(state, stale_alerted=flags[0], reconnect_notified=flags[1],
                                             lease_owner=lease[0], lease_until=lease[1])

    async def update_sync_flags(self, user_id: str, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None) -> None:
        current = self.states.get(user_id)
        if current is None:
            return
        if stale_alerted is not None:
            current = replace(current, stale_alerted=stale_alerted)
        if reconnect_notified is not None:
            current = replace(current, reconnect_notified=reconnect_notified)
        self.states[user_id] = current

    async def delete_sync_state(self, user_id: str) -> None:
        self.states.pop(user_id, None)

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
        for rec in records:
            if rec.due_at is not None and rec.priority is None:
                raise ValueError(f"record {rec.item_id} is due without a priority")   # never ordered by due_records
            key = (rec.user_id, rec.item_id)
            self.records[key] = rec
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
        capped = [r for r in self._user(user_id)
                  if r.state is RecordState.METADATA_ONLY and r.reason is Reason.USER_CARD_CAP]
        capped.sort(key=lambda r: (r.card_rank, -(r.modified_at.timestamp() if r.modified_at else 0.0)))
        return capped[:limit]

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
