"""Home's status lines and its place as the default section (CABINET_REDESIGN_RFC §4, §5.0, §8 step 2).

The pure line builders and the loader run under node against the real strings file and a recording `authFetch`;
the markup, the load order and the removed phone redirect are read as text."""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.web.cabinet_source import STATIC, cabinet_source

_HTML = cabinet_source()
_SHELL_JS = (STATIC / "cabinet-shell.js").read_text(encoding="utf-8")
_STRINGS_JS = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
_HOME_JS = (STATIC / "cabinet-home.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
TILES = ("memory", "drives", "reminders", "settings")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_PRELUDE = r"""
let _currentLang = "en";
const t = (key) => (LANG_DICT[_currentLang] || LANG_DICT.en)[key] || key;
const API = "";
const urls = [];
const routes = {};
const authFetch = async (url) => {
  urls.push(url);
  const route = routes[url];
  if (route === undefined || route === "fail") throw new Error("network");
  if (route.status) return { ok: false, status: route.status, json: async () => ({}) };
  return { ok: true, status: 200, json: async () => route };
};
const pinRequest = async (method, path) => {
  urls.push("/api/user/facts-pin" + path);
  const route = routes["/api/user/facts-pin"];
  if (route === undefined || route === "fail") throw new Error("network");
  return { ok: !route.status, status: route.status || 200, data: route.status ? null : route };
};
const tiles = {};
const lineOf = (id) => (tiles[id] = tiles[id] || { textContent: "", classes: new Set(),
  classList: { toggle(c, on) { on ? tiles[id].classes.add(c) : tiles[id].classes.delete(c); } } });
const document = { getElementById: (id) => (id.startsWith("home-") ? lineOf(id) : null) };
"""


def _run(body: str) -> object:
    script = _STRINGS_JS + _PRELUDE + _HOME_JS + "\n(async () => {\n" + body + "\n})();\n"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _status(call: str) -> list:
    """[text, warn] of one builder call."""
    return _run(f"const s = {call}; process.stdout.write(JSON.stringify([s.text, s.warn]));")


# ---------------------------------------------------------------------------------------------------- Memory

@_node
@pytest.mark.parametrize(("pin", "questions", "expected"), [
    ({"has_pin": True, "unlocked": False}, None, ["Closed with PIN", False]),
    ({"has_pin": True, "unlocked": False}, [{}, {}], ["Closed with PIN", False]),   # locked: the count is never shown
    ({"has_pin": True, "unlocked": True}, [], ["Open", False]),
    ({"has_pin": False, "unlocked": False}, [], ["Open", False]),
    ({"has_pin": False, "unlocked": False}, [{}], ["1 question about your memory", True]),
    ({"has_pin": True, "unlocked": True}, [{}, {}, {}], ["3 questions about your memory", True]),
    ({"has_pin": False, "unlocked": False}, None, ["Open", False]),                  # the queue read failed
    (None, None, ["Status unavailable", False]),                                     # the PIN state read failed
])
def test_memory_line_by_pin_state_and_review_questions(pin, questions, expected):
    assert _status(f"homeMemoryStatus({json.dumps(pin)}, {json.dumps(questions)})") == expected


# ---------------------------------------------------------------------------------------------------- Drives

@_node
@pytest.mark.parametrize(("data", "expected"), [
    ({"connections": []}, ["No drives connected", False]),
    ({"connections": [{"mount_name": "Work"}]}, ["1 drive, working", False]),
    ({"connections": [{"mount_name": "Work"}, {"mount_name": "Home"}]}, ["2 drives, all working", False]),
    ({"connections": [{"mount_name": "Work"}, {"mount_name": "Home", "reconnect": {"credential_id": "c"}}]},
     ["Home: Reconnect required", True]),
    ({"connections": [{"mount_name": "Work", "indexing": {"enabled": True, "last_error": "throttled"}}]},
     ["Work: Last problem", True]),
    ({"connections": [{"indexing": {"enabled": False, "last_error": "internal"}}]}, ["1 drive, working", False]),
    ({"connections": [{"mount_name": "A", "indexing": {"enabled": True, "last_error": "internal"}},
                      {"mount_name": "B", "reconnect": {"credential_id": "c"}}]}, ["A: Last problem", True]),
    ({"connections": [{"reconnect": {"credential_id": "c"}}]}, ["Reconnect required", True]),
    (None, ["Status unavailable", False]),
    ({}, ["Status unavailable", False]),
])
def test_drives_line_by_drive_states(data, expected):
    assert _status(f"homeDrivesStatus({json.dumps(data)})") == expected


# ---------------------------------------------------------------------------------------------------- Reminders

_NOW = "Date.parse('2026-11-23T10:00:00Z')"


@_node
def test_reminders_line_is_the_next_one_ahead_in_the_users_timezone():
    reminders = [
        {"text": "Past", "due": "2026-11-22T09:00:00+01:00"},
        {"text": "Later", "due": "2026-11-25T09:00:00+01:00"},
        {"text": "Check email", "due": "2026-11-24T08:30:00+01:00"},
        {"text": "No due", "due": None},
    ]
    text, warn = _status(f"homeRemindersStatus({json.dumps(reminders)}, 'Europe/Lisbon', {_NOW})")
    assert warn is False
    assert text.endswith(": Check email")
    assert "7:30" in text and "Nov 24, 2026" in text      # 08:30 at +01:00 is 07:30 in Lisbon


@_node
@pytest.mark.parametrize("reminders", [[], [{"text": "Past", "due": "2026-11-22T09:00:00+01:00"}], [{"text": "x", "due": None}]])
def test_reminders_line_says_none_scheduled_when_nothing_is_ahead(reminders):
    assert _status(f"homeRemindersStatus({json.dumps(reminders)}, 'UTC', {_NOW})") == ["None scheduled", False]


@_node
def test_reminders_line_is_neutral_when_the_read_failed_and_survives_a_bad_timezone():
    assert _status(f"homeRemindersStatus(null, 'UTC', {_NOW})") == ["Status unavailable", False]
    text, warn = _status(f"homeRemindersStatus([{{text: 'Check email', due: '2026-11-24T08:30:00Z'}}], 'Not/AZone', {_NOW})")
    assert warn is False and text.endswith(": Check email")


# ---------------------------------------------------------------------------------------------------- Settings

_OK = {"connected": True}


@_node
@pytest.mark.parametrize(("gmail", "tasks", "expected"), [
    (_OK, {"connected": True}, ["Connections working", False]),
    ({"connected": False}, {"connected": False}, ["Connections working", False]),      # never connected: nothing to fix
    ({"connected": True, "needs_reconnect": True}, _OK, ["Gmail: Reconnect required", True]),
    ({"connected": False, "reconnect_needed": True}, _OK, ["Gmail: Reconnect required", True]),
    (_OK, {"connected": False, "reconnect_needed": True}, ["Microsoft To Do: Reconnect required", True]),
    ({"connected": True, "needs_reconnect": True}, {"connected": False, "reconnect_needed": True},
     ["Gmail: Reconnect required", True]),                                              # the first one
    (None, None, ["Status unavailable", False]),
    (None, _OK, ["Status unavailable", False]),                                         # a failed read is no "working"
    (_OK, None, ["Status unavailable", False]),
    (None, {"connected": False, "reconnect_needed": True}, ["Microsoft To Do: Reconnect required", True]),
])
def test_settings_line_by_connection_states(gmail, tasks, expected):
    assert _status(f"homeSettingsStatus({json.dumps(gmail)}, {json.dumps(tasks)})") == expected


# ---------------------------------------------------------------------------------------------------- the loader

_CALM = {
    "/api/user/facts-pin": {"has_pin": False, "unlocked": False},
    "/api/user/facts/review-queue": {"questions": []},
    "/api/drive/connections": {"connections": [{"mount_name": "Work"}]},
    "/api/user/reminders": {"reminders": []},
    "/api/user/timezone": {"timezone": "UTC"},
    "/api/gmail/status": {"connected": True},
    "/api/tasks/microsoft/status": {"connected": True},
}


def _load(routes: dict) -> dict:
    return _run(f"""Object.assign(routes, {json.dumps(routes)});
      for (const [k, v] of Object.entries({json.dumps({k: "fail" for k, v in routes.items() if v == "fail"})})) routes[k] = v;
      await loadHome();
      const out = {{ urls }};
      for (const n of {json.dumps(list(TILES))}) out[n] = [lineOf("home-status-" + n).textContent, lineOf("home-tile-" + n).classes.has("c-tile-warn")];
      process.stdout.write(JSON.stringify(out));""")


@_node
def test_home_reads_in_parallel_and_shows_a_calm_account():
    out = _load(_CALM)
    assert out["memory"] == ["Open", False]
    assert out["drives"] == ["1 drive, working", False]
    assert out["reminders"] == ["None scheduled", False]
    assert out["settings"] == ["Connections working", False]


@_node
def test_a_warning_tile_gets_the_warning_class_and_the_others_stay_calm():
    routes = {**_CALM, "/api/user/facts/review-queue": {"questions": [{}, {}]},
              "/api/drive/connections": {"connections": [{"mount_name": "Work", "reconnect": {"credential_id": "c"}}]},
              "/api/gmail/status": {"connected": True, "needs_reconnect": True}}
    out = _load(routes)
    assert out["memory"] == ["2 questions about your memory", True]
    assert out["drives"] == ["Work: Reconnect required", True]
    assert out["reminders"][1] is False
    assert out["settings"] == ["Gmail: Reconnect required", True]


@_node
def test_every_failed_read_leaves_a_neutral_line_and_nothing_escapes():
    routes = {url: "fail" for url in _CALM}
    out = _load(routes)
    for tile in TILES:
        assert out[tile] == ["Status unavailable", False], tile


@_node
def test_a_failed_http_status_is_neutral_too_and_one_failure_does_not_touch_the_other_tiles():
    routes = {**_CALM, "/api/drive/connections": {"status": 500}, "/api/user/reminders": {"status": 501}}
    out = _load(routes)
    assert out["drives"] == ["Status unavailable", False]
    assert out["reminders"] == ["Status unavailable", False]
    assert out["memory"] == ["Open", False] and out["settings"] == ["Connections working", False]


@_node
def test_memory_reads_no_facts_route_and_asks_for_review_questions_only_when_open():
    facts_routes = lambda urls: [u for u in urls if "/facts" in u]   # noqa: E731
    open_pin = _load({**_CALM, "/api/user/facts-pin": {"has_pin": True, "unlocked": True}})
    assert facts_routes(open_pin["urls"]) == ["/api/user/facts-pin", "/api/user/facts/review-queue"]
    no_pin = _load(_CALM)
    assert facts_routes(no_pin["urls"]) == ["/api/user/facts-pin", "/api/user/facts/review-queue"]
    closed = _load({**_CALM, "/api/user/facts-pin": {"has_pin": True, "unlocked": False}})
    assert facts_routes(closed["urls"]) == ["/api/user/facts-pin"]
    assert closed["memory"] == ["Closed with PIN", False]
    unknown = _load({**_CALM, "/api/user/facts-pin": "fail"})
    assert facts_routes(unknown["urls"]) == ["/api/user/facts-pin"]


@_node
def test_home_makes_only_calls_the_cabinet_already_makes():
    urls = set(_load(_CALM)["urls"])
    assert urls == set(_CALM)


# ---------------------------------------------------------------------------------------------------- the page

def test_each_tile_has_a_status_line_and_every_new_string_exists_in_four_languages():
    for tile in TILES:
        assert re.search(rf'id="home-tile-{tile}".*?<span class="c-tile-status" id="home-status-{tile}" aria-live="polite"></span>',
                         _HTML, re.S), tile
    for key in ("unknown", "mem_closed", "mem_open", "mem_q_one", "mem_q_many", "drives_none", "drives_ok_one",
                "drives_ok_many", "rem_none", "rem_next", "set_ok"):
        assert len(re.findall(rf'"home\.{key}": "', _STRINGS_JS)) == len(LANGS), key


def test_the_home_script_is_served_and_loaded_after_the_sections():
    from src.web.user_cabinet_app import CABINET_ASSETS
    assert CABINET_ASSETS["cabinet-home.js"] == "application/javascript"
    shell = (STATIC / "cabinet.html").read_text(encoding="utf-8")
    order = [m for m in re.findall(r'<script src="/cabinet/assets/([\w.-]+)"></script>', shell)]
    assert order == ["cabinet-strings.js", "cabinet-shell.js", "cabinet-sections.js", "cabinet-home.js"]
    assert 'style="' not in _element_markup("home-tile-memory")


def _element_markup(element_id: str) -> str:
    start = _HTML.index(f'id="{element_id}"')
    return _HTML[start:_HTML.index("</button>", start)]


def test_navigating_to_home_loads_its_lines_and_a_language_change_redraws_them():
    assert 'if (name === "home") loadHome();' in _SHELL_JS
    assert 'if (changed && active && active.id === "section-home") loadHome();' in _SHELL_JS


# ---------------------------------------------------------------------------------------------------- default section and redirect

def test_the_phone_redirect_is_gone():
    assert "redirectPhoneToCall" not in _SHELL_JS and "redirectPhoneToCall" not in _HTML
    assert "location.replace" not in _SHELL_JS
    assert "matchMedia" not in _SHELL_JS


@_node
@pytest.mark.parametrize("qs", ["", "tab=", "tab=nonsense", "tab=constructor", "tab=__proto__", "joined=true", "tab=home"])
def test_home_is_the_default_section_on_every_width(qs):
    script = _SHELL_JS_HEAD + f"process.stdout.write(JSON.stringify(sectionFor(new URLSearchParams({json.dumps(qs)}), [])));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == "home"


@_node
@pytest.mark.parametrize(("qs", "section"), [
    ("tab=integrations", "drives"), ("tab=devices", "settings"), ("tab=team", "settings"), ("tab=memory", "memory"),
    ("tab=reminders", "reminders"), ("connected=gmail&tab=home", "settings"), ("connected=onedrive", "drives"),
])
def test_aliases_and_connect_returns_are_unchanged(qs, section):
    connectors = [{"kind": "gmail", "integration": "mail"}, {"kind": "onedrive", "integration": "drive"}]
    script = _SHELL_JS_HEAD + f"process.stdout.write(JSON.stringify(sectionFor(new URLSearchParams({json.dumps(qs)}), {json.dumps(connectors)})));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == section


# The shell's pure address functions need only these globals.
_SHELL_JS_HEAD = (
    'globalThis.window = { alekSession: { authFetch: async () => ({}), addPasskey: async () => ({}) }, location: {} };\n'
    'globalThis.document = { addEventListener() {} };\nglobalThis.IntersectionObserver = class {};\n'
    'const LANG_DICT = { en: {} };\n' + _SHELL_JS + "\n"
)
