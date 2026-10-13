"""Windows the file budget cut are never "the file has no text" (round-2 review P-m4; USER_DRIVE_INDEXING_RFC §24.3,
§24.8). The outcome used `timed_out = not finished`: when window 1 came back blank and the others ran into the budget,
the file was NO_TEXT — a permanent demotion with 19 windows never read. Unread windows with no text read are a
per-file transient (`error`); only a file whose every window was read and found blank is `no_text`."""
import asyncio

from src.domain.drive_extraction import ExtractionStatus
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner import _key, _request, _runner

S = ExtractionStatus


async def test_a_blank_first_window_and_windows_cut_by_the_budget_are_an_error_not_no_text(tmp_path):
    async def handler(request):
        if _key(request) != "SOURCE|w1":
            await asyncio.sleep(5)
        return reply({"text": "", "brief": None})

    runner, _, _ = _runner(handler, budget=0.3)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.ERROR
    assert out.truncated is True and out.text == "" and "cut=2" in out.detail


async def test_every_window_read_and_blank_is_still_no_text(tmp_path):
    runner, _, _ = _runner(lambda request: reply({"text": "", "brief": None}), budget=10)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.NO_TEXT and "cut=0" in out.detail
