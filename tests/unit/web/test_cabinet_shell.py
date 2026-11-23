"""The Cabinet shell (CABINET_REDESIGN_RFC §4, §5.0, §8 step 1): the five-item menu, the addresses and their aliases,
Home, and where the old sections' content now sits. The page is read as text through `cabinet_source()`; the
navigation runs under node against a fake DOM, with the section loaders recorded instead of called."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.web.cabinet_source import STATIC, cabinet_source

_HTML = cabinet_source()
_SHELL_JS = (STATIC / "cabinet-shell.js").read_text(encoding="utf-8")
MENU = ["home", "memory", "drives", "reminders", "settings"]
LANGS = ("en", "uk", "fr", "es")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _element(element_id: str) -> str:
    """The markup of the element with this id, up to its matching close tag."""
    start = re.search(r'<(\w+)[^>]*\bid="' + re.escape(element_id) + '"', _HTML)
    assert start, element_id
    tag, depth, i = start.group(1), 0, start.start()
    for m in re.finditer(rf"<(/?){tag}\b[^>]*>", _HTML[i:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return _HTML[i:i + m.end()]
    raise AssertionError(element_id)


def _menu(nav_id: str) -> list:
    return re.findall(r"<button [^>]*id=\"nav-(?:mobile-)?(\w+)\" onclick=\"navigate\('(\w+)'\)\"", _element(nav_id))


def _dict_values(key: str) -> list:
    return re.findall(r'"' + re.escape(key) + r'": "([^"]+)"', _HTML)


# ---------------------------------------------------------------------------------------------------- the menu

def test_the_header_and_the_phone_bar_hold_the_same_five_items_in_order():
    for nav in ("nav-desktop", "nav-phone"):
        items = _menu(nav)
        assert [section for _, section in items] == MENU, nav
        assert all(item_id == section for item_id, section in items), nav
    assert re.search(r'const SECTIONS = \["home", "memory", "drives", "reminders", "settings"\];', _SHELL_JS)


def test_call_and_modes_leave_the_menu():
    for nav in ("nav-desktop", "nav-phone"):
        assert "/cabinet/call" not in _element(nav), nav


@pytest.mark.parametrize("section", MENU)
def test_every_menu_label_is_localised_in_the_four_languages_on_both_menus(section):
    assert len(_dict_values(f"nav.{section}")) == len(LANGS), section
    assert f'"#nav-{section}":' in _SHELL_JS
    assert re.search(r'"#nav-mobile-' + section + r' \.c-bar-label":\s+"nav\.' + section + '"', _SHELL_JS)
    assert re.search(r'"#nav-' + section + r'":\s+"nav\.' + section + '"', _SHELL_JS)


def test_the_header_is_a_solid_sticky_bar():
    rule = re.search(r"\n      \.c-top \{(.*?)\}", _HTML, re.S).group(1)
    assert "position: sticky;" in rule and "top: 0;" in rule and "background: var(--c-ground);" in rule
    assert "blur" not in rule and "rgba" not in rule


def test_the_account_email_and_log_out_moved_from_the_header_to_settings():
    header = _element("nav-desktop")
    assert "user-email" not in header and "logout()" not in header
    settings = _element("section-settings")
    assert 'id="user-email"' in settings and 'onclick="logout()"' in settings
    assert settings.index('id="user-email"') < settings.index('id="settings-tz-title"')
    for key in ("settings.account", "settings.logout"):
        assert len(_dict_values(key)) == len(LANGS), key


def test_new_markup_carries_no_inline_style():
    for element_id in ("nav-desktop", "nav-phone", "section-home"):
        assert "style=" not in _element(element_id), element_id
    for snippet in re.findall(r'<(?:h1|h2|p|div)[^>]*class="c-[^"]*"[^>]*>', _HTML):
        assert "style=" not in snippet, snippet
    header = _HTML[_HTML.index('<header class="c-top">'):_HTML.index("</header>")]
    assert "style=" not in header


# ---------------------------------------------------------------------------------------------------- sections

def test_the_page_has_the_five_sections_and_no_old_ones():
    assert re.findall(r'<section id="section-(\w+)"', _HTML) == ["home", "memory", "drives", "settings", "reminders"]
    for old in ("integrations", "devices", "team"):
        assert f'id="section-{old}"' not in _HTML, old


def test_home_offers_the_call_then_the_modes_link_then_a_tile_per_section():
    home = _element("section-home")
    call = re.search(r'<a class="c-call" id="home-call" href="([^"]+)"', home)
    modes = re.search(r'<a class="c-lnk c-modes" id="home-modes" href="([^"]+)"', home)
    assert call.group(1) == "/cabinet/call" and modes.group(1) == "/cabinet/call/modes"
    tiles = re.findall(r'class="c-tile" id="home-tile-(\w+)" onclick="navigate\(\'(\w+)\'\)"', home)
    assert tiles == [(s, s) for s in MENU[1:]]
    assert call.start() < modes.start() < home.index('class="c-tiles"')
    for key in ("home.call_title", "home.call_sub", "home.modes"):
        assert len(_dict_values(key)) == len(LANGS), key


def test_the_drive_card_is_the_drives_section_and_the_reminders_card_its_own_section():
    assert 'id="drive-container"' in _element("section-drives")
    reminders = _element("section-reminders")
    for marker in ('id="reminders-list"', 'id="reminder-form"', 'onclick="showReminderForm()"',
                   'onclick="saveReminder()"', 'onclick="hideReminderForm()"'):
        assert marker in reminders, marker
        assert marker not in _element("section-settings"), marker


def test_settings_holds_the_old_sections_content_in_the_order_of_its_groups():
    # CABINET_REDESIGN_RFC §5.1: Account, Profile, Connections, Security (the PIN row's body, devices, passkeys, the
    # lost phone), More (deep research, team).
    settings = _element("section-settings")
    order = ['id="user-email"', 'id="settings-tz-title"', 'id="location-input"', 'id="settings-lang-title"',
             'id="settings-voice-title"', 'id="platforms-container"', 'id="gmail-container"', 'id="tasks-container"',
             'id="srow-pin-body"', 'id="devices-list"', 'id="passkeys-list"', 'id="devices-lost-title"',
             'id="settings-dr-title"', 'id="settings-team"', 'id="team-generate-btn"', 'id="codes-list-container"']
    positions = [settings.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_team_shows_for_an_owner_only():
    assert '<div id="settings-team" class="hidden">' in _HTML
    init = re.search(r"\n      async function init\(\) \{.*?\n      \}\n", _SHELL_JS, re.S).group(0)
    assert 'if (isOwner()) document.getElementById("settings-team").classList.remove("hidden");' in init
    assert 'return !!currentUser && currentUser.account.role === "owner";' in _SHELL_JS


def test_the_phone_redirect_stays_until_step_two():
    init = re.search(r"\n      async function init\(\) \{.*?\n      \}\n", _SHELL_JS, re.S).group(0)
    assert init.index("if (redirectPhoneToCall()) return;") < init.index("authFetch(")
    assert 'if (isPhone && [...qs.keys()].length === 0) {\n          window.location.replace("/cabinet/call");' in _SHELL_JS


def test_init_opens_the_section_of_the_address_after_the_connect_return_is_handled():
    init = re.search(r"\n      async function init\(\) \{.*?\n      \}\n", _SHELL_JS, re.S).group(0)
    opened = init.index("navigate(sectionFor(qs, connectReturn ? await loadConnectors() : []));")
    assert init.index("await showConnectToasts(qs);") < init.index("await finishAddedDrive(qs);") < opened


# ---------------------------------------------------------------------------------------------------- under node

_PRELUDE = r"""
const calls = [];
const address = [];
const els = {};
const el = (id) => (els[id] = els[id] || {
  id, attrs: {}, classes: new Set(),
  classList: { add(c) { els[id].classes.add(c); }, remove(c) { els[id].classes.delete(c); } },
  setAttribute(k, v) { els[id].attrs[k] = v; }, removeAttribute(k) { delete els[id].attrs[k]; },
  querySelectorAll() { return []; },
});
const NAV_IDS = %(nav_ids)s;
globalThis.window = { alekSession: { authFetch: async () => ({}), addPasskey: async () => ({}) },
                      location: { pathname: "/cabinet", search: "?connected=k&tab=team", hash: "#top" } };
