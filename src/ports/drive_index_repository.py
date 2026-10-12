"""
DriveIndexRepository — storage of the drive index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5).

Records and the per-user sync state; chunks join in Slice 2. Batching and transactions are the
adapter's business. Card vectors travel with record writes but never inside the domain record.
Ownership of DriveSyncState fields: the tick writes everything through `save_sync_state` under the
lease, except `stale_alerted` and `reconnect_notified`, which only the fan-out writes, through
`update_sync_flags`; `save_sync_state` must leave those two untouched. It also leaves the lease
(`lease_owner`, `lease_until`) untouched: only `acquire_lease` and `release_lease` write it, so a tick
that outlived its lease cannot write it back and then release the new holder's.
Fencing (§7.1): `save_sync_state` and `delete_sync_state` apply atomically and only while `owner` holds the
stored lease; `owner=None` is a write outside any tick and applies only while no lease holder is stored (a
missing state counts as one with none). Anything else raises `drive_index.LeaseLost` and writes nothing, so a tick that
outlived its lease cannot purge or overwrite the state of the tick that took over.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_index import DriveIndexRecord, DriveSyncState


class DriveIndexRepository(ABC):

    # -- sync state --------------------------------------------------------------------------

    @abstractmethod
    async def get_sync_state(self, user_id: str) -> Optional[DriveSyncState]:
        """The user's sync state, or None if nothing was ever stored."""

    @abstractmethod
    async def save_sync_state(self, state: DriveSyncState, owner: Optional[str] = None) -> None:
        """Write the whole state except the fan-out flags and the lease, fenced by `owner` (LeaseLost)."""

    @abstractmethod
    async def create_sync_state_if_absent(self, state: DriveSyncState) -> bool:
        """Store `state` only if the user has none (an atomic create); True when it was written. The fan-out's minimal
        state, so staleness covers a user whose ticks never land (§12.1)."""

    @abstractmethod
    async def update_sync_flags(self, user_id: str, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None) -> None:
        """Field-level update of the fan-out's flags; a missing state is left missing."""

    @abstractmethod
    async def delete_sync_state(self, user_id: str, owner: Optional[str] = None) -> None:
        """Remove the state (end of a purge), fenced by `owner` (LeaseLost); the lease goes with it. A missing state
        is a no-op."""

    @abstractmethod
    async def acquire_lease(self, user_id: str, owner: str, now: datetime, until: datetime, *,
                            create: bool) -> Optional[DriveSyncState]:
        """Transactional: take the lease if free or expired and return the fresh state; None if busy.
        With create=False and no state stored, returns None without writing."""

    @abstractmethod
    async def release_lease(self, user_id: str, owner: str) -> None:
        """Clear the lease if `owner` still holds it; a missing state is a no-op."""

    @abstractmethod
    async def list_sync_user_ids(self) -> List[str]:
        """Users that have a sync state (the fan-out visits them even with indexing off, §7.1)."""

    # -- records -----------------------------------------------------------------------------

    @abstractmethod
    async def get_records(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        """The records that exist among `item_ids`, keyed by item id."""

    @abstractmethod
    async def upsert_records(self, records: Sequence[DriveIndexRecord],
                             card_vectors: Optional[Mapping[str, Sequence[float]]] = None) -> None:
        """Write whole records. A vector given for an item is stored as its card vector; a record
        with has_card=False loses its vector; otherwise the stored vector is kept."""

    @abstractmethod
    async def delete_records(self, user_id: str, item_ids: Sequence[str]) -> None:
        """Delete records (and their card vectors)."""

    @abstractmethod
    async def due_records(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with due_at <= now, ordered by (priority, due_at, item_id) (§7.8)."""

    @abstractmethod
    async def oldest_due_at(self, user_id: str, priority: int) -> Optional[datetime]:
        """The earliest due_at among records of one priority class, or None."""

    @abstractmethod
    async def children(self, user_id: str, parent_id: str, after_item_id: Optional[str],
                       limit: int) -> List[DriveIndexRecord]:
        """Direct children ordered by item_id, starting after `after_item_id`."""

    @abstractmethod
    async def records_page(self, user_id: str, after_item_id: Optional[str], limit: int) -> List[DriveIndexRecord]:
        """All records ordered by item_id, starting after `after_item_id` (upgrade pass)."""

    @abstractmethod
    async def records_seen_before(self, user_id: str, epoch: int, limit: int) -> List[DriveIndexRecord]:
        """Records with seen_epoch < epoch (the sweep, §7.5)."""

    @abstractmethod
    async def count_carded(self, user_id: str) -> int:
        """Records that carry a card vector (§5.4)."""

    @abstractmethod
    async def lowest_ranked_carded(self, user_id: str) -> Optional[DriveIndexRecord]:
        """The carded, non-deleting record with the highest card_rank, oldest modified_at first (eviction, §5.4)."""

    @abstractmethod
    async def card_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        """Records with `card_capped`, best card_rank first, then newest modified_at (release, §5.4)."""

    @abstractmethod
    async def count_by_state(self, user_id: str) -> Dict[str, int]:
        """Records per state value, plus "carded" (aggregation queries; Cabinet counters, §3.5, §6)."""

    @abstractmethod
    async def delete_user_records(self, user_id: str, limit: int) -> int:
        """Delete up to `limit` of the user's records; returns how many were deleted (purge)."""

    # -- search ------------------------------------------------------------------------------

    @abstractmethod
    async def find_nearest_cards(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveIndexRecord, float]]:
        """Card kNN under the user (+ kind) pre-filter; (record, cosine distance), nearest first."""

    @abstractmethod
    async def find_by_name_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                kind: Optional[str] = None) -> List[DriveIndexRecord]:
        """Records whose name_tokens contain any of `keys` (at most 30)."""

    @abstractmethod
    async def filter_records(self, user_id: str, kind: Optional[str], date_from: Optional[datetime],
                             date_to: Optional[datetime], limit: int) -> List[DriveIndexRecord]:
        """Filter-only query on kind and effective_date, newest effective_date first (§10.1)."""

    @abstractmethod
    async def card_vectors(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, List[float]]:
        """Stored card vectors of the given records (folder-scoped ranking in memory)."""
