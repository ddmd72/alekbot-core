"""The far-cell hook is what sees a value past the column read bound (round-2 review P-m2; USER_DRIVE_INDEXING_RFC §5.1,
§8.3). openpyxl writes a <dimension> that covers every cell it saves, so a fixture saved by openpyxl alone never
proves the hook: these sheets declare no dimension, or one that stops at column A, and still hold a value in column
1500. A hook that stops seeing cells (an openpyxl upgrade renaming `_get_row`) fails here."""
import re
import zipfile

import pytest

from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus


def _book(path, cells):
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Wide"
    for row, column, value in cells:
        sheet.cell(row=row, column=column, value=value)
    book.save(str(path))
    return path


def _rewrite_dimension(path, target, replacement):
    """A copy whose first sheet's <dimension .../> is `replacement` ("" removes it)."""
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(target, "w") as out:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text, count = re.subn(r"<dimension [^>]*/>", replacement, data.decode())
                assert count == 1
                data = text.encode()
            out.writestr(item, data)
    return target


@pytest.mark.parametrize("dimension", ["", '<dimension ref="A1:A2"/>'], ids=["removed", "understated"])
def test_a_value_past_column_1025_marks_truncated_whatever_the_sheet_declares(tmp_path, dimension):
    saved = _book(tmp_path / "w.xlsx", [(1, 1, "Item"), (2, 1, "Rent"), (2, 1500, "hidden")])
    sheet = _rewrite_dimension(saved, tmp_path / "declared.xlsx", dimension)
    outcome = read_content(str(sheet), "xlsx")
    assert outcome.status is ExtractionStatus.TEXT
    assert "| Rent |" in outcome.text and "hidden" not in outcome.text
    assert outcome.truncated is True


@pytest.mark.parametrize("dimension", ["", '<dimension ref="A1:A2"/>'], ids=["removed", "understated"])
def test_the_same_sheet_without_the_far_value_is_not_truncated(tmp_path, dimension):
    saved = _book(tmp_path / "w.xlsx", [(1, 1, "Item"), (2, 1, "Rent")])
    sheet = _rewrite_dimension(saved, tmp_path / "declared.xlsx", dimension)
    assert read_content(str(sheet), "xlsx").truncated is False
