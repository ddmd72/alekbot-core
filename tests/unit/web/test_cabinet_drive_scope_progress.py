"""The Cabinet shows a scope change's progress and its edges (DRIVE_SCOPE_WALK_RFC §8, W20, W21): the `scope_progress`
block of the index status, a saved folder the drive no longer has (`missing`, kept savable so it can be removed), the
one status line per drive (removing outside -> removing -> walking with the blocked count -> building -> checking), and
the warning before a whole drive -> folders save. Invented cast only."""
import json
import subprocess
from typing import Any

import pytest

from src.domain.drive_indexing_status import DriveIndexingStatus
from tests.unit.web import test_cabinet_drive_redesign as page
from tests.unit.web.test_cabinet_drive_connections_2b import AUTH, USER, World, call
from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts

KEYS = ("drive.scope_removing", "drive.scope_removing_outside", "drive.scope_walking", "drive.scope_blocked",
        "drive.scope_narrow_warning", "drive.folder_missing")

# ------------------------------------------------------------------------------------------------- the server


class _ProgressSearch:
    def __init__(self, **fields: Any) -> None:
        self.fields = fields

    async def indexing_status(self, user_id, *, connection_id=None):
        return DriveIndexingStatus(enabled=True, counters={}, **self.fields)

    async def has_mirror_root(self, user_id):
        return True


async def test_the_index_block_carries_the_scope_progress():
    w = World()
    await w.connect()
    search = _ProgressSearch(scope_removing=1, scope_removing_outside=True, scope_walking=True,
                             scope_listed=10, scope_blocked=2)
    block = (await call(w.app(drive_search_service=search), "get", "/api/drive/connections")
             )[1]["connections"][0]["indexing"]
    assert block["scope_progress"] == {"removing": 1, "removing_outside": True, "walking": True,
                                       "listed": 10, "blocked": 2}


async def test_an_idle_index_block_has_a_zero_scope_progress():
    w = World()
    await w.connect()
    block = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]["indexing"]
    assert block["scope_progress"] == {"removing": 0, "removing_outside": False, "walking": False,
                                       "listed": 0, "blocked": 0}


async def _saved_folder(w, conn, name="R&D"):
    folder = await w.folder(conn, name)
    capabilities = await w.adapter.capabilities(conn.handle)
    chain = await w.drive.ancestry(conn.handle, folder)
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[folder], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={folder: chain})
    return folder


async def test_a_saved_folder_the_drive_no_longer_has_is_listed_as_missing():
    w = World()
    conn = await w.connect(writable=False)
    kept = await _saved_folder(w, conn, "Springfield")
    gone = await w.folder(conn, "Coimbra 2024")
    capabilities = await w.adapter.capabilities(conn.handle)
    chains = {kept: await w.drive.ancestry(conn.handle, kept), gone: await w.drive.ancestry(conn.handle, gone)}
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[kept, gone], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None, folder_ancestries=chains)
    await w.adapter.delete(conn.handle, gone)
    folders = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]["scope"]["folders"]
    assert folders == [{"id": kept, "path": "Springfield"}, {"id": gone, "path": "", "missing": True}]
    assert "missing" not in folders[0]


async def test_a_save_that_keeps_a_missing_saved_folder_is_accepted():
    w = World()
    conn = await w.connect(writable=False)
    gone = await _saved_folder(w, conn, "Coimbra 2024")
    other = await w.folder(conn, "R&D")
    await w.adapter.delete(conn.handle, gone)
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    status, body = await call(w.app(), "put", path, {"whole_drive": False, "folder_ids": [gone, other]})
    assert status == 200 and body["folder_ids"] == [gone, other]
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == (gone, other)


async def test_removing_a_missing_saved_folder_is_a_save_without_it():
    w = World()
    conn = await w.connect(writable=False)
    gone = await _saved_folder(w, conn, "Coimbra 2024")
    other = await w.folder(conn, "R&D")
    await w.adapter.delete(conn.handle, gone)
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    status, body = await call(w.app(), "put", path, {"whole_drive": False, "folder_ids": [other]})
    assert status == 200 and body["folder_ids"] == [other]


async def test_a_new_unknown_folder_id_is_still_404():
    w = World()
    conn = await w.connect(writable=False)
    await _saved_folder(w, conn)
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    assert (await call(w.app(), "put", path, {"whole_drive": False, "folder_ids": ["f-nope"]}))[0] == 404


