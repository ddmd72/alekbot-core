"""A half that times out again fails the file, it is not a model outage (USER_DRIVE_INDEXING_RFC §24.3, §24.8;
round-2 review T-M2b, superseding C-M2's "a half timing out again = outage"). The model answered the file's other
windows, so it is up; the dense pages are this file's problem. As an outage the file was never counted and re-read
(and re-billed) its finished windows on every backoff; as `error` it is a counted per-file retry (§8.2)."""
from src.domain.drive_extraction import DriveModelUnavailable, ExtractionStatus
from src.domain.exceptions import LLMTimeoutError

from .test_drive_reader_runner_halving import _answer, _request, _runner

S = ExtractionStatus


async def test_a_half_that_times_out_again_fails_the_file(tmp_path):
    runner, _, _ = _runner(_answer({"11-20": LLMTimeoutError("t"), "16-20": LLMTimeoutError("t")}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.ERROR and "timed out" in out.detail
    assert out.pages_sent >= 5   # the pages answered before it are counted against the month (§24.7)


async def test_a_single_page_half_that_times_out_fails_the_file(tmp_path):
    runner, _, _ = _runner(_answer({"21-22": LLMTimeoutError("t"), "22-22": LLMTimeoutError("t")}), pages_total=22)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.ERROR


async def test_a_first_timeout_on_a_single_page_window_stays_an_outage(tmp_path):
    runner, _, _ = _runner(_answer({"11-11": LLMTimeoutError("t")}), pages_total=11)
    try:
        await runner.extract(_request(tmp_path))
    except DriveModelUnavailable:
        return
    raise AssertionError("a window never halved that times out is a model outage")
