"""drive_index_rebuild.py and the stored transcripts (USER_DRIVE_INDEXING_RFC §27.8 b): a rebuild sets
`purge_keep_transcripts` in the same lease-held write as `purging`, so the purge never deletes the paid-for store."""
import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveSyncState
from src.domain.drive_transcript import StoredTranscript

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0
from tests.unit.services.test_drive_indexing_service import World

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_rebuild.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_index_rebuild_keep", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _indexed(repo, **kw):
    await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u-c", epoch=3, root_item_id="root", created_at=T0, **kw))


async def test_apply_sets_purging_and_keep_in_one_write(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    state = await repo.get_sync_state(SCOPE)
    assert code == 0 and state.purging and state.purge_keep_transcripts and "kept" in message and state.lease_owner is None


async def test_drop_transcripts_clears_the_flag(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r", keep_transcripts=False)
    state = await repo.get_sync_state(SCOPE)
    assert code == 0 and state.purging and not state.purge_keep_transcripts and "deleted" in message


async def test_dry_run_says_which_and_writes_nothing(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    before = await repo.get_sync_state(SCOPE)
    _, kept = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r")
    _, dropped = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r", keep_transcripts=False)
    assert "keeping the stored transcripts" in kept and "deleting the stored transcripts" in dropped
    assert await repo.get_sync_state(SCOPE) == before


async def test_a_busy_lease_sets_neither_flag(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    await repo.acquire_lease(SCOPE, "tick", T0, T0 + timedelta(minutes=31), create=False)
    code, _ = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    state = await repo.get_sync_state(SCOPE)
    assert code == 1 and not state.purging and not state.purge_keep_transcripts


async def test_flags_come_from_the_leased_state_not_an_earlier_read(script):
    """The Task 1 carry: a tick's save of a state read before the flag was set. The write is built from the state the
    lease returned, so a counter a tick saved between the script's first read and its lease survives the flag write."""
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    real_acquire = repo.acquire_lease

    async def acquire_after_a_tick_save(user_id, owner, now, until, *, create):
        current = await repo.get_sync_state(user_id)
        await repo.save_sync_state(replace(current, counters={"records": 7}))     # a tick saved in between
        return await real_acquire(user_id, owner, now, until, create=create)
    repo.acquire_lease = acquire_after_a_tick_save
    await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    state = await repo.get_sync_state(SCOPE)
    assert state.purge_keep_transcripts and state.purging and dict(state.counters) == {"records": 7}


async def test_a_write_that_loses_a_flag_exits_one(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    real_save = repo.save_sync_state

    async def lossy(state, owner=None):
        await real_save(replace(state, purge_keep_transcripts=False), owner)
    repo.save_sync_state = lossy
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 1 and "did not stick" in message


async def test_a_purge_already_running_without_the_flag_gets_it(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo, purging=True)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    state = await repo.get_sync_state(SCOPE)
    assert code == 0 and state.purge_keep_transcripts and "may have run already" in message and "orphaned" in message


async def test_a_purge_already_running_with_the_flag_is_left_alone(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo, purging=True, purge_keep_transcripts=True)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 0 and "already running" in message


async def _world_with_a_stored_transcript():
    w = World()
    doc = w.drive.add("a.pdf")
    await w.settle()
    await w.repo.put_transcript("u", StoredTranscript(content_key="k", transcript=None, stub="no_speech",
                                                      transcribe_version=1))
    return w, doc


async def test_the_next_ticks_purge_keeps_the_store_after_a_rebuild(script):
    w, doc = await _world_with_a_stored_transcript()
    code, _ = await script.rebuild(w.repo, SCOPE, apply=True, now=w.clock.now, owner="r", force=True)
    assert code == 0
    await w.settle()
    state = await w.repo.get_sync_state(SCOPE)
    assert state.epoch == 1 and not state.purging and not state.purge_keep_transcripts
    assert ("u", "k") in w.repo.transcripts and w.rec(doc) is not None


async def test_drop_transcripts_purge_deletes_the_store(script):
    w, _ = await _world_with_a_stored_transcript()
    await script.rebuild(w.repo, SCOPE, apply=True, now=w.clock.now, owner="r", force=True, keep_transcripts=False)
    await w.settle()
    assert ("u", "k") not in w.repo.transcripts


async def test_run_passes_the_choice_to_every_user(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    code, _ = await script.run(repo, [SCOPE], hints_only=False, apply=True, now=T0, keep_transcripts=False)
    assert code == 0 and not (await repo.get_sync_state(SCOPE)).purge_keep_transcripts


async def test_drop_on_a_running_keep_purge_cannot_change_the_mode(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo, purging=True, purge_keep_transcripts=True)
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r", keep_transcripts=False)
    assert code == 1 and "cannot be changed" in message and (await repo.get_sync_state(SCOPE)).purge_keep_transcripts


async def test_the_read_back_happens_while_the_lease_is_still_held(script):
    repo = InMemoryDriveIndexRepository()
    await _indexed(repo)
    seen = []
    real_get = repo.get_sync_state

    async def get(user_id):
        state = await real_get(user_id)
        seen.append(state.lease_owner if state else None)
        return state
    repo.get_sync_state = get
    await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert seen[-1] == "r"
