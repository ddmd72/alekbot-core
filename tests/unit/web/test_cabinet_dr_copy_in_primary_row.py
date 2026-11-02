"""The deep-research copy switch is one setting per user, and the reports are copied to the primary drive
(MULTI_DRIVE_RFC §11.5, D7). The Cabinet therefore shows it inside the primary drive's row, and below the list only
while no drive is primary, so it never reads as a setting of whichever drive is last. Runs the drive card's
renderers under node when node is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")
_FUNCTIONS = ("renderDriveStatus", "renderDriveConnection", "renderDeepResearchCopy")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _function(name: str) -> str:
    found = re.search(rf"\n(      (?:async )?function {name}\(.*?\n      }}\n)", _HTML, re.S)
    assert found, name
    return found.group(1)


def _row(cid: str, name: str, primary: bool) -> dict:
    return {"connection_id": cid, "mount_name": name, "primary": primary, "state": "active", "write_folder": "",
            "provider_label": "Provider", "account_label": "", "reconnect": None, "indexing": None,
            "searched_by_default": True, "transcribe_enabled": False, "note": ""}


def _render(rows: list, dr_copy=True) -> str:
    data = {"connections": rows, "add_kinds": [{"kind": "k", "label": "K"}], "connect_href": "", "dr_copy": dr_copy}
    script = (
        'const container = { innerHTML: "" };\n'
        'const document = { getElementById: () => container };\n'
        'const DRIVE_BOX = "box"; const DRIVE_ERR = "err"; const DRIVE_NOTE = "note";\n'
        'function t(key) { return "T:" + key; }\n'
        'function escapeHtml(s) { return String(s); }\n'
        'function driveUiOf(id) { return {}; }\n'
        'function connectButton() { return ""; }\n'
        'function renderAddDrive() { return ""; }\n'
        'function renderDriveIndexing() { return ""; }\n'
        'function renderDriveWriteFolder() { return ""; }\n'
        'function renderDriveScope() { return ""; }\n'
        'function renderDrivePicker() { return ""; }\n'
        'function observeElements() {}\n'
        + "".join(_function(name) for name in _FUNCTIONS)
        + f"\nrenderDriveStatus({json.dumps(data)});\nprocess.stdout.write(container.innerHTML);\n")
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout


def test_the_switch_sits_in_the_primary_row_not_the_last_one():
    html = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)])
    assert html.count('id="drive-dr-copy-toggle"') == 1
    rows = [m.start() for m in re.finditer(r'id="drive-name-', html)]
    at = html.index('id="drive-dr-copy-toggle"')
    assert rows[0] < at < rows[1]


def test_the_switch_follows_the_primary_when_it_is_listed_second():
    html = _render([_row("c-two", "Archive", False), _row("c-main", "Drive", True)])
    rows = [m.start() for m in re.finditer(r'id="drive-name-', html)]
    assert html.count('id="drive-dr-copy-toggle"') == 1 and html.index('id="drive-dr-copy-toggle"') > rows[1]
    assert html.index('id="drive-dr-copy-toggle"') < html.index('class="integration-actions"')


def test_with_no_primary_the_switch_stays_below_the_list():
    html = _render([_row("c-one", "Drive", False), _row("c-two", "Archive", False)])
    assert html.count('id="drive-dr-copy-toggle"') == 1
    at = html.index('id="drive-dr-copy-toggle"')
    own_box = html.rindex('style="box"', 0, at)     # a box of its own, opened after the last row
    assert html.index('id="drive-name-c-two"') < own_box < at < html.index('class="integration-actions"')


def test_no_switch_when_its_state_is_unknown():
    assert 'drive-dr-copy-toggle' not in _render([_row("c-main", "Drive", True)], dr_copy=None)
