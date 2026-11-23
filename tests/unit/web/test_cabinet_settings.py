"""Cabinet -> Settings (CABINET_REDESIGN_RFC §5.1, §5.3): groups of collapsed rows, every operation of the old
Integrations (but the drive card), Devices, Team and Settings sections reachable from a row, the Memory PIN as one state
at a time, the two language rows sending the one stored pair, the strings in four languages. The page is read as text
through `cabinet_source()`; the row, PIN and language logic run under node against a fake DOM when node is installed.
Codes in this file are `0001` (a PIN) and `0002` (a duress code) only."""
import json
import re
import shutil
import subprocess

import pytest

from src.web.user_cabinet_app import CABINET_ASSETS
from tests.unit.web.cabinet_source import STATIC, cabinet_source

_HTML = cabinet_source()
_SHELL = (STATIC / "cabinet.html").read_text(encoding="utf-8")
_SETTINGS_JS = (STATIC / "cabinet-settings.js").read_text(encoding="utf-8")
_SECTIONS_JS = (STATIC / "cabinet-sections.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _element(element_id: str, html: str = _HTML) -> str:
    """The markup of the element with this id, up to its matching close tag."""
    start = re.search(r'<(\w+)[^>]*\bid="' + re.escape(element_id) + '"', html)
    assert start, element_id
    tag, depth, i = start.group(1), 0, start.start()
    for m in re.finditer(rf"<(/?){tag}\b[^>]*>", html[i:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html[i:i + m.end()]
    raise AssertionError(element_id)


def _function(name: str) -> str:
    match = re.search(r"\n(      (?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


def _dict(lang: str) -> dict:
    strings = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
    start = strings.index(f"        {lang}: {{")
    block = strings[start:strings.index("        },", start)]
    return dict(re.findall(r'"([a-z_.0-9]+)": "((?:[^"\\]|\\.)*)"', block))


_SETTINGS = _element("section-settings")
_ROWS = json.loads(re.search(r"const SETTINGS_ROWS = (\[.*?\]);", _SETTINGS_JS, re.S).group(1))


# ---------------------------------------------------------------------------------------------------- the rows

def test_settings_is_six_groups_in_the_order_of_the_rfc():
    labels = re.findall(r'<h2 class="c-glabel" id="settings-(\w+)-label">', _SETTINGS)
    assert labels == ["account", "profile", "connections", "security", "voice", "more"]


def test_the_rows_sit_in_their_groups_in_order():
    order = ["srow-account", "srow-tz", "srow-city", "srow-uilang", "srow-reply", "srow-voice", "srow-platforms",
             "srow-mail", "srow-todo", "srow-drives", "srow-pin", "srow-devices", "srow-passkeys", "srow-lost",
             "settings-modes", "dr-second-pass", "srow-team"]
    positions = [_SETTINGS.index(f'id="{row}"') for row in order]
    assert positions == sorted(positions)
    groups = ["settings-profile-label", "settings-connections-label", "settings-security-label",
              "settings-voice-label", "settings-more-label"]
    assert [_SETTINGS.index(f'id="{g}"') for g in groups] == sorted(_SETTINGS.index(f'id="{g}"') for g in groups)
    assert _SETTINGS.index('id="settings-connections-label"') < _SETTINGS.index('id="srow-platforms"')
    assert _SETTINGS.index('id="settings-security-label"') < _SETTINGS.index('id="srow-pin"')
    assert _SETTINGS.index('id="settings-more-label"') < _SETTINGS.index('id="dr-second-pass"')


@pytest.mark.parametrize("row", _ROWS)
def test_every_row_is_a_collapsed_button_that_opens_its_own_body(row):
    button = re.search(r'<button type="button" class="c-row" id="srow-' + row + r'" aria-expanded="false" '
                       r'aria-controls="srow-' + row + r'-body" onclick="toggleSettingsRow\(\'' + row + r'\'\)">',
                       _SETTINGS)
    assert button, row
    assert f'<div class="c-row-body hidden" id="srow-{row}-body">' in _SETTINGS, row
    title = _element(f"srow-{row}")
    assert 'class="c-row-title"' in title and 'class="c-chev"' in title, row


def test_the_rows_of_the_script_are_the_rows_of_the_markup():
    assert sorted(_ROWS) == sorted(re.findall(r"toggleSettingsRow\('(\w+)'\)", _SETTINGS))


def test_switches_and_selects_save_at_once_and_only_the_city_has_a_save_button():
    assert 'id="timezone-input" class="c-input" onchange="saveTimezone()"' in _SETTINGS
    assert 'id="lang-ui-select" class="c-input" onchange="setLangUI(this.value); saveLanguage()"' in _SETTINGS
    assert 'onclick="setLangMirror(true); saveLanguage()"' in _SETTINGS
    assert 'onclick="setLangMirror(false); saveLanguage()"' in _SETTINGS
    assert _SETTINGS.count('onclick="toggleVoiceLang(this); saveVoiceLanguages()"') == 5
    assert 'class="c-switch" id="dr-second-pass" onchange="saveDeepResearch()"' in _SETTINGS
    saves = re.findall(r'<button[^>]*>Save</button>', _SETTINGS)
    assert saves == ['<button type="button" class="c-btn c-pri" id="settings-city-save" onclick="saveLocation()">Save</button>']
    assert "if (event.key === 'Enter') saveLocation();" in _element("srow-city-body")


# Every operation the old Integrations (but the drive card), Devices, Team and Settings sections offered, by the row
# that offers it now: (where it is written, what is written there). "markup" is the row's body (or the row itself),
# a function name is the renderer whose markup the row's body receives.
_OPERATIONS = {
    "account": [("markup", 'onclick="logout()"')],
    "tz": [("markup", "saveTimezone()")],
    "city": [("markup", "saveLocation()")],
    "uilang": [("markup", "setLangUI(this.value); saveLanguage()")],
    "reply": [("markup", "setLangMirror(true); saveLanguage()"), ("markup", "setLangMirror(false); saveLanguage()")],
    "voice": [("markup", "saveVoiceLanguages()")],
    "platforms": [("platformItem", "linkPlatform('${id}', '${label}')"), ("platformItem", "unlinkPlatform('${id}')"),
                  ("renderPlatforms", 'platformItem("telegram"'), ("renderPlatforms", 'platformItem("slack"'),
                  ("renderPlatforms", 'platformItem("phone"')],
    "mail": [("renderGmailStatus", "saveDailyReview(this.checked"), ("renderGmailStatus", "saveDailyReview(true"),
             ("renderGmailStatus", "saveGmailAutoIndex(this.checked"), ("renderGmailStatus", "saveGmailAutoIndex(true"),
             ("renderGmailStatus", 'onclick="triggerGmailIndex()"'),
             ("renderGmailStatus", 'onclick="cancelGmailIndex(this.dataset.jobId, this)"'),
             ("renderGmailStatus", 'onclick="deleteGmailData()"'), ("renderGmailStatus", 'onclick="disconnectGmail()"'),
             ("renderGmailStatus", 'connectButton(data.connect_href, t("platform.connect_gmail")'),
             ("renderGmailStatus", 'connectButton(data.reconnect_href, t("gmail.reconnect")'),
             ("triggerGmailIndex", 'onchange="updateIndexModal()"'),
             ("triggerGmailIndex", 'onclick="submitIndexModal()"'), ("triggerGmailIndex", 'onclick="closeModal()"')],
    "todo": [("renderServiceStatus", "onclick=\"disconnectService('${id}')\""),
             ("renderServiceStatus", 'connectButton(data.connect_href, t("platform.connect_todo")'),
             ("renderServiceStatus", 'connectButton(data.connect_href, t("todo.reconnect")')],
    "pin": [("markup", 'onclick="submitPinSet()"'), ("markup", 'onclick="submitPinChange()"'),
            ("markup", 'onclick="submitPinRemove()"'), ("markup", 'onclick="requestPinReset()"'),
            ("markup", 'onclick="cancelPinReset()"'), ("markup", 'onclick="logoutOtherDevices()"'),
            ("markup", "onclick=\"pinShow('set1')\""), ("markup", "onclick=\"pinShow('ch1')\""),
            ("markup", "onclick=\"pinShow('remove')\""), ("markup", "onclick=\"pinShow('forgot')\"")],
    "devices": [("renderDevices", 'onclick="logoutDevice(this.dataset.sid, !!this.dataset.current)"'),
                ("markup", 'onclick="logoutOtherDevices()"'), ("markup", 'onclick="logoutAllDevices()"')],
    # A change without a passkey proof goes to the Google sign-in by itself and resumes in this row (main's #511).
    "passkeys": [("renderPasskeys", 'onclick="removePasskey(this.dataset.id)"'), ("markup", 'onclick="addPasskeyHere()"'),
                 ("addPasskeyHere", 'window.location.assign(googleProofUrl("add_passkey"))'),
                 ("removePasskey", 'window.location.assign(googleProofUrl("remove_passkey", id))'),
                 ("googleProofUrl", '"/auth/login/google?next="'),
                 ("resumePasskeyFlow", 'getElementById("passkeys-resume-hint")')],
    "lost": [("markup", 'id="devices-lost-1"'), ("markup", 'id="devices-lost-2"'), ("markup", 'id="devices-lost-3"')],
    "team": [("markup", 'onclick="generateTeamInvite()"'), ("generateTeamInvite", 'onclick="copyDeepLink()"'),
             ("loadCodes", 'getElementById("codes-list-container")')],
}
# The renderer's target, which must sit in the row's body.
_TARGETS = {"platformItem": None, "renderPlatforms": "platforms-container", "renderGmailStatus": "gmail-container",
            "triggerGmailIndex": None, "renderServiceStatus": "tasks-container", "renderDevices": "devices-list",
            "renderPasskeys": "passkeys-list", "addPasskeyHere": None, "removePasskey": None, "googleProofUrl": None,
            "resumePasskeyFlow": "passkeys-resume-hint", "generateTeamInvite": "generated-code-container",
            "loadCodes": "codes-list-container"}


@pytest.mark.parametrize("row", sorted(_OPERATIONS))
def test_every_operation_is_reachable_from_its_settings_row(row):
    body = _element(f"srow-{row}-body")
    for where, written in _OPERATIONS[row]:
        if where == "markup":
            assert written in body, (row, written)
            continue
        assert written in _function(where), (row, where, written)
        target = _TARGETS[where]
        if target:
            assert f'id="{target}"' in body and f'getElementById("{target}")' in _function(where), (row, target)
    assert sorted(_OPERATIONS) == sorted(_ROWS)


def test_the_rows_that_open_no_body_navigate_save_or_leave():
    assert '<button type="button" class="c-row" id="srow-drives" onclick="navigate(\'drives\')">' in _SETTINGS
    assert '<a class="c-row" id="settings-modes" href="/cabinet/call/modes">' in _SETTINGS
    assert 'onchange="saveDeepResearch()"' in _SETTINGS


def test_the_drive_card_stays_in_drives_and_settings_holds_no_drive_operation():
    assert 'id="drive-container"' in _element("section-drives") and 'id="drive-container"' not in _SETTINGS
    assert "integration-card" not in _HTML and "integration-bento" not in _HTML


def test_the_settings_markup_and_script_carry_no_inline_style():
    assert "style=" not in _SETTINGS
    assert "style=" not in _SETTINGS_JS and ".style." not in _SETTINGS_JS


def test_the_settings_script_is_an_asset_loaded_after_the_sections():
    assert CABINET_ASSETS["cabinet-settings.js"] == "application/javascript"
    order = [_SHELL.index(f'<script src="/cabinet/assets/{name}"></script>')
             for name in ("cabinet-strings.js", "cabinet-shell.js", "cabinet-sections.js", "cabinet-settings.js")]
    assert order == sorted(order)


def test_the_pin_row_holds_one_panel_per_state_and_only_off_shows_at_first():
    steps = json.loads(re.search(r"const PIN_STEPS = (\[.*?\]);", _SETTINGS_JS).group(1))
    assert steps == ["off", "set1", "set2", "on", "ch1", "ch2", "ch3", "remove", "forgot", "pending"]
    body = _element("srow-pin-body")
    for step in steps:
        hidden = "" if step == "off" else " hidden"
        assert f'<div class="c-flow{hidden}" id="pin-step-{step}">' in body, step
    assert body.count('type="password" inputmode="numeric"') == 10
    assert '<p class="c-msg hidden" id="pin-message" role="alert">' in body


# ---------------------------------------------------------------------------------------------------- the strings

def _keys_used() -> set:
    keys = set(re.findall(r'\bt\("([a-z_.0-9]+)"\)', _SETTINGS_JS))
    keys |= set(re.findall(r'"#[^"]+": "([a-z_.0-9]+)"', _SETTINGS_JS))
    keys |= set(re.findall(r'\["(?:off|ok|warn)", "(pin\.chip_\w+)"\]', _SETTINGS_JS))
    keys |= {f"settings.reply.always_{lang}" for lang in ("uk", "en", "fr", "es", "default")}
    keys |= {f"{base}_{form}" for base in ("devices.count", "passkeys.count") for form in ("one", "other")}
    return keys


def test_every_string_settings_uses_is_in_all_four_languages():
    keys = _keys_used()
    assert len(keys) > 120
    dicts = {lang: _dict(lang) for lang in LANGS}
    for key in sorted(keys):
        for lang in LANGS:
            assert dicts[lang].get(key), (lang, key)
    for key in ("devices.count", "passkeys.count"):
        for form in ("one", "few", "many", "other"):
            assert "{n}" in dicts["uk"][f"{key}_{form}"], (key, form)


def test_every_translated_selector_of_settings_names_an_element_of_the_page():
    for selector in re.findall(r'"#([a-z0-9-]+)": "[a-z_.0-9]+"', _SETTINGS_JS):
        assert f'id="{selector}"' in _HTML, selector


# ---------------------------------------------------------------------------------------------------- under node

def _block(text: str, start: str, end: str | None = None) -> str:
    a = text.index(start)
    return text[a:text.index(end, a)] if end else text[a:]


def _const(name: str) -> str:
    return re.search(r"\n(      const " + name + r" = .*?;)\n", _SETTINGS_JS, re.S).group(1)


_DOM = r"""
const EN = %(en)s;
let _currentLang = "en";
const t = (k) => EN[k] || k;
const focused = [];
const els = new Proxy({}, {get: (o, id) => o[id] ??= (() => {
  const classes = new Set(id.startsWith("pin-step-") && id !== "pin-step-off" ? ["hidden"] : []);
  const attrs = {};
  return {id, value: "", textContent: "", disabled: false, className: "", options: [], selectedIndex: -1,
    focus() { focused.push(id); }, setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k] ?? null; },
    attrs, scrollIntoView() {},
    classList: {add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); }}};
})()});
const FIRST_INPUT = {set1: "pin-new", set2: "pin-duress", ch1: "pin-current-change", ch2: "pin-new-c",
                     ch3: "pin-duress-c", remove: "pin-current-remove"};
const document = {getElementById: (id) => els[id],
  querySelector: (sel) => { const m = /^#pin-step-(\w+) input$/.exec(sel); return m && FIRST_INPUT[m[1]] ? els[FIRST_INPUT[m[1]]] : null; },
  querySelectorAll: () => []};
const API = "";
const calls = [];
let replies = [], asked = 0, answer = true, _activeSection = "settings", factsMode = "idle";
const confirm = () => { asked += 1; return answer; };
const browseInit = () => {};
const logoutOtherDevices = () => {};
const navigator = {sendBeacon: () => true};
const Blob = function () {};
const window = {addEventListener: () => {}, location: {search: ""}};
const authFetch = async (url, init) => {
  calls.push({url, method: (init && init.method) || "GET", body: init && init.body ? JSON.parse(init.body) : null});
  const r = replies.shift() || {status: 200, body: {}};
  return {ok: r.status < 400, status: r.status, json: async () => r.body, clone() { return this; }};
};
const shown = () => %(steps)s.filter((s) => !els["pin-step-" + s].classList.contains("hidden"));
const msg = () => [els["pin-message"].textContent, els["pin-message"].classList.contains("c-msg-err") ? "err" : "ok",
                   els["pin-message"].classList.contains("hidden")];
const posts = () => calls.filter((c) => c.method === "POST");
const typed = (pairs) => { for (const [id, v] of pairs) els[id].value = v; };
const empty = (ids) => ids.every((id) => els[id].value === "");
"""


def _run(body: str, extra: str = ""):
    steps = re.search(r"const PIN_STEPS = (\[.*?\]);", _SETTINGS_JS).group(1)
    sources = "\n".join([
        _block(_SECTIONS_JS, "      /* ── Facts PIN (CABINET_DEVICE_SESSIONS_RFC"),
        "let _openSettingsRow = \"pin\";",
        _block(_SETTINGS_JS, "      /* ── Settings → Security → Memory PIN", "      /* ── Settings ──"),
        _const("SETTINGS_ROWS"), _function("setSettingsChip"), _function("toggleSettingsRow"),
        _function("openSettingsRow"), extra,
    ])
    script = (_DOM % {"en": json.dumps(_dict("en")), "steps": steps}) + sources + "\n(async () => {\n" + body + \
        "\n})().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_ON = {"status": 200, "body": {"has_pin": True, "unlocked": False, "reset_due_at": None}}
_OFF = {"status": 200, "body": {"has_pin": False, "unlocked": False, "reset_due_at": None}}
_ALL_FIELDS = ["pin-new", "pin-new2", "pin-duress", "pin-duress2", "pin-current-change", "pin-new-c", "pin-new2-c",
               "pin-duress-c", "pin-duress2-c", "pin-current-remove"]


@_node
def test_setting_a_pin_takes_two_steps_and_one_request_with_each_code_twice():
    out = _run(f"""replies = [{json.dumps(_OFF)}]; await loadFactsPinSettings();
      const atRest = [shown(), els["pin-chip-text"].textContent, els["pin-chip"].className];
      pinShow("set1"); typed([["pin-new", "0001"], ["pin-new2", "0001"]]); pinNext();
      const second = [shown(), posts().length, focused.at(-1)];
      typed([["pin-duress", "0002"], ["pin-duress2", "0002"]]);
      replies = [{{status: 200, body: {{status: "done"}}}}, {json.dumps(_ON)}]; await submitPinSet();
      return [atRest, second, posts(), shown(), msg(), els["pin-chip"].className,
        els["pin-logout-offer"].classList.contains("hidden"), empty({json.dumps(_ALL_FIELDS)})];""")
    assert out[0] == [["off"], "Off", "c-chip c-chip-off"]
    assert out[1] == [["set2"], 0, "pin-duress"]
    assert out[2] == [{"url": "/api/user/facts-pin/set", "method": "POST",
                       "body": {"pin": "0001", "pin_confirm": "0001", "duress": "0002", "duress_confirm": "0002"}}]
    assert out[3:] == [["on"], ["PIN set.", "ok", False], "c-chip c-chip-ok", True, True]


@_node
@pytest.mark.parametrize(("first", "second", "error"), [
    ("0001", "0002", "pin.err_mismatch"), ("407", "407", "pin.err_not_four_digits"), ("", "", "pin.err_not_four_digits"),
])
def test_a_first_step_that_cannot_be_sent_stays_with_its_error_and_sends_nothing(first, second, error):
    out = _run(f"""pinShow("set1"); typed([["pin-new", "{first}"], ["pin-new2", "{second}"]]); pinNext();
      return [shown(), msg(), posts().length, empty(["pin-new", "pin-new2"])];""")
    assert out == [["set1"], [_dict("en")[error], "err", False], 0, True]


@_node
def test_a_duress_code_equal_to_the_new_pin_stays_on_its_step():
    out = _run("""pinShow("set1"); typed([["pin-new", "0001"], ["pin-new2", "0001"]]); pinNext();
      typed([["pin-duress", "0001"], ["pin-duress2", "0001"]]); await submitPinSet();
      return [shown(), msg()[0], posts().length, els["pin-new"].value];""")
    assert out == [["set2"], _dict("en")["pin.err_duress_same_as_pin"], 0, "0001"]


@_node
def test_a_refused_set_returns_to_the_step_whose_code_the_server_refused():
    out = _run("""pinShow("set1"); typed([["pin-new", "0001"], ["pin-new2", "0001"]]); pinNext();
      typed([["pin-duress", "0002"], ["pin-duress2", "0002"]]);
      replies = [{status: 400, body: {error: "trivial"}}]; await submitPinSet();
      return [shown(), msg(), posts().length, els["pin-new"].value, els["pin-duress"].value];""")
    assert out == [["set1"], [_dict("en")["pin.err_trivial"], "err", False], 1, "", ""]


@_node
def test_changing_the_pin_takes_three_steps_one_request_and_offers_to_log_out_the_others():
    out = _run(f"""replies = [{json.dumps(_ON)}]; await loadFactsPinSettings(); const atRest = shown();
      pinShow("ch1"); typed([["pin-current-change", "0001"]]); pinNext(); const two = shown();
      typed([["pin-new-c", "0002"], ["pin-new2-c", "0002"]]); pinNext(); const three = shown();
      typed([["pin-duress-c", "0001"], ["pin-duress2-c", "0001"]]);
      replies = [{{status: 200, body: {{status: "done"}}}}, {json.dumps(_ON)}]; await submitPinChange();
      return [atRest, two, three, posts(), shown(), msg(), els["pin-logout-offer"].classList.contains("hidden"),
        empty({json.dumps(_ALL_FIELDS)})];""")
    assert out[:3] == [["on"], ["ch2"], ["ch3"]]
    assert out[3] == [{"url": "/api/user/facts-pin/change", "method": "POST", "body": {
        "current": "0001", "pin": "0002", "pin_confirm": "0002", "duress": "0001", "duress_confirm": "0001"}}]
    assert out[4:] == [["on"], ["PIN changed.", "ok", False], False, True]


@_node
def test_a_wrong_current_pin_returns_to_step_one_with_the_attempts_left_and_the_new_codes_cleared():
    out = _run("""pinShow("ch1"); typed([["pin-current-change", "0002"]]); pinNext();
      typed([["pin-new-c", "0001"], ["pin-new2-c", "0001"]]); pinNext();
      typed([["pin-duress-c", "0002"], ["pin-duress2-c", "0002"]]);
      replies = [{status: 400, body: {error: "wrong_code", attempts_left: 3}}]; await submitPinChange();
      return [shown(), msg(), empty(["pin-current-change", "pin-new-c", "pin-new2-c", "pin-duress-c", "pin-duress2-c"]),
        focused.at(-1), els["pin-logout-offer"].classList.contains("hidden")];""")
    assert out == [["ch1"], ["Wrong PIN. Attempts left: 3.", "err", False], True, "pin-current-change", True]


@_node
def test_a_lock_while_changing_shows_account_locked():
    out = _run("""pinShow("ch1"); typed([["pin-current-change", "0002"]]); pinNext();
      typed([["pin-new-c", "0001"], ["pin-new2-c", "0001"]]); pinNext();
      typed([["pin-duress-c", "0002"], ["pin-duress2-c", "0002"]]);
      replies = [{status: 423, body: {error: "account_locked"}}]; await submitPinChange();
      return [msg()[0], els["facts-pin-input"].disabled];""")
    assert out == ["Account locked.", True]


@_node
def test_removing_sends_the_current_pin_once_and_the_row_goes_off():
    out = _run(f"""replies = [{json.dumps(_ON)}]; await loadFactsPinSettings();
      pinShow("remove"); typed([["pin-current-remove", "0001"]]);
      replies = [{{status: 200, body: {{status: "done"}}}}, {json.dumps(_OFF)}]; await submitPinRemove();
      return [posts(), shown(), msg(), els["pin-chip"].className, els["pin-current-remove"].value];""")
    assert out[0] == [{"url": "/api/user/facts-pin/remove", "method": "POST", "body": {"current": "0001"}}]
    assert out[1:] == [["off"], ["PIN removed.", "ok", False], "c-chip c-chip-off", ""]


@_node
def test_a_refused_remove_stays_on_its_step_with_the_attempts_left():
    out = _run("""pinShow("remove"); typed([["pin-current-remove", "0002"]]);
      replies = [{status: 400, body: {error: "wrong_code", attempts_left: 4}}]; await submitPinRemove();
      return [shown(), msg(), els["pin-current-remove"].value];""")
    assert out == [["remove"], ["Wrong PIN. Attempts left: 4.", "err", False], ""]


@_node
def test_forgot_explains_then_requests_without_a_second_question_then_pending_then_cancel():
    due = "2026-11-24T10:00:00+00:00"
    pending = {"status": 200, "body": {"has_pin": True, "unlocked": False, "reset_due_at": due}}
    out = _run(f"""replies = [{json.dumps(_ON)}]; await loadFactsPinSettings(); pinShow("forgot"); const explained = shown();
      replies = [{{status: 200, body: {{status: "pending"}}}}, {json.dumps(pending)}]; await requestPinReset();
      const requested = [asked, posts().map((c) => c.url), shown(), els["pin-chip-text"].textContent,
        els["pin-reset-note"].textContent.includes("2026"), els["pin-reset-cancel"].classList.contains("hidden"),
        els["pin-logout-offer"].classList.contains("hidden")];
      replies = [{{status: 200, body: {{status: "cancelled"}}}}, {json.dumps(_ON)}]; await cancelPinReset();
      return [explained, requested, posts().at(-1).url, shown(), msg(), els["pin-chip-text"].textContent];""")
    assert out[0] == ["forgot"]
    assert out[1] == [0, ["/api/user/facts-pin/reset"], ["pending"], "Resetting", True, False, False]
    assert out[2:] == ["/api/user/facts-pin/reset/cancel", ["on"], ["Reset cancelled.", "ok", False], "On"]


@_node
def test_the_memory_gate_still_asks_before_it_requests_a_reset():
    out = _run("""answer = false; await forgotPin(); const declined = posts().length;
      answer = true; replies = [{status: 403, body: {error: "reset_refused"}}]; await forgotPin();
      return [declined, asked, posts().length, msg()[0], els["facts-pin-error"].textContent];""")
    assert out == [0, 2, 1, _dict("en")["pin.err_reset_refused"], _dict("en")["pin.err_reset_refused"]]


@_node
def test_a_status_refresh_keeps_a_step_that_holds_codes_and_settles_a_resting_one():
    out = _run(f"""pinShow("set1"); typed([["pin-new", "0001"]]);
      replies = [{json.dumps(_OFF)}]; await loadFactsPinSettings(); const kept = [shown(), els["pin-new"].value];
      pinShow("forgot"); replies = [{json.dumps(_ON)}]; await loadFactsPinSettings();
      return [kept, shown()];""")
    assert out == [[["set1"], "0001"], ["on"]]


@_node
def test_one_row_opens_at_a_time_and_closing_the_pin_row_drops_the_flow_and_its_codes():
    out = _run("""_openSettingsRow = ""; toggleSettingsRow("pin"); pinShow("set1"); typed([["pin-new", "0001"]]);
      const open = [els["srow-pin"].attrs["aria-expanded"], els["srow-pin-body"].classList.contains("hidden"),
        els["srow-pin"].classList.contains("open")];
      toggleSettingsRow("tz");
      const switched = [els["srow-pin"].attrs["aria-expanded"], els["srow-pin-body"].classList.contains("hidden"),
        els["srow-tz"].attrs["aria-expanded"], shown(), els["pin-new"].value];
      toggleSettingsRow("tz");
      return [open, switched, els["srow-tz"].attrs["aria-expanded"], els["srow-tz-body"].classList.contains("hidden")];""")
    assert out == [["true", False, True], ["false", True, "true", ["off"], ""], "false", True]


# The language rows, the address a page was opened with, the connection rows' renderers

_SETTINGS_DOM = r"""
const EN = %(en)s;
let _currentLang = "en";
const t = (k) => EN[k] || k;
const els = new Proxy({}, {get: (o, id) => o[id] ??= (() => {
  const classes = new Set(); const attrs = {};
  return {id, value: "", textContent: "", innerHTML: "", className: "", options: [], selectedIndex: -1,
    setAttribute(k, v) { attrs[k] = v; }, getAttribute(k) { return attrs[k] ?? null; }, attrs,
    classList: {add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); }}};
})()});
els["lang-ui-select"].options = [{textContent: "System default"}, {textContent: "Українська"}, {textContent: "English"},
                                 {textContent: "Français"}, {textContent: "Español"}];
Object.defineProperty(els["lang-ui-select"], "value", {set(v) { this.selectedIndex = ["", "uk", "en", "fr", "es"].indexOf(v); }});
const document = {getElementById: (id) => els[id], querySelectorAll: () => []};
const API = "";
const calls = [], toasts = [];
let replies = [], currentUser = null, _activeSection = "settings";
const showToast = (m, kind) => toasts.push(kind || "success");
const applyTranslations = () => {};
const isOwner = () => !!currentUser && currentUser.account.role === "owner";
const authFetch = async (url, init) => {
  calls.push({url, method: (init && init.method) || "GET", body: init && init.body ? JSON.parse(init.body) : null});
  const r = replies.shift() || {status: 200, body: {}};
  return {ok: r.status < 400, status: r.status, json: async () => r.body};
};
let _settingsLoaded = true, _settingsLocation = "";
const _settingsCounts = {devices: null, passkeys: null};
let _langUiSelected = "";
let _langMirrorSelected = true;
"""


def _run_settings(body: str, functions: list, consts: list = (), search: str = ""):
    sources = f"const window = {{location: {{search: {json.dumps(search)}}}}};\n" + "\n".join(
        [_const(name) for name in consts] + [_function(name) for name in functions])
    script = (_SETTINGS_DOM % {"en": json.dumps(_dict("en"))}) + sources + "\n(async () => {\n" + body + \
        "\n})().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_LANGUAGE = ["setLangUI", "setLangMirror", "saveLanguage", "loadLanguage", "renderSettingsValues", "setSettingsValue",
             "plural"]


@_node
@pytest.mark.parametrize(("act", "sent", "reply_value"), [
    ('setLangMirror(false); setLangUI("fr")', {"preferred_language": "fr", "agent_mirror": False}, "Always in French"),
    ('setLangUI("uk"); setLangMirror(false)', {"preferred_language": "uk", "agent_mirror": False}, "Always in Ukrainian"),
    ('setLangUI("es"); setLangMirror(true)', {"preferred_language": "es", "agent_mirror": True}, "As I write"),
    ('setLangUI(""); setLangMirror(false)', {"preferred_language": None, "agent_mirror": False}, "Always in one language"),
], ids=["interface-row", "reply-row-fixed", "reply-row-mirror", "system-default"])
def test_either_language_row_sends_the_stored_pair_in_one_post(act, sent, reply_value):
    out = _run_settings(f"""{act}; await saveLanguage();
      return [calls, els["settings-reply-value"].textContent, els["lang-fixed-btn"].textContent,
        els["lang-mirror-btn"].attrs["aria-pressed"]];""", _LANGUAGE)
    assert out[0] == [{"url": "/api/user/language", "method": "POST", "body": sent}]
    assert out[1] == reply_value
    assert out[3] == ("true" if sent["agent_mirror"] else "false")


@_node
def test_a_refused_language_save_reads_the_stored_pair_back():
    out = _run_settings("""setLangUI("fr"); setLangMirror(false);
      replies = [{status: 400, body: {error: "unsupported"}}, {status: 200, body: {preferred_language: "en", agent_mirror: true}}];
      await saveLanguage();
      return [calls.map((c) => c.method), _langUiSelected, _langMirrorSelected, els["settings-uilang-value"].textContent,
        els["settings-reply-value"].textContent];""", _LANGUAGE)
    assert out == [["POST", "GET"], "en", True, "English", "As I write"]


@_node
@pytest.mark.parametrize(("search", "owner", "row"), [
    ("?connected=gmail", False, {"row": "mail", "group": "connections"}),
    ("?connect_error=scope&kind=microsoft_todo", False, {"row": "todo", "group": "connections"}),
    ("?connected=google_drive", False, {"row": "", "group": ""}),
    ("?connected=unknown_kind", False, {"row": "", "group": "connections"}),
    ("?tab=devices", False, {"row": "passkeys", "group": "security"}),
    ("?tab=team", True, {"row": "team", "group": "more"}),
    ("?tab=team", False, {"row": "", "group": ""}),
    ("?tab=settings", False, {"row": "", "group": ""}),
])
def test_the_address_the_page_was_opened_with_opens_its_row(search, owner, row):
    connectors = [{"kind": "gmail", "integration": "mail"}, {"kind": "microsoft_todo", "integration": "tasks"},
                  {"kind": "google_drive", "integration": "drive"}]
    sources = (f"const window = {{location: {{search: {json.dumps(search)}}}}};\n"
               f"const loadConnectors = async () => {json.dumps(connectors)};\n"
               + "\n".join([_const("SETTINGS_ENTRY"), _const("CONNECTION_ROWS"), _function("settingsEntryRow")]))
    script = (_SETTINGS_DOM % {"en": "{}"}) + sources + f"""
      currentUser = {{account: {{role: {json.dumps("owner" if owner else "member")}}}}};
      settingsEntryRow().then((out) => process.stdout.write(JSON.stringify(out)));"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == row


_PLATFORMS = ["renderPlatforms", "platformItem", "setSettingsChip", "escapeHtml"]


@_node
def test_the_platforms_row_counts_its_three_ids_and_offers_link_or_unlink_for_each():
    hostile = '<img src=x onerror="alert(1)">'
    out = _run_settings(f"""renderPlatforms({{telegram: {json.dumps(hostile)}, phone: "+1 555 0100"}});
      return [els["settings-platforms-chip"].className, els["settings-platforms-chip-text"].textContent,
        els["platforms-container"].innerHTML];""", _PLATFORMS)
    chip, count, html = out
    assert (chip, count) == ("c-chip c-chip-ok", "2 of 3")
    assert "unlinkPlatform('telegram')" in html and "unlinkPlatform('phone')" in html
    assert "linkPlatform('slack', 'Slack')" in html and 'id="input-slack"' in html
    assert "<img" not in html and "&#60;img" in html and "+1 555 0100" in html
    empty = _run_settings("""renderPlatforms({}); return [els["settings-platforms-chip"].className,
      els["settings-platforms-chip-text"].textContent, els["platforms-container"].innerHTML];""", _PLATFORMS)
    assert empty[:2] == ["c-chip c-chip-off", "0 of 3"]
    assert "linkPlatform('telegram', 'Telegram')" in empty[2] and "linkPlatform('phone'" not in empty[2]
    assert "unlinkPlatform" not in empty[2]


_GMAIL = ["renderGmailStatus", "setSettingsChip", "connectButton", "escapeHtml"]


@_node
@pytest.mark.parametrize(("data", "chip", "present", "absent"), [
    ({"connected": True, "indexed_through": "2026-10-01", "active_job_id": "j1", "indexing_active": True,
      "daily_review_enabled": True, "daily_review_hour": 7},
     "c-chip c-chip-ok", ["saveDailyReview(", "saveGmailAutoIndex(", "deleteGmailData()", "disconnectGmail()",
                          'data-job-id="j1"', 'id="dr-toggle" checked'], ["/auth/connect/"]),
    ({"connected": True, "needs_reconnect": True, "reconnect_href": "/auth/connect/mailkind?credential=c1"},
     "c-chip c-chip-warn", ['href="/auth/connect/mailkind?credential=c1" class="c-btn c-pri"', "disconnectGmail()"],
     ["triggerGmailIndex()"]),
    ({"connected": False, "reconnect_needed": True, "connect_href": "/auth/connect/mailkind"},
     "c-chip c-chip-warn", ['href="/auth/connect/mailkind" class="c-btn c-pri"', "c-box-danger"], ["disconnectGmail()"]),
    ({"connected": False, "connect_href": "/auth/connect/mailkind"},
     "c-chip c-chip-off", ['href="/auth/connect/mailkind" class="c-btn c-pri"'], ["disconnectGmail()", "saveDailyReview("]),
], ids=["works", "token-revoked", "connection-gone", "not-connected"])
def test_the_gmail_row_chip_and_actions_follow_the_status(data, chip, present, absent):
    out = _run_settings(f"""renderGmailStatus({json.dumps(data)});
      return [els["settings-gmail-chip"].className, els["gmail-container"].innerHTML];""", _GMAIL)
    assert out[0] == chip
    for text in present:
        assert text in out[1], text
    for text in absent:
        assert text not in out[1], text


@_node
@pytest.mark.parametrize(("data", "chip", "action"), [
    ({"connected": True}, "c-chip c-chip-ok", "disconnectService('tasks')"),
    ({"connected": False, "reconnect_needed": True, "connect_href": "/auth/connect/todokind"}, "c-chip c-chip-warn",
     'href="/auth/connect/todokind" class="c-btn c-pri">Reconnect To Do</a>'),
    ({"connected": False, "connect_href": "/auth/connect/todokind"}, "c-chip c-chip-off",
     'href="/auth/connect/todokind" class="c-btn c-pri">Connect Microsoft To Do</a>'),
], ids=["connected", "reconnect", "connect"])
def test_the_to_do_row_offers_connect_reconnect_or_disconnect(data, chip, action):
    out = _run_settings(f"""renderServiceStatus("tasks", "Microsoft To Do", "desc", {json.dumps(data)});
      return [els["settings-todo-chip"].className, els["tasks-container"].innerHTML];""",
                        ["renderServiceStatus", "setSettingsChip", "connectButton", "escapeHtml"])
    assert out[0] == chip and action in out[1]


@_node
@pytest.mark.parametrize(("lang", "n", "text"), [
    ("en", 1, "1 key"), ("en", 3, "3 keys"), ("uk", 1, "1 ключ"), ("uk", 3, "3 ключі"), ("uk", 5, "5 ключів"),
    ("fr", 2, "2 clés"), ("es", 1, "1 llave"),
])
def test_the_counts_of_the_security_rows_take_the_plural_form_of_the_language(lang, n, text):
    script = (f"const LANG = {json.dumps(_dict(lang))};\nlet _currentLang = {json.dumps(lang)};\n"
              "const t = (k) => LANG[k] || k;\n" + _function("plural") +
              f"\nprocess.stdout.write(JSON.stringify(plural('passkeys.count', {n})));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == text
