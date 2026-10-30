"""A tick writes only under its own lease (USER_DRIVE_INDEXING_RFC §7.1). It stops writing once its clock reaches the
lease's end less LEASE_MARGIN, and a write the repository refuses because another tick holds the lease ends it
quietly; neither costs the new holder its lease or its state."""
import logging
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index_params import TICK_DEADLINE_S
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World
from .drive_index_fakes import U_SCOPE


async def test_a_tick_past_its_own_lease_writes_nothing_more(caplog):
    w = World()
    w.drive.add("a.pdf")
    real = w.drive.get_changes

    async def slow(user_id, cursor):
        page = await real(user_id, cursor)
        w.clock.advance(TICK_DEADLINE_S)          # the provider call outlived the tick's deadline
        return page

    w.drive.get_changes = slow
    result = await w.tick()
    assert result.status == "lease_lost"
    assert w.repo.records == {}
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.cursor is None and state.lease_owner is None    # its own lease released, nothing else written
    assert any("lease" in r.getMessage() and r.levelname == "INFO" for r in caplog.records)


def _steal_lease(w):
    """Another tick takes over (the lease expired while this one stalled)."""
    st = w.repo.states["u"]
    w.repo.states["u"] = replace(st, lease_owner="B", lease_until=w.clock.now.replace(year=2100))


async def test_a_stale_purge_leaves_the_new_holders_state(caplog):
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    w.users.enabled["u"] = False                  # opt-out: the next tick purges
    real = w.repo.delete_user_records

    async def stolen_mid_purge(user_id, limit):
        _steal_lease(w)
        return await real(user_id, limit)

    w.repo.delete_user_records = stolen_mid_purge
    result = await w.tick()
    assert result.status == "lease_lost"
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state is not None and state.lease_owner == "B"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_a_stale_save_is_refused_and_ends_the_tick_quietly(caplog):
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    before = await w.repo.get_sync_state(U_SCOPE)
    w.drive.add("b.pdf")
    real = w.drive.get_changes

    async def stolen(user_id, cursor):
        _steal_lease(w)
        return await real(user_id, cursor)

    w.drive.get_changes = stolen
    result = await w.tick()
    assert result.status == "lease_lost"
    after = await w.repo.get_sync_state(U_SCOPE)
    assert after.lease_owner == "B" and after.cursor == before.cursor
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []   # a stop, not a failure



async def test_a_purge_past_its_own_lease_stops_between_batches(monkeypatch):
    monkeypatch.setattr(svc_mod, "RECORD_WRITE_BATCH", 1)
    w = World()
    for i in range(3):
        w.drive.add(f"doc{i}.pdf")
    await w.settle()
    total = len(w.repo.records)
    w.users.enabled["u"] = False                  # opt-out: the next tick purges
    real = w.repo.delete_user_records

    async def slow(user_id, limit):
        deleted = await real(user_id, limit)
        # wall clock only: the tick's own start budget still allows another batch, its lease does not
        w.clock.now += timedelta(seconds=TICK_DEADLINE_S)
        return deleted

    w.repo.delete_user_records = slow
    result = await w.tick()
    assert result.status == "lease_lost"
    assert len(w.repo.records) == total - 1       # no batch after the lease's end
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state is not None and state.purging and state.lease_owner is None
