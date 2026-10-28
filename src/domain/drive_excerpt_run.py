"""One run of a file's adjacent passages, merged (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A7). Pure."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExcerptRun:
    """`first` / `last` are display numbers (`drive_excerpt_chunk.display_number`); `text` the stored text, merged."""
    item_id: str
    first: int
    last: int
    text: str
