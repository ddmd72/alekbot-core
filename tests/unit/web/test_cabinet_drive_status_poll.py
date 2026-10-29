"""The Cabinet drive card re-polls rarely and quietly while the first build runs."""
import re
from pathlib import Path

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")


def _poll_block() -> str:
    return re.search(r"function scheduleDriveStatusPoll.*?\n      }\n", _HTML, re.S).group(0)


def test_poll_interval_is_two_minutes():
    assert "const DRIVE_STATUS_POLL_MS = 120000;" in _HTML


def test_poll_runs_only_while_visible_and_on_the_integrations_section():
    block = _poll_block()
    assert 'document.visibilityState === "visible"' in block
    assert '_activeSection === "integrations"' in block


def test_poll_starts_only_while_the_card_shows_building():
    assert "scheduleDriveStatusPoll(!!(data.indexing && data.indexing.building))" in _HTML
    assert "if (!building) return;" in _poll_block()


def test_render_only_when_the_data_changed():
    assert "snapshot !== _driveStatusSnapshot" in _HTML


def test_leaving_the_section_clears_the_timer():
    nav = re.search(r"function navigate\(name\) \{(.*?)const sec", _HTML, re.S).group(1)
    assert 'name !== "integrations"' in nav and "clearTimeout(_driveStatusTimer)" in nav
