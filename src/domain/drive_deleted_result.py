"""The deleted-files search's answer: the rendered text, the drive files its entries open, and whether any entry carries
stored transcript text (a passage). Pure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .user_drive import DriveItem


@dataclass(frozen=True)
class DeletedSearchResult:
    text: str
    items: List[DriveItem]
    has_passage: bool = False