async def test_a_scope_that_leaves_the_write_folder_outside_is_still_400():
    w = World()
    conn = await w.connect(writable=False)
    gone = await w.folder(conn, "Coimbra 2024")
    inbox = await w.folder(conn, "Springfield")
    capabilities = await w.adapter.capabilities(conn.handle)
    chains = {gone: await w.drive.ancestry(conn.handle, gone), inbox: await w.drive.ancestry(conn.handle, inbox)}
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[gone, inbox], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None, folder_ancestries=chains)
    await w.connections.set_write_folder(USER, conn.connection_id, inbox, capabilities=capabilities,
                                         ancestry=chains[inbox])
    await w.adapter.delete(conn.handle, gone)
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    status, body = await call(w.app(), "put", path, {"whole_drive": False, "folder_ids": [gone]})
    assert status == 400 and body["error"]
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == (gone, inbox)


# ------------------------------------------------------------------------------------------------- the strings

def test_the_six_keys_exist_in_all_four_languages_and_are_translated():
    _, dicts = _dicts()
    for key in KEYS:
        assert all(dicts[lang].get(key) for lang in LANGS), key
        assert all(dicts[lang][key] != dicts["en"][key] for lang in ("uk", "fr", "es")), key
    for key in ("drive.scope_removing", "drive.scope_walking", "drive.scope_blocked"):
        assert all("{n}" in dicts[lang][key] for lang in LANGS), key


# ------------------------------------------------------------------------------------------------- the page

_EN = _dicts()[1]["en"]
_PRELUDE = page.PRELUDE.replace('function t(key) { return "T:" + key; }',
                                "const EN = " + json.dumps({k: _EN[k] for k in KEYS}) + ";"
                                ' function t(key) { return EN[key] || "T:" + key; }'
                                " let confirmAnswer = false; const confirms = [];"
                                " function confirm(m) { confirms.push(m); return confirmAnswer; }")


def _run(body: str) -> Any:
    script = (_PRELUDE + page.ESCAPE + "\n" + page.CARD + page.COUNTERS + page.INDEX + page.PICKER
              + "\n(async () => {\n" + body + "\n})().catch((e) => { console.error(e); process.exit(1); });\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _row(progress=None, building=False, counters=None, **over):
    row = page._row("c1", "Work", means="area", counters=counters or {}, **over)
    row["indexing"]["building"] = building
    if progress is not None:
        row["indexing"]["scope_progress"] = {"removing": 0, "removing_outside": False, "walking": False,
                                             "listed": 0, "blocked": 0, **progress}
    return row


def _tabs(row) -> dict:
    data = {"connections": [row], "add_kinds": [], "dr_copy": None}
    return _run("driveData = " + json.dumps(data) + ";"
                " const out = {}; for (const tab of ['overview', 'index']) { driveUiOf('c1').open = true;"
                " driveUiOf('c1').tab = tab; renderDriveStatus(driveData); out[tab] = els['drive-container'].innerHTML; }"
                " process.stdout.write(JSON.stringify(out));")


LINES = {
    "outside": "Removing what lies outside the chosen folders from the index…",
    "removing": "Removing 1 folder(s) from the index…",
    "walking": "Reading the chosen folders: 10 folders read",
    "blocked": "2 folders could not be read yet",
    "building": "T:drive.index_building",
}


@page.node
@pytest.mark.parametrize("progress, building, shown, hidden", [
    (dict(removing_outside=True, removing=1, walking=True, listed=10, blocked=2), True, ["outside"],
     ["removing", "walking", "blocked", "building"]),
    (dict(removing=1, walking=True, listed=10, blocked=2), True, ["removing"], ["outside", "walking", "blocked", "building"]),
    (dict(walking=True, listed=10, blocked=2), True, ["walking", "blocked"], ["outside", "removing", "building"]),
    (dict(walking=True, listed=10), True, ["walking"], ["outside", "removing", "blocked", "building"]),
    (dict(), True, ["building"], ["outside", "removing", "walking", "blocked"]),
])
def test_one_scope_line_at_a_time_in_the_order_removing_walking_building(progress, building, shown, hidden):
    tabs = _tabs(_row(progress, building=building))
    for tab, html in tabs.items():
        for name in shown:
            assert LINES[name] in html, (tab, name)
        for name in hidden:
            assert LINES[name] not in html, (tab, name)


@page.node
def test_the_checking_line_shows_only_when_no_scope_line_does():
    counters = {"unresolved": 500, "carded": 10}
    quiet = _tabs(_row({}, counters=counters))
    assert all("T:drive.index_checking" in html for html in quiet.values())
    busy = _tabs(_row(dict(walking=True, listed=10), building=True, counters=counters))
    assert all("T:drive.index_checking" not in html for html in busy.values())


@page.node
def test_a_drive_that_needs_reconnect_shows_no_scope_line():
    row = _row(dict(walking=True, listed=10), building=True, reconnect={"credential_id": "k"})
    assert all(LINES["walking"] not in html for html in _tabs(row).values())


def test_the_poll_keeps_going_while_only_the_scope_change_is_busy():
    assert "row.indexing.building || scopeBusy(row.indexing)" in page._HTML
    assert "sp.removing_outside || sp.removing > 0 || sp.walking" in page._HTML


@page.node
def test_a_missing_folder_shows_its_line_and_the_remove_action():
    row = page._row("c1", "Work", means="nothing", folders=[("f-rd", "R&D")])
    row["scope"]["folders"].append({"id": "f-coimbra", "path": "", "missing": True})
    html = _run("driveData = " + json.dumps({"connections": [row], "add_kinds": [], "dr_copy": None})
                + "; driveUiOf('c1').open = true; driveUiOf('c1').tab = 'access'; renderDriveStatus(driveData);"
                  " process.stdout.write(JSON.stringify(els['drive-container'].innerHTML));")
    assert html.count(_EN["drive.folder_missing"]) == 1
    assert "removeDriveScopeFolder('c1', 1)" in html


WHOLE = page._row("c1", "Work", means="nothing", whole=True)
LISTING = page.LISTING


def _picker(body: str, row: dict) -> dict:
    data = {"connections": [row], "add_kinds": [], "dr_copy": None}
    return _run(LISTING + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "scope");\n' + body
                + "\nprocess.stdout.write(JSON.stringify({ puts: calls.filter((c) => c[0] === 'PUT'), confirms,"
                  " foot: els['drive-pk-foot'].innerHTML }));")


