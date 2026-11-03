"""OpenedFile and Prefetched (EXTERNAL_FILES_RFC §4.2, P18): exactly one answer per open; content or a note."""
import pytest

from src.domain.document_open import OpenedFile, Prefetched
from src.domain.document_read import ReadOutcome, ReadStatus

TEXT = ReadOutcome(status=ReadStatus.TEXT, kind="pdf", text="Lukas Weber owns the Q3 budget.")


def test_a_body_is_todays_file_wrapper_raw():
    body = "[File: scan_0012.pdf]\nLukas Weber owns the Q3 budget.\n[/File: scan_0012.pdf]"
    opened = OpenedFile(TEXT, body=body)
    assert opened.body == body and opened.note == "" and opened.native_path == ""


def test_a_note_without_a_read():
    opened = OpenedFile(None, note="[System: 'Docs' is a folder, not a file.]")
    assert opened.outcome is None and opened.note.startswith("[System:")


def test_a_native_file_carries_path_and_mime():
    native = ReadOutcome(status=ReadStatus.NATIVE, kind="pdf", native_mime="application/pdf")
    opened = OpenedFile(native, native_path="/tmp/stage/scan_0012.pdf", native_mime="application/pdf")
    assert (opened.native_path, opened.native_mime) == ("/tmp/stage/scan_0012.pdf", "application/pdf")


@pytest.mark.parametrize("kwargs", [
    {},
    {"body": "b", "note": "n"},
    {"body": "b", "native_path": "/p", "native_mime": "application/pdf"},
    {"note": "n", "native_path": "/p", "native_mime": "application/pdf"},
])
def test_exactly_one_answer(kwargs):
    with pytest.raises(ValueError):
        OpenedFile(TEXT, **kwargs)


@pytest.mark.parametrize("kwargs", [{"native_path": "/p"}, {"body": "b", "native_mime": "application/pdf"}])
def test_a_native_file_needs_both_halves(kwargs):
    with pytest.raises(ValueError):
        OpenedFile(TEXT, **kwargs)


def test_a_prefetch_gives_content_or_a_note():
    assert Prefetched(content="x").note == ""
    assert Prefetched(note="[System: x]").content == ""
    assert Prefetched() == Prefetched(content="", note="")
    with pytest.raises(ValueError):
        Prefetched(content="x", note="y")
