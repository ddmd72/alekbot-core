"""
DriveIndexRepository — storage of the drive index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5).

Records, their chunks and the per-connection sync state: records and chunks change together, so one repository.
Batching and transactions are the adapter's business. Card and chunk vectors travel with writes but never inside
the domain objects. Chunk reads never apply the read fence (§7.4): the search service checks each chunk's
generation against its record's `indexed_generation`.

Connection scope (MULTI_DRIVE_RFC §8, E8, I3): the sync state and lease, records, chunks, hints, transcript jobs (with
their window results) and batches belong to one connection. Every method of theirs takes the `ConnectionScope` where it
took the user id; documents are keyed `{connection_id}:…` (`record_doc_id`, `chunk_doc_id`, `sync_state_doc_id`,
`transcript_job_doc_id`, `transcript_batch_doc_id`) and carry both `user_id` and `connection_id`; queries filter
`connection_id`. A stored document whose `user_id` differs from the scope's is treated as absent and logged at ERROR
(I3). A write takes the connection from the object written (`DriveIndexRecord.connection_id`, …): an empty one is a
ValueError, before any write. User-scoped by design (T-5): the monthly model usage (`{user}:{YYYY-MM}`, D8), the
initial build (by provider account, `initial_build_doc_id`) and the transcript store (by content key, §10; shared by the
user's connections, so `transcript_key_in_use` and the orphan sweep look at every connection's records).
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
from typing import AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_cap_alert import CapCounts
from src.domain.drive_enrichment import DriveHintDoc, InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, RecordState
from src.domain.reconnect_policy import Cleared
from src.domain.drive_index_lean import ChunkId, DriveChunkLean, DriveHintLean, DriveRecordLean
from src.domain.drive_transcript import StoredTranscript, TranscriptBatch, TranscriptJob, WindowTranscript


class DriveIndexRepository(ABC):

    # -- sync state --------------------------------------------------------------------------

    @abstractmethod
    async def get_sync_state(self, scope: ConnectionScope) -> Optional[DriveSyncState]:
        """The connection's sync state, or None if nothing was ever stored (or it is another user's, I3)."""

    @abstractmethod
    async def save_sync_state(self, state: DriveSyncState, owner: Optional[str] = None) -> None:
        """Write the whole state except the fan-out flags and the lease, fenced by `owner` (LeaseLost). Keyed by
        `state.connection_id` (ValueError when empty)."""

    @abstractmethod
    async def create_sync_state_if_absent(self, state: DriveSyncState) -> bool:
        """Store `state` only if the connection has none (an atomic create); True when it was written. The fan-out's
        minimal state, so staleness covers a connection whose ticks never land (§12.1)."""

    @abstractmethod
    async def update_sync_flags(self, scope: ConnectionScope, *, stale_alerted: Optional[bool] = None,
                                reconnect_notified: Optional[bool] = None, migration_alerted: Optional[bool] = None,
                                stall_alerted: Optional[bool] = None,
                                fallback_alerted_day: Optional[str] = None,
                                card_cap_alerted: Optional[bool] = None,
                                chunk_cap_alerted: Optional[bool] = None,
                                disconnected_by_user_at: Union[datetime, Cleared, None] = None) -> None:
        """Field-level update of the fan-out's flags (the v1 two, Search v2's three alert flags, §25.8, the two cap
        alert flags, drive_cap_alert, and R1's deliberate-disconnect mark, SECRETS_AT_REST_RFC §6.2 F1); a missing state
        is left missing. `save_sync_state` never writes them. None leaves a flag as stored; `Cleared.MARK` empties
        `disconnected_by_user_at`."""

    @abstractmethod
    async def delete_sync_state(self, scope: ConnectionScope, owner: Optional[str] = None) -> None:
        """Remove the state (end of a purge), fenced by `owner` (LeaseLost); the lease goes with it. A missing state
        is a no-op."""

    @abstractmethod
    async def acquire_lease(self, scope: ConnectionScope, owner: str, now: datetime, until: datetime, *,
                            create: bool) -> Optional[DriveSyncState]:
        """Transactional: take the lease if free or expired and return the fresh state; None if busy (or the stored
        state is another user's, I3). With create=False and no state stored, returns None without writing; a created
        state carries the scope's `user_id` and `connection_id`."""

    @abstractmethod
    async def release_lease(self, scope: ConnectionScope, owner: str) -> None:
        """Clear the lease if `owner` still holds it; a missing state is a no-op."""

    @abstractmethod
    async def list_sync_scopes(self) -> List[ConnectionScope]:
        """The connections that have a sync state, by connection id (the fan-out visits them even with indexing off
        or the connection gone, §7.1, so a purge always runs)."""

    # -- records -----------------------------------------------------------------------------

    @abstractmethod
    async def get_records(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        """The records that exist among `item_ids`, keyed by item id."""

    @abstractmethod
    async def records_by_identity(self, scope: ConnectionScope,
                                  identities: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        """The connection's records whose item `identity` is among `identities`, keyed by identity (the owner notes'
        binding by identity, MULTI_DRIVE_RFC §7.2). Any number of identities: the adapter batches its queries. An
        identity no record carries is absent."""

    @abstractmethod
    async def upsert_records(self, records: Sequence[DriveIndexRecord],
                             card_vectors: Optional[Mapping[str, Sequence[float]]] = None) -> None:
        """Write whole records. A vector given for an item is stored as its card vector; a record
        with has_card=False loses its vector; otherwise the stored vector is kept. The fan-out's
        `inaccessible_notified_since` is never written here: the stored value is kept (§26.2, Δ54)."""

    @abstractmethod
    async def delete_records(self, scope: ConnectionScope, item_ids: Sequence[str]) -> None:
        """Delete records (and their card vectors)."""

    @abstractmethod
    async def due_records(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with due_at <= now, ordered by (priority, due_at, item_id) (§7.8)."""

    @abstractmethod
    async def oldest_due_at(self, scope: ConnectionScope, priority: int) -> Optional[datetime]:
        """The earliest due_at among records of one priority class, or None."""

    @abstractmethod
    async def has_record_in(self, scope: ConnectionScope, state: RecordState, priority: int) -> bool:
        """Whether any record is in `state` at priority class `priority` (one limit-1 equality query)."""

    @abstractmethod
    async def children(self, scope: ConnectionScope, parent_id: str, after_item_id: Optional[str],
                       limit: int) -> List[DriveIndexRecord]:
        """Direct children ordered by item_id, starting after `after_item_id`."""

    @abstractmethod
    async def records_page(self, scope: ConnectionScope, after_item_id: Optional[str], limit: int) -> List[DriveIndexRecord]:
        """All records ordered by item_id, starting after `after_item_id` (upgrade pass)."""

    @abstractmethod
    async def records_seen_before(self, scope: ConnectionScope, epoch: int, limit: int) -> List[DriveIndexRecord]:
        """Records with seen_epoch < epoch (the sweep, §7.5)."""

    @abstractmethod
    async def count_carded(self, scope: ConnectionScope) -> int:
        """Records that carry a card vector (§5.4)."""

    @abstractmethod
    async def lowest_ranked_carded(self, scope: ConnectionScope) -> Optional[DriveIndexRecord]:
        """The carded, non-deleting record with the highest card_rank, oldest modified_at first (eviction, §5.4)."""

    @abstractmethod
    async def card_capped(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        """Records with `card_capped`, best card_rank first, then newest modified_at (release, §5.4)."""

    @abstractmethod
    async def chunk_capped(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        """Records refused by the chunk cap (`metadata_only` / `user_chunk_cap`), best card_rank first, then newest
        modified_at (release, §5.4)."""

    @abstractmethod
    async def count_capped(self, scope: ConnectionScope) -> CapCounts:
        """Records held by each per-user cap (§5.4): `card_capped`, and `metadata_only` / `user_chunk_cap`. Count
        aggregations with equality filters only, never a scan (the fan-out's cap alert, drive_cap_alert)."""

    @abstractmethod
    async def count_unread(self, scope: ConnectionScope, kinds: AbstractSet[str],
                           triaged: AbstractSet[str] = frozenset(), *, pending: Optional[bool] = None,
                           transcribed: AbstractSet[str] = frozenset()) -> int:
        """Documents of a kind in `kinds`, and images of a kind in `triaged` whose `triage_verdict` is `text`, whose
        content is not searchable (§10.2 `unread`, drive_search.is_unread): not folders, and `pending` without an
        `indexed_generation`, or `failed`, or `metadata_only` for too_large, no_text, machine_text or user_chunk_cap
        (count aggregation queries). A kind in both lists counts as a document. `pending=True` counts only the
        documents not read yet (drive_search.is_not_read_yet: pending, not `card_capped`), `pending=False` only those
        that could not be read (the other classes); None counts every class (final review F5-2). `transcribed`: recordings
        counted like documents except that `no_text` is not an unread reason for them (a silent recording is a stub, not
        unreadable, §27.11, G20); empty by default, so every other caller is unchanged."""

    @abstractmethod
    async def inaccessible_records(self, scope: ConnectionScope, limit: int, *, lean: bool = False) -> List[DriveIndexRecord]:
        """Up to `limit` records the bot cannot open (`inaccessible_since` set, §26.2), oldest marking first, ties by
        item id, with `inaccessible_notified_since` (the tick's counters and the fan-out's notice, Δ56, Δ53). `lean`:
        only the fields the tick's counters and parent map read (an implementation may return more); the other fields
        take their defaults."""

    @abstractmethod
    async def set_inaccessible_notified(self, scope: ConnectionScope, values: Mapping[str, Optional[datetime]]) -> None:
        """Field-level write of `inaccessible_notified_since` per item id (the fan-out only, Δ54); a record that is gone
        is skipped, never re-created. Every other record write leaves this field as it is stored."""

    @abstractmethod
    async def count_by_state(self, scope: ConnectionScope) -> Dict[str, int]:
        """Records per state value, plus "carded" (aggregation queries; Cabinet counters, §3.5, §6)."""

    @abstractmethod
    async def delete_user_records(self, scope: ConnectionScope, limit: int) -> int:
        """Delete up to `limit` of the user's records; returns how many were deleted (purge)."""

    # -- search ------------------------------------------------------------------------------

    @abstractmethod
    async def find_nearest_cards(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveIndexRecord, float]]:
        """Card kNN under the user (+ kind) pre-filter; (record, cosine distance), nearest first."""

    @abstractmethod
    async def find_by_name_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                kind: Optional[str] = None) -> List[DriveIndexRecord]:
        """Records whose name_tokens contain any of `keys` (at most 30)."""

    @abstractmethod
    async def filter_records(self, scope: ConnectionScope, kind: Optional[str], date_from: Optional[datetime],
                             date_to: Optional[datetime], limit: int) -> List[DriveIndexRecord]:
        """Filter-only query on kind and effective_date, newest effective_date first (§10.1)."""

    @abstractmethod
    async def card_vectors(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, List[float]]:
        """Stored card vectors of the given records (folder-scoped ranking in memory)."""

    # -- chunks (Slice 2, §3.3, §7.4, §24.6) ---------------------------------------------------

    @abstractmethod
    async def write_chunks(self, chunks: Sequence[DriveChunk], vectors: Sequence[Sequence[float]]) -> None:
        """Write chunks with their vectors (`vectors[i]` belongs to `chunks[i]`; the lengths must match). A chunk
        with the same key `(connection_id, item_id, generation, index)` is replaced."""

    @abstractmethod
    async def delete_chunks_except(self, scope: ConnectionScope, item_id: str, keep_generation: Optional[int]) -> int:
        """Delete the item's chunks of every generation but `keep_generation` (None: all of them); returns how
        many were deleted (collection after a commit, a demotion or a deletion, §7.4, §7.6)."""

    @abstractmethod
    async def find_nearest_chunks(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                  kind: Optional[str] = None) -> List[Tuple[DriveChunk, float]]:
        """Chunk kNN under the user (+ kind) pre-filter; (chunk, cosine distance), nearest first. Not fenced.
        `text`, `content_terms`, `word_keys` and `keys_version` may be empty: windows are read with `get_chunks`."""

    @abstractmethod
    async def find_nearest_chunks_in(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                     item_ids: Sequence[str]) -> List[Tuple[DriveChunk, float]]:
        """`find_nearest_chunks` restricted to the chunks of `item_ids` (at most KNN_ITEM_IDS_MAX distinct; more is a
        ValueError, logged, never a silent cut: the caller batches; none returns []). No kind filter: the caller
        picks the ids, kinds included (RFC Δ246). Not fenced; the same projection as `find_nearest_chunks`."""

    @abstractmethod
    async def find_chunks_by_term(self, scope: ConnectionScope, term: str, limit: int,
                                  kind: Optional[str] = None) -> List[DriveChunk]:
        """Chunks whose `content_terms` contain `term` (one array-contains query), ordered by
        (item_id, generation, index). Not fenced. `text`, `content_terms`, `word_keys` and `keys_version` may be
        empty: windows are read with `get_chunks`."""

    @abstractmethod
    async def find_next_file_with_term(self, scope: ConnectionScope, term: str, after_item_id: str,
                                       kind: Optional[str] = None) -> Optional[DriveChunk]:
        """The first chunk, in (item_id, generation, index) order, whose `content_terms` contain `term` and whose
        item_id sorts after `after_item_id` — past EVERY chunk of that item, so each call names a file not seen
        before; None when no later file holds the term. One array-contains query with a cursor on item_id, limit 1.
        Not fenced; `text` and `content_terms` may be empty. Counts the files a term spans past a full term page
        (§24.6 selectivity, arbiter R2 S-M1)."""

    @abstractmethod
    async def get_chunks(self, scope: ConnectionScope, item_id: str, generation: int,
                         indexes: Sequence[int]) -> List[DriveChunk]:
        """The chunks among `indexes` of one generation that exist, by index (a hit's neighbour window, §10.2)."""

    @abstractmethod
    async def get_chunks_with_vectors(self, scope: ConnectionScope, item_id: str, generation: int, indexes: Sequence[int], *,
                                      text: bool = True, terms: bool = True,
                                      vectors: bool = True) -> List[Tuple[DriveChunk, List[float]]]:
        """`get_chunks` with each chunk's vector, by index (folder-scoped chunk ranking in memory, §10.1). A field
        not asked for is not read: `text` / `content_terms` come back empty (`terms=False` leaves out the v2
        `word_keys` too), and the vector as [] (the ranking reads only what it ranks with)."""

    @abstractmethod
    async def readable_chunk_total(self, scope: ConnectionScope) -> int:
        """Σ chunk_count over the user's records with an `indexed_generation` (the chunk cap, §5.4)."""

    @abstractmethod
    async def chunks_page(self, scope: ConnectionScope, after: Optional[Tuple[str, int, int]],
                          limit: int) -> List[DriveChunk]:
        """The user's chunks ordered by (item_id, generation, index), starting after the key `after` (the sweep's
        chunk backstop, §7.5). Ids only: `text`, `char_start`/`char_end`, `content_terms`, `word_keys` and
        `keys_version` may come back empty (a v2 chunk carries up to 900 keys)."""

    @abstractmethod
    async def delete_user_chunks(self, scope: ConnectionScope, limit: int) -> int:
        """Delete up to `limit` of the user's chunks; returns how many were deleted (purge: after the user's hint
        documents, before the records and the sync state, §25.2.2)."""


    # -- Search v2 (§25.13.1) -----------------------------------------------------------------------------------
    # Frozen signatures, implemented by both adapters (parity tests on order, limits and cursors:
    # tests/unit/adapters/test_drive_index_v2_parity.py). Conventions:
    #   - a "lean" read projects only the DriveRecordLean / DriveChunkLean / DriveHintLean fields (§25.13.3);
    #   - `valid_until` is stored as 00:00 UTC of the printed date; a `date` argument means 00:00 UTC of that date;
    #   - an item-id cursor (`after`, `after_item_id`) starts strictly after that item; reads are never fenced;
    #   - a key list takes at most CONCEPT_KEYS_MAX (30) distinct keys, Firestore's limit for one query; more is a
    #     ValueError (logged), never a silent cut: the caller splits its lists (§25.5.1).

    @abstractmethod
    async def find_by_about_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int, *, kind: Optional[str] = None,
                                 date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
                                 after: Optional[str] = None) -> List[DriveRecordLean]:
        """Records whose `about_keys` contain any of `keys` (at most CONCEPT_KEYS_MAX distinct; more is a ValueError),
        ordered by (effective_date DESC, item_id
        DESC), records without an `effective_date` last; a single `kind` through its composite; `date_from` /
        `date_to` inclusive on `effective_date` (a record without one is then excluded). `after` is the item id of
        the previous page's last record: the page starts after that record's stored (effective_date, item_id); a
        cursor record that no longer exists ends the paging (an empty page) (§25.5.2)."""

    @abstractmethod
    async def find_expiring(self, scope: ConnectionScope, keys: Sequence[str], after_date: date,
                            limit: int) -> List[DriveRecordLean]:
        """Records whose `about_keys` contain any of `keys` (at most CONCEPT_KEYS_MAX distinct; more is a ValueError)
        and whose `valid_until` >= 00:00 UTC of `after_date`,
        ordered by (valid_until ASC, item_id ASC) (§25.5.4 validity words)."""

    @abstractmethod
    async def find_chunks_by_word_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                       after_item_id: Optional[str] = None) -> List[DriveChunkLean]:
        """Chunks whose `word_keys` contain any of `keys` (at most CONCEPT_KEYS_MAX distinct; more is a ValueError),
        ordered by (item_id, generation, index), starting past every
        chunk of the item `after_item_id` (§25.5.2)."""

    @abstractmethod
    async def get_chunk_keys(self, scope: ConnectionScope, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        """The `word_keys` of the chunks among `ids` that exist, read by id. A candidate's chunk ids come only from
        the word pages that named it (§25.5.2); there is no per-item word-key read."""

    @abstractmethod
    async def find_hints_by_keys(self, scope: ConnectionScope, keys: Sequence[str], limit: int,
                                 after_item_id: Optional[str] = None) -> List[DriveHintLean]:
        """Hint documents whose `hint_keys` contain any of `keys` (at most CONCEPT_KEYS_MAX distinct; more is a
        ValueError), ordered by (item_id, generation, index), starting
        past every hint document of the item `after_item_id` (§25.5.2, §25.5.6)."""

    @abstractmethod
    async def get_hint_keys(self, scope: ConnectionScope, ids: Sequence[ChunkId]) -> Dict[ChunkId, Tuple[str, ...]]:
        """The `hint_keys` of the hint documents among `ids` that exist, read by id (candidate coverage, without
        their items)."""

    @abstractmethod
    async def find_nearest_hints(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                 kind: Optional[str] = None) -> List[Tuple[DriveHintLean, float]]:
        """Hint kNN under the user (+ kind) pre-filter; (hint, cosine distance), nearest first."""

    @abstractmethod
    async def find_nearest_hints_in(self, scope: ConnectionScope, vector: Sequence[float], limit: int,
                                    item_ids: Sequence[str]) -> List[Tuple[DriveHintLean, float]]:
        """`find_nearest_hints` restricted to the hint documents of `item_ids` (at most KNN_ITEM_IDS_MAX distinct; more
        is a ValueError, logged; none returns []). No kind filter (RFC Δ246)."""

    @abstractmethod
    async def get_hints(self, scope: ConnectionScope, item_id: str, generation: int) -> List[DriveHintDoc]:
        """Every hint document of one generation of an item, by index (windows, hint reuse)."""

    @abstractmethod
    async def get_hints_with_vectors(self, scope: ConnectionScope, item_id: str,
                                     generation: int) -> List[Tuple[DriveHintDoc, List[float]]]:
        """`get_hints` with each document's `hint_vector`, by index: the keys pass and hint reuse copy a document into
        a new generation without re-embedding it (§25.4.7, §25.4.8)."""

    @abstractmethod
    async def write_hints(self, docs: Sequence[DriveHintDoc], vectors: Sequence[Sequence[float]]) -> None:
        """Write hint documents with their `hint_vector` (`vectors[i]` belongs to `docs[i]`; the lengths must match),
        in batches of at most WRITE_BATCH_BYTES and RECORD_WRITE_BATCH documents (§25.2.6). A document with the same
        key is replaced."""

    @abstractmethod
    async def delete_hints_except(self, scope: ConnectionScope, item_id: str, keep_generation: Optional[int]) -> int:
        """Delete the item's hint documents of every generation but `keep_generation` (None: all of them); returns
        how many were deleted. Runs before `delete_chunks_except` (§25.2.2)."""

    @abstractmethod
    async def delete_user_hints(self, scope: ConnectionScope, limit: int) -> int:
        """Delete up to `limit` of the user's hint documents; returns how many (purge, first; rollback)."""

    @abstractmethod
    async def hints_page(self, scope: ConnectionScope, after: Optional[ChunkId], limit: int) -> List[DriveHintLean]:
        """The user's hint documents ordered by (item_id, generation, index), starting after the key `after` (the
        sweep, §25.2.2)."""

    @abstractmethod
    async def enrich_queue(self, scope: ConnectionScope, limit: int, job: Optional[str] = None) -> List[DriveIndexRecord]:
        """Records with `enrich_ready`, ordered by (effective_day DESC, parent_id, item_id) (§25.4.5). With `job` (an
        EnrichJob value): only the records whose job of that name is `ready`, in the same order (one agent type's own
        queue while the other's is backed off)."""

    @abstractmethod
    async def enrich_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with `enrich_wake_at <= now`, earliest first (§25.4.5)."""

    @abstractmethod
    async def count_describe_gaps(self, scope: ConnectionScope) -> Dict[str, int]:
        """Records per non-empty derived `describe_gap` ("waiting", "held", "no_passages"): the coverage line's
        enrichment counts, stored by the tick (§25.5.6, XC-3, XC-9). A value with no record is left out."""

    @abstractmethod
    async def count_by_job_state(self, scope: ConnectionScope, kinds: AbstractSet[str]) -> Dict[str, Dict[str, int]]:
        """Records of a kind in `kinds` counted per job and state: EnrichJob value -> JobState value -> count (the
        coverage line, §25.5.6)."""

    @abstractmethod
    async def keys_pending_page(self, scope: ConnectionScope, after: Optional[str], limit: int) -> List[DriveIndexRecord]:
        """Records with `keys_pending`, ordered by item_id, starting after the item `after` (§25.4.5)."""

    @abstractmethod
    async def get_records_lean(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        """The lean projections of the records that exist among `item_ids`, keyed by item id (§25.13.3)."""

    @abstractmethod
    async def get_model_usage(self, user_id: str, month: str) -> ModelUsageMonth:
        """The user's usage document for `month` ("YYYY-MM"); `ModelUsageMonth()` when none is stored (§25.4.5)."""

    @abstractmethod
    async def add_model_usage(self, user_id: str, month: str, day: str, cls: str, usd: float, fallback_usd: float,
                              calls: int, covered_usd: float = 0.0) -> None:
        """Atomically add `usd` to the month's `<cls>_usd` (`cls` a UsageClass value), `fallback_usd` and `calls`
        to theirs, `covered_usd` (the share of `usd` the initial build paid) to `<cls>_initial_usd`, and
        (usd, fallback_usd) to `daily[day]` ("YYYY-MM-DD"; `day == ""` writes no daily entry). Not idempotent: a call that fails after
        its write committed (an ambiguous timeout, DriveIndexStoreUnavailable) may have added, so a caller must not
        retry it blindly; a retry over-counts (the safe direction: a cap closes early)."""

    @abstractmethod
    async def reserve_model_usd(self, user_id: str, month: str, cls: str, delta_usd: float) -> None:
        """Atomically add `delta_usd` (negative releases) to the `<cls>_reserved_usd` of `month`'s usage document
        ("YYYY-MM", the RESERVATION's month, G9): the dollars a batch may still cost, counted by the hard cap beside
        what was collected (Δ106). Server-side increment, so concurrent reservations are never lost, and no
        cross-document transaction. Only classes bought ahead (`transcribe`) have the field; any other is a
        ValueError, before any write.

        Not idempotent, and the failure directions differ from `add_model_usage`: a retried reservation over-counts
        (safe, the cap closes early), but a retried RELEASE (negative delta) under-counts the open reservations, can
        take them below 0 and lift the available dollars above the cap (breaks Δ106). A release is issued exactly
        once, behind the batch/job document's state change, and never retried blindly; readers clamp the field at
        max(0, reserved)."""

    @abstractmethod
    async def get_initial_build(self, user_id: str, realm: str, subject: str) -> InitialBuild:
        """The initial-build document of the user's provider account `(realm, subject)` (`initial_build_doc_id`,
        MULTI_DRIVE_RFC §10, D8: one build per account, so a reconnect finds the same record); when none is stored, an
        unopened one with INITIAL_BUILD_USD as its allowance (D27). Reading never writes."""

    @abstractmethod
    async def spend_initial_build(self, user_id: str, realm: str, subject: str, usd: float,
                                  now: datetime) -> Tuple[InitialBuild, float]:
        """Transactional: cover as much of `usd` as the allowance has left, opening the build at `now` if new and
        closing it at `now` when the allowance is spent; returns (the build after, the dollars covered). The rest of
        `usd` belongs to the monthly class. A closed build covers 0 and writes nothing (D27). Not idempotent, like
        `add_model_usage`: a caller must not retry it blindly after a store error."""

    @abstractmethod
    async def close_initial_build(self, user_id: str, realm: str, subject: str, now: datetime) -> None:
        """Close the initial build at `now`; an already closed build keeps its `closed_at`. It never reopens (D27)."""

    # -- transcripts (§27.3-§27.5, §27.8 b): jobs, window results, batches, the store ---------------------------------
    # No document holds a download URL or a provider file URI: the job keeps the file NAME (`files/<id>`).

    @abstractmethod
    async def get_transcript_job(self, scope: ConnectionScope, item_id: str) -> Optional[TranscriptJob]:
        """The job document of a media file; None when there is none."""

    @abstractmethod
    async def save_transcript_job(self, job: TranscriptJob) -> None:
        """Replace the job document (windows included) with `job`. Window RESULTS are not part of it
        (`save_window_result`), and a save never touches them."""

    @abstractmethod
    async def delete_transcript_job(self, scope: ConnectionScope, item_id: str) -> None:
        """Delete the job document and its window results; missing is a no-op."""

    @abstractmethod
    async def save_window_result(self, scope: ConnectionScope, item_id: str, index: int, result: WindowTranscript) -> None:
        """Store window `index`'s parsed answer in the job's `windows/{index}` subcollection (G11); replaces."""

    @abstractmethod
    async def get_window_results(self, scope: ConnectionScope, item_id: str) -> Dict[int, WindowTranscript]:
        """Every stored window result of the job, by window index."""

    @abstractmethod
    async def create_batch(self, batch: TranscriptBatch) -> bool:
        """Write a new batch document keyed by (connection, provider batch name); False, writing nothing, when one
        exists."""

    @abstractmethod
    async def save_batch(self, batch: TranscriptBatch) -> None:
        """Replace the batch document. A closed batch (done, failed) is saved with `next_poll_at=None`."""

    @abstractmethod
    async def delete_batch(self, scope: ConnectionScope, name: str) -> None:
        """Delete the batch document; missing is a no-op."""

    @abstractmethod
    async def get_open_batches(self, scope: ConnectionScope, now: datetime, limit: int) -> List[TranscriptBatch]:
        """Batches due for a poll: `next_poll_at <= now` (an open batch has one, a closed batch does not), earliest
        `next_poll_at` first (§27.5)."""

    @abstractmethod
    async def get_transcript(self, user_id: str, content_key: str) -> Optional[StoredTranscript]:
        """The stored transcript under the content key, shards reassembled; None when absent (or when a shard the
        head names is missing, which is logged)."""

    @abstractmethod
    async def put_transcript(self, user_id: str, stored: StoredTranscript) -> None:
        """Store the transcript under its content key. Above TRANSCRIPT_DOC_BYTES the segments go into `parts/{n}`
        documents, written first; the head document is written last, so a head always names parts that exist."""

    @abstractmethod
    async def touch_transcript(self, user_id: str, content_key: str, last_used_at: datetime) -> None:
        """Set `last_used_at` on the head document only; a missing transcript is a no-op (nothing is created)."""

    @abstractmethod
    async def retain_transcript(self, user_id: str, content_key: str, at: datetime, reason: str) -> bool:
        """I8 (MULTI_DRIVE_RFC §10, E23): set `retained_at` (and `retained_reason`) on the head document only, before a
        disconnect purges the connection whose record carried it; an existing mark keeps its earlier time. A missing
        transcript is a no-op (nothing is created). True when a head was found."""

    @abstractmethod
    async def transcript_retained(self, user_id: str, content_key: str) -> bool:
        """True when the head document exists and carries `retained_at` (I8): the orphan sweep never deletes it. Reads
        the head only, never the parts."""

    @abstractmethod
    async def delete_transcript(self, user_id: str, content_key: str) -> None:
        """Delete the transcript's head and its parts; missing is a no-op."""

    # -- transcripts: queues on the records, copies in flight, orphans, purge (§27.3, §27.8 b, G12) ------------------

    @abstractmethod
    async def transcript_queue(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        """Records with `transcript_state == "ready"`, ordered by (effective_day DESC, parent_id, item_id): the
        `enrich_queue` order (§27.3)."""

    @abstractmethod
    async def transcript_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with `transcript_wake_at <= now`, earliest first (then item_id)."""

    @abstractmethod
    async def transcript_records(self, scope: ConnectionScope, state: str, limit: int,
                                 after: Optional[str] = None) -> List[DriveIndexRecord]:
        """Records with `transcript_state == state`, by item_id: the stage's in-flight jobs (`working`: extraction to
        resume, a mid-job park woken by the plan) and copies waiting on another file (`waiting`), which neither
        `transcript_queue` (ready only) nor `transcript_wakeups` (the wake-up is cleared) returns. Equality filters
        only: single-field indexes serve it. `after` is a start-after cursor (an item id): only records past it are
        returned (the mirror's seed pass, §27.9.4); the default is unchanged."""

    @abstractmethod
    async def jobs_by_content_key(self, scope: ConnectionScope, content_key: str) -> List[TranscriptJob]:
        """The connection's job documents whose `content_key` is the key, by item_id: the in-flight copy check (G12).
        The key is matched as a field value, never through a document id."""

    @abstractmethod
    async def orphan_transcripts(self, user_id: str, before: datetime, limit: int) -> List[str]:
        """Content keys of the user's stored transcripts with `last_used_at < before`, least recently used first. A head with
        `last_used_at=None` is not returned (callers always set it)."""

    @abstractmethod
    async def transcript_key_in_use(self, user_id: str, content_key: str) -> bool:
        """True when a record of the user, on any of the user's connections (the store is user-scoped, §10), carries
        `transcript_key == content_key`, in any transcript state: the orphan sweep keeps such a stored transcript (a
        re-finalize, G18/G31, reads it with no model call, Δ122). Equality filters only: single-field indexes serve it."""

    @abstractmethod
    async def delete_user_transcript_jobs(self, scope: ConnectionScope, limit: int) -> int:
        """Purge: delete up to `limit` of the connection's job documents with their window results. Returns the
        number of jobs deleted; call until 0. Filters on `connection_id` only."""

    @abstractmethod
    async def delete_user_batches(self, scope: ConnectionScope, limit: int) -> int:
        """Purge: delete up to `limit` of the connection's batch documents. Returns the number deleted; call until 0."""

    @abstractmethod
    async def delete_user_transcripts(self, user_id: str, limit: int) -> int:
        """Purge: delete up to `limit` of the user's stored transcripts with their parts (any token). Returns the
        number of transcripts deleted; call until 0."""

    @abstractmethod
    async def delete_user_transcripts_except(self, user_id: str, limit: int, keep: FrozenSet[str],
                                             after: Optional[str] = None) -> Tuple[int, Optional[str]]:
        """Purge (Δ186, Δ214): page the user's stored transcript heads by document id from `after` (None: the start), at
        most `limit` heads per call, and delete the ones whose content key is not in `keep`, with their parts. Returns
        `(deleted, next_cursor)`: pass `next_cursor` back as `after`; it is None only at the end (a page made only of kept
        heads deletes nothing but is not the end). `delete_user_transcripts` stays for its callers."""

    # -- the transcript mirror's queue (§27.9.4, Δ187, Δ217) ----------------------------------------------

    @abstractmethod
    async def mirror_queue(self, scope: ConnectionScope, limit: int) -> List[DriveIndexRecord]:
        """Records with `mirror_due == True`, newest `effective_day` first, then item_id."""

    @abstractmethod
    async def mirror_wakeups(self, scope: ConnectionScope, now: datetime, limit: int) -> List[DriveIndexRecord]:
        """Records with `mirror_retry_at <= now` (a parked record whose hold is ripe), earliest first, then item_id."""

    @abstractmethod
    async def delete_user_transcript_orphans(self, scope: ConnectionScope, limit: int, *, keep_transcripts: bool) -> int:
        """Purge: delete up to `limit` of the connection's window documents (by `connection_id`) and, unless
        `keep_transcripts`, the USER's part documents (by `user_id`: the store is user-scoped), through collection-group
        sweeps that need their composite indexes. It deletes ALL of those documents, live ones included: it cannot tell
        an orphan from a live document, so `keep_transcripts=False` is for a purge that deleted the user's whole store
        (no other connection reads it). It may run only after all three `delete_user_*` helpers returned 0 in the same
        purge. `keep_transcripts=True` (a rebuild that keeps the store,
        `purge_keep_transcripts`) sweeps the windows group only and never touches parts; False sweeps both. A
        DriveIndexStoreUnavailable from it (a missing index) is logged as ERROR naming the index and never fails or
        holds the purge: it is retried on the next purge or daily pass. Returns the number deleted."""
