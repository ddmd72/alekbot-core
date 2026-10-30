"""scripts/infrastructure/drive_index_rebuild.py (USER_DRIVE_INDEXING_RFC §12.2): marks a user's sync state `purging`
under the user's lease, dry run by default; the next tick purges and rebuilds from epoch 1."""
import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveSyncState, EpochPhase

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0
from tests.unit.services.test_drive_indexing_service import World

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_rebuild.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_index_rebuild", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _indexed(repo):
    await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u-c", epoch=3, root_item_id="root", created_at=T0))


async def test_a_dry_run_writes_nothing(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    before = await repo.get_sync_state(SCOPE)
    code, message = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="rebuild-1")
    assert code == 0 and "--apply" in message
    assert await repo.get_sync_state(SCOPE) == before


async def test_apply_marks_purging_under_the_lease_and_releases_it(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    code, _ = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="rebuild-1")
    state = await repo.get_sync_state(SCOPE)
    assert code == 0 and state.purging and state.epoch == 3 and state.lease_owner is None


async def test_a_running_tick_holds_the_lease_and_nothing_is_written(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    await repo.acquire_lease(SCOPE, "tick", T0, T0 + timedelta(minutes=31), create=False)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="rebuild-1")
    state = await repo.get_sync_state(SCOPE)
    assert code == 1 and "retry" in message and not state.purging and state.lease_owner == "tick"


async def test_nothing_stored_is_nothing_to_rebuild(script):
    repo = InMemoryDriveIndexRepository()
    code, _ = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 1 and repo.states == {}


async def test_the_next_ticks_purge_and_rebuild_from_epoch_one(script):
    w = World()
    doc = w.drive.add("a.pdf")
    await w.settle()
    ghost = replace(w.rec(doc), item_id="ghost", name="ghost.pdf")      # what "the index looks wrong" means
    await w.repo.upsert_records([ghost])
    code, _ = await script.rebuild(w.repo, SCOPE, apply=True, now=w.clock.now, owner="rebuild-1",
                                  force=True)
    assert code == 0
    await w.settle()
    state = await w.repo.get_sync_state(SCOPE)
    assert state.epoch == 1 and state.epoch_phase is EpochPhase.IDLE and not state.purging
    assert w.rec("ghost") is None and w.rec(doc) is not None and doc in w.carded()
