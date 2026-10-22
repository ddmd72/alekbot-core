"""Kind words as users say them (USER_DRIVE_INDEXING_RFC §10.1): "documents" include PDFs, "Word files" are
Word formats only, and an extension takes a plural "s"."""
import pytest

from src.domain.drive_search import canonical_kind, kinds_for


def test_documents_include_pdf():
    assert "pdf" in kinds_for("document") and "pdf" in kinds_for("documents")
    assert {"docx", "doc", "odt", "rtf"} <= kinds_for("document")


@pytest.mark.parametrize("raw", ["word", "Word", "words"])
def test_word_means_word_formats_only(raw):
    assert kinds_for(raw) == frozenset({"docx", "doc"})


@pytest.mark.parametrize("raw,kind", [("jpgs", "image"), ("pngs", "image"), ("docxs", "docx"), ("PDFs", "pdf"),
                                      ("xlsxs", "xlsx"), (".mp4s", "video")])
def test_an_extension_takes_a_plural_s(raw, kind):
    assert canonical_kind(raw) == kind


@pytest.mark.parametrize("raw", ["files", "scans", "s", "ss"])
def test_other_plurals_are_still_refused(raw):
    with pytest.raises(ValueError):
        canonical_kind(raw)


def test_the_kind_field_description_names_what_each_family_covers():
    from src.domain.drive_tool_texts import KIND
    text = KIND   # the drive specialist's `search_drive.kind` (DRIVE_SPECIALIST_RFC §7.6, §8.1)
    assert "document (docx, doc, odt, rtf, pdf, markdown, text)" in text and "word (docx, doc)" in text   # D23
