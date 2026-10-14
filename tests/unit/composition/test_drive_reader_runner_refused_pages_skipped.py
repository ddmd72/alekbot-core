"""A refused page is skipped, not the file (R3-P1; USER_DRIVE_INDEXING_RFC §24.3, §24.8). HTTP 400/422 on a multi-page
window halves it like 413, down to the single page; a single page still refused (413, 400 or 422) is skipped and the
rest of the file is read and indexed, `truncated`, with the refused pages counted on the outcome and in the log line.
Only a file whose every page was refused is terminal: `too_large` when any page's refusal was 413, else
`unsupported`. No outcome here is a retried `error`."""
import pytest

from src.domain.drive_extraction import ExtractionStatus
from src.domain.exceptions import LLMClientError
from tests.unit.composition.test_drive_reader_runner_halving import _answer, _request, _runner

S = ExtractionStatus


def _refused(status):
    return LLMClientError("refused", http_status=status)


@pytest.mark.parametrize("status", [400, 422])
async def test_an_outright_refusal_of_a_window_halves_it_like_413(tmp_path, status):
    runner, _, cutter = _runner(_answer({"1-10": _refused(status)}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.truncated is False and out.pages_refused == 0
    assert out.text.split("\n\n")[:2] == ["text of 1-5", "text of 6-10"]
    assert (0, 5) in cutter.cuts and (5, 5) in cutter.cuts


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_a_single_page_still_refused_is_skipped_and_the_rest_is_indexed(tmp_path, status, caplog):
    refused = _refused(status)
    runner, _, _ = _runner(_answer({"1-10": refused, "1-5": refused, "1-3": refused, "3-3": refused}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.truncated is True and out.pages_refused == 1
    assert out.text.split("\n\n") == ["text of 1-2", "text of 4-5", "text of 6-10", "text of 11-20",
                                      "text of 21-25"]
    assert out.pages_sent == 24 and "refused=1" in out.detail
    assert any("refused=1" in record.getMessage() for record in caplog.records)


async def test_every_page_refused_outright_is_unsupported(tmp_path):
    runner, _, _ = _runner(lambda request: _refused(400), pages_total=3)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.UNSUPPORTED and out.pages_refused == 3 and out.pages_sent == 0
    assert out.model_calls >= 3


async def test_every_page_refused_with_one_page_too_large_is_too_large(tmp_path):
    def handler(request):
        from tests.unit.composition.test_drive_reader_runner_halving import _key

        return _refused(413 if _key(request) == "2-2" else 422)

    runner, _, _ = _runner(handler, pages_total=3)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TOO_LARGE and out.pages_refused == 3


async def test_every_page_refused_as_too_large_is_too_large(tmp_path):
    runner, _, _ = _runner(lambda request: _refused(413), pages_total=2)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TOO_LARGE and out.pages_refused == 2
