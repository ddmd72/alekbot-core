"""
DriveSearchService — find the user's drive files by name and metadata
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §10.1, §10.2; Slice 1: no content stream).

Gate (uncached) -> folder scope -> card kNN + name stream (one query per whole-token and trigram key,
plan Δ17) -> read-time visibility -> kind, date and folder post-filters -> RRF -> names that cover the
query first (Δ17) -> widen once -> collapse duplicates -> top K with the coverage envelope. Every decision that needs
no I/O is in `src/domain/drive_search.py`; this class reads and calls it.

Public interface:
    DriveSearchService(repo, embeddings, users, drive, *, clock, nonce)
    await search(user_id, request) -> DriveSearchResult
    await indexing_status(user_id) -> DriveIndexingStatus     # Cabinet (§6)
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

from ..domain.drive_card import card_text
from ..domain.drive_index import DriveIndexRecord, DriveSyncState, EpochPhase, Priority
from ..domain.drive_index_params import (
    CARD_FETCH, FETCH_MAX, FOLDER_INMEM_CARDS, K, MAX_CARD_RECORDS_PER_USER, NAME_FETCH, NAME_KEY_QUERIES, RRF_K,
)
from ..domain.drive_indexing_status import DriveIndexingStatus
from ..domain.drive_name_keys import covers_query, name_match, query_name_keys, query_token_groups
from ..domain.drive_search import (
    MAX_CHAIN_DEPTH, STATUS_AMBIGUOUS_FOLDER, STATUS_FOLDER_NOT_FOUND, STATUS_OK, UNREAD_COUNTER,
    DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, cosine_distance, cover_first,
    hides_descendants, in_date_range, kinds_for, make_card, normalize_path, path_match, rank_names,
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _nonce() -> str:
    return secrets.token_hex(8)


@dataclass
class _Stats:
    """Counts after each stage, for the one summary line per search (§10.1)."""
    mode: str = "query"
    scope: str = "all"
    card: int = 0
    name: int = 0
    name_queries: int = 0
    name_kept: int = 0
    visible: int = 0
    filtered: int = 0
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

    def chain(self, rec: DriveIndexRecord, *, require_card: bool = True) -> Chain:
        return resolve_chain(rec, self.cache.get, self.root_item_id, require_card=require_card)


@dataclass(frozen=True)
class _Folder:
    record: DriveIndexRecord
    path: str


class DriveSearchService:
    def __init__(self, repo: DriveIndexRepository, embeddings: EmbeddingService, users: UserRepository,
                 drive: UserDrivePort, *, clock: Callable[[], datetime] = _utcnow,
                 nonce: Callable[[], str] = _nonce) -> None:
        self._repo = repo
        self._embeddings = embeddings
        self._users = users
        self._drive = drive
        self._clock = clock
        self._nonce = nonce

    # ------------------------------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------------------------------

    async def search(self, user_id: str, request: DriveSearchRequest) -> DriveSearchResult:
        stats = _Stats(mode="filter" if request.filter_only else "query")
        result = await self._search(user_id, request, stats)
        stats.returned = len(result.cards)
        logger.info(
            f"[DriveSearch] user={user_id[:8]} status={result.status} mode={stats.mode} scope={stats.scope} "
            f"card={stats.card} name={stats.name} name_queries={stats.name_queries} name_kept={stats.name_kept} "
            f"visible={stats.visible} filtered={stats.filtered} fused={stats.fused} covered={stats.covered} "
            f"widened={stats.widened} collapsed={stats.collapsed} returned={stats.returned} "
            f"building={result.envelope.building}")
        return result

    async def _search(self, user_id: str, request: DriveSearchRequest, stats: _Stats) -> DriveSearchResult:
        # §10.1 step 0: the flag, the sync state and the credentials, all read now, in parallel.
        profile, state, connection = await asyncio.gather(
            self._users.get_user(user_id), self._repo.get_sync_state(user_id), self._drive.connection(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        gate = search_gate(enabled, state, connection, self._clock())
        if gate.status != STATUS_OK:
            return DriveSearchResult(status=gate.status, envelope=DriveSearchEnvelope(enabled=enabled),
                                     request=request)
        if gate.rebuilding or state is None or not state.root_item_id:
            # Nothing stored yet, or a purge is deleting what was: say so instead of answering from it.
            envelope = DriveSearchEnvelope(building=True, reconnect_needed=gate.reconnect_needed)
            return DriveSearchResult(status=STATUS_OK, envelope=envelope, request=request,
                                     matched_total=0 if request.filter_only else None)
        envelope, result = await asyncio.gather(self._envelope(user_id, state, gate.reconnect_needed),
                                                self._body(user_id, request, state, stats))
        return replace(result, envelope=envelope)

    async def _body(self, user_id: str, request: DriveSearchRequest, state: DriveSyncState,
                    stats: _Stats) -> DriveSearchResult:
        kinds = kinds_for(request.kind)
        ancestry = _Ancestry(self._repo, user_id, state.root_item_id)
        folder: Optional[_Folder] = None
        if request.folder:
            found = await self._resolve_folder(user_id, request.folder, ancestry)
            if isinstance(found, DriveSearchResult):
                return replace(found, request=request)
            folder = found
        if folder is not None:
            subtree, complete = await self._walk(user_id, folder.record)
            if complete:
                stats.scope = "folder-memory"
                return await self._in_memory(user_id, request, kinds, folder, subtree, ancestry, stats)
            stats.scope = "folder-wide"
        if request.filter_only:
            return await self._filter_only(user_id, request, kinds, folder, ancestry, stats)
        return await self._ranked(user_id, request, kinds, folder, ancestry, stats)

    async def _ranked(self, user_id: str, request: DriveSearchRequest, kinds: FrozenSet[str],
                      folder: Optional[_Folder], ancestry: _Ancestry, stats: _Stats) -> DriveSearchResult:
        keys = query_name_keys(request.query)
        prefilter = next(iter(kinds)) if len(kinds) == 1 else None   # a family is a post-filter (Δ20)
        vector: Optional[List[float]] = None
        card_fetch, name_fetch = CARD_FETCH, NAME_FETCH
        while True:
            (vector, nearest), (named, name_full) = await asyncio.gather(
                self._nearest(user_id, request.query, vector, card_fetch, prefilter),
                self._name_candidates(user_id, keys, name_fetch, prefilter, stats))
            stats.card, stats.name = len(nearest), len(named)
            scores = {r.item_id: s for r in named if (s := name_match(r.name_tokens, keys)) is not None}
            stats.name_kept = len(scores)
            fused, paths = await self._fuse([[rec for rec, _ in nearest], rank_names(named, scores)],
                                            request, kinds, folder, ancestry, stats)
            full_page = len(nearest) >= card_fetch or name_full
            if len(fused) >= K or stats.widened or not full_page:
                break
            stats.widened = True   # §10.1 step 5: once, at the cap
            card_fetch, name_fetch = FETCH_MAX, FETCH_MAX
        ranked = self._covered_first(fused, request.query, stats)
        return DriveSearchResult(status=STATUS_OK, cards=self._cards(ranked, paths, stats), request=request,
                                 folder_path=folder.path if folder else None)

    async def _filter_only(self, user_id: str, request: DriveSearchRequest, kinds: FrozenSet[str],
                           folder: Optional[_Folder], ancestry: _Ancestry, stats: _Stats) -> DriveSearchResult:
        """§10.1: a plain query on kind / effective_date, newest first, no kNN, and how many matched. A kind
        family runs one query per member kind, merged (Δ20)."""
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
                         folder: _Folder, subtree: List[DriveIndexRecord], ancestry: _Ancestry,
                         stats: _Stats) -> DriveSearchResult:
        """A folder whose subtree fits FOLDER_INMEM_CARDS: rank its cards in memory (§10.1 Folder)."""
        ancestry.seed(subtree)
        pool = [r for r in subtree if not kinds or r.kind in kinds]
        stats.card = len(pool)
        if request.filter_only:
            kept, paths = await self._visible(pool, request, kinds, folder, ancestry, stats)
            kept.sort(key=lambda r: (r.effective_date or _EPOCH, r.item_id), reverse=True)
            stats.fused = len(kept)
            return DriveSearchResult(status=STATUS_OK, cards=self._cards(kept, paths, stats), request=request,
                                     matched_total=len(kept), folder_path=folder.path)
        vector, vectors = await asyncio.gather(
            self._embeddings.get_embedding(request.query, task_type="RETRIEVAL_QUERY"),
            self._repo.card_vectors(user_id, [r.item_id for r in pool if r.has_card]))
        by_distance = sorted((r for r in pool if r.item_id in vectors),
                             key=lambda r: (cosine_distance(vector, vectors[r.item_id]), r.item_id))
        keys = query_name_keys(request.query)
        scores = {r.item_id: s for r in pool if (s := name_match(r.name_tokens, keys)) is not None}
        stats.name, stats.name_kept = len(pool), len(scores)
        fused, paths = await self._fuse([by_distance, rank_names(pool, scores)], request, kinds, folder, ancestry,
                                        stats)
        ranked = self._covered_first(fused, request.query, stats)
        return DriveSearchResult(status=STATUS_OK, cards=self._cards(ranked, paths, stats), request=request,
                                 folder_path=folder.path)

    # ------------------------------------------------------------------------------------------
    # Streams and stages
    # ------------------------------------------------------------------------------------------

    async def _nearest(self, user_id: str, query: str, vector: Optional[List[float]], limit: int,
                       kind: Optional[str]) -> Tuple[List[float], List[Tuple[DriveIndexRecord, float]]]:
        """Card kNN (§10.1 step 2); the query is embedded once per search (step 1)."""
        if vector is None:
            vector = await self._embeddings.get_embedding(query, task_type="RETRIEVAL_QUERY")
        return vector, await self._repo.find_nearest_cards(user_id, vector, limit, kind)

    async def _name_candidates(self, user_id: str, keys: Sequence[str], limit: int, kind: Optional[str],
                               stats: _Stats) -> Tuple[List[DriveIndexRecord], bool]:
        """The name stream's pool (§10.1 step 3a, plan Δ17): one query per whole-token key and one per
        trigram key, each in query order and at most NAME_KEY_QUERIES of each kind, plus one any-key query
        for the remaining keys (at most NAME_QUERY_KEYS_MAX), all in parallel. A record sharing a query key
        held by at most `limit` records is always in the pool: a common key (`img`, `ent`) can no longer
        crowd out a rare one (`4912`), and a query that reaches its target only through trigrams (every
        token misspelled, a prefix) still finds it (premise audit P1). Returns (records, any page full)."""
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
        """Filter each stream, pool it to one hit per record, then RRF on item_id (§10.1 step 4)."""
        stats.visible = stats.filtered = 0
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
        """The step after RRF (plan Δ17): names that cover every query token first."""
        groups = query_token_groups(query)
        stats.covered = sum(1 for r in fused if covers_query(r.name_tokens, groups))
        return cover_first(fused, groups)

    def _cards(self, ranked: Sequence[DriveIndexRecord], paths: Dict[str, str],
               stats: _Stats) -> Tuple[DriveSearchCard, ...]:
        """Collapse identical files (same content hash) into one card with `also_at`, then cut at K."""
        cards: List[DriveSearchCard] = []
        by_hash: Dict[str, int] = {}
        for rec in ranked:
            path = paths[rec.item_id]
            if rec.content_hash and rec.content_hash in by_hash:
                first = by_hash[rec.content_hash]
                cards[first] = replace(cards[first], also_at=cards[first].also_at + (path,))
                stats.collapsed += 1
                continue
            if len(cards) >= K:
                continue   # keep scanning: a later duplicate still adds its path to a shown card
            if rec.content_hash:
                by_hash[rec.content_hash] = len(cards)
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
            chain = ancestry.chain(rec, require_card=False)
            if chain is None:
                return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND)
            return _Folder(rec, chain[0])
        segments = [p.strip() for p in wanted.replace("\\", "/").split("/") if p.strip()]
        if not segments:
            return DriveSearchResult(status=STATUS_FOLDER_NOT_FOUND)
        anchored, descent_complete = await self._descend(user_id, ancestry.root_item_id, segments)
        matches = await self._as_folders(anchored, ancestry)
        page_full = not descent_complete
        guessed = False   # one suffix match from a full name page: another may sit past it (rev_31 F3)
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

    async def _descend(self, user_id: str, root_item_id: str,
                       segments: Sequence[str]) -> Tuple[List[DriveIndexRecord], bool]:
        """Match the path segment by segment from the drive's root through `children`, reading at most
        FOLDER_INMEM_CARDS records. Returns (matching folders, the descent read everything it needed)."""
        level = [root_item_id]
        budget = FOLDER_INMEM_CARDS
        found: List[DriveIndexRecord] = []
        for depth, segment in enumerate(segments):
            want = normalize_path(segment)
            last = depth == len(segments) - 1
            found = []
            for parent_id in level:
                kids = await self._children_of(user_id, parent_id, budget + 1)
                budget -= len(kids)
                if budget < 0:
                    return [], False
                for kid in kids:
                    if not kid.is_folder or normalize_path(kid.name) != want:
                        continue
                    if record_visible(kid, require_card=False) and (last or not hides_descendants(kid)):
                        found.append(kid)
            if not found:
                return [], True
            level = [r.item_id for r in found]
        return found, True

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
        per parallel listing (rev_31 F4). Returns (records, complete)."""
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

    async def _envelope(self, user_id: str, state: DriveSyncState, reconnect_needed: bool) -> DriveSearchEnvelope:
        return DriveSearchEnvelope(
            building=await self._building(user_id, state), reconnect_needed=reconnect_needed,
            index_as_of=state.observed_as_of, unread=int(state.counters.get(UNREAD_COUNTER, 0)),
            index_full=int(state.counters.get("carded", 0)) >= MAX_CARD_RECORDS_PER_USER,
        )

    async def _building(self, user_id: str, state: DriveSyncState) -> bool:
        """The set of items may be incomplete (§10.2): an epoch not idle, or a first-seen or re-listing
        record due (class 2, plan Δ18) — one limit-1 query, never a counter. Edits and cascades do not count."""
        if state.epoch_phase is not EpochPhase.IDLE:
            return True
        return await self._repo.oldest_due_at(user_id, int(Priority.FIRST_SEEN)) is not None

    async def indexing_status(self, user_id: str) -> DriveIndexingStatus:
        """The Cabinet card (§6): counters, last sync, error category, index full, building."""
        profile, state = await asyncio.gather(self._users.get_user(user_id), self._repo.get_sync_state(user_id))
        enabled = bool(profile is not None and profile.config.drive_indexing_enabled)
        if state is None:
            return DriveIndexingStatus(enabled=enabled, counters={}, building=enabled)
        return DriveIndexingStatus(
            enabled=enabled, counters=dict(state.counters), last_success_at=state.last_success_at,
            last_error=state.last_error.value if state.last_error else None, index_as_of=state.observed_as_of,
            index_full=int(state.counters.get("carded", 0)) >= MAX_CARD_RECORDS_PER_USER,
            building=enabled and (state.purging or not state.root_item_id or await self._building(user_id, state)),
        )
