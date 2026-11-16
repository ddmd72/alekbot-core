"""The call pages show their texts in the owner's UI language from his settings (fetched from the gated
`/api/voice/ui-language`), not the phone's. Order until the fetch answers: the last server language stored in
localStorage, the phone language when the page has it, English. A different answer re-renders the visible texts.
The key checks read both pages; the behavioural ones run the page's own blocks under node (skipped without node)."""
import json
import re
from pathlib import Path

import pytest

from tests.unit.web.call_page_harness import FAKE_DOM, HTML, block, live_text_block, needs_node, run_node, show_view_block
from tests.unit.web.test_call_page_save_as_file import save_block

MODES_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call_modes.html").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
PAGES = {"call.html": HTML, "call_modes.html": MODES_HTML}


def _dict_keys(html: str, lang: str) -> set[str]:
    section = re.search(r"\n        " + lang + r": \{\n(.*?)\n        \},", html, re.S)
    assert section, lang
    return set(re.findall(r'^\s{10}"([\w.]+)":', section.group(1), re.M))


def _script(html: str) -> str:
    return html[html.index("<script>"):]


# ---------------------------------------------------------------- key coverage

@pytest.mark.parametrize("name", PAGES)
def test_the_four_dictionaries_hold_the_same_keys(name):
    en = _dict_keys(PAGES[name], "en")
    assert en
    for lang in LANGS:
        assert _dict_keys(PAGES[name], lang) == en, lang


@pytest.mark.parametrize("name", PAGES)
def test_every_key_the_page_can_pass_to_t_exists_in_every_language(name):
    """A key used in code is a quoted `area.name` literal outside the dictionaries (covers t("..."), the ternaries,
    the key tables and the `mode.${mode}` template through the mode names)."""
    html = PAGES[name]
    code = re.sub(r"const LANG_DICT = \{.*?\n      \};", "", _script(html), flags=re.S)
    used = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', code))
    for template in re.findall(r"t\(`([^`]+)`\)", code):  # t(`mode.${mode}.title`): expanded over the two modes
        used |= {re.sub(r"\$\{[^}]+\}", m, template) for m in ("interpreter", "tutor")}
    # Keys that are not dictionary keys (a URL path, a storage key) never match `area.name` with a dictionary area.
    areas = {k.split(".")[0] for k in _dict_keys(html, "en")}
    used = {k for k in used if k.split(".")[0] in areas}
    assert used, name
    for lang in LANGS:
        assert used <= _dict_keys(html, lang), (lang, used - _dict_keys(html, lang))


def test_every_dictionary_key_of_the_call_page_is_used_in_code():
    code = re.sub(r"const LANG_DICT = \{.*?\n      \};", "", _script(HTML), flags=re.S)
    dynamic = {"mode.interpreter", "mode.tutor"}  # t(`mode.${window.VOICE_MODE}`)
    for key in _dict_keys(HTML, "en") - dynamic:
        assert f'"{key}"' in code, key


# ---------------------------------------------------------------- no English literal outside t()

def test_no_user_visible_literal_is_set_outside_t_after_load():
    code = re.sub(r"const LANG_DICT = \{.*?\n      \};", "", _script(HTML), flags=re.S)
    assert not re.search(r'textContent = ["\'`][^"\'`$]*[A-Za-z]', code)
    assert not re.search(r'setState\(["\'`]', code)
    assert not re.search(r'setAttribute\("aria-label", ["\'`]', code)


