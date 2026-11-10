"""The fact search (fact profile RFC §5): embeds the queries' subjects, kinds and messages in one batch, reads the
account's snapshot (cached while the baseline cache document keeps its `refreshed_at`) and the kNN lists, then runs the
pure engine of `domain/fact_search_lists.py` per query and, for several phrases, fuses their scored lists
(`domain/fact_search_rank.py`).

Vectors: the snapshot projection leaves them out. Text vectors serve the subject and message similarity, the message
list and the duplicate pass; they are cached per account by fact id as unit float32 vectors: a document's `vector`
never changes (`IN_PLACE_FIELDS`), so a reload fetches only the ids it has not seen and drops the ones gone. Label
vectors serve the kind similarity; a re-profile rewrites them in place, so every load reads them all again. The
account's people cards are read with each load too.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import AbstractSet, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import numpy as np

from ..domain.deduplication_service import SmartDeduplication
from ..domain.entities import FactEntity
from ..domain.fact_query import SCOPE_ALL, FactQuery
from ..domain.fact_search_result import PhraseSearchResult
from ..domain.fact_search_lists import CUT, DEPTH_KNN, SearchOutcome, keep_distinct, run_search
from ..domain.fact_search_rank import PRIORITY_WEIGHTS, CardRef, HomePlace, QueryPlaceKeys, card_ref, fuse_phrases
from ..domain.fact_search_snapshot import FactSearchSnapshot
from ..domain.place_names import PlaceNames, fold, local_place_key, resolve_home
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_search_port import FactSearchPort
from ..ports.person_card_repository import PersonCardRepository
from ..ports.repository import FactRepository
from ..utils.logger import logger

_QUERY_TASK = "RETRIEVAL_QUERY"
MAX_PHRASES = len(PRIORITY_WEIGHTS)


@dataclass(frozen=True)
class _Loaded:
    """One account's snapshot with, per row, its text vector and its label vector (unit; `has_vector`/`has_label` mark
    the rows that have one), the text vectors by id, and the account's people cards."""
    snapshot: FactSearchSnapshot
    matrix: np.ndarray
    has_vector: np.ndarray
    vectors: Dict[str, np.ndarray]
    label_matrix: np.ndarray
    has_label: np.ndarray
    cards: Tuple[CardRef, ...]


@dataclass(frozen=True)
class _Phrase:
    """One query's outcome and the kNN hits the snapshot did not know."""
    outcome: SearchOutcome
    unsnapped: int


def _unit(vector: Sequence[float]) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm > 0 else array


