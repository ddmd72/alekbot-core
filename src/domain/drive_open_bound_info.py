"""
What an open's bound decided, carried on the outcome for the specialist's stand-in and the history
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29.6 L13). Pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .drive_text_extent import ExtentState, TextExtent


@dataclass(frozen=True)
class OpenBoundInfo:
    """`cut_state` is None for a refusal (nothing was delivered); `kept` is 0 then."""
    extent: TextExtent
    chars: int = 0
    kept: int = 0
    cut_state: Optional[ExtentState] = None
