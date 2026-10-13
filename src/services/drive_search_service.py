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
tier, the rest grouped; the coverage line names what the search could not see. A sync state not rebuilt under v2
(no `keys` in `applied_versions`, D30) is searched by names, meaning and filters only, and says so (the migration
guard). No model call at search time (D19, D28). Decisions that need no I/O are in `drive_tiers`,
`drive_tier_pool`, `drive_tier_explain` and `drive_search`; this class reads and calls them.

Public interface:
    DriveSearchService(repo, embeddings, users, drive, *, embedding_space, clock, nonce, monotonic)
    await search(user_id, request) -> DriveSearchResult
    await indexing_status(user_id) -> DriveIndexingStatus     # Cabinet (§6)
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..domain.drive_card import card_text
from ..domain.drive_enrichment import EnrichJob, HintItem, JobState
from ..domain.drive_index import (
    KEYS_VERSION, MIGRATION_GATE_KEY, POLICY_VERSION, DriveIndexRecord, DriveSyncState, EpochPhase, Priority,
    RecordState, Scope,
)
from ..domain.drive_index_lean import ChunkId
from ..domain.drive_index_params import (
    ABOUT_FETCH, ABOUT_SHOWN, CARD_FETCH, CHUNK_FETCH, FETCH_MAX, FOLDER_INMEM_CARDS, FOLDER_INMEM_CHUNKS,
    GROUP_READ_MAX, HINT_FETCH, K, LABELLED_SHOWN, MAX_CARD_RECORDS_PER_USER, MENTIONED_SHOWN, NAME_FETCH,
    NAME_KEY_QUERIES, PROFILE_MIN_CHARS, RELATED_MAX, RELATED_SMALL, TRANSLATED_SHOWN, WINDOW_CHARS_TOTAL_V2,
    WINDOW_MAX, WORD_FETCH, WORD_FILES_MAX,
)
from ..domain.drive_index_planner import usage_now
from ..domain.drive_indexing_status import DriveIndexingStatus
from ..domain.drive_keys import fetch_batches, query_concepts, query_name_keys_v2
from ..domain.drive_match import Concept, KeyFamily, MatchTier, SourceClass
from ..domain.drive_name_keys import covers_query, name_match, query_name_keys, query_token_groups
from ..domain.drive_search import (
    MAX_CHAIN_DEPTH, NOT_READ_YET_COUNTER, STATUS_AMBIGUOUS_FOLDER, STATUS_FOLDER_NOT_FOUND, STATUS_OK, UNREAD_COUNTER,
    DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, content_admitted, cosine_distance,
    hides_descendants, in_date_range, kinds_for, make_card, normalize_path, path_match, record_visible,
    KIND_NAMES, render_results, resolve_chain, search_gate,
)
from ..domain.drive_tier_explain import explain, not_matched, passage_window
from ..domain.drive_tier_pool import ConceptHits, candidate_ids, document_frequency, tier_order_key
from ..domain.drive_tier_result import CoverageFacts, GroupMember, TierBlock, TieredResult
from ..domain.drive_tiers import (
    WHERE_HINT, WHERE_NAME, Allowance, Cover, FileKeys, FileMatch, allowances, chunks_needing_text, classify,
    meaning_scores, name_query_keys, related_floor, size_band, source_scores,
)
from ..domain.user_drive import parse_drive_ref
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_CHILD_PAGE = 200
_WALK_PARALLEL = 16   # folders listed at once, per level of a subtree walk
_READ_PARALLEL = 16   # files whose chunk text or hint items are read at once
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)
_SHOWN: Mapping[MatchTier, int] = {MatchTier.ABOUT: ABOUT_SHOWN, MatchTier.MENTIONED: MENTIONED_SHOWN,
                                   MatchTier.TRANSLATED: TRANSLATED_SHOWN, MatchTier.LABELLED: LABELLED_SHOWN}
_TIERS: Tuple[MatchTier, ...] = (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED, MatchTier.LABELLED)
_PASSAGE_TIERS = frozenset({MatchTier.MENTIONED, MatchTier.TRANSLATED})
# Text kinds whose enrichment jobs the coverage line counts (§25.5.6): never folders, photos (their jobs are `none`),
# audio, video or other kinds that hold no text.
_NO_TEXT_KINDS = frozenset({"folder", "audio", "video", "archive", "code", "log", "data_dump", "database",
                            "executable", "disk_image", "other", "iwork"})
