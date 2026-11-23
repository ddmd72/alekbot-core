"""Cabinet -> the facts PIN (CABINET_DEVICE_SESSIONS_RFC §7.7): the settings card, the Memory tab's gate, the relock on
leaving, every string in four languages, and the page logic run under node when it is installed. The page only asks:
a code is sent once and cleared, never kept; every text is set as text. Codes in this file are `0001` and `0002` only."""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.web.cabinet_source import cabinet_source

_HTML = cabinet_source()
_EN: dict = {}
for _m in re.finditer(r'"(pin\.[a-z_0-9]+)": "([^"]+)"', _HTML):
    _EN.setdefault(_m.group(1), _m.group(2))     # the English block comes first
_KEYS = sorted(_EN)
_FUNCTIONS = ["pinValue", "clearPinFields", "pinRequest", "loadFactsPin", "pinErrorText", "pinMessage", "pinWhen",
              "renderPinReset", "renderPinSettings", "loadFactsPinSettings", "onAccountLocked", "pinFailed",
              "afterPinChange", "submitPinSet", "submitPinChange", "submitPinRemove", "forgotPin", "cancelPinReset",
              "showFactsGate", "hideFactsGate", "enterMemory", "unlockFacts", "factsFetch", "forgetFacts",
              "relockFacts"]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _function(name: str) -> str:
    match = re.search(r"\n(\s*(?:async )?function " + name + r"\(.*?\n      \})\n", _HTML, re.S)
    assert match, name
    return match.group(1)


def _block() -> str:
    start = _HTML.index("/* ── Facts PIN (CABINET_DEVICE_SESSIONS_RFC")
    return _HTML[start:_HTML.index("/* ── Settings ──", start)]


def test_every_pin_string_is_in_all_four_languages():
    assert len(_KEYS) >= 40
    for key in _KEYS:
        assert len(re.findall(r'"' + re.escape(key) + r'": "[^"]+"', _HTML)) == 4, key


def test_the_error_strings_cover_every_answer_the_server_gives():
    for error in ["mismatch", "not_four_digits", "trivial", "duress_same_as_pin", "wrong_code", "already_set", "no_pin",
                  "invalid_body", "session_ended", "reset_refused", "no_reset_pending"]:
        assert f"pin.err_{error}" in _EN, error
    assert "pin.account_locked" in _EN and _EN["pin.account_locked"] == "Account locked."


def test_the_static_elements_are_translated_and_exist():
    for selector, key in re.findall(r'"(#(?:facts-)?pin-[a-z0-9-]+)":\s+"([a-z_.]+)"', _HTML):
        assert f'id="{selector[1:]}"' in _HTML, selector
        assert re.search(r'"' + re.escape(key) + r'": "', _HTML), key


def test_the_settings_card_has_set_change_remove_and_forgot():
    for handler in ["submitPinSet()", "submitPinChange()", "submitPinRemove()", "forgotPin()", "cancelPinReset()",
                    "logoutOtherDevices()", "unlockFacts()"]:
        assert f'onclick="{handler}"' in _HTML or handler == "unlockFacts()" and "unlockFacts();" in _HTML, handler
    assert _HTML.count('type="password" inputmode="numeric"') >= 11
    assert 'if (name === "settings") { loadSettings(); loadFactsPinSettings(); }' in _HTML


def test_the_memory_tab_enters_through_the_gate_and_leaving_relocks():
    assert 'if (name === "memory") enterMemory();' in _HTML
    assert 'if (_activeSection === "memory" && name !== "memory") relockFacts();' in _HTML
    assert 'window.addEventListener("pagehide", relockFacts);' in _HTML
    assert "navigator.sendBeacon(`${API}/api/user/facts-pin/relock`" in _HTML
    assert "#section-memory.pin-locked > :not(.hero-header):not(#facts-pin-gate)" in _HTML


def test_every_facts_call_goes_through_the_guarded_fetch():
    calls = re.findall(r"(\w+)\(\s*`\$\{API\}/api/user/fact(?!s-pin)", _HTML)
    assert sorted(calls) == ["factsFetch"] * 8, calls


def test_the_pin_code_never_reaches_the_dom_as_markup_or_a_url():
    block = _block()
    assert "innerHTML" not in block.replace('.innerHTML = "";', "")
    assert "localStorage" not in block and "sessionStorage" not in block and "console." not in block
    assert not re.search(r"facts-pin[^`\n]*\$\{", block.replace("${API}/api/user/facts-pin${path}", "")
                         .replace("${API}/api/user/facts-pin/relock", ""))


