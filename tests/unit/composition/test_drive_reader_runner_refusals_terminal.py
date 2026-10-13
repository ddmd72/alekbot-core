"""A provider refusal of the input is the file's terminal outcome, never a retried `error` (round-2 review P-m5;
USER_DRIVE_INDEXING_RFC §24.3, §24.8). A single page still refused as too large (HTTP 413), or a window or image refused
outright (HTTP 400/422), failed the file as `error`: retried up to MAX_ATTEMPTS, and every window read before the
refusal was sent and billed again on each attempt. The same bytes get the same refusal, so the outcome demotes:
`too_large` for 413, `unsupported` for 400/422. Output that stays invalid, a refusal raised before any HTTP exchange
and a safety stop remain per-file transients."""
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

async def test_a_single_page_still_too_large_is_too_large_and_keeps_what_was_spent(tmp_path):
    big = LLMClientError("too large", http_status=413)
    runner, _, _ = _runner(_answer({"21-25": big, "21-23": big, "21-22": big, "21-21": big}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TOO_LARGE and "413" in out.detail
    assert out.pages_sent >= 20 and out.model_calls >= 2      # the windows read before the refusal are counted once


@pytest.mark.parametrize("status", [400, 422])
async def test_a_refused_window_is_unsupported_and_cancels_the_rest(tmp_path, status):
    async def handler(request):
        if _key(request) == "1-10":
            return LLMClientError("bad page", http_status=status)
        await asyncio.sleep(5)
        return reply({"text": f"text of {_key(request)}", "brief": None})

    runner, _, _ = _runner(handler)
    out = await asyncio.wait_for(runner.extract(_request(tmp_path)), timeout=2)
    assert out.status is S.UNSUPPORTED and f"http={status}" in out.detail and out.model_calls >= 1


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
