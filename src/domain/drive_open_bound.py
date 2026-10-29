"""
The open bound (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §29): a drive text above `OPEN_MAX_CHARS` is never handed to a
model whole. Pure.

Public interface:
    refuses(extent, cap) -> bool                 L1: pageable and over the cap, or pageable and truncated
    head_at_paragraph(text, room) -> str         the longest prefix of whole `\\n\\n` paragraphs within `room`
    bound(text, extent, cap) -> BoundText        L8: whole within the cap, else the head
    refusal_text(path, extent) -> str            the outcome's text for a refused open
    cut_line(subject, bound, extent) -> str      the line after a cut body (subject: `path_subject(path)` or the transcript)
    stand_in_cut(handle, label, bound, ref, extent) -> str   the specialist's stand-in for a cut body
    path_subject(path) -> str                    the quoted path
"""
from __future__ import annotations

from typing import List

from .drive_excerpt_context import quoted
from .drive_index_params import OPEN_MAX_CHARS
from .drive_open_bound_text import BoundText
from .drive_open_texts import (
    CUT_INDEXED, CUT_NOT_INDEXED, CUT_STALE, REFUSAL, REFUSAL_TRUNCATED, STAND_IN_CUT, STAND_IN_CUT_STALE_TAIL, WHY,
)
from .drive_text_extent import ExtentState, ExtentWhy, TextExtent


def refuses(extent: TextExtent, cap: int = OPEN_MAX_CHARS) -> bool:
    """A truncated file is refused whatever its indexed length (L2); a stale or unindexed one is never refused (L1)."""
    return extent.state is ExtentState.PAGEABLE and (extent.text_chars > cap or extent.truncated)


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


def bound(text: str, extent: TextExtent, cap: int = OPEN_MAX_CHARS) -> BoundText:
    if len(text) <= cap:
        return BoundText(text, len(text), len(text))
    head = head_at_paragraph(text, cap)
    return BoundText(head, len(text), len(head), extent.state)


def path_subject(path: str) -> str:
    return quoted(path)


def refusal_text(path: str, extent: TextExtent) -> str:
    template = REFUSAL_TRUNCATED if extent.truncated else REFUSAL
    return template.format(path=quoted(path), chars=f"{extent.text_chars:,}", passages=f"{extent.passages:,}")


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