globalThis.document = {
  addEventListener() {},
  getElementById: (id) => (NAV_IDS.includes(id) || id.startsWith("section-") ? el(id) : null),
  querySelectorAll: (sel) => (sel === ".c-nav-item, .c-bar-item" ? NAV_IDS.map(el) : []),
  querySelector: () => null,
};
globalThis.history = { state: null, replaceState: (state, title, url) => address.push(url) };
globalThis.IntersectionObserver = class { observe() {} unobserve() {} };
const LANG_DICT = { en: {} };
let _driveStatusTimer = null, _driveStatusSnapshot = null;
for (const name of ["relockFacts", "enterMemory", "loadDriveStatus", "loadReminders", "loadSettings",
                    "loadFactsPinSettings", "loadPlatforms", "loadGmailStatus", "loadTasksStatus", "loadDevices",
                    "loadPasskeys", "loadCodes"]) {
  globalThis[name] = () => calls.push(name);
}
"""


def _run(body: str) -> object:
    nav_ids = [f"nav-{s}" for s in MENU] + [f"nav-mobile-{s}" for s in MENU]
    script = (_PRELUDE % {"nav_ids": json.dumps(nav_ids)}) + _SHELL_JS + "\n(async () => {\n" + body + "\n})();\n"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@_node
@pytest.mark.parametrize(("tab", "section"), [
    (None, "drives"), ("", "drives"), ("home", "home"), ("memory", "memory"), ("drives", "drives"),
    ("reminders", "reminders"), ("settings", "settings"),
    ("integrations", "drives"), ("devices", "settings"), ("team", "settings"),
    ("nonsense", "drives"), ("constructor", "drives"), ("__proto__", "drives"),
])
def test_tab_values_and_old_aliases_open_their_section(tab, section):
    qs = "" if tab is None else f"tab={tab}"
    assert _run(f"process.stdout.write(JSON.stringify(sectionFor(new URLSearchParams({json.dumps(qs)}), [])));") == section


_CONNECTORS = [{"kind": "gmail", "integration": "mail"}, {"kind": "microsoft_todo", "integration": "tasks"},
               {"kind": "google_drive", "integration": "drive"}, {"kind": "onedrive", "integration": "drive"}]


@_node
@pytest.mark.parametrize(("query", "section"), [
    ("connected=google_drive&tab=memory", "drives"),
    ("connected=onedrive", "drives"),
    ("connect_error=scope&kind=onedrive&tab=settings", "drives"),
    ("connected=gmail&tab=integrations", "settings"),
    ("connected=microsoft_todo&tab=drives", "settings"),
    ("connect_error=account&kind=gmail", "settings"),
    ("connect_error=failed", "settings"),
    ("connected=unknown_kind&tab=home", "settings"),
])
def test_a_connect_return_opens_the_place_of_its_connector_whatever_the_tab(query, section):
    js = f"process.stdout.write(JSON.stringify(sectionFor(new URLSearchParams({json.dumps(query)}), {json.dumps(_CONNECTORS)})));"
    assert _run(js) == section


@_node
@pytest.mark.parametrize(("name", "opened", "loads"), [
    ("home", "home", []),
    ("memory", "memory", ["enterMemory"]),
    ("drives", "drives", ["loadDriveStatus"]),
    ("integrations", "drives", ["loadDriveStatus"]),
    ("reminders", "reminders", ["loadReminders"]),
    ("settings", "settings", ["loadSettings", "loadFactsPinSettings", "loadPlatforms", "loadGmailStatus",
                              "loadTasksStatus", "loadDevices", "loadPasskeys"]),
    ("devices", "settings", ["loadSettings", "loadFactsPinSettings", "loadPlatforms", "loadGmailStatus",
                             "loadTasksStatus", "loadDevices", "loadPasskeys"]),
])
def test_navigate_opens_the_section_marks_both_menus_writes_the_address_and_loads_its_content(name, opened, loads):
    out = _run(f"""navigate({json.dumps(name)});
      const marked = Object.values(els).filter((e) => e.id.startsWith("nav-") && e.classes.has("active"))
        .map((e) => [e.id, e.attrs["aria-current"] || null]);
      process.stdout.write(JSON.stringify({{calls, address, marked, active: _activeSection,
        shown: els["section-{opened}"].classes.has("active")}}));""")
    assert out["active"] == opened and out["shown"]
    assert out["calls"] == loads
    assert out["address"] == [f"/cabinet?tab={opened}#top"]          # one-shot return parameters are dropped
    assert sorted(out["marked"]) == sorted([[f"nav-{opened}", "page"], [f"nav-mobile-{opened}", "page"]])


@_node
def test_an_owner_also_loads_the_team_codes_in_settings_and_leaving_memory_relocks_it():
    out = _run("""currentUser = { account: { role: "owner" } };
      navigate("memory"); calls.length = 0; navigate("team");
      process.stdout.write(JSON.stringify(calls));""")
    assert out[0] == "relockFacts" and out[-1] == "loadCodes"


_SECTIONS_JS = (STATIC / "cabinet-sections.js").read_text(encoding="utf-8")


@_node
@pytest.mark.parametrize(("active", "reloaded"), [("settings", True), ("home", False), ("memory", False)])
def test_adding_a_passkey_refreshes_the_list_while_settings_is_open(active, reloaded):
    add = re.search(r"\n(      async function addPasskeyHere\(\) \{.*?\n      \})\n", _SECTIONS_JS, re.S).group(1)
    script = f"""
      const calls = [];
      let _activeSection = {json.dumps(active)};
      const addPasskey = async () => "added";
      const window = {{ alekSession: {{ ensurePasskey: async () => true }} }};
      const document = {{ getElementById: () => null }};
      const t = (k) => k, showToast = () => {{}}, rememberPasskeyOffer = () => {{}};
      const loadPasskeys = () => calls.push("loadPasskeys");
      {add}
      addPasskeyHere().then((outcome) => process.stdout.write(JSON.stringify({{outcome, calls}})));
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"outcome": "added", "calls": ["loadPasskeys"] if reloaded else []}
