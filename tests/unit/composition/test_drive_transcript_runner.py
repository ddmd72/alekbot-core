"""DriveTranscriptRunner (USER_DRIVE_INDEXING_RFC §27.5, G28, G36): builds the prompt once per submission, submits
windows through the batch port, and on collect parses and bills each result with `BatchUsage.cost()`."""
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.composition.drive_transcript_runner import DriveTranscriptRunner
from src.domain.batch_transcription import BatchRef, BatchRequest, BatchResult
from src.domain.billing import BatchUsage
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_transcript import WindowInput, WindowOutcome, WindowSpec
from src.ports.batch_transcription_port import BatchTranscriptionPort
from src.ports.quota_service import QuotaService
from tests.unit.agents.drive_model_fakes import prompt_builder

MODEL = "gemini-3.8-flash"
GOOD = json.dumps({"language_codes": ["en"], "speakers": 1, "no_speech": False,
                   "segments": [{"start": "00:00", "speaker": None, "text": "hello"}]})


def _inputs(n=2):
    return [WindowInput(key=f"item:{i}", window=WindowSpec(index=i, start_s=i * 1740.0, duration_s=1800.0),
                        file_name=f"files/f{i}", file_mime="audio/ogg") for i in range(n)]


def _port(results=None):
    port = AsyncMock(spec=BatchTranscriptionPort)
    port.submit.return_value = BatchRef(name="batches/b1", keys=("item:0", "item:1"))
    port.collect.return_value = results or {}
    return port


def _usage(**kw):
    return BatchUsage(model=MODEL, **{"audio_input_tokens": 45_000, "text_input_tokens": 500, "output_tokens": 6_000,
                                       "thought_tokens": 500, **kw})


def _runner(port=None, builder=None, quota=None):
    quota = quota if quota is not None else AsyncMock(spec=QuotaService)
    return DriveTranscriptRunner(port or _port(), builder or prompt_builder(), quota), quota


async def test_submit_builds_prompt_once_and_sends_every_window():
    port, builder = _port(), prompt_builder()
    runner, _ = _runner(port, builder)
    ref = await runner.submit(_inputs(2), display_name="job")
    assert ref.name == "batches/b1"
    assert builder.build_for_agent.await_count == 1
    kwargs = builder.build_for_agent.await_args.kwargs
    assert kwargs["agent_type"] == "drive_transcriber" and kwargs["include_biographical"] is False
    sent = port.submit.await_args.args[0]
    assert [r.key for r in sent] == ["item:0", "item:1"]
    assert all(isinstance(r, BatchRequest) and r.file_mime == "audio/ogg" for r in sent)
    assert port.submit.await_args.kwargs["display_name"] == "job"


async def test_runner_never_sends_url():
    port = _port()
    runner, _ = _runner(port)
    await runner.submit(_inputs(2), display_name="job")
    for req in port.submit.await_args.args[0]:
        for value in (req.key, req.file_name, req.file_mime, req.user_text, req.system_instruction):
            assert "://" not in value and "http" not in value.lower()


async def test_missing_prompt_is_outage():
    port = _port()
    runner, _ = _runner(port, prompt_builder(prompt="no sections here"))
    with pytest.raises(DriveModelUnavailable):
        await runner.submit(_inputs(1), display_name="job")
    port.submit.assert_not_awaited()
    runner, _ = _runner(port, prompt_builder(error=RuntimeError("firestore down")))
    with pytest.raises(DriveModelUnavailable):
        await runner.submit(_inputs(1), display_name="job")
    port.submit.assert_not_awaited()


async def test_collect_bills_account_with_batch_cost():
    usage = _usage()
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD, usage=usage, finish_reason="STOP")})
    runner, quota = _runner(port)
    out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    r = out["item:0"]
    assert r.outcome is WindowOutcome.OK and r.transcript.segments[0].text == "hello"
    assert r.usd == pytest.approx(usage.cost()) and r.usd > 0
    quota.record_usage.assert_awaited_once_with("acc-1", MODEL, 45_000 + 500 + 6_000 + 500, usage.cost())


async def test_invalid_answer_is_billed_and_marked_invalid():
    usage = _usage()
    port = _port({"item:0": BatchResult(key="item:0", text="not json", usage=usage)})
    runner, quota = _runner(port)
    out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert out["item:0"].outcome is WindowOutcome.INVALID and out["item:0"].transcript is None
    quota.record_usage.assert_awaited_once()


