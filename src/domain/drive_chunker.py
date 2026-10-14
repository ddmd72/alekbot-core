"""
Chunker (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.4, §8.3, §5.4). Pure.

Public interface:
    TextChunk(text, char_start, char_end)
    ChunkedText(chunks, truncated)
    chunk_text(text, *, max_chunks=MAX_CHUNKS_PER_FILE) -> ChunkedText

Chunks of about CHUNK_CHARS end at a paragraph boundary when one leaves the chunk at least half full, else at a
sentence or line boundary, else at a word boundary or a hard cut. The next chunk repeats the trailing sentences of
the previous one that fit in CHUNK_OVERLAP; a Markdown table is never overlapped, its continued chunks start with
the table's header row and separator instead. `char_start`/`char_end` are the span of the source text a chunk covers;
a repeated header is not part of that span, so `text` is then the header plus `source[char_start:char_end]`.
A change here bumps EXTRACTION_VERSION (§12.3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .drive_index_params import CHUNK_CHARS, CHUNK_OVERLAP, MAX_CHUNKS_PER_FILE


@dataclass(frozen=True)
class TextChunk:
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ChunkedText:
    chunks: Tuple[TextChunk, ...]
    truncated: bool          # text was left after max_chunks (§5.4)


@dataclass(frozen=True)
class _Unit:
    """The smallest piece a chunk boundary may fall after: a sentence or line, a table row, a hard-cut part."""
    start: int
    end: int
    block_start: bool        # first unit of a paragraph or a table: a boundary before it is a paragraph boundary
    table: Optional[int]     # index of the table it belongs to
    header: bool = False     # the table's header row and separator


_BLOCK_SPLIT = re.compile(r"\n[ \t]*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?…。！？])\s+|\n")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _blocks(text: str) -> List[Tuple[int, int]]:
    """Paragraph spans, stripped of surrounding whitespace."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    for m in _BLOCK_SPLIT.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    out: List[Tuple[int, int]] = []
    for start, end in spans:
        piece = text[start:end]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        if end - trail > start + lead:
            out.append((start + lead, end - trail))
    return out


def _lines(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    pos = start
    while pos < end:
        nl = text.find("\n", pos, end)
        stop = end if nl < 0 else nl
        piece = text[pos:stop]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        if stop - trail > pos + lead:
            out.append((pos + lead, stop - trail))
        pos = stop + 1
    return out


def _is_table(text: str, lines: List[Tuple[int, int]]) -> bool:
    return len(lines) >= 2 and all(text[s:e].startswith("|") for s, e in lines)


def _hard_parts(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Cut a span longer than CHUNK_CHARS at the last whitespace in the second half of each window."""
    parts: List[Tuple[int, int]] = []
    while end - start > CHUNK_CHARS:
        limit = start + CHUNK_CHARS
        cut = text.rfind(" ", start + CHUNK_CHARS // 2, limit)
        cut = limit if cut < 0 else cut
        parts.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        parts.append((start, end))
    return parts


def _sentences(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    pos = start
    for m in _SENTENCE_END.finditer(text, start, end):
        if m.start() > pos:
            out.append((pos, m.start()))
        pos = m.end()
    if end > pos:
        out.append((pos, end))
    return out


def _units(text: str) -> List[_Unit]:
    units: List[_Unit] = []
    tables = 0
    for b_start, b_end in _blocks(text):
        lines = _lines(text, b_start, b_end)
        if _is_table(text, lines):
            table = tables
            tables += 1
            rows = lines
            if (_TABLE_SEPARATOR.match(text[lines[1][0]:lines[1][1]])
                    and lines[1][1] - lines[0][0] <= CHUNK_CHARS // 2):   # a wider header is not repeated
                units.append(_Unit(lines[0][0], lines[1][1], True, table, header=True))
                rows = lines[2:]
            first = not units or units[-1].table != table
            for r_start, r_end in rows:
                for p_start, p_end in _hard_parts(text, r_start, r_end):
                    units.append(_Unit(p_start, p_end, first, table))
                    first = False
            continue
        first = True
        for s_start, s_end in _sentences(text, b_start, b_end):
            for p_start, p_end in _hard_parts(text, s_start, s_end):
                units.append(_Unit(p_start, p_end, first, None))
                first = False
    return units


def _header_prefix(text: str, unit: _Unit, headers: Dict[int, _Unit]) -> str:
    """The header of the table `unit` continues, unless the chunk starts with it, or `unit` would not fit with it."""
    if unit.table is None or unit.header:
        return ""
    header = headers.get(unit.table)
    if header is None:
        return ""
    prefix = text[header.start:header.end] + "\n"
    return prefix if len(prefix) + unit.end - unit.start <= CHUNK_CHARS else ""


def chunk_text(text: str, *, max_chunks: int = MAX_CHUNKS_PER_FILE) -> ChunkedText:
    units = _units(text)
    headers = {u.table: u for u in units if u.header and u.table is not None}
    chunks: List[TextChunk] = []
    i = 0
    while i < len(units) and len(chunks) < max_chunks:
        prefix = _header_prefix(text, units[i], headers)
        start = units[i].start
        j = i
        while j + 1 < len(units) and units[j + 1].end - start + len(prefix) <= CHUNK_CHARS:
            j += 1
        if j + 1 < len(units):
            # prefer to end before a paragraph or table when that keeps the chunk at least half full
            for k in range(j, i, -1):
                if units[k + 1].block_start and units[k].end - start + len(prefix) >= CHUNK_CHARS // 2:
                    j = k
                    break
        end = units[j].end
        chunks.append(TextChunk(prefix + text[start:end], start, end))
        i = _next_start(units, i, j)
    return ChunkedText(tuple(chunks), truncated=i < len(units))


def _next_start(units: List[_Unit], i: int, j: int) -> int:
    """Overlap (§8.4): the next chunk starts with the trailing prose units of chunk i..j that fit in CHUNK_OVERLAP,
    and only if it can still take the first new unit; a table is never overlapped."""
    nxt = j + 1
    if nxt >= len(units) or units[nxt].table is not None:
        return nxt
    end = units[j].end
    best = nxt
    for k in range(j, i, -1):
        if units[k].table is not None or end - units[k].start > CHUNK_OVERLAP:
            break
        if units[nxt].end - units[k].start <= CHUNK_CHARS:
            best = k
    return best