@page.node
def test_the_picker_warns_only_for_a_whole_to_folders_draft():
    untouched = _picker("", WHOLE)
    assert EN_WARNING not in untouched["foot"]
    narrowed = _picker('toggleDrivePickerWhole("c1", false); toggleDrivePickerFolder("c1", 0);', WHOLE)
    assert EN_WARNING in narrowed["foot"] and narrowed["puts"] == []
    # a folders-only drive changing its folders is not a narrowing of a whole drive
    folders = page._row("c1", "Work", means="nothing", folders=[("f-proj", "Projects")])
    assert EN_WARNING not in _picker("toggleDrivePickerFolder('c1', 1);", folders)["foot"]


@page.node
def test_cancel_after_the_warning_saves_nothing():
    out = _picker('toggleDrivePickerWhole("c1", false); toggleDrivePickerFolder("c1", 0); closeDrivePicker("c1");', WHOLE)
    assert out["puts"] == []


@page.node
def test_unticking_the_whole_drive_outside_the_picker_asks_first_and_cancel_saves_nothing():
    base = "driveData = " + json.dumps({"connections": [WHOLE], "add_kinds": [], "dr_copy": None})
    declined = _run(base + '; await toggleDriveScopeWhole("c1", false);'
                           ' process.stdout.write(JSON.stringify({ calls, confirms }));')
    assert declined["calls"] == [] and declined["confirms"] == [EN_WARNING]
    accepted = _run(base + '; confirmAnswer = true; await toggleDriveScopeWhole("c1", false);'
                           ' process.stdout.write(JSON.stringify({ calls, confirms }));')
    assert accepted["calls"] == [["PUT", "/api/drive/connections/c1/scope", {"whole_drive": False, "folder_ids": []}]]


@page.node
def test_choosing_the_whole_drive_never_asks():
    folders = page._row("c1", "Work", means="nothing", folders=[("a", "Projects")])
    out = _run("driveData = " + json.dumps({"connections": [folders], "add_kinds": [], "dr_copy": None})
               + '; await toggleDriveScopeWhole("c1", true); process.stdout.write(JSON.stringify({ calls, confirms }));')
    assert out["confirms"] == [] and len(out["calls"]) == 1


EN_WARNING = _EN["drive.scope_narrow_warning"]
