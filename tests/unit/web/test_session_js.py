"""The shared silent-refresh script (CABINET_DEVICE_SESSIONS_RFC §7.4): its route, its use by the three pages, and its
behaviour. The static checks read the files; the behavioural ones run `session.js` under node with a fake `fetch`,
`location` and `document` (skipped when node is not installed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.web.oauth_app import create_oauth_blueprint
from tests.unit.web.cabinet_source import cabinet_source

STATIC = Path(__file__).resolve().parents[3] / "src" / "web" / "static"
SESSION_JS = (STATIC / "session.js").read_text(encoding="utf-8")
PAGES = ["cabinet.html", "call.html", "call_modes.html"]


def _page(page):
    """A page's text; the Cabinet's is the shell with its assets in place."""
    return cabinet_source() if page == "cabinet.html" else (STATIC / page).read_text(encoding="utf-8")


# ---------------------------------------------------------------- route

async def test_route_serves_the_script_as_javascript_without_a_session():
    app = Quart(__name__)
    app.register_blueprint(create_oauth_blueprint(
        auth_service=MagicMock(), cabinet_sessions=MagicMock(), device_sessions=MagicMock(), auth_registry=MagicMock(),
        auth_config=MagicMock()))
    resp = await app.test_client().get("/auth/session.js")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/javascript")
    assert resp.headers["Cache-Control"] == "no-cache"
    assert (await resp.get_data(as_text=True)) == SESSION_JS


# ---------------------------------------------------------------- static

@pytest.mark.parametrize("page", PAGES)
def test_page_includes_the_script_and_uses_its_functions(page):
    html = _page(page)
    assert '<script src="/auth/session.js"></script>' in html
    assert "window.alekSession" in html
    assert html.index("/auth/session.js") < html.index("window.alekSession")


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_direct_401_to_login_jump(page):
    html = _page(page)
    assert "goToLogin" not in html
    for line in html.splitlines():
        if "/auth/login" in line:
            # The only login mentions left are plain sign-in links or the Cabinet's catch-all failure path.
            assert "401" not in line, line
    assert not re.search(r"status\s*===?\s*401[^}]{0,200}/auth/login", html, re.S)


@pytest.mark.parametrize("page", ["call.html", "call_modes.html"])
def test_call_pages_check_the_session_on_load(page):
    html = _page(page)
    assert re.search(r"^\s*ensureSession\(\);", html, re.M)


@pytest.mark.parametrize("page", PAGES)
def test_authenticated_calls_go_through_authfetch(page):
    html = _page(page)
    plain = re.findall(r"(?<![\w.])fetch\(([^)\n]*)", html)
    # The Cabinet's own logout is the one request that must not trigger a refresh.
    assert [p for p in plain if "/auth/logout" not in p] == []


def test_session_js_has_single_flight_refresh_visibility_hook_and_next_encoding():
    assert "refreshInFlight" in SESSION_JS
    assert '"/auth/refresh"' in SESSION_JS and '"POST"' in SESSION_JS
    assert 'addEventListener("visibilitychange"' in SESSION_JS
    assert 'encodeURIComponent(next)' in SESSION_JS and '"/auth/login?next="' in SESSION_JS
    assert "location.pathname + location.search" in SESSION_JS
    assert "EARLY_REFRESH_SECONDS = 120" in SESSION_JS


# ---------------------------------------------------------------- behaviour under node

_HARNESS = r"""
const log = [];
let script = %(script)s;
let now = 1000000;
Date.now = () => now * 1000;
const listeners = {};
global.document = { visibilityState: "visible", addEventListener: (n, f) => { listeners[n] = f; } };
global.location = { pathname: "/cabinet/call", search: "?m=1", replace: (u) => log.push("replace:" + u) };
global.window = {};
const respond = (status, body) => ({ status, json: async () => body });
const routes = %(routes)s;   // url -> list of statuses (or {status, body}); the last one repeats
const calls = {};
global.fetch = async (url, init) => {
  calls[url] = (calls[url] || 0) + 1;
  log.push((init && init.method || "GET") + " " + url);
  const seq = routes[url];
  const item = seq[Math.min(calls[url], seq.length) - 1];
  await new Promise((r) => setTimeout(r, 5));
  return typeof item === "number" ? respond(item, {}) : respond(item.status, item.body);
};
eval(script);
const { authFetch, ensureSession } = window.alekSession;
"""


def _run(routes: dict, body: str):
    script = _HARNESS % {"script": json.dumps(SESSION_JS), "routes": json.dumps(routes)} + body
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_a_401_refreshes_once_and_retries_the_request_once():
    out = _run({"/api/x": [401, 200], "/auth/refresh": [200]}, """
        authFetch("/api/x").then((r) => console.log(JSON.stringify({ status: r.status, log })));
    """)
    assert out["status"] == 200
    assert out["log"] == ["GET /api/x", "POST /auth/refresh", "GET /api/x"]


@needs_node
def test_concurrent_401s_share_one_refresh():
    out = _run({"/api/a": [401, 200], "/api/b": [401, 200], "/auth/refresh": [200]}, """
        Promise.all([authFetch("/api/a"), authFetch("/api/b")]).then((rs) =>
          console.log(JSON.stringify({ statuses: rs.map((r) => r.status), refreshes: calls["/auth/refresh"] })));
    """)
    assert out == {"statuses": [200, 200], "refreshes": 1}


@needs_node
def test_a_failed_refresh_goes_to_login_with_the_encoded_page_as_next():
    out = _run({"/api/x": [401], "/auth/refresh": [401]}, """
        let settled = false;
        authFetch("/api/x").then(() => { settled = true; });
        setTimeout(() => console.log(JSON.stringify({ log, settled })), 100);
    """)
    assert out["log"][-1] == "replace:/auth/login?next=%2Fcabinet%2Fcall%3Fm%3D1"
    assert out["settled"] is False


@needs_node
def test_a_401_after_a_successful_refresh_is_not_retried_again():
    out = _run({"/api/x": [401], "/auth/refresh": [200]}, """
        authFetch("/api/x");
        setTimeout(() => console.log(JSON.stringify({ log, n: calls["/api/x"] })), 100);
    """)
    assert out["n"] == 2
    assert out["log"][-1].startswith("replace:/auth/login?next=")


@needs_node
def test_non_401_responses_pass_through_without_a_refresh():
    out = _run({"/api/x": [500], "/auth/refresh": [200]}, """
        authFetch("/api/x").then((r) => console.log(JSON.stringify({ status: r.status, log })));
    """)
    assert out == {"status": 500, "log": ["GET /api/x"]}


@needs_node
def test_ensure_session_refreshes_when_me_is_401():
    out = _run({"/auth/me": [401], "/auth/refresh": [200]}, """
        ensureSession().then(() => console.log(JSON.stringify({ log })));
    """)
    assert out["log"] == ["GET /auth/me", "POST /auth/refresh"]


@needs_node
def test_ensure_session_refreshes_early_when_expiry_is_under_120_seconds():
    out = _run({"/auth/me": [{"status": 200, "body": {"expires_at": 1000000 + 119}}], "/auth/refresh": [200]}, """
        ensureSession().then(() => console.log(JSON.stringify({ log })));
    """)
    assert out["log"] == ["GET /auth/me", "POST /auth/refresh"]


@needs_node
def test_ensure_session_leaves_a_fresh_session_alone():
    out = _run({"/auth/me": [{"status": 200, "body": {"expires_at": 1000000 + 600}}], "/auth/refresh": [200]}, """
        ensureSession().then(() => console.log(JSON.stringify({ log })));
    """)
    assert out["log"] == ["GET /auth/me"]


@needs_node
def test_becoming_visible_runs_the_check_and_hidden_does_not():
    out = _run({"/auth/me": [{"status": 200, "body": {"expires_at": 1000000 + 600}}], "/auth/refresh": [200]}, """
        document.visibilityState = "hidden"; listeners.visibilitychange();
        document.visibilityState = "visible"; listeners.visibilitychange();
        setTimeout(() => console.log(JSON.stringify({ log })), 50);
    """)
    assert out["log"] == ["GET /auth/me"]


# ---------------------------------------------------------------- an unreachable refresh, and the passkey helpers

# The harness above, plus a refresh or request that never reaches the server ("error": fetch rejects, as offline),
# request bodies kept per url, and a fake WebAuthn (`navigator.credentials`) that answers or refuses.
_HARNESS_2 = r"""
const log = [];
let script = %(script)s;
let now = 1000000;
Date.now = () => now * 1000;
const listeners = {};
global.document = { visibilityState: "visible", addEventListener: (n, f) => { listeners[n] = f; } };
global.location = { pathname: "/cabinet/call", search: "", replace: (u) => log.push("replace:" + u) };
const webauthn = %(webauthn)s;   // null: no WebAuthn in this browser; else {get|create: "ok" | error name}
global.window = webauthn ? { PublicKeyCredential: function () {} } : {};
const bytes = (s) => new TextEncoder().encode(s).buffer;
const sheets = [];
const answer = (kind) => (arg) => {
  sheets.push({ kind, challenge: new TextDecoder().decode(arg.publicKey.challenge),
                user: arg.publicKey.user ? new TextDecoder().decode(arg.publicKey.user.id) : null,
                excluded: (arg.publicKey.excludeCredentials || []).map((c) => new TextDecoder().decode(c.id)) });
  const outcome = webauthn[kind];
  if (outcome !== "ok") { const e = new Error(outcome); e.name = outcome; return Promise.reject(e); }
  const response = kind === "create"
    ? { clientDataJSON: bytes("cd"), attestationObject: bytes("att"), getTransports: () => ["internal"] }
    : { clientDataJSON: bytes("cd"), authenticatorData: bytes("ad"), signature: bytes("sig"), userHandle: bytes("u1") };
  return Promise.resolve({ id: "cred-1", rawId: bytes("cred-1"), type: "public-key", response,
                           authenticatorAttachment: "platform" });
};
Object.defineProperty(globalThis, "navigator", { configurable: true, value: webauthn
  ? { credentials: { get: answer("get"), create: answer("create") } } : {} });
