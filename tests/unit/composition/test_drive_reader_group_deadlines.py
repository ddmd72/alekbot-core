"""Group cutting under a clock: the indexer path's file budget before and during a group cut, the limited path's
deadline during a cut, right after it and during a window, a half cut handed the request's deadline, and a busy half
cut after a model call (spend travels with the busy note)."""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.composition import drive_reader_runner
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ParserBusy, ParserUnavailable
from src.domain.drive_index_params import MODEL_IN_FLIGHT
from src.domain.llm import FinishReason
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_grouped_cut import GroupCutter
from tests.unit.composition.test_drive_reader_runner_lazy import _key, _request, _runner, _text

S = ExtractionStatus


class SlowCutter(GroupCutter):
    def __init__(self, pages_total, delay):
        super().__init__(pages_total)
        self.delay = delay

    async def cut_pdf_windows(self, path, windows, out_paths, *, deadline_at=None):
        await asyncio.sleep(self.delay)
        await super().cut_pdf_windows(path, windows, out_paths, deadline_at=deadline_at)


async def test_a_spent_file_budget_cuts_and_reads_nothing(tmp_path):
    cutter = GroupCutter(60)
    runner, llm, work = _runner(_text, cutter, tmp_path, budget=0)
    out = await runner.extract(_request(tmp_path))
    assert cutter.group_requests == [] and llm.requests == [] and out.pages_sent == 0
    assert out.status is not S.TEXT and list(work.iterdir()) == []


async def test_a_group_cut_that_outlasts_the_file_budget_is_abandoned(tmp_path):
    cutter = SlowCutter(60, delay=30)
    runner, llm, work = _runner(_text, cutter, tmp_path, budget=0.05)
    out = await runner.extract(_request(tmp_path))
    assert llm.requests == [] and out.status is not S.TEXT
    assert list(work.iterdir()) == []


async def test_a_second_cut_that_outlasts_the_deadline_keeps_the_finished_prefix(tmp_path):
    class SlowSecond(GroupCutter):
        async def cut_pdf_windows(self, path, windows, out_paths, *, deadline_at=None):
            if self.group_requests:
                await asyncio.sleep(30)
            await super().cut_pdf_windows(path, windows, out_paths, deadline_at=deadline_at)

    cutter = SlowSecond(60)
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), deadline_at=time.time() + 1.0))
    assert out.status is S.TEXT and out.truncated is True and "stop=deadline" in out.detail
    assert len(cutter.group_requests) == 1 and len(llm.requests) == MODEL_IN_FLIGHT


async def test_a_deadline_passing_during_the_cut_stops_before_the_first_window(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(drive_reader_runner, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    cutter = GroupCutter(60)
    cutter.on_cut = lambda n: now.__setitem__(0, now[0] + 1000)
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), deadline_at=now[0] + 60))
    assert llm.requests == [] and not (out.status is S.TEXT and out.text)


async def test_a_window_that_outlasts_the_deadline_is_cancelled_and_the_prefix_kept(tmp_path):
    cutter = GroupCutter(60)

    async def handler(request):
        if _key(request) == "11-20":
            await asyncio.sleep(30)
        return _text(request)

    runner, llm, work = _runner(handler, cutter, tmp_path)
    out = await runner.extract(replace(_request(tmp_path), deadline_at=time.time() + 1.0))
    assert out.status is S.TEXT and out.truncated is True and out.text == "text of 1-10"
    assert "stop=deadline" in out.detail and list(work.iterdir()) == []


async def test_a_half_cut_carries_the_requests_deadline(tmp_path):
    seen = []

    class Recording(GroupCutter):
        async def cut_pdf_window(self, path, first, count, out_path, *, deadline_at=None):
            seen.append(deadline_at)
            await super().cut_pdf_window(path, first, count, out_path)

    def handler(request):
        if _key(request) == "1-10":
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        return _text(request)

    runner, _, _ = _runner(handler, Recording(10), tmp_path)
    deadline = time.time() + 600
    await runner.extract(replace(_request(tmp_path), deadline_at=deadline))
    assert seen == [deadline, deadline]


async def test_a_busy_half_cut_after_a_model_call_raises_unavailable_with_the_spend(tmp_path):
    class BusyHalf(GroupCutter):
        async def cut_pdf_window(self, path, first, count, out_path, *, deadline_at=None):
            raise ParserBusy()

    def handler(request):
        return reply(text="{", finish=FinishReason.MAX_TOKENS)

    runner, llm, _ = _runner(handler, BusyHalf(10), tmp_path)
    with pytest.raises(ParserUnavailable) as raised:
        await runner.extract(replace(_request(tmp_path), deadline_at=time.time() + 60))
    assert raised.value.reason == "busy" and raised.value.model_calls == len(llm.requests) >= 1
