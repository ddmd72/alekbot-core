"""The two request limits of an interactive open in the reader (EXTERNAL_FILES_RFC §4.4): `text_budget` and
`deadline_at`. With a field set the PDF is read window 1 alone, then one window at a time; nothing is cut or called
once the budget is reached or the deadline passed; the text is trimmed to the budget at a paragraph; a skipped page is
listed; the deadline cancels the window in flight and keeps the finished ones; an outage after the first window returns
the finished prefix; each failure ends in an outcome and a WARNING/ERROR log line the alerting reads. The indexer's
(both unset) behaviour is the other reader test files'."""
import asyncio
import logging
import time
from dataclasses import replace

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import DriveModelUnavailable, ParserBusy
from src.domain.exceptions import LLMClientError, LLMUnavailableError
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner_halving import _Cutter, _key, _request
from tests.unit.composition.test_drive_reader_runner_halving import _runner as _plain_runner

S = ExtractionStatus


class _DeadlineCutter(_Cutter):
    """The parser's page ops with the `deadline_at` keyword; records it. `busy_from`: the cut that starts at this
    page raises ParserBusy, as a parser whose slot stayed taken until the deadline."""

    def __init__(self, pages_total, busy_from=None):
        super().__init__(pages_total)
        self.deadlines = []
        self.counted = []
        self.busy_from = busy_from

    async def pdf_page_count(self, path, *, deadline_at=None):
        self.counted.append(deadline_at)
        return self.pages_total

    async def cut_pdf_windows(self, path, windows, out_paths, *, deadline_at=None):
        for (first_page, page_count), out_path in zip(windows, out_paths):
            if deadline_at is None:
                await self.cut_pdf_window(path, first_page, page_count, out_path)
            else:
                await self.cut_pdf_window(path, first_page, page_count, out_path, deadline_at=deadline_at)

    async def cut_pdf_window(self, path, first_page, page_count, out_path, *, deadline_at=None):
        self.deadlines.append(deadline_at)
        if self.busy_from is not None and first_page == self.busy_from:
            raise ParserBusy()
        await super().cut_pdf_window(path, first_page, page_count, out_path)


def _with_cutter(runner, cutter):
    runner._splitter = cutter
    return runner


def _runner(handler, pages_total=25):
    """The halving tests' runner, over a splitter that takes `deadline_at`."""
    runner, llm, _ = _plain_runner(handler, pages_total)
    cutter = _DeadlineCutter(pages_total)
    return _with_cutter(runner, cutter), llm, cutter


def _limited(tmp_path, **limits):
    return replace(_request(tmp_path), **limits)


def _paragraphs(request):
    n = _key(request).split("-")[0]
    return reply({"text": f"A{n}\n\nB{n}", "brief": None})


async def test_the_budget_reads_window_1_alone_and_stops_cutting_once_it_is_reached(tmp_path):
    runner, llm, cutter = _runner(lambda r: reply({"text": f"text of {_key(r)}", "brief": None}), pages_total=40)
    out = await runner.extract(_limited(tmp_path, text_budget=len("text of 1-10")))
    assert cutter.cuts == [(0, 10)] and len(llm.requests) == 1
    assert out.status is S.TEXT and out.text == "text of 1-10" and out.truncated is True
    assert (out.pages_total, out.pages_sent, out.pages_read, out.cut_within_pages) == (40, 10, 10, ())


async def test_with_a_budget_windows_are_read_one_at_a_time_in_page_order(tmp_path):
    runner, llm, cutter = _runner(lambda r: reply({"text": f"text of {_key(r)}", "brief": None}), pages_total=40)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert llm.peak == 1 and cutter.cuts == [(0, 10), (10, 10), (20, 10), (30, 10)]
    assert out.text.split("\n\n") == ["text of 1-10", "text of 11-20", "text of 21-30", "text of 31-40"]
    assert out.truncated is False and out.pages_read == 40


async def test_the_text_is_trimmed_to_the_budget_at_a_paragraph(tmp_path):
    runner, llm, cutter = _runner(_paragraphs, pages_total=40)
    # joined: "A1\n\nB1\n\nA11\n\nB11": room 12 holds "A1\n\nB1\n\nA11" (11), not B11
    out = await runner.extract(_limited(tmp_path, text_budget=12))
    assert out.text == "A1\n\nB1\n\nA11" and out.truncated is True
    assert out.pages_read == 10 and out.cut_within_pages == tuple(range(11, 21))
    assert cutter.cuts == [(0, 10), (10, 10)]


async def test_a_cut_between_windows_has_no_window_cut_in_part(tmp_path):
    runner, _, _ = _runner(_paragraphs, pages_total=40)
    out = await runner.extract(_limited(tmp_path, text_budget=6))
    assert out.text == "A1\n\nB1" and out.pages_read == 10 and out.cut_within_pages == () and out.truncated


async def test_a_refused_page_is_listed_and_not_counted_as_read(tmp_path):
    refused = LLMClientError("refused", http_status=413)
    answers = {"1-10": refused, "1-5": refused, "1-3": refused, "3-3": refused}

    def handler(request):
        key = _key(request)
        return answers.get(key) or reply({"text": f"text of {key}", "brief": None})

    runner, _, _ = _runner(handler, pages_total=10)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert out.status is S.TEXT and out.skipped_pages == (3,) and out.pages_read == 9 and out.truncated


