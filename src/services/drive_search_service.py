"""
DriveSearchService — find the user's drive files by name, metadata and content
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §10.1, §10.2, Search v2 §25.5).

Gate (uncached) -> folder scope -> a filter-only listing (§10.1, unchanged), or a query: the query's concepts
(`drive_keys.query_concepts`, numbers first, strict AND) are looked up in parallel streams, Firestore only and lean —
the about stream (records' `about_keys`), the word stream (chunks' `word_keys`), the hint stream (passage hints'
`hint_keys`), the name stream (name keys v2, RFC Δ17) and three kNN pages (cards, chunks, hint documents) for the
meaning score (§25.5.2). Each concept's document frequency decides its allowances (absent blocks; one low-weight word
and common words may be missed, §25.5.4); the rarest required concept's in-scope files are the candidates; each
candidate's coverage is tested on its about and name keys and the keys of the chunks and hint documents the streams
named (read by id), with the minimum window for two or more chunk-covered words. Files land in About, Mentioned in,
By translation or Labelled by the weakest source class they need; Related holds at most RELATED_MAX files by meaning
(a robust z per source over its pool, abstaining under MEANING_POOL_MIN files) or a similar name. Shown counts per
tier, the rest grouped; the coverage line names what the search could not see (its enrichment counts are the ones
the last tick stored, never counted per search). A sync state not rebuilt under v2 (no `keys` in `applied_versions`,
D30) is not searched at all: the result says the index is being rebuilt and states nothing absent (XC-1). No model
call at search time (D19, D28). Decisions that need no I/O are in `drive_tiers`,
`drive_tier_pool`, `drive_tier_explain` and `drive_search`; this class reads and calls them.

Public interface:
    DriveSearchService(repo, embeddings, users, drive, *, embedding_space, clock, nonce, monotonic, transcribe_cap_usd)
    await search(user_id, request) -> DriveSearchResult
    await excerpts(user_id, request, *, shown=frozenset()) -> ExcerptResult   # find_passages (§28.3 A4-A8, A18)
    await excerpt_range(user_id, request) -> ExcerptResult    # read_passages: one file's passages by number (§28.3 A7)
    await indexing_status(user_id) -> DriveIndexingStatus     # Cabinet (§6)
    await access_note(user_id, *, item_id=None, path=None) -> Optional[str]   # list/open on a 404 (§26.2, Δ55)
    await search_deleted(user_id, drive_query) -> str          # `drive_deleted`: archived transcripts (§27.9.11) and
                                                               # the sources added with add_deleted_source (notes)
    add_deleted_source(source)                                 # the owner-notes source (notes RFC §6.4)
    await archived_text(user_id, mirror_item_id, ...) -> Optional[str]   # opening a file of the bot's `_Transcripts`

Files the bot cannot open (§26.2, D33): their cards say so in the `index:` line (drive_search.index_line), the coverage
line counts them from the tick's `inaccessible` counter (Δ56), and `access_note` gives list and open the same sentence
when the provider answers "not found" for an item the index knows to be closed.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..domain.drive_accuracy import scorable
from ..domain.drive_card import card_text, note_line
from ..domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_HELD, GAP_NO_PASSAGES, GAP_WAITING
from ..domain.drive_enrichment import USD_CAPS, HintItem, JobState, UsageClass
from ..domain.drive_excerpt_chunk import ExcerptChunk, display_number, stored_index
from ..domain.drive_excerpt_context import ExcerptContext, file_description
from ..domain.drive_excerpt_file import ExcerptFile
from ..domain.drive_excerpt_file_context import FileContext
from ..domain.drive_excerpt_folder_note import FolderNote
from ..domain.drive_excerpt_range_request import ExcerptRangeRequest
from ..domain.drive_excerpt_request import ExcerptRequest
from ..domain.drive_excerpt_result import ExcerptResult
from ..domain.drive_excerpt_selection import Selection
from ..domain.drive_excerpts import rank_excerpts, select_excerpts
from ..domain.drive_index import (
    KEYS_VERSION, MIGRATION_GATE_KEY, POLICY_VERSION, DriveChunk, DriveIndexRecord, DriveSyncState, EpochPhase,
    Priority, RecordState, Scope, inaccessible,
)
from ..domain.drive_index_lean import ChunkId
from ..domain.drive_index_params import (
    EXCERPT_RANGE_MAX, EXCERPTS_CHARS_MAX, EXCERPTS_LEXICAL_WINDOW,
    ABOUT_FETCH, ABOUT_SHOWN, CARD_FETCH, CHUNK_FETCH, FETCH_MAX, FOLDER_INMEM_CARDS, FOLDER_INMEM_CHUNKS,
    ARCHIVE_QUERY_MAX, DELETED_SHOW_MAX, GROUP_READ_MAX, HINT_FETCH, INACCESSIBLE_SCAN_MAX, K, LABELLED_SHOWN, MAX_CARD_RECORDS_PER_USER, MENTIONED_SHOWN, NAME_FETCH,
    NAME_KEY_QUERIES, PASSAGE_MAX_CHUNKS_PER_FILE, PROFILE_MIN_CHARS, RELATED_MAX, RELATED_SMALL, TRANSLATED_SHOWN, WINDOW_CHARS_TOTAL_V2,
    TRANSCRIBE_USD_PER_MONTH, TRANSCRIPT_OPEN_MAX_CHARS, WINDOW_MAX, WORD_FETCH, WORD_FILES_MAX,
)
from ..domain.drive_index_planner import month_key
from ..domain.drive_indexing_status import DriveIndexingStatus
from ..domain.drive_keys import fetch_batches, name_match_v2, query_concepts, query_name_keys_v2
from ..domain.drive_match import Concept, KeyFamily, MatchTier, QueryConcepts
from ..domain.drive_search_line import SearchLine, search_line
from ..domain.drive_search_texts import ACCESS_NOTE_FILE, ACCESS_NOTE_FOLDER
from ..domain.drive_search import (
    INACCESSIBLE_COUNTER, MAX_CHAIN_DEPTH, REFERENCE_REMINDER, wrap_untrusted, NOT_READ_YET_COUNTER, STATUS_AMBIGUOUS_FOLDER, STATUS_FOLDER_NOT_FOUND, STATUS_OK, UNREAD_COUNTER,
    DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, content_admitted, cosine_distance,
    hides_descendants, in_date_range, kinds_for, make_card, normalize_path, path_match, record_visible,
    render_results, resolve_chain, search_gate,
)
from ..domain.drive_deleted_search import (
    DeletedHit, matches as deleted_matches, merge_hits, passage, render_deleted, transcript_line,
)
from ..domain.drive_mirror import KIND_FILE, MIRROR_ACTIVE, MirrorDoc, doc_key
from ..domain.drive_mirror_names import driver_keys
from ..domain.drive_transcript_render import card_segment, cap_plain_text, open_needs_cut, open_text
from ..domain.drive_tier_explain import explain, not_matched, passage_window
from ..domain.drive_tier_pool import (
    ConceptHits, candidate_ids, concept_trigrams, document_frequency, document_frequency_common, name_word_concepts,
    tier_order_key,
)
from ..domain.drive_tier_result import CoverageFacts, GroupMember, TierBlock, TieredResult
from ..domain.drive_tiers import (
    WHERE_HINT, Allowance, FileKeys, FileMatch, allowances, chunks_needing_text, classify,
    meaning_scores, name_query_keys, related_floor, size_band, source_scores,
)
from ..domain.user_drive import DriveConnection, DriveItem, DriveItemNotFoundError, make_drive_ref, parse_drive_ref
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY, DriveMirrorRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import exc_brief, logger

_CHILD_PAGE = 200
_WALK_PARALLEL = 16   # folders listed at once, per level of a subtree walk
_READ_PARALLEL = 16   # files whose chunk text or hint items are read at once
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_SHOWN: Mapping[MatchTier, int] = {MatchTier.ABOUT: ABOUT_SHOWN, MatchTier.MENTIONED: MENTIONED_SHOWN,
                                   MatchTier.TRANSLATED: TRANSLATED_SHOWN, MatchTier.LABELLED: LABELLED_SHOWN}
_TIERS: Tuple[MatchTier, ...] = (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED)
# Cards that show a passage when the match came from a chunk or a grounded keyword (P6 review m3: Labelled too)
_PASSAGE_TIERS = frozenset({MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED})
_VALIDITY = KeyFamily.VALIDITY_WORD.value

# A source of the deleted-files section (§27.9.11): (user, the query's concepts, the connection) -> (hits, its own
# coverage lines, e.g. "3 more archived transcripts matched."). The transcript source is built in; the owner-notes source (notes RFC §6.4) is added with
# `add_deleted_source`, without touching this one.
DeletedSource = Callable[[str, Sequence[Concept], DriveConnection], Awaitable[Tuple[List[DeletedHit], List[str]]]]
_MIRROR_CHAIN_MAX = 12   # ledger parents walked to build a mirror path

Chain = Optional[Tuple[str, Tuple[str, ...]]]   # (path, ancestor ids) or None when hidden
Rec = Any   # a DriveIndexRecord or a DriveRecordLean: both carry the fields visibility, filters and coverage read


def _not_indexed(folder_name: str) -> DriveSearchResult:
    return DriveSearchResult(status=STATUS_OK, folder_path=folder_name, folder_not_indexed=True)


def _expiry_match(match: FileMatch) -> bool:
    """The match came through validity words (a query for what expires): the card's date is the expiry date (F5)."""
    return any(c.keys and all(k.startswith(_VALIDITY) for k in c.keys) for c in match.covers)


def _filters(request: DriveSearchRequest) -> bool:
    return bool(request.folder or request.kind or request.date_from or request.date_to)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nonce() -> str:
    return secrets.token_hex(8)


def _similarity(distance: float) -> float:
    return 1.0 - distance


async def _empty() -> Dict[ChunkId, Tuple[str, ...]]:
    return {}


def _fenced(rec: Optional[Rec], generation: int) -> bool:
    """§7.4: a chunk or hint document counts only while its record names its generation as the readable one."""
    return (rec is not None and rec.state is not RecordState.DELETING and rec.indexed_generation is not None
            and rec.indexed_generation == generation)


@dataclass
class _Stats:
    """Counts after each stage, for the one summary line per search (§10.1, §25.5.7)."""
    mode: str = "query"
    scope: str = "all"
    content: str = "off"             # off | wide (user-wide streams) | memory (a folder's chunks in memory)
    concepts: int = 0
    ignored: int = 0
    common: int = 0
    absent: int = 0
    card: int = 0
    chunk: int = 0
    hint: int = 0
    name: int = 0
    name_queries: int = 0
    about_queries: int = 0
    word_queries: int = 0
    hint_queries: int = 0
    candidates_tested: int = 0
    tiers: Dict[str, int] = field(default_factory=dict)
    related: int = 0
    pool_card: str = "-"
    pool_chunk: str = "-"
    pool_hint: str = "-"
    band: int = 0
    coverage: str = "complete"
    chars: int = 0
    visible: int = 0
    filtered: int = 0
    candidates: Set[str] = field(default_factory=set)   # filter-only: distinct visible records, before the filters
    widened: bool = False
    collapsed: int = 0
    returned: int = 0


@dataclass
class _Pool:
    """Everything one query's streams found, before the tiers: records (lean or full), per-concept hits, the keys of
    named chunks and hint documents, the kNN pages and the chunk texts and hint items read for windows."""
    leans: Dict[str, Rec] = field(default_factory=dict)
    fulls: Dict[str, DriveIndexRecord] = field(default_factory=dict)
    hits: List[ConceptHits] = field(default_factory=list)
    names: Dict[str, Rec] = field(default_factory=dict)
    name_full: bool = False
    chunk_keys: Dict[ChunkId, FrozenSet[str]] = field(default_factory=dict)
    hint_keys: Dict[ChunkId, FrozenSet[str]] = field(default_factory=dict)
    hint_checked: Set[ChunkId] = field(default_factory=set)
    card_knn: List[Tuple[str, float]] = field(default_factory=list)          # (item_id, distance)
    chunk_knn: List[Tuple[ChunkId, float]] = field(default_factory=list)
    hint_knn: List[Tuple[ChunkId, float]] = field(default_factory=list)
    vectors: bool = False                                                   # the kNN streams ran
    texts: Dict[ChunkId, str] = field(default_factory=dict)
    items: Dict[ChunkId, Tuple[HintItem, ...]] = field(default_factory=dict)
    items_read: Set[Tuple[str, int]] = field(default_factory=set)

    def add(self, rec: Rec) -> bool:
        """Keep a record (always kept: a state not rebuilt under v2 is never searched, XC-1)."""
        if isinstance(rec, DriveIndexRecord):
            self.fulls[rec.item_id] = rec
            self.leans[rec.item_id] = rec
            return True
        self.leans.setdefault(rec.item_id, rec)
        return True


@dataclass(frozen=True)
class _PostFilters:
    """What the wide streams cannot read in one composite: a single kind (the about and name streams re-read it), a
    kind family, a date range (the about stream re-reads it) and a folder (never re-read)."""
    single_kind: Optional[str]
    family: bool
    dates: bool
    folder: bool


@dataclass(frozen=True)
class _ValidUntil:
    """A record dated by its expiry date for the date filter (a query for what expires)."""
    effective_date: Optional[datetime]


@dataclass
class _Scope:
    """Read-time visibility of every record the streams named, and which pass the kind, date and folder filters."""
    paths: Dict[str, str] = field(default_factory=dict)        # visible records -> path
    passing: Set[str] = field(default_factory=set)
    filtered: bool = False                                      # a kind, date or folder filter is set


