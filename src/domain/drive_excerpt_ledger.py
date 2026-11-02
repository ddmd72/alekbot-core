"""
The run's passages ledger (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A11, A18). Pure.

Keyed by (ref tag, item, generation, index): every chunk `find_passages` or `read_passages` returned in one
commission, once. The tag is the connection the file was read from (`ExcerptFile.ref_tag`, MULTI_DRIVE_RFC section 9):
the same item id on two connections is two files, so a chunk shown from one never suppresses the other's. It gives
the next find its "already shown" set and builds the one passages block the orchestrator reads: files in the order
they were first retrieved, chunks by index, adjacent runs merged. Per execution, never shared; `add` has no await,
so it needs no lock (as the collector).
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
        self._files: Dict[Tuple[str, str, int], ExcerptFile] = {}
        self._chunks: Dict[Tuple[str, str, int], Dict[int, ExcerptChunk]] = {}
        self._context = ExcerptContext.EMPTY
        self._folders: Set[str] = set()
        self._described: Set[str] = set()
        self._matched: Set[Tuple[str, ChunkId]] = set()         # (tag, chunk) a find selected or listed as repeated (L11)
        self._pages: Dict[Tuple[str, str, int], List[PageSpan]] = {}  # the pages read, per connection and generation

    def keys(self) -> FrozenSet[Tuple[str, ChunkId]]:
        """(ref tag, chunk id) of every chunk shown: `DriveSearchService.excerpts(shown=...)` filters it by each
        searched connection's tag."""
        return frozenset((tag, chunk.key) for (tag, _, _), by_index in self._chunks.items()
                         for chunk in by_index.values())

    def folders_shown(self) -> FrozenSet[str]:
        return frozenset(self._folders)

    def files_described(self) -> FrozenSet[str]:
        return frozenset(self._described)

    def add(self, result: ExcerptResult, plan: ContextPlan) -> None:
        """A file keeps the identity it was first retrieved with; its chunks are the union of every add."""
        for file in result.files:
            key = (file.ref_tag, file.item_id, file.generation)
            self._files.setdefault(key, file)
            by_index = self._chunks.setdefault(key, {})
            for chunk in file.chunks:
                by_index.setdefault(chunk.index, chunk)
        if isinstance(result.request, ExcerptRequest):
            self._matched.update((file.ref_tag, chunk.key) for file in result.files for chunk in file.chunks)
            self._matched.update(self._tagged(result, chunk) for chunk in result.repeated)
        if result.page is not None and not result.page.past_end and result.files:
            first = result.files[0]
            self._pages.setdefault((first.ref_tag, first.item_id, first.generation), []).append(result.page)
        self._context = self._context.merge(result.context)
        self._folders |= plan.shown_after
        self._described |= plan.described

    def _tagged(self, result: ExcerptResult, chunk: ChunkId) -> Tuple[str, ChunkId]:
        """A repeated chunk was shown earlier, so the ledger knows its file; the result's own tag names it when the
        call read one connection (`merge_excerpts` drops the repeated ids of a call that read several)."""
        if result.ref_tag:
            return result.ref_tag, chunk
        tag = next((t for (t, item, gen) in self._files if item == chunk[0] and gen == chunk[1]), "")
        return tag, chunk

    def files(self) -> Tuple[ExcerptFile, ...]:
        return tuple(replace(file, chunks=tuple(self._chunks[key][i] for i in sorted(self._chunks[key])))
                     for key, file in self._files.items())

    def block(self, *, nonce: Callable[[], str]) -> str:
        # The renderer keys a file by (item, generation): the same item on two connections shares its page lines.
        pages: Dict[Tuple[str, int], List[PageSpan]] = {}
        for (_, item_id, generation), spans in self._pages.items():
            pages.setdefault((item_id, generation), []).extend(spans)
        return render_block(self.files(), self._context, nonce=nonce, pages=pages,
                            matched=frozenset(chunk for _, chunk in self._matched))

    def __bool__(self) -> bool:
        return any(self._chunks.values())
