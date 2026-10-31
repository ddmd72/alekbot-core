"""The Cabinet status carries the access-loss stamp and the purge flag, so `index_kept` can tell a kept index from
one past its grace period or being purged (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §6, §7.7)."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index_params import ACCESS_LOSS_TTL

from tests.unit.services.test_drive_search_service import World
from .drive_index_fakes import U_SCOPE


async def _settled_world() -> World:
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    return w


async def test_access_lost_at_reaches_the_status_and_ends_the_kept_index():
    w = await _settled_world()
    state = await w.repo.get_sync_state(U_SCOPE)
    lost = w.clock.now - ACCESS_LOSS_TTL - timedelta(minutes=1)
    await w.repo.save_sync_state(replace(state, access_lost_at=lost))
    status = await w.search_svc.indexing_status("u")
    assert status.access_lost_at == lost
    assert not status.index_kept(w.clock.now)


async def test_purging_reaches_the_status_and_keeps_nothing():
    w = await _settled_world()
    state = await w.repo.get_sync_state(U_SCOPE)
    await w.repo.save_sync_state(replace(state, purging=True))
    status = await w.search_svc.indexing_status("u")
    assert status.purging
    assert not status.index_kept(w.clock.now)


async def test_a_fresh_index_is_kept():
    w = await _settled_world()
    status = await w.search_svc.indexing_status("u")
    assert status.access_lost_at is None and not status.purging
    assert status.index_kept(w.clock.now)
