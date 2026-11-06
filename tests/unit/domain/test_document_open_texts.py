"""The notes of an opened file (EXTERNAL_FILES_RFC §4.4 "Presentation", §4.3, D7). The four moved notes are pinned to
today's exact words, and `file_conversion` serves the same functions."""
import pytest

from src.domain import document_open_texts as texts
from src.domain.document_read import ReadOutcome, ReadStatus
from src.utils import file_conversion

MB = 1024 * 1024


def test_the_size_alert_is_todays():
    assert texts.size_alert("scan_0012.pdf", 7 * MB) == (
        "[Tool note: User attempted to attach 'scan_0012.pdf' (7.0 MB). File exceeds the 5 MB limit and was not "
        "included in this message. Ask the user to send a smaller file or paste the content directly.]")
    assert "the 10 MB limit" in texts.size_alert("notes.md", 12 * MB, 10 * MB)


def test_the_conversion_alert_is_todays():
    assert texts.conversion_alert("invoice_painting_es.pdf", "application/pdf") == (
        "[Tool note: User attempted to attach 'invoice_painting_es.pdf' (application/pdf). The file could not be read or "
        "is not a supported text format. Supported formats: images, PDF, plain text, CSV, DOCX, XLSX, MP3, WAV. Ask "
        "the user to convert the file or paste the content directly.]")
    assert "'scan_0012.pdf'. The file" in texts.conversion_alert("scan_0012.pdf")


def test_the_audio_note_is_todays():
    assert texts.audio_unavailable_note("meeting_2026_09_14.m4a") == (
        "[Tool note: User attached an audio file 'meeting_2026_09_14.m4a'. Audio transcription is not available. Ask the "
        "user to provide a text transcript or describe what they want to discuss about the audio.]")


def test_the_cut_note_is_todays():
    assert texts.cut_note("scan_0012.pdf", 60_000, 1_500) == (
        "[Tool note: File 'scan_0012.pdf' was truncated — only the first 60,000 characters were included (1,500 "
        "characters omitted). Inform the user that the file was too large for full analysis and suggest splitting it "
        "or pasting only the relevant section.]")


def test_file_conversion_serves_the_moved_notes():
    assert file_conversion._size_alert is texts.size_alert
    assert file_conversion._conversion_alert is texts.conversion_alert


def test_truncate_with_alert_ends_with_the_cut_note():
    cap = file_conversion.MAX_CONVERTED_CHARS
    out = file_conversion.truncate_with_alert("a" * (cap + 7), "scan_0012.pdf")
    assert out == "a" * cap + "\n" + texts.cut_note("scan_0012.pdf", cap, 7)


def test_not_in_time_reads_the_same_for_both():
    assert texts.still_working_note("scan_0012.pdf") == texts.deadline_note("scan_0012.pdf")
    assert "'scan_0012.pdf' could not be read in time; try again" in texts.deadline_note("scan_0012.pdf")


@pytest.mark.parametrize("note,words", [
    (texts.model_unavailable_note, "document reader is temporarily unavailable"),
    (texts.parser_unavailable_note, "file converter is temporarily unavailable"),
    (texts.image_too_large_note, "too large to view"),
    (texts.image_format_note, "format that cannot be read"),
])
def test_each_new_note_names_the_file_and_its_reason(note, words):
    line = note("scan_0012.pdf")
    assert line.startswith("[Tool note: ") and line.endswith("]")
    assert "scan_0012.pdf" in line and words in line


def test_parser_outage_asks_for_the_file_again():
    assert "ask the user to send it again later" in texts.parser_unavailable_note("scan_0012.pdf")


def _read(**kwargs) -> ReadOutcome:
    return ReadOutcome(status=ReadStatus.TEXT, text="x", **kwargs)


def test_a_read_that_was_not_page_wise_has_no_line():
    assert texts.shown_line("notes.md", _read()) == ""
    assert texts.shown_line("scan_0012.pdf", _read(pages_total=12)) == ""


def test_pages_read_in_full_of_the_total():
    assert texts.shown_line("scan_0012.pdf", _read(pages_total=40, pages_read=10)) == (
        "[Tool note: 'scan_0012.pdf': pages 1–10 of 40 read in full.]")


def test_one_page_and_an_unknown_total():
    assert texts.shown_line("scan_0012.pdf", _read(pages_read=1)) == "[Tool note: 'scan_0012.pdf': page 1 read in full.]"


def test_the_cut_window_and_skipped_pages():
    outcome = _read(pages_total=40, pages_read=10, cut_within_pages=tuple(range(11, 21)), skipped_pages=(7, 4))
    assert texts.shown_line("scan_0012.pdf", outcome) == (
        "[Tool note: 'scan_0012.pdf': pages 1–10 of 40 read in full; text cut within pages 11–20; page 4 skipped; "
        "page 7 skipped.]")


def test_a_cut_inside_the_first_window():
    assert texts.shown_line("scan_0012.pdf", _read(pages_total=3, cut_within_pages=(1, 2, 3))) == (
        "[Tool note: 'scan_0012.pdf': text cut within pages 1–3.]")


@pytest.mark.parametrize("pages,words", [((5,), "page 5"), ((3, 5, 9), "pages 3, 5, 9"), ((2, 1, 2), "pages 1–2")])
def test_page_sets_are_named_plainly(pages, words):
    assert f"text cut within {words}." in texts.shown_line("scan_0012.pdf", _read(cut_within_pages=pages))


def test_may_be_incomplete_is_said():
    assert texts.shown_line("scan_0012.pdf", _read(may_be_incomplete=True)) == (
        "[Tool note: 'scan_0012.pdf': the text may be incomplete.]")
