"""DriveReaderRunner halving a window the provider could not take (USER_DRIVE_INDEXING_RFC §24.3, §24.8):
- C-M2: a per-window timeout on a multi-page window halves it once, like the output limit; a single-page window that
  times out stays a model outage, a half that times out again fails the file (`error`, R2 T-M2b);
- C-M3: HTTP 413 halves the window, down to one page; a single page still too large is skipped and the rest of the
  file is read (`truncated`, `pages_refused`, R3-P1); an image refused as too large is `too_large` (R2 P-m5)."""
import asyncio
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_reader_runner import DriveReaderRunner
from src.domain.drive_extraction import DriveModelUnavailable, ExtractionRequest, ExtractionStatus
from src.domain.exceptions import LLMClientError, LLMTimeoutError
from src.domain.user import UserBotConfig
from src.infrastructure.drive_model_call import (
    DriveInputTooLarge, DriveModelCallFailed, DriveModelTimedOut, classify_model_error,
)
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, file_bytes, prompt_builder, reply

S = ExtractionStatus


class _Cutter:
    def __init__(self, pages_total):
        self.pages_total = pages_total
        self.cuts: List[tuple] = []

    async def pdf_page_count(self, path):
        return self.pages_total

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        self.cuts.append((first_page, page_count))
        last = min(first_page + page_count, self.pages_total)
        with open(out_path, "wb") as fh:
            fh.write(f"{first_page + 1}-{last}".encode())


def _key(request):
    return file_bytes(request).decode()


def _runner(handler, pages_total=25):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    cutter = _Cutter(pages_total)
    return DriveReaderRunner(contexts, users, prompt_builder(), cutter), llm, cutter


def _request(tmp_path):
    path = tmp_path / "f.pdf"
    path.write_bytes(b"%PDF")
    return ExtractionRequest(path=str(path), name="f.pdf", kind="pdf", mime_type="application/pdf", user_id="u",
                             account_id="a", max_model_pages=500)


def _answer(failing):
    """Answers every window with its page range, except those in `failing` (range -> exception)."""
    def handler(request):
        key = _key(request)
        if key in failing:
            return failing[key]
        return reply({"text": f"text of {key}", "brief": None})
    return handler


def _answer_after(first, failing):
    """`_answer`, except that a window in `failing` fails only once window `first` has answered and finished. The
    windows run concurrently and an outage cancels the ones still in flight (§24.8), so which one answers first is
    thread timing (each window's file is read through `asyncio.to_thread`); a test counting what `first` spent
    before the outage must fix that order, not assume it."""
    first_window: list = []
    answered = asyncio.Event()

    async def handler(request):
        key = _key(request)
        if key in failing:
            await asyncio.wait_for(answered.wait(), timeout=5)
            await asyncio.wait_for(asyncio.wait(first_window), timeout=5)   # its call counted, its pages sent
            return failing[key]
        if key == first:
            first_window.append(asyncio.current_task())
            answered.set()
        return reply({"text": f"text of {key}", "brief": None})
    return handler


# =============================================================================
# classification
# =============================================================================

@pytest.mark.parametrize("exc", [LLMTimeoutError("t"), asyncio.TimeoutError()])
def test_a_timeout_is_a_timed_out_outage(exc):
    mapped = classify_model_error(exc, "drive_reader")
    assert isinstance(mapped, DriveModelTimedOut) and isinstance(mapped, DriveModelUnavailable)


def test_413_is_an_input_too_large_refusal():
    mapped = classify_model_error(LLMClientError("request too large", http_status=413), "drive_reader")
    assert isinstance(mapped, DriveInputTooLarge) and isinstance(mapped, DriveModelCallFailed)


@pytest.mark.parametrize("status", [400, 422, None])
def test_other_refusals_are_not_too_large(status):
    mapped = classify_model_error(LLMClientError("x", http_status=status), "drive_reader")
    assert isinstance(mapped, DriveModelCallFailed) and not isinstance(mapped, DriveInputTooLarge)


# =============================================================================
# C-M2: timeout
# =============================================================================