def test_the_static_english_placeholders_are_replaced_on_init_in_every_mode():
    """The state line, the Call label, the back link and the mic texts ship in English in the HTML; the init lines
    replace each of them whatever the mode (they are not behind a mode check)."""
    init = HTML[HTML.rindex("      applyTranslations();"):]
    assert 'setState(t("state.idle"));' in init and 'renderButton("idle");' in init
    translations = block("      function applyTranslations() {", "\n      }")
    for id_ in ("back-link", "mic-summary", "mic-hint"):
        assert re.search(r'getElementById\("' + id_ + r'"\)\.textContent = [^;]*\bt\(', translations), id_
    assert "document.title = t(" in translations and "renderModeLabel();" in translations
    assert 'callLabel.textContent = t("button.call")' in HTML
    modes_init = MODES_HTML[MODES_HTML.rindex("      render();"):]
    assert modes_init.startswith("      render();\n      syncServerLanguage();")
    render = re.search(r"      function render\(\) \{.*?\n      \}", MODES_HTML, re.S).group(0)
    for id_ in ("title", "back-link"):
        assert re.search(r'getElementById\("' + id_ + r'"\)\.textContent = [^;]*\bt\(', render), id_


def test_apply_language_re_renders_every_translated_place():
    body = block("      function applyLanguage(previous) {", "\n      }\n")
    for call in ("applyTranslations();", "relabelState(previous);", 'callLabel.textContent', "refreshMicList();",
                 "renderAsk();", "renderSpeaker();", "liveTextRelabel();", "showRelabel();", "saveRelabel();"):
        assert call in body, call


# ---------------------------------------------------------------- behaviour (call.html)

def _blocks() -> str:
    dictionary = block("      const LANG_DICT = {", "\n      };")
    language = block("      /* ── UI language ──", "      async function syncServerLanguage() {").removesuffix(
        "      async function syncServerLanguage() {")
    sync = block("      async function syncServerLanguage() {", "\n      }\n")
    show_relabel = block("      function showRelabel() {", "\n      }\n")
    change = block("      function relabelState(previous) {", "\n      }\n") + "\n" + \
        block("      function applyLanguage(previous) {", "\n      }\n")
    return "\n".join([dictionary, language, sync, live_text_block(), show_view_block() or "", show_relabel, save_block(), change])


_STUBS = """
const stub = { applied: 0, mic: 0 };
function applyTranslations() { stub.applied++; }
const stateEl = { textContent: LANG_DICT.en["state.idle"] };
function setState(text) { stateEl.textContent = text; }
const btn = { classList: { contains: () => false } };
const callLabel = { textContent: LANG_DICT.en["button.call"] };
function refreshMicList() { stub.mic++; }
const ask = { btn: null };
function renderAsk() { stub.asked = (stub.asked || 0) + 1; }
function renderSpeaker() { stub.speaker = (stub.speaker || 0) + 1; }
"""


def _run(body: str, *, phone="en-US", stored=None, server=None, storage_throws=False, mode="interpreter") -> dict:
    """`server`: the ui-language reply ({status, body}), or "reject" for a network error; the save route answers ok."""
    prelude = f"""
const window = {{ VOICE_MODE: {json.dumps(mode)} }};
const navigator = {{ language: {json.dumps(phone)} }};
const storage = {{ data: {json.dumps({} if stored is None else {"voice_ui_language": stored})} }};
const localStorage = {{
  getItem: (k) => {{ if ({json.dumps(storage_throws)}) throw new Error("denied"); return k in storage.data ? storage.data[k] : null; }},
  setItem: (k, v) => {{ if ({json.dumps(storage_throws)}) throw new Error("denied"); storage.data[k] = v; }},
}};
const server = {json.dumps(server)};
const fetched = [];
globalThis.fetch = async (url, init) => {{
  fetched.push({{ url, init }});
  if (url === "/api/voice/ui-language") {{
    if (server === "reject") throw new Error("offline");
    return {{ ok: server.status === 200, status: server.status, json: async () => server.body }};
  }}
  return {{ ok: true, status: 200, json: async () => ({{ ok: true, path: "Inbox/Call.md" }}) }};
}};
const tick = () => new Promise((r) => setTimeout(r, 0));
"""
    script = "\n".join([prelude, FAKE_DOM, "const SCREEN = buildSkeleton(true);",
                        _dictionary_only(), _STUBS, _blocks_without_dict(), body])
    return run_node(script)


def _dictionary_only() -> str:
    return block("      const LANG_DICT = {", "\n      };")


