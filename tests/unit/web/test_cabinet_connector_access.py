"""The Cabinet's "Connector access" (DRIVE_MCP_CONNECTOR_RFC §7.6, §13; slice 3 DELTA 6): the list row carries it, `PUT
…/connector-access` stores nothing / the whole drive / folders inside the scope, a scope change that would strand a
connector folder is refused with the approved line, and the access tab's block and picker. The `World` of the 2b
Cabinet tests (production services over `FakeDriveAdapter`); the JS runs under node as the redesign tests do."""
import json
import re

from src.domain.drive_access_policy import (
    CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE, CONNECTOR_ROOT_LINE, SCOPE_STRANDS_CONNECTOR_LINE,
)
from src.domain.drive_connector_access import ConnectorAccess
from tests.unit.web.test_cabinet_drive_connections_2b import USER, World, call
from tests.unit.web.test_cabinet_drive_redesign import LISTING, _card, _render, _row, _run, node


async def world():
    w = World()
    conn = await w.connect(writable=False)
    rd, oak = await w.folder(conn, "R&D"), await w.folder(conn, "12 Oak Street")
    return w, conn, rd, oak


def access_url(conn) -> str:
    return f"/api/drive/connections/{conn.connection_id}/connector-access"


async def stored(w, conn):
    return (await w.connections.get(USER, conn.connection_id)).connector_access


async def test_the_list_row_carries_the_connector_access_with_paths():
    w, conn, rd, _ = await world()
    row = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]
    assert row["connector_access"] == {"mode": "nothing", "folders": []}
    await w.repo.update_connector_access(USER, conn.connection_id, ConnectorAccess.folders((rd,)))
    row = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]
    assert row["connector_access"] == {"mode": "folders", "folders": [{"id": rd, "path": "R&D"}]}


async def test_put_nothing_whole_and_folders_stores_and_answers_it():
    w, conn, rd, oak = await world()
    status, body = await call(w.app(), "put", access_url(conn), {"mode": "folders", "folder_ids": [rd, oak]})
    assert status == 200
    assert body == {"connector_access": {"mode": "folders", "folders": [{"id": rd, "path": "R&D"},
                                                                         {"id": oak, "path": "12 Oak Street"}]}}
    assert await stored(w, conn) == ConnectorAccess.folders((rd, oak))
    assert await call(w.app(), "put", access_url(conn), {"mode": "whole"}) == (
        200, {"connector_access": {"mode": "whole", "folders": []}})
    assert await stored(w, conn) == ConnectorAccess.whole_drive()
    assert await call(w.app(), "put", access_url(conn), {"mode": "nothing"}) == (
        200, {"connector_access": {"mode": "nothing", "folders": []}})
    assert await stored(w, conn) is None
    assert w.drive_tasks() == []                      # the index is unaffected: no nudge


async def test_a_bad_body_is_400_and_stores_nothing():
    w, conn, rd, _ = await world()
    for body in ({"mode": "everything"}, {"mode": "folders", "folder_ids": []}, {"mode": "folders"},
                 {"mode": "folders", "folder_ids": [1]}, {"mode": "folders", "folder_ids": [""]},
                 {"mode": "whole", "folder_ids": [rd]}, {"folder_ids": [rd]}, ["whole"]):
        status, answer = await call(w.app(), "put", access_url(conn), body)
        assert status == 400 and answer["error"], body
    assert await stored(w, conn) is None


async def test_a_folder_outside_the_scope_or_the_root_is_400_with_its_line():
    w, conn, rd, oak = await world()
    capabilities = await w.adapter.capabilities(conn.handle)
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[rd], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={rd: await w.drive.ancestry(conn.handle, rd)})
    status, body = await call(w.app(), "put", access_url(conn), {"mode": "folders", "folder_ids": [oak]})
    assert (status, body["error"]) == (400, CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE.format(drive="Drive"))
    root = (await w.adapter.get_root(conn.handle)).item_id
    status, body = await call(w.app(), "put", access_url(conn), {"mode": "folders", "folder_ids": [root]})
    assert (status, body["error"]) == (400, CONNECTOR_ROOT_LINE)
    assert await stored(w, conn) is None


async def test_another_users_or_a_disconnected_connection_is_404():
    w, conn, rd, _ = await world()
    other = await w.connect(user="usr-2", subject="subject-o", identity="microsoft_onedrive:drive-o", writable=False)
    status, _ = await call(w.app(), "put", access_url(other), {"mode": "whole"})
    assert status == 404 and await stored_of(w, "usr-2", other) is None
    await w.connections.disconnect(USER, conn.connection_id)
    status, _ = await call(w.app(), "put", access_url(conn), {"mode": "whole"})
    assert status == 404 and await stored(w, conn) is None


async def stored_of(w, user, conn):
    return (await w.connections.get(user, conn.connection_id)).connector_access


