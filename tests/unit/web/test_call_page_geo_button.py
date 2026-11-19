"""The `geo` button of the call page (GEO_MARKS_RFC §4.7): created by script, shown only while the call's
`welcome.features` lists `geo`, takes one fix with `getCurrentPosition` on tap (never the Permissions API) and sends it
as a `geo` frame, shows ✓ and the address (or "address unavailable") from `geo_state`, or the error. The static checks
read the page; the behavioural ones run the page's control block and Geo block under node with a fake DOM."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")
URL = "wss://relay.example.test/ctl"
TOKEN = "token-0001-invented"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _block(start: str, end: str) -> str:
    match = re.search(re.escape(start) + r".*?" + re.escape(end), HTML, re.S)
    assert match, start
    return match.group(0)


def _control_block() -> str:
    match = re.search(r"      // Control channel to the relay.*?(?=\n      function stopStatusPolling)", HTML, re.S)
    assert match
    return match.group(0)


def _geo_block() -> str:
    return _block("      /* ── Geo ──", "onControl(onGeoFrame);")


# ---------------------------------------------------------------- static

def test_the_markup_has_no_geo_button_and_it_is_shown_only_for_a_call_with_a_control_channel():
    body = HTML[HTML.index("<body>"):HTML.index("<script>", HTML.index("<body>"))]
    assert "geo" not in body.lower()
    assert re.findall(r".*showGeo\(\);.*", HTML) == ["          if (connectBody.control) showGeo();"]
    end_call = re.search(r"async function endCall\(.*?\n      }\n", HTML, re.S).group(0)
    assert "hideGeo();" in end_call


def test_the_page_never_uses_the_permissions_api():
    assert "navigator.permissions" not in HTML
    assert ".permissions" not in _geo_block()


def test_the_position_is_taken_fresh_and_with_high_accuracy_within_fifteen_seconds():
    block = _geo_block()
    assert "const GEO_TIMEOUT_MS = 15000;" in block
    assert "{ enableHighAccuracy: true, timeout: GEO_TIMEOUT_MS, maximumAge: 0 }" in block


@pytest.mark.parametrize("lang", ["en", "uk", "fr", "es"])
def test_every_page_locale_has_the_geo_strings(lang):
    block = _block(f"        {lang}: {{", "        },")
    for key in ("geo.button", "geo.locating", "geo.no_address", "geo.denied", "geo.unavailable", "geo.insecure",
                "geo.rate", "geo.failed"):
        assert f'"{key}":' in block


# ---------------------------------------------------------------- behaviour under node

_HARNESS = """
const navigator = %(navigator)s;
const t = (key) => key;
let callToken = 1; let inCall = true;
const setTimeout = () => 0; const setInterval = () => 0; const clearTimeout = () => {}; const clearInterval = () => {};
const sockets = [];
class WebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.sent = []; this.readyState = 0; sockets.push(this); }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen(); }
  says(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
}
const created = [];
const controls = { children: [], append(el) { this.children.push(el); } };
const document = {
  createElement(tag) {
    const el = { tag, hidden: false, disabled: false, textContent: "", attrs: {}, listeners: {}, className: "",
      setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      click() { this.listeners.click(); } };
    created.push(el);
    return el;
  },
  querySelector: (sel) => (sel === ".controls" ? controls : null),
};
const positionCalls = [];
function view() {
  if (!geo.btn) return { exists: false };
  return { exists: true, hidden: geo.btn.hidden, disabled: geo.btn.disabled, label: geo.btn.textContent,
           status: geo.status.hidden ? null : geo.status.textContent };
}
function geoFramesSent(ws) { return ws.sent.filter((f) => f.type === "geo"); }
function liveCall(features) {
  openControl({ url: %(url)s, token: %(token)s }, 1);
  showGeo();
  const ws = sockets[sockets.length - 1];
  ws.open();
  ws.says({ type: "welcome", v: 1, features });
  return ws;
}
"""

_FIXES = """{
  geolocation: { getCurrentPosition(ok, err, options) { positionCalls.push({ ok, err, options }); } } }"""


def _run(body: str, navigator: str = _FIXES):
    script = (_HARNESS % {"navigator": navigator, "url": json.dumps(URL), "token": json.dumps(TOKEN)}
              + _control_block() + "\n" + _geo_block() + "\n" + body + "\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@needs_node
def test_the_button_is_hidden_until_welcome_lists_geo_and_never_for_a_call_without_it():
    out = _run("""
        openControl({ url: "%s", token: "%s" }, 1);
        showGeo();
        const before = view();
        const ws = sockets[0];
        ws.open();
        ws.says({ type: "welcome", v: 1, features: ["text"] });
        const without = view();
        ws.says({ type: "welcome", v: 1, features: ["geo", "text"] });
        console.log(JSON.stringify({ before, without, after: view() }));
    """ % (URL, TOKEN))
    assert out["before"]["hidden"] is True and out["without"]["hidden"] is True
    assert out["after"] == {"exists": True, "hidden": False, "disabled": False, "label": "geo.button", "status": None}


@needs_node
def test_a_tap_asks_the_browser_once_and_sends_the_fix_as_a_geo_frame():
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        const locating = view();
        geo.btn.click();                                 // a tap while locating does nothing
        positionCalls[0].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 40 } });
        console.log(JSON.stringify({ locating, asked: positionCalls.length, options: positionCalls[0].options,
                                     sent: geoFramesSent(ws), after: view() }));
    """)
    assert out["locating"]["disabled"] is True and out["locating"]["label"] == "geo.locating"
    assert out["asked"] == 1
    assert out["options"] == {"enableHighAccuracy": True, "timeout": 15000, "maximumAge": 0}
    assert out["sent"] == [{"type": "geo", "seq": 1, "latitude": 41.1, "longitude": -8.6, "accuracy": 40}]
    assert out["after"]["disabled"] is True  # until the relay answers


