"""G1 (GOOGLE_DRIVE_RFC §5, design §1.1, Δ4): a document mime decides the kind when the name's extension gives none or
names another table kind; a text extension keeps its reading against a text mime, and an extension outside the table
(log, subtitles, ...) always keeps its reading."""
from pathlib import Path

import pytest

import src.domain.drive_index_policy as policy
from src.domain.document_kind import kind_of, mime_decides_kind, mime_kind

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# (name, mime, kind, the mime decides)
TRUTH = [
    ("Q3 budget", XLSX, "xlsx", True),
    ("brief.docx", "text/markdown", "markdown", True),
    ("budget.csv", XLSX, "xlsx", True),
    ("deck.pptx", "text/plain", "text", True),
    ("notes.md", "text/plain", "markdown", False),
    ("minutes.txt", "text/markdown", "text", False),
    ("server.log", "text/plain", "log", False),
    ("talk.srt", "text/plain", "subtitles", False),
    ("IMG_0001", "image/jpeg", "image", False),
    ("notes", "text/plain; charset=utf-8", "text", True),
    ("report.pdf", "", "pdf", False),
]


@pytest.mark.parametrize("name,mime,kind,decides", TRUTH)
def test_kind_of_follows_the_truth_table(name, mime, kind, decides):
    assert kind_of(name, mime, is_folder=False) == kind
    assert mime_decides_kind(name, mime) is decides


def test_a_folder_is_a_folder_whatever_its_mime():
    assert kind_of("Acme Hamburg", XLSX, is_folder=True) == "folder"


@pytest.mark.parametrize("mime,kind", [
    ("text/plain; charset=utf-8", "text"),
    ("Text/Markdown", "markdown"),
    (" APPLICATION/PDF ; q=1", "pdf"),
    (DOCX, "docx"),
    (PPTX, "pptx"),
    ("text/csv", "csv"),
])
def test_mime_kind_ignores_parameters_and_case(mime, kind):
    assert mime_kind(mime) == kind


@pytest.mark.parametrize("mime", ["", "image/jpeg", "application/octet-stream", "text/html"])
def test_a_mime_outside_the_table_names_no_kind(mime):
    assert mime_kind(mime) is None


def test_the_same_kind_from_both_sides_is_not_a_mime_decision():
    assert mime_decides_kind("Q3 budget.xlsx", XLSX) is False
    assert kind_of("Q3 budget.xlsx", XLSX, is_folder=False) == "xlsx"


def test_a_packaged_extension_is_read_through_the_package():
    assert mime_decides_kind("Garden plan.epub.pkgf", "text/plain") is False
    assert kind_of("Garden plan.epub.pkgf", "text/plain", is_folder=False) == "epub"


def test_the_policy_module_names_no_provider_type():
    assert "vnd.google-apps" not in Path(policy.__file__).read_text(encoding="utf-8")
