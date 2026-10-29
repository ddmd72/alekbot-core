"""
The cut of an open bound and its lines (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29.4 L8, §29.7). Pure; kept apart from
`drive_open_bound` because `drive_transcript_render` needs it and `drive_open_bound` reaches `drive_search`, which
imports the renderer.
"""
from __future__ import annotations

from typing import List

from .drive_open_bound_text import BoundText
from .drive_open_texts import (
    CUT_INDEXED, CUT_NOT_INDEXED, CUT_STALE, STAND_IN_CUT, STAND_IN_CUT_STALE_TAIL, WHY,
)
from .drive_text_extent import ExtentState, ExtentWhy, TextExtent


def head_at_paragraph(text: str, room: int) -> str:
    """Whole paragraphs only; a first paragraph larger than `room` is cut at `room`."""
    room = max(0, room)
    kept: List[str] = []
    size = 0
    for paragraph in text.split("\n\n"):
        added = len(paragraph) + (2 if kept else 0)
        if size + added > room:
            break
        kept.append(paragraph)
        size += added
    return "\n\n".join(kept) if kept else text[:room]


def cut_line(subject: str, cut: BoundText, extent: TextExtent) -> str:
    numbers = dict(subject=subject, chars=f"{cut.chars:,}", kept=f"{cut.kept:,}")
    if extent.state is ExtentState.PAGEABLE:
        return CUT_INDEXED.format(**numbers)
    if extent.state is ExtentState.STALE:
        return CUT_STALE.format(passages=f"{extent.passages:,}", **numbers)
    return CUT_NOT_INDEXED.format(why=WHY[extent.why or ExtentWhy.UNKNOWN], **numbers)


def stand_in_cut(handle: str, label: str, cut: BoundText, ref: str, extent: TextExtent) -> str:
    text = STAND_IN_CUT.format(handle=handle, label=label, kept=f"{cut.kept:,}", chars=f"{cut.chars:,}")
    if extent.state is ExtentState.STALE:
        text += STAND_IN_CUT_STALE_TAIL.format(ref=ref)
    return text
