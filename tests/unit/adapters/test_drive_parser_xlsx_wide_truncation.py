"""XLSX truncation is honest past the column read bound (arbiter R1-3; USER_DRIVE_INDEXING_RFC §5.1, §8.3; review A
"M xlsx"). Rows are read at most 1025 columns wide so a far-right cell cannot pad every row to 16384 values, but a
value past column 1025 is content the index does not hold: the outcome says `truncated`. A declared dimension past it
is not (R2 P-m3: the sheet's own cells decide, a dimension alone can be bogus)."""
import re
import zipfile

from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus


def _book(path, cells):
    """One sheet; `cells` is a list of (row, column, value)."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Wide"
    for row, column, value in cells:
        sheet.cell(row=row, column=column, value=value)
    book.save(str(path))
    return path


def _declare(path, ref, target):
    """A copy of the workbook whose first sheet declares the dimension `ref`, whatever its cells hold."""
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(target, "w") as out:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text = data.decode()
                assert "<dimension " in text
                data = re.sub(r'<dimension ref="[^"]*"', f'<dimension ref="{ref}"', text).encode()
            out.writestr(item, data)
    return target


def test_a_value_in_column_2000_is_text_marked_truncated_in_bounded_memory(tmp_path):
    import tracemalloc

    path = _book(tmp_path / "wide.xlsx", [(i, 1, f"r{i}") for i in range(1, 601)]
                 + [(i, 2000, "far") for i in range(1, 601)])
    tracemalloc.start()
    try:
        outcome = read_content(str(path), "xlsx")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.truncated is True
    assert "| r600 |" in outcome.text and "far" not in outcome.text
    assert peak < 40 * 1024 * 1024        # the same bound as the far-right-cell test: nothing padded to column 2000


def test_one_value_past_the_bound_on_a_single_row_marks_truncated(tmp_path):
    path = _book(tmp_path / "w.xlsx", [(1, 1, "Item"), (2, 1, "Rent"), (3, 1500, "hidden")])
    outcome = read_content(str(path), "xlsx")
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.truncated is True


def test_a_declared_dimension_past_the_bound_alone_is_not_truncation(tmp_path):
    plain = _book(tmp_path / "p.xlsx", [(1, 1, "Item"), (1, 2, "Cost"), (2, 1, "Rent"), (2, 2, 1200)])
    declared = _declare(plain, "A1:BZZ2", tmp_path / "declared.xlsx")
    outcome = read_content(str(declared), "xlsx")
    assert outcome.status is ExtractionStatus.TEXT
    assert "| Rent | 1200 |" in outcome.text
    assert outcome.truncated is False       # every value was read; only a value past the bound truncates (R2 P-m3)


def test_a_last_value_inside_the_column_cap_is_not_truncated(tmp_path):
    path = _book(tmp_path / "w.xlsx", [(1, 1, "a"), (1, 1024, "z")])
    outcome = read_content(str(path), "xlsx")
    assert outcome.truncated is False


def test_a_declared_dimension_inside_the_bound_is_not_truncated(tmp_path):
    plain = _book(tmp_path / "p.xlsx", [(1, 1, "Item"), (2, 1, "Rent")])
    declared = _declare(plain, "A1:D2", tmp_path / "declared.xlsx")
    assert read_content(str(declared), "xlsx").truncated is False

