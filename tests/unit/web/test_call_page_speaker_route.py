"""The "Speaker" button of the interpreter call page (decisions/lelik_voice_modes.md, "Speaker switch"): created by
script only on the interpreter page, shown only while the call is live with a loudspeaker and a headphones output
present, ON routes the remote track through a WebAudio bridge into a hidden <audio> steered by setSinkId (WebKit ignores
setSinkId on a remote WebRTC track), OFF and every failure return to the direct path, and every switch sends one `route`
client report. The static checks read the page; the behavioural ones run the page's Speaker block under node with a fake
DOM (skipped when node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")
SPEAKER_ID = "out-speaker-0001"
PHONES_ID = "out-phones-0001"


def _block(start: str, end: str) -> str:
    match = re.search(re.escape(start) + r".*?" + re.escape(end), HTML, re.S)
    assert match, start
    return match.group(0)


def _speaker_block() -> str:
    return _block("      /* ── Speaker route", "      speakerInit();")


def _dict(lang: str) -> str:
    return _block(f"        {lang}: {{", "        },")


# ---------------------------------------------------------------- static

def test_the_page_markup_has_no_speaker_button_or_row():
    body = HTML[HTML.index("<body>"):HTML.index("<script>", HTML.index("<body>"))]
    for word in ("speaker", "call-row", "call-slot", "call-mid"):
        assert word not in body.lower()
    assert '<button id="call-btn" class="call-btn" aria-labelledby="call-label">' in body


def test_the_row_and_the_button_are_gated_on_the_interpreter_mode():
    block = _speaker_block()
    gate = 'window.VOICE_MODE !== "interpreter"'
    for fn in ("function buildSpeakerRow() {", "function showSpeaker() {", "function speakerInit() {"):
        assert gate in block[block.index(fn):block.index("\n      }\n", block.index(fn))], fn


@pytest.mark.parametrize("lang, label", [("en", "Speaker"), ("uk", "Динамік"), ("fr", "Haut-parleur"), ("es", "Altavoz")])
def test_every_page_locale_has_the_speaker_label(lang, label):
    assert f'"route.speaker": "{label}"' in _dict(lang)


def test_the_right_slot_is_reserved_and_the_default_is_one_named_constant():
    block = _speaker_block()
    assert 'right.id = "call-slot-right";' in block and "VOICE_CALL_LIVE_TEXT_RFC" in block
    assert len(re.findall(r"const SPEAKER_DEFAULT_ON = false;", HTML)) == 1
    assert ".call-slot:empty {\n        visibility: hidden;" in HTML


def test_the_page_starts_the_speaker_when_live_and_tears_it_down_at_the_end_of_the_call():
    assert len(re.findall(r"\bshowSpeaker\(\);", HTML)) == 1
    assert 'renderButton("live");\n          showSpeaker();' in HTML
    end_call = re.search(r"async function endCall\(.*?\n      }\n", HTML, re.S).group(0)
    assert "hideSpeaker();" in end_call


def test_ontrack_only_remembers_the_track_for_the_bridge():
    handler = re.search(r"pc\.ontrack = \(ev\) => \{.*?\n          \};", HTML, re.S).group(0)
    assert "speaker.track = ev.track;" in handler and "muted" not in handler


def test_only_the_speaker_block_touches_the_mute_of_the_remote_audio():
    outside = HTML.replace(_speaker_block(), "")
    assert not re.search(r"remoteAudio\s*\.\s*muted\s*=(?!=)", outside)


def test_the_route_report_sends_the_documented_fields_through_the_existing_report():
    fn = re.search(r"async function sendClientReport.*?\n      }\n", HTML, re.S).group(0)
    for key in ("route", "ok", "err", "outs", "ctx_state"):
        assert f'put("{key}"' in fn
    assert 'sendClientReport("route", callToken, { route, ok, err, outs: speaker.outs })' in _speaker_block()


def test_the_button_style_follows_the_page_tokens_and_reduced_motion():
    css = _block("      .speaker-btn {", "      @media (prefers-reduced-motion: reduce) {\n        .speaker-btn")
    for needle in ("width: 64px;", "var(--bg-panel-solid)", "1px solid var(--border-light)", "var(--shadow-md)",
                   "var(--text-main)", "touch-action: manipulation;", "-webkit-tap-highlight-color: transparent;",
                   "background: var(--accent);", "scale(0.94)"):
        assert needle in css, needle
    assert "prefers-reduced-motion" in HTML[HTML.index(".speaker-btn svg"):]


# ---------------------------------------------------------------- behaviour under node

_HARNESS = """
const window = { VOICE_MODE: %(mode)s };
const t = (key) => key;
let callToken = 1;
const reports = [];
function sendClientReport(stage, token, extra) { reports.push(Object.assign({ stage, token }, extra)); }
class MediaStream { constructor(tracks) { this.tracks = tracks; } }
let made = 0;
function makeEl(tag) {
  const classes = new Set();
  const el = { tag, children: [], parentNode: null, attrs: {}, listeners: {}, hidden: false, textContent: "",
    classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), has: (c) => classes.has(c) },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    append(...kids) { for (const k of kids) { if (k.parentNode) k.parentNode.children.splice(k.parentNode.children.indexOf(k), 1); k.parentNode = this; this.children.push(k); } },
    insertBefore(node, ref) { node.parentNode = this; this.children.splice(this.children.indexOf(ref), 0, node); },
    click() { return this.listeners.click(); } };
  return el;
}
const flags = { failSink: null, failPlay: null, hold: null };
const events = [];
function makeAudio() {
  const el = makeEl("audio");
  el.muted = false; el.paused = true; el.srcObject = null; el.sinkCalls = []; el.playCalls = 0;
  el.setSinkId = async (id) => {
    events.push("sink:" + id); el.sinkCalls.push(id);
    if (flags.hold) await flags.hold;
    if (flags.failSink) { const e = new Error("x"); e.name = flags.failSink; throw e; }
  };
  el.play = async () => {
    events.push("play"); el.playCalls++;
    if (flags.failPlay) { const e = new Error("x"); e.name = flags.failPlay; throw e; }
    el.paused = false;
  };
  el.pause = () => { el.paused = true; };
  return el;
}
const controls = makeEl("div");
const btn = makeEl("button"); btn.id = "call-btn";
const callLabel = makeEl("div"); callLabel.id = "call-label";
controls.append(btn, callLabel);
const bodyEl = makeEl("body");
const document = {
  body: bodyEl,
  createElement(tag) { made++; return tag === "audio" ? makeAudio() : makeEl(tag); },
};
const remoteAudio = { _muted: false };
Object.defineProperty(remoteAudio, "muted", { get() { return this._muted; }, set(v) { this._muted = v; events.push("mute:" + v); } });
const fakeCtx = {
  state: "running", listeners: [], resumeCalls: 0, stuck: false, sources: [],
  resume() { this.resumeCalls++; if (!this.stuck) this.state = "running"; return Promise.resolve(); },
  createMediaStreamSource(ms) { const s = { ms, disconnected: false, connect(d) { this.to = d; }, disconnect() { this.disconnected = true; } }; this.sources.push(s); return s; },
  createMediaStreamDestination() { return { stream: { id: "dest-stream" } }; },
  addEventListener(type, fn) { this.listeners.push(fn); },
  removeEventListener(type, fn) { this.listeners = this.listeners.filter((f) => f !== fn); },
  fire(state) { this.state = state; this.listeners.slice().forEach((f) => f()); },
};
let ctxAvailable = true;
function ensureAudioContext() { if (!ctxAvailable) return null; if (fakeCtx.state !== "running") fakeCtx.resume(); return fakeCtx; }
const dev = (kind, deviceId, label) => ({ kind, deviceId, label });
const DEVICES = [
  dev("audioinput", "in-mic-0001", "Microphone"),
  dev("audiooutput", "default", "Default - Headphones"),
  dev("audiooutput", "%(phones)s", "Headphones"),
  dev("audiooutput", "%(speaker)s", "Speaker"),
  dev("audiooutput", "out-receiver-0001", "Receiver"),
];
const navigator = { mediaDevices: { devices: DEVICES.slice(), handlers: [],
  enumerateDevices() { return Promise.resolve(this.devices); },
  addEventListener(type, fn) { if (type === "devicechange") this.handlers.push(fn); },
  fire() { return Promise.all(this.handlers.map((f) => f())); } } };