def _blocks_without_dict() -> str:
    return _blocks().replace(_dictionary_only(), "", 1)


def _out(**expr: str) -> str:
    return "console.log(JSON.stringify({" + ", ".join(f"{k}: ({v})" for k, v in expr.items()) + "}));"


_OK = {"status": 200, "body": {"language": "uk"}}


@needs_node
def test_the_stored_language_is_used_before_the_phone_language():
    out = _run(_out(lang="_currentLang"), phone="fr-FR", stored="es")
    assert out["lang"] == "es"


@needs_node
def test_the_phone_language_is_used_when_nothing_is_stored_and_the_page_has_it():
    assert _run(_out(lang="_currentLang"), phone="fr-FR")["lang"] == "fr"


@needs_node
def test_english_when_neither_the_store_nor_the_phone_gives_a_language_the_page_has():
    assert _run(_out(lang="_currentLang"), phone="pt-BR", stored="de")["lang"] == "en"


@needs_node
def test_a_localstorage_that_throws_does_not_break_the_page_and_the_server_language_still_applies():
    out = _run("await_ = syncServerLanguage();\n" + "await_.then(() => {" + _out(lang="_currentLang", applied="stub.applied") + "});",
               phone="fr-FR", server=_OK, storage_throws=True)
    assert out == {"lang": "uk", "applied": 1}


_AFTER = "syncServerLanguage().then(() => {" + "%s" + "});"


@needs_node
def test_the_server_language_is_stored_and_the_state_and_call_label_are_rendered_again():
    body = _AFTER % _out(
        lang="_currentLang", stored="storage.data.voice_ui_language", applied="stub.applied", mic="stub.mic",
        state="stateEl.textContent === LANG_DICT.uk['state.idle']",
        label="callLabel.textContent === LANG_DICT.uk['button.call']",
        asked="stub.asked || 0", speaker="stub.speaker", url="fetched[0].url", creds="fetched[0].init.credentials")
    out = _run(body, phone="en-US", server=_OK)
    assert out == {"lang": "uk", "stored": "uk", "applied": 1, "mic": 1, "state": True, "label": True, "asked": 0,
                   "speaker": 1, "url": "/api/voice/ui-language", "creds": "same-origin"}


@needs_node
def test_a_hangup_button_and_any_state_key_follow_the_language():
    body = ("btn.classList.contains = () => true; callLabel.textContent = LANG_DICT.en['button.hangup'];"
            "stateEl.textContent = LANG_DICT.en['state.connecting'];\n"
            + _AFTER % _out(label="callLabel.textContent === LANG_DICT.uk['button.hangup']",
                            state="stateEl.textContent === LANG_DICT.uk['state.connecting']"))
    assert _run(body, server=_OK) == {"label": True, "state": True}


@needs_node
def test_a_state_text_that_is_not_a_dictionary_text_is_left_alone():
    body = "stateEl.textContent = 'a server note';\n" + _AFTER % _out(state="stateEl.textContent")
    assert _run(body, server=_OK) == {"state": "a server note"}


@needs_node
def test_the_same_language_is_stored_without_a_re_render():
    out = _run(_AFTER % _out(applied="stub.applied", stored="storage.data.voice_ui_language"),
               phone="uk-UA", server=_OK)
    assert out == {"applied": 0, "stored": "uk"}


@needs_node
@pytest.mark.parametrize("server", [
    {"status": 200, "body": {"language": "de"}},
    {"status": 200, "body": {"language": None}},
    {"status": 200, "body": {}},
    {"status": 200, "body": {"language": "constructor"}},
    {"status": 404, "body": {"error": "not found"}},
    {"status": 500, "body": {"error": "unavailable"}},
    "reject",
])
def test_an_unknown_language_or_a_failed_fetch_keeps_the_fallback(server):
    out = _run(_AFTER % _out(lang="_currentLang", applied="stub.applied", stored="storage.data.voice_ui_language || null"),
               phone="fr-FR", server=server)
    assert out == {"lang": "fr", "applied": 0, "stored": None}