_NO_PASSAGE_KINDS = frozenset({"epub", "xlsx", "xls", "ods", "csv", "json"})   # no doc2query by policy (§25.3)
_NOT_DESCRIBED_STATES = (JobState.READY.value, JobState.DEFERRED.value, JobState.PARKED.value)
_VALIDITY = KeyFamily.VALIDITY_WORD.value
_ALL_KINDS = KIND_NAMES

Chain = Optional[Tuple[str, Tuple[str, ...]]]   # (path, ancestor ids) or None when hidden
Rec = Any   # a DriveIndexRecord or a DriveRecordLean: both carry the fields visibility, filters and coverage read


def _not_indexed(folder_name: str) -> DriveSearchResult:
    return DriveSearchResult(status=STATUS_OK, folder_path=folder_name, folder_not_indexed=True)


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
    migration: bool = False
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

    def add(self, rec: Rec) -> None:
        if isinstance(rec, DriveIndexRecord):
            self.fulls[rec.item_id] = rec
        self.leans.setdefault(rec.item_id, rec)


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




class DriveSearchService:
    def __init__(self, repo: DriveIndexRepository, embeddings: EmbeddingService, users: UserRepository,
                 drive: UserDrivePort, *, embedding_space: str, clock: Callable[[], datetime] = _utcnow,
                 nonce: Callable[[], str] = _nonce, monotonic: Callable[[], float] = time.monotonic) -> None:
        """`embedding_space` is the space query vectors are embedded in (the adapter's EMBEDDING_SPACE). The
        kNN streams run only while the index's cards are in it (§12.3)."""
        self._repo = repo
        self._embeddings = embeddings
        self._users = users
        self._drive = drive
        self._clock = clock
        self._nonce = nonce
        self._monotonic = monotonic
        self._space = embedding_space

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
            f"content={stats.content} migration={stats.migration} concepts={stats.concepts} "
            f"ignored={stats.ignored} common={stats.common} absent={stats.absent} {tiers} related={stats.related} "
            f"card={stats.card} chunk={stats.chunk} hint={stats.hint} name={stats.name} "
            f"name_queries={stats.name_queries} about_queries={stats.about_queries} "
            f"word_queries={stats.word_queries} hint_queries={stats.hint_queries} "
            f"tested={stats.candidates_tested} pool_card={stats.pool_card} pool_chunk={stats.pool_chunk} "
            f"pool_hint={stats.pool_hint} band={stats.band} coverage={stats.coverage} chars={stats.chars} "
            f"visible={stats.visible} filtered={stats.filtered} collapsed={stats.collapsed} "
            f"returned={stats.returned} building={result.envelope.building} ms={ms}")
        return result

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
        use_cards = state.cards_in_space(self._space)
        envelope, result = await asyncio.gather(self._envelope(user_id, state, gate.reconnect_needed, use_cards),
                                                self._body(user_id, request, state, use_cards, stats))
        result = replace(result, envelope=envelope)
        if result.tiered is not None:
            coverage = replace(result.tiered.coverage, building=envelope.building, reading=envelope.reading,
                               refreshing=envelope.refreshing, not_read_yet=envelope.not_read_yet,
                               unread=envelope.unread, names_only=not envelope.content_search)
            stats.coverage = "complete" if coverage == CoverageFacts() else "incomplete"
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
                     use_cards: bool, stats: _Stats) -> DriveSearchResult:
        q = query_concepts(request.query)
        concepts = list(q.concepts)
        migration = MIGRATION_GATE_KEY not in state.applied_versions   # D30: a state not rebuilt under v2
        stats.migration, stats.concepts, stats.ignored = migration, len(concepts), len(q.ignored)
        with_content = content_admitted() and not migration
        stats.content = "off" if not with_content else "wide"
        members: List[Optional[str]] = list(sorted(kinds)) or [None]   # a family: one kNN per member kind
        pool = _Pool(hits=[ConceptHits() for _ in concepts])
        # the migration guard reads v1 name keys the way v1 did; a rebuilt index has name keys v2
        name_keys = query_name_keys(request.query) if migration else query_name_keys_v2(request.query)
        if subtree is not None:
            carded = sum(1 for r in subtree if r.has_card) or 1
            await self._memory_pool(user_id, request.query, name_keys, concepts, subtree, pool, ancestry,
                                    with_content, use_cards, members, stats)
        else:
            carded = max(int(state.counters.get("carded", 0)), 1)
            await self._wide_pool(user_id, request, name_keys, concepts, members, pool, with_content, use_cards,
                                  stats)
        await self._records(user_id, pool)
        self._fence(pool)
        scope = await self._scope(pool, request, kinds, folder, ancestry)
        stats.band = size_band(carded)

        df, full = document_frequency(pool.hits)
        in_scope = [h.files() & scope.passing for h in pool.hits]
        if migration:
            absent: List[str] = []
            absent_in_scope: List[str] = []
        else:
            absent = [c.word for c, n in zip(concepts, df) if n == 0 and subtree is None]
            absent_in_scope = [c.word for c, n, s in zip(concepts, df, in_scope)
                               if c.word not in absent and not s and (scope.filtered or subtree is not None)]
        blocked = set(absent) | set(absent_in_scope)
        stats.absent = len(blocked)
        others = [c for c in concepts if c.word not in blocked]
        df_of = {c.word: n for c, n in zip(concepts, df)}
        allowance = Allowance() if migration else allowances(others, df_of, carded)
        stats.common = len(allowance.common)
        tested = [i for i, c in enumerate(concepts) if c.word not in blocked]
        if migration:
            matches = self._name_matches(request.query, concepts, pool, scope)
        else:
            matches = await self._matches(user_id, pool, [concepts[i] for i in tested],
                                          [pool.hits[i] for i in tested], [in_scope[i] for i in tested],
                                          [df[i] for i in tested], allowance, stats)
        page_full = any(full[i] for i in tested) or pool.name_full
        without: Optional[int] = None
        if blocked:
            without = len(matches) if others else None
            matches = {}

        meaning = self._meaning(pool, scope, stats)
        today = self._clock().date()
        tiered_ids, blocks, expired = self._tiers(matches, pool, scope, meaning, page_full, today)
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
        if not migration and not blocked and not matches and len(concepts) >= 2:
            conjunction = tuple((c.word, len(s)) for c, s in zip(concepts, in_scope))
        keys_v = state.applied_versions.get(MIGRATION_GATE_KEY)
        not_described, not_describable, no_passages = await self._job_counts(user_id)
        coverage = CoverageFacts(
            rekeying=keys_v is not None and int(keys_v) != KEYS_VERSION, full_page=page_full,
            keys_cut=sum(1 for c in built if c.keys_cut), cut=q.cut, not_described=not_described,
            not_describable=not_describable, no_passages=no_passages)
        tiered = TieredResult(
            concepts=tuple(c.word for c in concepts), ignored=q.ignored,
            common=tuple(c.word for c in concepts if c.word in allowance.common), migration=migration,
            coverage=coverage, blocks=tuple(blocks[t] for t in _TIERS if t in blocks),
            related_small=related_small, absent=tuple(absent), absent_in_scope=tuple(absent_in_scope),
            without=without, without_at_least=bool(blocked) and page_full, conjunction=conjunction,
            expired=expired)
        return DriveSearchResult(status=STATUS_OK, cards=built, request=request,
                                 folder_path=folder.path if folder else None, tiered=tiered)

    # -- streams --------------------------------------------------------------------------------

    async def _wide_pool(self, user_id: str, request: DriveSearchRequest, name_keys: Sequence[str],
                         concepts: Sequence[Concept], members: Sequence[Optional[str]], pool: _Pool,
                         with_content: bool, use_cards: bool, stats: _Stats) -> None:
        """The user-wide streams (§25.5.2), all in parallel: about, word and hint pages per concept, the name stream,
        and the kNN pages. Kind, date and folder filters are post-filters (the about stream also reads its filtered
        composite past a full page), so `df` is user-wide."""
        single_kind = members[0] if len(members) == 1 else None
        about = [self._about_hits(user_id, c, request, single_kind, stats) for c in concepts] if with_content else []
        words = [self._word_hits(user_id, c, stats) for c in concepts] if with_content else []
        hints = [self._hint_hits(user_id, c, stats) for c in concepts] if with_content else []
        results = await asyncio.gather(
            asyncio.gather(*about), asyncio.gather(*words), asyncio.gather(*hints),
            self._name_candidates(user_id, name_keys, NAME_FETCH, None, stats),
            self._knn(user_id, request.query, members, use_cards, content_admitted(), pool, stats))
        about_pages, word_pages, hint_pages, (named, name_full), _ = results
        stats.content = "wide" if with_content else "off"
        for i, (recs, full) in enumerate(about_pages):
            for rec in recs:
                pool.add(rec)
                pool.hits[i].about.add(rec.item_id)
            pool.hits[i].full |= full
        for i, (chunks, full) in enumerate(word_pages):
            for cid in chunks:
                pool.hits[i].words.setdefault(cid[0], set()).add(cid)
            pool.hits[i].full |= full
        for i, (docs, full) in enumerate(hint_pages):
            for doc in docs:
                cid = (doc.item_id, doc.generation, doc.index)
                pool.hint_keys[cid] = frozenset(doc.hint_keys)
                pool.hint_checked.add(cid)
                pool.hits[i].hints.setdefault(doc.item_id, set()).add(cid)
            pool.hits[i].full |= full
        self._add_names(named, name_full, concepts, pool, stats)

    def _add_names(self, named: Sequence[DriveIndexRecord], name_full: bool, concepts: Sequence[Concept],
                   pool: _Pool, stats: _Stats) -> None:
        stats.name = len(named)
        pool.name_full = name_full
        for rec in named:
            pool.add(rec)
            pool.names[rec.item_id] = rec
            tokens = set(rec.name_tokens)
            for i, concept in enumerate(concepts):
                if tokens & name_query_keys(concept):
                    pool.hits[i].names.add(rec.item_id)

    async def _about_hits(self, user_id: str, concept: Concept, request: DriveSearchRequest,
                          single_kind: Optional[str], stats: _Stats) -> Tuple[List[Rec], bool]:
        """One concept's about pages (§25.5.2), one per key batch of at most 30: newest first, ABOUT_FETCH a page,
        read on through the cursor up to GROUP_READ_MAX when full. Past a full page under a single-kind or date
        filter, the filtered composite is read too, so a filtered search still sees its files. A concept covered by
        validity words also reads the soonest-expiring records (`find_expiring`, §25.5.4)."""
        filtered = single_kind is not None or request.date_from is not None or request.date_to is not None

        async def batch(keys: Tuple[str, ...]) -> Tuple[List[Rec], bool]:
            out: List[Rec] = []
            after: Optional[str] = None
            while True:
                page = await self._repo.find_by_about_keys(user_id, keys, ABOUT_FETCH, after=after)
                stats.about_queries += 1
                out.extend(page)
                if len(page) < ABOUT_FETCH:
                    return out, False
                if len(out) >= GROUP_READ_MAX:
                    break
                after = page[-1].item_id
            if filtered:
                out.extend(await self._repo.find_by_about_keys(
                    user_id, keys, ABOUT_FETCH, kind=single_kind, date_from=request.date_from,
                    date_to=request.date_to))
                stats.about_queries += 1
            return out, True

        pages = await asyncio.gather(*(batch(b) for b in fetch_batches(concept.about_keys)))
        recs = [r for page, _ in pages for r in page]
        full = any(f for _, f in pages)
        validity = [k for k in concept.about_keys if k.startswith(_VALIDITY)]
        if validity and any(set(r.about_keys) & set(validity) for r in recs):
            expiring = await asyncio.gather(*(self._repo.find_expiring(user_id, b, self._clock().date(), ABOUT_FETCH)
                                              for b in fetch_batches(validity)))
            stats.about_queries += len(expiring)
            recs += [r for page in expiring for r in page]
        return recs, full

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

    async def _memory_pool(self, user_id: str, query: str, name_keys: Sequence[str], concepts: Sequence[Concept],
                           subtree: Sequence[DriveIndexRecord], pool: _Pool, ancestry: _Ancestry, with_content: bool,
                           use_cards: bool, members: Sequence[Optional[str]], stats: _Stats) -> None:
        """A folder whose subtree fits FOLDER_INMEM_CARDS (§25.5.5): its records' about and name keys in memory, and
        its chunks' word keys, hint documents and vectors while they fit FOLDER_INMEM_CHUNKS; past that the user-wide
        word, hint and kNN streams run under the folder post-filter."""
        for rec in subtree:
            pool.add(rec)
        named = [r for r in subtree if name_match(r.name_tokens, name_keys) is not None]
        self._add_names(named, False, concepts, pool, stats)
        if with_content:
            for rec in subtree:
                held = set(rec.about_keys)
                for i, concept in enumerate(concepts):
                    if held & set(concept.about_keys):
                        pool.hits[i].about.add(rec.item_id)
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

    async def _name_candidates(self, user_id: str, keys: Sequence[str], limit: int, kind: Optional[str],
                               stats: _Stats) -> Tuple[List[DriveIndexRecord], bool]:
        """The name stream's pool (§10.1 step 3a, RFC Δ17): one query per whole-token key and one per
        trigram key, each in query order and at most NAME_KEY_QUERIES of each kind, plus one any-key query
        for the remaining keys (at most NAME_QUERY_KEYS_MAX), all in parallel. A record sharing a query key
        held by at most `limit` records is always in the pool. Returns (records, any page full)."""
        whole = [k for k in keys if not k.startswith("t:")][:NAME_KEY_QUERIES]
        grams = [k for k in keys if k.startswith("t:")][:NAME_KEY_QUERIES]
        rest = [k for k in keys if k not in whole and k not in grams]
        batches = [[k] for k in whole + grams] + ([rest] if rest else [])
        pages = await asyncio.gather(*(self._repo.find_by_name_keys(user_id, b, limit, kind) for b in batches))
        stats.name_queries += len(batches)
        merged: Dict[str, DriveIndexRecord] = {}
        for page in pages:
            for rec in page:
                merged.setdefault(rec.item_id, rec)
        return list(merged.values()), any(len(page) >= limit for page in pages)

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
                pool.fulls[rec.item_id] = rec
                pool.leans[rec.item_id] = rec

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

    async def _scope(self, pool: _Pool, request: DriveSearchRequest, kinds: FrozenSet[str],
                     folder: Optional[_Folder], ancestry: _Ancestry) -> _Scope:
        """Read-time visibility (§5.2, §17.1 d) of every record named, then the kind, date and folder filters."""
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
            if not in_date_range(rec, request):
                continue
            if folder is not None and folder.record.item_id not in ancestors:
                continue
            scope.passing.add(item)
        return scope

    # -- coverage and tiers ---------------------------------------------------------------------

    @staticmethod
    def _name_matches(query: str, concepts: Sequence[Concept], pool: _Pool, scope: _Scope) -> Dict[str, FileMatch]:
        """The migration guard (D30): an index not rebuilt under v2 holds v1 name keys only, so a file is About when its
        name covers every query token the v1 way (RFC Δ17's cover); meaning and similar names fill Related."""
        groups = query_token_groups(query)
        covers = tuple(Cover(c.word, SourceClass.VERIFIED_RECORD, WHERE_NAME, ()) for c in concepts)
        return {item: FileMatch(item, MatchTier.ABOUT, covers) for item, rec in pool.names.items()
                if item in scope.passing and covers_query(rec.name_tokens, groups)}

    async def _matches(self, user_id: str, pool: _Pool, concepts: Sequence[Concept], hits: Sequence[ConceptHits],
                       in_scope: Sequence[Set[str]], df: Sequence[int], allowance: Allowance,
                       stats: _Stats) -> Dict[str, FileMatch]:
        """Test each candidate's coverage (§25.5.2, §25.5.4): its about and name keys and the keys of the chunks and
        hint documents the streams named for it, read by id; chunk texts and hint items only where two or more words
        are covered in one chunk (the minimum window)."""
        if not concepts:
            return {}
        candidates = sorted(candidate_ids(concepts, in_scope, df, allowance))
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
               page_full: bool, today: date) -> Tuple[Dict[MatchTier, List[str]], Dict[MatchTier, TierBlock], int]:
        """Each match in its tier, ordered (validity matches soonest first, expired ones counted, §25.5.4), with its
        totals; the members past the shown count become group members."""
        by_tier: Dict[MatchTier, List[str]] = {}
        expired = 0
        start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        validity: Dict[str, datetime] = {}
        for item, match in matches.items():
            rec = pool.leans[item]
            if any(c.keys and all(k.startswith(_VALIDITY) for k in c.keys) for c in match.covers):
                if rec.valid_until is not None and rec.valid_until < start:
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
        """Related by meaning only (§25.5.3): similar names first (over the name_match floor, not covering the
        query), then files whose meaning clears the band's floor; at most RELATED_MAX, never a file of another tier.
        When every source abstained on a drive under the first band and nothing else matched, the best RELATED_SMALL
        by similarity (never under a kind filter; beside matches it would only be filler)."""
        taken = set(matched) | {i for items in tiered.values() for i in items}
        names = sorted(((name_match(r.name_tokens, qkeys) or 0, i) for i, r in pool.names.items()
                        if i in scope.passing and i not in taken and name_match(r.name_tokens, qkeys) is not None),
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
        """The shown files in tier order, identical copies (same content hash, not empty) collapsed into the first
        shown one's `also_at`."""
        shown: List[Tuple[MatchTier, str]] = []
        first: Dict[str, str] = {}
        also_at: Dict[str, List[str]] = {}
        for tier in _TIERS:
            for item in tiered.get(tier, [])[:_SHOWN[tier]]:
                digest = pool.leans[item].content_hash
                if digest and not digest.endswith(":0") and digest in first:
                    also_at.setdefault(first[digest], []).append(scope.paths[item])
                    continue
                if digest:
                    first[digest] = item
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
            card = make_card(rec, scope.paths[item], card_text(rec), self._nonce())
            out.append(replace(card, tier=tier.value, sources=explained.sources, passage=passage,
                               not_matched=not_matched(match), keys_cut=rec.keys_capped,
                               also_at=tuple(also_at.get(item, ()))))
        return tuple(out)

    def _related_card(self, pool: _Pool, scope: _Scope, item: str, similar: bool) -> DriveSearchCard:
        rec = pool.fulls[item]
        card = make_card(rec, scope.paths[item], card_text(rec), self._nonce())
        return replace(card, tier=MatchTier.RELATED.value, similar_name=similar)

    async def _job_counts(self, user_id: str) -> Tuple[int, int, int]:
        """The coverage line's enrichment counts (§25.5.6): text files whose profile or passages job waits or is held
        (per job; a file waiting on both counts once, so the larger count is named), and text files without passage
        keywords by policy (books, sheets, data)."""
        text_kinds = frozenset(k for k in _ALL_KINDS if k not in _NO_TEXT_KINDS)
        counts, no_passage = await asyncio.gather(self._repo.count_by_job_state(user_id, text_kinds),
                                                  self._repo.count_by_job_state(user_id, _NO_PASSAGE_KINDS))

        def total(job: str, states: Iterable[str]) -> int:
            return sum(counts.get(job, {}).get(s, 0) for s in states)

        jobs = (EnrichJob.PROFILE.value, EnrichJob.PASSAGES.value)
        profiled = no_passage.get(EnrichJob.PROFILE.value, {})
        return (max(total(j, _NOT_DESCRIBED_STATES) for j in jobs), max(total(j, (JobState.HELD.value,)) for j in jobs),
                sum(n for s, n in profiled.items() if s != JobState.NONE.value))

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
            cards.append(make_card(rec, path, card_text(rec), self._nonce()))
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
            keys = [k for k in query_name_keys(segments[-1]) if k.startswith("w:")]
            named, page_full_by_name = (await self._name_candidates(user_id, keys, NAME_FETCH, "folder", _Stats())
                                        if keys else ([], False))
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
        cards = tuple(make_card(f.record, f.path, card_text(f.record), self._nonce())
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
        )

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
        pages, images = usage_now(state, self._clock())   # a new month reads as zero before the tick resets it
        return DriveIndexingStatus(
            enabled=enabled, counters=dict(state.counters), last_success_at=state.last_success_at,
            last_error=state.last_error.value if state.last_error else None, index_as_of=state.observed_as_of,
            index_full=int(state.counters.get("carded", 0)) >= MAX_CARD_RECORDS_PER_USER,
            building=enabled and (state.purging or not state.root_item_id or await self._building(user_id, state)),
            access_lost_at=state.access_lost_at, purging=state.purging,
            readable_chunks=await self._repo.readable_chunk_total(user_id),
            model_pages_month=pages, triage_images_month=images,
        )
