"""`scope.empty_means` on each row of `GET /api/drive/connections`: what a connection with no folders chosen sees, read
from the drive's `feed_scope` capability ("area" for a drive with one area, "nothing" for an account-wide drive), never
from the provider. When the capabilities cannot be read the row says `null` and warns, like the other best-effort fields
(`test_cabinet_drive_connections_2b_degraded`). Over the 2b Cabinet harness. Invented cast only."""
import logging

from src.domain.drive_feed_scope import FeedScope
from tests.unit.web.test_cabinet_drive_connections_2b import USER, World, call
from tests.unit.web.test_cabinet_drive_picker_outer import OuterWorld


async def _scope(w, **over):
    status, body = await call(w.app(**over), "get", "/api/drive/connections")
    assert status == 200
    return body["connections"][0]["scope"]


async def test_a_drive_with_one_area_says_area():
    w = World()
    await w.connect()
    assert (await _scope(w))["empty_means"] == "area"


async def test_an_account_wide_drive_says_nothing():
    w = OuterWorld(FeedScope.ACCOUNT)
    assert (await _scope(w, connectors=None))["empty_means"] == "nothing"


async def test_an_area_drive_built_like_the_account_one_still_says_area():
    w = OuterWorld(FeedScope.AREA)
    assert (await _scope(w, connectors=None))["empty_means"] == "area"


async def test_the_rest_of_the_scope_is_unchanged():
    w = World()
    connection = await w.connect(writable=False)
    folder = await w.folder(connection)
    capabilities = await w.adapter.capabilities(connection.handle)
    await w.connections.set_scope(USER, connection.connection_id, folder_ids=[folder], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={folder: await w.drive.ancestry(connection.handle, folder)})
    scope = await _scope(w)
    assert scope["whole_drive"] is False and scope["empty_means"] == "area"
    assert [f["id"] for f in scope["folders"]] == [folder]


async def test_without_the_drive_service_it_is_null():
    w = World()
    await w.connect()
    assert (await _scope(w, user_drive_service=None))["empty_means"] is None


async def test_a_failing_capabilities_read_makes_it_null_and_warns(caplog):
    w = World()
    await w.connect()

    async def broken(handle):
        raise RuntimeError("drive down")
    w.drive.access = broken
    with caplog.at_level(logging.WARNING):
        scope = await _scope(w)
    assert scope["empty_means"] is None
    assert any("reading the drive capabilities failed" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)
