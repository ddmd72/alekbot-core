"""Adding or removing a passkey never opens an empty passkey sheet (CABINET_DEVICE_SESSIONS_RFC §7.3): a browser with no
local passkey marker goes straight to Google with a resume flag; one with the marker steps up first; the Cabinet resumes
the change after the Google sign-in. Runs `session.js` and the Cabinet functions under node (skipped without node)."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

_STATIC = Path(__file__).resolve().parents[3] / "src/web/static"
_HTML = (_STATIC / "cabinet.html").read_text(encoding="utf-8")
_SESSION = (_STATIC / "session.js").read_text(encoding="utf-8")
_NO_NODE = shutil.which("node") is None
pytestmark = pytest.mark.skipif(_NO_NODE, reason="node not installed")


def _function(name: str) -> str:
    match = re.search(r"\n(\s*(?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


def _node(script: str) -> dict:
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


# ---- the Cabinet flow, with a stubbed alekSession -------------------------------------------------------------------

_CABINET = """
  const log = {{ stepUps: 0, adds: 0, removes: 0, assigned: [], replaced: [], hint: false, toasts: [] }};
  const hintEl = {{ classList: {{ remove: () => {{ log.hint = true; }} }} }};
  const els = {{ "passkeys-resume-hint": hintEl, "section-devices": {{ classList: {{ contains: () => false }} }} }};
  const document = {{ getElementById: (id) => els[id] }};
  const t = (k) => k;
  const showToast = (m) => log.toasts.push(m);
  const rememberPasskeyOffer = () => {{}};
  const loadPasskeys = () => {{}};
  const API = "";
  const confirm = () => true;
  const addOutcomes = {adds};
  const addPasskey = async () => {{ log.adds++; return addOutcomes.shift(); }};
  const authFetch = async () => {{ log.removes++; return log.removes === 1
    ? {{ ok: false, clone: () => ({{}}) }} : {{ ok: true }}; }};
  const window = {{
    alekSession: {{ hasLocalPasskey: () => {marker}, needsPasskey: async () => true,
                   ensurePasskey: async () => {{ log.stepUps++; return {step_up}; }} }},
    location: {{ assign: (u) => log.assigned.push(u), search: {search}, pathname: "/cabinet" }},
    history: {{ replaceState: (a, b, u) => log.replaced.push(u) }},
  }};
  {functions}
  Promise.resolve({call}).then((outcome) => process.stdout.write(JSON.stringify({{ outcome, log }})));
