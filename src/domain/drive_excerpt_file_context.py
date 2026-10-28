"""Where a passages file sits and what it is (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A18). Pure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class FileContext:
    """`ancestors` are folder item ids, nearest first, as `drive_search.resolve_chain` returns them; `description`
    is `drive_excerpt_context.file_description` of the record (model-written: shown inside the wrapper);
    `own_note` the record's `drive_card.note_line` ("" for none)."""
    item_id: str
    path: str
    ancestors: Tuple[str, ...] = ()
    description: str = ""
    own_note: str = ""
