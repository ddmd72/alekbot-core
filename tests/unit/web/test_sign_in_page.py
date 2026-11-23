"""The sign-in page `sign_in.html` (CABINET_DEVICE_SESSIONS_RFC §7.3): the passkey path first, Google second, and
every way the passkey path can fail falls through to Google. The page's own script runs under node with a fake DOM,
`fetch`, `location` and WebAuthn (skipped when node is not installed); its route is tested in
`test_device_auth_passkeys.py`."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "sign_in.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_HARNESS = r"""
const log = [];
const els = {};
["title", "lead", "passkey", "google"].forEach((id) => {
  els[id] = { id, textContent: "", disabled: false, attrs: {}, listeners: {},
              setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.listeners[t] = f; } };
});
global.document = { documentElement: { lang: "en" }, title: "", getElementById: (id) => els[id] };
global.location = { search: %(search)s, replace: (u) => log.push("replace:" + u) };
const webauthn = %(webauthn)s;
global.window = webauthn ? { PublicKeyCredential: function () {} } : {};
const bytes = (s) => new TextEncoder().encode(s).buffer;
let asked = null;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
  language: %(lang)s, languages: [%(lang)s],
  credentials: webauthn ? { get: (arg) => {
    asked = new TextDecoder().decode(arg.publicKey.challenge);
    if (webauthn !== "ok") { const e = new Error(webauthn); e.name = webauthn; return Promise.reject(e); }
    return Promise.resolve({ id: "cred-1", rawId: bytes("cred-1"), type: "public-key", authenticatorAttachment: "platform",
      response: { clientDataJSON: bytes("cd"), authenticatorData: bytes("ad"), signature: bytes("sig"), userHandle: null } });
  } } : undefined } });
const routes = %(routes)s;
const bodies = {};
global.fetch = async (url, init) => {
  log.push(init.method + " " + url);
  bodies[url] = JSON.parse(init.body);
  const item = routes[url];
  if (item === "error") throw new TypeError("Failed to fetch");
  return { status: item.status, json: async () => item.body };
};
%(page)s
const done = () => console.log(JSON.stringify({ log, bodies, asked, google: els.google.attrs.href,
  texts: { title: els.title.textContent, passkey: els.passkey.textContent, google: els.google.textContent },
  lang: document.documentElement.lang, listening: !!els.passkey.listeners.click }));
if (els.passkey.listeners.click && %(click)s) { els.passkey.listeners.click(); setTimeout(done, 50); } else done();
"""

OPTIONS = {"status": 200, "body": {"options": {"challenge": "Y2hhbA", "rpId": "cabinet.example.com",
                                               "allowCredentials": [], "userVerification": "required"}}}


def _run(search="?next=%2Fcabinet%2Fcall", webauthn="ok", routes=None, click=True, lang="en-US"):
    script = _HARNESS % {"search": json.dumps(search), "webauthn": json.dumps(webauthn), "lang": json.dumps(lang),
                         "routes": json.dumps(routes or {}), "page": SCRIPT, "click": json.dumps(click)}
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def test_the_passkey_button_comes_before_the_google_link():
    assert HTML.index('id="passkey"') < HTML.index('id="google"')
    assert 'href="/auth/login/google"' in HTML
    assert "innerHTML" not in SCRIPT                       # every text is set as text


@needs_node
def test_a_passkey_sign_in_posts_the_assertion_with_next_and_goes_where_the_server_says():
    out = _run(routes={"/auth/passkey/options": OPTIONS,
                       "/auth/passkey/verify": {"status": 200, "body": {"next": "/cabinet/call"}}})
    assert out["log"] == ["POST /auth/passkey/options", "POST /auth/passkey/verify", "replace:/cabinet/call"]
    assert out["asked"] == "chal"
    sent = out["bodies"]["/auth/passkey/verify"]
    assert sent["next"] == "/cabinet/call" and sent["credential"]["rawId"] == "Y3JlZC0x"
    assert sent["credential"]["response"] == {"clientDataJSON": "Y2Q", "authenticatorData": "YWQ", "signature": "c2ln"}


@needs_node
def test_the_google_link_carries_the_pages_own_query():
    out = _run(click=False)
    assert out["google"] == "/auth/login/google?next=%2Fcabinet%2Fcall" and out["log"] == []


@needs_node
@pytest.mark.parametrize("webauthn, routes", [
    ("NotAllowedError", {"/auth/passkey/options": OPTIONS}),                                     # cancelled / none here
    ("ok", {"/auth/passkey/options": OPTIONS, "/auth/passkey/verify": {"status": 401, "body": {}}}),   # refused
    ("ok", {"/auth/passkey/options": OPTIONS, "/auth/passkey/verify": {"status": 403, "body": {}}}),   # inactive user
    ("ok", {"/auth/passkey/options": {"status": 501, "body": {}}}),
    ("ok", {"/auth/passkey/options": "error"}),
], ids=["cancelled-or-no-passkey", "refused", "access-denied", "not-configured", "offline"])
def test_every_failure_of_the_passkey_path_falls_through_to_google(webauthn, routes):
    out = _run(webauthn=webauthn, routes=routes)
    assert out["log"][-1] == "replace:/auth/login/google?next=%2Fcabinet%2Fcall"


@needs_node
@pytest.mark.parametrize("search, webauthn", [("?next=%2Fcabinet", None), ("?next=%2Fjoin&code=INV-1", "ok")],
                         ids=["no-webauthn", "an-invite"])
def test_a_browser_without_webauthn_or_an_invite_goes_straight_to_google(search, webauthn):
    out = _run(search=search, webauthn=webauthn, click=False)
    assert out["log"] == [f"replace:/auth/login/google{search}"] and out["listening"] is False


@needs_node
@pytest.mark.parametrize("lang, expect", [("uk-UA", "uk"), ("fr-FR", "fr"), ("es-ES", "es"), ("de-DE", "en")])
def test_the_texts_follow_the_browser_language_with_english_as_fallback(lang, expect):
    out, english = _run(lang=lang, click=False), _run(lang="en-US", click=False)
    assert out["lang"] == expect and all(out["texts"].values())
    assert (out["texts"] == english["texts"]) is (expect == "en")
