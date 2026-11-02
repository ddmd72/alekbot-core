"""The deep-research copy switch is one setting per user, and the reports are copied to the primary drive
(MULTI_DRIVE_RFC §11.5, D7). The Cabinet therefore shows it inside the primary drive's card (its Settings tab), and
below the list only while no drive is primary, so it never reads as a setting of whichever drive is last. Runs the
drive card's renderers under node when node is installed, over the harness of test_cabinet_drive_redesign with the
real switch renderer."""
import json
import re
import shutil
from pathlib import Path

import pytest

from tests.unit.web.test_cabinet_drive_redesign import CARD, COUNTERS, INDEX, PICKER, _card, _run

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")

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
    """Every card opened on its Settings tab, so the switch shows wherever a card carries it."""
    data = {"connections": rows, "add_kinds": [{"kind": "k", "label": "K"}], "connect_href": "", "dr_copy": dr_copy}
    opened = "".join(f'driveUiOf({json.dumps(r["connection_id"])}).open = true;'
                     f'driveUiOf({json.dumps(r["connection_id"])}).tab = "settings";' for r in rows)
    scope = CARD + COUNTERS + INDEX + PICKER + _function("renderDeepResearchCopy")   # the real switch, not the stub
    out = _run(f"driveData = {json.dumps(data)};{opened}\nrenderDriveStatus(driveData);"
               " process.stdout.write(JSON.stringify({ html: els['drive-container'].innerHTML }));", scope=scope)
    return out["html"]


def test_the_switch_sits_in_the_primary_row_not_the_last_one():
    html = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)])
    assert html.count('id="drive-dr-copy-toggle"') == 1
    assert 'id="drive-dr-copy-toggle"' in _card(html, "c-main") and 'id="drive-dr-copy-toggle"' not in _card(html, "c-two")


def test_the_switch_follows_the_primary_when_it_is_listed_second():
    html = _render([_row("c-two", "Archive", False), _row("c-main", "Drive", True)])
    assert html.count('id="drive-dr-copy-toggle"') == 1
    assert 'id="drive-dr-copy-toggle"' in _card(html, "c-main") and 'id="drive-dr-copy-toggle"' not in _card(html, "c-two")
    assert html.index('id="drive-card-c-two"') < html.index('id="drive-card-c-main"')


def test_with_no_primary_the_switch_stays_below_the_list():
    html = _render([_row("c-one", "Drive", False), _row("c-two", "Archive", False)])
    assert html.count('id="drive-dr-copy-toggle"') == 1
    at = html.index('id="drive-dr-copy-toggle"')
    own_box = html.rindex('class="dv-box"', 0, at)     # a box of its own, opened after the last row
    assert html.index('id="drive-name-c-two"') < html.rindex("</article>") < own_box < at


def test_no_switch_when_its_state_is_unknown():
    assert 'drive-dr-copy-toggle' not in _render([_row("c-main", "Drive", True)], dr_copy=None)
    assert 'drive-dr-copy-toggle' not in _render([_row("c-one", "Drive", False)], dr_copy=None)
