"""DriveReaderRunner (USER_DRIVE_INDEXING_RFC §24.2, §24.3, §24.8, §24.9): per-file agent built from the user's
configuration and billed to the account; PDF page windows counted and cut lazily by the sandboxed parser (review A
"M split"), capped by the month and by
MODEL_EXTRACT_MAX_PAGES, run at most MODEL_IN_FLIGHT at a time and joined in page order; the output-limit split; the
file budget; the outcome classes. The real agent runs over a content-routed fake LLM."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.composition.drive_reader_runner import READER_VERSION, DriveReaderRunner
from src.domain.drive_extraction import (
    READER, DriveModelUnavailable, ExtractionRequest, ExtractionStatus,
)
from src.domain.drive_index_params import MODEL_EXTRACT_MAX_PAGES, MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL
from src.domain.exceptions import LLMClientError, LLMRateLimitError
from src.domain.llm import FinishReason
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, file_bytes, prompt_builder, reply

S = ExtractionStatus
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20
BRIEF = {"doc_type": "contract", "description": "Lease", "doc_date": "2026-01-31", "parties": ["A", "B"],
         "amount": None}


class FakeSplitter:
    """The parser's lazy page-window ops (§24.3). `pdf_page_count` answers `pages_total` (or raises `error`); every
    `cut_pdf_window` writes `<source>|w<n>#<pages>` to the caller's path, where `n` is the window of
    MODEL_PAGES_PER_CALL pages the cut starts in; a later cut inside that window is a half, `|w<n>|w1` (it starts
    the window) or `|w<n>|w2`. Records every count and cut."""

    def __init__(self, pages_total: int, truncated: bool = False, error: Exception = None) -> None:
        self.pages_total = pages_total
        self.error = error
        self.counts: List[str] = []
        self.cuts: List[Tuple[int, int]] = []
        self._windows_cut: set = set()

    @property
    def calls(self) -> List[object]:
        """Every parser op, counts and cuts."""
        return [*self.counts, *self.cuts]

    async def pdf_page_count(self, path):
        self.counts.append(path)
        if self.error is not None:
            raise self.error
        return self.pages_total

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        self.cuts.append((first_page, page_count))
        source = Path(path).read_bytes().decode()
        window = first_page // MODEL_PAGES_PER_CALL + 1
        key = f"{source}|w{window}"
        if first_page % MODEL_PAGES_PER_CALL:
            key += "|w2"
        elif window in self._windows_cut:
            key += "|w1"
        self._windows_cut.add(window)
        pages = min(first_page + page_count, self.pages_total) - first_page
        Path(out_path).write_bytes(f"{key}#{pages}".encode())


def _key(request) -> str:
    """`SOURCE|w2` for window 2, `SOURCE|w2|w1` for its first half."""
    return file_bytes(request).decode().split("#")[0]


def _text(request, payload_text=None):
    first = "fill `brief`" in request.messages[0].parts[-1].text
    return reply({"text": payload_text if payload_text is not None else f"text of {_key(request)}",
                  "brief": BRIEF if first else None})


def _runner(handler, *, splitter=None, config=None, builder=None, budget=600.0, quota=None, user_missing=False):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = None if user_missing else SimpleNamespace(config=config or UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    runner = DriveReaderRunner(contexts, users, builder or prompt_builder(), splitter or FakeSplitter(25),
                               quota_service=quota, file_budget_s=budget)
    return runner, llm, contexts


def _request(tmp_path, kind="pdf", data=b"SOURCE", pages=500, mime="application/pdf"):
    path = tmp_path / ("file.pdf" if kind == "pdf" else "img")
    path.write_bytes(data)
    return ExtractionRequest(path=str(path), name="scan.pdf", kind=kind, mime_type=mime, user_id="user-1",
                             account_id="acc-1", max_model_pages=pages)


class TestIdentity:
    def test_reader_id_and_version(self):
        runner, _, _ = _runner(_text)
        assert (runner.extractor_id, runner.version) == (READER, READER_VERSION) == ("reader", 1)


class TestGates:
    async def test_no_pages_left_this_month_waits(self, tmp_path):
        splitter = FakeSplitter(25)
        runner, llm, _ = _runner(_text, splitter=splitter)
        out = await runner.extract(_request(tmp_path, pages=0))
        assert out.status is S.BUDGET_EXHAUSTED and splitter.calls == [] and llm.requests == []

    async def test_other_kinds_are_not_read(self, tmp_path):
        runner, llm, _ = _runner(_text)
        assert (await runner.extract(_request(tmp_path, kind="docx"))).status is S.UNSUPPORTED
        assert llm.requests == []


class TestWindows:
    async def test_windows_join_in_page_order_whatever_finishes_first(self, tmp_path):
        async def handler(request):
            await asyncio.sleep({"SOURCE|w1": 0.05, "SOURCE|w2": 0.02, "SOURCE|w3": 0.0}[_key(request)])
            return _text(request)

        splitter = FakeSplitter(25)
        runner, llm, _ = _runner(handler, splitter=splitter)
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.TEXT
        assert out.text == "text of SOURCE|w1\n\ntext of SOURCE|w2\n\ntext of SOURCE|w3"
        assert sorted(splitter.cuts) == [(0, MODEL_PAGES_PER_CALL), (10, MODEL_PAGES_PER_CALL), (20, 5)]
        assert (out.pages_total, out.pages_sent, out.model_calls, out.truncated) == (25, 25, 3, False)
        assert out.brief.doc_type == "contract" and out.extracted_by == "reader@1"
        assert out.model_cost == pytest.approx(3 * (1000 * 0.10e-6 + 200 * 0.50e-6))
        names = sorted(r.messages[0].parts[0].file_data["name"] for r in llm.requests)
        assert names == ["window-001.pdf", "window-002.pdf", "window-003.pdf"]

    async def test_only_the_first_window_asks_for_the_brief(self, tmp_path):
        runner, llm, _ = _runner(_text)
        await runner.extract(_request(tmp_path))
        firsts = {_key(r): "fill `brief`" in r.messages[0].parts[-1].text for r in llm.requests}
        assert firsts == {"SOURCE|w1": True, "SOURCE|w2": False, "SOURCE|w3": False}

    async def test_month_cap_limits_the_pages_cut(self, tmp_path):
        splitter = FakeSplitter(25)
        runner, _, _ = _runner(_text, splitter=splitter)
        out = await runner.extract(_request(tmp_path, pages=12))
        assert sorted(splitter.cuts) == [(0, 10), (10, 2)]
        assert (out.pages_sent, out.model_calls, out.truncated) == (12, 2, True)

    async def test_at_most_model_in_flight_calls_at_once(self, tmp_path):
        async def handler(request):
            await asyncio.sleep(0.01)
            return _text(request)

        runner, llm, _ = _runner(handler, splitter=FakeSplitter(95))
        out = await runner.extract(_request(tmp_path))
        assert out.model_calls == 10 and llm.peak == MODEL_IN_FLIGHT

    async def test_reader_finding_no_text_is_no_text(self, tmp_path):
        runner, _, _ = _runner(lambda r: _text(r, payload_text="  "))
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.NO_TEXT and out.pages_sent == 25 and out.text == ""

    async def test_splitter_failure_is_an_error(self, tmp_path):
        runner, llm, _ = _runner(_text, splitter=FakeSplitter(25, error=RuntimeError("encrypted")))
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.ERROR and llm.requests == []

    async def test_no_pages_is_no_text(self, tmp_path):
        runner, llm, _ = _runner(_text, splitter=FakeSplitter(0))
        assert (await runner.extract(_request(tmp_path))).status is S.NO_TEXT and llm.requests == []


class TestOutputLimit:
    async def test_limited_window_is_split_in_half_once(self, tmp_path):
        def handler(request):
            if _key(request) == "SOURCE|w2":
                return reply(text='{"text": "cut', finish=FinishReason.MAX_TOKENS)
            return _text(request)

        splitter = FakeSplitter(25)
        runner, _, _ = _runner(handler, splitter=splitter)
        out = await runner.extract(_request(tmp_path))
        assert (10, 5) in splitter.cuts and (15, 5) in splitter.cuts and len(splitter.cuts) == 5
        assert out.text == ("text of SOURCE|w1\n\ntext of SOURCE|w2|w1\n\ntext of SOURCE|w2|w2\n\n"
                            "text of SOURCE|w3")
        assert (out.model_calls, out.pages_sent, out.truncated) == (5, 35, False)

    async def test_a_half_still_limited_adds_no_text_and_truncates(self, tmp_path):
        def handler(request):
            if _key(request) in ("SOURCE|w2", "SOURCE|w2|w2"):
                return reply(text="{", finish=FinishReason.MAX_TOKENS)
            return _text(request)

        runner, _, _ = _runner(handler)
        out = await runner.extract(_request(tmp_path))
        assert "SOURCE|w2|w1" in out.text and "SOURCE|w2|w2" not in out.text and out.truncated is True

    async def test_both_halves_at_the_limit_add_no_text_and_truncate(self, tmp_path):
        def handler(request):
            if _key(request).startswith("SOURCE|w2"):
                return reply(text="{", finish=FinishReason.MAX_TOKENS)
            return _text(request)

        runner, _, _ = _runner(handler)
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.TEXT and out.truncated is True
        assert out.text == "text of SOURCE|w1\n\ntext of SOURCE|w3" and out.model_calls == 5

    async def test_the_brief_survives_a_split_first_window(self, tmp_path):
        def handler(request):
            if _key(request) == "SOURCE|w1":
                return reply(text="{", finish=FinishReason.MAX_TOKENS)
            return _text(request)

        runner, _, _ = _runner(handler)
        assert (await runner.extract(_request(tmp_path))).brief.description == "Lease"

    async def test_a_single_page_at_the_limit_is_not_split(self, tmp_path):
        def handler(request):
            if _key(request) == "SOURCE|w2":
                return reply(text="{", finish=FinishReason.MAX_TOKENS)
            return _text(request)

        splitter = FakeSplitter(11)
        runner, _, _ = _runner(handler, splitter=splitter)
        out = await runner.extract(_request(tmp_path))
        assert sorted(splitter.cuts) == [(0, 10), (10, 1)] and out.truncated is True and out.text == "text of SOURCE|w1"


class TestFileBudget:
    async def test_windows_done_by_the_budget_are_kept(self, tmp_path):
        async def handler(request):
            await asyncio.sleep(5 if _key(request) == "SOURCE|w3" else 0)
            return _text(request)

        runner, _, _ = _runner(handler, budget=0.3)
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.TEXT and out.truncated is True
        assert out.text == "text of SOURCE|w1\n\ntext of SOURCE|w2" and "cut=1" in out.detail

    async def test_nothing_done_by_the_budget_is_a_per_file_error_not_a_bomb(self, tmp_path):
        async def handler(request):
            await asyncio.sleep(5)
            return _text(request)

        runner, _, _ = _runner(handler, budget=0.1)
        out = await runner.extract(_request(tmp_path))
        assert out.status is S.ERROR and out.truncated is True and out.text == "" and "cut=3" in out.detail


class TestFailures:
    async def test_a_refused_window_fails_the_file_and_cancels_the_rest(self, tmp_path):
        async def handler(request):
            if _key(request) == "SOURCE|w1":
                return LLMClientError("bad page", http_status=400)
            await asyncio.sleep(5)
            return _text(request)

        runner, _, _ = _runner(handler, budget=10)
        out = await asyncio.wait_for(runner.extract(_request(tmp_path)), timeout=2)
        assert out.status is S.ERROR and out.model_calls >= 1

    async def test_an_outage_on_any_window_raises(self, tmp_path):
        def handler(request):
            if _key(request) == "SOURCE|w2":
                return LLMRateLimitError("429", http_status=429)
            return _text(request)

        runner, _, _ = _runner(handler)
        with pytest.raises(DriveModelUnavailable):
            await runner.extract(_request(tmp_path))

    async def test_missing_prompt_raises(self, tmp_path):
        runner, llm, _ = _runner(_text, builder=prompt_builder(prompt=""))
        with pytest.raises(DriveModelUnavailable):
            await runner.extract(_request(tmp_path))
        assert llm.requests == []


class TestImage:
    async def test_rendition_is_one_call_typed_by_its_bytes(self, tmp_path):
        runner, llm, _ = _runner(lambda r: reply({"text": "TOTAL 12.50", "brief": BRIEF}))
        out = await runner.extract(_request(tmp_path, kind="image", data=JPEG, mime="image/heic"))
        assert out.status is S.TEXT and out.text == "TOTAL 12.50" and out.brief is not None
        assert (out.pages_total, out.pages_sent, out.model_calls) == (1, 1, 1)
        assert llm.requests[0].messages[0].parts[0].file_data["mime_type"] == "image/jpeg"

    async def test_image_without_text_is_no_text(self, tmp_path):
        runner, _, _ = _runner(lambda r: reply({"text": "", "brief": None}))
        assert (await runner.extract(_request(tmp_path, kind="image", data=JPEG))).status is S.NO_TEXT

    async def test_image_at_the_output_limit_adds_no_text_and_truncates(self, tmp_path):
        runner, _, _ = _runner(lambda r: reply(text="{", finish=FinishReason.MAX_TOKENS))
        out = await runner.extract(_request(tmp_path, kind="image", data=JPEG))
        assert (out.status, out.text, out.truncated, out.model_calls) == (S.NO_TEXT, "", True, 1)

    async def test_too_large_image_is_not_sent(self, tmp_path):
        runner, llm, _ = _runner(_text)
        data = JPEG + b"x" * (5 * 1024 * 1024)
        assert (await runner.extract(_request(tmp_path, kind="image", data=data))).status is S.TOO_LARGE
        assert llm.requests == []

    async def test_unknown_image_type_is_unsupported(self, tmp_path):
        runner, llm, _ = _runner(_text)
        out = await runner.extract(_request(tmp_path, kind="image", data=b"\x00\x00\x00\x18ftypheic", mime="image/heic"))
        assert out.status is S.UNSUPPORTED and llm.requests == []

    async def test_refused_image_is_an_error(self, tmp_path):
        runner, _, _ = _runner(lambda r: LLMClientError("no", http_status=400))
        assert (await runner.extract(_request(tmp_path, kind="image", data=JPEG))).status is S.ERROR


class TestAgentWiring:
    async def test_built_from_the_users_config_and_billed_to_the_account(self, tmp_path):
        config = UserBotConfig(agent_providers={"drive_reader": "openai"}, agent_thinking={"drive_reader": "medium"})
        quota = AsyncMock()
        runner, llm, contexts = _runner(_text, config=config, quota=quota)
        await runner.extract(_request(tmp_path))
        contexts.build.assert_called_once_with("drive_reader", config)
        assert {r.thinking for r in llm.requests} == {"medium"}
        assert quota.record_usage.await_count == 3
        assert {c.kwargs["account_id"] for c in quota.record_usage.await_args_list} == {"acc-1"}

    async def test_missing_user_is_an_outage(self, tmp_path):
        runner, _, _ = _runner(_text, user_missing=True)
        with pytest.raises(DriveModelUnavailable):
            await runner.extract(_request(tmp_path))

    async def test_provider_without_a_key_is_an_outage(self, tmp_path):
        runner, _, contexts = _runner(_text)
        contexts.build.side_effect = ValueError("Provider 'openai' not registered")
        with pytest.raises(DriveModelUnavailable):
            await runner.extract(_request(tmp_path))
