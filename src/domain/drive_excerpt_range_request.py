"""A `read_passages` request (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A7): one file's passages by number."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExcerptRangeRequest:
    """`first` and `last` are display numbers, 1-based, inclusive (`drive_excerpt_chunk.stored_index` converts)."""
    file_ref: str
    first: int
    last: int
    page: bool = False        # "from N": a page in file order (RFC §29.5 L9), not a neighbour range
