"""A noted ancestor folder of a passages file (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A18). Pure."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FolderNote:
    """`depth` counts levels from the drive root (a top-level folder is 1); `line` is the folder record's
    `drive_card.note_line`, never empty for a folder that gets one."""
    item_id: str
    path: str
    depth: int
    line: str
