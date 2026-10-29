"""
A drive text bounded for an open (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29.4 L8). Pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .drive_text_extent import ExtentState


@dataclass(frozen=True)
class BoundText:
    """`text`: what is handed on; `chars`: the length of the whole; `kept`: the length of `text`;
    `cut`: the extent state the cut was decided under, or None when the whole text fits."""
    text: str
    chars: int
    kept: int
    cut: Optional[ExtentState] = None
