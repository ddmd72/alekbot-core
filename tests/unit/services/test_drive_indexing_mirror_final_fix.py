"""The T2 final fix round in the tick (USER_DRIVE_INDEXING_RFC §27.9.5, §27.9.9; Δ236, Δ239; deploy Minor 2): a
throttled mirror or a mirror index missing never fails the tick or skips the index's work-off; a root-loss mark past
its grace is work the step can do now (its one confirming read gets the reserve); a step whose UI language cannot be had
does not take the reserve on every tick. The real service, stage and mirror step on the in-memory repositories.
Synthetic items only."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest

from src.domain.drive_index import DriveIndexStoreUnavailable, RecordState
from src.domain.drive_index_params import MIRROR_RESERVE_S, MIRROR_ROOT_GRACE
from src.domain.user_drive import DriveThrottled

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_step_world import StepWorld

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> StepWorld:
        return StepWorld(tmp_path, **kw)
    return make


async def _mirrored(w: StepWorld, name: str = "Interview.mp4") -> str:
    audio = w.add_recording(name)
    await w.to_current(audio)
    await w.settle()
    assert f"_Transcripts/{name}.md" in w.mirror_files(), w.mirror_files()
    return audio


def _healthy(w: StepWorld) -> None:
    state = w.repo.states["u"]
    assert state.systemic_failures == 0 and state.last_error is None
    assert state.last_success_at == w.clock()
    assert state.counters.get(RecordState.INDEXED.value)


# -- I3: mirror-only systemic errors are contained (Δ236) -------------------------------------------------------------

async def test_a_throttled_mirror_write_leaves_the_tick_done(world, caplog):
    w = world()
    audio = await _mirrored(w)
    w.clock.advance(60)
    doc = w.add_doc("notes.md", prose(6))                   # index work in the same tick
    w.drive.rename(audio, "Renamed.mp4")                    # mirror work, whose move is throttled
    w.writes.fail["move"] = [(DriveThrottled(30), None)]
    with caplog.at_level(logging.INFO):
        result = await w.tick()
    assert result.status == "done"
    assert w.rec(doc).state is RecordState.INDEXED            # the work-off's result is saved
    _healthy(w)
    ticks = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveIndex] tick ")]
    assert any("'mirror_systemic': 1" in t for t in ticks)
    w.clock.advance(60)
    await w.tick()                                           # the fan-out's next tick moves it
    assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()


async def test_a_mirror_index_missing_never_skips_the_work_off(world, caplog):
    w = world()
    await _mirrored(w)
    w.clock.advance(60)
    doc = w.add_doc("notes.md", prose(6))
    secret = "mirror_queue: FailedPrecondition: https://console.example.invalid/project/secret-project/indexes"

    async def missing(*a: Any, **kw: Any) -> Any:
        raise DriveIndexStoreUnavailable(secret)
    w.repo.mirror_queue = missing   # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        result = await w.tick()
    assert result.status == "done"
    assert w.budgets[-1].mirror_reserve == 0.0                # no reserve: the readiness check failed
    assert w.rec(doc).state is RecordState.INDEXED
    _healthy(w)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "transcript mirror" in r.getMessage()]
    assert errors
    assert not any("secret-project" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("stop", ["lease", "not_connected"])
async def test_lease_lost_and_not_connected_still_end_the_tick(world, stop):
    from src.domain.drive_index import LeaseLost
    from src.domain.user_drive import DriveNotConnectedError
    w = world()
    audio = await _mirrored(w)
    w.drive.rename(audio, "Renamed.mp4")
    exc = LeaseLost("lost") if stop == "lease" else DriveNotConnectedError("gone")
    w.writes.fail["move"] = [(exc, None)]
    result = await w.tick()
    assert result.status == ("lease_lost" if stop == "lease" else "error")


# -- M2: a root-loss mark past its grace takes the reserve (Δ239) ------------------------------------------------------

async def test_root_loss_mark_past_its_grace_gets_the_reserve(world):
    w = world()
    await _mirrored(w)
    old_root = w.root_doc().root_item_id
    w.drive.delete(old_root)                                 # the owner deletes `_Transcripts`
    await w.tick()
    assert w.root_doc().root_gone_at is not None
    w.clock.advance(3600)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0               # inside the grace: the step only waits
    w.clock.advance(MIRROR_ROOT_GRACE.total_seconds())
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S  # its confirming read is due
    assert old_root in w.root_doc().previous_root_ids        # and was made: the loss is confirmed


# -- deploy Minor 2: an unreadable UI language does not hold the reserve every tick ----------------------------------

@pytest.mark.parametrize("case", ["root_setup", "due_record"])
async def test_language_failure_holds_the_reserve_off(world, case):
    w = world()
    if case == "due_record":
        audio = await _mirrored(w)
        w.drive.rename(audio, "Renamed.mp4")
    w.lang.fail = RuntimeError("unreadable")
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S  # the first try has the tail
    calls = w.lang.calls
    w.clock.advance(1)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0               # held: no tail for it
    assert w.lang.calls == calls + 1                         # the step is still tried
    w.lang.fail = None
    await w.tick()                                           # in what the start budget left
    assert "u" not in w.svc._mirror_step_holds
    if case == "root_setup":
        assert w.root_doc() is not None
    else:
        assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()
    w.clock.advance(timedelta(minutes=1).total_seconds())
    await w.tick()
    assert w.repo.states["u"].systemic_failures == 0


# -- round 2: a root-loss confirming read that keeps failing is held off the reserve (Δ244) ----------------------------

async def test_failing_root_loss_read_backs_off_the_reserve(world):
    from src.services import drive_indexing_service as svc_mod
    w = world()
    await _mirrored(w)
    old_root = w.root_doc().root_item_id
    w.drive.delete(old_root)
    await w.tick()
    assert w.root_doc().root_gone_at is not None
    real = w.writes.get_change

    async def get_change(user_id: str, item_id: str) -> Any:
        if item_id == old_root:
            raise ValueError("503")
        return await real(user_id, item_id)
    w.writes.get_change = get_change   # type: ignore[method-assign]
    w.clock.advance(MIRROR_ROOT_GRACE.total_seconds() + 60)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S    # the first confirming read has the tail
    assert w.outs[-1].get("mirror_root_read_failed") == 1
    assert w.svc._root_setup_holds["u"][0] == 1
    w.clock.advance(1)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0                  # held: no tail for it, the step still tries
    assert w.svc._root_setup_holds["u"][0] == 2
    w.writes.get_change = real   # type: ignore[method-assign]
    w.clock.advance(svc_mod.ROOT_SETUP_HOLD_BASE.total_seconds() * 2 + 1)
    await w.tick()                                              # the read answers: the loss is confirmed
    assert old_root in w.root_doc().previous_root_ids
    assert "u" not in w.svc._root_setup_holds                   # the mark is gone: the hold with it


async def test_unexpected_error_on_the_root_loss_read_also_backs_off_the_reserve(world):
    """Round 3 (Δ244): a non-charged exception on the confirming read is counted as `mirror_errors`, not as a charged
    read failure; a run that keeps the root in `gone_ids` stops at step 0, so it steps the same hold."""
    w = world()
    await _mirrored(w)
    old_root = w.root_doc().root_item_id
    w.drive.delete(old_root)
    await w.tick()
    real = w.writes.get_change

    async def get_change(user_id: str, item_id: str) -> Any:
        if item_id == old_root:
            raise KeyError("unexpected")
        return await real(user_id, item_id)
    w.writes.get_change = get_change   # type: ignore[method-assign]
    w.clock.advance(MIRROR_ROOT_GRACE.total_seconds() + 60)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    assert w.outs[-1].get("mirror_errors") == 1 and not w.outs[-1].get("mirror_root_read_failed")
    w.clock.advance(1)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0                  # held
    assert w.svc._root_setup_holds["u"][0] == 2