def _rows_matrix(ids: Sequence[str], vectors: Dict[str, np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
    dimension = len(next(iter(vectors.values()))) if vectors else 0
    matrix = np.zeros((len(ids), dimension), dtype=np.float32)
    present = np.zeros(len(ids), dtype=bool)
    for row, fact_id in enumerate(ids):
        if fact_id in vectors:
            matrix[row] = vectors[fact_id]
            present[row] = True
    return matrix, present


class FactSearchService(FactSearchPort):
    def __init__(
        self, repository: FactRepository, embedding_service: EmbeddingService, place_names: Callable[[], PlaceNames],
        person_cards: PersonCardRepository,
    ) -> None:
        self._repository = repository
        self._embedder = embedding_service
        self._place_loader = place_names
        self._person_cards = person_cards
        self._cache: Dict[str, Tuple[object, _Loaded]] = {}
        self._known_vectors: Dict[str, Dict[str, np.ndarray]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._places: Optional[PlaceNames] = None
        self._places_lock = asyncio.Lock()
        self._homes: Dict[str, Optional[HomePlace]] = {}
        self._dedup = SmartDeduplication(moderate_threshold=0.96, strict_threshold=0.98)

    async def search(
        self, account_id: str, query: FactQuery, *, exclude: AbstractSet[str] = frozenset()
    ) -> List[FactEntity]:
        return list((await self._run(account_id, [query], exclude)).facts)

    async def search_phrases(self, account_id: str, queries: Sequence[FactQuery]) -> PhraseSearchResult:
        queries = list(queries)
        if not 1 <= len(queries) <= MAX_PHRASES:
            raise ValueError(f"search_phrases takes 1 to {MAX_PHRASES} queries, got {len(queries)}")
        return await self._run(account_id, queries, frozenset())

    async def warm(self, account_id: str) -> None:
        """Build the account's snapshot through the same guarded `_snapshot` a search uses, so a search that arrives
        meanwhile waits on the account's lock and then hits the cache. Without a baseline stamp the snapshot cannot be
        cached, so nothing is loaded."""
        started = time.perf_counter()
        refreshed_at = await self._repository.get_baseline_refreshed_at(account_id)
        if refreshed_at is None:
            return
        _, status = await self._snapshot(account_id, refreshed_at)
        logger.info(f"[FactSearch] warm acc={account_id[:8]} snapshot={status} "
                    f"ms={int((time.perf_counter() - started) * 1000)}")

    async def _run(
        self, account_id: str, queries: List[FactQuery], exclude: AbstractSet[str]
    ) -> PhraseSearchResult:
        started = time.perf_counter()
        try:
            return await self._search(account_id, queries, exclude, started)
        except Exception as exc:
            logger.error(f"[FactSearch] path={queries[0].path} acc={account_id[:8]} phrases={len(queries)} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            raise

    async def _search(
        self, account_id: str, queries: List[FactQuery], exclude: AbstractSet[str], started: float
    ) -> PhraseSearchResult:
        texts = list(dict.fromkeys(t for q in queries for t in (q.subject, q.kind, q.message) if t))
        embedded, refreshed_at = await asyncio.gather(
            self._embed(texts), self._repository.get_baseline_refreshed_at(account_id))
        by_text = dict(zip(texts, embedded))
        knn_calls = []
        for q in queries:
            knn_calls.append(self._nearest(account_id, "vector", by_text.get(q.subject) if q.subject else None))
            knn_calls.append(self._nearest(account_id, "label_vector", by_text.get(q.kind) if q.kind else None))
        (loaded, status), *hits = await asyncio.gather(self._snapshot(account_id, refreshed_at), *knn_calls)
        phrases = []
        for n, q in enumerate(queries):
            phrases.append(await self._phrase(loaded, q, hits[2 * n], hits[2 * n + 1], by_text, exclude))
        snapshot = loaded.snapshot
        outcomes = [p.outcome for p in phrases]
        if len(outcomes) == 1:
            kept = outcomes[0].kept
            lists_of = [set(kept)]
            candidates = len(outcomes[0].fused)
        else:
            fused = fuse_phrases(
                [[(fact_id, o.scores[fact_id]) for fact_id in o.fused] for o in outcomes],
                [o.scores[o.kept[:CUT][-1]] if o.kept else 0.0 for o in outcomes])
            limit = None if any(q.scope == SCOPE_ALL for q in queries) else CUT
            kept = keep_distinct(fused, lambda a, b: self._is_duplicate(loaded, a, b), limit)
            lists_of = [set(o.kept[:CUT]) for o in outcomes]
            candidates = len(fused)
        lists = ",".join(f"{name}:{len(ids)}" for name, ids in outcomes[0].lists.items()) or "none"
        logger.info(
            f"[FactSearch] path={queries[0].path} acc={account_id[:8]} phrases={len(queries)} lists={lists} "
            f"cands={candidates} gate_dropped={sum(o.gate_dropped for o in outcomes)} kept={len(kept)} "
            f"unsnapped={sum(p.unsnapped for p in phrases)} snapshot={status} "
            f"ms={int((time.perf_counter() - started) * 1000)}")
        return PhraseSearchResult(
            tuple(snapshot.entities[fact_id] for fact_id in kept),
            tuple(tuple(n for n, found in enumerate(lists_of) if fact_id in found) for fact_id in kept))

    async def _phrase(
        self, loaded: _Loaded, query: FactQuery, semantic: Optional[List[str]], labels: Optional[List[str]],
        by_text: Dict[str, List[float]], exclude: AbstractSet[str],
    ) -> _Phrase:
        snapshot = loaded.snapshot
        in_snapshot = set(snapshot.ids)
        unsnapped = 0
        knn: List[Optional[List[str]]] = []
        for found in (semantic, labels):
            if found is None:
                knn.append(None)
                continue
            knn.append([i for i in found if i in in_snapshot])
            unsnapped += len(found) - len(knn[-1] or [])
        similarity = self._similarity(snapshot.ids, loaded.matrix, loaded.has_vector, by_text, query.subject)
        label_similarity = self._similarity(snapshot.ids, loaded.label_matrix, loaded.has_label, by_text, query.kind)
        message_ids: Optional[List[str]] = None
        message_similarity: Dict[str, float] = {}
        if query.message:
            message_ids, message_similarity = self._message_list(loaded, by_text[query.message])
        place_keys, places = await self._query_places(query)
        home = await self._home(query.home)
        outcome = run_search(
            snapshot, query, semantic_ids=knn[0], label_ids=knn[1], similarity=similarity,
            query_place_keys=place_keys, is_duplicate=lambda a, b: self._is_duplicate(loaded, a, b),
            message_ids=message_ids, label_similarity=label_similarity, message_similarity=message_similarity,
            query_places=places, home=home, cards=loaded.cards, exclude=exclude,
        )
        return _Phrase(outcome, unsnapped)

    @staticmethod
    def _similarity(ids: Sequence[str], matrix: np.ndarray, present: np.ndarray, by_text: Dict[str, List[float]],
                    text: str) -> Dict[str, float]:
        """Cosine of the text's vector to every row (0.0 for a row without a vector); empty without the text."""
        if not text or not present.any():
            return {}
        return dict(zip(ids, (matrix @ _unit(by_text[text])).tolist()))

    @staticmethod
    def _message_list(loaded: _Loaded, vector: Sequence[float]) -> Tuple[List[str], Dict[str, float]]:
        """The DEPTH_KNN snapshot facts whose text vector is nearest to the message (rows without one excluded; ties by
        snapshot order) and the message similarity of every row."""
        ids = loaded.snapshot.ids
        if not loaded.has_vector.any():
            return [], {}
        scores = loaded.matrix @ _unit(vector)
        masked = np.where(loaded.has_vector, scores, -np.inf)
        order = np.argsort(-masked, kind="stable")[:DEPTH_KNN]
        return [ids[j] for j in order if np.isfinite(masked[j])], dict(zip(ids, scores.tolist()))

    async def _embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        return await self._embedder.get_embeddings_batch(texts, _QUERY_TASK)

    async def _nearest(self, account_id: str, field: str, vector: Optional[Sequence[float]]) -> Optional[List[str]]:
        if vector is None:
            return None
        return await self._repository.nearest_fact_ids(account_id, field, vector, DEPTH_KNN)

    async def _names(self) -> PlaceNames:
        async with self._places_lock:
            if self._places is None:
                self._places = await asyncio.to_thread(self._place_loader)
            return self._places

    async def _place_keys(self, query: FactQuery) -> FrozenSet[str]:
        return (await self._query_places(query))[0]

    async def _query_places(self, query: FactQuery) -> Tuple[FrozenSet[str], Tuple[QueryPlaceKeys, ...]]:
        """The place keys the query's places reach (the places list) and the same per place (the place score)."""
        if not query.places:
            return frozenset(), ()
        names = await self._names()
        keys = set()
        places = []
        for place in query.places:
            reached = names.query_keys(place.name, place.country)
            keys.update(reached)
            # A place the owner confirmed that the atlas lacks is stored under its folded name (RFC §4.7, §5.3).
            local = local_place_key(place.name) if fold(place.name) else None
            if local is not None:
                keys.add(local)
            country = None
            if reached and reached[0].startswith("pl:"):
                country = next((k for k in names.keys_of(reached[0][3:]) if k.startswith("co:")), None)
            elif reached and reached[0].startswith("co:"):
                country = reached[0]
            places.append(QueryPlaceKeys(keys=tuple(reached), country=country, lp=local))
        return frozenset(keys), tuple(places)

    async def _home(self, location: str) -> Optional[HomePlace]:
        """The owner's home town, resolved once per location text."""
        if not location:
            return None
        if location not in self._homes:
            self._homes[location] = resolve_home(await self._names(), location)
        return self._homes[location]

    async def _snapshot(self, account_id: str, refreshed_at: object) -> Tuple[_Loaded, str]:
        cached = self._cache.get(account_id)
        if refreshed_at is not None and cached is not None and cached[0] == refreshed_at:
            return cached[1], "hit"
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(account_id)
            if refreshed_at is not None and cached is not None and cached[0] == refreshed_at:
                return cached[1], "hit"
            loaded = await self._load(account_id)
            if refreshed_at is None:
                self._cache.pop(account_id, None)
            else:
                self._cache[account_id] = (refreshed_at, loaded)
            return loaded, f"load:{len(loaded.snapshot)}"

    async def _load(self, account_id: str) -> _Loaded:
        snapshot = FactSearchSnapshot.from_facts(await self._repository.list_search_snapshot(account_id))
        known = self._known_vectors.get(account_id, {})
        kept = {fact_id: known[fact_id] for fact_id in snapshot.ids if fact_id in known}
        missing = [fact_id for fact_id in snapshot.ids if fact_id not in kept]

        async def text_vectors() -> Dict[str, List[float]]:
            return await self._repository.get_fact_vectors(account_id, missing) if missing else {}

        fetched, labels, cards = await asyncio.gather(
            text_vectors(),
            self._repository.get_fact_vectors(account_id, list(snapshot.ids), "label_vector"),
            self._person_cards.list_cards(account_id),
        )
        kept.update({fact_id: _unit(vector) for fact_id, vector in fetched.items() if len(vector)})
        self._known_vectors[account_id] = kept
        matrix, has_vector = _rows_matrix(snapshot.ids, kept)
        label_matrix, has_label = _rows_matrix(
            snapshot.ids, {fact_id: _unit(vector) for fact_id, vector in labels.items() if len(vector)})
        return _Loaded(snapshot, matrix, has_vector, kept, label_matrix, has_label,
                       tuple(card_ref(card) for card in sorted(cards, key=lambda c: c.card_key)))

    def _is_duplicate(self, loaded: _Loaded, candidate: str, kept: str) -> bool:
        first, second = loaded.vectors.get(candidate), loaded.vectors.get(kept)
        if first is None or second is None:
            return False
        rows = loaded.snapshot.rows
        return self._dedup.is_duplicate(rows[candidate].text, rows[kept].text, float(first @ second))[0]
