"""`GET /api/drive/connections` reads its rows and each row's parts concurrently. Deterministic: the fake drive and the
index status wait on an event that is set only once every expected call is in flight, so the request completes only
when the calls truly overlap (a sequential implementation times out instead of hanging). Row order and scope-folder
order follow the stored order; an index failure on one row leaves `indexing: null` there and the rest intact.
Over the 2b Cabinet harness. Invented cast only."""
import asyncio

from tests.unit.web.test_cabinet_drive_connections_2b import USER, World, call

TIMEOUT_S = 5.0


class Rendezvous:
    """Releases every waiter once `expected` calls are waiting at the same time."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.waiting = 0
        self.event = asyncio.Event()

    async def arrive(self) -> None:
        self.waiting += 1
        if self.waiting >= self.expected:
            self.event.set()
        await self.event.wait()


async def _two_connections_with_two_folders(w: World):
    rows = []
    for subject, identity in (("subject-a", "microsoft_onedrive:drive-a"), ("subject-b", "microsoft_onedrive:drive-b")):
        connection = await w.connect(subject=subject, identity=identity, writable=False)
        folders = [await w.folder(connection, name) for name in ("Docs", "Plans")]
        capabilities = await w.adapter.capabilities(connection.handle)
        await w.connections.set_scope(
            USER, connection.connection_id, folder_ids=folders, whole_drive=False, capabilities=capabilities,
            write_folder_ancestry=None,
            folder_ancestries={f: await w.drive.ancestry(connection.handle, f) for f in folders})
        rows.append((connection, folders))
    return rows


async def test_scope_folders_of_all_rows_are_read_at_the_same_time():
    w = World()
    rows = await _two_connections_with_two_folders(w)
    gate = Rendezvous(4)
    original = w.drive.get_item

    async def gated(handle, item_id):
        await gate.arrive()
        return await original(handle, item_id)
    w.drive.get_item = gated
    status, body = await asyncio.wait_for(call(w.app(), "get", "/api/drive/connections"), TIMEOUT_S)
    assert status == 200
    assert [r["connection_id"] for r in body["connections"]] == [c.connection_id for c, _ in rows]
    for row, (_, folders) in zip(body["connections"], rows):
        assert [f["id"] for f in row["scope"]["folders"]] == folders
        assert [f["path"] for f in row["scope"]["folders"]] == ["Docs", "Plans"]


async def test_the_index_status_of_all_rows_is_read_at_the_same_time():
    w = World()
    rows = await _two_connections_with_two_folders(w)
    gate = Rendezvous(2)
    original = w.search.indexing_status

    async def gated(user_id, *, connection_id=None):
        await gate.arrive()
        return await original(user_id, connection_id=connection_id)
    w.search.indexing_status = gated
    status, body = await asyncio.wait_for(call(w.app(), "get", "/api/drive/connections"), TIMEOUT_S)
    assert status == 200
    assert [r["connection_id"] for r in body["connections"]] == [c.connection_id for c, _ in rows]
    assert all(r["indexing"] is not None for r in body["connections"])


async def test_a_row_part_overlaps_with_another_part_of_the_same_row():
    w = World()
    connection = await w.connect(writable=False)
    folder = await w.folder(connection)
    capabilities = await w.adapter.capabilities(connection.handle)
    await w.connections.set_scope(USER, connection.connection_id, folder_ids=[folder], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={folder: await w.drive.ancestry(connection.handle, folder)})
    gate = Rendezvous(2)
    original_item, original_status = w.drive.get_item, w.search.indexing_status

    async def gated_item(handle, item_id):
        await gate.arrive()
        return await original_item(handle, item_id)

    async def gated_status(user_id, *, connection_id=None):
        await gate.arrive()
        return await original_status(user_id, connection_id=connection_id)
    w.drive.get_item, w.search.indexing_status = gated_item, gated_status
    status, body = await asyncio.wait_for(call(w.app(), "get", "/api/drive/connections"), TIMEOUT_S)
    assert status == 200
    row = body["connections"][0]
    assert row["scope"]["folders"] == [{"id": folder, "path": "R&D"}] and row["indexing"] is not None


async def test_an_index_failure_on_one_row_leaves_only_that_rows_indexing_null():
    w = World()
    rows = await _two_connections_with_two_folders(w)
    failing = rows[0][0].connection_id
    original = w.search.indexing_status

    async def flaky(user_id, *, connection_id=None):
        if connection_id == failing:
            raise RuntimeError("index down")
        return await original(user_id, connection_id=connection_id)
    w.search.indexing_status = flaky
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200
    first, second = body["connections"]
    assert (first["connection_id"], first["indexing"]) == (failing, None)
    assert second["indexing"] is not None
    assert [f["path"] for f in first["scope"]["folders"]] == ["Docs", "Plans"]
    assert [f["path"] for f in second["scope"]["folders"]] == ["Docs", "Plans"]