@dataclass
class _Ancestry:
    """Ancestor records loaded level by level in batches, shared by every candidate of one search."""
    repo: DriveIndexRepository
    user_id: str
    root_item_id: str
    cache: Dict[str, Optional[Rec]] = field(default_factory=dict)

    def seed(self, recs: Iterable[Rec]) -> None:
        for rec in recs:
            self.cache[rec.item_id] = rec

    def _missing(self, parent_id: str) -> bool:
        return bool(parent_id) and parent_id != self.root_item_id and parent_id not in self.cache

    async def load(self, recs: Iterable[Rec]) -> None:
        frontier = {r.parent_id for r in recs if self._missing(r.parent_id)}
        for _ in range(MAX_CHAIN_DEPTH):
            if not frontier:
                return
            found = await self.repo.get_records(self.user_id, sorted(frontier))
            for item_id in frontier:
                self.cache[item_id] = found.get(item_id)
            frontier = {r.parent_id for r in found.values() if self._missing(r.parent_id)}

    def hidden_above(self, rec: DriveIndexRecord) -> bool:
        """An ancestor (loaded) hides its contents: opaque, inside an opaque subtree, or being deleted."""
        parent_id, seen = rec.parent_id, {rec.item_id}
        while parent_id and parent_id != self.root_item_id and parent_id not in seen and len(seen) <= MAX_CHAIN_DEPTH:
            parent = self.cache.get(parent_id)
            if parent is None:
                return False
            if hides_descendants(parent):
                return True
            seen.add(parent_id)
            parent_id = parent.parent_id
        return False

    def chain(self, rec: Rec, *, require_card: bool = True) -> Chain:
        return resolve_chain(rec, self.cache.get, self.root_item_id, require_card=require_card)


@dataclass(frozen=True)
class _Folder:
    record: DriveIndexRecord
    path: str


_SELECT_ROUNDS = 3        # passages: re-selections once read texts are longer than their spans (a table header)
_NO_TEXT_LISTED = 10      # passages: paths named on the no-text line (the count is the whole)


@dataclass
class _ExcerptStats:
    """Counts for the one `[DriveSearch] mode=excerpts` line (no text)."""
    mode: str = "excerpts"
    scope: str = "all"            # gate | all | files | folder-memory | folder-wide | range
    hits: int = 0                 # kNN hits, or chunk vectors read in memory, before the fence
    candidates: int = 0           # fenced and visible
    keyed: int = 0                # the nearest chunks whose keys were read for the lexical vote
    reads: int = 0                # get_chunks calls for texts


@dataclass
class _ExcerptPool:
    """One passages call's candidates: fenced, visible chunk hits with their cosine distance and span size, and the
    records and chains of their files; the in-scope files with no indexed text."""
    knn: List[Tuple[ChunkId, float]] = field(default_factory=list)
    spans: Dict[ChunkId, int] = field(default_factory=dict)
    records: Dict[str, DriveIndexRecord] = field(default_factory=dict)
    chains: Dict[str, Tuple[str, Tuple[str, ...]]] = field(default_factory=dict)
    no_text: List[str] = field(default_factory=list)
    folder_path: str = ""

    def add(self, chunk: DriveChunk, distance: float, rec: DriveIndexRecord,
            chain: Tuple[str, Tuple[str, ...]]) -> None:
        cid = (chunk.item_id, chunk.generation, chunk.index)
        self.knn.append((cid, distance))
        self.spans[cid] = max(chunk.char_end - chunk.char_start, 1)
        self.records[rec.item_id] = rec
        self.chains[rec.item_id] = chain


def _readable(rec: DriveIndexRecord) -> bool:
    return not rec.is_folder and rec.indexed_generation is not None and rec.chunk_count > 0


def _copy_hash(rec: DriveIndexRecord) -> Optional[str]:
    """The `_collapse` copy rule: a non-empty content hash that is not an empty file's."""
    digest = rec.content_hash
    return digest if digest and not digest.endswith(":0") else None