_CALL = """
send(line("l1", "caller", "set", "We have a booking under RM-55120."));
send(line("l2", "lelik", "append", "Abbiamo una prenotazione con il codice RM-55120."));
send(endLine("l2", "done"));
"""


@needs_node
def test_a_late_server_language_re_renders_the_panel_show_view_save_row_and_open_sheet():
    body = f"""
showLiveText();
{_CALL}
openShow();
callFileToDrive = true;
endLiveText();
const chatBtn = () => document.body.querySelector(".save-row").querySelectorAll(".save-btn")[1];
chatBtn().click();
await_ = tick().then(() => syncServerLanguage()).then(() => {{
  const uk = LANG_DICT.uk;
  const row = document.body.querySelector(".save-row");
  const sheet = document.body.querySelector(".save-sheet");
  const view = document.body.querySelector(".show-view");
  const panel = document.body.querySelector(".text-panel");
  {_out(
        lang="_currentLang",
        panel_label="panel.getAttribute('aria-label') === uk['text.panel']",
        hint="panel.querySelector('.text-hint').textContent === uk['text.heard_hint']",
        you="document.body.querySelectorAll('.text-label').map((l) => l.textContent).every((x) => x === uk['text.you'])",
        show_label="document.body.querySelector('.show-label').textContent === uk['show.button']",
        show_aria="document.getElementById('show-btn').getAttribute('aria-label') === uk['show.button']",
        view_aria="view.getAttribute('aria-label') === uk['show.button']",
        view_back="view.querySelector('.show-back').textContent === uk['show.back']",
        view_flip="view.querySelector('.show-flip').textContent === uk['show.flip']",
        buttons="JSON.stringify(row.querySelectorAll('.save-btn').map((b) => b.textContent)) === JSON.stringify(['' + uk['save.copy'], '✓ ' + uk['save.chat'], uk['save.drive']])",
        sheet_text="sheet.querySelector('.save-sheet-text').textContent === uk['save.sent']",
        sheet_buttons="JSON.stringify(sheet.querySelectorAll('.save-sheet-btn').map((b) => b.textContent)) === JSON.stringify([uk['save.more'], uk['save.done']])",
        sheet_aria="sheet.getAttribute('aria-label') === uk['save.done']",
        sheets="document.body.querySelectorAll('.save-sheet').length")}
}});
"""
    out = _run(body, phone="en-US", server=_OK)
    assert out["lang"] == "uk" and out["sheets"] == 1
    assert all(v is True for k, v in out.items() if k not in ("lang", "sheets")), out


@needs_node
def test_the_inline_result_line_and_a_failed_sheet_follow_the_language():
    body = f"""
showLiveText();
{_CALL}
endLiveText();
globalThis.fetch = ((orig) => async (url, init) => url === "/api/voice/ui-language" ? orig(url, init)
  : {{ ok: false, status: 502, json: async () => ({{ ok: false, reason: "failed" }}) }})(globalThis.fetch);
saveSay("save.copied");
document.body.querySelector(".save-row").querySelectorAll(".save-btn")[1].click();
await_ = tick().then(() => syncServerLanguage()).then(() => {{
  const uk = LANG_DICT.uk;
  const sheet = document.body.querySelector(".save-sheet");
  {_out(
        say="document.body.querySelector('.save-result').textContent === uk['save.copied']",
        text="sheet.querySelector('.save-sheet-text').textContent === uk['save.failed']",
        buttons="JSON.stringify(sheet.querySelectorAll('.save-sheet-btn').map((b) => b.textContent)) === JSON.stringify([uk['save.back'], uk['save.done']])")}
}});
"""
    out = _run(body, server=_OK)
    assert out == {"say": True, "text": True, "buttons": True}


@needs_node
def test_a_late_language_with_no_panel_no_show_view_and_no_save_row_does_not_throw():
    out = _run(_AFTER % _out(lang="_currentLang"), mode=None, server=_OK)
    assert out == {"lang": "uk"}


