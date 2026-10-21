"""The purge and the transcripts (USER_DRIVE_INDEXING_RFC §27.8 b; plan Task 14B; G9, G30, T-R14): jobs, batches and the
store go before the records; open reservations are released once, in their own months; a rebuild keeps the store; the
orphan sweep runs last and never holds the purge. Synthetic recordings only."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, List

import pytest

from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_index_planner import month_key

from .test_drive_indexing_transcripts import TranscriptWorld, admitted, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("admitted")


def record_calls(w: TranscriptWorld) -> List[Any]:
    calls: List[Any] = []
    for name in ("delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts",
                 "delete_user_transcript_orphans", "delete_user_records", "delete_user_chunks", "delete_user_hints"):
        real = getattr(w.repo, name)

        async def spy(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            out = await _real(*a, **kw)
            calls.append((_name, out, kw.get("keep_transcripts")))
            return out
        setattr(w.repo, name, spy)
    return calls


def record_reservations(w: TranscriptWorld) -> List[Any]:
    deltas: List[Any] = []
    real = w.repo.reserve_model_usd

    async def spy(user_id: str, month: str, cls: str, delta: float) -> None:
        deltas.append((month, delta))
        await real(user_id, month, cls, delta)
    w.repo.reserve_model_usd = spy
    return deltas


async def waiting_world(world, **kw):
    w = world(**kw)
    audio = w.add_recording()
    await w.tick()
    assert w.rec(audio).transcript_state == "waiting" and w.reserved() > 0 and w.job(audio) is not None
    return w, audio


async def test_purge_deletes_jobs_batches_and_store(world):
    w, audio = await waiting_world(world)
    assert w.batches()
    calls = record_calls(w)
    deltas = record_reservations(w)
    w.users.indexing = False
    await w.settle()   # the opt-out purge
    assert w.repo.states.get("u") is None
    assert w.job(audio) is None and w.batches() == [] and w.reserved() == pytest.approx(0.0)
    assert [d for d in deltas if d[1] > 0] == []
    assert len(deltas) >= 1 and sum(d[1] for d in deltas) < 0
    assert w.batch.deleted, "the provider files were deleted best-effort"
    names = [c[0] for c in calls]
    assert "delete_user_transcripts" in names
    sweep = [c for c in calls if c[0] == "delete_user_transcript_orphans"]
    assert sweep and sweep[0][2] is False
    # T-R14: the sweep ran after the three helpers had returned 0, and before the records
    first_sweep = names.index("delete_user_transcript_orphans")
    for helper in ("delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts"):
        last_nonzero = max(i for i, c in enumerate(calls) if c[0] == helper and c[1])  if any(
            c[0] == helper and c[1] for c in calls) else -1
        assert last_nonzero < first_sweep
        assert any(c[0] == helper and c[1] == 0 for c in calls[:first_sweep])
    assert first_sweep < names.index("delete_user_records")


async def test_reservations_are_released_exactly_once_in_their_own_month(world):
    w, audio = await waiting_world(world)
    job = w.job(audio)
    month = job.reserved_month
    held = job.reserved_usd
    assert month == month_key(w.clock())
    deltas = record_reservations(w)
    w.users.indexing = False
    await w.settle()
    assert sum(d[1] for d in deltas) == pytest.approx(-held)
    assert {d[0] for d in deltas} == {month}
    assert w.reserved() == pytest.approx(0.0)


async def test_a_purge_that_stops_midway_never_releases_twice(world):
    w, audio = await waiting_world(world)
    held = w.job(audio).reserved_usd
    deltas = record_reservations(w)
    real = w.repo.delete_user_transcript_jobs
    boom = {"n": 0}

    async def failing(*a: Any, **kw: Any) -> int:
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("store down")
        return await real(*a, **kw)
    w.repo.delete_user_transcript_jobs = failing
    w.users.indexing = False
    with pytest.raises(RuntimeError):
        await w.tick()
    await w.settle()
    assert w.repo.states.get("u") is None
    assert sum(d[1] for d in deltas) == pytest.approx(-held)


async def test_rebuild_purge_keeps_store(world):
    w, audio = await waiting_world(world)
    w.repo.states["u"] = replace(w.repo.states["u"], purge_keep_transcripts=True)
    calls = record_calls(w)
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    names = [c[0] for c in calls]
    assert "delete_user_transcripts" not in names
    sweep = [c for c in calls if c[0] == "delete_user_transcript_orphans"]
    assert sweep and all(c[2] is True for c in sweep)
    assert w.job(audio) is None and w.batches() == []


async def test_the_sweeps_missing_index_never_holds_the_purge(world, caplog):
    w, audio = await waiting_world(world)

    async def unavailable(*a: Any, **kw: Any) -> int:
        raise DriveIndexStoreUnavailable("index drive_transcript_windows is not ready")
    w.repo.delete_user_transcript_orphans = unavailable
    w.users.indexing = False
    with caplog.at_level(logging.ERROR):
        await w.settle()
    assert w.repo.states.get("u") is None
    assert any("orphan sweep" in r.message and r.levelno == logging.ERROR for r in caplog.records)


async def test_a_purge_without_the_stage_still_clears_and_releases(world):
    """The kill switch off at deploy (the stage unbuilt): a leftover job and its reservation are still cleared."""
    w, audio = await waiting_world(world)
    held = w.reserved()
    w.svc._transcripts = None
    w.svc._transcribe_enabled = False
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert w.job(audio) is None and w.batches() == [] and w.reserved() == pytest.approx(0.0)
    assert held > 0 and w.batch.deleted == []   # no stage: the provider files expire by themselves (G30)


async def test_a_user_who_never_transcribed_is_purged_without_transcript_queries(world):
    w = world(switch=False, flag=False)
    w.add_doc("notes.md", "plain text " * 50)
    await w.settle()
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert w.repo.transcript_calls == []


# -- T-R25: money that may still be spent is booked, not released -----------------------------------------------------

def spent(w: TranscriptWorld, month: str) -> float:
    usage = w.repo.usage.get(("u", month))
    return usage.cap_usd("transcribe") if usage is not None else 0.0


async def test_a_running_batch_is_booked_as_spend_in_its_month_not_released(world):
    w, audio = await waiting_world(world)
    month = w.job(audio).reserved_month
    held = w.job(audio).reserved_usd
    assert any(win.state == "submitted" for win in w.job(audio).windows)
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert spent(w, month) == pytest.approx(held)   # the batch may still bill: its reservation is now spend
    assert w.reserved() == pytest.approx(0.0)       # and no longer an open reservation


async def test_an_open_batch_reservation_is_booked_in_its_own_month(world):
    w, audio = await waiting_world(world)
    batch = w.batches()[0]
    w.repo.batches[("u", batch.name)] = replace(batch, reserved_usd=0.5, reserved_month="2020-01")
    await w.repo.reserve_model_usd("u", "2020-01", "transcribe", 0.5)
    w.users.indexing = False
    await w.settle()
    assert spent(w, "2020-01") == pytest.approx(0.5)
    assert w.repo.usage[("u", "2020-01")].transcribe_reserved_usd == pytest.approx(0.0)


async def test_a_job_with_nothing_in_flight_is_released_not_booked(world):
    w, audio = await waiting_world(world)
    job = w.job(audio)
    month, held = job.reserved_month, job.reserved_usd
    w.repo.transcript_jobs[("u", audio)] = replace(
        job, windows=tuple(replace(win, state="uploaded") for win in job.windows))
    w.users.indexing = False
    await w.settle()
    assert spent(w, month) == pytest.approx(0.0)
    assert w.reserved() == pytest.approx(0.0) and held > 0


async def test_the_rebuild_path_follows_the_same_rule(world):
    w, audio = await waiting_world(world)
    month, held = w.job(audio).reserved_month, w.job(audio).reserved_usd
    w.repo.states["u"] = replace(w.repo.states["u"], purge_keep_transcripts=True)
    w.users.indexing = False
    await w.settle()
    assert spent(w, month) == pytest.approx(held) and w.reserved() == pytest.approx(0.0)


async def test_the_document_is_saved_zeroed_before_it_is_booked_or_released(world):
    w, audio = await waiting_world(world)
    events: List[str] = []
    real_save, real_usage, real_reserve = (w.repo.save_transcript_job, w.repo.add_model_usage, w.repo.reserve_model_usd)

    async def save(job: Any) -> None:
        events.append(f"save:{job.reserved_usd}")
        await real_save(job)

    async def usage(*a: Any, **kw: Any) -> Any:
        events.append("spend")
        return await real_usage(*a, **kw)

    async def reserve(*a: Any, **kw: Any) -> Any:
        events.append("reserve")
        return await real_reserve(*a, **kw)
    w.repo.save_transcript_job, w.repo.add_model_usage, w.repo.reserve_model_usd = save, usage, reserve
    w.users.indexing = False
    await w.settle()
    assert events[0] == "save:0.0" and "spend" in events and "reserve" in events
    assert events.index("save:0.0") < events.index("spend") < events.index("reserve")


async def test_a_reservation_with_no_month_is_logged_and_left_alone(world, caplog):
    w, audio = await waiting_world(world)
    w.repo.transcript_jobs[("u", audio)] = replace(w.job(audio), reserved_month="")
    deltas = record_reservations(w)
    w.users.indexing = False
    with caplog.at_level(logging.ERROR):
        await w.settle()
    assert deltas == [] and w.repo.states.get("u") is None
    assert any("no reservation month" in r.message for r in caplog.records)


def test_the_look_back_steps_through_month_starts():
    from datetime import datetime, timezone

    from src.services.drive_indexing_service import _last_months
    assert _last_months(datetime(2027, 5, 1, tzinfo=timezone.utc), 3) == ["2027-05", "2027-04", "2027-03"]
    assert _last_months(datetime(2027, 1, 1, 0, 0, 1, tzinfo=timezone.utc), 3) == ["2027-01", "2026-12", "2026-11"]
    assert _last_months(datetime(2027, 3, 31, 23, 59, tzinfo=timezone.utc), 3) == ["2027-03", "2027-02", "2027-01"]
