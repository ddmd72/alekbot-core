"""Cabinet -> Devices (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6): the nav items, the section with the lost-phone checklist,
every string in all four languages, and `renderDevices` escaping a hostile label (run under node when installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (Path(__file__).resolve().parents[3] / "src/web/static/cabinet.html").read_text(encoding="utf-8")
_KEYS = ["nav.devices", "devices.title", "devices.subtitle", "devices.current", "devices.created", "devices.last_used",
         "devices.kind_google", "devices.logout", "devices.logout_others", "devices.logout_all", "devices.confirm_all",
         "devices.confirm_others", "devices.empty", "devices.failed", "devices.done", "devices.lost_title",
         "devices.lost_intro", "devices.lost_1", "devices.lost_2", "devices.lost_3"]


def _function(name: str) -> str:
    match = re.search(r"\n(\s*(?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


def test_nav_items_and_section_exist():
    assert "navigate('devices')" in _HTML and 'id="nav-devices"' in _HTML and 'id="nav-mobile-devices"' in _HTML
    assert 'id="section-devices"' in _HTML
    assert 'if (name === "devices") loadDevices();' in _HTML


@pytest.mark.parametrize("key", _KEYS)
def test_every_string_is_in_all_four_languages(key):
    assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4


def test_the_lost_phone_checklist_is_rendered_from_translated_lines():
    for element in ("devices-lost-title", "devices-lost-intro", "devices-lost-1", "devices-lost-2", "devices-lost-3"):
        assert f'id="{element}"' in _HTML and f'"#{element}"' in _HTML
    assert "$devices off" in re.search(r'"devices\.lost_1": "([^"]+)"', _HTML).group(1)


def test_the_buttons_call_the_three_logouts():
    assert "logoutOtherDevices()" in _HTML and "logoutAllDevices()" in _HTML and "logoutDevice(" in _HTML
    for path in ("logout-others", "logout-all", "/logout"):
        assert path in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_hostile_label_and_hint_are_escaped_in_the_rendered_row():
    hostile = '<img src=x onerror="alert(1)">'
    devices = [
        {"sid": 'a"b', "label": hostile, "platform_hint": hostile, "auth_kind": "google", "is_current": True,
         "created_at": "2026-11-20T10:00:00+00:00", "last_seen_at": "2026-11-21T10:00:00+00:00"},
        {"sid": "s2", "label": "Mac — Chrome", "platform_hint": "macOS", "auth_kind": "passkey", "is_current": False,
         "created_at": "2026-11-20T10:00:00+00:00", "last_seen_at": "2026-11-21T10:00:00+00:00"}]
    script = f"""
      const els = {{"devices-list": {{innerHTML: ""}}}};
      const document = {{getElementById: (id) => els[id]}};
      const t = (k) => k;
      {_function("escapeHtml")}
      {_function("renderDevices")}
      renderDevices({json.dumps(devices)});
      const first = els["devices-list"].innerHTML;
      renderDevices([]);
      process.stdout.write(JSON.stringify([first, els["devices-list"].innerHTML]));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    html, empty = json.loads(out)
    assert "<img" not in html and 'onerror="' not in html and "&#60;img" in html
    assert 'data-sid="a&#34;b"' in html
    assert "devices.current" in html and "Mac — Chrome" in html and "passkey" in html
    assert "devices.empty" in empty