class DriveSearchService:
    def __init__(self, repo: DriveIndexRepository, embeddings: EmbeddingService, users: UserRepository,
                 drive: UserDrivePort, *, embedding_space: str, clock: Callable[[], datetime] = _utcnow,
                 nonce: Callable[[], str] = _nonce, monotonic: Callable[[], float] = time.monotonic,
                 transcribe_cap_usd: float = TRANSCRIBE_USD_PER_MONTH,
                 mirror: Optional[DriveMirrorRepository] = None) -> None:
        """`embedding_space` is the space query vectors are embedded in (the adapter's EMBEDDING_SPACE). The
        kNN streams run only while the index's cards are in it (§12.3). `transcribe_cap_usd` is the transcription cap
        the indexer applies (`effective_monthly_cap(DRIVE_TRANSCRIBE_USD_CAP)`, T-R3), shown by the Cabinet. `mirror` is
        the transcript mirror's repository (§27.9): the deleted-files search and the open of an archived transcript
        read it; without it neither has anything to say."""
        self._repo = repo
        self._embeddings = embeddings
        self._users = users
        self._drive = drive
        self._clock = clock
        self._nonce = nonce
        self._monotonic = monotonic
        self._space = embedding_space
        self._transcribe_cap = transcribe_cap_usd
        self._mirror = mirror
        self._deleted_sources: List[DeletedSource] = [self._deleted_transcripts]

    # ------------------------------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------------------------------

    async def search(self, user_id: str, request: DriveSearchRequest) -> DriveSearchResult:
        started = self._monotonic()
        stats = _Stats(mode="filter" if request.filter_only else "query")
        result = await self._search(user_id, request, stats)
        stats.returned = len(result.cards)
        if result.tiered is not None:
            stats.chars = len(render_results(result))
        ms = round((self._monotonic() - started) * 1000)
        tiers = " ".join(f"{t.value}={stats.tiers.get(t.value, 0)}" for t in _TIERS)
        logger.info(
            f"[DriveSearch] user={user_id[:8]} status={result.status} mode={stats.mode} scope={stats.scope} "
            f"content={stats.content} concepts={stats.concepts} "
            f"ignored={stats.ignored} common={stats.common} absent={stats.absent} {tiers} related={stats.related} "
            f"card={stats.card} chunk={stats.chunk} hint={stats.hint} name={stats.name} "
            f"name_queries={stats.name_queries} about_queries={stats.about_queries} "
            f"word_queries={stats.word_queries} hint_queries={stats.hint_queries} "
            f"tested={stats.candidates_tested} pool_card={stats.pool_card} pool_chunk={stats.pool_chunk} "
            f"pool_hint={stats.pool_hint} band={stats.band} coverage={stats.coverage} chars={stats.chars} "
            f"visible={stats.visible} filtered={stats.filtered} collapsed={stats.collapsed} "
            f"returned={stats.returned} building={result.envelope.building} ms={ms}")
        return result

    async def search_state(self, user_id: str) -> SearchLine:
        """The drive specialist's `search` line (DRIVE_SPECIALIST_RFC §5.3): the same flag, sync state and connection a
        search reads first, read now and in parallel; no stream runs."""
        profile, state, connection = await asyncio.gather(
            self._users.get_user(user_id), self._repo.get_sync_state(user_id), self._drive.connection(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        return search_line(enabled, state, connection, self._clock())

    async def _search(self, user_id: str, request: DriveSearchRequest, stats: _Stats) -> DriveSearchResult:
        # §10.1 step 0: the flag, the sync state and the credentials, all read now, in parallel.
        profile, state, connection = await asyncio.gather(
            self._users.get_user(user_id), self._repo.get_sync_state(user_id), self._drive.connection(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        gate = search_gate(enabled, state, connection, self._clock())
        if gate.status != STATUS_OK:
            return DriveSearchResult(status=gate.status, envelope=DriveSearchEnvelope(enabled=enabled),
                                     request=request, other_drive=gate.other_drive)
        if gate.rebuilding or state is None or not state.root_item_id:
            # Nothing stored yet, or a purge is deleting what was: say so instead of answering from it.
            envelope = DriveSearchEnvelope(building=True, reconnect_needed=gate.reconnect_needed)
            return DriveSearchResult(status=STATUS_OK, envelope=envelope, request=request,
                                     matched_total=0 if request.filter_only else None)
        if MIGRATION_GATE_KEY not in state.applied_versions:
            # D30: built by v1 and not rebuilt yet. Its records are keyed by another keyer, so no stream runs (no
            # query embedding, no reads) and nothing is stated absent or counted: every word would read "No file
            # holds" and a listing would serve v1 records (XC-1). The tick does no work on it either.
            stats.mode = "awaiting_rebuild"
            envelope = DriveSearchEnvelope(building=True, reconnect_needed=gate.reconnect_needed,
                                           index_as_of=state.observed_as_of)
            return DriveSearchResult(status=STATUS_OK, envelope=envelope, request=request, awaiting_rebuild=True,
                                     matched_total=0 if request.filter_only else None)
        use_cards = state.cards_in_space(self._space)
        envelope, result = await asyncio.gather(self._envelope(user_id, state, gate.reconnect_needed, use_cards),
                                                self._body(user_id, request, state, use_cards, stats))
        result = replace(result, envelope=envelope)
        if result.tiered is not None:
            coverage = replace(result.tiered.coverage, building=envelope.building, reading=envelope.reading,
                               refreshing=envelope.refreshing, not_read_yet=envelope.not_read_yet,
                               unread=envelope.unread,
                               names_only=result.tiered.coverage.names_only or not envelope.content_search)
            stats.coverage = "complete" if replace(coverage, inaccessible=0) == CoverageFacts() else "incomplete"
            result = replace(result, tiered=replace(result.tiered, coverage=coverage))
        return result

    async def _body(self, user_id: str, request: DriveSearchRequest, state: DriveSyncState, use_cards: bool,
                    stats: _Stats) -> DriveSearchResult:
        kinds = kinds_for(request.kind)
        ancestry = _Ancestry(self._repo, user_id, state.root_item_id)
        folder: Optional[_Folder] = None
        if request.folder:
            found = await self._resolve_folder(user_id, request.folder, ancestry)
            if isinstance(found, DriveSearchResult):
                return replace(found, request=request)
            folder = found
            if hides_descendants(folder.record):   # nothing below it is carded: an empty walk is not "no match"
                return DriveSearchResult(status=STATUS_OK, request=request, folder_path=folder.path,
                                         folder_not_indexed=True)
        subtree: Optional[List[DriveIndexRecord]] = None
        if folder is not None:
            walked, complete = await self._walk(user_id, folder.record)
            if complete:
                stats.scope = "folder-memory"
                ancestry.seed(walked)
                subtree = walked
            else:
                stats.scope = "folder-wide"
        if request.filter_only and request.name_words and not _filters(request):
            return await self._query(user_id, request, state, kinds, folder, subtree, ancestry, use_cards, stats,
                                     names_only=True)
        if request.filter_only:
            if subtree is not None:
                return await self._filter_in_memory(request, kinds, folder, subtree, ancestry, stats)
            return await self._filter_only(user_id, request, kinds, folder, ancestry, stats)
        return await self._query(user_id, request, state, kinds, folder, subtree, ancestry, use_cards, stats)

    # ------------------------------------------------------------------------------------------
    # Query (Search v2, §25.5)
    # ------------------------------------------------------------------------------------------

    async def _query(self, user_id: str, request: DriveSearchRequest, state: DriveSyncState, kinds: FrozenSet[str],
                     folder: Optional[_Folder], subtree: Optional[List[DriveIndexRecord]], ancestry: _Ancestry,
                     use_cards: bool, stats: _Stats, names_only: bool = False) -> DriveSearchResult:
        """One query (§25.5). `names_only`: the request held only generic words ("todo", "files"), searched as name
        words by the name stream alone (P6 review m6), never by content and never with a D29 line."""
        if names_only:
            q = QueryConcepts(name_word_concepts(request.query), (), ())
        else:
            q = query_concepts(request.query)
        concepts = list(q.concepts)
        stats.concepts, stats.ignored = len(concepts), len(q.ignored)
        with_content = content_admitted() and not names_only
        members: List[Optional[str]] = list(sorted(kinds)) or [None]   # a family: one kNN per member kind
        pool = _Pool(hits=[ConceptHits() for _ in concepts])
        post = _PostFilters(single_kind=members[0] if len(members) == 1 else None, family=len(members) > 1,
                            dates=request.date_from is not None or request.date_to is not None,
                            folder=folder is not None and subtree is None)
        if subtree is not None:
            carded = sum(1 for r in subtree if r.has_card) or 1
            await self._memory_pool(user_id, request.query, concepts, subtree, pool, ancestry, with_content,
                                    use_cards, members, stats)
        else:
            carded = max(int(state.counters.get("carded", 0)), 1)
            await self._wide_pool(user_id, request, concepts, members, post, pool, with_content, use_cards, stats)
        await self._records(user_id, pool)
        self._fence(pool)
        validity = self._validity_keys(concepts, pool)
        scope = await self._scope(pool, request, kinds, folder, ancestry, validity)
        stats.band = size_band(carded)

        df, full = document_frequency(pool.hits)
        in_scope = [h.files() & scope.passing for h in pool.hits]
        unchecked = [c.word for c, h, s in zip(concepts, pool.hits, in_scope)
                     if not s and h.unchecked and scope.filtered]
        if names_only:
            absent: List[str] = []
            absent_in_scope: List[str] = []
        else:
            absent = [c.word for c, n in zip(concepts, df) if n == 0 and subtree is None]
            # a scope's absence is stated only when it was read, never from a truncated page (P6 review M1)
            absent_in_scope = [c.word for c, n, s in zip(concepts, df, in_scope)
                               if c.word not in absent and c.word not in unchecked and not s
                               and (scope.filtered or subtree is not None)]
        blocked = set(absent) | set(absent_in_scope)
        stats.absent = len(blocked)
        others = [c for c in concepts if c.word not in blocked]
        df_of = {c.word: n for c, n in zip(concepts, document_frequency_common(pool.hits))}   # Δ90: no `sl:` hits
        allowance = allowances(others, df_of, carded)
        stats.common = len(allowance.common)
        tested = [i for i, c in enumerate(concepts) if c.word not in blocked]
        matches = await self._matches(user_id, pool, [concepts[i] for i in tested], [pool.hits[i] for i in tested],
                                      [in_scope[i] for i in tested], [df[i] for i in tested],
                                      [full[i] for i in tested], allowance, stats)
        page_full = any(full[i] for i in tested)
        without: Optional[int] = None
        if blocked:
            without = len(matches) if others else None
            matches = {}

        meaning = self._meaning(pool, scope, stats)
        today = self._clock().date()
        tiered_ids, blocks, expired = self._tiers(matches, pool, scope, meaning, page_full, today,
                                                  count_expired=request.date_from is None)
        name_keys = tuple(dict.fromkeys(k for c in concepts for k in (*sorted(name_query_keys(c)),
                                                                         *concept_trigrams(c))))
        related, related_small = self._related(name_keys, pool, scope, meaning, kinds, carded, tiered_ids,
                                               set(matches))
        shown_ids = [i for tier in _TIERS for i in tiered_ids.get(tier, [])[:_SHOWN[tier]]]
        await self._load_full(user_id, pool, shown_ids + [i for i, _ in related])
        cards, also_at = self._collapse(tiered_ids, pool, scope)
        built = await self._build_cards(user_id, cards, matches, pool, scope, also_at)
        built += tuple(self._related_card(pool, scope, item, similar) for item, similar in related)
        stats.tiers = {t.value: b.total for t, b in blocks.items()}
        stats.related = len(related)

        conjunction: Tuple[Tuple[str, int], ...] = ()
        if not names_only and not blocked and not unchecked and not matches and len(concepts) >= 2:
            conjunction = tuple((c.word, len(s)) for c, s in zip(concepts, in_scope))
        keys_v = state.applied_versions.get("keys")   # not the gate key (Δ65: that is `accuracy`)
        not_described, not_describable, no_passages = self._job_counts(state)
        long_docs = sum(1 for i, r in pool.fulls.items()
                        if i in scope.passing and r.chunk_count > PASSAGE_MAX_CHUNKS_PER_FILE)
        coverage = CoverageFacts(
            rekeying=keys_v is not None and int(keys_v) != KEYS_VERSION, full_page=page_full,
            keys_cut=sum(1 for c in built if c.keys_cut), cut=q.cut, not_described=not_described,
            not_describable=not_describable, no_passages=no_passages + long_docs, unchecked=tuple(unchecked),
            names_only=names_only, inaccessible=0 if built else await self._closed_in_scope(user_id, state, folder, subtree, ancestry))   # Δ93
        position = request.query.casefold()
        ordered = sorted(concepts, key=lambda c: (position.find(c.word) % (len(position) + 1), concepts.index(c)))
        tiered = TieredResult(
            concepts=tuple(c.word for c in ordered), ignored=q.ignored,
            common=tuple(c.word for c in concepts if c.word in allowance.common),
            coverage=coverage, blocks=tuple(blocks[t] for t in _TIERS if t in blocks),
            related_small=related_small, absent=tuple(absent), absent_in_scope=tuple(absent_in_scope),
            without=without, without_at_least=bool(blocked) and page_full, conjunction=conjunction,
            expired=expired)
        return DriveSearchResult(status=STATUS_OK, cards=built, request=request,
                                 folder_path=folder.path if folder else None, tiered=tiered)

    # -- streams --------------------------------------------------------------------------------

    async def _wide_pool(self, user_id: str, request: DriveSearchRequest, concepts: Sequence[Concept],
                         members: Sequence[Optional[str]], post: _PostFilters, pool: _Pool, with_content: bool,
                         use_cards: bool, stats: _Stats) -> None:
        """The user-wide streams (§25.5.2), all in parallel: about, word and hint pages per concept, the name stream
        per concept, and the kNN pages. Kind, date and folder filters are post-filters, so `df` is user-wide; past a
        full page the about stream reads its filtered composite and the name stream its kind prefilter (P6 review
        M1). A concept with a full page no scoped read covered is `unchecked`: the scope's absence is never claimed
        from it."""
        about = [self._about_hits(user_id, c, request, post.single_kind, stats) for c in concepts] \
            if with_content else []
        words = [self._word_hits(user_id, c, stats) for c in concepts] if with_content else []
        hints = [self._hint_hits(user_id, c, stats) for c in concepts] if with_content else []
        results = await asyncio.gather(
            asyncio.gather(*about), asyncio.gather(*words), asyncio.gather(*hints),
            self._name_hits(user_id, concepts, post.single_kind, stats),
            self._knn(user_id, request.query, members, use_cards, content_admitted(), pool, stats))
        about_pages, word_pages, hint_pages, (named, name_pages), _ = results
        stats.content = "wide" if with_content else "off"
        for i, (recs, full, scoped) in enumerate(about_pages):
            for rec in recs:
                if pool.add(rec):
                    pool.hits[i].add_about(rec.item_id, set(rec.about_keys) & set(concepts[i].about_keys))
            pool.hits[i].full |= full
            pool.hits[i].unchecked |= full and not (scoped and not post.family and not post.folder)
        for i, (chunks, full) in enumerate(word_pages):
            for cid in chunks:
                pool.hits[i].words.setdefault(cid[0], set()).add(cid)
            pool.hits[i].full |= full
            pool.hits[i].unchecked |= full
        for i, (docs, full) in enumerate(hint_pages):
            for doc in docs:
                cid = (doc.item_id, doc.generation, doc.index)
                pool.hint_keys[cid] = frozenset(doc.hint_keys)
                pool.hint_checked.add(cid)
                pool.hits[i].hints.setdefault(doc.item_id, set()).add(cid)
            pool.hits[i].full |= full
            pool.hits[i].unchecked |= full
        self._add_names(named, concepts, pool, stats)
        for i, (full, scoped) in enumerate(name_pages):
            pool.hits[i].full |= full
            pool.hits[i].unchecked |= full and not (scoped and not post.dates and not post.family and not post.folder)

    async def _name_hits(self, user_id: str, concepts: Sequence[Concept], single_kind: Optional[str],
                         stats: _Stats) -> Tuple[List[DriveIndexRecord], List[Tuple[bool, bool]]]:
        """The name stream on name keys v2 (§25.5.2, RFC Δ17), per concept (P6 review M4): each concept's whole keys
        in fetches of at most 30 (`fetch_batches`), so a word's hits never depend on the other words; past a full
        page under a single kind, the kind-prefiltered page too. Then single-trigram queries (native and skeleton
        trigrams, round-robin over the words, at most NAME_KEY_QUERIES): they find similar names, never cover.
        Returns (records, per concept (a page was full, a full page was re-read under the kind and was not full))."""

        async def concept_pages(concept: Concept) -> Tuple[List[DriveIndexRecord], bool, bool]:
            whole = [*(k for k in concept.word_keys), *sorted(name_query_keys(concept) - set(concept.word_keys))]
            out: List[DriveIndexRecord] = []
            full = False
            scoped = True
            for keys in fetch_batches(whole):
                page = await self._repo.find_by_name_keys(user_id, keys, NAME_FETCH, None)
                stats.name_queries += 1
                out.extend(page)
                if len(page) >= NAME_FETCH:
                    full = True
                    if single_kind is None:
                        scoped = False
                        continue
                    narrow = await self._repo.find_by_name_keys(user_id, keys, NAME_FETCH, single_kind)
                    stats.name_queries += 1
                    out.extend(narrow)
                    scoped = scoped and len(narrow) < NAME_FETCH
            return out, full, scoped

        grams: List[str] = []
        lists = [list(concept_trigrams(c)) for c in concepts if not c.is_number]
        for i in range(max((len(g) for g in lists), default=0)):
            for gram_list in lists:
                if i < len(gram_list) and gram_list[i] not in grams and len(grams) < NAME_KEY_QUERIES:
                    grams.append(gram_list[i])
        pages, gram_pages = await asyncio.gather(
            asyncio.gather(*(concept_pages(c) for c in concepts)),
            asyncio.gather(*(self._repo.find_by_name_keys(user_id, [g], NAME_FETCH, None) for g in grams)))
        stats.name_queries += len(grams)
        merged: Dict[str, DriveIndexRecord] = {}
        for recs in [p[0] for p in pages] + list(gram_pages):
            for rec in recs:
                merged.setdefault(rec.item_id, rec)
        return list(merged.values()), [(full, scoped) for _, full, scoped in pages]

    def _add_names(self, named: Sequence[DriveIndexRecord], concepts: Sequence[Concept], pool: _Pool,
                   stats: _Stats) -> None:
        stats.name = len(named)
        for rec in named:
            if not pool.add(rec):
                continue
            pool.names[rec.item_id] = rec
            tokens = set(rec.name_tokens)
            for i, concept in enumerate(concepts):
                if tokens & name_query_keys(concept):
                    pool.hits[i].names.add(rec.item_id)

    async def _about_hits(self, user_id: str, concept: Concept, request: DriveSearchRequest,
                          single_kind: Optional[str], stats: _Stats) -> Tuple[List[Rec], bool, bool]:
        """One concept's about pages (§25.5.2), one per key batch of at most 30: newest first, ABOUT_FETCH a page,
        read on through the cursor up to GROUP_READ_MAX when full. Past a full page under a single-kind or date
        filter, the filtered composite is read too, so a filtered search still sees its files. A concept covered by
        validity words also reads the soonest-expiring records (`find_expiring`, §25.5.4) from `drive_date_from`
        (else today): for what expires the dates bound the expiry date (P6 review M2). Returns (records, a page was
        full, every full page was re-read in scope and the re-read was not full)."""
        filtered = single_kind is not None or request.date_from is not None or request.date_to is not None

        async def batch(keys: Tuple[str, ...]) -> Tuple[List[Rec], bool, bool]:
            out: List[Rec] = []
            after: Optional[str] = None
            while True:
                page = await self._repo.find_by_about_keys(user_id, keys, ABOUT_FETCH, after=after)
                stats.about_queries += 1
                out.extend(page)
                if len(page) < ABOUT_FETCH:
                    return out, False, True
                if len(out) >= GROUP_READ_MAX:
                    break
                after = page[-1].item_id
            if not filtered:
                return out, True, False
            narrow = await self._repo.find_by_about_keys(
                user_id, keys, ABOUT_FETCH, kind=single_kind, date_from=request.date_from, date_to=request.date_to)
            stats.about_queries += 1
            return out + narrow, True, len(narrow) < ABOUT_FETCH

        pages = await asyncio.gather(*(batch(b) for b in fetch_batches(concept.about_keys)))
        recs = [r for page, _, _ in pages for r in page]
        full = any(f for _, f, _ in pages)
        scoped = all(s for _, _, s in pages)
        validity = [k for k in concept.about_keys if k.startswith(_VALIDITY)]
        if validity and any(set(r.about_keys) & set(validity) for r in recs):
            start = request.date_from.date() if request.date_from is not None else self._clock().date()
            expiring = await asyncio.gather(*(self._repo.find_expiring(user_id, b, start, ABOUT_FETCH)
                                              for b in fetch_batches(validity)))
            stats.about_queries += len(expiring)
            recs += [r for page in expiring for r in page]
        return recs, full, scoped

    async def _word_hits(self, user_id: str, concept: Concept, stats: _Stats) -> Tuple[List[ChunkId], bool]:
        """One concept's word pages (§25.5.2): WORD_FETCH chunks ordered by item, continued past the last item while
        the page is full and it names fewer than WORD_FILES_MAX files. Chunk ids only."""

        async def batch(keys: Tuple[str, ...]) -> Tuple[List[ChunkId], bool]:
            out: List[ChunkId] = []
            files: Set[str] = set()
            after: Optional[str] = None
            while True:
                page = await self._repo.find_chunks_by_word_keys(user_id, keys, WORD_FETCH, after_item_id=after)
                stats.word_queries += 1
                out.extend((c.item_id, c.generation, c.index) for c in page)
                files.update(c.item_id for c in page)
                if len(page) < WORD_FETCH:
                    return out, False
                if len(files) >= WORD_FILES_MAX:
                    return out, True
                after = page[-1].item_id

        pages = await asyncio.gather(*(batch(b) for b in fetch_batches(concept.word_keys)))
        return [cid for page, _ in pages for cid in page], any(f for _, f in pages)

    async def _hint_hits(self, user_id: str, concept: Concept, stats: _Stats) -> Tuple[List[Any], bool]:
        """One concept's hint pages (§25.5.2): HINT_FETCH hint documents ordered by item; a number has none."""
        batches = fetch_batches(concept.hint_keys)
        pages = await asyncio.gather(*(self._repo.find_hints_by_keys(user_id, b, HINT_FETCH) for b in batches))
        stats.hint_queries += len(batches)
        return [d for page in pages for d in page], any(len(page) >= HINT_FETCH for page in pages)

    async def _knn(self, user_id: str, query: str, members: Sequence[Optional[str]], use_cards: bool,
                   with_content: bool, pool: _Pool, stats: _Stats) -> None:
        """The meaning streams (§25.5.2): one query embedding; card, chunk and hint kNN, one per member kind under a
        kind filter (so the floor is the filtered pool's). Off while the index's vectors are in another space."""
        if not use_cards:
            return
        vector = await self._embeddings.get_embedding(query, task_type="RETRIEVAL_QUERY")
        pool.vectors = True

        async def none() -> List[Any]:
            return []

        cards, chunks, hints = await asyncio.gather(
            asyncio.gather(*(self._repo.find_nearest_cards(user_id, vector, CARD_FETCH, k) for k in members)),
            asyncio.gather(*(self._repo.find_nearest_chunks(user_id, vector, CHUNK_FETCH, k) for k in members))
            if with_content else none(),
            asyncio.gather(*(self._repo.find_nearest_hints(user_id, vector, HINT_FETCH, k) for k in members))
            if with_content else none())
        for page in cards:
            for rec, distance in page:
                pool.add(rec)
                pool.card_knn.append((rec.item_id, distance))
        for page in chunks:
            pool.chunk_knn += [((c.item_id, c.generation, c.index), d) for c, d in page]
        for page in hints:
            pool.hint_knn += [((h.item_id, h.generation, h.index), d) for h, d in page]
        stats.card, stats.chunk, stats.hint = len(pool.card_knn), len(pool.chunk_knn), len(pool.hint_knn)

    async def _memory_pool(self, user_id: str, query: str, concepts: Sequence[Concept],
                           subtree: Sequence[DriveIndexRecord], pool: _Pool, ancestry: _Ancestry, with_content: bool,
                           use_cards: bool, members: Sequence[Optional[str]], stats: _Stats) -> None:
        """A folder whose subtree fits FOLDER_INMEM_CARDS (§25.5.5): its records' about and name keys in memory, and
        its chunks' word keys, hint documents and vectors while they fit FOLDER_INMEM_CHUNKS; past that the user-wide
        word, hint and kNN streams run under the folder post-filter."""
        for rec in subtree:
            pool.add(rec)
        name_keys = [k for c in concepts for k in (*name_query_keys(c), *concept_trigrams(c))]
        named = [r for r in subtree if name_match_v2(r.name_tokens, name_keys) is not None]
        self._add_names(named, concepts, pool, stats)
        if with_content:
            for rec in subtree:
                held = set(rec.about_keys)
                for i, concept in enumerate(concepts):
                    if held & set(concept.about_keys):
                        pool.hits[i].add_about(rec.item_id, held & set(concept.about_keys))
        vector: Optional[List[float]] = None
        if use_cards:
            vector = await self._embeddings.get_embedding(query, task_type="RETRIEVAL_QUERY")
            pool.vectors = True
            carded = [r.item_id for r in subtree if r.has_card and (members == [None] or r.kind in members)]
            vectors = await self._repo.card_vectors(user_id, carded)
            pool.card_knn = [(i, cosine_distance(vector, v)) for i, v in vectors.items()]
            stats.card = len(pool.card_knn)
        if not with_content:
            return
        readable = [r for r in subtree if r.indexed_generation is not None and r.chunk_count > 0
                    and ancestry.chain(r) is not None]
        if sum(r.chunk_count for r in readable) > FOLDER_INMEM_CHUNKS:
            stats.content = "wide"
            words, hints = await asyncio.gather(asyncio.gather(*(self._word_hits(user_id, c, stats) for c in concepts)),
                                                asyncio.gather(*(self._hint_hits(user_id, c, stats) for c in concepts)))
            for i, (chunks, full) in enumerate(words):
                for cid in chunks:
                    pool.hits[i].words.setdefault(cid[0], set()).add(cid)
                pool.hits[i].full |= full
            for i, (docs, full) in enumerate(hints):
                for doc in docs:
                    cid = (doc.item_id, doc.generation, doc.index)
                    pool.hint_keys[cid] = frozenset(doc.hint_keys)
                    pool.hint_checked.add(cid)
                    pool.hits[i].hints.setdefault(doc.item_id, set()).add(cid)
                pool.hits[i].full |= full
            if vector is not None:
                await self._knn_vectors(user_id, vector, members, pool, stats)
            return
        stats.content = "memory"
        for start in range(0, len(readable), _WALK_PARALLEL):
            batch = readable[start:start + _WALK_PARALLEL]
            rows, hint_rows = await asyncio.gather(
                asyncio.gather(*(self._repo.get_chunks_with_vectors(
                    user_id, r.item_id, int(r.indexed_generation or 0), range(r.chunk_count), text=False, terms=True,
                    vectors=vector is not None) for r in batch)),
                asyncio.gather(*(self._repo.get_hints_with_vectors(user_id, r.item_id, int(r.indexed_generation or 0))
                                 for r in batch)))
            for row in rows:
                for chunk, chunk_vector in row:
                    cid = (chunk.item_id, chunk.generation, chunk.index)
                    pool.chunk_keys[cid] = frozenset(chunk.word_keys)
                    if vector is not None and chunk_vector:
                        pool.chunk_knn.append((cid, cosine_distance(vector, chunk_vector)))
                    for i, concept in enumerate(concepts):
                        if pool.chunk_keys[cid] & set(concept.word_keys):
                            pool.hits[i].words.setdefault(chunk.item_id, set()).add(cid)
            for docs in hint_rows:
                for doc, hint_vector in docs:
                    cid = (doc.item_id, doc.generation, doc.index)
                    pool.hint_keys[cid] = frozenset(doc.hint_keys)
                    pool.hint_checked.add(cid)
                    pool.items[cid] = tuple(doc.items)
                    if vector is not None and hint_vector:
                        pool.hint_knn.append((cid, cosine_distance(vector, hint_vector)))
                    for i, concept in enumerate(concepts):
                        if pool.hint_keys[cid] & set(concept.hint_keys):
                            pool.hits[i].hints.setdefault(doc.item_id, set()).add(cid)
                pool.items_read.update((d.item_id, d.generation) for d, _ in docs)
        stats.chunk, stats.hint = len(pool.chunk_knn), len(pool.hint_knn)

    async def _knn_vectors(self, user_id: str, vector: List[float], members: Sequence[Optional[str]], pool: _Pool,
                           stats: _Stats) -> None:
        chunks, hints = await asyncio.gather(
            asyncio.gather(*(self._repo.find_nearest_chunks(user_id, vector, CHUNK_FETCH, k) for k in members)),
            asyncio.gather(*(self._repo.find_nearest_hints(user_id, vector, HINT_FETCH, k) for k in members)))
        pool.chunk_knn = [((c.item_id, c.generation, c.index), d) for page in chunks for c, d in page]
        pool.hint_knn = [((h.item_id, h.generation, h.index), d) for page in hints for h, d in page]
        stats.chunk, stats.hint = len(pool.chunk_knn), len(pool.hint_knn)

    # -- records, fence, visibility -------------------------------------------------------------

    async def _records(self, user_id: str, pool: _Pool) -> None:
        """Whole records for the files named only by chunks or hint documents, and for every kNN file (the meaning
        score reads its chunk count and length)."""
        named = {i for h in pool.hits for i in (*h.words, *h.hints)} - set(pool.leans)
        meaning = {cid[0] for cid, _ in pool.chunk_knn + pool.hint_knn} - set(pool.fulls)
        await self._load_full(user_id, pool, sorted(named | meaning))

    async def _load_full(self, user_id: str, pool: _Pool, item_ids: Iterable[str]) -> None:
        wanted = sorted({i for i in item_ids if i not in pool.fulls})
        if wanted:
            for rec in (await self._repo.get_records(user_id, wanted)).values():
                pool.add(rec)

    def _fence(self, pool: _Pool) -> None:
        """§7.4 on every stream's chunks and hint documents: only the readable generation counts."""
        for hits in pool.hits:
            for by_file in (hits.words, hits.hints):
                for item in list(by_file):
                    kept = {cid for cid in by_file[item] if _fenced(pool.leans.get(item), cid[1])}
                    if kept:
                        by_file[item] = kept
                    else:
                        del by_file[item]
        pool.chunk_knn = [(cid, d) for cid, d in pool.chunk_knn if _fenced(pool.leans.get(cid[0]), cid[1])]
        pool.hint_knn = [(cid, d) for cid, d in pool.hint_knn if _fenced(pool.leans.get(cid[0]), cid[1])]

    @staticmethod
    def _validity_keys(concepts: Sequence[Concept], pool: _Pool) -> FrozenSet[str]:
        """The validity keys (`lv:`) of the concepts some about hit holds them for: a query for what expires."""
        held = {k for i in set().union(*(h.about for h in pool.hits)) if i in pool.leans
                for k in pool.leans[i].about_keys if k.startswith(_VALIDITY)} if pool.hits else set()
        return frozenset(k for c in concepts for k in c.about_keys if k in held)

    async def _scope(self, pool: _Pool, request: DriveSearchRequest, kinds: FrozenSet[str],
                     folder: Optional[_Folder], ancestry: _Ancestry, validity: FrozenSet[str]) -> _Scope:
        """Read-time visibility (§5.2, §17.1 d) of every record named, then the kind, date and folder filters. A
        record holding a validity key of the query is dated by its `valid_until` (P6 review M2)."""
        scope = _Scope(filtered=bool(kinds or request.date_from or request.date_to or folder))
        await ancestry.load(pool.leans.values())
        for item, rec in pool.leans.items():
            chain = ancestry.chain(rec)
            if chain is None:
                continue
            path, ancestors = chain
            scope.paths[item] = path
            if kinds and rec.kind not in kinds:
                continue
            dated = (_ValidUntil(rec.valid_until) if validity and set(rec.about_keys) & validity else rec)
            if not in_date_range(dated, request):
                continue
            if folder is not None and folder.record.item_id not in ancestors:
                continue
            scope.passing.add(item)
        return scope

    # -- coverage and tiers ---------------------------------------------------------------------

    async def _matches(self, user_id: str, pool: _Pool, concepts: Sequence[Concept], hits: Sequence[ConceptHits],
                       in_scope: Sequence[Set[str]], df: Sequence[int], full: Sequence[bool], allowance: Allowance,
                       stats: _Stats) -> Dict[str, FileMatch]:
        """Test each candidate's coverage (§25.5.2, §25.5.4): its about and name keys and the keys of the chunks and
        hint documents the streams named for it, read by id; chunk texts and hint items only where two or more words
        are covered in one chunk (the minimum window)."""
        if not concepts:
            return {}
        candidates = sorted(candidate_ids(concepts, in_scope, df, allowance, full))
        stats.candidates_tested += len(candidates)
        chunk_ids = {item: set().union(*(h.words.get(item, set()) | h.hints.get(item, set()) for h in hits))
                     for item in candidates}
        await self._read_keys(user_id, pool, [cid for ids in chunk_ids.values() for cid in ids])
        files = {item: FileKeys(
            item_id=item, about_keys=frozenset(pool.leans[item].about_keys),
            name_keys=frozenset(pool.leans[item].name_tokens),
            chunk_keys={cid: pool.chunk_keys[cid] for cid in chunk_ids[item] if cid in pool.chunk_keys},
            hint_keys={cid: pool.hint_keys[cid] for cid in chunk_ids[item] if cid in pool.hint_keys})
            for item in candidates}
        needed = {item: chunks_needing_text(f, concepts) for item, f in files.items()}
        await self._read_texts(user_id, pool, [cid for ids in needed.values() for cid in ids])
        await self._read_items(user_id, pool, [cid for item, ids in needed.items() for cid in ids
                                               if cid in files[item].hint_keys])
        out: Dict[str, FileMatch] = {}
        for item, f in files.items():
            match = classify(f, concepts, allowance, pool.texts, pool.items)
            if match is not None:
                out[item] = match
        return out

    async def _read_keys(self, user_id: str, pool: _Pool, ids: Iterable[ChunkId]) -> None:
        wanted = sorted(set(ids))
        chunk_ids = [cid for cid in wanted if cid not in pool.chunk_keys]
        hint_ids = [cid for cid in wanted if cid not in pool.hint_checked]
        chunks, hints = await asyncio.gather(
            self._repo.get_chunk_keys(user_id, chunk_ids) if chunk_ids else _empty(),
            self._repo.get_hint_keys(user_id, hint_ids) if hint_ids else _empty())
        pool.chunk_keys.update({cid: frozenset(keys) for cid, keys in chunks.items()})
        pool.hint_keys.update({cid: frozenset(keys) for cid, keys in hints.items()})
        pool.hint_checked.update(hint_ids)

    async def _read_texts(self, user_id: str, pool: _Pool, ids: Iterable[ChunkId]) -> None:
        """Chunk texts by id, one read per file generation."""
        by_file: Dict[Tuple[str, int], Set[int]] = {}
        for item, gen, index in ids:
            if (item, gen, index) not in pool.texts:
                by_file.setdefault((item, gen), set()).add(index)
        groups = sorted(by_file.items())
        for start in range(0, len(groups), _READ_PARALLEL):
            part = groups[start:start + _READ_PARALLEL]
            rows = await asyncio.gather(*(self._repo.get_chunks(user_id, item, gen, sorted(indexes))
                                          for (item, gen), indexes in part))
            for row in rows:
                for chunk in row:
                    pool.texts[(chunk.item_id, chunk.generation, chunk.index)] = chunk.text

    async def _read_items(self, user_id: str, pool: _Pool, ids: Iterable[ChunkId]) -> None:
        """Hint items (evidence spans) by file generation."""
        wanted = sorted({(item, gen) for item, gen, _ in ids} - pool.items_read)
        for start in range(0, len(wanted), _READ_PARALLEL):
            part = wanted[start:start + _READ_PARALLEL]
            rows = await asyncio.gather(*(self._repo.get_hints(user_id, item, gen) for item, gen in part))
            for docs in rows:
                for doc in docs:
                    pool.items[(doc.item_id, doc.generation, doc.index)] = tuple(doc.items)
            pool.items_read.update(part)

    def _meaning(self, pool: _Pool, scope: _Scope, stats: _Stats) -> Dict[str, float]:
        """`meaning(file)` (§25.5.3): per source, each visible file's best similarity over its kNN page, the robust z
        over the best MEANING_POOL files (a pool under MEANING_POOL_MIN abstains), the long-document correction on
        chunks and hints; EPUBs give no chunk score and notes under PROFILE_MIN_CHARS no chunk or hint score."""
        excluded = frozenset(i for i, r in pool.fulls.items()
                             if r.kind == "epub" or 0 < r.text_chars < PROFILE_MIN_CHARS)

        def best(page: Sequence[Tuple[Any, float]], by_item: bool, skip: FrozenSet[str]) -> Dict[str, float]:
            out: Dict[str, float] = {}
            for key, distance in page:
                item = key if by_item else key[0]
                if item in scope.paths and item not in skip:
                    out[item] = max(out.get(item, -1.0), _similarity(distance))
            return out

        sources = [best(pool.card_knn, True, frozenset()), best(pool.chunk_knn, False, excluded),
                   best(pool.hint_knn, False, excluded)]
        scored = [source_scores(s) for s in sources]
        stats.pool_card, stats.pool_chunk, stats.pool_hint = (
            "abstain" if z is None else str(len(z)) for z in scored)
        counts = {i: r.chunk_count for i, r in pool.fulls.items()}
        return meaning_scores(scored[0] or {}, scored[1] or {}, scored[2] or {}, chunk_counts=counts,
                              no_chunk_score=excluded)

    def _tiers(self, matches: Mapping[str, FileMatch], pool: _Pool, scope: _Scope, meaning: Mapping[str, float],
               page_full: bool, today: date, *, count_expired: bool = True
               ) -> Tuple[Dict[MatchTier, List[str]], Dict[MatchTier, TierBlock], int]:
        """Each match in its tier, ordered (validity matches soonest first, expired ones counted, §25.5.4), with its
        totals; the members past the shown count become group members."""
        by_tier: Dict[MatchTier, List[str]] = {}
        expired = 0
        start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        validity: Dict[str, datetime] = {}
        for item, match in matches.items():
            rec = pool.leans[item]
            if _expiry_match(match):
                if count_expired and rec.valid_until is not None and rec.valid_until < start:
                    expired += 1
                    continue
                if rec.valid_until is not None:
                    validity[item] = rec.valid_until
            by_tier.setdefault(match.tier, []).append(item)
        blocks: Dict[MatchTier, TierBlock] = {}
        for tier, items in by_tier.items():
            items.sort(key=lambda i: (0 if i in validity else 1, validity.get(i, _EPOCH).timestamp(),
                                      *tier_order_key(len(matches[i].missed), meaning.get(i),
                                                      pool.leans[i].effective_date, i)))
            rest = tuple(self._member(pool, scope, i) for i in items[_SHOWN[tier]:])
            blocks[tier] = TierBlock(tier, len(items), at_least=page_full, rest=rest, nonce=self._nonce())
        return by_tier, blocks, expired

    @staticmethod
    def _member(pool: _Pool, scope: _Scope, item: str) -> GroupMember:
        rec = pool.leans[item]
        path = scope.paths.get(item, rec.name)
        when = rec.effective_date
        return GroupMember(folder=path.rsplit("/", 1)[0] if "/" in path else "/", kind=rec.kind,
                           month=when.strftime("%Y-%m") if when else "")

    def _related(self, qkeys: Sequence[str], pool: _Pool, scope: _Scope, meaning: Mapping[str, float],
                 kinds: FrozenSet[str], carded: int, tiered: Mapping[MatchTier, List[str]],
                 matched: Set[str]) -> Tuple[List[Tuple[str, bool]], bool]:
        """Related by meaning only (§25.5.3): similar names first (over the name_match_v2 floor, not covering the
        query), then files whose meaning clears the band's floor; at most RELATED_MAX, never a file of another tier.
        When every source abstained on a drive under the first band and nothing else matched, the best RELATED_SMALL
        by similarity (never under a kind filter; beside matches it would only be filler)."""
        taken = set(matched) | {i for items in tiered.values() for i in items}
        names = sorted(((name_match_v2(r.name_tokens, qkeys) or 0, i) for i, r in pool.names.items()
                        if i in scope.passing and i not in taken and name_match_v2(r.name_tokens, qkeys) is not None),
                       key=lambda p: (-p[0], p[1]))
        out: List[Tuple[str, bool]] = [(i, True) for _, i in names[:RELATED_MAX]]
        floor = related_floor(size_band(carded))
        for item, z in sorted(meaning.items(), key=lambda kv: (-kv[1], kv[0])):
            if len(out) >= RELATED_MAX:
                break
            if z >= floor and item in scope.passing and item not in taken and item not in dict(out):
                out.append((item, False))
        small = False
        if not meaning and pool.vectors and not out and not taken and not kinds and carded < 200:
            sims: Dict[str, float] = {}
            for key, distance in [(k, d) for k, d in pool.card_knn] + [(c[0], d) for c, d in pool.chunk_knn]:
                if key in scope.passing and key not in taken:
                    sims[key] = max(sims.get(key, -1.0), _similarity(distance))
            best = sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))[:RELATED_SMALL]
            out = [(i, False) for i, _ in best]
            small = bool(out)
        return out, small

    # -- cards ----------------------------------------------------------------------------------

    def _collapse(self, tiered: Mapping[MatchTier, List[str]], pool: _Pool,
                  scope: _Scope) -> Tuple[List[Tuple[MatchTier, str]], Dict[str, List[str]]]:
        """The shown files in tier order, identical copies (same content hash, not empty) collapsed into one shown
        copy's `also_at`: the first noted copy when any copy carries the owner's note (decided on the lean
        `owner_note_id`, DRIVE_FILE_NOTES_RFC §6.1, review I7), else the first shown one."""
        candidates = [(tier, item) for tier in _TIERS for item in tiered.get(tier, [])[:_SHOWN[tier]]]

        def collapsible(item: str) -> Optional[str]:
            digest = pool.leans[item].content_hash
            return digest if digest and not digest.endswith(":0") else None

        keeper: Dict[str, str] = {}
        for _, item in candidates:
            digest = collapsible(item)
            if digest is None:
                continue
            noted = bool(pool.leans[item].owner_note_id)
            if digest not in keeper or (noted and not pool.leans[keeper[digest]].owner_note_id):
                keeper[digest] = item
        shown: List[Tuple[MatchTier, str]] = []
        also_at: Dict[str, List[str]] = {}
        for tier, item in candidates:
            digest = collapsible(item)
            if digest is not None and keeper[digest] != item:
                also_at.setdefault(keeper[digest], []).append(scope.paths[item])
                continue
            shown.append((tier, item))
        return shown, also_at

    async def _build_cards(self, user_id: str, shown: Sequence[Tuple[MatchTier, str]],
                           matches: Mapping[str, FileMatch], pool: _Pool, scope: _Scope,
                           also_at: Mapping[str, List[str]]) -> Tuple[DriveSearchCard, ...]:
        """Cards of the shown files: card text, match sources, the passage of Mentioned and By-translation cards cut
        around the matched words or the evidence span, sharing WINDOW_CHARS_TOTAL_V2 in tier order."""
        chunks = [matches[i].chunk for _, i in shown if matches[i].chunk is not None]
        await self._read_texts(user_id, pool, [c for c in chunks if c is not None])
        await self._read_items(user_id, pool, [matches[i].chunk for _, i in shown  # type: ignore[misc]
                                               if matches[i].chunk is not None
                                               and any(c.where == WHERE_HINT for c in matches[i].covers)])
        budget = WINDOW_CHARS_TOTAL_V2
        out: List[DriveSearchCard] = []
        for tier, item in shown:
            rec = pool.fulls.get(item)
            if rec is None:
                continue
            match = matches[item]
            text = pool.texts.get(match.chunk) if match.chunk is not None else None
            items = pool.items.get(match.chunk, ()) if match.chunk is not None else ()
            explained = explain(match, rec, rec.name, text, items)
            passage = ""
            if tier in _PASSAGE_TIERS and text:
                passage = passage_window(text, explained.spans, min(WINDOW_MAX, budget))
                budget -= len(passage)
            card = make_card(rec, scope.paths[item], card_text(rec, owner=False), self._nonce())
            out.append(replace(card, tier=tier.value, sources=explained.sources, passage=passage,
                               not_matched=not_matched(match), keys_cut=rec.keys_capped,
                               also_at=tuple(also_at.get(item, ())), expiry_match=_expiry_match(match)))
        return tuple(out)

    def _related_card(self, pool: _Pool, scope: _Scope, item: str, similar: bool) -> DriveSearchCard:
        rec = pool.fulls[item]
        card = make_card(rec, scope.paths[item], card_text(rec, owner=False), self._nonce())
        return replace(card, tier=MatchTier.RELATED.value, similar_name=similar)

    @staticmethod
    def _job_counts(state: DriveSyncState) -> Tuple[int, int, int]:
        """The coverage line's enrichment counts (§25.5.6), as the last tick stored them (XC-9: no count query per
        search): text files with no description yet whose job waits, or is held — a file already described whose
        re-description waits is not one (XC-3) — and profiled books, sheets and data files (no passage keywords)."""
        def count(gap: str) -> int:
            return int(state.counters.get(COVERAGE_COUNTERS[gap], 0))

        return count(GAP_WAITING), count(GAP_HELD), count(GAP_NO_PASSAGES)

    # ------------------------------------------------------------------------------------------
    # Passages (§28.3 A4-A8, A18): find_passages and read_passages
    # ------------------------------------------------------------------------------------------

    async def excerpts(self, user_id: str, request: ExcerptRequest, *,
                       shown: FrozenSet[ChunkId] = frozenset()) -> ExcerptResult:
        """The indexed passages nearest the question (A4), in scope (A5: `file_refs`, else `folder`, else the whole
        drive), selected new-only against `shown` (A6), with the "where it comes from" context (A18). The search's
        gate first; vectors in another embedding space answer `wrong_space` (plan D-P4: nothing to rank)."""
        started = self._monotonic()
        stats = _ExcerptStats()
        result = await self._excerpts(user_id, request, shown, stats)
        self._log_excerpts(user_id, result, stats, started)
        return result

    async def excerpt_range(self, user_id: str, request: ExcerptRangeRequest) -> ExcerptResult:
        """One file's passages by display number (A7): its readable generation only, at most EXCERPT_RANGE_MAX chunks
        and EXCERPTS_CHARS_MAX characters (the first always), `continue_at` naming the first passage left out. Reads
        no vector, so only the search's gate applies, not the embedding space."""
        started = self._monotonic()
        stats = _ExcerptStats(mode="excerpt_range", scope="range")
        result = await self._excerpt_range(user_id, request, stats)
        self._log_excerpts(user_id, result, stats, started)
        return result

    def _log_excerpts(self, user_id: str, result: ExcerptResult, stats: _ExcerptStats, started: float) -> None:
        ms = round((self._monotonic() - started) * 1000)
        chunks = sum(len(f.chunks) for f in result.files)
        chars = sum(len(c.text) for f in result.files for c in f.chunks)
        logger.info(
            f"[DriveSearch] user={user_id[:8]} status={result.status} mode={stats.mode} scope={stats.scope} "
            f"hits={stats.hits} candidates={stats.candidates} keyed={stats.keyed} selected={chunks} "
            f"files={len(result.files)} repeated={len(result.repeated)} no_text={result.no_text} "
            f"continue_at={result.continue_at} chars={chars} reads={stats.reads} building={result.building} "
            f"awaiting_rebuild={result.awaiting_rebuild} wrong_space={result.wrong_space} ms={ms}")

    async def _excerpt_gate(self, user_id: str,
                            request: Union[ExcerptRequest, ExcerptRangeRequest]) -> Union[ExcerptResult, DriveSyncState]:
        """The search's gate sequence (§10.1 step 0, D30), answered with the search's statuses and flags."""
        profile, state, connection = await asyncio.gather(
            self._users.get_user(user_id), self._repo.get_sync_state(user_id), self._drive.connection(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        gate = search_gate(enabled, state, connection, self._clock())
        if gate.status != STATUS_OK:
            return ExcerptResult(status=gate.status, request=request, other_drive=gate.other_drive)
        if gate.rebuilding or state is None or not state.root_item_id:
            return ExcerptResult(status=STATUS_OK, request=request, building=True)
        if MIGRATION_GATE_KEY not in state.applied_versions:
            return ExcerptResult(status=STATUS_OK, request=request, building=True, awaiting_rebuild=True)
        return state

    async def _excerpts(self, user_id: str, request: ExcerptRequest, shown: FrozenSet[ChunkId],
                        stats: _ExcerptStats) -> ExcerptResult:
        gated = await self._excerpt_gate(user_id, request)
        if isinstance(gated, ExcerptResult):
            stats.scope = "gate"
            return gated
        state = gated
        if not state.cards_in_space(self._space):
            stats.scope = "gate"
            return ExcerptResult(status=STATUS_OK, request=request, wrong_space=True)
        ancestry = _Ancestry(self._repo, user_id, state.root_item_id)
        scoped, building = await asyncio.gather(self._excerpt_scope(user_id, request, ancestry, stats),
                                                self._building(user_id, state))
        if isinstance(scoped, ExcerptResult):
            return replace(scoped, building=building)
        pool = scoped
        stats.candidates = len(pool.knn)
        files, selection = await self._excerpt_files(user_id, request.question, pool, shown, stats)
        return ExcerptResult(
            status=STATUS_OK, request=request, files=files, repeated=selection.repeated,
            no_text=len(pool.no_text), no_text_paths=tuple(pool.no_text[:_NO_TEXT_LISTED]), building=building,
            folder_path=pool.folder_path, context=self._excerpt_context(files, pool.records, pool.chains, ancestry))

    async def _excerpt_scope(self, user_id: str, request: ExcerptRequest, ancestry: _Ancestry,
                             stats: _ExcerptStats) -> Union[ExcerptResult, _ExcerptPool]:
        """A5: `file_refs` (they take precedence over a folder), a folder, or the whole drive."""
        if request.file_refs:
            stats.scope = "files"
            return await self._refs_pool(user_id, request, ancestry, stats)
        if request.folder:
            found = await self._resolve_folder(user_id, request.folder, ancestry)
            if isinstance(found, DriveSearchResult):
                return ExcerptResult(
                    status=found.status, request=request, folder_path=found.folder_path or "",
                    folder_candidates=tuple(c.path for c in found.cards) if found.status == STATUS_AMBIGUOUS_FOLDER
                    else (), folder_lookup_truncated=found.folder_lookup_truncated,
                    folder_not_indexed=found.folder_not_indexed)
            if hides_descendants(found.record):   # nothing below it is indexed
                return ExcerptResult(status=STATUS_OK, request=request, folder_path=found.path,
                                     folder_not_indexed=True)
            return await self._folder_pool(user_id, request, found, ancestry, stats)
        stats.scope = "all"
        vector = await self._question_vector(request.question)
        hits = await self._repo.find_nearest_chunks(user_id, vector, CHUNK_FETCH)
        pool = _ExcerptPool()
        await self._hits_pool(user_id, hits, ancestry, pool, stats)
        return pool

    async def _question_vector(self, question: str) -> List[float]:
        return await self._embeddings.get_embedding(question, task_type="RETRIEVAL_QUERY")

    async def _refs_pool(self, user_id: str, request: ExcerptRequest, ancestry: _Ancestry,
                         stats: _ExcerptStats) -> _ExcerptPool:
        """Each named file's readable generation, its chunk vectors by index, cosine in memory. A ref that names no
        visible file with indexed text is counted on the no-text line."""
        named = [(ref, parse_drive_ref(ref) or "") for ref in request.file_refs]
        found, vector = await asyncio.gather(self._repo.get_records(user_id, [i for _, i in named if i]),
                                             self._question_vector(request.question))
        await ancestry.load(found.values())
        pool = _ExcerptPool()
        readable: List[Tuple[DriveIndexRecord, Tuple[str, Tuple[str, ...]]]] = []
        for ref, item_id in named:
            rec = found.get(item_id)
            chain = ancestry.chain(rec) if rec is not None else None
            if rec is None or chain is None or not _readable(rec):
                pool.no_text.append(chain[0] if chain is not None else ref)
                continue
            readable.append((rec, chain))
        await self._excerpt_memory_pool(user_id, vector, readable, pool, stats)
        return pool

    async def _folder_pool(self, user_id: str, request: ExcerptRequest, folder: _Folder, ancestry: _Ancestry,
                           stats: _ExcerptStats) -> _ExcerptPool:
        """The search's walk: a complete one whose Σ chunk_count fits FOLDER_INMEM_CHUNKS ranks in memory; past that
        bound, the user-wide kNN at FETCH_MAX kept to the walked files; an incomplete walk, the same kNN kept by
        ancestry. The no-text line counts the walked files only when the walk is complete."""
        (walked, complete), vector = await asyncio.gather(self._walk(user_id, folder.record),
                                                          self._question_vector(request.question))
        ancestry.seed([folder.record, *walked])
        pool = _ExcerptPool(folder_path=folder.path)
        if not complete:
            stats.scope = "folder-wide"
            hits = await self._repo.find_nearest_chunks(user_id, vector, FETCH_MAX)
            await self._hits_pool(user_id, hits, ancestry, pool, stats, folder_id=folder.record.item_id)
            return pool
        readable: List[Tuple[DriveIndexRecord, Tuple[str, Tuple[str, ...]]]] = []
        for rec in walked:
            if rec.is_folder:
                continue
            chain = ancestry.chain(rec)
            if chain is None:
                continue
            if _readable(rec):
                readable.append((rec, chain))
            else:
                pool.no_text.append(chain[0])
        if sum(rec.chunk_count for rec, _ in readable) <= FOLDER_INMEM_CHUNKS:
            stats.scope = "folder-memory"
            await self._excerpt_memory_pool(user_id, vector, readable, pool, stats)
            return pool
        stats.scope = "folder-wide"
        hits = await self._repo.find_nearest_chunks(user_id, vector, FETCH_MAX)
        await self._hits_pool(user_id, hits, ancestry, pool, stats, known=dict((r.item_id, (r, c)) for r, c in readable))
        return pool

    async def _excerpt_memory_pool(self, user_id: str, vector: List[float],
                           readable: Sequence[Tuple[DriveIndexRecord, Tuple[str, Tuple[str, ...]]]], pool: _ExcerptPool,
                           stats: _ExcerptStats) -> None:
        """Chunk vectors of the readable generation by index, no text and no keys, ranked by cosine in memory."""
        for start in range(0, len(readable), _WALK_PARALLEL):
            batch = readable[start:start + _WALK_PARALLEL]
            rows = await asyncio.gather(*(self._repo.get_chunks_with_vectors(
                user_id, rec.item_id, int(rec.indexed_generation or 0), range(rec.chunk_count), text=False,
                terms=False) for rec, _ in batch))
            for (rec, chain), row in zip(batch, rows):
                for chunk, chunk_vector in row:
                    stats.hits += 1
                    if chunk_vector and _fenced(rec, chunk.generation):
                        pool.add(chunk, cosine_distance(vector, chunk_vector), rec, chain)

    async def _hits_pool(self, user_id: str, hits: Sequence[Tuple[DriveChunk, float]], ancestry: _Ancestry,
                         pool: _ExcerptPool, stats: _ExcerptStats, *, folder_id: Optional[str] = None,
                         known: Optional[Mapping[str, Tuple[DriveIndexRecord, Tuple[str, Tuple[str, ...]]]]] = None
                         ) -> None:
        """`find_nearest_chunks` is not fenced (port): every hit's record is read and the hit kept only for its
        readable generation and a visible file (inside `folder_id` when given). `known`: the complete walk's readable
        files with their chains; hits outside them are out of scope and cost no read."""
        stats.hits = len(hits)
        if known is not None:
            resolved = dict(known)
        else:
            recs = await self._repo.get_records(user_id, sorted({chunk.item_id for chunk, _ in hits}))
            await ancestry.load(recs.values())
            resolved = {}
            for item_id, rec in recs.items():
                chain = ancestry.chain(rec)
                if chain is not None and (folder_id is None or folder_id in chain[1]):
                    resolved[item_id] = (rec, chain)
        for chunk, distance in hits:
            entry = resolved.get(chunk.item_id)
            if entry is not None and _fenced(entry[0], chunk.generation):
                pool.add(chunk, distance, *entry)

    async def _excerpt_files(self, user_id: str, question: str, pool: _ExcerptPool, shown: FrozenSet[ChunkId],
                             stats: _ExcerptStats) -> Tuple[Tuple[ExcerptFile, ...], Selection]:
        """A4 rank, A6 select, texts by one `get_chunks` per file generation. Selection runs on span sizes first;
        a read text longer than its span (a continued table's header) re-runs it, at most _SELECT_ROUNDS times."""
        concepts = query_concepts(question).concepts
        nearest = [cid for cid, _ in sorted(pool.knn, key=lambda hit: hit[1])][:EXCERPTS_LEXICAL_WINDOW]
        keys = await self._repo.get_chunk_keys(user_id, nearest) if nearest and concepts else {}
        stats.keyed = len(nearest) if concepts else 0
        ranked = rank_excerpts(pool.knn, keys, concepts)
        hashes = {item_id: _copy_hash(rec) for item_id, rec in pool.records.items()}
        noted = frozenset(item_id for item_id, rec in pool.records.items() if rec.owner_note_id)
        sizes = dict(pool.spans)
        texts: Dict[ChunkId, DriveChunk] = {}
        absent: Set[ChunkId] = set()
        selection = Selection()
        for _ in range(_SELECT_ROUNDS):
            selection = select_excerpts(ranked, sizes, shown=shown, hashes=hashes, noted=noted)
            missing = [key for key in selection.chunks if key not in texts]
            if not missing:
                break
            read = await self._excerpt_texts(user_id, missing, stats)
            texts.update(read)
            absent.update(key for key in missing if key not in read)
            keeper_of = {copy: keeper for keeper, copies in selection.also_at.items() for copy in copies}
            for cid in ranked:
                keeper = keeper_of.get(cid[0], cid[0])
                key = (keeper, int(pool.records[keeper].indexed_generation or 0), cid[2])
                if key in texts:
                    sizes[cid] = len(texts[key].text)
            ranked = [cid for cid in ranked if cid not in absent]
        chosen = [key for key in selection.chunks if key in texts]
        by_file: Dict[str, List[DriveChunk]] = {}
        for key in chosen:
            by_file.setdefault(key[0], []).append(texts[key])
        files = tuple(self._excerpt_file(pool.records[item_id], pool.chains[item_id], chunks,
                                         [pool.chains[c][0] for c in selection.also_at.get(item_id, ())
                                          if c in pool.chains])
                      for item_id, chunks in by_file.items())
        return files, selection

    async def _excerpt_texts(self, user_id: str, keys: Sequence[ChunkId],
                             stats: _ExcerptStats) -> Dict[ChunkId, DriveChunk]:
        by_file: Dict[Tuple[str, int], Set[int]] = {}
        for item_id, generation, index in keys:
            by_file.setdefault((item_id, generation), set()).add(index)
        groups = sorted(by_file.items())
        out: Dict[ChunkId, DriveChunk] = {}
        for start in range(0, len(groups), _READ_PARALLEL):
            part = groups[start:start + _READ_PARALLEL]
            rows = await asyncio.gather(*(self._repo.get_chunks(user_id, item_id, generation, sorted(indexes))
                                          for (item_id, generation), indexes in part))
            stats.reads += len(part)
            for row in rows:
                for chunk in row:
                    out[(chunk.item_id, chunk.generation, chunk.index)] = chunk
        return out

    @staticmethod
    def _excerpt_file(rec: DriveIndexRecord, chain: Tuple[str, Tuple[str, ...]], chunks: Sequence[DriveChunk],
                      also_at: Sequence[str] = ()) -> ExcerptFile:
        """The file's identity from its record (as a search card's), its marks and its chunks in index order."""
        return ExcerptFile(
            item_id=rec.item_id, ref=make_drive_ref(rec.item_id), name=rec.name, path=chain[0], kind=rec.kind,
            generation=int(rec.indexed_generation or 0), chunk_count=rec.chunk_count, size_bytes=rec.size_bytes,
            modified_at=rec.modified_at, web_url=rec.web_url, stale=rec.stale, truncated=rec.truncated,
            capture=rec.capture, card_accuracy=rec.card_accuracy if scorable(rec) else None,
            content_hash=rec.content_hash, also_at=tuple(also_at),
            chunks=tuple(ExcerptChunk(c.item_id, c.generation, c.index, c.text, c.char_start, c.char_end)
                         for c in sorted(chunks, key=lambda c: c.index)))

    @staticmethod
    def _excerpt_context(files: Sequence[ExcerptFile], records: Mapping[str, DriveIndexRecord],
                         chains: Mapping[str, Tuple[str, Tuple[str, ...]]], ancestry: _Ancestry) -> ExcerptContext:
        """A18 from what is already loaded: each shown file's ancestor ids (its chain), description and own note;
        each noted ancestor folder from the ancestry cache (no read). Copies show the keeper's chain only; a chain
        broken by an unrecorded ancestor shows its known part."""
        contexts: List[FileContext] = []
        folders: Dict[str, FolderNote] = {}
        for file in files:
            rec = records[file.item_id]
            path, ancestors = chains[file.item_id]
            contexts.append(FileContext(item_id=file.item_id, path=path, ancestors=ancestors,
                                        description=file_description(rec), own_note=note_line(rec)))
            for folder_id in ancestors:
                folder = ancestry.cache.get(folder_id)
                if folder_id in folders or folder is None:
                    continue
                line = note_line(folder)
                chain = ancestry.chain(folder, require_card=False) if line else None
                if chain is not None:
                    folders[folder_id] = FolderNote(item_id=folder_id, path=chain[0], depth=len(chain[1]) + 1,
                                                    line=line)
        return ExcerptContext(files=tuple(contexts), folders=folders)

    async def _excerpt_range(self, user_id: str, request: ExcerptRangeRequest, stats: _ExcerptStats) -> ExcerptResult:
        gated = await self._excerpt_gate(user_id, request)
        if isinstance(gated, ExcerptResult):
            stats.scope = "gate"
            return gated
        state = gated
        ancestry = _Ancestry(self._repo, user_id, state.root_item_id)
        item_id = parse_drive_ref(request.file_ref) or ""
        found, building = await asyncio.gather(self._repo.get_records(user_id, [item_id]),
                                               self._building(user_id, state))
        rec = found.get(item_id)
        chain: Optional[Tuple[str, Tuple[str, ...]]] = None
        if rec is not None:
            await ancestry.load([rec])
            chain = ancestry.chain(rec)
        if rec is None or chain is None or not _readable(rec):
            return ExcerptResult(status=STATUS_OK, request=request, no_text=1, building=building,
                                 no_text_paths=(chain[0] if chain is not None else request.file_ref,))
        first = stored_index(request.first)
        last = min(stored_index(request.last), rec.chunk_count - 1, first + EXCERPT_RANGE_MAX - 1)
        generation = int(rec.indexed_generation or 0)
        rows = await self._repo.get_chunks(user_id, item_id, generation, list(range(first, last + 1))) \
            if first <= last else []
        stats.reads = 1 if first <= last else 0
        stats.hits = stats.candidates = len(rows)
        kept: List[DriveChunk] = []
        chars = 0
        continue_at: Optional[int] = None
        for chunk in sorted(rows, key=lambda c: c.index):
            if kept and chars + len(chunk.text) > EXCERPTS_CHARS_MAX:
                continue_at = display_number(chunk.index)
                break
            kept.append(chunk)
            chars += len(chunk.text)
        files = (self._excerpt_file(rec, chain, kept),) if kept else ()
        return ExcerptResult(status=STATUS_OK, request=request, files=files, building=building,
                             continue_at=continue_at,
                             context=self._excerpt_context(files, {item_id: rec}, {item_id: chain}, ancestry))

    # ------------------------------------------------------------------------------------------
    # Filter-only listings (§10.1, unchanged)
    # ------------------------------------------------------------------------------------------

    async def _filter_in_memory(self, request: DriveSearchRequest, kinds: FrozenSet[str], folder: Optional[_Folder],
                                subtree: List[DriveIndexRecord], ancestry: _Ancestry,
                                stats: _Stats) -> DriveSearchResult:
        pool = [r for r in subtree if not kinds or r.kind in kinds]
        stats.card = len(pool)
        kept, paths = await self._visible(pool, request, kinds, folder, ancestry, stats)
        kept.sort(key=lambda r: (r.effective_date or _EPOCH, r.item_id), reverse=True)
        return DriveSearchResult(status=STATUS_OK, cards=self._cards(kept, paths, stats), request=request,
                                 matched_total=len(kept), folder_path=folder.path if folder else None)

    async def _filter_only(self, user_id: str, request: DriveSearchRequest, kinds: FrozenSet[str],
                           folder: Optional[_Folder], ancestry: _Ancestry, stats: _Stats) -> DriveSearchResult:
        """§10.1: a plain query on kind / effective_date, newest first, no kNN, and how many matched. A kind
        family runs one query per member kind, merged (RFC Δ20)."""
        members: List[Optional[str]] = list(sorted(kinds)) or [None]
        fetch = CARD_FETCH
        while True:
            pages = await asyncio.gather(*(
                self._repo.filter_records(user_id, k, request.date_from, request.date_to, fetch) for k in members))
            recs = sorted((r for page in pages for r in page),
                          key=lambda r: (r.effective_date or _EPOCH, r.item_id), reverse=True)
            stats.card = len(recs)
            full = any(len(page) >= fetch for page in pages)
            kept, paths = await self._visible(recs, request, kinds, folder, ancestry, stats)
            if len(kept) >= K or stats.widened or not full:
                break
            stats.widened = True
            fetch = FETCH_MAX
        return DriveSearchResult(status=STATUS_OK, cards=self._cards(kept, paths, stats), request=request,
                                 matched_total=len(kept), matched_at_least=full,
                                 folder_path=folder.path if folder else None)

    async def _visible(self, recs: Sequence[DriveIndexRecord], request: DriveSearchRequest, kinds: FrozenSet[str],
                       folder: Optional[_Folder], ancestry: _Ancestry,
                       stats: _Stats) -> Tuple[List[DriveIndexRecord], Dict[str, str]]:
        """Read-time visibility (§5.2, §17.1 d), then the kind, date and folder post-filters (§10.1 step 4)."""
        await ancestry.load(recs)
        kept: List[DriveIndexRecord] = []
        paths: Dict[str, str] = {}
        visible = 0
        for rec in recs:
            if rec.item_id in paths:
                continue
            chain = ancestry.chain(rec)
            if chain is None:
                continue
            visible += 1
            path, ancestor_ids = chain
            stats.candidates.add(rec.item_id)
            if kinds and rec.kind not in kinds:
                continue
            if not in_date_range(rec, request):
                continue
            if folder is not None and folder.record.item_id not in ancestor_ids:
                continue
            kept.append(rec)
            paths[rec.item_id] = path
        stats.visible += visible
        stats.filtered += len(kept)
        return kept, paths

    def _cards(self, ranked: Sequence[DriveIndexRecord], paths: Dict[str, str],
               stats: _Stats) -> Tuple[DriveSearchCard, ...]:
        """Collapse identical files (same content hash) into one card with `also_at`, then cut at K. Empty files
        all share one hash, so only files with content collapse."""
        cards: List[DriveSearchCard] = []
        by_hash: Dict[str, int] = {}
        for rec in ranked:
            path = paths[rec.item_id]
            same_as = rec.content_hash if rec.size_bytes > 0 else None
            if same_as and same_as in by_hash:
                first = by_hash[same_as]
                cards[first] = replace(cards[first], also_at=cards[first].also_at + (path,))
                stats.collapsed += 1
                continue
            if len(cards) >= K:
                continue   # keep scanning: a later duplicate still adds its path to a shown card
            if same_as:
                by_hash[same_as] = len(cards)
            cards.append(make_card(rec, path, card_text(rec, owner=False), self._nonce()))
        return tuple(cards)

    # ------------------------------------------------------------------------------------------
    # Folder scope
    # ------------------------------------------------------------------------------------------

    async def _resolve_folder(self, user_id: str, wanted: str,
                              ancestry: _Ancestry) -> Union[_Folder, DriveSearchResult]:
        """By `drive:` ref, or by path after NFC + casefold: root-anchored descent first, then folders named
        like the last segment anywhere (one exact path wins, else one suffix match). Several matches are
        returned as candidates, never guessed (§10.1). Nothing found while a lookup page was full is "could
        not resolve", not "not in the index"; so is a single *suffix* match from a full name page, since
        another match may lie past the page. One exact path is unique in a drive, so it is used even then."""
        item_id = parse_drive_ref(wanted)
        if item_id is not None:
            rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
            if rec is None or not rec.is_folder:
                return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND)
            await ancestry.load([rec])
            if rec.scope is Scope.INSIDE_OPAQUE or rec.state is RecordState.DELETING or ancestry.hidden_above(rec):
                return _not_indexed(rec.name)
            chain = ancestry.chain(rec, require_card=False)
            if chain is None:
                return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND)
            return _Folder(rec, chain[0])
        segments = [p.strip() for p in wanted.replace("\\", "/").split("/") if p.strip()]
        if not segments:
            return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND)
        anchored, descent_complete, hider = await self._descend(user_id, ancestry.root_item_id, segments)
        if not anchored and hider is not None:
            return _not_indexed(hider.name)   # the path runs into a folder whose contents are not indexed
        matches = await self._as_folders(anchored, ancestry)
        page_full = not descent_complete
        guessed = False   # one suffix match from a full name page: another may sit past it (§10.1)
        if not matches:
            # name keys v2 without trigrams: words, skeletons and digits ("2026" is n:/y:, P6 review m1)
            keys = [k for k in query_name_keys_v2(segments[-1]) if not k.startswith("t:")]
            pages = [await self._repo.find_by_name_keys(user_id, b, NAME_FETCH, "folder") for b in fetch_batches(keys)]
            named = list({r.item_id: r for page in pages for r in page}.values())
            page_full_by_name = any(len(page) >= NAME_FETCH for page in pages)
            page_full = page_full or page_full_by_name
            candidates = await self._as_folders([r for r in named if r.is_folder], ancestry)
            exact = [f for f in candidates if path_match(f.path, wanted) == "exact"]
            suffix = [f for f in candidates if path_match(f.path, wanted) == "suffix"]
            matches = exact or suffix
            guessed = page_full_by_name and not exact and len(matches) == 1
        if len(matches) == 1 and not guessed:
            return matches[0]
        if not matches or guessed:
            return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND, folder_lookup_truncated=page_full)
        cards = tuple(make_card(f.record, f.path, card_text(f.record, owner=False), self._nonce())
                      for f in sorted(matches, key=lambda f: f.path)[:K])
        return DriveSearchResult(status=STATUS_AMBIGUOUS_FOLDER, cards=cards)

    async def _as_folders(self, recs: List[DriveIndexRecord], ancestry: _Ancestry) -> List[_Folder]:
        await ancestry.load(recs)
        out: List[_Folder] = []
        for rec in recs:
            chain = ancestry.chain(rec, require_card=False)
            if chain is not None:
                out.append(_Folder(rec, chain[0]))
        return out

    async def _descend(self, user_id: str, root_item_id: str, segments: Sequence[str]
                       ) -> Tuple[List[DriveIndexRecord], bool, Optional[DriveIndexRecord]]:
        """Match the path segment by segment from the drive's root through `children`, reading at most
        FOLDER_INMEM_CARDS records. Returns (matching folders, the descent read everything it needed, a folder
        hiding its contents that a middle segment matched)."""
        level = [root_item_id]
        budget = FOLDER_INMEM_CARDS
        found: List[DriveIndexRecord] = []
        hider: Optional[DriveIndexRecord] = None
        for depth, segment in enumerate(segments):
            want = normalize_path(segment)
            last = depth == len(segments) - 1
            found = []
            for parent_id in level:
                kids = await self._children_of(user_id, parent_id, budget + 1)
                budget -= len(kids)
                if budget < 0:
                    return [], False, hider
                for kid in kids:
                    if not kid.is_folder or normalize_path(kid.name) != want:
                        continue
                    if not last and kid.scope is not Scope.UNRESOLVED and hides_descendants(kid):
                        hider = hider or kid
                    elif record_visible(kid, require_card=False):
                        found.append(kid)
            if not found:
                return [], True, hider
            level = [r.item_id for r in found]
        return found, True, hider

    async def _children_of(self, user_id: str, parent_id: str, cap: int,
                           budget: Optional[List[int]] = None) -> List[DriveIndexRecord]:
        """A folder's direct children, paged, at most `cap` of them. `budget` (one mutable count shared by
        parallel listings) is checked before each page and charged after it; at zero the listing stops."""
        out: List[DriveIndexRecord] = []
        after: Optional[str] = None
        while len(out) < cap:
            want = min(_CHILD_PAGE, cap - len(out))
            if budget is not None:
                if budget[0] <= 0:
                    break
                want = min(want, budget[0])
            page = await self._repo.children(user_id, parent_id, after, want)
            out.extend(page)
            if budget is not None:
                budget[0] -= len(page)
            if len(page) < want:
                break
            after = page[-1].item_id
        return out

    async def _walk(self, user_id: str, folder: DriveIndexRecord) -> Tuple[List[DriveIndexRecord], bool]:
        """The folder's subtree, level by level, up to _WALK_PARALLEL folders listed at once, never entering
        an opaque or deleting folder; stops once more than FOLDER_INMEM_CARDS records are read. One budget is
        shared by every listing and checked before each page, so the walk overshoots by at most one page
        per parallel listing. Returns (records, complete)."""
        out: List[DriveIndexRecord] = []
        budget = [FOLDER_INMEM_CARDS + 1]   # records left to read before the walk is known incomplete
        level = [folder.item_id]
        while level:
            below: List[str] = []
            for i in range(0, len(level), _WALK_PARALLEL):
                cap = FOLDER_INMEM_CARDS - len(out) + 1
                pages = await asyncio.gather(*(self._children_of(user_id, pid, cap, budget)
                                               for pid in level[i:i + _WALK_PARALLEL]))
                for page in pages:
                    out.extend(page)
                    below.extend(r.item_id for r in page if r.is_folder and not hides_descendants(r))
                if len(out) > FOLDER_INMEM_CARDS:
                    return out, False
            level = below
        return out, True

    # ------------------------------------------------------------------------------------------
    # Envelope and status
    # ------------------------------------------------------------------------------------------

    async def _envelope(self, user_id: str, state: DriveSyncState, reconnect_needed: bool,
                        use_cards: bool) -> DriveSearchEnvelope:
        unread = int(state.counters.get(UNREAD_COUNTER, 0))
        waiting = int(state.counters.get(NOT_READ_YET_COUNTER, 0))
        return DriveSearchEnvelope(
            building=await self._building(user_id, state), reconnect_needed=reconnect_needed,
            refreshing=not use_cards, content_search=content_admitted(),
            index_as_of=state.observed_as_of, unread=max(0, unread - waiting), not_read_yet=waiting,
            # the flip's upgrade pass re-plans records it has not reached yet: reading whatever the counters say (F4-1)
            reading=int(state.applied_versions.get("policy", POLICY_VERSION)) != POLICY_VERSION,
            index_full=int(state.counters.get("carded", 0)) >= MAX_CARD_RECORDS_PER_USER,
            inaccessible=int(state.counters.get(INACCESSIBLE_COUNTER, 0)),
        )

    async def _closed_in_scope(self, user_id: str, state: DriveSyncState, folder: Optional[_Folder],
                               subtree: Optional[List[DriveIndexRecord]], ancestry: _Ancestry) -> int:
        """Files the bot cannot open that this search's scope holds (B-minor 2): all of them for an unscoped search
        (the tick's counter), inside the folder for a folder-scoped one."""
        total = int(state.counters.get(INACCESSIBLE_COUNTER, 0))
        if folder is None or total == 0:
            return total
        if subtree is not None:
            return sum(1 for r in subtree if inaccessible(r))
        marked = await self._repo.inaccessible_records(user_id, INACCESSIBLE_SCAN_MAX, lean=True)
        await ancestry.load(marked)
        top = folder.record.item_id
        count = 0
        for rec in marked:
            parent_id, seen = rec.parent_id, {rec.item_id}
            while parent_id and parent_id not in seen and len(seen) <= MAX_CHAIN_DEPTH:
                if parent_id == top:
                    count += 1
                    break
                parent = ancestry.cache.get(parent_id)
                if parent is None:
                    break
                seen.add(parent_id)
                parent_id = parent.parent_id
        return count

    async def access_note(self, user_id: str, *, item_id: Optional[str] = None,
                          path: Optional[str] = None) -> Optional[str]:
        """The search card's sentence for an item the index knows the bot cannot open (§26.2, Δ55), by id or by folder
        path (resolved in the index as a search scope is); None for an item the index does not know or can read, so
        the caller keeps its own answer."""
        rec: Optional[DriveIndexRecord] = None
        if item_id is not None:
            rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        elif path:
            state = await self._repo.get_sync_state(user_id)
            if state is None or not state.root_item_id:
                return None
            found = await self._resolve_folder(user_id, path, _Ancestry(self._repo, user_id, state.root_item_id))
            rec = found.record if isinstance(found, _Folder) else None
        if rec is None or not inaccessible(rec):
            return None
        return ACCESS_NOTE_FOLDER if rec.is_folder else ACCESS_NOTE_FILE

    async def transcript_text(self, user_id: str, item_id: str, *, path: str = "",
                              label: str = "") -> Optional[str]:
        """What opening a transcribed recording returns (§27.11): the transcript, or the stub's card line, inside the
        nonce wrapper under the reference reminder, capped at `TRANSCRIPT_OPEN_MAX_CHARS` (G19). None when the record
        has no current transcript, so the caller keeps its own branch (Δ121). Speech is other people's: never logged."""
        rec = (await self._repo.get_records(user_id, [item_id])).get(item_id)
        if rec is None or rec.transcript_state != JobState.CURRENT.value:
            return None
        if rec.transcript_stub:
            body = card_segment(rec)
        elif rec.transcript_key:
            stored = await self._repo.get_transcript(user_id, rec.transcript_key)
            body = None
            if stored is not None:
                # the file's path is looked up only when the cut note will use it
                mirror_path = (await self._active_mirror_path(user_id, item_id)
                               if open_needs_cut(stored, max_chars=TRANSCRIPT_OPEN_MAX_CHARS) else "")
                body = open_text(stored, max_chars=TRANSCRIPT_OPEN_MAX_CHARS, mirror_path=mirror_path)
        else:
            body = None
        if not body:
            return None
        wrapped = wrap_untrusted(f"{label}\n{body}" if label else body, nonce=self._nonce(), ref=f"drive:{item_id}", path=path)
        return f"{REFERENCE_REMINDER}\n{wrapped}"

    # ------------------------------------------------------------------------------------------
    # The transcript mirror's read side (§27.9.11)
    # ------------------------------------------------------------------------------------------

    def add_deleted_source(self, source: DeletedSource) -> None:
        """Add a source to the deleted-files section (the owner-notes RFC's, §6.4): one flag, one section."""
        self._deleted_sources.append(source)

    async def search_deleted(self, user_id: str, drive_query: str) -> Tuple[str, List[DriveItem]]:
        """The deleted-files search: the heading `Deleted files (N):` and one wrapped entry per hit,
        for the orchestrator, and the drive files the shown refs open (so "open the second one" works next turn). Runs before (and
        without) the index gate: with drive search off it still answers from the ledger. An empty query lists the
        newest. Speech is other people's: entries go inside the nonce wrapper, never logged."""
        concepts = query_concepts(drive_query or "").concepts
        try:
            connection = await self._drive.connection(user_id)
        except Exception as exc:
            logger.warning(f"[DriveSearch] deleted-files search: connection lookup failed: {exc_brief(exc)}")
            connection = DriveConnection(connected=False)
        hits: List[DeletedHit] = []
        coverage: List[str] = []
        for source in self._deleted_sources:
            found, lines = await source(user_id, concepts, connection)
            hits.extend(found)
            coverage.extend(lines)
        hits = merge_hits(hits)   # an item with an archived transcript and a note is one entry (notes RFC §0.3)
        logger.info(f"[DriveSearch] user={user_id[:8]} deleted-files hits={len(hits)} coverage={len(coverage)} "
                    f"concepts={len(concepts)} connected={connection.connected}")
        text = render_deleted(hits, connected=connection.connected and not connection.needs_reconnect,
                              coverage=coverage)
        return text, [h.item for h in hits if h.item is not None]

    async def _deleted_transcripts(self, user_id: str, concepts: Sequence[Concept],
                                   connection: DriveConnection) -> Tuple[List[DeletedHit], List[str]]:
        if self._mirror is None:
            return [], []
        candidates = await self._archived_candidates(user_id, concepts)
        checkable = connection.connected and not connection.needs_reconnect
        shown: List[Tuple[MirrorDoc, bool, bool]] = []     # (document, on the connected drive, verified)
        index = 0
        check_cap = 2 * DELETED_SHOW_MAX                   # a hit the owner deleted is replaced by the next, within reason
        while index < len(candidates) and len(shown) < DELETED_SHOW_MAX and index < check_cap:
            batch = candidates[index:index + min(DELETED_SHOW_MAX - len(shown), check_cap - index)]
            index += len(batch)
            verdicts = await asyncio.gather(*(self._verify_archived(user_id, d, connection, checkable) for d in batch))
            shown.extend((d, on_drive, verified) for d, (keep, on_drive, verified) in zip(batch, verdicts) if keep)
        wanted = {k for c in concepts for k in c.word_keys}
        roots = {r.root_item_id: r.root_name for r in await self._mirror.roots(user_id)}
        folders: Dict[str, Optional[MirrorDoc]] = {}
        stored_all = await asyncio.gather(*(self._stored(user_id, d) for d, _, _ in shown))
        hits: List[DeletedHit] = []
        for (doc, on_drive, verified), stored in zip(shown, stored_all):
            archive_path, _ = await self._mirror_chain(user_id, doc, roots, folders)
            hits.append(DeletedHit(
                name=doc.source_name, path=doc.source_path, day=doc.archive_day,
                transcript_line=transcript_line(doc.seconds, doc.speakers, doc.languages),
                passage=(passage(stored, wanted) or "") if stored is not None and wanted else "",
                refs=(f"archived transcript: drive:{doc.mirror_item_id} ({archive_path})",),
                on_connected_drive=on_drive, verified=verified, nonce=self._nonce(),
                ref=f"drive:{doc.mirror_item_id}",
                item=DriveItem(item_id=doc.mirror_item_id, name=doc.mirror_name, path=archive_path, is_folder=False),
                item_key=f"{doc.drive_id}:{doc.source_item_id}" if doc.source_item_id else ""))
        rest = len(candidates) - index
        return hits, ([f"{rest} more archived transcripts matched."] if rest > 0 else [])

    async def _archived_candidates(self, user_id: str, concepts: Sequence[Concept]) -> List[MirrorDoc]:
        """Archived transcript files matching every concept (strict AND over `archive_keys` and `name_keys`), newest
        first; with no concept at all the newest ten (§27.9.11)."""
        if self._mirror is None:
            return []
        if not concepts:
            newest = await self._mirror.archived_newest(user_id, ARCHIVE_QUERY_MAX)
            return [d for d in newest if d.kind == KIND_FILE][:10]
        keys = driver_keys(concepts)
        by_archive, by_name = await asyncio.gather(
            self._mirror.archived_by_keys(user_id, ARCHIVE_FAMILY, keys, ARCHIVE_QUERY_MAX),
            self._mirror.archived_by_keys(user_id, NAME_FAMILY, keys, ARCHIVE_QUERY_MAX))
        found: Dict[str, MirrorDoc] = {}
        for doc in (*by_archive, *by_name):
            if doc.kind == KIND_FILE and deleted_matches(concepts, set(doc.archive_keys) | set(doc.name_keys)):
                found[doc.key] = doc
        return sorted(found.values(), key=lambda d: (d.archived_at or _EPOCH, d.key), reverse=True)

    async def _verify_archived(self, user_id: str, doc: MirrorDoc, connection: DriveConnection,
                               checkable: bool) -> Tuple[bool, bool, bool]:
        """(keep, on the connected drive, verified). One `get_change` of the mirror file: not found or deleted means
        the owner removed it, so it is not shown. A document of another drive, or an unreachable drive, is shown unchecked."""
        if connection.drive_id and doc.drive_id != connection.drive_id:
            return True, False, False
        if not checkable:
            return True, True, False
        try:
            change = await self._drive.get_change(user_id, doc.mirror_item_id)
        except DriveItemNotFoundError:
            return False, True, True
        except Exception as exc:
            logger.warning(f"[DriveSearch] deleted-files search: archive check failed: {exc_brief(exc)}")
            return True, True, False
        return (not change.deleted), True, True

    async def _stored(self, user_id: str, doc: MirrorDoc) -> Optional[Any]:
        if not doc.content_key:
            return None
        try:
            return await self._repo.get_transcript(user_id, doc.content_key)
        except Exception as exc:
            logger.warning(f"[DriveSearch] deleted-files search: store read failed: {exc_brief(exc)}")
            return None

    async def _mirror_chain(self, user_id: str, doc: MirrorDoc, roots: Mapping[str, str],
                            folders: Dict[str, Optional[MirrorDoc]]) -> Tuple[str, bool]:
        """The file's path in the bot's folder (`_Transcripts/Archive/<day>/…/<name>`), built from the ledger's
        `mirror_parent_id` chain, and whether the chain reached a root; a chain that breaks ends at what is known."""
        if self._mirror is None:
            return doc.mirror_name, False
        parts = [doc.mirror_name]
        parent = doc.mirror_parent_id
        complete = False
        for _ in range(_MIRROR_CHAIN_MAX):
            if not parent:
                break
            if parent in roots:
                parts.append(roots[parent])
                complete = True
                break
            if parent not in folders:
                folders[parent] = await self._mirror.by_mirror_item(user_id, parent)
            folder = folders[parent]
            if folder is None:
                break
            parts.append(folder.mirror_name)
            parent = folder.mirror_parent_id
        return "/".join(p for p in reversed(parts) if p), complete

    async def _mirror_path(self, user_id: str, doc: MirrorDoc, roots: Mapping[str, str],
                           folders: Dict[str, Optional[MirrorDoc]]) -> str:
        return (await self._mirror_chain(user_id, doc, roots, folders))[0]

    async def has_mirror_root(self, user_id: str) -> bool:
        """The user has a `_Transcripts` folder on some drive (the Cabinet's disclosure clause, Δ202)."""
        return self._mirror is not None and bool(await self._mirror.roots(user_id))

    async def _active_mirror_path(self, user_id: str, item_id: str) -> str:
        """The path of a source recording's transcript file in `_Transcripts`, for an open's cut note; "" without one
        (no mirror, no file yet, or a failed lookup: the note then names nothing)."""
        if self._mirror is None:
            return ""
        try:
            key = doc_key(user_id, KIND_FILE, item_id)
            doc = (await self._mirror.get_docs(user_id, [key])).get(key)
            if doc is None or not doc.mirror_item_id or doc.state != MIRROR_ACTIVE:
                return ""
            roots = {r.root_item_id: r.root_name for r in await self._mirror.roots(user_id)}
            return await self._mirror_path(user_id, doc, roots, {})
        except Exception as exc:
            logger.warning(f"[DriveSearch] mirror path lookup failed: {exc_brief(exc)}")
            return ""

    async def archived_text(self, user_id: str, mirror_item_id: str, *, label: str = "", path: str = "",
                            file_text: Optional[Callable[[], Awaitable[str]]] = None) -> Optional[str]:
        """What opening a transcript file of the mirror returns (§27.9.11): `open_text` of the pinned store entry, capped
        at `TRANSCRIPT_OPEN_MAX_CHARS`, the cut note naming the file's path, inside the nonce wrapper under the
        reference reminder. With no store entry, `file_text` (the downloaded file) is capped the same way. None when
        the item is no mirror file (the caller opens it as any file). Speech is other people's: never logged."""
        if self._mirror is None:
            return None
        doc = await self._mirror.by_mirror_item(user_id, mirror_item_id)
        if doc is None or doc.kind != KIND_FILE:
            return None
        roots = {r.root_item_id: r.root_name for r in await self._mirror.roots(user_id)}
        chain_path, complete = await self._mirror_chain(user_id, doc, roots, {})
        mirror_path = chain_path if complete else (path or chain_path)   # a broken ledger chain: the item's own path
        stored = await self._stored(user_id, doc)
        body: Optional[str] = None
        if stored is not None:
            body = open_text(stored, max_chars=TRANSCRIPT_OPEN_MAX_CHARS, mirror_path=mirror_path) or None
        if body is None and file_text is not None:
            body = cap_plain_text(await file_text(), max_chars=TRANSCRIPT_OPEN_MAX_CHARS, path=mirror_path)
        if not body:
            return None
        wrapped = wrap_untrusted(f"{label}\n{body}" if label else body, nonce=self._nonce(),
                                 ref=f"drive:{mirror_item_id}", path=path or mirror_path)
        return f"{REFERENCE_REMINDER}\n{wrapped}"

    async def _building(self, user_id: str, state: DriveSyncState) -> bool:
        """The set of items may be incomplete (§10.2): an epoch not idle, a first-seen or re-listing record
        due (class 2, RFC Δ18), or an unresolved record being resolved at CASCADE priority — limit-1 queries,
        never a counter. Edits and other cascades do not count; nor does an unresolved record waiting on a
        RETRY-class lookup or parked, or a parent that is never found would keep `building` true for good."""
        if state.epoch_phase is not EpochPhase.IDLE:
            return True
        first_seen, resolving = await asyncio.gather(
            self._repo.oldest_due_at(user_id, int(Priority.FIRST_SEEN)),
            self._repo.has_record_in(user_id, RecordState.UNRESOLVED, int(Priority.CASCADE)))
        return first_seen is not None or resolving

    async def indexing_status(self, user_id: str) -> DriveIndexingStatus:
        """The Cabinet card (§6): counters, last sync, error category, index full, building."""
        profile, state = await asyncio.gather(self._users.get_user(user_id), self._repo.get_sync_state(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        if state is None:
            return DriveIndexingStatus(enabled=enabled, counters={}, building=enabled)
        now = self._clock()
        usage, build = await asyncio.gather(self._repo.get_model_usage(user_id, month_key(now)),
                                            self._repo.get_initial_build(user_id))
        return DriveIndexingStatus(
            awaiting_rebuild=MIGRATION_GATE_KEY not in state.applied_versions and not state.purging,
            enabled=enabled, counters=dict(state.counters), last_success_at=state.last_success_at,
            last_error=state.last_error.value if state.last_error else None, index_as_of=state.observed_as_of,
            index_full=int(state.counters.get("carded", 0)) >= MAX_CARD_RECORDS_PER_USER,
            building=enabled and (state.purging or not state.root_item_id or await self._building(user_id, state)),
            access_lost_at=state.access_lost_at, purging=state.purging,
            readable_chunks=await self._repo.readable_chunk_total(user_id),
            # what each month's cap has paid: the initial build's dollars are its own line, never against a cap (XC-4)
            usd_month={c.value: usage.cap_usd(c.value) for c in UsageClass},
            # the transcription cap as the indexer applies it: the optional knob only lowers it (T-R3)
            usd_caps={**USD_CAPS, UsageClass.TRANSCRIBE.value: self._transcribe_cap},
            initial_build_spent_usd=build.spent_usd, initial_build_allowance_usd=build.allowance_usd,
            initial_build_closed=build.closed_at is not None,
        )
