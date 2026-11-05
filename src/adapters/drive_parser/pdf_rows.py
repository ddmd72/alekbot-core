"""PDF table rows (EXTERNAL_FILES_RFC §7 U5 (2), A6): a page with a table gets one pipe line per row, so a label and its
numbers stay on one line; a page without a table keeps pdfminer's text (the caller decides, this returns None).

Runs only inside the sandboxed child. pdfplumber's `lines` strategy finds ruled tables; the `text` strategy (aligned
columns, no rules) runs only when no ruled table was found and only under a guard: at least _MIN_ROWS rows, at least
_MIN_COLUMNS columns and one mostly numeric column, so a two-column prose page never becomes a table.

Public interface:
    table_page_text(page) -> Optional[str]     # the page text with its tables as pipe rows; None: no table here
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

_MIN_ROWS = 3
_MIN_COLUMNS = 2
_NUMERIC_SHARE = 0.6
_NUMBER = re.compile(r"^[-+(]?[€$£]?\s*\d[\d .,' ]*\s*(?:%|[€$£]|EUR|USD|GBP)?\)?$")

_Found = List[Tuple[Tuple[float, float, float, float], List[List[str]]]]


def table_page_text(page: Any) -> Optional[str]:
    """The text of one pdfplumber page with each detected table as one `| a | b |` line per row and the rest of the
    page (above, between and below the tables) as pdfplumber extracts it; None when the page has no table."""
    tables = _ruled_tables(page) or _aligned_tables(page)
    if not tables:
        return None
    segments: List[str] = []
    cursor = 0.0
    for box, rows in sorted(tables, key=lambda item: item[0][1]):
        segments.append(_text_between(page, cursor, box[1]))
        segments.append("\n".join(_pipe_row(row) for row in rows))
        cursor = box[3]
    segments.append(_text_between(page, cursor, float(page.height)))
    return "\n".join(segment for segment in segments if segment.strip())


def _ruled_tables(page: Any) -> _Found:
    if not (page.lines or page.rects or page.curves):
        return []                                       # nothing is drawn: the lines strategy cannot find a table
    found: _Found = []
    for table in page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"}):
        rows = _clean(table.extract())
        if len(rows) >= 2 and _columns(rows) >= _MIN_COLUMNS:
            found.append((table.bbox, rows))
    return found


def _aligned_tables(page: Any) -> _Found:
    found: _Found = []
    for table in page.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"}):
        rows = _clean(table.extract())
        if len(rows) >= _MIN_ROWS and _columns(rows) >= _MIN_COLUMNS and _has_numeric_column(rows):
            found.append((table.bbox, rows))
    return found


def _clean(raw: Sequence[Sequence[Optional[str]]]) -> List[List[str]]:
    rows = [[" ".join((cell or "").split()) for cell in row] for row in raw]
    return [row for row in rows if any(row)]


def _columns(rows: Sequence[Sequence[str]]) -> int:
    return max((sum(1 for cell in row if cell) for row in rows), default=0)


def _has_numeric_column(rows: Sequence[Sequence[str]]) -> bool:
    width = max(len(row) for row in rows)
    for column in range(width):
        cells = [row[column] for row in rows if column < len(row) and row[column]]
        if len(cells) >= _MIN_ROWS - 1 and sum(1 for cell in cells if _NUMBER.match(cell)) >= _NUMERIC_SHARE * len(cells):
            return True
    return False


def _pipe_row(row: Sequence[str]) -> str:
    cells = list(row)
    while cells and not cells[-1]:
        cells.pop()
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _text_between(page: Any, top: float, bottom: float) -> str:
    if bottom - top < 1:
        return ""
    return str(page.crop((0, max(top, 0.0), float(page.width), min(bottom, float(page.height)))).extract_text() or "")
