"""
Where a page of `read_passages` stands in its file (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29.5 L10). Pure.

Public interface:
    PageSpan(first, last, total, version, next, end, truncated, stale, past_end, path, ref)
    last_page_start(total) -> int      the display number the last page starts at
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .drive_index_params import EXCERPT_RANGE_MAX


def last_page_start(total: int) -> int:
    return max(1, total - EXCERPT_RANGE_MAX + 1)


@dataclass(frozen=True)
class PageSpan:
    """Display numbers, 1-based. `first`/`last`: the delivered passages (a past-the-end page carries the requested
    `first` and `last == first - 1`); `next`: the number the following page starts at, None at the end; `end`: the
    last passage of the indexed text was delivered; `truncated`: the file goes on past the indexed text."""
    first: int
    last: int
    total: int
    version: int
    next: Optional[int] = None
    end: bool = False
    truncated: bool = False
    stale: bool = False
    past_end: bool = False
    path: str = ""
    ref: str = ""
