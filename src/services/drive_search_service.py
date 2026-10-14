"""
DriveSearchService — find the user's drive files by name, metadata and content
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §10.1, §10.2, §24.6).

Gate (uncached) -> folder scope -> card kNN + name stream (one query per whole-token and trigram key,
RFC Δ17) + chunk kNN + exact-term stream (one array-contains query per query term, §24.6), the content streams
fenced (§7.4) and pooled to one hit per record before fusion (§17.6) -> read-time visibility -> kind, date and
folder post-filters -> RRF -> names that cover the query first (RFC Δ17) -> widen once -> collapse duplicates ->
top K, content hits with their window (best chunk ± 1), and the coverage envelope. The content streams run only
while the policy admits content (`content_admitted`). Every decision that needs no I/O is in
`src/domain/drive_search.py` and `src/domain/drive_content_search.py`; this class reads and calls them.

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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple, Union

from ..domain.drive_card import card_text
from ..domain.drive_content_search import (
    ContentHit, WindowRequest, budget_windows, matched_label, pool_nearest, pool_terms, selective_term_hits,
    term_file_probes, term_selective, window_indexes,
)
from ..domain.drive_content_terms import clip_query, query_terms
from ..domain.drive_index import (
    POLICY_VERSION, DriveChunk, DriveIndexRecord, DriveSyncState, EpochPhase, Priority, RecordState, Scope,
)
from ..domain.drive_index_params import (
    CARD_FETCH, CHUNK_FETCH, CONTENT_TERM_FETCH, CONTENT_TERM_QUERIES, FETCH_MAX, FOLDER_INMEM_CARDS,
    FOLDER_INMEM_CHUNKS, K, MAX_CARD_RECORDS_PER_USER, NAME_FETCH, NAME_KEY_QUERIES, QUERY_TERM_CHARS, RRF_K,
    WINDOW_CHARS_TOTAL,
)
from ..domain.drive_index_planner import usage_now
from ..domain.drive_indexing_status import DriveIndexingStatus
from ..domain.drive_name_keys import covers_query, name_match, query_name_keys, query_token_groups
from ..domain.drive_search import (
    MAX_CHAIN_DEPTH, NOT_READ_YET_COUNTER, STATUS_AMBIGUOUS_FOLDER, STATUS_FOLDER_NOT_FOUND, STATUS_OK, UNREAD_COUNTER,
    DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, content_admitted, cosine_distance,
    cover_first, hides_descendants, in_date_range, kinds_for, make_card, normalize_path, path_match, rank_names,
    record_visible, resolve_chain, search_gate,
)
from ..domain.rrf import apply_rrf_ranking
from ..domain.user_drive import parse_drive_ref
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_CHILD_PAGE = 200
_WALK_PARALLEL = 16   # folders listed at once, per level of a subtree walk
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

Chain = Optional[Tuple[str, Tuple[str, ...]]]   # (path, ancestor ids) or None when hidden


def _not_indexed(folder_name: str) -> DriveSearchResult:
    return DriveSearchResult(status=STATUS_OK, folder_path=folder_name, folder_not_indexed=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nonce() -> str:
    return secrets.token_hex(8)


async def _no_pages() -> List[Any]:
    return []


@dataclass
class _Stats:
    """Counts after each stage, for the one summary line per search (§10.1)."""
    mode: str = "query"
    scope: str = "all"
    content: str = "off"             # off | wide (user-wide streams) | memory (a folder's chunks ranked in memory)
    card: int = 0
    name: int = 0
    name_queries: int = 0
    name_kept: int = 0
    chunk: int = 0                   # chunk kNN hits, before the fence
    chunk_kept: int = 0              # records after the fence and pooling
    term_queries: int = 0
    term_probes: int = 0             # files looked for past a full term page from few files (arbiter R2 S-M1)
    term: int = 0                    # exact-term chunk hits, before the fence
    term_kept: int = 0
    windows: int = 0
    window_chars: int = 0
    visible: int = 0
    filtered: int = 0
    candidates: Set[str] = field(default_factory=set)   # distinct visible records, before the filters
    fused: int = 0
    covered: int = 0
    widened: bool = False
    collapsed: int = 0
    returned: int = 0


@dataclass
class _Ancestry:
    """Ancestor records loaded level by level in batches, shared by every candidate of one search."""
    repo: DriveIndexRepository
    user_id: str
    root_item_id: str
    cache: Dict[str, Optional[DriveIndexRecord]] = field(default_factory=dict)

    def seed(self, recs: Iterable[DriveIndexRecord]) -> None:
        for rec in recs:
            self.cache[rec.item_id] = rec

    def _missing(self, parent_id: str) -> bool:
        return bool(parent_id) and parent_id != self.root_item_id and parent_id not in self.cache

    async def load(self, recs: Iterable[DriveIndexRecord]) -> None:
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

    def chain(self, rec: DriveIndexRecord, *, require_card: bool = True) -> Chain:
        return resolve_chain(rec, self.cache.get, self.root_item_id, require_card=require_card)


@dataclass(frozen=True)
class _Folder:
    record: DriveIndexRecord
    path: str


@dataclass(frozen=True)
class _Fetch:
    """Page sizes of the streams; widened once to FETCH_MAX (§10.1 step 5). Built at call time from the module's
    parameters."""
    card: int
    name: int
    chunk: int
    term: int

    @classmethod
    def first(cls) -> "_Fetch":
        return cls(CARD_FETCH, NAME_FETCH, CHUNK_FETCH, CONTENT_TERM_FETCH)

    @classmethod
    def widest(cls) -> "_Fetch":
        return cls(FETCH_MAX, FETCH_MAX, FETCH_MAX, FETCH_MAX)


@dataclass
class _Content:
    """The content streams of one search: records pooled per stream (best first), each record's window anchor, and
    the selective query terms (the windows are trimmed around them)."""
    nearest: List[DriveIndexRecord] = field(default_factory=list)
    terms: List[DriveIndexRecord] = field(default_factory=list)
    anchors: Dict[str, DriveChunk] = field(default_factory=dict)
    full: bool = False
    selective: Tuple[str, ...] = ()

    @classmethod
    def of(cls, nearest: List[ContentHit], terms: List[ContentHit], full: bool,
           selective: Sequence[str] = ()) -> "_Content":
        anchors = {h.record.item_id: h.chunk for h in nearest}
        # a selective term's chunk holds the number or code the user asked for: it anchors the window
        anchors.update({h.record.item_id: h.chunk for h in terms})
        return cls([h.record for h in nearest], [h.record for h in terms], anchors, full, tuple(selective))


@dataclass
class _TermPages:
    """Each query term's latest page, kept across the widening. Selectivity is decided on the first page
    (selective_term_hits, arbiter R1-2): a term whose full first page spans many files is `non_selective` and never
    read again; a selective term whose page was full (`full`) is read again, wider, on the widening; a page that was
    not full is complete."""
    pages: Dict[str, List[DriveChunk]] = field(default_factory=dict)
    non_selective: Set[str] = field(default_factory=set)
    full: Set[str] = field(default_factory=set)

    def selective(self) -> Dict[str, List[DriveChunk]]:
        return {t: c for t, c in self.pages.items() if t not in self.non_selective}


class DriveSearchService:
    def __init__(self, repo: DriveIndexRepository, embeddings: EmbeddingService, users: UserRepository,
                 drive: UserDrivePort, *, embedding_space: str, clock: Callable[[], datetime] = _utcnow,
                 nonce: Callable[[], str] = _nonce, monotonic: Callable[[], float] = time.monotonic) -> None:
        """`embedding_space` is the space query vectors are embedded in (the adapter's EMBEDDING_SPACE). The
        card stream runs only while the index's cards are in it (§12.3)."""
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
        ms = round((self._monotonic() - started) * 1000)
        logger.info(
            f"[DriveSearch] user={user_id[:8]} status={result.status} mode={stats.mode} scope={stats.scope} "
            f"content={stats.content} card={stats.card} name={stats.name} name_queries={stats.name_queries} "
            f"name_kept={stats.name_kept} chunk={stats.chunk} chunk_kept={stats.chunk_kept} "
            f"term_queries={stats.term_queries} term_probes={stats.term_probes} term={stats.term} term_kept={stats.term_kept} "
            f"visible={stats.visible} filtered={stats.filtered} fused={stats.fused} covered={stats.covered} "
            f"widened={stats.widened} collapsed={stats.collapsed} returned={stats.returned} windows={stats.windows} "
            f"window_chars={stats.window_chars} "
            f"building={result.envelope.building} ms={ms}")
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
        return replace(result, envelope=envelope)

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
        if folder is not None:
            subtree, complete = await self._walk(user_id, folder.record)
            if complete:
                stats.scope = "folder-memory"
                return await self._in_memory(user_id, request, kinds, folder, subtree, ancestry, use_cards, stats)
            stats.scope = "folder-wide"
        if request.filter_only:
            return await self._filter_only(user_id, request, kinds, folder, ancestry, stats)
        return await self._ranked(user_id, request, kinds, folder, ancestry, use_cards, stats)

    async def _ranked(self, user_id: str, request: DriveSearchRequest, kinds: FrozenSet[str],
                      folder: Optional[_Folder], ancestry: _Ancestry, use_cards: bool,
                      stats: _Stats) -> DriveSearchResult:
        keys = query_name_keys(request.query)
        members: List[Optional[str]] = list(sorted(kinds)) or [None]   # a family: one kNN per member kind
        single_kind = members[0] if len(members) == 1 else None        # names, terms: a family is a post-filter
        with_content = content_admitted()
        stats.content = "wide" if with_content else "off"
        terms = self._terms(request.query) if with_content else ()
        term_pages = _TermPages()
        vector: Optional[List[float]] = None
        fetch = _Fetch.first()
        while True:
            (vector, nearest, card_full, chunk_hits, chunk_full), (named, name_full), _ = await asyncio.gather(
                self._vector_streams(user_id, request.query, vector, fetch, members, use_cards,
                                     with_content and use_cards, stats),
                self._name_candidates(user_id, keys, fetch.name, single_kind, stats),
                self._term_hits(user_id, terms, fetch.term, single_kind, term_pages, stats))
            stats.card, stats.name = len(nearest), len(named)
            content = await self._pool_content(user_id, chunk_hits, term_pages, terms, chunk_full, stats)
            scores = {r.item_id: s for r in named if (s := name_match(r.name_tokens, keys)) is not None}
            stats.name_kept = len(scores)
            by_name = rank_names(named, scores)
            fused, paths = await self._fuse([nearest, by_name, content.nearest, content.terms], request, kinds,
                                            folder, ancestry, stats)
            full_page = card_full or name_full or content.full
            if len(fused) >= K or stats.widened or not full_page:
                break
            stats.widened = True   # §10.1 step 5: once, at the cap
            fetch = _Fetch.widest()
        ranked = self._covered_first(fused, request.query, stats)
        # The filters saw only the streams' pages: past a full page, a matching file may exist (§10.2 honesty).
        capped = full_page and stats.filtered < stats.visible
        card_side = {r.item_id for r in nearest} | set(scores)
        cards = await self._with_content(user_id, self._cards(ranked, paths, stats), card_side, set(scores), content,
                                         stats)
        return DriveSearchResult(status=STATUS_OK, cards=cards, request=request,
                                 folder_path=folder.path if folder else None, capped=capped,
                                 candidates=len(stats.candidates) if capped else 0)

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
        stats.fused = len(kept)
        return DriveSearchResult(status=STATUS_OK, cards=self._cards(kept, paths, stats), request=request,
                                 matched_total=len(kept), matched_at_least=full,
                                 folder_path=folder.path if folder else None)

    async def _in_memory(self, user_id: str, request: DriveSearchRequest, kinds: FrozenSet[str],
                         folder: _Folder, subtree: List[DriveIndexRecord], ancestry: _Ancestry, use_cards: bool,
                         stats: _Stats) -> DriveSearchResult:
        """A folder whose subtree fits FOLDER_INMEM_CARDS: rank its cards in memory, and its chunks too while they
        fit FOLDER_INMEM_CHUNKS; past that the user-wide content streams run under the folder post-filter
        (§10.1 Folder)."""
        ancestry.seed(subtree)
        pool = [r for r in subtree if not kinds or r.kind in kinds]
        stats.card = len(pool)
        if request.filter_only:
            kept, paths = await self._visible(pool, request, kinds, folder, ancestry, stats)
            kept.sort(key=lambda r: (r.effective_date or _EPOCH, r.item_id), reverse=True)
            stats.fused = len(kept)
            return DriveSearchResult(status=STATUS_OK, cards=self._cards(kept, paths, stats), request=request,
                                     matched_total=len(kept), folder_path=folder.path)
        by_distance: List[DriveIndexRecord] = []
        vector: Optional[List[float]] = None
        if use_cards:
            embedded, vectors = await asyncio.gather(
                self._embeddings.get_embedding(request.query, task_type="RETRIEVAL_QUERY"),
                self._repo.card_vectors(user_id, [r.item_id for r in pool if r.has_card]))
            vector = embedded
            by_distance = sorted((r for r in pool if r.item_id in vectors),
                                 key=lambda r: (cosine_distance(embedded, vectors[r.item_id]), r.item_id))
        keys = query_name_keys(request.query)
        scores = {r.item_id: s for r in pool if (s := name_match(r.name_tokens, keys)) is not None}
        stats.name, stats.name_kept = len(pool), len(scores)
        by_name = rank_names(pool, scores)
        with_content = content_admitted()
        terms = self._terms(request.query) if with_content else ()
        readable = [r for r in pool if r.indexed_generation is not None and r.chunk_count > 0
                    and ancestry.chain(r) is not None]
        members: List[Optional[str]] = list(sorted(kinds)) or [None]
        single_kind = members[0] if len(members) == 1 else None
        term_pages = _TermPages()
        fetch = _Fetch.first()
        while True:
            if not with_content:
                content = _Content()
            elif sum(r.chunk_count for r in readable) <= FOLDER_INMEM_CHUNKS:
                stats.content = "memory"
                content = await self._content_in_memory(user_id, readable, vector, terms, stats)
            else:
                stats.content = "wide"
                (_, _, _, chunk_hits, chunk_full), _ = await asyncio.gather(
                    self._vector_streams(user_id, request.query, vector, fetch, members, False, use_cards, stats),
                    self._term_hits(user_id, terms, fetch.term, single_kind, term_pages, stats))
                content = await self._pool_content(user_id, chunk_hits, term_pages, terms, chunk_full, stats)
            fused, paths = await self._fuse([by_distance, by_name, content.nearest, content.terms], request, kinds,
                                            folder, ancestry, stats)
            if len(fused) >= K or stats.widened or not content.full:
                break
            stats.widened = True
            fetch = _Fetch.widest()
        ranked = self._covered_first(fused, request.query, stats)
        # matched by card: the cards a user-wide card page would hold, or a name match (§10.2)
        card_side = {r.item_id for r in by_distance[:CARD_FETCH]} | set(scores)
        cards = await self._with_content(user_id, self._cards(ranked, paths, stats), card_side, set(scores), content,
                                         stats)
        return DriveSearchResult(status=STATUS_OK, cards=cards, request=request, folder_path=folder.path)

    # ------------------------------------------------------------------------------------------
    # Streams and stages
    # ------------------------------------------------------------------------------------------

    async def _vector_streams(self, user_id: str, query: str, vector: Optional[List[float]], fetch: _Fetch,
                              kinds: Sequence[Optional[str]], cards: bool, chunks: bool, stats: _Stats
                              ) -> Tuple[Optional[List[float]], List[DriveIndexRecord], bool,
                                         List[Tuple[DriveChunk, float]], bool]:
        """Card kNN (§10.1 step 2) and chunk kNN (step 3), one per kind each, merged nearest first; the query is
        embedded once per search (step 1). Both are off while the index's vectors are in another embedding space
        (§12.3; `use_cards`). Returns (vector, card records, card page full, chunk hits, chunk page full)."""
        if vector is None and (cards or chunks):
            vector = await self._embeddings.get_embedding(query, task_type="RETRIEVAL_QUERY")
        if vector is None:
            return None, [], False, [], False
        card_pages, chunk_pages = await asyncio.gather(
            asyncio.gather(*(self._repo.find_nearest_cards(user_id, vector, fetch.card, k) for k in kinds))
            if cards else _no_pages(),
            asyncio.gather(*(self._repo.find_nearest_chunks(user_id, vector, fetch.chunk, k) for k in kinds))
            if chunks else _no_pages())
        merged = sorted((hit for page in card_pages for hit in page), key=lambda hit: (hit[1], hit[0].item_id))
        chunk_hits = sorted((hit for page in chunk_pages for hit in page),
                            key=lambda hit: (hit[1], hit[0].item_id, hit[0].index))
        stats.chunk = len(chunk_hits)
        return (vector, [rec for rec, _ in merged], any(len(page) >= fetch.card for page in card_pages),
                chunk_hits, any(len(page) >= fetch.chunk for page in chunk_pages))

    async def _name_candidates(self, user_id: str, keys: Sequence[str], limit: int, kind: Optional[str],
                               stats: _Stats) -> Tuple[List[DriveIndexRecord], bool]:
        """The name stream's pool (§10.1 step 3a, RFC Δ17): one query per whole-token key and one per
        trigram key, each in query order and at most NAME_KEY_QUERIES of each kind, plus one any-key query
        for the remaining keys (at most NAME_QUERY_KEYS_MAX), all in parallel. A record sharing a query key
        held by at most `limit` records is always in the pool: a common key (`img`, `ent`) can no longer
        crowd out a rare one (`4912`), and a query that reaches its target only through trigrams (every
        token misspelled, a prefix) still finds it. Returns (records, any page full)."""
        whole = [k for k in keys if k.startswith("w:")][:NAME_KEY_QUERIES]
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

    @staticmethod
    def _terms(query: str) -> Tuple[str, ...]:
        """The query's exact terms (amounts, dates, ids), most specific first, at most CONTENT_TERM_QUERIES (§24.6),
        taken from at most QUERY_TERM_CHARS of the query (arbiter R2 S-m1)."""
        return query_terms(clip_query(query, QUERY_TERM_CHARS))[:CONTENT_TERM_QUERIES]

    async def _term_hits(self, user_id: str, terms: Sequence[str], limit: int, kind: Optional[str],
                         pages: _TermPages, stats: _Stats) -> None:
        """The exact-term stream (§24.6): one array-contains query per term, in parallel, never one
        array-contains-any (it returns the first matches by id, so a common term would hide a rare one). Selectivity
        is decided once, on a term's first read (term_selective, review D-M2, arbiter R1-2): a page that was not full
        is complete; a full one is id-ordered, so when it spans few files the files past it are probed first
        (arbiter R2 S-M1) — a non-selective term is never read again; the widening reads again only a selective term
        whose page was full."""
        due = [t for t in terms if t not in pages.pages or t in pages.full]
        if not due:
            return
        first = {t for t in due if t not in pages.pages}
        found = await asyncio.gather(*(self._repo.find_chunks_by_term(user_id, t, limit, kind) for t in due))
        stats.term_queries += len(due)
        read = dict(zip(due, found))
        probing = [t for t in due if t in first and term_file_probes(read[t], limit)]
        past = dict(zip(probing, await asyncio.gather(*(
            self._files_past_page(user_id, t, read[t], term_file_probes(read[t], limit), kind, stats)
            for t in probing))))
        for term, chunks in read.items():
            if term in first and not term_selective(chunks, limit, past.get(term, 0)):
                pages.non_selective.add(term)
            pages.pages[term] = list(chunks)
            if term not in pages.non_selective and len(chunks) >= limit:
                pages.full.add(term)
            else:
                pages.full.discard(term)
        stats.term = sum(len(c) for c in pages.pages.values())

    async def _files_past_page(self, user_id: str, term: str, page: Sequence[DriveChunk], wanted: int,
                               kind: Optional[str], stats: _Stats) -> int:
        """The files holding `term` past its first page, counted up to `wanted` (arbiter R2 S-M1): each probe starts
        past the last file seen, so it names a new file or finds the term exhausted."""
        after = max(c.item_id for c in page)
        seen = 0
        while seen < wanted:
            nxt = await self._repo.find_next_file_with_term(user_id, term, after, kind)
            stats.term_probes += 1
            if nxt is None:
                break
            seen += 1
            after = nxt.item_id
        return seen

    async def _pool_content(self, user_id: str, chunk_hits: Sequence[Tuple[DriveChunk, float]],
                            term_pages: _TermPages, terms: Sequence[str], chunk_full: bool,
                            stats: _Stats) -> _Content:
        """Fence both content streams against the records read now (§7.4) and pool each to one hit per record
        before fusion (§17.6); only the selective terms' hits take part (review D-M2, arbiter R1-2). A selective
        term whose page was full leaves the streams `full`: the widening reads it again."""
        selective = term_pages.selective()
        ids = {c.item_id for c, _ in chunk_hits} | {c.item_id for page in selective.values() for c in page}
        records = await self._repo.get_records(user_id, sorted(ids)) if ids else {}
        nearest = pool_nearest(chunk_hits, records)
        kept_terms = [t for t in terms if t in selective]
        by_term = pool_terms(selective, records, kept_terms)
        stats.chunk_kept, stats.term_kept = len(nearest), len(by_term)
        return _Content.of(nearest, by_term, chunk_full or bool(term_pages.full), kept_terms)

    async def _content_in_memory(self, user_id: str, readable: Sequence[DriveIndexRecord],
                                 vector: Optional[List[float]], terms: Sequence[str], stats: _Stats) -> _Content:
        """A folder's readable chunks, ranked in memory (§10.1 Folder): read by id at each record's readable
        generation (so fenced by construction), scored as they arrive; vectors are not kept. Only what ranks is read:
        never the text (windows read it by id), the terms only for a query with terms, the vectors only with a query
        vector — refreshing with no terms reads nothing."""
        hits: List[Tuple[DriveChunk, float]] = []
        by_term: Dict[str, List[DriveChunk]] = {t: [] for t in terms}
        if vector is None and not terms:
            return _Content()
        for i in range(0, len(readable), _WALK_PARALLEL):
            batch = readable[i:i + _WALK_PARALLEL]
            rows = await asyncio.gather(*(
                self._repo.get_chunks_with_vectors(user_id, r.item_id, int(r.indexed_generation or 0),
                                                   range(r.chunk_count), text=False, terms=bool(terms),
                                                   vectors=vector is not None) for r in batch))
            for row in rows:
                for chunk, chunk_vector in row:
                    if vector is not None:
                        hits.append((chunk, cosine_distance(vector, chunk_vector)))
                    for term in terms:
                        if term in chunk.content_terms:
                            by_term[term].append(chunk)
        records = {r.item_id: r for r in readable}
        nearest = pool_nearest(hits, records)
        selective = selective_term_hits(by_term, CONTENT_TERM_FETCH)   # the user-wide stream's rule (D-M2)
        kept_terms = [t for t in terms if t in selective]
        pooled_terms = pool_terms(selective, records, kept_terms)
        stats.chunk, stats.term = len(hits), sum(len(c) for c in by_term.values())
        stats.chunk_kept, stats.term_kept = len(nearest), len(pooled_terms)
        return _Content.of(nearest, pooled_terms, False, kept_terms)

    async def _with_content(self, user_id: str, cards: Tuple[DriveSearchCard, ...], card_side: Set[str],
                            name_side: Set[str], content: _Content, stats: _Stats) -> Tuple[DriveSearchCard, ...]:
        """`matched` for every card (and whether the name stream found it) and, for content hits, the window: the
        anchor chunk and its neighbours read by id from stored text (§10.2), fitted in rank order into
        WINDOW_CHARS_TOTAL. Stream reads may leave `text` empty, so the window is always read here. A window anchored
        by the chunk kNN alone is the nearest passage, not one holding the query's terms (F5-8)."""
        anchored = [c for c in cards if c.item_id in content.anchors]   # in rank order
        read = await asyncio.gather(*(
            self._repo.get_chunks(user_id, c.item_id, content.anchors[c.item_id].generation,
                                  window_indexes(content.anchors[c.item_id].index)) for c in anchored))
        fitted = budget_windows([WindowRequest(content.anchors[c.item_id].index, chunks)
                                 for c, chunks in zip(anchored, read)], content.selective, WINDOW_CHARS_TOTAL)
        windows = {c.item_id: w for c, w in zip(anchored, fitted)}
        stats.windows = sum(1 for w in fitted if w.text)
        stats.window_chars = sum(len(w.text) for w in fitted)
        exact = {r.item_id for r in content.terms}
        out = []
        for card in cards:
            window = windows.get(card.item_id)
            out.append(replace(card, matched=matched_label(card.item_id in card_side, card.item_id in content.anchors),
                               name_matched=card.item_id in name_side, passage_exact=card.item_id in exact,
                               window=window.text if window else "",
                               window_shortened=bool(window and window.text and window.shortened)))
        return tuple(out)

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

    async def _fuse(self, streams: List[List[DriveIndexRecord]], request: DriveSearchRequest,
                    kinds: FrozenSet[str], folder: Optional[_Folder], ancestry: _Ancestry,
                    stats: _Stats) -> Tuple[List[DriveIndexRecord], Dict[str, str]]:
        """Filter each stream, pool it to one hit per record, then RRF on item_id (§10.1 step 4). The ancestors of
        every stream's records load in one pass first: per stream, they took one sequential round each."""
        stats.visible = stats.filtered = 0
        stats.candidates = set()
        await ancestry.load([rec for stream in streams for rec in stream])
        pooled: List[List[DriveIndexRecord]] = []
        paths: Dict[str, str] = {}
        for stream in streams:
            kept, stream_paths = await self._visible(stream, request, kinds, folder, ancestry, stats)
            pooled.append(kept)   # _visible keeps the first (best) hit per record: pooling before fusion
            paths.update(stream_paths)
        fused = apply_rrf_ranking(pooled, key_fn=lambda r: r.item_id, k=RRF_K)
        stats.fused = len(fused)
        return fused, paths

    @staticmethod
    def _covered_first(fused: List[DriveIndexRecord], query: str, stats: _Stats) -> List[DriveIndexRecord]:
        """The step after RRF (RFC Δ17): names that cover every query token first."""
        groups = query_token_groups(query)
        stats.covered = sum(1 for r in fused if covers_query(r.name_tokens, groups))
        return cover_first(fused, groups)

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
