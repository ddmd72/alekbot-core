"""PdfSplitFailed carries the outcome status of a PDF the sandboxed split could not cut (USER_DRIVE_INDEXING_RFC
§24.3): the reader returns that status as its own outcome."""
from src.domain.drive_extraction import ExtractionStatus, PdfSplitFailed


def test_carries_status_and_detail():
    failed = PdfSplitFailed(ExtractionStatus.ENCRYPTED, "user password")
    assert failed.status is ExtractionStatus.ENCRYPTED
    assert failed.detail == "user password"
    assert str(failed) == "encrypted: user password"


def test_detail_is_optional():
    failed = PdfSplitFailed(ExtractionStatus.TIMEOUT)
    assert failed.detail == "" and str(failed) == "timeout"
