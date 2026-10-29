"""
What the index holds of one drive file's text, as the open bound needs it (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md
§29.4 L5). Pure.

Public interface:
    ExtentState         pageable | stale | not_indexed
    ExtentWhy           why a file is not pageable
    TextExtent          the answer of `DriveSearchService.text_extent`; `TextExtent.UNKNOWN` for a failed lookup (L7)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Optional


class ExtentState(str, Enum):
    PAGEABLE = "pageable"
    STALE = "stale"
    NOT_INDEXED = "not_indexed"


class ExtentWhy(str, Enum):
    OFF = "off"
    NOT_CONNECTED = "not_connected"
    REBUILDING = "rebuilding"
    NOT_READ = "not_read"
    NO_TEXT = "no_text"
    HIDDEN = "hidden"
    UNKNOWN = "unknown"          # a failed lookup (L7)


@dataclass(frozen=True)
class TextExtent:
    """`pageable` means exactly that `read_passages from 1` returns text (L3)."""
    UNKNOWN: ClassVar["TextExtent"]

    state: ExtentState
    why: Optional[ExtentWhy] = None
    text_chars: int = 0
    passages: int = 0
    truncated: bool = False
    version: int = 0
    pages_total: int = 0


TextExtent.UNKNOWN = TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.UNKNOWN)
