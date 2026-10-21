"""Wire tests for GeminiBatchAdapter.find_batch (T-R28): a submission whose call failed ambiguously is looked up by its
display name. The SDK's `aio.batches.list` is the mocked boundary and returns a real `AsyncPager` over fake pages, so the
SDK's own paging runs (docs/how_to/ADAPTER_WIRE_TESTING.md). No live call."""
import asyncio
from typing import Dict, List, Optional

import pytest
from google.genai import errors as genai_errors
from google.genai import pagers, types

from src.adapters.gemini_batch_adapter import FIND_MAX_JOBS, GeminiBatchAdapter
from src.domain.batch_transcription import BatchRef, BatchRequest
from src.domain.exceptions import LLMAccountError, LLMRateLimitError, LLMUnavailableError
from tests.contracts.adapter_contracts import BATCH_FIND_BY_SUBMITTED_DISPLAY_NAME

DISPLAY = "u-test:submitting/abc"


def _adapter() -> GeminiBatchAdapter:
    return GeminiBatchAdapter(api_key="test-key", model="gemini-test-model")


async def _submitted_display_name(adapter: GeminiBatchAdapter, display: str) -> str:
    captured: Dict = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return types.BatchJob(name="batches/b1", state=types.JobState.JOB_STATE_PENDING)
    adapter.client.aio.batches.create = create
    await adapter.submit([BatchRequest(key="k", system_instruction="s", user_text="t")], display_name=display)
    return captured["config"].display_name


def _install_pages(adapter: GeminiBatchAdapter, pages: List[List[types.BatchJob]], seen: List[Optional[str]]):
    """`batches.list` answers page by page; each page after the first is requested with the previous page's token."""
    def response(i: int) -> types.ListBatchJobsResponse:
        return types.ListBatchJobsResponse(batch_jobs=pages[i],
                                           next_page_token=str(i + 1) if i + 1 < len(pages) else None)

    async def request(*, config=None):
        token = (config or {}).get("page_token")
        seen.append(token)
        return response(int(token))

    async def list_(*, config=None):
        seen.append(None)
        return pagers.AsyncPager("batch_jobs", request, response(0), config)
    adapter.client.aio.batches.list = list_


def _job(name: str, display: Optional[str]) -> types.BatchJob:
    return types.BatchJob(name=name, display_name=display, state=types.JobState.JOB_STATE_RUNNING)


@pytest.mark.asyncio
async def test_find_matches_the_display_name_submit_sent_across_pages():
    adapter = _adapter()
    shown = await _submitted_display_name(adapter, DISPLAY)
    seen: List[Optional[str]] = []
    _install_pages(adapter, [[_job("batches/x1", "tr-other"), _job("batches/x2", None)],
                             [_job("batches/x3", "tr-another"), _job("batches/b9", shown)]], seen)
    found = await adapter.find_batch(DISPLAY)
    assert found == BatchRef(name="batches/b9")
    assert seen == [None, "1"]   # the SDK's pager fetched the second page with the first page's token
    BATCH_FIND_BY_SUBMITTED_DISPLAY_NAME.validate("gemini_batch", {"argument": DISPLAY, "submitted": shown,
                                                                    "matched": shown})


@pytest.mark.asyncio
async def test_find_returns_none_when_the_listing_ends_without_it():
    adapter = _adapter()
    seen: List[Optional[str]] = []
    _install_pages(adapter, [[_job("batches/x1", "tr-other")], [_job("batches/x2", "tr-again")]], seen)
    assert await adapter.find_batch(DISPLAY) is None
    assert seen == [None, "1"]


@pytest.mark.asyncio
async def test_find_never_matches_the_raw_argument():
    adapter = _adapter()
    _install_pages(adapter, [[_job("batches/raw", DISPLAY)]], [])
    assert await adapter.find_batch(DISPLAY) is None


@pytest.mark.asyncio
async def test_find_stops_at_its_scan_bound():
    adapter = _adapter()
    shown = await _submitted_display_name(adapter, DISPLAY)
    seen: List[Optional[str]] = []
    page = [_job(f"batches/n{i}", "tr-other") for i in range(100)]
    pages = [page] * (FIND_MAX_JOBS // 100) + [[_job("batches/late", shown)]]
    _install_pages(adapter, pages, seen)
    # Round 2: the bound is inconclusive (the job may lie past it), an outage-class error, never a `None` miss
    with pytest.raises(LLMUnavailableError, match="scan bound"):
        await adapter.find_batch(DISPLAY)
    assert len(seen) == FIND_MAX_JOBS // 100   # the page past the bound is never fetched


@pytest.mark.asyncio
async def test_find_sends_a_page_size():
    adapter = _adapter()
    configs: List = []

    async def list_(*, config=None):
        configs.append(config)

        async def request(*, config=None):
            raise AssertionError("one page only")
        return pagers.AsyncPager("batch_jobs", request, types.ListBatchJobsResponse(batch_jobs=[]), config)
    adapter.client.aio.batches.list = list_
    assert await adapter.find_batch(DISPLAY) is None
    assert configs[0].page_size and configs[0].page_size <= 1000


def _sdk_error(cls, code, msg="boom"):
    return cls(code, {"error": {"code": code, "message": msg, "status": "X"}}, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", [
    (_sdk_error(genai_errors.ServerError, 503), LLMUnavailableError),
    (_sdk_error(genai_errors.ClientError, 429), LLMRateLimitError),
    (_sdk_error(genai_errors.ClientError, 403, "permission denied"), LLMAccountError),
    (asyncio.TimeoutError(), LLMUnavailableError),
])
async def test_find_errors_are_mapped_like_every_call(exc, expected):
    adapter = _adapter()

    async def boom(**kwargs):
        raise exc
    adapter.client.aio.batches.list = boom
    with pytest.raises(expected):
        await adapter.find_batch(DISPLAY)


@pytest.mark.asyncio
async def test_find_error_on_a_later_page_is_mapped_too():
    adapter = _adapter()

    async def request(*, config=None):
        raise _sdk_error(genai_errors.ServerError, 500)

    async def list_(*, config=None):
        return pagers.AsyncPager("batch_jobs", request,
                                 types.ListBatchJobsResponse(batch_jobs=[_job("batches/x", "tr-o")],
                                                             next_page_token="1"), config)
    adapter.client.aio.batches.list = list_
    with pytest.raises(LLMUnavailableError):
        await adapter.find_batch(DISPLAY)


@pytest.mark.asyncio
async def test_find_at_the_scan_bound_is_inconclusive_not_a_miss():
    """Round 2: the job may lie past the bound (jobs are never deleted): an outage-class error, never `None`."""
    adapter = _adapter()
    seen: List[Optional[str]] = []
    page = [_job(f"batches/n{i}", "tr-other") for i in range(100)]
    _install_pages(adapter, [page] * (FIND_MAX_JOBS // 100) + [[_job("batches/late", "x")]], seen)
    with pytest.raises(LLMUnavailableError, match="scan bound"):
        await adapter.find_batch(DISPLAY)


@pytest.mark.asyncio
async def test_find_that_finds_it_at_the_last_scanned_job_still_returns_it():
    adapter = _adapter()
    shown = await _submitted_display_name(adapter, DISPLAY)
    seen: List[Optional[str]] = []
    page = [_job(f"batches/n{i}", "tr-other") for i in range(99)] + [_job("batches/last", shown)]
    _install_pages(adapter, [page] * 1, seen)
    found = await adapter.find_batch(DISPLAY)
    assert found is not None and found.name == "batches/last"
