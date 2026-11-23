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

STATIC = Path(__file__).resolve().parents[3] / "src" / "web" / "static"
SESSION_JS = (STATIC / "session.js").read_text(encoding="utf-8")
PAGES = ["cabinet.html", "call.html", "call_modes.html"]


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
    html = (STATIC / page).read_text(encoding="utf-8")
    assert '<script src="/auth/session.js"></script>' in html
    assert "window.alekSession" in html
    assert html.index("/auth/session.js") < html.index("window.alekSession")


@pytest.mark.parametrize("page", PAGES)
def test_page_has_no_direct_401_to_login_jump(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    assert "goToLogin" not in html
    for line in html.splitlines():
        if "/auth/login" in line:
            # The only login mentions left are plain sign-in links or the Cabinet's catch-all failure path.
            assert "401" not in line, line
    assert not re.search(r"status\s*===?\s*401[^}]{0,200}/auth/login", html, re.S)


@pytest.mark.parametrize("page", ["call.html", "call_modes.html"])
def test_call_pages_check_the_session_on_load(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    assert re.search(r"^\s*ensureSession\(\);", html, re.M)


@pytest.mark.parametrize("page", PAGES)
def test_authenticated_calls_go_through_authfetch(page):
    html = (STATIC / page).read_text(encoding="utf-8")
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
