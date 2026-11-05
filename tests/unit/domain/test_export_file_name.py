"""`document_kind.export_file_name`: a provider export's name gets the export's extension, never twice, and a mime
outside the table leaves the name alone (EXTERNAL_FILES U4b)."""
import pytest

from src.domain.document_kind import export_file_name


@pytest.mark.parametrize("name, mime, expected", [
    ("Q3 plan", "text/markdown", "Q3 plan.md"),
    ("Q3 plan.MD", "text/markdown", "Q3 plan.MD"),
    ("Hamburg talk", "text/plain; charset=utf-8", "Hamburg talk.txt"),
    ("Invoice bathroom", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Invoice bathroom.xlsx"),
    ("Garden plan 2026", "application/pdf", "Garden plan 2026.pdf"),
    ("v2.1 notes", "text/markdown", "v2.1 notes.md"),
    ("Q3 plan", "application/vnd.google-apps.form", "Q3 plan"),
    ("Q3 plan", "", "Q3 plan"),
])
def test_export_file_name(name, mime, expected):
    assert export_file_name(name, mime) == expected