@needs_node
def test_geo_state_shows_the_address_or_that_none_is_available():
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        positionCalls[0].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 40 } });
        ws.says({ type: "geo_state", ok: true, seq: 1, address: "12 Oak Street, Springfield" });
        const withAddress = view();
        geo.btn.click();
        positionCalls[1].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 250 } });
        ws.says({ type: "geo_state", ok: true, seq: 2 });
        console.log(JSON.stringify({ withAddress, withoutAddress: view() }));
    """)
    assert out["withAddress"] == {"exists": True, "hidden": False, "disabled": False, "label": "geo.button",
                                  "status": "✓ 12 Oak Street, Springfield"}
    assert out["withoutAddress"]["status"] == "✓ geo.no_address" and out["withoutAddress"]["disabled"] is False


@needs_node
def test_a_refusal_and_a_stale_answer():
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        positionCalls[0].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 40 } });
        ws.says({ type: "geo_state", ok: true, seq: 7, address: "Main Street, Springfield" });   // not ours
        const stale = view();
        ws.says({ type: "geo_state", ok: false, seq: 1, reason: "rate" });
        const rate = view();
        geo.btn.click();
        positionCalls[1].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 40 } });
        ws.says({ type: "geo_state", ok: false, seq: 2, reason: "bad" });
        console.log(JSON.stringify({ stale, rate, bad: view() }));
    """)
    assert out["stale"]["disabled"] is True and out["stale"]["status"] is None
    assert out["rate"]["status"] == "geo.rate" and out["rate"]["disabled"] is False
    assert out["bad"]["status"] == "geo.failed"


@needs_node
@pytest.mark.parametrize("code, key", [(1, "geo.denied"), (2, "geo.unavailable"), (3, "geo.unavailable")])
def test_a_browser_error_shows_its_message_and_sends_nothing(code, key):
    out = _run(f"""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        positionCalls[0].err({{ code: {code} }});
        console.log(JSON.stringify({{ state: view(), sent: geoFramesSent(ws) }}));
    """)
    assert out["state"]["status"] == key and out["state"]["disabled"] is False and out["sent"] == []


@needs_node
def test_a_browser_without_geolocation_says_so_and_a_fix_after_the_socket_dropped_fails():
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        console.log(JSON.stringify({ state: view(), asked: positionCalls.length }));
    """, navigator="{}")
    assert out["state"]["status"] == "geo.insecure" and out["asked"] == 0
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        ws.readyState = 3;
        positionCalls[0].ok({ coords: { latitude: 41.1, longitude: -8.6, accuracy: 40 } });
        console.log(JSON.stringify({ state: view() }));
    """)
    assert out["state"]["status"] == "geo.failed" and out["state"]["disabled"] is False


@needs_node
def test_losing_the_channel_or_the_end_of_the_call_hides_the_button():
    out = _run("""
        const ws = liveCall(["geo"]);
        geo.btn.click();
        ws.says({ type: "bye", reason: "call_ended" });
        const afterBye = view();
        const ws2 = liveCall(["geo"]);
        const shown = view();
        hideGeo();
        console.log(JSON.stringify({ afterBye, shown, hidden: view() }));
    """)
    assert out["afterBye"]["hidden"] is True and out["afterBye"]["status"] is None
    assert out["shown"]["hidden"] is False and out["hidden"]["hidden"] is True