const respond = (status, body) => ({ status, json: async () => body });
const routes = %(routes)s;
const calls = {}; const bodies = {};
global.fetch = async (url, init) => {
  calls[url] = (calls[url] || 0) + 1;
  log.push((init && init.method || "GET") + " " + url);
  if (init && init.body) bodies[url] = JSON.parse(init.body);
  const seq = routes[url];
  const item = seq[Math.min(calls[url], seq.length) - 1];
  await new Promise((r) => setTimeout(r, 5));
  if (item === "error") throw new TypeError("Failed to fetch");
  return typeof item === "number" ? respond(item, {}) : respond(item.status, item.body);
};
eval(script);
const { authFetch, ensureSession, ensurePasskey, addPasskey } = window.alekSession;
"""

# base64url of the texts the fake WebAuthn returns, as the server receives them.
_B64 = {"cd": "Y2Q", "att": "YXR0", "ad": "YWQ", "sig": "c2ln", "u1": "dTE", "cred-1": "Y3JlZC0x"}
# A challenge "chal" and a user id "u1", base64url, as the server's options carry them.
_REQUEST = {"challenge": "Y2hhbA", "rpId": "cabinet.example.com", "allowCredentials": [], "userVerification": "required"}
_CREATION = {"challenge": "Y2hhbA", "rp": {"id": "cabinet.example.com", "name": "Alek"},
             "user": {"id": "dTE", "name": "alex@example.com", "displayName": "Alex"},
             "excludeCredentials": [{"id": "b2xk", "type": "public-key"}]}
STEP_UP, STEP_UP_VERIFY = "/api/user/passkeys/step-up/options", "/api/user/passkeys/step-up/verify"
REGISTER, REGISTER_VERIFY = "/api/user/passkeys/register/options", "/api/user/passkeys/register/verify"


def _run_2(routes: dict, body: str, webauthn=None):
    script = _HARNESS_2 % {"script": json.dumps(SESSION_JS), "routes": json.dumps(routes),
                           "webauthn": json.dumps(webauthn)} + body
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@needs_node
def test_an_unreachable_refresh_keeps_the_page_and_the_next_request_refreshes_again():
    out = _run_2({"/api/x": [401, 401, 200], "/auth/refresh": ["error", 200]}, """
        authFetch("/api/x").then((first) => authFetch("/api/x").then((second) =>
          console.log(JSON.stringify({ statuses: [first.status, second.status], log }))));
    """)
    assert out["statuses"] == [401, 200]                      # the first fails as offline, the next one recovers
    assert out["log"] == ["GET /api/x", "POST /auth/refresh", "GET /api/x", "POST /auth/refresh", "GET /api/x"]
    assert not any(line.startswith("replace:") for line in out["log"])


@needs_node
def test_an_unreachable_refresh_on_return_to_the_page_does_not_go_to_login():
    out = _run_2({"/auth/me": [401], "/auth/refresh": ["error"]}, """
        ensureSession().then(() => { listeners.visibilitychange(); setTimeout(() =>
          console.log(JSON.stringify({ log })), 50); });
    """)
    assert out["log"] == ["GET /auth/me", "POST /auth/refresh", "GET /auth/me", "POST /auth/refresh"]


@needs_node
@pytest.mark.parametrize("status", [401, 403, 500])
def test_any_refusal_of_the_refresh_ends_the_session(status):
    out = _run_2({"/auth/me": [{"status": 200, "body": {"expires_at": 1000000 + 10}}], "/auth/refresh": [status]}, """
        ensureSession().then(() => console.log(JSON.stringify({ log })));
    """)
    assert out["log"][-1] == "replace:/auth/login?next=%2Fcabinet%2Fcall"


@needs_node
def test_a_session_that_needs_no_passkey_runs_no_sheet():
    out = _run_2({STEP_UP: [{"status": 200, "body": {"required": False}}]}, """
        ensurePasskey().then((ok) => console.log(JSON.stringify({ ok, log, sheets })));
    """, webauthn={"get": "ok"})
    assert out == {"ok": True, "log": [f"POST {STEP_UP}"], "sheets": []}


@needs_node
def test_a_needed_passkey_runs_the_sheet_over_the_decoded_options_and_hands_back_the_assertion():
    out = _run_2({STEP_UP: [{"status": 200, "body": {"required": True, "options": _REQUEST}}],
                  STEP_UP_VERIFY: [200]}, """
        ensurePasskey().then((ok) => console.log(JSON.stringify({ ok, log, sheets, sent: bodies })));
    """, webauthn={"get": "ok"})
    assert out["ok"] is True and out["log"] == [f"POST {STEP_UP}", f"POST {STEP_UP_VERIFY}"]
    assert out["sheets"] == [{"kind": "get", "challenge": "chal", "user": None, "excluded": []}]
    assert out["sent"][STEP_UP_VERIFY] == {"credential": {
        "id": "cred-1", "rawId": _B64["cred-1"], "type": "public-key", "clientExtensionResults": {},
        "authenticatorAttachment": "platform",
        "response": {"clientDataJSON": _B64["cd"], "authenticatorData": _B64["ad"], "signature": _B64["sig"],
                     "userHandle": _B64["u1"]}}}


@needs_node
@pytest.mark.parametrize("routes, webauthn", [
    ({STEP_UP: [{"status": 200, "body": {"required": True, "options": _REQUEST}}]}, {"get": "NotAllowedError"}),
    ({STEP_UP: [{"status": 200, "body": {"required": True, "options": _REQUEST}}], STEP_UP_VERIFY: [400]},
     {"get": "ok"}),
    ({STEP_UP: [{"status": 200, "body": {"required": True, "options": _REQUEST}}]}, None),
    ({STEP_UP: [500]}, {"get": "ok"}),
    ({STEP_UP: ["error"]}, {"get": "ok"}),
], ids=["sheet-cancelled", "assertion-refused", "no-webauthn", "server-error", "offline"])
def test_a_passkey_that_could_not_be_given_resolves_false(routes, webauthn):
    out = _run_2(routes, """
        ensurePasskey().then((ok) => console.log(JSON.stringify({ ok, log })));
    """, webauthn=webauthn)
    assert out["ok"] is False
    assert not any(line.startswith("replace:") for line in out["log"])


@needs_node
def test_add_passkey_creates_over_the_decoded_options_and_sends_the_attestation():
    out = _run_2({REGISTER: [{"status": 200, "body": {"options": _CREATION}}], REGISTER_VERIFY: [201]}, """
        addPasskey().then((outcome) => console.log(JSON.stringify({ outcome, sheets, sent: bodies })));
    """, webauthn={"create": "ok"})
    assert out["outcome"] == "added"
    assert out["sheets"] == [{"kind": "create", "challenge": "chal", "user": "u1", "excluded": ["old"]}]
    assert out["sent"][REGISTER_VERIFY]["credential"]["response"] == {
        "clientDataJSON": _B64["cd"], "attestationObject": _B64["att"], "transports": ["internal"]}


@needs_node
@pytest.mark.parametrize("routes, webauthn, outcome", [
    ({REGISTER: [{"status": 200, "body": {"options": _CREATION}}]}, {"create": "InvalidStateError"}, "exists"),
    ({REGISTER: [{"status": 200, "body": {"options": _CREATION}}]}, {"create": "NotAllowedError"}, "cancelled"),
    ({REGISTER: [{"status": 200, "body": {"options": _CREATION}}], REGISTER_VERIFY: [400]}, {"create": "ok"},
     "failed"),
    ({REGISTER: [403]}, {"create": "ok"}, "failed"),
    ({REGISTER: [200]}, None, "failed"),
    ({REGISTER: ["error"]}, {"create": "ok"}, "failed"),
], ids=["already-in-this-keychain", "cancelled", "refused", "not-allowed", "no-webauthn", "offline"])
def test_add_passkey_names_what_happened(routes, webauthn, outcome):
    out = _run_2(routes, """
        addPasskey().then((outcome) => console.log(JSON.stringify({ outcome })));
    """, webauthn=webauthn)
    assert out["outcome"] == outcome


def test_the_call_page_runs_the_passkey_helper_before_its_media_and_refuses_on_a_stale_start():
    html = (STATIC / "call.html").read_text(encoding="utf-8")
    start = re.search(r"      async function startCall\(\) \{.*?\n      }\n", html, re.S).group(0)
    assert "const { authFetch, ensureSession, ensurePasskey } = window.alekSession;" in html
    assert start.index("startRingback();") < start.index("await ensurePasskey()") < start.index("getUserMedia")
    assert start.count("ensurePasskey(") == 1
    assert 'if (startResp.status === 403) {' in start and 'setState(t("state.passkey_needed"));' in start


@needs_node
@pytest.mark.parametrize("routes", [
    {REGISTER: [{"status": 400, "body": {"error": "passkey_required"}}]},
    {REGISTER: [{"status": 200, "body": {"options": _CREATION}}],
     REGISTER_VERIFY: [{"status": 400, "body": {"error": "passkey_required"}}]},
], ids=["at-options", "at-verify"])
def test_add_passkey_names_a_missing_fresh_assertion_so_the_page_can_step_up(routes):
    out = _run_2(routes, """
        addPasskey().then((outcome) => console.log(JSON.stringify({ outcome })));
    """, webauthn={"create": "ok"})
    assert out["outcome"] == "passkey_required"


@needs_node
@pytest.mark.parametrize("status, body, expected", [
    (400, {"error": "passkey_required"}, True), (400, {"error": "passkey_refused"}, False),
    (401, {"error": "passkey_required"}, False), (200, {}, False)])
def test_needs_passkey_reads_only_a_400_passkey_required(status, body, expected):
    out = _run_2({"/api/x": [{"status": status, "body": body}]}, """
        authFetch("/api/x").then((resp) => window.alekSession.needsPasskey(resp))
          .then((needed) => console.log(JSON.stringify({ needed })));
    """)
    assert out["needed"] is expected


def test_the_cabinet_steps_up_and_retries_once_when_adding_or_removing_needs_a_passkey():
    html = cabinet_source()
    add = re.search(r"async function addPasskeyHere\(\) \{.*?\n      }\n", html, re.S).group(0)
    remove = re.search(r"async function removePasskey\(id\) \{.*?\n      }\n", html, re.S).group(0)
    assert 'outcome === "passkey_required"' in add and "ensurePasskey()" in add and add.count("addPasskey()") == 2
    assert "needsPasskey(" in remove and "ensurePasskey()" in remove and remove.count("authFetch(") == 2
