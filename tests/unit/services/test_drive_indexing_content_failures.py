"""Failure classes during content work (USER_DRIVE_INDEXING_RFC §8.2, §7.7, §17.1): systemic errors end the tick
without counting an attempt, so no sequence of them moves a record to `failed`; per-file failures back off alone."""
import asyncio

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import ErrorCategory, Reason, RecordState
from src.domain.exceptions import LLMClientError, LLMUnavailableError
from src.domain.user_drive import DriveThrottled
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


class FlakyEmbeddings:
    """Chunk texts (not cards) fail with `error` while `failing` is set."""

    def __init__(self, inner, error):
        self.inner, self.error, self.failing = inner, error, True

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        if self.failing and " | " not in text:
            raise self.error
        return await self.inner.get_embedding(text, task_type)


async def test_systemic_errors_never_fail_a_record(w):
    flaky = FlakyEmbeddings(w.emb, LLMUnavailableError("quota"))
    w.svc._embeddings = flaky
    doc = w.add_doc("a.md", prose(30))
    for _ in range(10):
        result = await w.tick()
        rec = w.rec(doc)
        assert result.status == "throttled"
        assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 0, None)
        assert w.chunks(doc) == []
        w.clock.advance(1800)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.last_error is ErrorCategory.THROTTLED and state.systemic_failures == 10
    generations = w.rec(doc).index_generation
    flaky.failing = False
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.indexed_generation == generations + 1   # never reused


async def test_a_throttled_download_ends_the_tick_without_an_attempt(w):
    doc = w.add_doc("a.md", prose(3))
    w.drive.download_errors[doc] = DriveThrottled(retry_after_s=30)
    result = await w.tick()
    rec = w.rec(doc)
    assert result.status == "throttled" and (rec.attempts, rec.attempt_started_at) == (0, None)
    assert w.queue.tasks[-1]["delay"] == 30
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED


async def test_a_refused_chunk_is_this_files_failure(w):
    w.svc._embeddings = FlakyEmbeddings(w.emb, LLMClientError("bad input", http_status=400))
    doc = w.add_doc("a.md", prose(3))
    other = w.add_doc("b.md", prose(3))
    w.parser.outcomes["b.md"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT)
    result = await w.settle()
    rec = w.rec(doc)
    assert result.status == "done" and (rec.state, rec.attempts) == (RecordState.PENDING, 1)
    assert w.rec(other).reason is Reason.NO_TEXT


async def test_embeddings_past_the_file_budget_are_an_embed_timeout(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "EMBED_FILE_BUDGET_S", 0.01)

    class Slow:
        async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
            if " | " not in text:
                await asyncio.sleep(0.2)
            return [0.1] * 8
    w.svc._embeddings = Slow()
    doc = w.add_doc("a.md", prose(3))
    await w.tick()
    rec = w.rec(doc)
    assert (rec.state, rec.attempts) == (RecordState.PENDING, 1) and w.chunks(doc) == []


async def test_an_opt_out_during_a_run_drops_the_result(w):
    doc = w.add_doc("a.md", prose(3))

    def opt_out(request):
        w.users.enabled["u"] = False
        w.clock.mono += 10   # past the gate's re-read interval
    w.parser.before = opt_out
    result = await w.tick()
    assert result.status == "disabled" and w.chunks(doc) == []
    await w.settle()
    assert w.repo.records == {} and w.repo.chunks == {}
