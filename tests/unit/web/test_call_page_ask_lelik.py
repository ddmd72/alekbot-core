"""The "Ask Lelik" button of the interpreter call page (VOICE_CALL_CONTROL_CHANNEL_RFC §5.5): created by script only on
the interpreter page and only when the call hands out a control channel (so `/cabinet/call` is unchanged), disabled
until `welcome` offers `ask`, pressed on tap (`ask_arm {seq}`), cancelled by a second tap before speaking
(`ask_disarm`), busy while Lelik answers, released by the relay's `ask_state`; a tap the relay never answers rolls
back after 2 s. The static checks read the page; the behavioural ones run the page's control block and its Ask block
under node with a fake DOM (skipped when node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")
URL = "wss://relay.example.test/ctl"
TOKEN = "token-0001-invented"


def _block(start: str, end: str) -> str:
    match = re.search(re.escape(start) + r".*?" + re.escape(end), HTML, re.S)
    assert match, start
    return match.group(0)


def _control_block() -> str:
    match = re.search(r"      // Control channel to the relay.*?(?=\n      function stopStatusPolling)", HTML, re.S)
    assert match
    return match.group(0)


def _ask_block() -> str:
    return _block("      /* ── Ask Lelik", "onControl(onAskFrame);")


def _dict(lang: str) -> str:
    return _block(f"        {lang}: {{", "        },")


# ---------------------------------------------------------------- static

def test_the_page_markup_has_no_ask_button_and_the_script_makes_one_only_on_the_interpreter_page():
    body = HTML[HTML.index("<body>"):HTML.index("<script>", HTML.index("<body>"))]
    assert "ask" not in body.lower()
    assert 'if (window.VOICE_MODE !== "interpreter") return;' in _ask_block()


def test_the_button_is_shown_only_when_the_call_hands_out_the_control_channel_and_hidden_at_the_end():
    assert re.findall(r".*showAsk\(\);.*", HTML) == [
        "          if (wantsControlChannel() && connectBody.control) showAsk();"]
    end_call = re.search(r"async function endCall\(.*?\n      }\n", HTML, re.S).group(0)
    assert "hideAsk();" in end_call


@pytest.mark.parametrize("lang, label", [("en", "Ask Lelik"), ("uk", None), ("fr", None), ("es", None)])
def test_every_page_locale_has_the_three_ask_labels(lang, label):
    block = _dict(lang)
    for key in ("ask.button", "ask.armed", "ask.busy"):
        assert f'"{key}":' in block
    if label:
        assert f'"ask.button": "{label}"' in block


# ---------------------------------------------------------------- behaviour under node

_HARNESS = """
const window = { VOICE_MODE: %(mode)s };
const vibrations = [];
const navigator = { vibrate: (ms) => vibrations.push(ms) };
const t = (key) => key;
let callToken = 1; let inCall = true;
const timers = []; let nextId = 1;
const setTimeout = (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms, every: false }); return id; };
const setInterval = (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms, every: true }); return id; };
const clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
const clearInterval = clearTimeout;
const sockets = [];
class WebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.sent = []; this.readyState = 0; sockets.push(this); }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() { this.readyState = 3; }
  open() { this.readyState = 1; this.onopen(); }
  says(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  drop() { this.readyState = 3; this.onclose(); }
}
const created = [];
const controls = { children: [], append(el) { this.children.push(el); } };
const document = {
  createElement(tag) {
    const classes = new Set();
    const el = { tag, hidden: false, disabled: false, textContent: "", attrs: {}, listeners: {},
      classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), has: (c) => classes.has(c) },
      setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      click() { this.listeners.click(); } };
    el.classes = classes;
    created.push(el);
    return el;
  },
  querySelector: (sel) => (sel === ".controls" ? controls : null),
};
function view() {
  const b = ask.btn;
  if (!b) return { exists: false };
  return { exists: true, hidden: b.hidden, disabled: b.disabled, armed: b.classes.has("armed"),
           busy: b.classes.has("busy"), pressed: b.attrs["aria-pressed"], label: b.textContent };
}
function sentByPage(ws) { return ws.sent.filter((f) => f.type.startsWith("ask_")); }
function liveCall(features = ["ask"], state = "idle") {
  openControl({ url: %(url)s, token: %(token)s }, 1);
  showAsk();
  const ws = sockets[sockets.length - 1];
  ws.open();
  ws.says(Object.assign({ type: "welcome", v: 1, features }, features.includes("ask") ? { ask: state } : {}));
  return ws;
}
"""


def _run(body: str, mode="interpreter"):
    script = (_HARNESS % {"mode": json.dumps(mode), "url": json.dumps(URL), "token": json.dumps(TOKEN)}
              + _control_block() + "\n" + _ask_block() + "\n" + body + "\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_IDLE = {"exists": True, "hidden": False, "disabled": False, "armed": False, "busy": False, "pressed": "false",
         "label": "ask.button"}
_ARMED = {**_IDLE, "armed": True, "pressed": "true", "label": "ask.armed"}
_BUSY = {**_IDLE, "disabled": True, "busy": True, "label": "ask.busy"}
_DISABLED = {**_IDLE, "disabled": True}


@needs_node
def test_the_button_waits_disabled_for_welcome_and_is_enabled_when_the_call_offers_ask():
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        showAsk();
        const before = view();
        const ws = sockets[0];
        ws.open();
        ws.says({{ type: "welcome", v: 1, features: ["ask"], ask: "idle" }});
        console.log(JSON.stringify({{ before, after: view(), inControls: controls.children.length }}));
    """)
    assert out == {"before": _DISABLED, "after": _IDLE, "inControls": 1}


