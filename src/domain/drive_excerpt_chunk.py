"""
One indexed chunk shown as a passage (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A10), and the one place passage
numbers convert. Pure.

`index` (and `ChunkId`) hold the stored chunk index, 0-based. Every number a model sees or gives is the display
number, index + 1: head lines, "passages i–j of N", `ExcerptRun.first/last`, `ExcerptResult.continue_at`,
`ExcerptRangeRequest.first/last`.

Public interface:
    ExcerptChunk(item_id, generation, index, text, char_start, char_end), .key -> ChunkId
    display_number(index) -> int;  stored_index(number) -> int
"""
from __future__ import annotations

from dataclasses import dataclass

from .drive_index_lean import ChunkId


def display_number(index: int) -> int:
    return index + 1


def stored_index(number: int) -> int:
    if number < 1:
        raise ValueError(f"passage numbers start at 1, got {number}")
    return number - 1


@dataclass(frozen=True)
class ExcerptChunk:
    """`text` is the stored text (markdown escapes kept, a repeated table header included); `char_start` /
    `char_end` the span of the source text it covers (`drive_chunker`)."""
    item_id: str
    generation: int
    index: int
    text: str
    char_start: int
    char_end: int

    @property
    def key(self) -> ChunkId:
        return (self.item_id, self.generation, self.index)
