"""Builders for the passages tests (USER_DRIVE_INDEXING_RFC §28.3). Names and paths from the invented universe."""
from __future__ import annotations

from itertools import count
from typing import Callable, List, Optional, Sequence, Tuple

from src.domain.drive_chunker import chunk_text
from src.domain.drive_excerpt_chunk import ExcerptChunk
from src.domain.drive_excerpt_file import ExcerptFile
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_search import STATUS_OK
from src.domain.user_drive import make_drive_ref

TOMS_PLAN = "R&D/Tom's plan.docx"
LEASE = "lease_porto.pdf"
SCAN = "scan_0012.pdf"


def chunk(item_id: str = "item-1", index: int = 0, text: str = "", *, generation: int = 1,
          start: Optional[int] = None) -> ExcerptChunk:
    """A chunk whose offsets agree with its text (no header, no overlap unless `start` says so)."""
    begin = index * 1000 if start is None else start
    return ExcerptChunk(item_id=item_id, generation=generation, index=index, text=text, char_start=begin,
                        char_end=begin + len(text))


def chunks_of(source: str, item_id: str = "item-1", *, generation: int = 1) -> List[ExcerptChunk]:
    """The chunker's own chunks of `source`, as the index stores them."""
    return [ExcerptChunk(item_id=item_id, generation=generation, index=i, text=c.text, char_start=c.char_start,
                         char_end=c.char_end) for i, c in enumerate(chunk_text(source).chunks)]


def excerpt_file(item_id: str = "item-1", path: str = TOMS_PLAN, chunks: Sequence[ExcerptChunk] = (), *,
                 kind: str = "docx", generation: int = 1, chunk_count: int = 12, capture: str = "digital",
                 card_accuracy: Optional[int] = None, stale: bool = False, truncated: bool = False,
                 also_at: Tuple[str, ...] = ()) -> ExcerptFile:
    return ExcerptFile(item_id=item_id, ref=make_drive_ref(item_id), name=path.rsplit("/", 1)[-1], path=path,
                       kind=kind, generation=generation, chunk_count=chunk_count, size_bytes=2048,
                       web_url=f"https://example.com/{item_id}", stale=stale, truncated=truncated, capture=capture,
                       card_accuracy=card_accuracy, also_at=also_at, chunks=tuple(chunks))


def result(*files: ExcerptFile, question: str = "How does the review process work?", **fields: object) -> ExcerptResult:
    return ExcerptResult(status=fields.pop("status", STATUS_OK),  # type: ignore[arg-type]
                         request=fields.pop("request", ExcerptRequest(question=question)),  # type: ignore[arg-type]
                         files=files, **fields)  # type: ignore[arg-type]


def nonces() -> Callable[[], str]:
    counter = count(1)
    return lambda: f"nonce{next(counter):04d}"
