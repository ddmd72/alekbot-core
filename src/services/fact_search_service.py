"""The fact search (fact profile RFC §5): embeds the query's subject and kind, reads the account's snapshot (cached while
the baseline cache document keeps its `refreshed_at`) and two kNN lists, then runs the pure engine of
`domain/fact_search_lists.py`.

Vectors: the engine orders key-list candidates by subject similarity, so every snapshot fact needs its text vector, which
the snapshot projection leaves out. They are cached per account by fact id as unit float32 vectors: a document's
`vector` never changes (`IN_PLACE_FIELDS`), so a reload fetches only the ids it has not seen and drops the ones gone.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import numpy as np

from ..domain.deduplication_service import SmartDeduplication
from ..domain.entities import FactEntity
from ..domain.fact_query import FactQuery
from ..domain.fact_search_lists import DEPTH_KNN, run_search
from ..domain.fact_search_snapshot import FactSearchSnapshot
from ..domain.place_names import PlaceNames
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_search_port import FactSearchPort
from ..ports.repository import FactRepository
from ..utils.logger import logger

_QUERY_TASK = "RETRIEVAL_QUERY"


@dataclass(frozen=True)
class _Loaded:
    """One account's snapshot and the text vectors of its rows (unit; `has_vector` marks the rows that have one)."""
    snapshot: FactSearchSnapshot
    matrix: np.ndarray
    has_vector: np.ndarray
    vectors: Dict[str, np.ndarray]


def _unit(vector: Sequence[float]) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm > 0 else array


class FactSearchService(FactSearchPort):
    def __init__(
        self, repository: FactRepository, embedding_service: EmbeddingService, place_names: Callable[[], PlaceNames]
    ) -> None:
        self._repository = repository
        self._embedder = embedding_service
        self._place_loader = place_names
        self._cache: Dict[str, Tuple[object, _Loaded]] = {}
        self._known_vectors: Dict[str, Dict[str, np.ndarray]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._places: Optional[PlaceNames] = None
        self._places_lock = asyncio.Lock()
        self._dedup = SmartDeduplication(moderate_threshold=0.96, strict_threshold=0.98)

    async def search(self, account_id: str, query: FactQuery) -> List[FactEntity]:
        started = time.perf_counter()
        try:
            return await self._search(account_id, query, started)
        except Exception as exc:
            logger.error(f"[FactSearch] path={query.path} acc={account_id[:8]} failed: {type(exc).__name__}: {exc}",
                         exc_info=True)
            raise

    async def _search(self, account_id: str, query: FactQuery, started: float) -> List[FactEntity]:
        texts = [text for text in (query.subject, query.kind) if text]
        embedded, refreshed_at = await asyncio.gather(self._embed(texts), self._repository.get_baseline_refreshed_at(account_id))
        vectors = iter(embedded)
        subject_vector = next(vectors) if query.subject else None
        kind_vector = next(vectors) if query.kind else None
        (loaded, status), semantic, labels, place_keys = await asyncio.gather(
            self._snapshot(account_id, refreshed_at),
            self._nearest(account_id, "vector", subject_vector),
            self._nearest(account_id, "label_vector", kind_vector),
            self._place_keys(query),
        )
        snapshot = loaded.snapshot
        in_snapshot = set(snapshot.ids)
        unsnapped = 0
        knn: List[Optional[List[str]]] = []
        for hits in (semantic, labels):
            if hits is None:
                knn.append(None)
                continue
            knn.append([i for i in hits if i in in_snapshot])
            unsnapped += len(hits) - len(knn[-1] or [])
        similarity: Dict[str, float] = {}
        if subject_vector is not None and loaded.has_vector.any():
            scores = loaded.matrix @ _unit(subject_vector)
            similarity = dict(zip(snapshot.ids, scores.tolist()))
        outcome = run_search(
            snapshot, query, semantic_ids=knn[0], label_ids=knn[1], similarity=similarity,
            query_place_keys=place_keys, is_duplicate=lambda a, b: self._is_duplicate(loaded, a, b),
        )
        lists = ",".join(f"{name}:{len(ids)}" for name, ids in outcome.lists.items()) or "none"
        logger.info(
            f"[FactSearch] path={query.path} acc={account_id[:8]} lists={lists} fused={len(outcome.fused)} "
            f"kept={len(outcome.kept)} gate_dropped={outcome.gate_dropped} unsnapped={unsnapped} snapshot={status} "
            f"ms={int((time.perf_counter() - started) * 1000)}")
        return [snapshot.entities[fact_id] for fact_id in outcome.kept]

    async def _embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        return await self._embedder.get_embeddings_batch(texts, _QUERY_TASK)

    async def _nearest(self, account_id: str, field: str, vector: Optional[Sequence[float]]) -> Optional[List[str]]:
        if vector is None:
            return None
        return await self._repository.nearest_fact_ids(account_id, field, vector, DEPTH_KNN)

    async def _place_keys(self, query: FactQuery) -> FrozenSet[str]:
        if not query.places:
            return frozenset()
        async with self._places_lock:
            if self._places is None:
                self._places = await asyncio.to_thread(self._place_loader)
            names = self._places
        keys = set()
        for place in query.places:
            keys.update(names.query_keys(place.name, place.country))
        return frozenset(keys)

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
        if missing:
            fetched = await self._repository.get_fact_vectors(account_id, missing)
            kept.update({fact_id: _unit(vector) for fact_id, vector in fetched.items() if len(vector)})
        self._known_vectors[account_id] = kept
        dimension = len(next(iter(kept.values()))) if kept else 0
        matrix = np.zeros((len(snapshot), dimension), dtype=np.float32)
        has_vector = np.zeros(len(snapshot), dtype=bool)
        for row, fact_id in enumerate(snapshot.ids):
            if fact_id in kept:
                matrix[row] = kept[fact_id]
                has_vector[row] = True
        return _Loaded(snapshot, matrix, has_vector, kept)

    def _is_duplicate(self, loaded: _Loaded, candidate: str, kept: str) -> bool:
        first, second = loaded.vectors.get(candidate), loaded.vectors.get(kept)
        if first is None or second is None:
            return False
        rows = loaded.snapshot.rows
        return self._dedup.is_duplicate(rows[candidate].text, rows[kept].text, float(first @ second))[0]
