"""The Cabinet's drive routes when a collaborator is absent or fails (slice 2b, P-E): the card keeps answering with
empty display fields and a warning, a route that needs the drive service answers 501, a malformed heir answers 400.
Over the world of `test_cabinet_drive_connections_2b`."""
import logging

import pytest

from tests.unit.web.test_cabinet_drive_connections_2b import Notes, USER, World, call


class BrokenNotes(Notes):
    async def drive_note_texts(self, user_id):
        raise RuntimeError("notes down")


class GrantPort:
    """The credential store's key listing as it is; a decrypt of the grant fails."""

    def __init__(self, real):
        self._real = real

    async def list_keys(self, user_id, kind):
        return await self._real.list_keys(user_id, kind)

    async def get(self, user_id, credential_id):
        raise RuntimeError("decrypt failed")


async def _with_write_folder(w, connection, name="R&D"):
    folder = await w.folder(connection, name)
    capabilities = await w.adapter.capabilities(connection.handle)
    chain = await w.drive.ancestry(connection.handle, folder)
    await w.connections.set_write_folder(USER, connection.connection_id, folder, capabilities=capabilities,
                                         ancestry=chain)
    return folder


async def _row(w, **over):
    status, body = await call(w.app(**over), "get", "/api/drive/connections")
    assert status == 200
    return body["connections"][0]


async def test_without_the_drive_service_the_write_folder_path_is_empty():
    w = World()
    connection = await w.connect()
    folder = await _with_write_folder(w, connection)
    row = await _row(w, user_drive_service=None)
    assert row["write_folder"] == {"id": folder, "path": ""}


async def test_a_failing_folder_read_leaves_the_path_empty_and_warns(caplog):
    w = World()
    connection = await w.connect()
    folder = await _with_write_folder(w, connection)

    async def broken(handle, item_id):
        raise RuntimeError("drive down")
    w.drive.get_item = broken
    with caplog.at_level(logging.WARNING):
        row = await _row(w)
    assert row["write_folder"] == {"id": folder, "path": ""}
    assert any("reading a folder path failed" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)


async def test_without_a_credentials_port_the_account_label_is_empty():
    w = World()
    await w.connect()
    assert (await _row(w, oauth_credentials_port=None))["account_label"] == ""


async def test_a_failing_grant_read_leaves_the_account_label_empty_and_warns(caplog):
    w = World()
    await w.connect()
    with caplog.at_level(logging.WARNING):
        row = await _row(w, oauth_credentials_port=GrantPort(w.store.repo))
    assert row["account_label"] == "" and row["reconnect"] is None
    assert any("reading the account label failed" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)


async def test_a_failing_notes_read_leaves_the_note_empty_and_warns(caplog):
    w = World()
    await w.connect()
    w.notes = BrokenNotes()
    with caplog.at_level(logging.WARNING):
        row = await _row(w)
    assert row["note"] == ""
    assert any("reading the drive notes failed" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)


async def test_a_failing_read_of_the_primarys_folders_does_not_stop_a_rename(caplog):
    w = World()
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b", writable=False)

    async def broken(handle):
        raise RuntimeError("listing down")
    w.drive.top_folder_names = broken
    with caplog.at_level(logging.WARNING):
        status, body = await call(w.app(), "put", f"/api/drive/connections/{second.connection_id}/name",
                                  {"name": "Acme"})
    assert status == 200 and body["name"] == "Acme"
    assert any("reading the primary's folders failed" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)
    assert w.repo.connections[(USER, second.connection_id)].mount_name == "Acme"
    assert first.connection_id != second.connection_id


@pytest.mark.parametrize("method, suffix, body", [
    ("put", "/scope", {"whole_drive": True, "folder_ids": []}),
    ("put", "/write-folder", {"folder_id": ""}),
    ("get", "/folders", None),
])
async def test_a_route_that_reads_the_drive_answers_501_without_the_drive_service(method, suffix, body):
    w = World()
    connection = await w.connect()
    before = w.repo.connections[(USER, connection.connection_id)]
    status, answer = await call(w.app(user_drive_service=None), method,
                                f"/api/drive/connections/{connection.connection_id}{suffix}", body)
    assert (status, answer) == (501, {"error": "Drive integration not configured"})
    assert w.repo.connections[(USER, connection.connection_id)] == before


@pytest.mark.parametrize("heir", [7, ["conn-2"], {"id": "conn-2"}])
async def test_a_heir_that_is_not_a_string_is_400_and_nothing_is_disconnected(heir):
    w = World()
    first = await w.connect()
    await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    status, body = await call(w.app(), "delete", f"/api/drive/connections/{first.connection_id}", {"heir_id": heir})
    assert (status, body) == (400, {"error": "heir_id must be a string"})
    assert w.repo.connections[(USER, first.connection_id)].state.value == "active"
