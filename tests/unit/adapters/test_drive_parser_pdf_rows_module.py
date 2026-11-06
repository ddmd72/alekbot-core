"""Direct tests of `pdf_rows.table_page_text` on real pdfplumber pages: the gate (table page vs prose page), the ruled
and the aligned branch, the pipe-row shape and the numeric-line threshold. The page cap lives in `readers`, not here."""
from pathlib import Path
from typing import Sequence

import pdfplumber
import pytest

from src.adapters.drive_parser.pdf_rows import MIN_NUMERIC_LINES, NUMBER_CELL, table_page_text
from tests.unit.adapters.test_drive_parser_pdf_rows import INVOICE, _grid, _text, _write_pdf

_COLUMNS = [0, 220, 300, 400]


def _invoice_stream(ruled: bool) -> bytes:
    cells = [(72, 700, "Bathroom repair invoice for Alex Doe, 12 Oak Street, Springfield")]
    for r, row in enumerate(INVOICE):
        for c, value in enumerate(row):
            if value:
                cells.append((72 + _COLUMNS[c] + 6, 584 - r * 24, value))
    return (_grid(72, 600, _COLUMNS, len(INVOICE)) if ruled else b"") + _text(cells)


def _page_text(tmp_path: Path, stream: bytes, ruled: bool, aligned: bool):
    with pdfplumber.open(str(_write_pdf(tmp_path / "p.pdf", [stream]))) as pdf:
        return table_page_text(pdf.pages[0], ruled, aligned)


def test_a_ruled_page_gives_pipe_rows_with_each_number_on_its_label_line(tmp_path):
    out = _page_text(tmp_path, _invoice_stream(ruled=True), ruled=True, aligned=False)
    lines = out.splitlines()
    assert "| Bathroom repair | 852,40 | 852,40 |" in lines
    assert "| IVA 23 % | 852,40 | 196,05 |" in lines
    assert "| Total |  | 1.048,45 |" in lines                 # an empty middle cell stays, trailing empties go
    assert lines[0].startswith("Bathroom repair invoice")      # the prose above the table is kept, before it


def test_the_ruled_branch_needs_the_ruled_flag(tmp_path):
    stream = _invoice_stream(ruled=True)
    assert _page_text(tmp_path, stream, ruled=False, aligned=False) is None


def test_prose_with_both_flags_set_is_not_a_table(tmp_path):
    lines = ["The garden committee met on Tuesday", "to discuss the shed permit and the", "water schedule for the summer"]
    stream = _text([(72, 700 - i * 16, t) for i, t in enumerate(lines)])
    assert _page_text(tmp_path, stream, ruled=True, aligned=True) is None


def _numeric_page(count: int) -> bytes:
    cells = [(72, 700 - i * 20, "Row") for i in range(count)] + [(300, 700 - i * 20, str(100 * (i + 1))) for i in range(count)]
    return _text(cells)


@pytest.mark.parametrize("count, expected_rows", [(MIN_NUMERIC_LINES - 1, False), (MIN_NUMERIC_LINES, True)])
def test_the_aligned_branch_needs_three_rows(tmp_path, count, expected_rows):
    out = _page_text(tmp_path, _numeric_page(count), ruled=False, aligned=True)
    assert (out is not None) is expected_rows
    if out is not None:
        pipes = [line for line in out.splitlines() if line.startswith("|")]
        assert pipes and all(line.startswith("| Row | ") for line in pipes)       # each number stays on its label's line


def test_the_aligned_branch_ignores_a_table_without_a_numeric_column(tmp_path):
    words: Sequence[str] = ("red", "green", "blue", "gray")
    cells = [(72, 700 - i * 20, w) for i, w in enumerate(words)] + [(300, 700 - i * 20, w) for i, w in enumerate(words)]
    assert _page_text(tmp_path, _text(cells), ruled=False, aligned=True) is None


@pytest.mark.parametrize("cell, ok", [("852,40", True), ("1.048,45", True), ("23 %", True), ("(196,05)", True),
                                      ("Total", False), ("IVA 23 %", False), ("", False)])
def test_number_cell_accepts_only_numbers_and_amounts(cell, ok):
    assert bool(NUMBER_CELL.match(cell)) is ok


@pytest.mark.parametrize("count", [MIN_NUMERIC_LINES, MIN_NUMERIC_LINES + 2])
def test_every_row_of_an_aligned_table_is_a_table_row_and_none_is_repeated_as_loose_text(tmp_path, count):
    cells = [(72, 700 - i * 20, f"Row {i + 1}") for i in range(count)] + [(300, 700 - i * 20, str(100 * (i + 1))) for i in range(count)]
    out = _page_text(tmp_path, _text(cells), ruled=False, aligned=True)
    assert out is not None
    lines = out.splitlines()
    assert len(lines) == count                                    # all rows in the table, no loose line around it
    for i, line in enumerate(lines):
        assert line.startswith("| Row") and line.endswith(f"| {100 * (i + 1)} |") and f" {i + 1} " in line
