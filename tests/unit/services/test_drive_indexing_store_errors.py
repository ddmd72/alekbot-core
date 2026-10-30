"""A store outage ends the tick and is never a file's failure (USER_DRIVE_INDEXING_RFC §8.2; round-2 review T-m4).
DriveIndexStoreUnavailable in an index run abandons the attempt uncounted, as any systemic error does; a re-embed
classifies its errors like an index run: systemic ends the tick, anything else is the file's and sends it to a full
index run, which counts it."""
import pytest

from src.domain.drive_index import DriveIndexStoreUnavailable, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import LegacyConnections

pytestmark = pytest.mark.usefixtures("admitted")


def _flaky(w, method, times):
    real = getattr(w.repo, method)
    left = {"n": times}

    async def run(*args, **kwargs):
        if left["n"] > 0:
            left["n"] -= 1
            raise DriveIndexStoreUnavailable("DeadlineExceeded")
        return await real(*args, **kwargs)

    setattr(w.repo, method, run)


async def test_a_store_outage_in_an_index_run_is_not_counted():
    w = ContentWorld()
    doc = w.add_doc("notes.md", prose(80))
    _flaky(w, "write_chunks", MAX_ATTEMPTS + 1)
    for _ in range(MAX_ATTEMPTS + 1):
        result = await w.tick()
        assert result.status == "error"
        w.clock.advance(3600)
    rec = w.rec(doc)
    assert rec.state is RecordState.PENDING and rec.attempts == 0 and rec.attempt_started_at is None
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED


def _switch_space(w, space):
    w.svc = svc_mod.DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: None, space,
                                         extractors=w.svc._extractors, triage=w.svc._triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                         monotonic=w.clock.monotonic)


async def test_a_store_outage_in_a_re_embed_ends_the_tick_and_keeps_the_re_embed_due():
    w = ContentWorld()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    _switch_space(w, "new-space/768")
    await w.tick()   # the upgrade pass schedules the re-embed
    _flaky(w, "write_chunks", 1)
    result = await w.tick()
    rec = w.rec(doc)
    assert result.status == "error" and rec.chunk_reembed_pending and rec.state is RecordState.INDEXED
    await w.settle()
    assert w.rec(doc).chunk_space == "new-space/768"


async def test_an_unexpected_re_embed_error_is_the_files_not_the_ticks(monkeypatch):
    w = ContentWorld()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    parses = len(w.parser.requests)
    _switch_space(w, "new-space/768")
    await w.tick()
    real = svc_mod._keys_of   # Search v2: a chunk's word keys (§25.2.2) replaced its v1 content terms
    calls = {"n": 0}

    def broken(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("keys blew up")
        return real(texts)

    monkeypatch.setattr(svc_mod, "_keys_of", broken)
    result = await w.tick()
    assert result.status == "done"
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.chunk_space == "new-space/768"
    assert len(w.parser.requests) == parses + 1   # read again by a full index run
