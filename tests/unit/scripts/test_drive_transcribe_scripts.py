"""Operator scripts of the transcript build (USER_DRIVE_INDEXING_RFC §27.13, §27.15): the backlog
estimate, the requeue (Δ122) and the orphan sweep. In-memory repositories only: no network, no cloud write."""
import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import ModelUsageMonth
from src.domain.drive_index import DriveIndexRecord, DriveIndexStoreUnavailable, DriveSyncState
from src.domain.drive_transcript import JobWindow, StoredTranscript, TranscriptBatch, TranscriptJob, WindowSpec

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_media_facts import DriveMediaFacts

from tests.unit.services.drive_index_fakes import T0

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def backlog():
    return _load("drive_transcribe_backlog")


@pytest.fixture
def requeue():
    return _load("drive_transcribe_requeue")


@pytest.fixture
def sweep():
    return _load("drive_transcript_sweep")


# -- the backlog -----------------------------------------------------------------------------------------------------

def _rec(item_id, kind, size=1_000_000, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u-c", item_id=item_id, kind=kind, size_bytes=size, **kw)


def test_backlog_sums_video_duration_and_counts_audio_by_bytes(backlog):
    records = [_rec("v", "video", media=DriveMediaFacts(video_duration_ms=3_600_000)),   # 1 h exactly
               _rec("a", "audio", size=int(10.8e6)),                              # 1 h by bytes
               _rec("d", "pdf")]
    result = backlog.tally(records)
    assert result.counts == {"video": 1, "audio_by_bytes": 1}
    hours = sum(result.seconds.values()) / 3600
    assert hours == pytest.approx(2.0)
    text = "\n".join(backlog.report(result))
    assert "$0.16" in text and "$0.32" in text and "$0.08" in text and "$0.40" in text and "held price" in text


def test_backlog_leaves_out_transcribed_and_stub_sized_files(backlog):
    records = [_rec("c", "audio", transcript_state="current"),
               _rec("big", "video", size=9 * 1024 ** 3),
               _rec("long", "video", media=DriveMediaFacts(video_duration_ms=5 * 3_600_000)),
               _rec("folder", "video", is_folder=True)]
    result = backlog.tally(records)
    assert result.counts == {"already_transcribed": 1, "too_large": 1, "too_long": 1}
    assert sum(result.seconds.get(b, 0) for b in ("video", "audio_by_bytes", "video_by_bytes")) == 0
    assert result.held_usd == 0


def test_backlog_video_without_a_facet_is_estimated_apart(backlog):
    result = backlog.tally([_rec("v", "video", size=int(10.8e6))])
    assert result.counts == {"video_by_bytes": 1}


def test_backlog_report_carries_no_identifier(backlog):
    text = "\n".join(backlog.report(backlog.tally([_rec("secret-id", "audio", name="private name.mp3")])))
    assert "secret-id" not in text and "private" not in text


async def test_backlog_scan_pages_through_the_index(backlog, monkeypatch):
    monkeypatch.setattr(backlog, "_PAGE", 2)
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([_rec(f"a{i}", "audio", size=int(10.8e6)) for i in range(5)])
    result = await backlog.scan(repo, SCOPE)
    assert result.counts == {"audio_by_bytes": 5}


# -- the requeue -----------------------------------------------------------------------------------------------------

async def _store(repo, key, stub=""):
    await repo.put_transcript("u", StoredTranscript(content_key=key, transcript=None, stub=stub, transcribe_version=1))


def _done(item_id, key, version=1, seconds=3600, **kw):
    return _rec(item_id, "audio", transcript_state="current", transcript_key=key, transcript_version=version,
                transcript_seconds=seconds, **kw)


async def _seeded(requeue, monkeypatch, records, bump=True):
    if bump:
        monkeypatch.setattr(requeue, "TRANSCRIBE_VERSION", 2)
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u-c", epoch=1, root_item_id="root", created_at=T0))
    await repo.upsert_records(records)
    for rec in records:
        if rec.transcript_key:
            await _store(repo, rec.transcript_key)
    return repo


async def test_a_version_bump_by_itself_requeues_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")], bump=False)
    code, message = await requeue.requeue(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 0 and "nothing to redo" in message
    assert (await repo.get_records(SCOPE, ["a"]))["a"].transcript_state == "current"


async def test_dry_run_shows_the_estimate_and_writes_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k2", seconds=1800)])
    code, message = await requeue.requeue(repo, SCOPE, apply=False, now=T0, owner="r")
    assert code == 0 and "2 files in 2 transcripts" in message and "$" in message and "--apply" in message
    assert all(r.transcript_state == "current" for r in (await repo.get_records(SCOPE, ["a", "b"])).values())
    assert ("u", "k1") in repo.transcripts


async def test_estimate_is_the_stage_reservation(requeue, monkeypatch):
    from src.domain.drive_transcript_plan import file_estimate_usd
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    plan = await requeue.load_plan(repo, SCOPE)
    assert plan.estimate_usd == pytest.approx(file_estimate_usd(3600))


async def test_apply_resets_the_record_then_deletes_the_store(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1", transcript_attempts=2, transcript_hold="retry")])
    code, message = await requeue.requeue(repo, SCOPE, apply=True, now=T0, owner="r")
    rec = (await repo.get_records(SCOPE, ["a"]))["a"]
    assert code == 0 and "requeued" in message
    assert rec.transcript_state == "ready" and rec.transcript_attempts == 0 and rec.transcript_hold is None
    assert ("u", "k1") not in repo.transcripts
    assert (await repo.get_sync_state(SCOPE)).lease_owner is None


async def test_copies_share_one_purchase(requeue, monkeypatch):
    from src.domain.drive_transcript_plan import file_estimate_usd
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k1")])
    plan = await requeue.load_plan(repo, SCOPE)
    assert plan.files == 2 and len(plan.groups) == 1 and plan.estimate_usd == pytest.approx(file_estimate_usd(3600))


async def test_a_key_shared_with_a_current_version_copy_is_skipped(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k1", version=2)])
    code, message = await requeue.requeue(repo, SCOPE, apply=True, now=T0, owner="r")
    assert "nothing to redo" in message and ("u", "k1") in repo.transcripts


async def test_metadata_stubs_and_failures_are_not_redone(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1", transcript_stub="too_long"),
                                                _done("b", "k2", transcript_stub="no_audio"),
                                                _done("c", "k3", transcript_stub="too_large"),
                                                _rec("d", "audio", transcript_state="failed"),
                                                _done("e", "k5", transcript_stub="no_speech")])
    plan = await requeue.load_plan(repo, SCOPE)
    assert [r.item_id for recs in plan.groups.values() for r in recs] == ["e"]


async def test_a_busy_lease_writes_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    await repo.acquire_lease(SCOPE, "tick", T0, T0 + timedelta(minutes=30), create=False)
    code, message = await requeue.requeue(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 1 and "retry" in message
    assert (await repo.get_records(SCOPE, ["a"]))["a"].transcript_state == "current" and ("u", "k1") in repo.transcripts


async def test_nothing_indexed_is_an_error(requeue):
    code, _ = await requeue.requeue(InMemoryDriveIndexRepository(), SCOPE, apply=True, now=T0, owner="r")
    assert code == 1


# -- the sweep -------------------------------------------------------------------------------------------------------

async def _orphaned(sweep, with_state=False):
    repo = InMemoryDriveIndexRepository()
    if with_state:
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u-c", epoch=1, root_item_id="root", created_at=T0))
    month = T0.strftime("%Y-%m")
    window = JobWindow(spec=WindowSpec(index=0, start_s=0.0, duration_s=60.0), state="submitted")
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u-c", item_id="i", content_key="k", windows=(window,),
                                                 reserved_usd=0.5, reserved_month=month))
    await repo.upsert_records([_rec("i", "audio", transcript_state="working")])
    await repo.create_batch(TranscriptBatch(name="batches/b", user_id="u", connection_id="u-c", request_keys=("i:0",), reserved_usd=1.0,
                                            reserved_month=month, next_poll_at=T0))
    await _store(repo, "k")
    repo.usage[("u", month)] = ModelUsageMonth(transcribe_reserved_usd=1.5, transcribe_usd=2.0)
    return repo


async def test_sweep_dry_run_reports_reservations_and_writes_nothing(sweep):
    repo = await _orphaned(sweep)
    code, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=False, now=T0, owner="s", after_rollback=True)
    assert code == 0 and "book $1.5000" in message and "release $0.0000" in message and "--apply" in message
    assert repo.transcript_jobs and repo.batches and repo.transcripts
    assert repo.usage[("u", T0.strftime("%Y-%m"))].transcribe_reserved_usd == 1.5


async def test_sweep_apply_removes_documents_and_books_the_money_in_flight(sweep):
    repo = await _orphaned(sweep)
    code, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    usage = repo.usage[("u", T0.strftime("%Y-%m"))]
    assert code == 0 and not repo.transcript_jobs and not repo.batches and not repo.transcripts
    assert usage.transcribe_reserved_usd == pytest.approx(0.0) and usage.transcribe_usd == pytest.approx(3.5)
    assert "booked $1.5000" in message and "released $0.0000" in message


async def test_sweep_zeroes_the_months_a_reservation_was_made_in(sweep):
    repo = await _orphaned(sweep)
    repo.usage[("u", "2026-09")] = ModelUsageMonth(transcribe_reserved_usd=0.25, transcribe_usd=1.0)
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    older = repo.usage[("u", "2026-09")]
    assert older.transcribe_reserved_usd == pytest.approx(0.0) and older.transcribe_usd == 1.0   # released, not booked


async def test_sweep_leaves_months_outside_the_scan_alone(sweep):
    repo = await _orphaned(sweep)
    repo.usage[("u", "2025-01")] = ModelUsageMonth(transcribe_reserved_usd=0.25)
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", months=3)
    assert repo.usage[("u", "2025-01")].transcribe_reserved_usd == 0.25


async def test_sweep_runs_the_window_sweep_only_after_the_helpers_returned_zero(sweep):
    repo = await _orphaned(sweep)
    order = []
    for name in ("delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts"):
        original = getattr(repo, name)

        async def spy(user_id, limit, _o=original, _n=name):
            result = await _o(user_id, limit)
            order.append((_n, result))
            return result
        setattr(repo, name, spy)
    original_orphans = repo.delete_user_transcript_orphans

    async def orphans(user_id, limit, *, keep_transcripts):
        order.append(("orphans", keep_transcripts))
        return await original_orphans(user_id, limit, keep_transcripts=keep_transcripts)
    repo.delete_user_transcript_orphans = orphans
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    first_orphan = order.index(("orphans", False))
    helpers = [(n, r) for n, r in order[:first_orphan]]
    assert {n for n, _ in helpers} == {"delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts"}
    assert all(n != "orphans" for n, _ in order[:first_orphan])
    for name in {n for n, _ in helpers}:    # each helper's last answer before the sweep was 0
        assert [r for n, r in helpers if n == name][-1] == 0


async def test_sweep_takes_and_releases_the_lease_when_a_state_exists(sweep):
    repo = await _orphaned(sweep, with_state=True)
    code, _ = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert code == 0 and (await repo.get_sync_state(SCOPE)).lease_owner is None


async def test_sweep_refuses_while_a_tick_holds_the_lease(sweep):
    repo = await _orphaned(sweep, with_state=True)
    await repo.acquire_lease(SCOPE, "tick", T0, T0 + timedelta(minutes=30), create=False)
    code, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert code == 1 and "retry" in message and repo.transcript_jobs and repo.transcripts


async def test_sweep_missing_index_is_reported_after_the_reservations_are_settled(sweep):
    repo = await _orphaned(sweep)

    async def unavailable(user_id, limit, *, keep_transcripts):
        raise DriveIndexStoreUnavailable("index not ready")
    repo.delete_user_transcript_orphans = unavailable
    code, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    usage = repo.usage[("u", T0.strftime("%Y-%m"))]
    assert code == 1 and "indexes" in message
    assert usage.transcribe_reserved_usd == pytest.approx(0.0) and usage.transcribe_usd == pytest.approx(3.5)   # settled first


async def test_sweep_run_covers_several_users_and_prefixes_the_lines(sweep):
    repo = await _orphaned(sweep)
    code, lines = await sweep.run(repo, ["u", "w"], apply=False, now=T0, after_rollback=True)
    assert code == 0 and [line.split(":")[0] for line in lines] == ["u", "w"]


def test_months_back_crosses_a_year(sweep):
    assert sweep.months_back(datetime(2026, 2, 10, tzinfo=timezone.utc), 3) == ["2026-02", "2026-01", "2025-12"]


async def test_a_batch_is_booked_in_its_own_month_not_the_current_one(sweep):
    repo = await _orphaned(sweep)
    repo.batches[("u-c", "batches/b")] = replace(repo.batches[("u-c", "batches/b")], reserved_month="2026-09")
    repo.usage[("u", "2026-09")] = ModelUsageMonth(transcribe_reserved_usd=1.0, transcribe_usd=0.0)
    repo.usage[("u", "2026-10")] = ModelUsageMonth(transcribe_reserved_usd=0.5, transcribe_usd=2.0)
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert repo.usage[("u", "2026-09")].transcribe_usd == pytest.approx(1.0)
    assert repo.usage[("u", "2026-10")].transcribe_usd == pytest.approx(2.5)


async def test_a_job_with_nothing_in_flight_is_released_not_booked(sweep):
    repo = await _orphaned(sweep)
    await repo.delete_batch(SCOPE, "batches/b")
    job = repo.transcript_jobs[("u-c", "i")]
    repo.transcript_jobs[("u-c", "i")] = replace(job, windows=(replace(job.windows[0], state="uploaded"),))
    repo.usage[("u", T0.strftime("%Y-%m"))] = ModelUsageMonth(transcribe_reserved_usd=0.5, transcribe_usd=2.0)
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    usage = repo.usage[("u", T0.strftime("%Y-%m"))]
    assert usage.transcribe_usd == 2.0 and usage.transcribe_reserved_usd == pytest.approx(0.0)


async def test_settlement_happens_before_the_first_delete(sweep):
    repo = await _orphaned(sweep)
    events = []
    real_add, real_del = repo.add_model_usage, repo.delete_user_transcript_jobs

    async def add(*a, **k):
        events.append("book")
        return await real_add(*a, **k)

    async def delete(*a, **k):
        events.append("delete")
        return await real_del(*a, **k)
    repo.add_model_usage, repo.delete_user_transcript_jobs = add, delete
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert events.index("book") < events.index("delete")


async def test_a_rerun_settles_nothing_twice(sweep):
    repo = await _orphaned(sweep)
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    before = repo.usage[("u", T0.strftime("%Y-%m"))]
    _, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert repo.usage[("u", T0.strftime("%Y-%m"))] == before and "booked $0.0000" in message


async def test_a_crash_after_the_zeroed_save_does_not_book_twice(sweep):
    repo = await _orphaned(sweep)
    real_add = repo.add_model_usage

    async def boom(*a, **k):
        raise RuntimeError("store down")
    repo.add_model_usage = boom
    with pytest.raises(RuntimeError):
        await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    repo.add_model_usage = real_add
    await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s", after_rollback=True)
    assert repo.usage[("u", T0.strftime("%Y-%m"))].transcribe_usd <= 3.5 + 1e-9    # never more than the reservation


async def test_sweep_refuses_while_records_are_in_a_live_transcript_state(sweep):
    repo = await _orphaned(sweep)
    code, message = await sweep.sweep_user(repo, "u", scopes=[SCOPE], apply=True, now=T0, owner="s")
    assert code == 1 and "--after-rollback" in message and repo.transcript_jobs and repo.transcripts


async def test_requeue_prints_the_headroom_and_warns_when_the_estimate_exceeds_it(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    repo.usage[("u", T0.strftime("%Y-%m"))] = ModelUsageMonth(transcribe_usd=19.99, transcribe_reserved_usd=0.0)
    _, message = await requeue.requeue(repo, SCOPE, apply=False, now=T0, owner="r")
    assert "headroom $0.01" in message and "estimate exceeds" in message


async def test_requeue_warns_when_the_connections_flag_is_off(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    _, off = await requeue.requeue(repo, SCOPE, apply=False, now=T0, owner="r", transcribe_enabled=False)
    _, unknown = await requeue.requeue(repo, SCOPE, apply=False, now=T0, owner="r")
    assert "transcription (transcribe_enabled) is OFF for this connection" in off and "OFF" not in unknown and "headroom $20.00" in unknown
