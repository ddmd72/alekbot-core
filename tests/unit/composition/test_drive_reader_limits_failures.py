"""The failure and cut paths of the limited PDF read (EXTERNAL_FILES_RFC §4.4) that need no clock: a window that fails
for a reason other than the model being down, a parser split failure, every page refused, and the cut of the joined text
falling across a blank window. The clock-driven paths (deadline cancel, image timeout) are the `timing` tests in
test_drive_reader_limits.py."""
import logging
import time
from dataclasses import replace

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import PdfSplitFailed
from src.domain.exceptions import LLMClientError
from src.infrastructure.drive_model_call import DriveModelCallFailed
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_limits import _DeadlineCutter, _limited, _runner

S = ExtractionStatus


class _FailingCutter(_DeadlineCutter):
    """A parser whose cut of the window starting at `fail_from` raises `error`."""

    def __init__(self, pages_total, fail_from, error):
        super().__init__(pages_total)
        self.fail_from = fail_from
        self.error = error

    async def cut_pdf_windows(self, path, windows, out_paths, *, deadline_at=None):
        for (first_page, page_count), out_path in zip(windows, out_paths):
            if deadline_at is None:
                await self.cut_pdf_window(path, first_page, page_count, out_path)
            else:
                await self.cut_pdf_window(path, first_page, page_count, out_path, deadline_at=deadline_at)

    async def cut_pdf_window(self, path, first_page, page_count, out_path, *, deadline_at=None):
        if first_page == self.fail_from:
            raise self.error
        await super().cut_pdf_window(path, first_page, page_count, out_path, deadline_at=deadline_at)


def _ok(request):
    from tests.unit.composition.test_drive_reader_runner_halving import _key
    return reply({"text": f"text of {_key(request)}", "brief": None})


async def test_a_split_failure_on_a_later_window_is_that_status_with_the_reason(tmp_path):
    runner, _, _ = _runner(_ok, pages_total=25)
    runner._splitter = _FailingCutter(25, 10, PdfSplitFailed(S.ERROR, "bad xref"))
    out = await runner.extract(_limited(tmp_path, deadline_at=time.time() + 600))
    assert out.status is S.ERROR and out.detail == "split failed: bad xref" and out.pages_total == 25


async def test_an_unexpected_window_failure_is_an_error_with_an_error_log(tmp_path, caplog):
    runner, _, _ = _runner(_ok, pages_total=25)
    runner._splitter = _FailingCutter(25, 10, KeyError("window-2"))
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert out.status is S.ERROR and "window-2" in out.detail
    assert any(r.levelno == logging.ERROR and "window failed unexpectedly" in r.getMessage() and "KeyError" in r.getMessage()
               for r in caplog.records)


async def test_a_model_call_failure_on_a_later_window_is_an_error_without_the_unexpected_log(tmp_path, caplog):
    runner, _, _ = _runner(_ok, pages_total=25)
    runner._splitter = _FailingCutter(25, 10, DriveModelCallFailed("invalid output after the parse retry"))
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert out.status is S.ERROR and "invalid output" in out.detail and out.pages_total == 25
    assert not any("failed unexpectedly" in r.getMessage() for r in caplog.records)


async def test_every_page_refused_as_too_large_is_terminal_too_large(tmp_path):
    runner, _, _ = _runner(lambda request: LLMClientError("refused", http_status=413), pages_total=1)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert out.status is S.TOO_LARGE and out.pages_refused == 1 and out.detail == "every page refused (1)"


async def test_the_cut_falls_in_the_window_after_a_blank_one(tmp_path):
    from tests.unit.composition.test_drive_reader_runner_halving import _key

    def handler(request):
        text = "" if _key(request) == "1-10" else "A11\n\nB11"
        return reply({"text": text, "brief": None})

    runner, _, _ = _runner(handler, pages_total=20)
    out = await runner.extract(_limited(tmp_path, text_budget=6))
    assert out.status is S.TEXT and out.text == "A11" and out.truncated
    assert out.cut_within_pages == tuple(range(11, 21))
