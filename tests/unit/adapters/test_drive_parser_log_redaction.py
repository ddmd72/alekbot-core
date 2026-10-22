"""What a parser failure leaves in logs and outcome details (R3 F3-5, USER_DRIVE_INDEXING_RFC §9): the exception
type, the code position that raised it and a short hash of the message; the child's stderr as its length and a hash.
Never the message or the stderr text, which can quote the document being parsed."""
import logging

import pytest

from src.adapters.drive_parser import readers
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import ExtractionRequest, ExtractionStatus, PdfSplitFailed, failure_label, text_digest
from tests.unit.adapters.drive_parser_stderr_stub import SECRET

STDERR_STUB = "tests.unit.adapters.drive_parser_stderr_stub"


def _raise(_path, _kind):
    raise ValueError(f"unexpected token in '{SECRET}'")


def test_failure_label_names_type_position_and_hash_not_the_message():
    try:
        _raise("", "")
    except ValueError as exc:
        label, message = failure_label(exc), str(exc)
    assert SECRET not in label and "unexpected token" not in label
    assert label.startswith("ValueError at test_drive_parser_log_redaction.py:")
    assert label.endswith(f"#{text_digest(message)}")


def test_text_digest_is_short_and_stable():
    assert text_digest("abc") == text_digest(b"abc") and len(text_digest("abc")) == 8
    assert text_digest("abc") != text_digest("abd")


def test_a_reader_failure_detail_carries_no_message(tmp_path, monkeypatch):
    monkeypatch.setitem(readers._READERS, "text", _raise)
    path = tmp_path / "a.txt"
    path.write_text("x")
    outcome = readers.read_content(str(path), "text")
    assert outcome.status is ExtractionStatus.ERROR
    assert SECRET not in outcome.detail and outcome.detail.startswith("ValueError at ")


def test_a_page_window_failure_detail_carries_no_message():
    with pytest.raises(PdfSplitFailed) as failed:
        with readers._pdf_failures():
            _raise("", "")
    assert SECRET not in failed.value.detail and failed.value.detail.startswith("ValueError at ")


async def test_child_stderr_reaches_logs_and_detail_only_as_length_and_hash(tmp_path, caplog):
    path = tmp_path / "input.bin"
    path.write_bytes(b"x")
    work = tmp_path / "work"
    work.mkdir()
    extractor = SubprocessContentExtractor(child_module=STDERR_STUB, temp_root=str(work))
    request = ExtractionRequest(path=str(path), name="input.bin", kind="text", mime_type="", user_id="u1",
                                account_id="a1")
    with caplog.at_level(logging.DEBUG):
        outcome = await extractor.extract(request)
    assert outcome.status is ExtractionStatus.ERROR
    assert SECRET not in outcome.detail and SECRET not in caplog.text
    assert "stderr " in outcome.detail and " bytes #" in outcome.detail
