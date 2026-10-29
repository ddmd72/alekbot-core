"""
The run's passages ledger (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A11, A18). Pure.

Keyed by (item, generation, index): every chunk `find_passages` or `read_passages` returned in one commission, once.
It gives the next find its "already shown" set and builds the one passages block the orchestrator reads: files in
the order they were first retrieved, chunks by index, adjacent runs merged. Per execution, never shared; `add` has no
await, so it needs no lock (as the collector).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, FrozenSet, List, Set, Tuple

from .drive_excerpt_chunk import ExcerptChunk
from .drive_excerpt_context import ExcerptContext
from .drive_excerpt_context_plan import ContextPlan
from .drive_excerpt_file import ExcerptFile
from .drive_excerpt_page import PageSpan
from .drive_excerpt_render import render_block
from .drive_excerpt_request import ExcerptRequest
from .drive_excerpt_result import ExcerptResult
from .drive_index_lean import ChunkId


class ExcerptLedger:
    def __init__(self) -> None:
        self._files: Dict[Tuple[str, int], ExcerptFile] = {}
        self._chunks: Dict[Tuple[str, int], Dict[int, ExcerptChunk]] = {}
        self._context = ExcerptContext.EMPTY
        self._folders: Set[str] = set()
        self._described: Set[str] = set()
        self._matched: Set[ChunkId] = set()                     # chunks a find selected or listed as repeated (L11)
        self._pages: Dict[Tuple[str, int], List[PageSpan]] = {}  # the pages read, per file generation

    def keys(self) -> FrozenSet[ChunkId]:
        return frozenset(chunk.key for by_index in self._chunks.values() for chunk in by_index.values())

    def folders_shown(self) -> FrozenSet[str]:
        return frozenset(self._folders)

    def files_described(self) -> FrozenSet[str]:
        return frozenset(self._described)

    def add(self, result: ExcerptResult, plan: ContextPlan) -> None:
        """A file keeps the identity it was first retrieved with; its chunks are the union of every add."""
        for file in result.files:
            key = (file.item_id, file.generation)
            self._files.setdefault(key, file)
            by_index = self._chunks.setdefault(key, {})
            for chunk in file.chunks:
                by_index.setdefault(chunk.index, chunk)
        if isinstance(result.request, ExcerptRequest):
            self._matched.update(chunk.key for file in result.files for chunk in file.chunks)
            self._matched.update(result.repeated)
        if result.page is not None and not result.page.past_end and result.files:
            self._pages.setdefault((result.files[0].item_id, result.files[0].generation), []).append(result.page)
        self._context = self._context.merge(result.context)
        self._folders |= plan.shown_after
        self._described |= plan.described

    def files(self) -> Tuple[ExcerptFile, ...]:
        return tuple(replace(file, chunks=tuple(self._chunks[key][i] for i in sorted(self._chunks[key])))
                     for key, file in self._files.items())

    def block(self, *, nonce: Callable[[], str]) -> str:
        return render_block(self.files(), self._context, nonce=nonce, pages=self._pages,
                            matched=frozenset(self._matched))

    def __bool__(self) -> bool:
        return any(self._chunks.values())
