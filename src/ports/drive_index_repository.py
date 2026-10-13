"""
DriveIndexRepository — storage of the drive index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5).

Records, their chunks and the per-user sync state: records and chunks change together, so one repository.
Batching and transactions are the adapter's business. Card and chunk vectors travel with writes but never inside
the domain objects. Chunk reads never apply the read fence (§7.4): the search service checks each chunk's
generation against its record's `indexed_generation`.
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
from datetime import date, datetime
from typing import AbstractSet, Dict, List, Mapping, Optional, Sequence, Tuple

from src.domain.drive_enrichment import DriveHintDoc, InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, RecordState
from src.domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean

SEARCH_V2_PENDING = "Search v2 P2"


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
    async def has_record_in(self, user_id: str, state: RecordState, priority: int) -> bool:
        """Whether any record is in `state` at priority class `priority` (one limit-1 equality query)."""

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
    async def chunk_capped(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        """Records refused by the chunk cap (`metadata_only` / `user_chunk_cap`), best card_rank first, then newest
        modified_at (release, §5.4)."""

    @abstractmethod
    async def count_unread(self, user_id: str, kinds: AbstractSet[str],
                           triaged: AbstractSet[str] = frozenset(), *, pending: Optional[bool] = None) -> int:
        """Documents of a kind in `kinds`, and images of a kind in `triaged` whose `triage_verdict` is `text`, whose
        content is not searchable (§10.2 `unread`, drive_search.is_unread): not folders, and `pending` without an
        `indexed_generation`, or `failed`, or `metadata_only` for too_large, no_text, machine_text or user_chunk_cap
        (count aggregation queries). A kind in both lists counts as a document. `pending=True` counts only the
        documents not read yet (drive_search.is_not_read_yet: pending, not `card_capped`), `pending=False` only those
        that could not be read (the other classes); None counts every class (final review F5-2)."""

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

    # -- chunks (Slice 2, §3.3, §7.4, §24.6) ---------------------------------------------------

    @abstractmethod
    async def write_chunks(self, chunks: Sequence[DriveChunk], vectors: Sequence[Sequence[float]]) -> None:
        """Write chunks with their vectors (`vectors[i]` belongs to `chunks[i]`; the lengths must match). A chunk
        with the same key `(user_id, item_id, generation, index)` is replaced."""

    @abstractmethod
    async def delete_chunks_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        """Delete the item's chunks of every generation but `keep_generation` (None: all of them); returns how
        many were deleted (collection after a commit, a demotion or a deletion, §7.4, §7.6)."""

    @abstractmethod
    async def find_nearest_chunks(self, user_id: str, vector: Sequence[float], limit: int,
                                  kind: Optional[str] = None) -> List[Tuple[DriveChunk, float]]:
        """Chunk kNN under the user (+ kind) pre-filter; (chunk, cosine distance), nearest first. Not fenced.
        `text` and `content_terms` may be empty: windows are read with `get_chunks`."""

    @abstractmethod
    async def find_chunks_by_term(self, user_id: str, term: str, limit: int,
                                  kind: Optional[str] = None) -> List[DriveChunk]:
        """Chunks whose `content_terms` contain `term` (one array-contains query), ordered by
        (item_id, generation, index). Not fenced. `text` and `content_terms` may be empty: windows are read with
        `get_chunks`."""

    @abstractmethod
    async def find_next_file_with_term(self, user_id: str, term: str, after_item_id: str,
                                       kind: Optional[str] = None) -> Optional[DriveChunk]:
        """The first chunk, in (item_id, generation, index) order, whose `content_terms` contain `term` and whose
        item_id sorts after `after_item_id` — past EVERY chunk of that item, so each call names a file not seen
        before; None when no later file holds the term. One array-contains query with a cursor on item_id, limit 1.
        Not fenced; `text` and `content_terms` may be empty. Counts the files a term spans past a full term page
        (§24.6 selectivity, arbiter R2 S-M1)."""

    @abstractmethod
    async def get_chunks(self, user_id: str, item_id: str, generation: int,
                         indexes: Sequence[int]) -> List[DriveChunk]:
        """The chunks among `indexes` of one generation that exist, by index (a hit's neighbour window, §10.2)."""

    @abstractmethod
    async def get_chunks_with_vectors(self, user_id: str, item_id: str, generation: int, indexes: Sequence[int], *,
                                      text: bool = True, terms: bool = True,
                                      vectors: bool = True) -> List[Tuple[DriveChunk, List[float]]]:
        """`get_chunks` with each chunk's vector, by index (folder-scoped chunk ranking in memory, §10.1). A field
        not asked for is not read: `text` / `content_terms` come back empty, and the vector as [] (the ranking
        reads only what it ranks with)."""

    @abstractmethod
    async def readable_chunk_total(self, user_id: str) -> int:
        """Σ chunk_count over the user's records with an `indexed_generation` (the chunk cap, §5.4)."""

    @abstractmethod
    async def chunks_page(self, user_id: str, after: Optional[Tuple[str, int, int]],
                          limit: int) -> List[DriveChunk]:
        """The user's chunks ordered by (item_id, generation, index), starting after the key `after` (the sweep's
        chunk backstop, §7.5)."""

    @abstractmethod
    async def delete_user_chunks(self, user_id: str, limit: int) -> int:
        """Delete up to `limit` of the user's chunks; returns how many were deleted (purge)."""

    # -- Search v2 (§25.13.1) -----------------------------------------------------------------------------------
    # Frozen signatures, implemented by P2 in the Firestore and in-memory adapters (with parity tests on order,
    # limits and cursors). Until then each raises NotImplementedError; nothing calls them. Not abstract, so both
    # adapters stay untouched until P2 (precedent: PromptContentStore.flush, UserRepository.add_platform_id).

    async def find_by_about_keys(self, user_id: str, keys: Sequence[str], limit: int, *, kind: Optional[str] = None,
                                 date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                                 after: Optional[str] = None) -> List[DriveRecordLean]:
        """Records whose `about_keys` contain any of `keys` (at most 30), newest `effective_date` first; a single
        `kind` through its composite, a date range in the query; the cursor `after` is an item id (§25.5.2)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def find_expiring(self, user_id: str, keys: Sequence[str], after_date: date,
                            limit: int) -> List[DriveRecordLean]:
        """Records whose `about_keys` contain any of `keys`, with `valid_until` from `after_date`, soonest first
        (§25.5.4 validity words)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def find_chunks_by_word_keys(self, user_id: str, keys: Sequence[str], limit: int,
                                       after_item_id: Optional[str] = None) -> List[DriveChunkLean]:
        """Chunks whose `word_keys` contain any of `keys`, ordered by (item_id, generation, index), after the item
        `after_item_id`. Not fenced (§25.5.2)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def get_chunk_keys(self, user_id: str, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        """The `word_keys` of the chunks among `ids` that exist, read by id (candidate coverage, §25.5.2)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def find_hints_by_keys(self, user_id: str, keys: Sequence[str], limit: int) -> List[DriveHintLean]:
        """Hint documents whose `hint_keys` contain any of `keys`. Not fenced (§25.5.2)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def find_nearest_hints(self, user_id: str, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveHintLean, float]]:
        """Hint kNN under the user (+ kind) pre-filter; (hint, cosine distance), nearest first. Not fenced."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def get_hints(self, user_id: str, item_id: str, generation: int) -> List[DriveHintDoc]:
        """Every hint document of one generation of an item, by index (windows, hint reuse)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def write_hints(self, docs: Sequence[DriveHintDoc], vectors: Sequence[Sequence[float]]) -> None:
        """Write hint documents with their `hint_vector` (`vectors[i]` belongs to `docs[i]`), in byte-sized batches
        (§25.2.6). A document with the same key is replaced."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def delete_hints_except(self, user_id: str, item_id: str, keep_generation: Optional[int]) -> int:
        """Delete the item's hint documents of every generation but `keep_generation` (None: all of them); returns
        how many were deleted. Runs before `delete_chunks_except` (§25.2.2)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def delete_user_hints(self, user_id: str, limit: int) -> int:
        """Delete up to `limit` of the user's hint documents; returns how many (purge, first; rollback)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def hints_page(self, user_id: str, after: Optional[Tuple[str, int, int]],
                         limit: int) -> List[DriveHintLean]:
        """The user's hint documents ordered by (item_id, generation, index), after the key `after` (the sweep)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def enrich_queue(self, user_id: str, limit: int) -> List[DriveIndexRecord]:
        """Records with `enrich_ready`, `effective_day` newest first, then `parent_id`, then `item_id` (§25.4.5)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def enrich_wakeups(self, user_id: str, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with `enrich_wake_at <= now` (§25.4.5)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def count_by_job_state(self, user_id: str, kinds: AbstractSet[str]) -> Dict[str, Dict[str, int]]:
        """Records of a kind in `kinds` counted per job and state: job ("profile", "passages") -> state -> count
        (the coverage line, §25.5.6)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def keys_pending_page(self, user_id: str, after: Optional[str], limit: int) -> List[DriveIndexRecord]:
        """Records with `keys_pending`, ordered by item_id, after the item `after` (§25.4.5)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def get_records_lean(self, user_id: str, item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        """The lean projections of the records that exist among `item_ids`, keyed by item id (§25.13.3)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def get_model_usage(self, user_id: str, month: str) -> ModelUsageMonth:
        """The user's usage document for `month` ("YYYY-MM"); zeros when none is stored (§25.4.5)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def add_model_usage(self, user_id: str, month: str, day: str, cls: str, usd: float, fallback_usd: float,
                              calls: int) -> None:
        """Add to the month's `<cls>_usd`, `fallback_usd`, `calls` and the day's totals (atomic increments)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def get_initial_build(self, user_id: str) -> InitialBuild:
        """The user's initial-build document; a new one with the allowance when none is stored (D27)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def spend_initial_build(self, user_id: str, usd: float, now: datetime) -> InitialBuild:
        """Add `usd` to `spent_usd` (opening it at `now` if new) and return the result (D27)."""
        raise NotImplementedError(SEARCH_V2_PENDING)

    async def close_initial_build(self, user_id: str, now: datetime) -> None:
        """Close the initial build at `now`; once closed it never reopens (D27)."""
        raise NotImplementedError(SEARCH_V2_PENDING)
