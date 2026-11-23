"""The Drives section shows a loading line while its first read runs, an error with Retry when that read fails, and
keeps a drawn card when a later read (re-open, the poll) fails. `loadDriveStatus` of `cabinet-sections.js` runs under
node against a small fake DOM; the script goes through a temp file (`node -e` would carry the whole text, and a Linux
argument is limited to ~128 KB). The call pages' back link goes to Home, not to the Drives section."""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.web.cabinet_source import STATIC

_STRINGS_JS = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
_SECTIONS_JS = (STATIC / "cabinet-sections.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_PRELUDE = r"""
const nodes = {};
const stub = (id) => (nodes[id] = nodes[id] || { id, innerHTML: "", classList: { add() {}, remove() {} } });
globalThis.document = { getElementById: (id) => stub(id), visibilityState: "visible", addEventListener() {},
  querySelector: () => null, querySelectorAll: () => [] };
const API = "";
let _activeSection = "drives";
let _currentLang = "en";
%(strings)s
function t(key) { return (LANG_DICT[_currentLang] || LANG_DICT["en"])[key] || key; }
function escapeHtml(str) { return String(str ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";"); }
function observeElements() {}
const rendered = [];
const _CONN = { connections: [{ connection_id: "c1", kind: "google_drive", name: "Main", primary: true }], mirror_root: null };
let gate = null;          // when set, the connections read waits for it
let failNext = false;
const authFetch = async (url) => {
  if (url.endsWith("/api/drive/connections")) {
    if (gate) await gate;
    if (failNext) return { ok: false, status: 500, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(_CONN)) };
  }
  return { ok: true, status: 200, json: async () => ({ enabled: false }) };
};
function loadConnectors() { return Promise.resolve([{ integration: "drive", kind: "google_drive" }]); }
%(sections)s
// A render under test: the real renderer needs the whole card; the probe records what it was given and draws a marker.
renderDriveStatus = function (data) { rendered.push(data.connections.map((c) => c.name)); nodes["drive-container"].innerHTML = "<div class=\"dv-list\">CARD</div>"; };
const box = () => nodes["drive-container"].innerHTML;
"""


def _run(tmp_path, body: str):
    # `renderDriveStatus` is a function declaration in the script; the probe re-assigns it after the script text.
    script = (_PRELUDE % {"strings": _STRINGS_JS, "sections": _SECTIONS_JS}) + "\n(async () => {\n" + body + "\n})();\n"
    path = tmp_path / "drives_loading_probe.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@_node
def test_the_loading_line_shows_before_the_read_resolves_and_success_replaces_it(tmp_path):
    out = _run(tmp_path, """
      let release; gate = new Promise((r) => (release = r));
      const pending = loadDriveStatus();
      await new Promise((r) => setTimeout(r, 0));
      const during = box();
      release(); await pending;
      process.stdout.write(JSON.stringify({ during, after: box(), rendered }));""")
    assert 'role="status"' in out["during"] and "Loading your drives" in out["during"]
    assert "Loading" not in out["after"] and "CARD" in out["after"]
    assert out["rendered"] == [["Main"]]


@_node
def test_a_failed_first_load_shows_the_error_and_retry_loads_again(tmp_path):
    out = _run(tmp_path, """
      failNext = true;
      await loadDriveStatus();
      const failed = box();
      failNext = false;
      // Retry is the button's handler: the markup says which function it calls
      const handler = /onclick="(\\w+)\\(\\)"/.exec(failed)[1];
      await eval(handler + "()");
      process.stdout.write(JSON.stringify({ failed, handler, after: box(), rendered }));""")
    assert 'role="alert"' in out["failed"] and "Could not load your drives." in out["failed"]
    assert 'id="drive-retry"' in out["failed"] and "Try again" in out["failed"]
    assert out["handler"] == "loadDriveStatus"
    assert "CARD" in out["after"] and "Could not load" not in out["after"]
    assert out["rendered"] == [["Main"]]


@_node
def test_a_failed_poll_after_a_success_keeps_the_card_and_shows_nothing_new(tmp_path):
    out = _run(tmp_path, """
      await loadDriveStatus();
      failNext = true;
      await loadDriveStatus();
      process.stdout.write(JSON.stringify({ after: box(), rendered }));""")
    assert out["after"] == '<div class="dv-list">CARD</div>'
    assert out["rendered"] == [["Main"]]


@_node
def test_a_reopen_with_the_card_drawn_shows_no_loading_line(tmp_path):
    out = _run(tmp_path, """
      await loadDriveStatus();
      let release; gate = new Promise((r) => (release = r));
      const pending = loadDriveStatus();
      await new Promise((r) => setTimeout(r, 0));
      const during = box();
      release(); await pending;
      process.stdout.write(JSON.stringify({ during }));""")
    assert out["during"] == '<div class="dv-list">CARD</div>'


# ------------------------------------------------------------------------------------------------ strings and markup

def _values(key: str) -> list:
    return re.findall(r'"' + re.escape(key) + r'": "([^"]+)"', _STRINGS_JS)


def test_the_three_strings_exist_in_the_four_languages_and_differ():
    for key in ("drives.loading", "drives.load_failed", "drives.retry"):
        assert len(_values(key)) == len(LANGS), key
        assert len(set(_values(key))) == len(LANGS), key


def test_the_loading_and_error_markup_carry_no_inline_style():
    block = _SECTIONS_JS[_SECTIONS_JS.index("function showDriveLoading"):_SECTIONS_JS.index("async function loadDriveStatus")]
    assert not re.search(r"\sstyle\s*=", block)


# ------------------------------------------------------------------------------------------------ the call pages' back link

@pytest.mark.parametrize("page", ["call.html", "call_modes.html"])
def test_a_call_pages_back_link_goes_to_home(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    assert re.findall(r'<a class="back-link" href="([^"]*)" id="back-link">', html) == ["/cabinet"]