async def test_the_deadline_is_handed_to_the_parser_ops(tmp_path):
    cutter = _DeadlineCutter(25)
    runner, _, _ = _runner(_paragraphs, pages_total=25)
    _with_cutter(runner, cutter)
    deadline = time.time() + 60
    out = await runner.extract(_limited(tmp_path, deadline_at=deadline))
    assert out.status is S.TEXT and cutter.counted == [deadline] and cutter.deadlines == [deadline] * 3


@pytest.mark.timing
async def test_at_the_deadline_the_window_in_flight_is_cancelled_and_the_prefix_is_kept(tmp_path, caplog):
    cancelled = []

    async def handler(request):
        key = _key(request)
        if key == "1-10":
            return reply({"text": "text of 1-10", "brief": None})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(key)
            raise

    runner, llm, _ = _runner(handler, pages_total=40)
    caplog.set_level(logging.WARNING)
    started = time.time()
    out = await runner.extract(_limited(tmp_path, deadline_at=time.time() + 0.5))
    assert time.time() - started < 5 and cancelled == ["11-20"] and len(llm.requests) == 2
    assert out.status is S.TEXT and out.text == "text of 1-10" and out.truncated is True and out.pages_read == 10
    assert any(r.levelno >= logging.WARNING and "deadline" in r.getMessage() for r in caplog.records)


@pytest.mark.timing
async def test_nothing_read_at_the_deadline_is_an_error(tmp_path, caplog):
    async def handler(request):
        await asyncio.sleep(30)

    runner, _, _ = _runner(handler, pages_total=40)
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, deadline_at=time.time() + 0.3))
    assert out.status is S.ERROR and out.text == "" and out.pages_read == 0
    assert any(r.levelno >= logging.WARNING and "deadline" in r.getMessage() for r in caplog.records)


async def test_a_deadline_already_passed_calls_nothing(tmp_path, caplog):
    runner, llm, cutter = _runner(_paragraphs, pages_total=40)
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, deadline_at=time.time() - 1))
    assert out.status is S.ERROR and cutter.cuts == [] and llm.requests == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_a_busy_parser_ends_the_run_like_the_deadline_and_keeps_the_prefix(tmp_path, caplog):
    runner, llm, _ = _runner(_paragraphs, pages_total=40)
    _with_cutter(runner, _DeadlineCutter(40, busy_from=10))
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, deadline_at=time.time() + 60))
    assert out.status is S.TEXT and out.text == "A1\n\nB1" and out.truncated and len(llm.requests) == 1
    assert any(r.levelno >= logging.WARNING and "deadline" in r.getMessage() for r in caplog.records)


async def test_an_outage_after_window_2_returns_windows_1_and_2_with_the_spend(tmp_path, caplog):
    def handler(request):
        key = _key(request)
        return LLMUnavailableError("503") if key == "21-25" else reply({"text": f"text of {key}", "brief": None})

    runner, _, _ = _runner(handler, pages_total=25)
    caplog.set_level(logging.WARNING)
    out = await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert out.status is S.TEXT and out.text == "text of 1-10\n\ntext of 11-20" and out.truncated is True
    assert out.detail == "model unavailable" and out.pages_sent == 20 and out.model_calls >= 3 and out.model_cost > 0
    assert any(r.levelno >= logging.ERROR and "unavailable" in r.getMessage() for r in caplog.records)


async def test_an_outage_on_window_1_raises(tmp_path):
    runner, _, _ = _runner(lambda r: LLMUnavailableError("503"), pages_total=25)
    with pytest.raises(DriveModelUnavailable) as raised:
        await runner.extract(_limited(tmp_path, text_budget=10_000))
    assert raised.value.model_calls >= 1


async def test_an_image_past_the_deadline_is_a_timeout_and_is_not_sent(tmp_path, caplog):
    runner, llm, _ = _runner(_paragraphs)
    path = tmp_path / "scan_0012.png"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 20)
    request = replace(_request(tmp_path), path=str(path), kind="image", mime_type="image/jpeg",
                      deadline_at=time.time() - 1)
    caplog.set_level(logging.WARNING)
    out = await runner.extract(request)
    assert out.status is S.TIMEOUT and out.detail == "deadline" and llm.requests == []
    assert any(r.levelno >= logging.WARNING and "deadline" in r.getMessage() for r in caplog.records)


@pytest.mark.timing
async def test_an_image_read_that_outlasts_the_deadline_is_a_timeout(tmp_path, caplog):
    async def handler(request):
        await asyncio.sleep(30)

    runner, _, _ = _runner(handler)
    path = tmp_path / "scan_0012.png"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 20)
    request = replace(_request(tmp_path), path=str(path), kind="image", mime_type="image/jpeg",
                      deadline_at=time.time() + 0.3)
    caplog.set_level(logging.WARNING)
    out = await runner.extract(request)
    assert out.status is S.TIMEOUT and out.detail == "deadline"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
