"""`DriveSearchService.search_states`: the `search` line of every live connection by connection id, for the preflight's
`drives` lines (multi-drive 2a, design §4)."""
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_search_line import SearchLine

from .drive_mounts_harness import MountedWorld, drive_connection, CREATED


async def test_one_line_per_live_connection_by_connection_id():
    w = await MountedWorld().open()
    states = await w.service.search_states("u")
    assert set(states) == {"u", "conn-2"}
    assert states["u"] is await w.service.search_state("u")   # the primary's line is the single-drive one
    assert states["conn-2"] is SearchLine.ON


async def test_a_switch_that_is_off_reads_off_for_that_connection_only():
    w = MountedWorld()
    w.acme = drive_connection("conn-2", "b2", "Acme", drive_identity="fake:d2", created_at=CREATED, indexing=False)
    await w.open()
    states = await w.service.search_states("u")
    assert states["conn-2"] is SearchLine.OFF and states["u"] is SearchLine.ON


async def test_a_connection_that_is_not_live_has_no_line():
    w = MountedWorld()
    w.acme = drive_connection("conn-2", "b2", "Acme", drive_identity="fake:d2", created_at=CREATED,
                              state=ConnectionState.DISCONNECTED)
    await w.open()
    assert set(await w.service.search_states("u")) == {"u"}


async def test_a_failed_read_leaves_that_connection_out():
    w = await MountedWorld().open()
    real = w.repo.get_sync_state

    async def flaky(scope):
        if scope.connection_id == "conn-2":
            raise RuntimeError("store unavailable")
        return await real(scope)

    w.repo.get_sync_state = flaky
    assert set(await w.service.search_states("u")) == {"u"}


async def test_a_user_with_no_connection_has_no_lines():
    w = MountedWorld()
    assert await w.service.search_states("u") == {}
