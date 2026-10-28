"""
A `find_passages` request and the parsing of both passages tools' arguments
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A3, A5, A7). Pure.

A refusal is a `ValueError` whose message is the correction text the specialist's model reads. CRUD: an empty field
is "not given"; a required one empty is refused, never guessed.

Public interface:
    ExcerptRequest(question, folder, file_refs)
    parse_file_refs(value) -> (ref, ...)         a list or a comma string of drive: refs, at most EXCERPT_FILE_REFS_MAX
                                                 (a passages head line's id `drive:<id>:<i>-<j>` or `:<i>` is a ref:
                                                 the passage numbers are dropped, here and in range_request)
    parse_range(value) -> (first, last)          "3", "3-5", "3–5": display numbers, at most EXCERPT_RANGE_MAX of them
    excerpt_request(question, folder, file_refs) -> ExcerptRequest
    range_request(file_ref, passages) -> ExcerptRangeRequest
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Tuple

from .drive_excerpt_range_request import ExcerptRangeRequest
from .drive_index_params import EXCERPT_FILE_REFS_MAX, EXCERPT_RANGE_MAX
from .user_drive import make_drive_ref, parse_drive_ref

QUESTION_REQUIRED = "question is required: the full question the passages should answer, in the user's words."
FILE_REF_REQUIRED = "file_ref is required: the drive: ref of the file, from a passages head line or a card."
PASSAGES_REQUIRED = "passages is required: a passage number or a range such as 3-5, from a passages head line."
NOT_A_REF = "'{value}' is not a drive: ref. Give the drive: ref of each file, from a card or a passages head line."
TOO_MANY_REFS = "At most {limit} files per call ({count} given): name the files that matter most."
BAD_RANGE = "'{value}' is not a passage number or a range such as 3-5."
REVERSED_RANGE = "The range '{value}' ends before it starts: give the first passage, then the last."
WIDE_RANGE = "At most {limit} passages per call ('{value}' is {count}): read the rest in a second call."

_RANGE = re.compile(r"\s*(\d+)\s*(?:[-–—]\s*(\d+)\s*)?")
# The passage numbers a head line appends to the ref (`drive_excerpt_render.HEAD`: `[id <ref>:<i>-<j> · …]`).
_HEAD_NUMBERS = re.compile(r":\d+(?:-\d+)?$")


@dataclass(frozen=True)
class ExcerptRequest:
    """`folder` is a folder path or a drive: folder ref ("" for none); `file_refs` normalised drive: refs."""
    question: str
    folder: str = ""
    file_refs: Tuple[str, ...] = ()


def _ref(raw: Any) -> str:
    text = str(raw).strip()
    item_id = parse_drive_ref(text)
    if item_id is None:
        raise ValueError(NOT_A_REF.format(value=text))
    item_id = _HEAD_NUMBERS.sub("", item_id)
    if not item_id:
        raise ValueError(NOT_A_REF.format(value=text))
    return make_drive_ref(item_id)


def parse_file_refs(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    raw = value.split(",") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else [value]
    refs: List[str] = []
    for part in raw:
        if not str(part).strip():
            continue
        ref = _ref(part)
        if ref not in refs:
            refs.append(ref)
    if len(refs) > EXCERPT_FILE_REFS_MAX:
        raise ValueError(TOO_MANY_REFS.format(limit=EXCERPT_FILE_REFS_MAX, count=len(refs)))
    return tuple(refs)


def parse_range(value: Any) -> Tuple[int, int]:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(PASSAGES_REQUIRED)
    match = _RANGE.fullmatch(text)
    if match is None:
        raise ValueError(BAD_RANGE.format(value=text))
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) is not None else first
    if first < 1:
        raise ValueError(BAD_RANGE.format(value=text))
    if last < first:
        raise ValueError(REVERSED_RANGE.format(value=text))
    if last - first + 1 > EXCERPT_RANGE_MAX:
        raise ValueError(WIDE_RANGE.format(limit=EXCERPT_RANGE_MAX, value=text, count=last - first + 1))
    return first, last


def excerpt_request(question: Any, folder: Any = "", file_refs: Any = None) -> ExcerptRequest:
    text = " ".join(str(question or "").split())
    if not text:
        raise ValueError(QUESTION_REQUIRED)
    return ExcerptRequest(question=text, folder=str(folder or "").strip(), file_refs=parse_file_refs(file_refs))


def range_request(file_ref: Any, passages: Any) -> ExcerptRangeRequest:
    if not str(file_ref or "").strip():
        raise ValueError(FILE_REF_REQUIRED)
    ref = _ref(file_ref)
    first, last = parse_range(passages)
    return ExcerptRangeRequest(file_ref=ref, first=first, last=last)