# ---------------------------------------------------------------- the page logic under node

_PREAMBLE = r"""
const EN = %(en)s;
const t = (k) => EN[k] || k;
const els = new Proxy({}, {get: (o, id) => o[id] ??= (() => {
  const hidden = new Set(); const classes = new Set();
  return {value: "", textContent: "", disabled: false, style: {}, innerHTML: "x",
    classList: {add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); }}};
})()});
const document = {getElementById: (id) => els[id]};
const API = "";
const calls = [];
let browsed = 0, beacons = [], asked = true, replies = [];
const browseInit = () => { browsed += 1; };
const confirm = () => asked;
const navigator = {sendBeacon: (url, body) => { beacons.push(url); return true; }};
const Blob = function (parts, opts) { this.type = opts.type; };
const window = {addEventListener: () => {}};
let factsMode = "idle", _activeSection = "memory";
const logoutOtherDevices = () => {};
const authFetch = async (url, init) => {
  calls.push({url, method: init && init.method, body: init && init.body ? JSON.parse(init.body) : null});
  const r = replies.shift() || {status: 200, body: {}};
  return {ok: r.status < 400, status: r.status, json: async () => r.body, clone() { return this; }};
};
%(sources)s
const cls = (id) => ["hidden", "pin-locked"].filter((c) => els[id].classList.contains(c));
"""


def _run(body: str):
    sources = "const NO_PIN = { has_pin: false, unlocked: false, reset_due_at: null, can_request_reset: false };\n" \
              "let factsPin = { ...NO_PIN };\n" \
              'const PIN_FIELDS = ["pin-new", "pin-new2", "pin-duress", "pin-duress2", "pin-current-change", ' \
              '"pin-new-c", "pin-new2-c", "pin-duress-c", "pin-duress2-c", "pin-current-remove", "facts-pin-input"];\n' \
              + "\n".join(_function(name) for name in _FUNCTIONS)
    script = (_PREAMBLE % {"en": json.dumps(_EN), "sources": sources}) + "(async () => {\n" + body + "\n})().then(" \
             "(out) => process.stdout.write(JSON.stringify(out)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_error_texts_say_what_the_server_said():
    out = _run("""return [pinErrorText({error: "wrong_code", attempts_left: 3}), pinErrorText({error: "mismatch"}),
      pinErrorText({error: "from_the_future"}), pinErrorText({error: "account_locked"}), pinErrorText(null)];""")
    assert out == ["Wrong PIN. Attempts left: 3.", _EN["pin.err_mismatch"], _EN["pin.err_failed"],
                   "Account locked.", _EN["pin.err_failed"]]


@needs_node
def test_a_locked_tab_shows_the_gate_and_does_not_load_the_facts():
    out = _run("""replies = [{status: 200, body: {has_pin: true, unlocked: false, reset_due_at: null}}];
      await enterMemory(); return [cls("section-memory"), browsed];""")
    assert out == [["pin-locked"], 0]


@needs_node
def test_a_tab_without_a_pin_or_already_unlocked_loads_the_facts():
    out = _run("""replies = [{status: 200, body: {has_pin: false, unlocked: false}}];
      await enterMemory(); const plain = [cls("section-memory"), browsed];
      factsMode = "idle"; replies = [{status: 200, body: {has_pin: true, unlocked: true}}];
      await enterMemory(); return [plain, browsed];""")
    assert out == [[[], 1], 2]


@needs_node
def test_a_failed_status_read_loads_the_tab_and_leaves_the_guard_to_the_server():
    out = _run("""replies = [{status: 500, body: {}}]; await enterMemory(); return browsed;""")
    assert out == 1


@needs_node
def test_leaving_before_the_status_arrived_does_nothing():
    out = _run("""replies = [{status: 200, body: {has_pin: true, unlocked: false}}]; _activeSection = "devices";
      await enterMemory(); return [cls("section-memory"), browsed];""")
    assert out == [[], 0]


