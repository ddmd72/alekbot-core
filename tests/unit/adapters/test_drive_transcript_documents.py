"""The transcript documents behave alike on both adapters (USER_DRIVE_INDEXING_RFC §27.3-§27.5, §27.8 b): job +
window results, batches and the sharded store, on InMemoryDriveIndexRepository and on FirestoreDriveIndexRepository
over the evaluating Firestore fake. Firestore-only cases pin the wire: shards, write order, no URL in any document."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters import firestore_drive_index_repo as fs_module
from src.adapters.firestore_drive_index_repo import stored_bytes
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index_params import TRANSCRIPT_DOC_BYTES
from src.domain.drive_transcript import (
    JobWindow, Segment, StitchedTranscript, StoredTranscript, TranscriptBatch, TranscriptJob, WindowSpec,
    WindowTranscript,
)

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, EvalDocRef, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
JOBS, BATCHES, STORE = "eval_drive_transcript_jobs", "eval_drive_transcript_batches", "eval_drive_transcripts"


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = JOBS
    mock_env_config.drive_transcript_batches_collection = BATCHES
    mock_env_config.drive_transcripts_collection = STORE
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


def _window(i, **kw):
    return JobWindow(spec=WindowSpec(i, i * 1740.0, 1800.0), **kw)


def _job(**kw):
    base = dict(
        user_id="u", item_id="i1", content_key="k1", content_version=2, duration_s=5400.5,
        windows=(_window(0, state="done", file_ref="files/abc", request_key="i1:0", attempts=1, time_resubmits=1,
                         interp_resubmits=1, gap=True, batch_name="batches/b1", files_expire_at=T,
                         reupload_used=True),
                 _window(1)),
        batch_name="batches/b1", files_expire_at=T + timedelta(hours=48), reserved_usd=0.42,
        reserved_month="2026-10", transcribed_at=T)
    base.update(kw)
    return TranscriptJob(**base)


def _result(n=2):
    return WindowTranscript(language_codes=("uk", "en"), speakers=2, no_speech=False,
                            segments=tuple(Segment(i * 30.0, i % 2 or None, f"line {i}", interpolated=i == 1)
                                           for i in range(n)))


def _batch(name="batches/b1", **kw):
    base = dict(name=name, user_id="u", request_keys=("i1:0", "i1:1"), submitted_at=T, last_polled_at=T,
                reserved_usd=1.25, reserved_month="2026-10", state="submitted", created_at=T,
                next_poll_at=T + timedelta(minutes=5), kind="transcribe")
    base.update(kw)
    return TranscriptBatch(**base)


def _stored(n=3, **kw):
    segs = tuple(Segment(i * 10.0, i % 3 if i % 2 else None, f"speech {i}", interpolated=i == 2, gap=i == 3)
                 for i in range(n))
    base = dict(content_key="k1", transcript=StitchedTranscript(segs, ("uk",), 2, 123.4, True, True),
                summary="short", summary_language="uk", transcribe_version=1, render_version=2, summary_version=3,
                last_used_at=T)
    base.update(kw)
    return StoredTranscript(**base)


# -- jobs and window results ------------------------------------------------------------------------------------------

async def test_job_roundtrip_and_absent(adapter):
    repo, _ = adapter
    assert await repo.get_transcript_job("u", "i1") is None
    await repo.save_transcript_job(_job())
    assert await repo.get_transcript_job("u", "i1") == _job()
    assert await repo.get_transcript_job("other", "i1") is None


async def test_job_with_defaults_roundtrips(adapter):
    repo, _ = adapter
    bare = TranscriptJob(user_id="u", item_id="i2", content_key="k2")
    await repo.save_transcript_job(bare)
    assert await repo.get_transcript_job("u", "i2") == bare


async def test_window_results_roundtrip_by_index(adapter):
    repo, _ = adapter
    await repo.save_window_result("u", "i1", 1, _result(1))
    await repo.save_window_result("u", "i1", 0, _result(2))
    await repo.save_window_result("u", "other", 0, _result(3))
    got = await repo.get_window_results("u", "i1")
    assert list(got) == [0, 1] and got[0] == _result(2) and got[1] == _result(1)
    assert await repo.get_window_results("u", "none") == {}


async def test_saving_the_job_leaves_window_results(adapter):
    repo, _ = adapter
    await repo.save_window_result("u", "i1", 0, _result())
    await repo.save_transcript_job(_job())
    await repo.save_transcript_job(_job(batch_name="batches/b2"))
    assert 0 in await repo.get_window_results("u", "i1")


async def test_delete_job_removes_its_window_results_only(adapter):
    repo, _ = adapter
    await repo.save_transcript_job(_job())
    await repo.save_transcript_job(_job(item_id="i2"))
    await repo.save_window_result("u", "i1", 0, _result())
    await repo.save_window_result("u", "i2", 0, _result())
    await repo.delete_transcript_job("u", "i1")
    assert await repo.get_transcript_job("u", "i1") is None and await repo.get_window_results("u", "i1") == {}
    assert await repo.get_transcript_job("u", "i2") is not None and 0 in await repo.get_window_results("u", "i2")
    await repo.delete_transcript_job("u", "i1")   # missing is a no-op


# -- batches ----------------------------------------------------------------------------------------------------------

async def test_batch_roundtrip_create_save_delete(adapter):
    repo, _ = adapter
    assert await repo.create_batch(_batch()) is True
    assert await repo.create_batch(_batch(reserved_usd=9.0)) is False   # writes nothing
    assert (await repo.get_open_batches("u", T + timedelta(hours=1), 5)) == [_batch()]
    closed = _batch(state="done", next_poll_at=None)
    await repo.save_batch(closed)
    assert await repo.get_open_batches("u", T + timedelta(hours=1), 5) == []
    await repo.save_batch(_batch())
    await repo.delete_batch("u", "batches/b1")
    assert await repo.get_open_batches("u", T + timedelta(hours=1), 5) == []
    await repo.delete_batch("u", "batches/b1")   # missing is a no-op


async def test_open_batches_are_due_ordered_and_limited(adapter):
    repo, _ = adapter
    await repo.save_batch(_batch("batches/late", next_poll_at=T + timedelta(minutes=9)))
    await repo.save_batch(_batch("batches/early", next_poll_at=T + timedelta(minutes=1)))
    await repo.save_batch(_batch("batches/future", next_poll_at=T + timedelta(hours=3)))
    await repo.save_batch(_batch("batches/other", user_id="v", next_poll_at=T))
    names = [b.name for b in await repo.get_open_batches("u", T + timedelta(minutes=30), 10)]
    assert names == ["batches/early", "batches/late"]
    assert [b.name for b in await repo.get_open_batches("u", T + timedelta(minutes=30), 1)] == ["batches/early"]


# -- the store --------------------------------------------------------------------------------------------------------

async def test_store_roundtrip_stub_and_full(adapter):
    repo, _ = adapter
    assert await repo.get_transcript("u", "k1") is None
    await repo.put_transcript("u", _stored())
    assert await repo.get_transcript("u", "k1") == _stored()
    stub = StoredTranscript(content_key="k2", transcript=None, stub="no_speech", last_used_at=T)
    await repo.put_transcript("u", stub)
    assert await repo.get_transcript("u", "k2") == stub
    assert await repo.get_transcript("v", "k1") is None


async def test_touch_sets_last_used_and_never_creates(adapter):
    repo, _ = adapter
    await repo.put_transcript("u", _stored())
    later = T + timedelta(days=3)
    await repo.touch_transcript("u", "k1", later)
    assert dataclasses.replace(await repo.get_transcript("u", "k1"), last_used_at=T) == _stored()
    assert (await repo.get_transcript("u", "k1")).last_used_at == later
    await repo.touch_transcript("u", "missing", later)
    assert await repo.get_transcript("u", "missing") is None


async def test_delete_transcript(adapter):
    repo, _ = adapter
    await repo.put_transcript("u", _stored())
    await repo.put_transcript("u", _stored(content_key="k2"))
    await repo.delete_transcript("u", "k1")
    assert await repo.get_transcript("u", "k1") is None and await repo.get_transcript("u", "k2") is not None
    await repo.delete_transcript("u", "k1")   # missing is a no-op


# -- Firestore wire: shards, order, no URL ----------------------------------------------------------------------------

@pytest.fixture
def fs(mock_env_config):
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = JOBS
    mock_env_config.drive_transcript_batches_collection = BATCHES
    mock_env_config.drive_transcripts_collection = STORE
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


def _big(n):
    return _stored(n, summary="")


def _parts(db, key="k1"):
    return db.collection(f"{STORE}/u:{key}/parts").docs


async def test_store_shards_above_doc_bytes_and_reads_back(fs, monkeypatch):
    repo, db = fs
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 6_000)
    stored = _big(300)
    await repo.put_transcript("u", stored)
    head = db.collection(STORE).docs["u:k1"]
    assert head["parts"] > 1 and "segments" not in head
    assert sorted(_parts(db), key=int) == [str(n) for n in range(head["parts"])]
    for doc in _parts(db).values():
        assert stored_bytes(doc) <= 6_000 + 4_096
    assert await repo.get_transcript("u", "k1") == stored


async def test_small_transcript_stays_inline_with_no_parts(fs):
    repo, db = fs
    await repo.put_transcript("u", _big(5))
    head = db.collection(STORE).docs["u:k1"]
    assert head["parts"] == 0 and len(head["segments"]) == 5 and _parts(db) == {}


async def test_overwrite_with_fewer_parts_removes_the_stale_ones(fs, monkeypatch):
    repo, db = fs
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 6_000)
    await repo.put_transcript("u", _big(300))
    await repo.put_transcript("u", _big(5))
    assert _parts(db) == {} and await repo.get_transcript("u", "k1") == _big(5)
    await repo.put_transcript("u", _big(300))
    await repo.delete_transcript("u", "k1")
    assert _parts(db) == {} and db.collection(STORE).docs == {}


async def test_missing_part_reads_as_absent(fs, monkeypatch):
    repo, db = fs
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 6_000)
    await repo.put_transcript("u", _big(300))
    del _parts(db)["1"]
    assert await repo.get_transcript("u", "k1") is None


async def test_shard_head_written_last(fs, monkeypatch):
    repo, db = fs
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 6_000)
    order = []
    plain_set = EvalDocRef.set

    async def recording(self, data, merge=False):
        order.append((self.col.name, self.id))
        await plain_set(self, data, merge)

    monkeypatch.setattr(EvalDocRef, "set", recording)
    await repo.put_transcript("u", _big(300))
    assert len(order) > 2 and order[-1] == (STORE, "u:k1")
    assert all(name == f"{STORE}/u:k1/parts" for name, _ in order[:-1])


def _http_values(value, path=""):
    if isinstance(value, str):
        return [path] if "http" in value else []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _http_values(v, f"{path}.{k}")]
    if isinstance(value, (list, tuple)):
        return [p for i, v in enumerate(value) for p in _http_values(v, f"{path}[{i}]")]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _http_values(dataclasses.asdict(value), path)
    return []


async def test_no_url_field_ever_written(adapter, monkeypatch):
    repo, db = adapter
    await repo.save_transcript_job(_job())
    await repo.save_window_result("u", "i1", 0, _result())
    await repo.create_batch(_batch())
    await repo.put_transcript("u", _stored())
    if db is None:
        stores = [repo.transcript_jobs, repo.window_results, repo.batches, repo.transcripts]
        assert not [p for store in stores for v in store.values() for p in _http_values(v)]
        return
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 600)
    await repo.put_transcript("u", _stored(20, content_key="k9"))   # a sharded one too
    docs = {name: col.docs for name, col in db.cols.items()}
    assert any(name.endswith("/parts") for name in docs) and docs[JOBS] and docs[BATCHES]
    assert not [(name, p) for name, col in docs.items() for doc in col.values() for p in _http_values(doc)]


async def test_four_hour_transcript_fits_doc_bound(fs):
    """§27.4 build pin: 4 h at ~200 words a minute (48 000 words) in segments of ~40 words, two speakers."""
    repo, db = fs
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    segs = tuple(Segment(i * 12.0, i % 2, " ".join(words[(i + j) % 12] for j in range(40))) for i in range(1200))
    stored = StoredTranscript(content_key="k4h", transcript=StitchedTranscript(segs, ("en",), 2, 14_400.0))
    assert sum(len(s.text.split()) for s in segs) == 48_000
    await repo.put_transcript("u", stored)
    written = [doc for name, col in db.cols.items() if name.startswith(STORE) for doc in col.docs.values()]
    assert written and all(stored_bytes(doc) < 1_048_576 for doc in written)
    assert await repo.get_transcript("u", "k4h") == stored
    assert 0 < TRANSCRIPT_DOC_BYTES < 1_048_576
