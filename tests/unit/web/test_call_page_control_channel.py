"""The call page's control socket (VOICE_CALL_CONTROL_CHANNEL_RFC §4.2, §4.4): every web page opens it, whatever its
mode, only when `/connect` hands out `control`; the token is the first frame and never in the URL; pings every 20 s after
`welcome`; redials with backoff on a drop, never after `bye`; closed with the call. The static checks read the page;
the behavioural ones run the page's control block under node (skipped when node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")
URL = "wss://relay.example.test/ctl"
TOKEN = "token-0001-invented"


def _control_block() -> str:
    block = re.search(r"      // Control channel to the relay.*?(?=\n      function stopStatusPolling)", HTML, re.S)
    assert block
    return block.group(0)


def test_every_page_opens_the_socket_and_only_with_a_hand_out():
    assert "VOICE_MODE" not in _control_block()
    calls = re.findall(r".*openControl\(connectBody.*", HTML)
    assert calls == ['          if (connectBody.control) openControl(connectBody.control, myToken);']
    assert HTML.count("new WebSocket(") == 1 and "new WebSocket(ch.url)" in HTML


def test_the_call_end_closes_the_socket():
    end_call = re.search(r"async function endCall\(.*?\n      }\n", HTML, re.S).group(0)
    assert "closeControl();" in end_call


def test_the_main_page_markup_is_unchanged_by_the_channel():
    # v1 adds no element and no style: the block is script only, and creates no DOM
    block = _control_block()
    assert "document." not in block and "innerHTML" not in block and "classList" not in block


def _run(body: str, mode="interpreter"):
    script = (
        f"const window = {{ VOICE_MODE: {json.dumps(mode)} }};\n"
        "let callToken = 1; let inCall = true;\n"
        "const timers = []; let nextId = 1;\n"
        "const setTimeout = (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms, every: false }); return id; };\n"
        "const setInterval = (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms, every: true }); return id; };\n"
        "const clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };\n"
        "const clearInterval = clearTimeout;\n"
        "const sockets = [];\n"
        "class WebSocket {\n"
        "  static OPEN = 1;\n"
        "  constructor(url) { this.url = url; this.sent = []; this.readyState = 0; this.closed = false; sockets.push(this); }\n"
        "  send(text) { this.sent.push(JSON.parse(text)); }\n"
        "  close() { this.closed = true; }\n"
        "  open() { this.readyState = 1; this.onopen(); }\n"
        "  says(msg) { this.onmessage({ data: JSON.stringify(msg) }); }\n"
        "  drop() { this.readyState = 3; this.onclose(); }\n"
        "}\n"
        + _control_block()
        + "\n" + body + "\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_hello_with_the_token_is_the_first_frame_and_pings_follow_welcome():
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        const ws = sockets[0];
        ws.open();
        const beforeWelcome = timers.filter((t) => t.every).length;
        ws.says({{ type: "welcome", v: 1, features: [] }});
        const ping = timers.find((t) => t.every);
        ping.fn();
        console.log(JSON.stringify({{ url: ws.url, sent: ws.sent, beforeWelcome, pingMs: ping.ms,
                                      features: control.features }}));
    """)
    assert out["url"] == URL and TOKEN not in out["url"]
    assert out["sent"] == [{"type": "hello", "v": 1, "token": TOKEN}, {"type": "ping"}]
    assert out["beforeWelcome"] == 0 and out["pingMs"] == 20000 and out["features"] == []


@needs_node
def test_a_drop_redials_with_backoff_and_welcome_resets_it():
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        const delays = [];
        for (let i = 0; i < 6; i++) {{
          sockets[sockets.length - 1].drop();
          const retry = timers.pop();
          delays.push(retry.ms);
          retry.fn();
        }}
        const ws = sockets[sockets.length - 1];
        ws.open();
        ws.says({{ type: "welcome", v: 1, features: [] }});
        ws.drop();
        console.log(JSON.stringify({{ delays, afterWelcome: timers.filter((t) => !t.every).map((t) => t.ms),
                                      dials: sockets.length }}));
    """)
    assert out["delays"] == [1000, 2000, 4000, 8000, 10000, 10000]
    assert out["afterWelcome"] == [1000] and out["dials"] == 7


@needs_node
@pytest.mark.parametrize("reason", ["auth", "version", "call_ended", "replaced"])
def test_bye_stops_the_redials(reason):
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        const ws = sockets[0];
        ws.open();
        ws.says({{ type: "bye", reason: "{reason}" }});
        ws.drop();
        console.log(JSON.stringify({{ pending: timers.length, dials: sockets.length }}));
    """)
    assert out == {"pending": 0, "dials": 1}


@needs_node
def test_closing_with_the_call_closes_the_socket_and_cancels_everything():
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        const ws = sockets[0];
        ws.open();
        ws.says({{ type: "welcome", v: 1, features: [] }});
        closeControl();
        ws.drop();
        console.log(JSON.stringify({{ closed: ws.closed, pending: timers.length, dials: sockets.length,
                                      control }}));
    """)
    assert out == {"closed": True, "pending": 0, "dials": 1, "control": None}


@needs_node
def test_no_redial_once_the_call_is_over_or_superseded():
    out = _run(f"""
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        inCall = false;
        sockets[0].drop();
        const afterEnd = timers.length;
        inCall = true;
        openControl({{ url: "{URL}", token: "{TOKEN}" }}, 1);
        callToken = 2;                      // a newer call owns the page
        sockets[1].drop();
        console.log(JSON.stringify({{ afterEnd, afterSupersede: timers.length, dials: sockets.length }}));
    """)
    assert out == {"afterEnd": 0, "afterSupersede": 0, "dials": 2}


@needs_node
def test_a_hand_out_without_url_or_token_opens_nothing():
    out = _run("""
        openControl({ url: "wss://relay.example.test/ctl" }, 1);
        openControl(null, 1);
        console.log(JSON.stringify({ dials: sockets.length }));
    """)
    assert out == {"dials": 0}