# ---------------------------------------------------------------- behaviour (call_modes.html)

def _modes_run(*, phone="en-US", stored=None, server=None, storage_throws=False) -> dict:
    """Runs the whole modes page script against a fake DOM keyed by id."""
    script_src = re.search(r"<script>(.*)</script>", MODES_HTML, re.S).group(1)
    ids = ["title", "back-link", "interpreter-title", "interpreter-desc", "tutor-title", "tutor-desc"]
    prelude = f"""
const els = {{}}; for (const id of {json.dumps(ids)}) els[id] = {{ textContent: "" }};
const document = {{ title: "", getElementById: (id) => els[id] }};
const window = {{ location: {{ replace() {{}} }} }};
const navigator = {{ language: {json.dumps(phone)} }};
const data = {json.dumps({} if stored is None else {"voice_ui_language": stored})};
const localStorage = {{
  getItem: (k) => {{ if ({json.dumps(storage_throws)}) throw new Error("denied"); return k in data ? data[k] : null; }},
  setItem: (k, v) => {{ if ({json.dumps(storage_throws)}) throw new Error("denied"); data[k] = v; }},
}};
const server = {json.dumps(server)};
globalThis.fetch = async (url) => {{
  if (url === "/auth/me") return {{ ok: true, status: 200 }};
  if (server === "reject") throw new Error("offline");
  return {{ ok: server.status === 200, status: server.status, json: async () => server.body }};
}};
"""
    tail = """
const shown = () => [document.title, els.title.textContent, els["back-link"].textContent,
  els["interpreter-title"].textContent, els["interpreter-desc"].textContent,
  els["tutor-title"].textContent, els["tutor-desc"].textContent];
const expected = (lang) => [LANG_DICT[lang]["app.title"], LANG_DICT[lang]["app.title"], LANG_DICT[lang]["back.cabinet"],
  LANG_DICT[lang]["mode.interpreter.title"], LANG_DICT[lang]["mode.interpreter.desc"],
  LANG_DICT[lang]["mode.tutor.title"], LANG_DICT[lang]["mode.tutor.desc"]];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const first = shown();
new Promise((r) => setTimeout(r, 5)).then(() => console.log(JSON.stringify({
  first_is: ["en", "uk", "fr", "es"].find((l) => same(first, expected(l))) || null,
  last_is: ["en", "uk", "fr", "es"].find((l) => same(shown(), expected(l))) || null,
  stored: data.voice_ui_language || null,
})));
"""
    return run_node(prelude + script_src + tail)


@needs_node
def test_modes_page_phone_language_first_then_the_server_language_re_renders():
    out = _modes_run(phone="fr-FR", server=_OK)
    assert out == {"first_is": "fr", "last_is": "uk", "stored": "uk"}


@needs_node
def test_modes_page_uses_the_stored_language_before_the_phone_language():
    out = _modes_run(phone="fr-FR", stored="es", server="reject")
    assert out == {"first_is": "es", "last_is": "es", "stored": "es"}


@needs_node
def test_modes_page_english_for_a_phone_language_it_lacks_and_an_unknown_server_language_is_ignored():
    out = _modes_run(phone="pt-BR", server={"status": 200, "body": {"language": "de"}})
    assert out == {"first_is": "en", "last_is": "en", "stored": None}


@needs_node
@pytest.mark.parametrize("server", [{"status": 500, "body": {"error": "x"}}, {"status": 404, "body": {}}, "reject"])
def test_modes_page_keeps_the_fallback_when_the_fetch_fails(server):
    assert _modes_run(phone="fr-FR", server=server) == {"first_is": "fr", "last_is": "fr", "stored": None}


@needs_node
def test_modes_page_survives_a_localstorage_that_throws():
    out = _modes_run(phone="fr-FR", server=_OK, storage_throws=True)
    assert out == {"first_is": "fr", "last_is": "uk", "stored": None}
