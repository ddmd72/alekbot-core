"""Operator scripts of the transcript build (USER_DRIVE_INDEXING_RFC §27.13, §27.15): the per-user flag, the backlog
estimate, the requeue (Δ122) and the orphan sweep. In-memory repositories only: no network, no cloud write."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import ModelUsageMonth
from src.domain.drive_index import DriveIndexRecord, DriveIndexStoreUnavailable, DriveSyncState
from src.domain.drive_transcript import JobWindow, StoredTranscript, TranscriptBatch, TranscriptJob, WindowSpec
from src.domain.user import UserBotConfig, UserProfile

from tests.unit.services.drive_index_fakes import T0

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def flag():
    return _load("drive_transcribe_flag")


@pytest.fixture
def backlog():
    return _load("drive_transcribe_backlog")


@pytest.fixture
def requeue():
    return _load("drive_transcribe_requeue")


@pytest.fixture
def sweep():
    return _load("drive_transcript_sweep")


# -- the per-user flag -----------------------------------------------------------------------------------------------

class FakeUsers:
    def __init__(self, enabled=False, exists=True, sticky=True):
        self.user = UserProfile(user_id="u", account_id="a", config=UserBotConfig(drive_transcribe_enabled=enabled)) if exists else None
        self.updates = 0
        self.sticky = sticky

    async def get_user(self, user_id):
        return self.user.model_copy(deep=True) if self.user is not None else None

    async def update_user(self, user):
        self.updates += 1
        if self.sticky:
            self.user = user
        return user


async def test_flag_dry_run_writes_nothing_and_says_before_and_after(flag):
    users = FakeUsers()
    code, message = await flag.set_flag(users, "u", enabled=True, apply=False)
    assert code == 0 and users.updates == 0 and users.user.config.drive_transcribe_enabled is False
    assert "False" in message and "True" in message and "--apply" in message


async def test_flag_apply_sets_it_and_prints_before_and_after(flag):
    users = FakeUsers()
    code, message = await flag.set_flag(users, "u", enabled=True, apply=True)
    assert code == 0 and users.updates == 1 and users.user.config.drive_transcribe_enabled is True
    assert "before False" in message and "after True" in message


async def test_flag_off_clears_it(flag):
    users = FakeUsers(enabled=True)
    code, _ = await flag.set_flag(users, "u", enabled=False, apply=True)
    assert code == 0 and users.user.config.drive_transcribe_enabled is False


async def test_flag_already_in_place_writes_nothing(flag):
    users = FakeUsers(enabled=True)
    code, message = await flag.set_flag(users, "u", enabled=True, apply=True)
    assert code == 0 and users.updates == 0 and "already" in message


async def test_flag_unknown_user_is_an_error(flag):
    code, _ = await flag.set_flag(FakeUsers(exists=False), "u", enabled=True, apply=True)
    assert code == 1


async def test_flag_write_that_does_not_stick_fails_loudly(flag):
    code, message = await flag.set_flag(FakeUsers(sticky=False), "u", enabled=True, apply=True)
    assert code == 1 and "did not stick" in message


# -- the backlog -----------------------------------------------------------------------------------------------------

def _rec(item_id, kind, size=1_000_000, **kw):
    return DriveIndexRecord(user_id="u", item_id=item_id, kind=kind, size_bytes=size, **kw)


def test_backlog_sums_video_duration_and_counts_audio_by_bytes(backlog):
    records = [_rec("v", "video", facets={"video": {"duration": 3_600_000}}),   # 1 h exactly
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
               _rec("long", "video", facets={"video": {"duration": 5 * 3_600_000}}),
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
    result = await backlog.scan(repo, "u")
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
    await repo.save_sync_state(DriveSyncState(user_id="u", epoch=1, root_item_id="root", created_at=T0))
    await repo.upsert_records(records)
    for rec in records:
        if rec.transcript_key:
            await _store(repo, rec.transcript_key)
    return repo


async def test_a_version_bump_by_itself_requeues_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")], bump=False)
    code, message = await requeue.requeue(repo, "u", apply=True, now=T0, owner="r")
    assert code == 0 and "nothing to redo" in message
    assert (await repo.get_records("u", ["a"]))["a"].transcript_state == "current"


async def test_dry_run_shows_the_estimate_and_writes_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k2", seconds=1800)])
    code, message = await requeue.requeue(repo, "u", apply=False, now=T0, owner="r")
    assert code == 0 and "2 files in 2 transcripts" in message and "$" in message and "--apply" in message
    assert all(r.transcript_state == "current" for r in (await repo.get_records("u", ["a", "b"])).values())
    assert ("u", "k1") in repo.transcripts


async def test_estimate_is_the_stage_reservation(requeue, monkeypatch):
    from src.domain.drive_transcript_plan import file_estimate_usd
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    plan = await requeue.load_plan(repo, "u")
    assert plan.estimate_usd == pytest.approx(file_estimate_usd(3600))


async def test_apply_resets_the_record_then_deletes_the_store(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1", transcript_attempts=2, transcript_hold="retry")])
    code, message = await requeue.requeue(repo, "u", apply=True, now=T0, owner="r")
    rec = (await repo.get_records("u", ["a"]))["a"]
    assert code == 0 and "requeued" in message
    assert rec.transcript_state == "ready" and rec.transcript_attempts == 0 and rec.transcript_hold is None
    assert ("u", "k1") not in repo.transcripts
    assert (await repo.get_sync_state("u")).lease_owner is None


async def test_copies_share_one_purchase(requeue, monkeypatch):
    from src.domain.drive_transcript_plan import file_estimate_usd
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k1")])
    plan = await requeue.load_plan(repo, "u")
    assert plan.files == 2 and len(plan.groups) == 1 and plan.estimate_usd == pytest.approx(file_estimate_usd(3600))


async def test_a_key_shared_with_a_current_version_copy_is_skipped(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1"), _done("b", "k1", version=2)])
    code, message = await requeue.requeue(repo, "u", apply=True, now=T0, owner="r")
    assert "nothing to redo" in message and ("u", "k1") in repo.transcripts


async def test_metadata_stubs_and_failures_are_not_redone(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1", transcript_stub="too_long"),
                                                _done("b", "k2", transcript_stub="no_audio"),
                                                _done("c", "k3", transcript_stub="too_large"),
                                                _rec("d", "audio", transcript_state="failed"),
                                                _done("e", "k5", transcript_stub="no_speech")])
    plan = await requeue.load_plan(repo, "u")
    assert [r.item_id for recs in plan.groups.values() for r in recs] == ["e"]


async def test_a_busy_lease_writes_nothing(requeue, monkeypatch):
    repo = await _seeded(requeue, monkeypatch, [_done("a", "k1")])
    await repo.acquire_lease("u", "tick", T0, T0 + timedelta(minutes=30), create=False)
    code, message = await requeue.requeue(repo, "u", apply=True, now=T0, owner="r")
    assert code == 1 and "retry" in message
    assert (await repo.get_records("u", ["a"]))["a"].transcript_state == "current" and ("u", "k1") in repo.transcripts


async def test_nothing_indexed_is_an_error(requeue):
    code, _ = await requeue.requeue(InMemoryDriveIndexRepository(), "u", apply=True, now=T0, owner="r")
    assert code == 1


# -- the sweep -------------------------------------------------------------------------------------------------------

async def _orphaned(sweep, with_state=False):
    repo = InMemoryDriveIndexRepository()
    if with_state:
        await repo.save_sync_state(DriveSyncState(user_id="u", epoch=1, root_item_id="root", created_at=T0))
    window = JobWindow(spec=WindowSpec(index=0, start_s=0.0, duration_s=60.0))
    await repo.save_transcript_job(TranscriptJob(user_id="u", item_id="i", content_key="k", windows=(window,)))
    await repo.create_batch(TranscriptBatch(name="batches/b", user_id="u"))
    await _store(repo, "k")
    month = T0.strftime("%Y-%m")
    repo.usage[("u", month)] = ModelUsageMonth(transcribe_reserved_usd=1.5, transcribe_usd=2.0)
    return repo


async def test_sweep_dry_run_reports_reservations_and_writes_nothing(sweep):
    repo = await _orphaned(sweep)
    code, message = await sweep.sweep_user(repo, "u", apply=False, now=T0, owner="s")
    assert code == 0 and "$1.5000" in message and "--apply" in message
    assert repo.transcript_jobs and repo.batches and repo.transcripts
    assert repo.usage[("u", T0.strftime("%Y-%m"))].transcribe_reserved_usd == 1.5


async def test_sweep_apply_removes_documents_and_zeroes_reservations_keeping_spend(sweep):
    repo = await _orphaned(sweep)
    code, message = await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    usage = repo.usage[("u", T0.strftime("%Y-%m"))]
    assert code == 0 and not repo.transcript_jobs and not repo.batches and not repo.transcripts
    assert usage.transcribe_reserved_usd == pytest.approx(0.0) and usage.transcribe_usd == 2.0
    assert "zeroed $1.5000" in message


async def test_sweep_zeroes_the_months_a_reservation_was_made_in(sweep):
    repo = await _orphaned(sweep)
    repo.usage[("u", "2026-09")] = ModelUsageMonth(transcribe_reserved_usd=0.25)
    await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    assert repo.usage[("u", "2026-09")].transcribe_reserved_usd == pytest.approx(0.0)


async def test_sweep_leaves_months_outside_the_scan_alone(sweep):
    repo = await _orphaned(sweep)
    repo.usage[("u", "2025-01")] = ModelUsageMonth(transcribe_reserved_usd=0.25)
    await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s", months=3)
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
    await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    first_orphan = order.index(("orphans", False))
    helpers = [(n, r) for n, r in order[:first_orphan]]
    assert {n for n, _ in helpers} == {"delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts"}
    assert all(n != "orphans" for n, _ in order[:first_orphan])
    for name in {n for n, _ in helpers}:    # each helper's last answer before the sweep was 0
        assert [r for n, r in helpers if n == name][-1] == 0


async def test_sweep_takes_and_releases_the_lease_when_a_state_exists(sweep):
    repo = await _orphaned(sweep, with_state=True)
    code, _ = await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    assert code == 0 and (await repo.get_sync_state("u")).lease_owner is None


async def test_sweep_refuses_while_a_tick_holds_the_lease(sweep):
    repo = await _orphaned(sweep, with_state=True)
    await repo.acquire_lease("u", "tick", T0, T0 + timedelta(minutes=30), create=False)
    code, message = await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    assert code == 1 and "retry" in message and repo.transcript_jobs and repo.transcripts


async def test_sweep_missing_index_is_reported_and_reservations_are_left(sweep):
    repo = await _orphaned(sweep)

    async def unavailable(user_id, limit, *, keep_transcripts):
        raise DriveIndexStoreUnavailable("index not ready")
    repo.delete_user_transcript_orphans = unavailable
    code, message = await sweep.sweep_user(repo, "u", apply=True, now=T0, owner="s")
    assert code == 1 and "indexes" in message
    assert repo.usage[("u", T0.strftime("%Y-%m"))].transcribe_reserved_usd == 1.5


async def test_sweep_run_covers_several_users_and_prefixes_the_lines(sweep):
    repo = await _orphaned(sweep)
    code, lines = await sweep.run(repo, ["u", "w"], apply=False, now=T0)
    assert code == 0 and [line.split(":")[0] for line in lines] == ["u", "w"]


def test_months_back_crosses_a_year(sweep):
    assert sweep.months_back(datetime(2026, 2, 10, tzinfo=timezone.utc), 3) == ["2026-02", "2026-01", "2025-12"]
