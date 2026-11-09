"""The call page's `listening` (VOICE_CALL_CONTROL_CHANNEL_RFC §4.4): every web page opens the control channel when
`/connect` hands it out, whatever the mode; the page sends `listening` once per call, as soon as its `/renegotiate` is
done and the channel is welcomed (either order), never again on a reconnect; the "Ask Lelik" button stays
interpreter-only. The static checks read the page; the behavioural ones run the page's control and Ask blocks under
node with fakes (skipped when node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")
URL = "wss://relay.example.test/ctl"
TOKEN = "token-91f4c2aa-invented"


def _block(pattern: str) -> str:
    match = re.search(pattern, HTML, re.S)
    assert match, pattern
    return match.group(0)


def _control_block() -> str:
    return _block(r"      // Control channel to the relay.*?(?=\n      function stopStatusPolling)")


def _ask_block() -> str:
    return _block(r"      /\* ── Ask Lelik.*?onControl\(onAskFrame\);")


def _start_call() -> str:
    return _block(r"      async function startCall\(\) \{.*?\n      }\n")


# ---------------------------------------------------------------- static

def test_the_channel_is_opened_on_every_page_when_connect_hands_it_out():
    start = _start_call()
    opens = re.findall(r".*openControl\(connectBody.*", start)
    assert len(opens) == 1 and "VOICE_MODE" not in opens[0] and "connectBody.control" in opens[0]
    assert "wantsControlChannel" not in HTML


def test_listening_is_signalled_after_renegotiate_and_after_the_channel_is_opened():
    start = _start_call()
    renegotiate = start.index("/renegotiate`")
    opened = start.index("openControl(connectBody.control, myToken)")
    listening = start.index("controlListening(myToken);")
    assert renegotiate < listening and opened < listening
    assert "await controlListening" not in start                 # never blocks the call flow


def test_the_ask_button_stays_interpreter_only():
    assert 'if (window.VOICE_MODE !== "interpreter") return;' in _block(r"function showAsk\(\) \{.*?\n      }")


# ---------------------------------------------------------------- behaviour under node

_HARNESS = """
const window = { VOICE_MODE: %(mode)s };
const navigator = {};
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
    const el = { tag, hidden: false, disabled: false, textContent: "", attrs: {}, listeners: {},
      classList: { toggle: () => {} }, setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(type, fn) { this.listeners[type] = fn; } };
    created.push(el);
    return el;
  },
  querySelector: (sel) => (sel === ".controls" ? controls : null),
};
function types(ws) { return ws.sent.map((f) => f.type).filter((type) => type !== "ping"); }
function welcome(ws, features = []) { ws.says({ type: "welcome", v: 1, features }); }
"""


def _run(body: str, mode="interpreter"):
    script = (_HARNESS % {"mode": json.dumps(mode)} + _control_block() + "\n" + _ask_block() + "\n"
              + f"const HANDOUT = {{ url: {json.dumps(URL)}, token: {json.dumps(TOKEN)} }};\n" + body + "\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_welcome_first_then_renegotiate_sends_listening_once_renegotiate_is_done():
    out = _run("""
        openControl(HANDOUT, 1);
        const ws = sockets[0];
        ws.open();
        welcome(ws);
        const beforeRenegotiate = types(ws);
        controlListening(1);
        console.log(JSON.stringify({ beforeRenegotiate, after: types(ws) }));
    """)
    assert out == {"beforeRenegotiate": ["hello"], "after": ["hello", "listening"]}


@needs_node
def test_renegotiate_first_then_welcome_sends_listening_at_the_welcome():
    out = _run("""
        openControl(HANDOUT, 1);
        const ws = sockets[0];
        controlListening(1);                 // the socket is not even open yet
        ws.open();
        const beforeWelcome = types(ws);
        welcome(ws);
        console.log(JSON.stringify({ beforeWelcome, after: types(ws), frame: ws.sent[1] }));
    """)
    assert out == {"beforeWelcome": ["hello"], "after": ["hello", "listening"], "frame": {"type": "listening"}}


@needs_node
def test_a_reconnect_after_listening_was_sent_does_not_send_it_again():
    out = _run("""
        openControl(HANDOUT, 1);
        sockets[0].open();
        welcome(sockets[0]);
        controlListening(1);
        controlListening(1);                 // a second signal changes nothing
        sockets[0].drop();
        timers.find((t) => !t.every && t.ms === 1000).fn();
        const again = sockets[1];
        again.open();
        welcome(again);
        console.log(JSON.stringify({ first: types(sockets[0]), again: types(again) }));
    """)
    assert out == {"first": ["hello", "listening"], "again": ["hello"]}


@needs_node
def test_a_drop_before_any_welcome_sends_listening_on_the_first_welcome_of_the_redial():
    out = _run("""
        openControl(HANDOUT, 1);
        sockets[0].open();
        controlListening(1);
        sockets[0].drop();                   // never welcomed
        timers.find((t) => !t.every && t.ms === 1000).fn();
        const again = sockets[1];
        again.open();
        welcome(again);
        console.log(JSON.stringify({ first: types(sockets[0]), again: types(again) }));
    """)
    assert out == {"first": ["hello"], "again": ["hello", "listening"]}


@needs_node
def test_a_drop_after_welcome_but_before_renegotiate_waits_for_the_next_welcome():
    out = _run("""
        openControl(HANDOUT, 1);
        sockets[0].open();
        welcome(sockets[0]);
        sockets[0].drop();
        controlListening(1);                 // the channel is down: nothing goes out
        timers.find((t) => !t.every && t.ms === 1000).fn();
        const again = sockets[1];
        again.open();
        const beforeWelcome = types(again);
        welcome(again);
        console.log(JSON.stringify({ first: types(sockets[0]), beforeWelcome, again: types(again) }));
    """)
    assert out == {"first": ["hello"], "beforeWelcome": ["hello"], "again": ["hello", "listening"]}


@needs_node
def test_a_superseded_or_channel_less_call_sends_nothing():
    out = _run("""
        controlListening(1);                 // no channel handed out: a no-op
        openControl(HANDOUT, 1);
        sockets[0].open();
        welcome(sockets[0]);
        controlListening(2);                 // a stale continuation of another call
        console.log(JSON.stringify({ sent: types(sockets[0]) }));
    """)
    assert out == {"sent": ["hello"]}


@needs_node
@pytest.mark.parametrize("mode", [None, "tutor", "interpreter"])
def test_every_mode_sends_listening_and_only_the_interpreter_page_gets_the_ask_button(mode):
    out = _run("""
        openControl(HANDOUT, 1);
        showAsk();
        const ws = sockets[0];
        ws.open();
        welcome(ws, window.VOICE_MODE === "interpreter" ? ["ask"] : []);
        controlListening(1);
        console.log(JSON.stringify({ sent: types(ws), buttons: created.length }));
    """, mode=mode)
    assert out == {"sent": ["hello", "listening"], "buttons": 1 if mode == "interpreter" else 0}
