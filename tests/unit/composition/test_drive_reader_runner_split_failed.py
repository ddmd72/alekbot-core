"""DriveReaderRunner with the sandboxed parser's PdfSplitFailed (USER_DRIVE_INDEXING_RFC §24.3): the split's own
status is the file's outcome, and no model call is made."""
import pytest

from src.domain.drive_extraction import ExtractionStatus, PdfSplitFailed

from .test_drive_reader_runner import FakeSplitter, _request, _runner, _text

S = ExtractionStatus


@pytest.mark.parametrize("status", [S.ENCRYPTED, S.TIMEOUT, S.MEMORY, S.TOO_LARGE, S.UNSUPPORTED, S.ERROR])
async def test_split_failure_status_is_the_outcome(tmp_path, status):
    runner, llm, _ = _runner(_text, splitter=FakeSplitter(25, error=PdfSplitFailed(status, "child said so")))
    out = await runner.extract(_request(tmp_path))
    assert out.status is status and llm.requests == []
    assert "child said so" in out.detail and out.model_calls == 0
