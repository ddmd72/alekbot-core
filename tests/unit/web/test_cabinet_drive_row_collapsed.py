"""Each drive of the Cabinet list (MULTI_DRIVE_RFC §11.7) is one summary that opens into its settings: the summary
names the drive, its provider, its account and its state, and the primary drive carries its badge there. An opened card
stays open when the page redraws; a card that needs a reconnect opens by itself until the owner closes it. Runs the card
renderer under node when node is installed, over the harness of test_cabinet_drive_redesign."""
import json
import re
import shutil

import pytest

from tests.unit.web.test_cabinet_drive_redesign import _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _row(cid: str, name: str, primary: bool, reconnect=None) -> dict:
    return {"connection_id": cid, "mount_name": name, "primary": primary, "state": "active", "write_folder": "",
            "provider_label": "Provider", "account_label": "someone@example.com", "reconnect": reconnect,
            "reconnect_href": "", "indexing": None, "searched_by_default": True, "transcribe_enabled": False,
            "note": ""}


def _render(rows: list, body: str = "") -> list:
    """The rendered cards, after `body` ran (it may set the ui or toggle a card); one string per card."""
    data = {"connections": rows, "add_kinds": [], "dr_copy": None}
    out = _run(f"driveData = {json.dumps(data)};\n{body}\nrenderDriveStatus(driveData);"
               " process.stdout.write(JSON.stringify({ html: els['drive-container'].innerHTML }));")
    html = out["html"]
    cards = re.findall(r"<article .*?</article>", html, re.S)
    assert len(cards) == len(rows)
    return [next(c for c in cards if f'id="drive-card-{r["connection_id"]}"' in c.split(">", 1)[0]) for r in rows]


def _summary(card_html: str) -> str:
    return re.search(r'<button type="button" class="dv-sum".*?</button>', card_html, re.S).group(0)


def _opening_tag(card_html: str) -> str:
    return re.match(r"<article[^>]*>", card_html).group(0)


def _is_open(card_html: str, cid: str) -> bool:
    return 'aria-expanded="true"' in _summary(card_html) and f'id="drive-body-{cid}"' in card_html


def test_a_row_is_one_closed_line_naming_drive_provider_account_and_state():
    main, other = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)])
    for html, cid, name in ((main, "c-main", "Drive"), (other, "c-two", "Archive")):
        assert 'class="card dv-card"' in _opening_tag(html) and " open" not in _opening_tag(html)
        assert not _is_open(html, cid) and 'aria-expanded="false"' in _summary(html)
        summary = _summary(html)
        assert f'<span class="dv-sum-name">{name}</span>' in summary
        assert '<span class="dv-sum-sub">Provider · someone@example.com</span>' in summary
        assert re.search(r'<span class="dv-chip [a-z]+">T:platform\.connected</span>', summary)
        assert 'id="drive-name-' not in html                    # the settings sit below the summary, closed
    assert "T:drive.primary_badge" in _summary(main) and "T:drive.primary_badge" not in _summary(other)


def test_make_primary_is_a_setting_inside_the_row_not_on_the_line():
    opened = 'for (const id of ["c-main", "c-two"]) { driveUiOf(id).open = true; driveUiOf(id).tab = "settings"; }'
    main, other = _render([_row("c-main", "Drive", True), _row("c-two", "Archive", False)], opened)
    assert "makeDrivePrimary" not in _summary(other) and "makeDrivePrimary('c-two')" in other
    assert "makeDrivePrimary" not in main


def test_an_opened_row_stays_open_when_the_page_redraws():
    html, = _render([_row("c-two", "Archive", False)], 'driveUiOf("c-two").open = true;')
    assert " open" in _opening_tag(html) and _is_open(html, "c-two")
    assert "onclick=\"toggleDriveCard('c-two')\"" in _summary(html)
    # the summary's click is what opens it, and the state outlives a redraw
    html, = _render([_row("c-two", "Archive", False)], 'toggleDriveCard("c-two"); redrawDrive();')
    assert _is_open(html, "c-two")
    html, = _render([_row("c-two", "Archive", False)], 'toggleDriveCard("c-two"); toggleDriveCard("c-two"); redrawDrive();')
    assert not _is_open(html, "c-two")


def test_a_row_that_needs_a_reconnect_opens_by_itself_until_the_owner_closes_it():
    reconnect = {"credential_id": "cred-1"}
    html, = _render([_row("c-two", "Archive", False, reconnect)])
    assert " open" in _opening_tag(html) and _is_open(html, "c-two") and "T:drive.needs_reconnect" in _summary(html)
    closed, = _render([_row("c-two", "Archive", False, reconnect)], 'driveUiOf("c-two").open = false;')
    assert " open" not in _opening_tag(closed) and not _is_open(closed, "c-two")
    clicked, = _render([_row("c-two", "Archive", False, reconnect)], 'toggleDriveCard("c-two");')   # one click closes it
    assert not _is_open(clicked, "c-two")