async def test_a_scope_change_that_strands_a_connector_folder_is_400_with_the_approved_line():
    w, conn, rd, oak = await world()
    assert (await call(w.app(), "put", access_url(conn), {"mode": "folders", "folder_ids": [rd]}))[0] == 200
    status, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/scope",
                              {"whole_drive": False, "folder_ids": [oak]})
    assert (status, body["error"]) == (400, SCOPE_STRANDS_CONNECTOR_LINE)
    assert body["error"] == ("This would leave folders the connector reads outside what the bot sees: change the "
                             "connector access first.")
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == ()
    status, _ = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/scope",
                           {"whole_drive": False, "folder_ids": [rd]})
    assert status == 200


# --------------------------------------------------------------------------------------------- the page

CONNECTOR_ROW = _row(means="nothing", folders=[("f-proj", "Projects")], write="none",
                     connector_access={"mode": "folders", "folders": [{"id": "f-proj", "path": "Projects"}]})


def _connector_picker(body: str, row=CONNECTOR_ROW) -> dict:
    data = {"connections": [row], "add_kinds": [], "dr_copy": None}
    return _run(LISTING + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "connector");\n' + body
                + "\nprocess.stdout.write(JSON.stringify({ calls: calls.filter((c) => c[0] === 'PUT'),"
                  " open: driveUiOf('c1').picker !== null, error: driveUiOf('c1').picker ? driveUiOf('c1').picker.error : null,"
                  " list: els['drive-pk-list'].innerHTML, side: els['drive-pk-side'].innerHTML }));")


@node
def test_the_access_tab_shows_the_connector_block_with_the_current_choice():
    html = _card(_render([CONNECTOR_ROW], 'driveUiOf("c1").tab = "access";'), "c1")
    block = html[html.index('id="drive-connector-c1"'):]
    for key in ("connector_title", "connector_explain", "connector_nothing", "connector_whole", "connector_choose"):
        assert f"T:drive.{key}" in block, key
    assert "Projects" in block
    pressed = re.findall(r'aria-pressed="(true|false)"[^>]*>T:drive\.(connector_\w+)<', block)
    assert pressed == [("false", "connector_nothing"), ("false", "connector_whole"), ("true", "connector_choose")]
    whole = _card(_render([_row(connector_access={"mode": "whole", "folders": []})], 'driveUiOf("c1").tab = "access";'),
                  "c1")
    assert re.search(r'aria-pressed="true"[^>]*>T:drive\.connector_whole<', whole)
    overview = _card(_render([CONNECTOR_ROW], 'driveUiOf("c1").tab = "overview";'), "c1")
    assert "T:drive.connector_title" in overview and "T:drive.connector_n" in overview


@node
def test_saving_the_connector_picker_puts_to_the_connector_access_route():
    out = _connector_picker('removeDrivePickerChip("c1", 0); await saveDrivePicker("c1");')
    assert out["calls"] == [["PUT", "/api/drive/connections/c1/connector-access", {"mode": "nothing"}]]
    out = _connector_picker('removeDrivePickerChip("c1", 0); toggleDrivePickerFolder("c1", 2); await saveDrivePicker("c1");')
    assert out["calls"] == [["PUT", "/api/drive/connections/c1/connector-access", {"mode": "folders", "folder_ids": ["f-sub"]}]]
    assert out["open"] is False
    refused = _connector_picker('respond = (m) => m === "PUT" ? { ok: false, data: { error: "refused line" } } : { ok: true, data: {} };'
                                ' removeDrivePickerChip("c1", 0); toggleDrivePickerFolder("c1", 2); await saveDrivePicker("c1");')
    assert refused["open"] is True and refused["error"] == "refused line"
    data = {"connections": [CONNECTOR_ROW], "add_kinds": [], "dr_copy": None}
    out = _run("driveData = " + json.dumps(data) + '; await saveDriveConnectorAccess("c1", "whole", [], false);'
               " process.stdout.write(JSON.stringify(calls));")
    assert out == [["PUT", "/api/drive/connections/c1/connector-access", {"mode": "whole"}]]


@node
def test_folders_outside_the_scope_are_not_selectable_for_the_connector():
    row = _row(means="nothing", folders=[("f-proj", "Projects")], write="none",
               connector_access={"mode": "nothing", "folders": []})
    out = _connector_picker("", row=row)
    rows = {re.search(r'class="dv-prow-name">([^<]+)<', r).group(1): r
            for r in out["list"].split('<div class="dv-prow')[1:]}
    assert set(rows) == {"Projects", "Invoices 2026", "Inbox"}
    assert "disabled" not in rows["Projects"].split("<button")[0]
    assert "disabled" in rows["Invoices 2026"].split("<button")[0]
    assert "T:drive.pk_not_visible" in rows["Invoices 2026"]
    assert "T:drive.whole_title" not in out["side"]


async def test_without_the_drive_service_the_route_says_501_and_stores_nothing():
    w, conn, _, _ = await world()
    status, _ = await call(w.app(user_drive_service=None), "put", access_url(conn), {"mode": "whole"})
    assert status == 501 and await stored(w, conn) is None