@needs_node
def test_a_right_pin_opens_the_tab_and_the_code_leaves_the_field():
    out = _run("""els["facts-pin-input"].value = "0001"; els["section-memory"].classList.add("pin-locked");
      replies = [{status: 200, body: {status: "unlocked"}}]; await unlockFacts();
      return [calls, els["facts-pin-input"].value, cls("section-memory"), factsPin.unlocked, browsed];""")
    assert out == [[{"url": "/api/user/facts-pin/unlock", "method": "POST", "body": {"pin": "0001"}}], "", [], True, 1]


@needs_node
def test_a_wrong_pin_says_how_many_attempts_are_left():
    out = _run("""els["facts-pin-input"].value = "0002"; replies = [{status: 400, body: {error: "wrong_code", attempts_left: 2}}];
      await unlockFacts(); return [els["facts-pin-error"].textContent, els["facts-pin-input"].value, browsed];""")
    assert out == ["Wrong PIN. Attempts left: 2.", "", 0]


@needs_node
def test_a_lock_answer_shows_account_locked_and_disables_the_gate():
    out = _run("""replies = [{status: 423, body: {error: "account_locked"}}]; await unlockFacts();
      return [els["facts-pin-error"].textContent, els["pin-message"].textContent, els["facts-pin-input"].disabled,
        els["facts-pin-unlock"].disabled, cls("section-memory")];""")
    assert out == ["Account locked.", "Account locked.", True, True, ["pin-locked"]]


@needs_node
def test_a_facts_call_refused_as_locked_closes_the_tab_and_forgets_the_facts():
    out = _run("""factsPin = {has_pin: true, unlocked: true}; factsMode = "browse"; els["facts-container"].innerHTML = "secret";
      replies = [{status: 403, body: {error: "facts_locked"}}];
      const res = await factsFetch("/api/user/facts"); 
      return [res.status, factsPin.unlocked, factsMode, els["facts-container"].innerHTML, cls("section-memory")];""")
    assert out == [403, False, "idle", "", ["pin-locked"]]


@needs_node
def test_another_refusal_or_a_success_leaves_the_tab_alone():
    out = _run("""factsMode = "browse"; replies = [{status: 403, body: {error: "Not authorized"}}, {status: 200, body: {}}];
      await factsFetch("/api/user/facts/1/invalidate"); await factsFetch("/api/user/facts");
      return [factsMode, cls("section-memory")];""")
    assert out == ["browse", []]


@needs_node
def test_a_locked_answer_in_another_section_does_not_raise_the_gate():
    out = _run("""_activeSection = "settings"; replies = [{status: 403, body: {error: "facts_locked"}}];
      await factsFetch("/api/user/facts"); return cls("section-memory");""")
    assert out == []


@needs_node
def test_leaving_the_tab_relocks_by_beacon_only_when_it_was_unlocked():
    out = _run("""relockFacts(); const none = beacons.length;
      factsPin = {has_pin: true, unlocked: false}; relockFacts(); const locked = beacons.length;
      factsPin = {has_pin: true, unlocked: true}; factsMode = "browse"; relockFacts();
      return [none, locked, beacons, factsPin.unlocked, factsMode];""")
    assert out == [0, 0, ["/api/user/facts-pin/relock"], False, "idle"]


@needs_node
def test_a_broken_beacon_is_not_an_error():
    out = _run("""navigator.sendBeacon = () => { throw new Error("blocked"); };
      factsPin = {has_pin: true, unlocked: true}; relockFacts(); return factsPin.unlocked;""")
    assert out is False


@needs_node
def test_setting_a_pin_sends_each_code_twice_clears_the_fields_and_reloads_the_status():
    out = _run("""for (const [id, v] of [["pin-new", "0001"], ["pin-new2", "0001"], ["pin-duress", "0002"], ["pin-duress2", "0002"]])
        els[id].value = v;
      replies = [{status: 200, body: {status: "done"}}, {status: 200, body: {has_pin: true, unlocked: false}}];
      await submitPinSet();
      return [calls[0], els["pin-new"].value, els["pin-message"].textContent, cls("pin-manage"), cls("pin-set-form"),
        cls("pin-logout-offer")];""")
    assert out[0] == {"url": "/api/user/facts-pin/set", "method": "POST", "body": {
        "pin": "0001", "pin_confirm": "0001", "duress": "0002", "duress_confirm": "0002"}}
    assert out[1:] == ["", "PIN set.", [], ["hidden"], ["hidden"]]


