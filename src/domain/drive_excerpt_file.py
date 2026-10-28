"""One file of a passages result (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A8, A10, A11): its identity, the
marks its head line carries and its selected chunks. Pure."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .drive_excerpt_chunk import ExcerptChunk
from .user_drive import DriveItem


@dataclass(frozen=True)
class ExcerptFile:
    """`chunk_count` is N of "passages i–j of N"; `capture` the record's (`drive_capture`, "" when none recorded);
    `card_accuracy` the card's score, set only for a record `drive_accuracy.scorable` scores."""
    item_id: str
    ref: str
    name: str
    path: str
    kind: str
    generation: int
    chunk_count: int
    size_bytes: int = 0
    modified_at: Optional[datetime] = None
    web_url: str = ""
    stale: bool = False
    truncated: bool = False
    capture: str = ""
    card_accuracy: Optional[int] = None
    content_hash: Optional[str] = None
    also_at: Tuple[str, ...] = ()
    chunks: Tuple[ExcerptChunk, ...] = ()

    def drive_item(self) -> DriveItem:
        """The `drive_context` item, built as `DriveSearchCard.drive_item` builds it."""
        return DriveItem(item_id=self.item_id, name=self.name, path=self.path, is_folder=False,
                         size_bytes=self.size_bytes, modified_at=self.modified_at, web_url=self.web_url)
