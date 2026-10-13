"""A provider refusal of the input is never a retried `error` (round-2 review P-m5; USER_DRIVE_INDEXING_RFC §24.3,
§24.8). As `error` it was retried up to MAX_ATTEMPTS, and every window read before the refusal was sent and billed
again on each attempt. The same bytes get the same refusal, so: an image refused is terminal (`too_large` for 413,
`unsupported` for 400/422); in a PDF a refused window is halved down to one page and a single page still refused is
skipped, the rest of the file read (R3-P1, which superseded P-m5's "the PDF is terminal"; the every-page-refused
terminal path is pinned in test_drive_reader_runner_refused_pages_skipped.py). Output that stays invalid, a refusal
raised before any HTTP exchange and a safety stop remain per-file transients."""
import asyncio

import pytest

from src.domain.drive_extraction import ExtractionRequest, ExtractionStatus
from src.domain.drive_index_run import RunStep, outcome_step
from src.domain.exceptions import LLMClientError
from src.domain.llm import FinishReason
from src.infrastructure.drive_model_call import (
    DriveInputRefused, DriveInputTooLarge, DriveModelCallFailed, classify_model_error,
)
from tests.unit.agents.drive_model_fakes import reply
from tests.unit.composition.test_drive_reader_runner_halving import _answer, _key, _request, _runner

S = ExtractionStatus
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20


def _image(tmp_path):
    path = tmp_path / "img"
    path.write_bytes(JPEG)
    return ExtractionRequest(path=str(path), name="i.jpg", kind="image", mime_type="image/jpeg", user_id="u",
                             account_id="a", max_model_pages=500)


# -- classification ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 422])
def test_an_http_refusal_of_the_input_is_input_refused(status):
    mapped = classify_model_error(LLMClientError("x", http_status=status), "drive_reader")
    assert isinstance(mapped, DriveInputRefused) and isinstance(mapped, DriveModelCallFailed)
    assert not isinstance(mapped, DriveInputTooLarge)


def test_a_refusal_before_any_http_exchange_is_not_input_refused():
    mapped = classify_model_error(LLMClientError("x", http_status=None), "drive_reader")
    assert isinstance(mapped, DriveModelCallFailed) and not isinstance(mapped, DriveInputRefused)


def test_both_terminal_statuses_demote_and_never_retry():
    assert outcome_step(S.TOO_LARGE)[0] is RunStep.DEMOTE
    assert outcome_step(S.UNSUPPORTED)[0] is RunStep.DEMOTE


# -- pdf windows ------------------------------------------------------------------------------------------------------

async def test_a_single_page_still_too_large_is_skipped_and_keeps_what_was_spent(tmp_path):
    big = LLMClientError("too large", http_status=413)
    runner, llm, _ = _runner(_answer({"21-25": big, "21-23": big, "21-22": big, "21-21": big}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.truncated is True and out.pages_refused == 1
    assert out.pages_sent == 24                               # every answered page counts against the month, once
    # nine calls: 1-10, 11-20, the four refused (21-25, 21-23, 21-22, 21-21), 22-22, 23-23, 24-25 — refused ones too
    assert out.model_calls == len(llm.requests) == 9


@pytest.mark.parametrize("status", [400, 422])
async def test_a_wholly_refused_window_is_skipped_and_the_other_windows_complete(tmp_path, status, caplog):
    # R3-P1: a refusal no longer cancels the file. Every piece of pages 1-10 is refused, down to each single page,
    # while the other windows are still in flight; they complete and are indexed.
    async def handler(request):
        last = int(_key(request).split("-")[1])
        if last <= 10:
            return LLMClientError("bad page", http_status=status)
        await asyncio.sleep(0.2)
        return reply({"text": f"text of {_key(request)}", "brief": None})

    runner, llm, _ = _runner(handler)
    out = await asyncio.wait_for(runner.extract(_request(tmp_path)), timeout=2)
    assert out.status is S.TEXT and out.truncated is True and out.pages_refused == 10
    assert out.text == "text of 11-20\n\ntext of 21-25" and out.pages_sent == 15
    assert out.model_calls == len(llm.requests) == 21      # 19 refused pieces of 1-10, then 11-20 and 21-25
    assert any(f"http={status}" in r.getMessage() for r in caplog.records)


async def test_a_window_whose_output_stays_invalid_is_still_a_retried_error(tmp_path):
    runner, _, _ = _runner(_answer({"1-10": reply(text="not json", finish=FinishReason.STOP)}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.ERROR


async def test_a_refusal_before_any_http_exchange_is_still_a_retried_error(tmp_path):
    runner, _, _ = _runner(_answer({"1-10": LLMClientError("part not built", http_status=None)}))
    assert (await runner.extract(_request(tmp_path))).status is S.ERROR


# -- images -----------------------------------------------------------------------------------------------------------

async def test_an_image_refused_as_too_large_is_too_large(tmp_path):
    runner, _, _ = _runner(lambda r: LLMClientError("too large", http_status=413))
    out = await runner.extract(_image(tmp_path))
    assert out.status is S.TOO_LARGE and out.pages_total == 1


@pytest.mark.parametrize("status", [400, 422])
async def test_an_image_refused_outright_is_unsupported(tmp_path, status):
    runner, _, _ = _runner(lambda r: LLMClientError("no", http_status=status))
    out = await runner.extract(_image(tmp_path))
    assert out.status is S.UNSUPPORTED and out.model_calls >= 1
