"""Shared pieces for the call-page tests that run the page's own script under node with a fake DOM
(live text panel and Show view). The page is cut by start/end markers, wrapped in `FAKE_DOM` plus a small prelude,
run with `node -e`, and the script prints one JSON line. There is no jsdom and no browser."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[3] / "src" / "web" / "static"
HTML = (STATIC / "call.html").read_text(encoding="utf-8")
SESSION_JS = (STATIC / "session.js").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def session_prelude(html: str, *, binding: bool = True) -> str:
    """What a page has before its own blocks run: the real `session.js` (loaded by `<script src>` ahead of the inline
    script) evaluated over the harness's `window`, `document` and `fetch`, then the page's own `window.alekSession`
    binding line, so the blocks' `authFetch` is the real wrapper over the faked `fetch`. `binding=False` when the
    page's whole inline script runs (it carries that line itself). Needs `window` and `document.addEventListener`."""
    parts = [f"eval({json.dumps(SESSION_JS)});"]
    if binding:
        line = re.search(r"^\s*const \{[^}]*\} = window\.alekSession;$", html, re.M)
        assert line, "the page binds no window.alekSession"
        parts.append(line.group(0))
    return "\n".join(parts) + "\n"


def block(start: str, end: str) -> str:
    match = re.search(re.escape(start) + r".*?" + re.escape(end), HTML, re.S)
    assert match, start
    return match.group(0)


def live_text_block() -> str:
    return block("      /* ── Live text ──", "      onControl(onLiveTextFrame);")


def show_view_block() -> str | None:
    """The Show view block, or None while the page has none."""
    if "      /* ── Show view ──" not in HTML:
        return None
    return block("      /* ── Show view ──", "      showViewInit();")


def dictionary(lang: str) -> str:
    return block(f"        {lang}: {{", "        },")


FAKE_DOM = r"""
class FakeEl {
  constructor(tag, ns) {
    this.tag = tag; this.ns = ns || null; this.children = []; this.parentNode = null;
    this.attrs = {}; this.listeners = {}; this._text = ""; this.hidden = false; this.id = ""; this.type = "";
    this._cls = new Set(); this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.style = {};
    const self = this;
    this.classList = {
      add: (c) => self._cls.add(c), remove: (c) => self._cls.delete(c), contains: (c) => self._cls.has(c),
      toggle: (c, on) => { if (on === undefined) on = !self._cls.has(c); if (on) self._cls.add(c); else self._cls.delete(c); return on; },
    };
  }
  get className() { return [...this._cls].join(" "); }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v); }
  _adopt(n) { if (n.parentNode) n.remove(); n.parentNode = this; }
  append(...nodes) { nodes.forEach((n) => { this._adopt(n); this.children.push(n); }); }
  prepend(...nodes) { nodes.reverse().forEach((n) => { this._adopt(n); this.children.unshift(n); }); }
  insertBefore(node, ref) {
    this._adopt(node);
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
  }
  remove() {
    if (!this.parentNode) return;
    const sibs = this.parentNode.children; sibs.splice(sibs.indexOf(this), 1); this.parentNode = null;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "id") this.id = String(v);
    if (k === "class") this.className = v;
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, ev) { (this.listeners[type] || []).slice().forEach((fn) => fn(ev || { target: this })); }
  click() { this.fire("click"); }
  focus() { document.activeElement = this; }
  matches(sel) {
    if (sel[0] === "#") return this.id === sel.slice(1);
    return sel.split(".").filter(Boolean).every((c) => this._cls.has(c));
  }
  all(sel, out) { this.children.forEach((c) => { if (c.matches(sel)) out.push(c); c.all(sel, out); }); return out; }
  querySelector(sel) { return this.all(sel, [])[0] || null; }
  querySelectorAll(sel) { return this.all(sel, []); }
}
const document = {
  body: new FakeEl("body"), activeElement: null, listeners: {},
  createElement: (tag) => new FakeEl(tag),
  createElementNS: (ns, tag) => new FakeEl(tag, ns),
  querySelector(sel) { return this.body.querySelector(sel); },
  querySelectorAll(sel) { return this.body.querySelectorAll(sel); },
  getElementById(id) { return this.body.querySelector("#" + id); },
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
};
// The page's skeleton: .screen > .identity, .controls (> call button slot row on the interpreter page).
function buildSkeleton(withSlots) {
  const screen = document.createElement("main"); screen.className = "screen";
  const identity = document.createElement("div"); identity.className = "identity";
  const controls = document.createElement("div"); controls.className = "controls";
  screen.append(identity, controls); document.body.append(screen);
  if (withSlots) {
    const row = document.createElement("div"); row.className = "call-row";
    const left = document.createElement("div"); left.id = "call-slot-left"; left.className = "call-slot";
    const right = document.createElement("div"); right.id = "call-slot-right"; right.className = "call-slot";
    row.append(left, right); controls.append(row);
  }
  return screen;
}
const controlListeners = [];
function onControl(listener) { controlListeners.push(listener); }
function send(msg) { controlListeners.forEach((l) => l(msg)); }
let seqCounter = 0;
function line(id, who, op, text, extra) { return Object.assign({ v: 1, seq: ++seqCounter, type: "line", line: id, who, op, text }, extra || {}); }
function endLine(id, status) { return { v: 1, seq: ++seqCounter, type: "line", line: id, op: "end", status }; }
function snapshot(lines) { return { v: 1, seq: ++seqCounter, type: "snapshot", lines, state: "listening" }; }
const panelOf = () => document.querySelector(".text-panel");
const lineEls = () => document.querySelectorAll(".text-line");
"""


def page_script(mode: str | None, body: str, *, skeleton: bool = True) -> str:
    """The prelude, the page's live-text block (and the Show view block when the page has one), then `body`."""
    parts = [
        f"const window = {{ VOICE_MODE: {json.dumps(mode) if mode else 'undefined'} }};",
        "const t = (key) => key;",
        FAKE_DOM,
        session_prelude(HTML),
        f"const SCREEN = {'buildSkeleton(' + json.dumps(mode == 'interpreter') + ')' if skeleton else 'null'};",
        live_text_block(),
    ]
    show = show_view_block()
    if show:
        parts.append(show)
    parts.append(body)
    return "\n".join(parts)


def run_node(script: str) -> dict:
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])