async def test_refused_and_error_results_are_classified():
    port = _port({
        "a": BatchResult(key="a", error="blocked", refused=True),
        "b": BatchResult(key="b", error="unavailable", code=14, transient=True),
        "c": BatchResult(key="c", error="bad request", code=3),
    })
    runner, quota = _runner(port)
    out = await runner.collect(BatchRef(name="b", keys=("a", "b", "c")), account_id="acc-1")
    assert out["a"].outcome is WindowOutcome.REFUSED
    assert out["b"].outcome is WindowOutcome.ERROR and out["b"].transient is True
    assert out["c"].outcome is WindowOutcome.ERROR and out["c"].transient is False
    quota.record_usage.assert_not_awaited()


async def test_missing_usage_bills_nothing_and_warns_without_text(caplog):
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD, usage=None)})
    runner, quota = _runner(port)
    with caplog.at_level(logging.WARNING):
        out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert out["item:0"].outcome is WindowOutcome.OK and out["item:0"].usd == 0.0
    quota.record_usage.assert_not_awaited()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and all("hello" not in m for m in warned)


async def test_billing_failure_does_not_lose_the_result():
    quota = AsyncMock(spec=QuotaService)
    quota.record_usage.side_effect = RuntimeError("firestore")
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD, usage=_usage())})
    runner, _ = _runner(port, quota=quota)
    out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert out["item:0"].outcome is WindowOutcome.OK


async def test_collect_never_logs_transcript_text(caplog):
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD.replace("hello", "SECRETWORDS"), usage=_usage())})
    runner, _ = _runner(port)
    with caplog.at_level(logging.DEBUG):
        await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert all("SECRETWORDS" not in r.getMessage() for r in caplog.records)


async def test_failed_record_usage_still_reports_the_priced_cost():
    quota = AsyncMock(spec=QuotaService)
    quota.record_usage.side_effect = RuntimeError("firestore")
    usage = _usage()
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD, usage=usage)})
    runner, _ = _runner(port, quota=quota)
    out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert out["item:0"].usd == pytest.approx(usage.cost()) and out["item:0"].usd > 0


async def test_no_quota_service_still_reports_the_priced_cost():
    usage = _usage()
    port = _port({"item:0": BatchResult(key="item:0", text=GOOD, usage=usage)})
    runner = DriveTranscriptRunner(port, prompt_builder(), None)
    out = await runner.collect(BatchRef(name="b", keys=("item:0",)), account_id="acc-1")
    assert out["item:0"].usd == pytest.approx(usage.cost()) and out["item:0"].usd > 0


async def test_unpriced_model_is_zero_and_one_error_per_collect(caplog):
    usage = BatchUsage(model="no-such-model", audio_input_tokens=1000, output_tokens=10)
    port = _port({k: BatchResult(key=k, text=GOOD, usage=usage) for k in ("a", "b")})
    runner, quota = _runner(port)
    with caplog.at_level(logging.ERROR):
        out = await runner.collect(BatchRef(name="b", keys=("a", "b")), account_id="acc-1")
    assert out["a"].usd == 0.0 and out["b"].usd == 0.0
    quota.record_usage.assert_not_awaited()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


async def test_collect_log_counts_finish_reasons_only(caplog):
    port = _port({"a": BatchResult(key="a", text=GOOD, usage=_usage(), finish_reason="STOP"),
                  "b": BatchResult(key="b", text="not json", usage=_usage(), finish_reason="MAX_TOKENS")})
    runner, _ = _runner(port)
    with caplog.at_level(logging.INFO):
        await runner.collect(BatchRef(name="b", keys=("a", "b")), account_id="acc-1")
    line = next(r.getMessage() for r in caplog.records if "collected" in r.getMessage())
    assert "MAX_TOKENS" in line and "STOP" in line


async def test_an_error_result_carries_its_rpc_code():
    """T-R18: the stage recognises 5/7 on a file-ref request as the input file gone (re-upload once)."""
    port = _port({"a": BatchResult(key="a", error="file not found", code=5)})
    runner, _ = _runner(port)
    out = await runner.collect(BatchRef(name="b", keys=("a",)), account_id="acc-1")
    assert out["a"].outcome is WindowOutcome.ERROR and out["a"].code == 5
