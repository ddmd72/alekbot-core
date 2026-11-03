"""The reader cuts its PDF windows in GROUPS of MODEL_IN_FLIGHT (owner decision D11): one `cut_pdf_windows` request per
group, the next group requested only when the previous one is read, so at most MODEL_IN_FLIGHT window files are on
disk; a group not yet requested is never cut once the deadline or the text budget is reached; a failed group maps to the
same outcomes as a failed window did; a half is cut from its window's file, not from the source."""
import asyncio
import math
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.composition import drive_reader_runner
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ParserBusy, PdfSplitFailed
from src.domain.drive_index_params import MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL
from src.domain.llm import FinishReason
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner_lazy import _key, _request, _runner, _text

S = ExtractionStatus


class GroupCutter:
    """Counts `pages_total` pages and cuts windows as files `<first>-<last>` (1-based). Records every request."""

    def __init__(self, pages_total, fail_group=None, fail_with=None):
        self.pages_total = pages_total
        self.fail_group = fail_group
        self.fail_with = fail_with
        self.group_requests = []      # one list of (first, count) per cut_pdf_windows request
        self.single_cuts = []         # (path, first, count) of every cut_pdf_window (halves only)
        self.on_disk = []
        self.on_cut = None

    async def pdf_page_count(self, path, *, deadline_at=None):
        return self.pages_total

    async def cut_pdf_windows(self, path, windows, out_paths, *, deadline_at=None):
        self.group_requests.append(list(windows))
        if self.fail_group is not None and len(self.group_requests) == self.fail_group + 1:
            raise self.fail_with
        for (first, count), out in zip(windows, out_paths):
            Path(out).write_bytes(f"{first + 1}-{min(first + count, self.pages_total)}".encode())
        self.on_disk.append(len(os.listdir(os.path.dirname(out_paths[0]))))
        if self.on_cut is not None:
            self.on_cut(len(self.group_requests))

    async def cut_pdf_window(self, path, first, count, out_path, *, deadline_at=None):
        self.single_cuts.append((path, first, count))
        Path(out_path).write_bytes(f"half-{first}-{count}".encode())


async def test_a_200_page_pdf_is_cut_in_ceil_windows_over_in_flight_requests(tmp_path):
    cutter = GroupCutter(200)
    runner, llm, work = _runner(_text, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path))
    windows = math.ceil(200 / MODEL_PAGES_PER_CALL)
    assert out.status is S.TEXT and out.pages_sent == 200 and out.model_calls == windows
    assert len(cutter.group_requests) == math.ceil(windows / MODEL_IN_FLIGHT)
    assert [len(g) for g in cutter.group_requests] == [MODEL_IN_FLIGHT] * (windows // MODEL_IN_FLIGHT)
    assert sum(len(g) for g in cutter.group_requests) == windows and cutter.single_cuts == []
    assert out.text.split("\n\n")[0] == "text of 1-10" and out.text.split("\n\n")[-1] == "text of 191-200"
    assert list(work.iterdir()) == []


async def test_never_more_than_in_flight_window_files_are_on_disk(tmp_path):
    cutter = GroupCutter(200)
    seen = []

    async def handler(request):
        await asyncio.sleep(0.01)
        seen.append(len(os.listdir(tmp_path / "work" / os.listdir(tmp_path / "work")[0])))
        return _text(request)

    runner, _, work = _runner(handler, cutter, tmp_path)
    await runner.extract(_request(tmp_path))
    assert max(cutter.on_disk) <= MODEL_IN_FLIGHT and max(seen) <= MODEL_IN_FLIGHT
    assert max(seen) >= 1 and list(work.iterdir()) == []


async def test_the_limited_path_requests_a_group_only_when_the_previous_is_read(tmp_path):
    cutter = GroupCutter(200)
    reads_at_cut = []
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    cutter.on_cut = lambda n: reads_at_cut.append(len(llm.requests))
    out = await runner.extract(replace(_request(tmp_path), text_budget=10 ** 6))
    assert out.status is S.TEXT and len(cutter.group_requests) == math.ceil(20 / MODEL_IN_FLIGHT)
    assert reads_at_cut == [MODEL_IN_FLIGHT * n for n in range(len(reads_at_cut))]


async def test_the_budget_between_groups_stops_cutting(tmp_path):
    cutter = GroupCutter(200)
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    # one window of text reaches the budget; the rest of its group is unread and no further group is requested
    out = await runner.extract(replace(_request(tmp_path), text_budget=5))
    assert len(cutter.group_requests) == 1 and len(llm.requests) == 1 and out.truncated is True


async def test_the_deadline_between_groups_stops_cutting(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(drive_reader_runner, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    cutter = GroupCutter(200)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == MODEL_IN_FLIGHT:
            now[0] += 1000     # the deadline passes as the first group's last window is read
        return _text(request)

    runner, llm, _ = _runner(handler, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), deadline_at=now[0] + 60))
    assert len(cutter.group_requests) == 1 and len(llm.requests) == MODEL_IN_FLIGHT
    assert out.status is S.TEXT and out.truncated is True


@pytest.mark.parametrize("limits", [{}, {"text_budget": 10 ** 6}], ids=["indexer", "limited"])
@pytest.mark.parametrize("status", [S.MEMORY, S.TIMEOUT, S.ERROR])
async def test_a_failed_group_is_the_files_outcome_like_a_failed_window(tmp_path, limits, status):
    cutter = GroupCutter(60, fail_group=1, fail_with=PdfSplitFailed(status, "killed"))
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), **limits))
    assert out.status is status and out.detail == "split failed: killed"
    assert out.model_calls == MODEL_IN_FLIGHT == len(llm.requests) and out.pages_sent == MODEL_IN_FLIGHT * 10


