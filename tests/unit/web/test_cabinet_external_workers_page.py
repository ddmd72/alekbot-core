"""Cabinet -> Settings -> Connections -> External workers (EXTERNAL_TASK_QUEUE_RFC §5.6, CABINET_REDESIGN_RFC §5.1): the
row of the page. The markup and the strings are read as text through `cabinet_source()`; the row's logic (hidden until the
server says `enabled`, the chip, the key shown once and dropped when the row closes) runs under node against a fake DOM
when node is installed. The routes themselves: `test_cabinet_external_workers.py`. Examples: Alex Doe's worker `research`.
"""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.web.cabinet_source import STATIC, cabinet_source

_HTML = cabinet_source()
_SHELL = (STATIC / "cabinet.html").read_text(encoding="utf-8")
_SHELL_JS = (STATIC / "cabinet-shell.js").read_text(encoding="utf-8")
_SETTINGS_JS = (STATIC / "cabinet-settings.js").read_text(encoding="utf-8")
_SECTIONS_JS = (STATIC / "cabinet-sections.js").read_text(encoding="utf-8")
_STRINGS = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
_KEY = "wk_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq"


def _dict(lang: str) -> dict:
    start = _STRINGS.index(f"        {lang}: {{")
    block = _STRINGS[start:_STRINGS.index("        },", start)]
    return dict(re.findall(r'"([a-z_.0-9]+)": "((?:[^"\\]|\\.)*)"', block))


def _function(text: str, name: str) -> str:
    match = re.search(r"\n(      (?:async )?function " + name + r"\(.*?\n      \})\n", text, re.S)
    assert match, name
    return match.group(1)


_WORKERS_JS = _SETTINGS_JS[_SETTINGS_JS.index("/* ── Settings → Connections: External workers"):
                           _SETTINGS_JS.index("/* ── Settings → Security: devices")]


# ---------------------------------------------------------------------------------------------------- the markup

def test_the_row_is_hidden_until_the_server_says_enabled():
    wrapper = re.search(r'<div class="c-item hidden" id="settings-workers-item">\s*<button type="button" class="c-row" '
                        r'id="srow-workers" aria-expanded="false" aria-controls="srow-workers-body" '
                        r'onclick="toggleSettingsRow\(\'workers\'\)">', _SHELL)
    assert wrapper
    assert 'id="settings-workers-chip"' in _SHELL and '<div class="c-row-body hidden" id="srow-workers-body">' in _SHELL
    assert re.search(r"const SETTINGS_ROWS = \[[^\]]*\"workers\"", _SETTINGS_JS)


def test_the_row_sits_between_to_do_and_drives_and_is_loaded_with_the_other_connections():
    assert _SHELL.index('id="srow-todo"') < _SHELL.index('id="srow-workers"') < _SHELL.index('id="srow-drives"')
    assert _SHELL_JS.count("loadExternalWorkers();") == 2
    assert _SHELL_JS.count("loadExternalWorkers();") == _SHELL_JS.count("loadTasksStatus();")


def test_the_row_markup_and_script_carry_no_inline_style():
    start = _SHELL.index('id="settings-workers-item"')
    markup = _SHELL[start:_SHELL.index('id="srow-drives"')]
    assert "style=" not in markup and "style=" not in _WORKERS_JS and ".style." not in _WORKERS_JS


# ---------------------------------------------------------------------------------------------------- the strings

def _keys_used() -> set:
    keys = set(re.findall(r'\bt\("(workers\.[a-z_.0-9]+)"\)', _WORKERS_JS))
    keys |= {k for k in re.findall(r'"#[^"]+": "(workers\.[a-z_.0-9]+)"', _SETTINGS_JS)}
    keys |= {f"workers.err_{code}" for code in ("label_invalid", "label_taken", "workers_cap", "keys_cap", "failed")}
    keys |= {"workers.count_one", "workers.count_other"}
    return keys


def test_every_string_of_the_row_is_in_all_four_languages():
    keys = _keys_used()
    assert len(keys) > 25
    dicts = {lang: _dict(lang) for lang in LANGS}
    for key in sorted(keys):
        for lang in LANGS:
            assert dicts[lang].get(key), (lang, key)
    for form in ("one", "few", "many", "other"):
        assert "{n}" in dicts["uk"][f"workers.count_{form}"]


def test_every_translated_selector_and_placeholder_of_the_row_names_an_element_of_the_page():
    for selector in re.findall(r'"#([a-z0-9-]+)": "workers\.[a-z_.0-9]+"', _SETTINGS_JS):
        assert f'id="{selector}"' in _HTML, selector


# ---------------------------------------------------------------------------------------------------- under node

_DOM = r"""
const EN = %(en)s;
let _currentLang = "en";
const t = (k) => EN[k] || k;
const els = new Proxy({}, {get: (o, id) => o[id] ??= (() => {
  const classes = new Set(id === "settings-workers-item" ? ["hidden"] : []);
  return {id, innerHTML: "", textContent: "", className: "", value: "",
    setAttribute() {},
    classList: {add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); }}};
})()});
const document = {getElementById: (id) => els[id]};
const API = "";
const calls = [], toasts = [], copied = [];
let replies = [], answer = true, _pinStep = "";
const confirm = () => answer;
const pinCancel = () => {};
const navigator = {clipboard: {writeText: (v) => copied.push(v)}};
const showToast = (m, kind) => toasts.push([m, kind]);
const authFetch = async (url, init) => {
  calls.push({url, method: (init && init.method) || "GET", body: init && init.body ? JSON.parse(init.body) : null});
  const r = replies.shift() || {status: 200, body: {}};
  return {ok: r.status < 400, status: r.status, json: async () => r.body};
};
const hidden = (id) => els[id].classList.contains("hidden");
"""