@needs_node
def test_a_call_that_offers_no_ask_keeps_the_button_disabled():
    out = _run("""
        const ws = liveCall([]);
        ask.btn.click();
        console.log(JSON.stringify({ view: view(), sent: sentByPage(ws) }));
    """)
    assert out == {"view": _DISABLED, "sent": []}


@needs_node
def test_the_main_call_page_never_gets_a_button():
    out = _run("""
        showAsk();
        onAskFrame({ type: "welcome", v: 1, features: ["ask"], ask: "idle" });
        console.log(JSON.stringify({ created: created.length, view: view() }));
    """, mode=None)
    assert out == {"created": 0, "view": {"exists": False}}


@needs_node
def test_a_press_arms_then_lelik_answers_and_the_button_releases():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        const pressed = view();
        ws.says({ type: "ask_state", state: "armed", seq: 1 });
        const confirmed = view();
        const pendingRollbacks = timers.filter((t) => !t.every && t.ms === 2000).length;
        ws.says({ type: "ask_state", state: "active" });
        const busy = view();
        ask.btn.click();                       // a tap while Lelik answers does nothing
        ws.says({ type: "ask_state", state: "idle", reason: "answered" });
        console.log(JSON.stringify({ sent: sentByPage(ws), pressed, confirmed, pendingRollbacks, busy,
                                     released: view(), vibrations }));
    """)
    assert out["sent"] == [{"type": "ask_arm", "seq": 1}]
    assert out["pressed"] == _ARMED and out["confirmed"] == _ARMED and out["pendingRollbacks"] == 0
    assert out["busy"] == _BUSY and out["released"] == _IDLE and out["vibrations"] == [30]


@needs_node
def test_pressing_again_before_speaking_cancels():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        ws.says({ type: "ask_state", state: "armed", seq: 1 });
        ask.btn.click();
        const cancelled = view();
        ws.says({ type: "ask_state", state: "idle", seq: 2, reason: "cancelled" });
        console.log(JSON.stringify({ sent: sentByPage(ws), cancelled, after: view() }));
    """)
    assert out["sent"] == [{"type": "ask_arm", "seq": 1}, {"type": "ask_disarm", "seq": 2}]
    assert out["cancelled"] == _IDLE and out["after"] == _IDLE


@needs_node
def test_a_press_the_relay_never_answers_rolls_back_after_two_seconds():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        const rollback = timers.find((t) => !t.every && t.ms === 2000);
        rollback.fn();
        console.log(JSON.stringify({ view: view() }));
    """)
    assert out == {"view": _IDLE}


@needs_node
def test_the_relays_timeout_release_returns_the_button_to_normal():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        ws.says({ type: "ask_state", state: "armed", seq: 1 });
        ws.says({ type: "ask_state", state: "idle", reason: "timeout" });
        console.log(JSON.stringify({ view: view() }));
    """)
    assert out == {"view": _IDLE}


@needs_node
def test_a_dropped_channel_disables_the_button_and_welcome_on_reconnect_restores_the_relays_state():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        ws.says({ type: "ask_state", state: "armed", seq: 1 });
        ws.drop();
        const down = view();
        ask.btn.click();
        timers.find((t) => !t.every && t.ms === 1000).fn();   // the control channel redials
        const again = sockets[sockets.length - 1];
        again.open();
        again.says({ type: "welcome", v: 1, features: ["ask"], ask: "armed" });
        console.log(JSON.stringify({ down, sentWhileDown: sentByPage(again), back: view() }));
    """)
    assert out == {"down": _DISABLED, "sentWhileDown": [], "back": _ARMED}


@needs_node
def test_the_call_ending_hides_the_button_and_a_late_frame_does_not_bring_it_back():
    out = _run("""
        const ws = liveCall();
        ask.btn.click();
        closeControl();
        hideAsk();
        onAskFrame({ type: "ask_state", state: "armed", seq: 1 });
        console.log(JSON.stringify({ view: view(), rollbacks: timers.filter((t) => t.ms === 2000).length }));
    """)
    assert out["view"]["hidden"] is True and out["rollbacks"] == 0