async def test_a_busy_group_in_the_limited_path_stops_like_the_deadline_and_keeps_the_prefix(tmp_path):
    cutter = GroupCutter(60, fail_group=1, fail_with=ParserBusy())
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), deadline_at=time.time() + 60))
    assert out.status is S.TEXT and out.truncated is True and len(llm.requests) == MODEL_IN_FLIGHT


async def test_a_half_is_cut_from_its_window_file_with_a_relative_offset(tmp_path):
    cutter = GroupCutter(30)

    def handler(request):
        if _key(request) == "11-20":
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        return _text(request)

    runner, _, work = _runner(handler, cutter, tmp_path)
    source = str(tmp_path / "file.pdf")
    await runner.extract(_request(tmp_path))
    assert [(first, count) for _, first, count in cutter.single_cuts] == [(0, 5), (5, 5)]
    for path, _, _ in cutter.single_cuts:
        assert path != source and os.path.dirname(path).startswith(str(work)) and "pages-0011-010" in path
    assert list(work.iterdir()) == []


async def test_busy_on_the_first_group_answers_the_busy_note_not_a_generic_error(tmp_path, caplog):
    import logging

    from src.domain.document_open_texts import parser_unavailable_note
    from src.domain.document_presentation import note_for
    from src.domain.document_read import ReadStatus
    from src.services.document_service import DocumentService
    from tests.unit.services.test_document_service_read import PDF, SCAN_OUT, FakeExtractor, FakeSplitter, req

    cutter = GroupCutter(60, fail_group=0, fail_with=ParserBusy())
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    service = DocumentService(FakeExtractor("parser", SCAN_OUT), runner, FakeSplitter())
    with caplog.at_level(logging.INFO):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, "busy") and llm.requests == []
    assert note_for(out, "scan_0012.pdf", "application/pdf") == parser_unavailable_note("scan_0012.pdf")
    assert any(r.levelno == logging.WARNING and "parser was busy before any window" in r.getMessage()
               for r in caplog.records)
    assert not any("deadline reached" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR and "DriveReader" in r.getMessage() for r in caplog.records)


async def test_busy_after_a_prefix_keeps_the_prefix_and_logs_busy_not_deadline(tmp_path, caplog):
    import logging

    cutter = GroupCutter(60, fail_group=1, fail_with=ParserBusy())
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    with caplog.at_level(logging.INFO):
        out = await runner.extract(replace(_request(tmp_path), deadline_at=time.time() + 60))
    assert out.status is S.TEXT and out.truncated is True and len(llm.requests) == MODEL_IN_FLIGHT
    assert "stop=busy" in out.detail
    assert any(r.levelno == logging.WARNING and "parser was busy" in r.getMessage() for r in caplog.records)
    assert not any("deadline reached" in r.getMessage() for r in caplog.records)