"""


def _cabinet(call: str, marker="false", step_up="true", adds='["passkey_required", "added"]', search='""') -> dict:
    names = ("googleProofUrl", "stepUpIfLocal", "addPasskeyHere", "removePasskey", "resumePasskeyFlow")
    functions = "\n".join(_function(n) for n in names)
    script = _CABINET.format(call=call, marker=marker, step_up=step_up, adds=adds, search=search, functions=functions)
    return _node(script)


def test_no_marker_goes_straight_to_google_with_the_resume_flag_and_never_steps_up():
    out = _cabinet("addPasskeyHere()", marker="false")
    assert out["log"]["stepUps"] == 0 and out["outcome"] == "redirect"
    (target,) = out["log"]["assigned"]
    url = urlparse(target)
    assert url.path == "/auth/login/google"
    assert parse_qs(url.query) == {"next": ["/cabinet?tab=devices&resume=add_passkey"]}


def test_a_marker_steps_up_and_retries_once():
    out = _cabinet("addPasskeyHere()", marker="true", step_up="true")
    assert out["log"]["stepUps"] == 1 and out["log"]["adds"] == 2 and out["outcome"] == "added"
    assert out["log"]["assigned"] == []


def test_a_failed_step_up_goes_to_google():
    out = _cabinet("addPasskeyHere()", marker="true", step_up="false")
    assert out["log"]["stepUps"] == 1 and out["outcome"] == "redirect"
    assert out["log"]["assigned"][0].startswith("/auth/login/google?next=")


def test_remove_without_marker_goes_to_google_with_the_encoded_id():
    out = _cabinet("removePasskey('a/b+c=')", marker="false")
    assert out["log"]["stepUps"] == 0
    next_url = parse_qs(urlparse(out["log"]["assigned"][0]).query)["next"][0]
    assert next_url == "/cabinet?tab=devices&resume=remove_passkey&id=a%2Fb%2Bc%3D"


def test_remove_with_marker_steps_up_then_retries():
    out = _cabinet("removePasskey('k1')", marker="true", step_up="true")
    assert out["log"]["stepUps"] == 1 and out["log"]["removes"] == 2 and out["log"]["assigned"] == []


def test_the_resume_flag_starts_the_add_once_and_is_removed_from_the_url():
    out = _cabinet("resumePasskeyFlow(new URLSearchParams('tab=devices&resume=add_passkey'))",
                   search='"?tab=devices&resume=add_passkey"', adds='["added"]')
    assert out["log"]["adds"] == 1 and out["log"]["replaced"] == ["/cabinet?tab=devices"]
    assert out["log"]["assigned"] == []


def test_a_resumed_add_the_browser_refuses_shows_the_hint_and_does_not_loop_to_google():
    out = _cabinet("resumePasskeyFlow(new URLSearchParams('resume=add_passkey'))",
                   search='"?resume=add_passkey"', adds='["cancelled"]')
    assert out["log"]["hint"] is True and out["log"]["assigned"] == []


def test_a_resumed_remove_runs_without_a_second_confirm_and_clears_the_id():
    out = _cabinet("resumePasskeyFlow(new URLSearchParams('tab=devices&resume=remove_passkey&id=k1'))",
                   marker="true", search='"?tab=devices&resume=remove_passkey&id=k1"')
    assert out["log"]["removes"] == 2 and out["log"]["replaced"] == ["/cabinet?tab=devices"]


def test_no_flag_means_nothing_starts():
    out = _cabinet("resumePasskeyFlow(new URLSearchParams('tab=devices'))", search='"?tab=devices"')
    assert out["log"]["adds"] == 0 and out["log"]["replaced"] == []


# ---- session.js: the marker --------------------------------------------------------------------------------------

_SESSION_RUN = """
  const store = {store};
  const calls = {{ gets: 0 }};
  global.localStorage = {storage};
  global.window = {{ PublicKeyCredential: function () {{}}, addEventListener() {{}} }};
  global.document = {{ addEventListener() {{}}, visibilityState: "hidden" }};
  global.location = {{ pathname: "/cabinet", search: "", assign() {{}}, replace() {{}} }};
  global.atob = (s) => Buffer.from(s, "base64").toString("binary");
  global.btoa = (s) => Buffer.from(s, "binary").toString("base64");
  const cred = {{ id: "c", rawId: new ArrayBuffer(1), type: "public-key",
    response: {{ clientDataJSON: new ArrayBuffer(1), authenticatorData: new ArrayBuffer(1), signature: new ArrayBuffer(1) }} }};
  Object.defineProperty(globalThis, "navigator", {{ configurable: true, value: {{ credentials: {{ get: async () => {{ calls.gets++; {get_body} }} }} }} }});
  const body = {{ required: true, options: {{ challenge: "AA", allowCredentials: [] }} }};
  global.fetch = async (url) => ({{ status: 200, ok: true, json: async () => body }});
  {script}
  (async () => {{ const r = await {call}; process.stdout.write(JSON.stringify({{ r, store, gets: calls.gets }})); }})();
"""


def _session(call: str, store='{}', storage=None, get_body="return cred;") -> dict:
    storage = storage or ("{ getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, "
                          "removeItem: (k) => { delete store[k]; } }")
    return _node(_SESSION_RUN.format(store=store, storage=storage, get_body=get_body, script=_SESSION, call=call))


def test_a_successful_step_up_sets_the_marker():
    out = _session("window.alekSession.ensurePasskey()")
    assert out["r"] is True and out["store"] == {"alek.hasPasskey": "1"}


def test_a_not_allowed_step_up_clears_the_marker():
    out = _session("window.alekSession.ensurePasskey()", store='{"alek.hasPasskey": "1"}',
                   get_body='throw Object.assign(new Error("x"), { name: "NotAllowedError" });')
    assert out["r"] is False and out["store"] == {}


def test_storage_that_throws_means_no_marker_and_no_crash():
    boom = "{ getItem() { throw new Error('no'); }, setItem() { throw new Error('no'); }, removeItem() { throw new Error('no'); } }"
    out = _session("window.alekSession.hasLocalPasskey()", storage=boom)
    assert out["r"] is False
    assert _session("window.alekSession.ensurePasskey()", storage=boom)["r"] is True


def test_the_marker_is_read_back_by_has_local_passkey():
    assert _session("window.alekSession.hasLocalPasskey()", store='{"alek.hasPasskey": "1"}')["r"] is True
    assert _session("window.alekSession.hasLocalPasskey()")["r"] is False


def test_passkey_sign_in_page_sets_the_same_marker():
    assert '"alek.hasPasskey"' in (_STATIC / "sign_in.html").read_text(encoding="utf-8")