_WORKER = {"worker_id": "wrk-1", "label": "research", "description": "web research for home projects",
           "created_at": "2026-11-20T10:00:00+00:00",
           "keys": [{"key_id": "1a2b3c4d", "hint": "wk_ABCD…nopq", "created_at": "2026-11-20T10:05:00+00:00",
                     "last_used_at": None}]}


def _run(body: str):
    sources = "\n".join([
        _function(_SECTIONS_JS, "escapeHtml"),
        re.search(r"\n(      const SETTINGS_ROWS = .*?;)\n", _SETTINGS_JS, re.S).group(1),
        "let _openSettingsRow = \"\";",
        _function(_SETTINGS_JS, "plural"), _function(_SETTINGS_JS, "setSettingsChip"),
        _function(_SETTINGS_JS, "openSettingsRow"), _function(_SETTINGS_JS, "toggleSettingsRow"),
        _WORKERS_JS,
    ])
    en = {k: v for k, v in _dict("en").items()}
    script = (_DOM % {"en": json.dumps(en)}) + sources + "\n(async () => {\n" + body + \
        "\n})().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@_node
def test_the_row_shows_only_when_enabled_and_the_chip_counts_the_workers():
    out = _run(f"""renderExternalWorkers({{enabled: false}}); const off = hidden("settings-workers-item");
      renderExternalWorkers({{enabled: true, workers: []}});
      const none = [hidden("settings-workers-item"), els["settings-workers-chip-text"].textContent];
      renderExternalWorkers({{enabled: true, workers: [{json.dumps(_WORKER)}]}});
      return [off, none, [hidden("settings-workers-item"), els["settings-workers-chip-text"].textContent,
        els["settings-workers-chip"].className], els["workers-list"].innerHTML];""")
    assert out[0] is True
    assert out[1] == [False, "0 workers"]
    assert out[2] == [False, "1 worker", "c-chip c-chip-ok"]
    assert "research" in out[3] and "wk_ABCD…nopq" in out[3] and "never" in out[3] and "1a2b3c4d" in out[3]


@_node
def test_a_label_or_description_is_escaped_in_the_list():
    worker = dict(_WORKER, label="<img src=x onerror=alert(1)>", description="a & b")
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(worker)}]}});
      return els["workers-list"].innerHTML;""")
    assert "<img" not in out and "&#60;img" in out


@_node
def test_a_minted_key_is_shown_once_and_dropped_when_the_row_closes():
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: [{json.dumps(_WORKER)}]}});
      openSettingsRow("workers");
      replies = [{{status: 200, body: {{key: "{_KEY}", key_id: "9f9f9f9f", hint: "wk_ABCD…nopq"}}}},
                 {{status: 200, body: {{enabled: true, workers: [{json.dumps(_WORKER)}]}}}}];
      await mintWorkerKey("wrk-1", "research");
      const shown = els["workers-key-box"].innerHTML;
      const listHasKey = els["workers-list"].innerHTML.includes("{_KEY}");
      els["workers-key-value"].textContent = /id="workers-key-value">([^<]*)</.exec(shown)[1];   // the fake DOM builds no children
      copyWorkerKey();
      openSettingsRow("tz");
      return [shown, listHasKey, copied, els["workers-key-box"].innerHTML, calls.map((c) => c.method + " " + c.url)];""")
    shown, list_has_key, copied, after_close, calls = out
    assert _KEY in shown and "Copy" in shown and not list_has_key
    assert copied == [_KEY] and after_close == ""
    assert calls == ["POST /api/external-workers/wrk-1/keys", "GET /api/external-workers"]


@_node
def test_removing_the_flag_hides_the_row_closes_it_and_drops_a_shown_key():
    out = _run(f"""renderExternalWorkers({{enabled: true, workers: []}}); openSettingsRow("workers");
      els["workers-key-box"].innerHTML = "{_KEY}";
      renderExternalWorkers({{enabled: false}});
      return [hidden("settings-workers-item"), els["workers-key-box"].innerHTML, _openSettingsRow];""")
    assert out == [True, "", ""]


@_node
def test_a_refusal_code_has_its_own_message_and_an_unknown_one_the_generic_failure():
    out = _run("""replies = [{status: 400, body: {error: "label_taken"}}, {status: 400, body: {error: "surprise"}}];
      els["workers-label-input"].value = " research "; await addExternalWorker();
      await addExternalWorker();
      return [toasts, calls[0]];""")
    en = _dict("en")
    assert out[0] == [[en["workers.err_label_taken"], "error"], [en["workers.err_failed"], "error"]]
    assert out[1]["body"] == {"label": "research", "description": None}


@_node
def test_removing_a_worker_asks_first_and_revoking_a_key_names_it_by_its_id():
    out = _run("""answer = false; await removeExternalWorker("wrk-1"); const asked = calls.length;
      answer = true; replies = [{status: 200, body: {revoked: true}}, {status: 200, body: {enabled: false}}];
      await revokeWorkerKey("wrk-1", "1a2b3c4d");
      return [asked, calls.map((c) => c.method + " " + c.url)];""")
    assert out == [0, ["DELETE /api/external-workers/wrk-1/keys/1a2b3c4d", "GET /api/external-workers"]]