const HTMLMediaElement = { prototype: %(proto)s };
const without = (...ids) => navigator.mediaDevices.devices.filter((d) => !ids.includes(d.deviceId));
function view() {
  return { visible: !speaker.ctl.classList.has("off"), pressed: speaker.btn.attrs["aria-pressed"],
           direct: remoteAudio.muted, bridgeMuted: speaker.el ? speaker.el.muted : null };
}
async function live() { speaker.track = { id: "remote-track" }; await showSpeaker(); }
"""

_SRC = """
(async () => {
%s
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(body: str, mode="interpreter", setsink=True):
    script = (_HARNESS % {"mode": json.dumps(mode), "phones": PHONES_ID, "speaker": SPEAKER_ID,
                          "proto": "{ setSinkId() {} }" if setsink else "{}"}
              + _speaker_block() + "\n" + _SRC % body)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_OFF_VISIBLE = {"visible": True, "pressed": "false", "direct": False, "bridgeMuted": None}


@needs_node
def test_the_interpreter_page_wraps_the_call_button_into_a_row_with_a_left_slot_and_a_reserved_right_slot():
    out = _run("""
        const row = controls.children[0];
        const [left, mid, right] = row.children;
        console.log(JSON.stringify({ controls: controls.children.length, rowClass: row.className,
          left: left.id, mid: mid.children.map((c) => c.id), right: right.id, rightEmpty: right.children.length === 0,
          leftHolds: left.children[0] === speaker.ctl, hidden: !view().visible }));
    """)
    assert out == {"controls": 1, "rowClass": "call-row", "left": "call-slot-left", "mid": ["call-btn", "call-label"],
                   "right": "call-slot-right", "rightEmpty": True, "leftHolds": True, "hidden": True}


@needs_node
def test_the_main_call_page_never_gets_the_row_or_the_button():
    out = _run("""
        speakerInit();
        await live();
        await navigator.mediaDevices.fire();
        hideSpeaker();
        console.log(JSON.stringify({ made, controls: controls.children.map((c) => c.id),
                                     handlers: navigator.mediaDevices.handlers.length, row: speaker.row }));
    """, mode=None)
    assert out == {"made": 0, "controls": ["call-btn", "call-label"], "handlers": 0, "row": None}


@needs_node
def test_it_stays_hidden_before_the_call_is_live():
    out = _run("""
        await navigator.mediaDevices.fire();
        console.log(JSON.stringify(view()));
    """)
    assert out["visible"] is False


@needs_node
def test_it_is_visible_while_live_with_a_loudspeaker_and_headphones_and_defaults_to_off():
    out = _run("""
        await live();
        console.log(JSON.stringify({ view: view(), outs: speaker.outs, label: speaker.label.textContent,
                                     btnType: speaker.btn.type }));
    """)
    assert out == {"view": _OFF_VISIBLE, "outs": 4, "label": "route.speaker", "btnType": "button"}


@needs_node
@pytest.mark.parametrize("hidden_ids", [
    pytest.param(f'"{PHONES_ID}"', id="no-headphones"),
    pytest.param(f'"{SPEAKER_ID}"', id="no-loudspeaker"),
    pytest.param(f'"{PHONES_ID}", "{SPEAKER_ID}"', id="neither"),
])
def test_it_is_hidden_without_headphones_or_without_a_loudspeaker(hidden_ids):
    out = _run(f"""
        navigator.mediaDevices.devices = without({hidden_ids});
        await live();
        console.log(JSON.stringify(view()));
    """)
    assert out["visible"] is False


@needs_node
def test_an_earpiece_or_the_default_entry_alone_is_not_headphones():
    out = _run(f"""
        navigator.mediaDevices.devices = without("{PHONES_ID}");
        await live();
        console.log(JSON.stringify({{ visible: view().visible, phones: speaker.phonesId }}));
    """)
    assert out == {"visible": False, "phones": None}


@needs_node
def test_it_is_hidden_when_set_sink_id_is_unsupported():
    out = _run("""
        await live();
        console.log(JSON.stringify({ view: view(), live: speaker.live }));
    """, setsink=False)
    assert out["view"]["visible"] is False and out["live"] is False


@needs_node
def test_a_tap_plays_the_bridge_on_the_loudspeaker_and_only_then_mutes_the_direct_path():
    out = _run(f"""
        await live();
        await speaker.btn.click();
        const el = speaker.el;
        console.log(JSON.stringify({{ view: view(), events, sink: el.sinkCalls, plays: el.playCalls,
          playsinline: el.attrs.playsinline === "", inBody: bodyEl.children.includes(el),
          stream: el.srcObject.id, tapped: fakeCtx.sources[0].ms.tracks[0].id, connected: !!fakeCtx.sources[0].to }}));
    """)
    assert out["view"] == {"visible": True, "pressed": "true", "direct": True, "bridgeMuted": False}
    assert out["events"] == [f"sink:{SPEAKER_ID}", "play", "mute:true"]
    assert out["sink"] == [SPEAKER_ID] and out["plays"] == 1 and out["playsinline"] and out["inBody"]
    assert out["stream"] == "dest-stream" and out["tapped"] == "remote-track" and out["connected"]


@needs_node
def test_a_tap_with_a_suspended_context_resumes_it_inside_the_gesture():
    out = _run("""
        await live();
        fakeCtx.state = "interrupted";
        await speaker.btn.click();
        console.log(JSON.stringify({ view: view(), resumes: fakeCtx.resumeCalls }));
    """)
    assert out["view"]["pressed"] == "true" and out["resumes"] >= 1


@needs_node
def test_a_second_tap_unmutes_the_direct_path_and_silences_the_bridge():
    out = _run("""
        await live();
        await speaker.btn.click();
        await speaker.btn.click();
        console.log(JSON.stringify({ view: view(), paused: speaker.el.paused }));
    """)
    assert out["view"] == {"visible": True, "pressed": "false", "direct": False, "bridgeMuted": True}
    assert out["paused"] is True


@needs_node
def test_a_third_tap_reuses_the_graph():
    out = _run("""
        await live();
        for (let i = 0; i < 3; i++) await speaker.btn.click();
        console.log(JSON.stringify({ view: view(), sources: fakeCtx.sources.length, plays: speaker.el.playCalls }));
    """)
    assert out["view"]["pressed"] == "true" and out["view"]["direct"] is True
    assert out["sources"] == 1 and out["plays"] == 2


@needs_node
@pytest.mark.parametrize("flag", ["failSink", "failPlay"])
def test_a_rejection_leaves_the_voice_on_the_direct_path(flag):
    out = _run(f"""
        flags.{flag} = "NotAllowedError";
        await live();
        await speaker.btn.click();
        console.log(JSON.stringify({{ view: view(), report: reports[reports.length - 1] }}));
    """)
    assert out["view"] == {"visible": True, "pressed": "false", "direct": False, "bridgeMuted": True}
    assert out["report"] == {"stage": "route", "token": 1, "route": "phones", "ok": False, "err": "NotAllowedError",
                             "outs": 4}


@needs_node
def test_a_context_that_does_not_run_after_the_resume_keeps_the_direct_path_and_builds_no_bridge():
    out = _run("""
        await live();
        fakeCtx.state = "suspended"; fakeCtx.stuck = true;
        await speaker.btn.click();
        console.log(JSON.stringify({ view: view(), bridge: speaker.el === null, report: reports[reports.length - 1] }));
    """)
    assert out["view"] == {"visible": True, "pressed": "false", "direct": False, "bridgeMuted": None}
    assert out["bridge"] is True and out["report"]["ok"] is False and out["report"]["route"] == "phones"


@needs_node
def test_a_missing_audio_context_keeps_the_direct_path():
    out = _run("""
        await live();
        ctxAvailable = false;
        await speaker.btn.click();
        console.log(JSON.stringify(view()));
    """)
    assert out == {**_OFF_VISIBLE}


@needs_node
@pytest.mark.parametrize("state", ["suspended", "interrupted", "closed"])
def test_the_context_stopping_while_on_returns_to_the_direct_path(state):
    out = _run(f"""
        await live();
        await speaker.btn.click();
        fakeCtx.fire("{state}");
        console.log(JSON.stringify({{ view: view(), report: reports[reports.length - 1] }}));
    """)
    assert out["view"] == {"visible": True, "pressed": "false", "direct": False, "bridgeMuted": True}
    assert out["report"]["route"] == "phones" and out["report"]["ok"] is False and out["report"]["err"] == f"ctx_{state}"


@needs_node
def test_a_context_state_change_to_running_while_on_changes_nothing():
    out = _run("""
        await live();
        await speaker.btn.click();
        fakeCtx.fire("running");
        console.log(JSON.stringify(view()));
    """)
    assert out["pressed"] == "true" and out["direct"] is True


@needs_node
@pytest.mark.parametrize("gone", [PHONES_ID, SPEAKER_ID])
def test_losing_the_headphones_or_the_loudspeaker_while_on_returns_to_the_direct_path_and_hides_the_button(gone):
    out = _run(f"""
        await live();
        await speaker.btn.click();
        navigator.mediaDevices.devices = without("{gone}");
        await navigator.mediaDevices.fire();
        console.log(JSON.stringify(view()));
    """)
    assert out == {"visible": False, "pressed": "false", "direct": False, "bridgeMuted": True}


@needs_node
def test_headphones_connecting_during_the_call_reveal_the_button():
    out = _run(f"""
        navigator.mediaDevices.devices = without("{PHONES_ID}");
        await live();
        const before = view().visible;
        navigator.mediaDevices.devices = DEVICES.slice();
        await navigator.mediaDevices.fire();
        console.log(JSON.stringify({{ before, after: view().visible }}));
    """)
    assert out == {"before": False, "after": True}


@needs_node
def test_the_end_of_the_call_tears_the_bridge_down_and_a_late_devicechange_does_not_show_it():
    out = _run("""
        await live();
        await speaker.btn.click();
        const el = speaker.el; const src = fakeCtx.sources[0];
        hideSpeaker();
        const ended = view();
        await navigator.mediaDevices.fire();
        console.log(JSON.stringify({ ended, late: view(), disconnected: src.disconnected, paused: el.paused,
          srcObject: el.srcObject, listeners: fakeCtx.listeners.length, on: speaker.on, track: speaker.track }));
    """)
    assert out["ended"] == {"visible": False, "pressed": "false", "direct": False, "bridgeMuted": True}
    assert out["late"] == out["ended"]
    assert out["disconnected"] and out["paused"] and out["srcObject"] is None
    assert out["listeners"] == 0 and out["on"] is False and out["track"] is None


@needs_node
def test_a_tap_still_waiting_when_the_call_ends_never_mutes_the_direct_path():
    out = _run("""
        let release; flags.hold = new Promise((r) => { release = r; });
        await live();
        const tap = speaker.btn.click();
        hideSpeaker();
        release();
        await tap;
        console.log(JSON.stringify({ direct: remoteAudio.muted, on: speaker.on, plays: speaker.el.playCalls,
                                     reports: reports.length }));
    """)
    assert out == {"direct": False, "on": False, "plays": 0, "reports": 0}


@needs_node
def test_a_new_call_after_the_end_builds_a_fresh_graph_from_the_new_track():
    out = _run("""
        await live();
        await speaker.btn.click();
        hideSpeaker();
        await live();
        await speaker.btn.click();
        console.log(JSON.stringify({ view: view(), sources: fakeCtx.sources.length }));
    """)
    assert out["view"]["pressed"] == "true" and out["sources"] == 2


@needs_node
def test_a_hidden_button_does_nothing_when_tapped():
    out = _run(f"""
        navigator.mediaDevices.devices = without("{PHONES_ID}");
        await live();
        await speaker.btn.click();
        console.log(JSON.stringify({{ made, events, reports: reports.length }}));
    """)
    assert out["events"] == [] and out["reports"] == 0


@needs_node
def test_one_route_report_per_switch_with_the_documented_fields():
    out = _run("""
        await live();
        await speaker.btn.click();
        const afterOn = reports.slice();
        await speaker.btn.click();
        console.log(JSON.stringify({ afterOn, all: reports }));
    """)
    on = {"stage": "route", "token": 1, "route": "speaker", "ok": True, "err": "", "outs": 4}
    assert out["afterOn"] == [on]
    assert out["all"] == [on, {**on, "route": "phones"}]
