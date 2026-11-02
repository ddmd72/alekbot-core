"""Each drive of the Cabinet list (MULTI_DRIVE_RFC §11.7) is one status line that opens into its settings: the line
names the drive, its provider, its account and its state, and the primary drive carries its badge there. An opened row
stays open when the page redraws; a row that needs a reconnect opens by itself. Runs the row renderer under node when
node is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _function(name: str) -> str:
    found = re.search(rf"\n(      (?:async )?function {name}\(.*?\n      }}\n)", _HTML, re.S)
    assert found, name
    return found.group(1)


def _row(cid: str, name: str, primary: bool, reconnect=None) -> dict:
    return {"connection_id": cid, "mount_name": name, "primary": primary, "state": "active", "write_folder": "",
            "provider_label": "Provider", "account_label": "someone@example.com", "reconnect": reconnect,
            "reconnect_href": "", "indexing": None, "searched_by_default": True, "transcribe_enabled": False,
            "note": ""}


def _render(rows: list, body: str = "") -> list:
    """The rendered rows, after `body` ran (it may set `ui`); one string per row."""
    data = {"connections": rows, "dr_copy": None}
    script = (
        'const DRIVE_BOX = "box"; const DRIVE_ERR = "err"; const DRIVE_NOTE = "note";\n'
        'const driveUi = {}; const driveUiOf = (id) => (driveUi[id] = driveUi[id] || {});\n'
        'function t(key) { return "T:" + key; }\n'
        'function escapeHtml(s) { return String(s); }\n'
        'function connectButton() { return ""; }\n'
        'function renderDriveIndexing() { return ""; }\n'
        'function renderDriveWriteFolder() { return ""; }\n'
        'function renderDriveScope() { return ""; }\n'
        'function renderDrivePicker() { return ""; }\n'
        'function renderDeepResearchCopy() { return ""; }\n'
        + _function("renderDriveConnection")
        + f"\nconst data = {json.dumps(data)};\n" + body
        + "\nprocess.stdout.write(JSON.stringify(data.connections.map((r) => renderDriveConnection(r, data))));\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _summary(row_html: str) -> str:
    return re.search(r"<summary.*?</summary>", row_html, re.S).group(0)


def _opening_tag(row_html: str) -> str:
    return re.search(r"<details[^>]*>", row_html).group(0)


def test_a_row_is_one_closed_line_naming_drive_provider_account_and_state():
    main, other = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)])
    for html, name in ((main, "Drive"), (other, "Archive")):
        assert html.lstrip().startswith('<details class="drive-row"')
        assert " open" not in _opening_tag(html)
        line = re.sub(r"<[^>]+>", "", _summary(html))
        assert f"{name} · Provider · someone@example.com · T:platform.connected" in line
        assert 'id="drive-name-' not in _summary(html)          # the settings sit below the line
    assert "T:drive.primary_badge" in _summary(main) and "T:drive.primary_badge" not in _summary(other)


def test_make_primary_is_a_setting_inside_the_row_not_on_the_line():
    main, other = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)])
    assert "makeDrivePrimary" not in _summary(other) and "makeDrivePrimary('c-two')" in other
    assert "makeDrivePrimary" not in main


def test_an_opened_row_stays_open_when_the_page_redraws():
    html, = _render([_row("c-two", "Archive", False)], 'driveUiOf("c-two").open = true;')
    assert " open" in _opening_tag(html)
    assert "ontoggle=\"driveUiOf('c-two').open = this.open\"" in _opening_tag(html)


def test_a_row_that_needs_a_reconnect_opens_by_itself_until_the_owner_closes_it():
    reconnect = {"credential_id": "cred-1"}
    html, = _render([_row("c-two", "Archive", False, reconnect)])
    assert " open" in _opening_tag(html) and "T:drive.needs_reconnect" in _summary(html)
    closed, = _render([_row("c-two", "Archive", False, reconnect)], 'driveUiOf("c-two").open = false;')
    assert " open" not in _opening_tag(closed)