async def test_a_timed_out_window_is_halved_and_read(tmp_path):
    runner, _, cutter = _runner(_answer({"11-20": LLMTimeoutError("180 s")}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.truncated is False
    assert out.text == "text of 1-10\n\ntext of 11-15\n\ntext of 16-20\n\ntext of 21-25"
    assert (10, 5) in cutter.cuts and (15, 5) in cutter.cuts
    assert out.pages_sent == 25


async def test_a_half_that_times_out_again_fails_the_file_not_an_outage(tmp_path):
    # R2 T-M2b: the model answered the other windows, so it is up — the dense pages are this file's problem
    runner, _, _ = _runner(_answer({"11-20": LLMTimeoutError("t"), "16-20": LLMTimeoutError("t")}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.ERROR and "timed out after halving" in out.detail


async def test_a_single_page_timeout_is_an_outage(tmp_path):
    runner, _, cutter = _runner(_answer({"11-11": LLMTimeoutError("t")}), pages_total=11)
    with pytest.raises(DriveModelUnavailable):
        await runner.extract(_request(tmp_path))
    assert (10, 1) in cutter.cuts and len(cutter.cuts) == 2   # never halved


async def test_an_image_timeout_stays_an_outage(tmp_path):
    runner, _, _ = _runner(lambda r: LLMTimeoutError("t"))
    path = tmp_path / "img"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 20)
    request = ExtractionRequest(path=str(path), name="i.jpg", kind="image", mime_type="image/jpeg", user_id="u",
                                account_id="a", max_model_pages=500)
    with pytest.raises(DriveModelUnavailable):
        await runner.extract(request)


# =============================================================================
# C-M3: 413
# =============================================================================

async def test_a_too_large_window_is_halved(tmp_path):
    runner, _, _ = _runner(_answer({"1-10": LLMClientError("too large", http_status=413)}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT
    assert out.text.split("\n\n")[:2] == ["text of 1-5", "text of 6-10"]


async def test_a_too_large_half_is_halved_down_to_one_page(tmp_path):
    big = LLMClientError("too large", http_status=413)
    runner, _, cutter = _runner(_answer({"1-10": big, "1-5": big, "1-3": big, "1-2": big}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT
    assert out.text.split("\n\n")[:5] == ["text of 1-1", "text of 2-2", "text of 3-3", "text of 4-5",
                                          "text of 6-10"]
    assert (0, 1) in cutter.cuts and (1, 1) in cutter.cuts


async def test_the_brief_comes_from_the_first_piece_that_answered(tmp_path):
    brief = {"doc_type": "invoice", "description": "x", "doc_date": None, "parties": [], "amount": None}

    def handler(request):
        key = _key(request)
        if key == "1-10":
            return LLMClientError("too large", http_status=413)
        first = "fill `brief`" in request.messages[0].parts[-1].text
        return reply({"text": key, "brief": brief if first else None})

    runner, llm, _ = _runner(handler)
    out = await runner.extract(_request(tmp_path))
    asked = {_key(r) for r in llm.requests if "fill `brief`" in r.messages[0].parts[-1].text}
    # reader 2 reads no brief doc type (Δ64): the brief is known by its description
    assert asked == {"1-10", "1-5"} and out.brief is not None and out.brief.description == "x"


async def test_a_single_page_still_too_large_is_skipped_and_the_rest_is_read(tmp_path, caplog):
    big = LLMClientError("too large", http_status=413)
    runner, _, cutter = _runner(_answer({"21-25": big, "21-23": big, "21-22": big, "21-21": big}))
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.truncated is True and out.pages_refused == 1
    assert out.text.split("\n\n") == ["text of 1-10", "text of 11-20", "text of 22-22", "text of 23-23",
                                      "text of 24-25"]
    assert (20, 1) in cutter.cuts and (21, 1) in cutter.cuts
    assert any("page 21 refused" in r.getMessage() and "413" in r.getMessage() for r in caplog.records)


async def test_a_too_large_image_is_too_large(tmp_path):
    runner, _, _ = _runner(lambda r: LLMClientError("too large", http_status=413))
    path = tmp_path / "img"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 20)
    request = ExtractionRequest(path=str(path), name="i.jpg", kind="image", mime_type="image/jpeg", user_id="u",
                                account_id="a", max_model_pages=500)
    out = await runner.extract(request)
    assert out.status is S.TOO_LARGE and out.pages_total == 1   # terminal, never a retried `error` (R2 P-m5)


async def test_413_then_output_limit_on_a_piece_still_halves_once(tmp_path):
    from src.domain.llm import FinishReason

    def handler(request):
        key = _key(request)
        if key == "1-10":
            return LLMClientError("too large", http_status=413)
        if key == "1-5":
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        return reply({"text": f"text of {key}", "brief": None})

    runner, _, _ = _runner(handler)
    out = await runner.extract(_request(tmp_path))
    assert out.text.split("\n\n")[:3] == ["text of 1-3", "text of 4-5", "text of 6-10"]


def test_a_timeout_the_provider_strategy_wrapped_is_still_a_timeout():
    from src.domain.exceptions import BothProvidersUnavailableError, LLMRateLimitError, TranscriptLockedError

    wrapped = [BothProvidersUnavailableError("claude", None, LLMTimeoutError("t")),
               TranscriptLockedError("claude", LLMTimeoutError("t"), 2)]
    assert all(isinstance(classify_model_error(e, "drive_reader"), DriveModelTimedOut) for e in wrapped)
    other = classify_model_error(BothProvidersUnavailableError("claude", None, LLMRateLimitError("429")), "x")
    assert isinstance(other, DriveModelUnavailable) and not isinstance(other, DriveModelTimedOut)


async def test_a_timed_out_call_is_made_once_before_it_is_halved(tmp_path):
    runner, llm, _ = _runner(_answer({"11-20": LLMTimeoutError("t")}))
    await runner.extract(_request(tmp_path))
    assert [_key(r) for r in llm.requests].count("11-20") == 1   # no same-call retry inside the agent
