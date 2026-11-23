"""Cabinet -> Settings, the devices block (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6; in Settings since CABINET_REDESIGN_RFC
§4): Settings loading it, the block with the lost-phone checklist,
every string in all four languages, and `renderDevices` escaping a hostile label (run under node when installed)."""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()
_KEYS = ["nav.devices", "devices.title", "devices.subtitle", "devices.current", "devices.created", "devices.last_used",
         "devices.kind_google", "devices.logout", "devices.logout_others", "devices.logout_all", "devices.confirm_all",
         "devices.confirm_others", "devices.empty", "devices.failed", "devices.done", "devices.lost_title",
         "devices.lost_intro", "devices.lost_1", "devices.lost_2", "devices.lost_3"]


def _function(name: str) -> str:
    match = re.search(r"\n(\s*(?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


def _settings_loads() -> str:
    """What `navigate` loads when Settings opens (the Devices section moved into Settings, CABINET_REDESIGN_RFC §4)."""
    nav = re.search(r"\n      function navigate\(name\) \{.*?\n      \}\n", _HTML, re.S).group(0)
    return "".join(re.findall(r'if \(name === "settings"\) \{(.*?)\}', nav, re.S))


def test_the_devices_block_sits_in_settings_and_opening_settings_loads_it():
    assert "navigate('devices')" not in _HTML and 'id="nav-devices"' not in _HTML and 'id="nav-mobile-devices"' not in _HTML
    assert "section-devices" not in _HTML       # no section of that name, and no code that still looks one up
    settings = _HTML[_HTML.index('<section id="section-settings"'):_HTML.index('<section id="section-reminders"')]
    assert 'id="devices-list"' in settings
    assert "loadDevices();" in _settings_loads()


@pytest.mark.parametrize("key", _KEYS)
def test_every_string_is_in_all_four_languages(key):
    assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4


def test_the_lost_phone_checklist_is_rendered_from_translated_lines():
    for element in ("devices-lost-title", "devices-lost-intro", "devices-lost-1", "devices-lost-2", "devices-lost-3"):
        assert f'id="{element}"' in _HTML and f'"#{element}"' in _HTML
    assert "$devices off" in re.search(r'"devices\.lost_1": "([^"]+)"', _HTML).group(1)


def test_the_buttons_call_the_three_logouts():
    assert "logoutOtherDevices()" in _HTML and "logoutAllDevices()" in _HTML and "logoutDevice(" in _HTML
    for path in ("logout-others", "logout-all", "/logout"):
        assert path in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_hostile_label_and_hint_are_escaped_in_the_rendered_row():
    hostile = '<img src=x onerror="alert(1)">'
    devices = [
        {"sid": 'a"b', "label": hostile, "platform_hint": hostile, "auth_kind": "google", "is_current": True,
         "created_at": "2026-11-20T10:00:00+00:00", "last_seen_at": "2026-11-21T10:00:00+00:00"},
        {"sid": "s2", "label": "Mac — Chrome", "platform_hint": "macOS", "auth_kind": "passkey", "is_current": False,
         "created_at": "2026-11-20T10:00:00+00:00", "last_seen_at": "2026-11-21T10:00:00+00:00"}]
    script = f"""
      const els = {{"devices-list": {{innerHTML: ""}}}};
      const document = {{getElementById: (id) => els[id]}};
      const t = (k) => k;
      {_function("escapeHtml")}
      {_function("renderDevices")}
      renderDevices({json.dumps(devices)});
      const first = els["devices-list"].innerHTML;
      renderDevices([]);
      process.stdout.write(JSON.stringify([first, els["devices-list"].innerHTML]));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    html, empty = json.loads(out)
    assert "<img" not in html and 'onerror="' not in html and "&#60;img" in html
    assert 'data-sid="a&#34;b"' in html
    assert "devices.current" in html and "Mac — Chrome" in html and "passkey" in html
    assert "devices.empty" in empty


# ---------------------------------------------------------------- passkeys (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5)

_PASSKEY_KEYS = ["devices.kind_passkey", "passkeys.title", "passkeys.subtitle", "passkeys.add", "passkeys.remove",
                 "passkeys.empty", "passkeys.synced", "passkeys.last_used", "passkeys.never_used", "passkeys.added",
                 "passkeys.exists", "passkeys.failed", "passkeys.confirm_remove", "passkeys.offer_text",
                 "passkeys.offer_add", "passkeys.offer_dismiss"]


@pytest.mark.parametrize("key", _PASSKEY_KEYS)
def test_every_passkey_string_is_in_all_four_languages(key):
    assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4


def test_the_devices_tab_lists_the_passkeys_under_the_sessions_and_offers_to_add_one():
    assert _HTML.index('id="devices-list"') < _HTML.index('id="passkeys-list"') < _HTML.index('id="devices-lost-title"')
    assert 'onclick="addPasskeyHere()"' in _HTML and "loadPasskeys();" in _settings_loads()
    assert "const { authFetch, addPasskey } = window.alekSession;" in _HTML
    for element in ("passkeys-title", "passkeys-subtitle", "passkeys-add", "passkey-offer-text", "passkey-offer-add",
                    "passkey-offer-dismiss"):
        assert f'id="{element}"' in _HTML and f'"#{element}"' in _HTML


def test_the_offer_runs_once_the_cabinet_has_loaded_and_starts_hidden():
    init = _function("init")
    assert init.index("navigate(") < init.index("offerPasskey();")
    assert 'id="passkey-offer" class="card hidden"' in _HTML


_NODE = shutil.which("node") is None


@pytest.mark.skipif(_NODE, reason="node not installed")
def test_a_hostile_passkey_label_is_escaped_and_each_row_removes_its_own_key():
    hostile = '<img src=x onerror="alert(1)">'
    keys = [{"id": 'k"1', "label": hostile, "platform_hint": hostile, "synced": True, "last_used_at": None,
             "created_at": "2026-11-20T10:00:00+00:00"},
            {"id": "k2", "label": "Mac — Chrome", "platform_hint": "Mac — Chrome", "synced": False,
             "last_used_at": "2026-11-21T10:00:00+00:00", "created_at": "2026-11-20T10:00:00+00:00"}]
    script = f"""
      const els = {{"passkeys-list": {{innerHTML: ""}}}};
      const document = {{getElementById: (id) => els[id]}};
      const t = (k) => k;
      {_function("escapeHtml")}
      {_function("renderPasskeys")}
      renderPasskeys({json.dumps(keys)});
      const first = els["passkeys-list"].innerHTML;
      renderPasskeys([]);
      process.stdout.write(JSON.stringify([first, els["passkeys-list"].innerHTML]));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    html, empty = json.loads(out)
    assert "<img" not in html and 'onerror="' not in html and "&#60;img" in html
    assert 'data-id="k&#34;1"' in html and 'data-id="k2"' in html and "removePasskey(this.dataset.id)" in html
    assert html.count("passkeys.synced") == 1 and "passkeys.never_used" in html and "passkeys.last_used" in html
    assert "passkeys.empty" in empty


_OFFER = """
  const shown = {{ hidden: true }};
  const banner = {{ classList: {{ add: () => {{ shown.hidden = true; }}, remove: () => {{ shown.hidden = false; }} }} }};
  const document = {{ getElementById: () => banner }};
  const store = {store};
  const localStorage = store === null
    ? {{ getItem: () => {{ throw new Error("blocked"); }}, setItem: () => {{ throw new Error("blocked"); }} }}
    : {{ getItem: (k) => store[k] || null, setItem: (k, v) => {{ store[k] = v; }} }};
  const window = {{ PublicKeyCredential: {webauthn} }};
  const API = "";
  let fetched = 0;
  const authFetch = async () => {{ fetched++; return {{ ok: {ok}, json: async () => ({{ passkeys: [], offer: {offer} }}) }}; }};
  {functions}
  offerPasskey().then(() => {{
    const before = shown.hidden;
    if ({dismiss}) dismissPasskeyOffer();
    process.stdout.write(JSON.stringify({{ shownAfterLoad: !before, hiddenAtEnd: shown.hidden, fetched, store }}));
  }});
"""


def _offer(store="{}", webauthn="function () {}", ok="true", offer="true", dismiss="false"):
    functions = "\n".join(_function(n) for n in ("passkeyOfferDone", "rememberPasskeyOffer", "fetchPasskeys",
                                                  "offerPasskey", "dismissPasskeyOffer"))
    functions = 'const PASSKEY_OFFER_KEY = "alek.passkeyOffer";\n' + functions
    script = _OFFER.format(store=store, webauthn=webauthn, ok=ok, offer=offer, dismiss=dismiss, functions=functions)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(_NODE, reason="node not installed")
def test_after_a_google_sign_in_the_offer_shows_and_a_dismissal_is_remembered_in_this_browser():
    out = _offer(dismiss="true")
    assert out["shownAfterLoad"] is True and out["hiddenAtEnd"] is True
    assert out["store"] == {"alek.passkeyOffer": "done"}


@pytest.mark.skipif(_NODE, reason="node not installed")
@pytest.mark.parametrize("kwargs, fetched", [
    (dict(store='{"alek.passkeyOffer": "done"}'), 0),     # dismissed or enrolled earlier in this browser
    (dict(webauthn="undefined"), 0),                      # no WebAuthn here
    (dict(offer="false"), 1),                             # a passkey session
    (dict(ok="false"), 1),                                # the list could not be read
], ids=["remembered", "no-webauthn", "passkey-session", "unreadable"])
def test_the_offer_stays_hidden(kwargs, fetched):
    out = _offer(**kwargs)
    assert out["shownAfterLoad"] is False and out["fetched"] == fetched


@pytest.mark.skipif(_NODE, reason="node not installed")
def test_blocked_storage_shows_the_offer_and_never_breaks_the_page():
    out = _offer(store="null", dismiss="true")
    assert out["shownAfterLoad"] is True and out["hiddenAtEnd"] is True