@needs_node
def test_changing_the_pin_sends_the_current_one_and_offers_to_log_out_the_others():
    out = _run("""els["pin-current-change"].value = "0001";
      for (const [id, v] of [["pin-new-c", "0002"], ["pin-new2-c", "0002"], ["pin-duress-c", "0001"], ["pin-duress2-c", "0001"]])
        els[id].value = v;
      replies = [{status: 200, body: {status: "done"}}, {status: 200, body: {has_pin: true}}];
      await submitPinChange(); return [calls[0].body, cls("pin-logout-offer"), els["pin-message"].textContent];""")
    assert out[0] == {"current": "0001", "pin": "0002", "pin_confirm": "0002", "duress": "0001",
                      "duress_confirm": "0001"}
    assert out[1:] == [[], "PIN changed."]


@needs_node
def test_removing_the_pin_sends_the_current_one_and_a_refusal_shows_its_text():
    out = _run("""els["pin-current-remove"].value = "0002";
      replies = [{status: 400, body: {error: "wrong_code", attempts_left: 4}}]; await submitPinRemove();
      return [calls[0].body, els["pin-message"].textContent, els["pin-message"].style.color, els["pin-current-remove"].value];""")
    assert out == [{"current": "0002"}, "Wrong PIN. Attempts left: 4.", "var(--danger-text)", ""]


@needs_node
def test_a_lock_while_changing_the_pin_shows_account_locked():
    out = _run("""replies = [{status: 423, body: {error: "account_locked"}}]; await submitPinChange();
      return els["pin-message"].textContent;""")
    assert out == "Account locked."


@needs_node
def test_forgot_pin_asks_first_then_requests_the_reset_and_offers_the_logout():
    out = _run("""asked = false; await forgotPin(); const declined = calls.length; asked = true;
      replies = [{status: 200, body: {status: "pending"}}, {status: 200, body: {has_pin: true,
        reset_due_at: "2026-11-24T10:00:00+00:00"}}];
      await forgotPin();
      return [declined, calls[0].url, els["pin-message"].textContent, cls("pin-logout-offer"),
        els["pin-reset-note"].textContent.includes("2026") || els["pin-reset-note"].textContent.includes("24"),
        cls("pin-reset-cancel"), cls("pin-forgot"), cls("facts-pin-forgot")];""")
    assert out == [0, "/api/user/facts-pin/reset", "Reset requested.", [], True, [], ["hidden"], ["hidden"]]


@needs_node
def test_a_refused_reset_says_to_sign_in_with_google_again_in_both_places():
    out = _run("""replies = [{status: 403, body: {error: "reset_refused"}}]; await forgotPin();
      return [els["pin-message"].textContent, els["facts-pin-error"].textContent];""")
    assert out == [_EN["pin.err_reset_refused"]] * 2


@needs_node
def test_a_lock_on_a_reset_request_shows_account_locked():
    out = _run("""replies = [{status: 423, body: {error: "account_locked"}}]; await forgotPin();
      return els["pin-message"].textContent;""")
    assert out == "Account locked."


@needs_node
def test_cancelling_a_reset_clears_the_notice_and_a_refusal_is_shown():
    out = _run("""factsPin = {has_pin: true, reset_due_at: "2026-11-24T10:00:00+00:00"}; renderPinReset();
      const shown = cls("pin-reset-note");
      replies = [{status: 200, body: {status: "cancelled"}}, {status: 200, body: {has_pin: true, reset_due_at: null}}];
      await cancelPinReset(); const after = [els["pin-message"].textContent, cls("pin-reset-note"), cls("pin-forgot")];
      replies = [{status: 409, body: {error: "no_reset_pending"}}]; await cancelPinReset();
      return [shown, after, els["pin-message"].textContent];""")
    assert out == [[], ["Reset cancelled.", ["hidden"], []], _EN["pin.err_no_reset_pending"]]


@needs_node
def test_the_settings_card_shows_the_set_form_without_a_pin_and_the_manage_form_with_one():
    out = _run("""replies = [{status: 200, body: {has_pin: false}}]; await loadFactsPinSettings();
      const none = [cls("pin-set-form"), cls("pin-manage"), els["pin-state"].textContent];
      replies = [{status: 200, body: {has_pin: true}}]; await loadFactsPinSettings();
      return [none, [cls("pin-set-form"), cls("pin-manage"), els["pin-state"].textContent]];""")
    assert out == [[[], ["hidden"], _EN["pin.state_none"]], [["hidden"], [], _EN["pin.state_set"]]]
