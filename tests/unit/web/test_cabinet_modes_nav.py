"""Cabinet nav carries a Modes item next to Call, desktop and mobile."""
import re
from pathlib import Path

_HTML = (Path(__file__).resolve().parents[3] / "src/web/static/cabinet.html").read_text(encoding="utf-8")


def _href(item_id: str) -> str:
    m = re.search(r"location\.href='([^']+)'\"\s+id=\"" + re.escape(item_id) + '"', _HTML)
    assert m, item_id
    return m.group(1)


def test_modes_items_point_to_modes_page():
    assert _href("nav-modes") == "/cabinet/call/modes"
    assert _href("nav-mobile-modes") == "/cabinet/call/modes"


def test_call_items_still_point_to_call():
    assert _href("nav-call") == "/cabinet/call"
    assert _href("nav-mobile-call") == "/cabinet/call"


def test_modes_label_localized_in_every_language():
    assert len(re.findall(r'"nav\.modes": "', _HTML)) == len(re.findall(r'"nav\.call": "', _HTML)) == 4
    assert '"#nav-modes"' in _HTML and '"#nav-mobile-modes .tab-label"' in _HTML
