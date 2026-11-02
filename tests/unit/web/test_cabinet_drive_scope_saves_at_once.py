"""The Cabinet's drive scope (MULTI_DRIVE_RFC §11.7) is saved by the action itself, like the write folder: picking a
folder, removing one and the whole-drive box each send `PUT /api/drive/connections/<id>/scope` at once. There is no
separate Save step, which let a picked folder look chosen while the drive kept its default scope. The server keeps no
folders under "whole drive", so the page remembers them while it is open and unticking the box restores them. The
static check reads the page; the behavioural ones run the scope functions under node when node is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")
_FUNCTIONS = ("driveScopeOf", "toggleDriveScopeWhole", "removeDriveScopeFolder", "saveDriveScope", "pickDriveFolder")
_ROW = {"connection_id": "c1", "scope": {"whole_drive": False,
                                         "folders": [{"id": "fold-a", "path": "Projects"},
                                                     {"id": "fold-b", "path": "Archive"}]}}


def _function(name: str) -> str:
    found = re.search(rf"\n(      (?:async )?function {name}\(.*?\n      }}\n)", _HTML, re.S)
    assert found, name
    return found.group(1)


def _run(body: str, *, refuse: bool = False):
    # a saved scope comes back on the row the way the reload after a save shows it
    stored = ('if (method === "PUT" && path.endsWith("/scope")) driveData.connections[0].scope = {'
              ' whole_drive: body.whole_drive, folders: body.folder_ids.map((id) => ({ id, path: id })) };\n')
    script = (
        'const API = ""; const calls = []; const toasts = []; let reloads = 0; let _driveStatusSnapshot = {};\n'
        f'const driveData = {{ connections: [{json.dumps(_ROW)}] }};\n'
        'const ui = { picker: { purpose: "scope", trail: [], folders: [{ id: "fold-c", name: "Drafts", path: "Drafts" },'
        ' { id: "fold-a", name: "Projects", path: "Projects" }], error: "" } };\n'
        'function driveUiOf(id) { return ui; }\n'
        'function t(key) { return "T:" + key; }\n'
        'function showToast(msg, type) { toasts.push([msg, type]); }\n'
        'function redrawDrive() {}\n'
        'async function loadDriveStatus() { reloads += 1; }\n'
        'async function saveDriveWriteFolder(id, value) { calls.push(["write-folder", value]); }\n'
        'let refuse = ' + json.dumps(refuse) + ';\n'
        'async function driveCall(method, path, body) { calls.push([method, path, body]);\n'
        '  if (refuse) return { ok: false, data: { error: "refused" } };\n'
        + stored + '  return { ok: true, data: {} }; }\n'
        + "".join(_function(name) for name in _FUNCTIONS)
        + "\n(async () => {\n" + body
        + "\nprocess.stdout.write(JSON.stringify({ calls, toasts, reloads, picker: ui.picker !== null,"
          " error: ui.scopeError || '' }));\n})().catch((e) => { console.error(e); process.exit(1); });\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _put(whole: bool, ids: list) -> list:
    return ["PUT", "/api/drive/connections/c1/scope", {"whole_drive": whole, "folder_ids": ids}]


def test_the_scope_block_has_no_separate_save_step():
    block = _function("renderDriveScope")
    assert "saveDriveScope" not in block and "drive.save" not in block
    assert "draft" not in "".join(_function(name) for name in _FUNCTIONS + ("renderDriveScope",))


node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
def test_picking_a_folder_saves_the_scope_with_it_and_closes_the_picker():
    out = _run('await pickDriveFolder("c1", 0);')
    assert out["calls"] == [_put(False, ["fold-a", "fold-b", "fold-c"])]
    assert out["toasts"] == [["T:toast.drive_saved", "success"]] and out["reloads"] == 1 and not out["picker"]


@node
def test_picking_a_folder_already_in_the_scope_does_not_list_it_twice():
    assert _run('await pickDriveFolder("c1", 1);')["calls"] == [_put(False, ["fold-a", "fold-b"])]


@node
def test_removing_a_folder_saves_the_scope_without_it():
    assert _run('await removeDriveScopeFolder("c1", 0);')["calls"] == [_put(False, ["fold-b"])]


@node
def test_unticking_whole_drive_restores_the_folders_chosen_before_it():
    out = _run('await toggleDriveScopeWhole("c1", true); await toggleDriveScopeWhole("c1", false);')
    assert out["calls"] == [_put(True, []), _put(False, ["fold-a", "fold-b"])]


@node
def test_after_a_page_reload_unticking_whole_drive_leaves_the_default_scope():
    out = _run('await toggleDriveScopeWhole("c1", true); delete ui.foldersBeforeWhole;'
               ' await toggleDriveScopeWhole("c1", false);')
    assert out["calls"] == [_put(True, []), _put(False, [])]


@node
def test_a_refused_untick_keeps_the_remembered_folders_for_the_next_try():
    out = _run('await toggleDriveScopeWhole("c1", true); refuse = true; await toggleDriveScopeWhole("c1", false);'
               ' refuse = false; await toggleDriveScopeWhole("c1", false);')
    assert out["calls"] == [_put(True, []), _put(False, ["fold-a", "fold-b"]), _put(False, ["fold-a", "fold-b"])]


@node
def test_a_refused_scope_shows_the_reason_and_keeps_the_picker_open():
    out = _run('await pickDriveFolder("c1", 0);', refuse=True)
    assert out["calls"] == [_put(False, ["fold-a", "fold-b", "fold-c"])]
    assert out["error"] == "refused" and out["picker"] and out["reloads"] == 0 and out["toasts"] == []


@node
def test_the_write_folder_picker_still_saves_the_write_folder():
    out = _run('ui.picker.purpose = "write"; await pickDriveFolder("c1", 0);')
    assert out["calls"] == [["write-folder", "fold-c"]]
