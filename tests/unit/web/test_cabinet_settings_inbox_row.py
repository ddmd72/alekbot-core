"""The memory inbox's row in Settings -> Connections (CABINET_REDESIGN_RFC §5.1, MCP_MEMORY_SUBMISSIONS_RFC §8.2): one
row after Microsoft To Do with the triage frequency as a segmented control that saves at once and the connected apps with
a revoke behind a confirm. The row's logic runs under node on a stand-in DOM; the app is the invented "Desk assistant"."""
import json
import re
import subprocess

from tests.unit.web.test_cabinet_settings import (
    LANGS, STATIC, _HTML, _SETTINGS, _SETTINGS_JS, _dict, _element, _function, _node)

APP = {"grant_id": "ga", "client_name": "Desk <assistant>", "granted_at": "2026-11-20T10:00:00+00:00",
       "last_refresh": None}
APP_B = {"grant_id": "gb", "client_name": "Notes helper", "granted_at": "2026-11-21T10:00:00+00:00",
         "last_refresh": None}

_DOM = r"""
const EN = %(en)s, UK = %(uk)s;
let _currentLang = "en";
const t = (k) => (_currentLang === "uk" ? UK : EN)[k] || k;
const els = new Proxy({}, {get: (o, id) => o[id] ??= (() => {
  const classes = new Set(), attrs = {};
  return {id, textContent: "", innerHTML: "", attrs, setAttribute(k, v) { attrs[k] = v; },
    getAttribute(k) { return attrs[k] ?? null; },
    classList: {toggle: (c, on) => { (on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c); },
                contains: (c) => classes.has(c)}};
})()});
const document = {getElementById: (id) => els[id]};
const API = "";
const toasts = [], requests = [];
let routes = {}, failing = new Set(), asked = [], answer = true;
const confirm = (m) => { asked.push(m); return answer; };
const showToast = (m, kind) => toasts.push([m, kind]);
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");
const authFetch = async (url, init) => {
  const method = (init && init.method) || "GET";
  requests.push({method, url, body: init && init.body ? JSON.parse(init.body) : null});
  const key = method + " " + url;
  if (failing.has(key)) return {ok: false, status: 400, json: async () => ({})};
  if (failing.has("throw " + key)) throw new Error("offline");
  const body = routes[key];
  return {ok: body !== undefined, status: body !== undefined ? 200 : 404, json: async () => body};
};
const setSettingsValue = (id, text) => { els[id].textContent = text; };
const SETTINGS = "GET /api/user/memory-inbox/settings", APPS = "GET /api/user/memory-inbox/apps";
const PUT = "PUT /api/user/memory-inbox/settings";
const seg = () => [1, 2, 3, 7].filter((d) => els["inbox-days-" + d].classList.contains("on"));
const pressed = () => [1, 2, 3, 7].map((d) => els["inbox-days-" + d].getAttribute("aria-pressed"));
const world = (days, apps) => { routes = {[SETTINGS]: {triage_interval_days: days, choices: [1, 2, 3, 7]},
                                           [APPS]: {apps}}; };
"""


def _run(body: str):
    sources = "\n".join([
        re.search(r"\n(      const INBOX_CHOICES = .*?;)\n", _HTML).group(1),
        "let _inboxDays = null, _inboxApps = null;",
        _function("plural"), *(_function(n) for n in ("loadInbox", "renderInbox", "setInboxInterval", "revokeInboxApp")),
    ])
    script = (_DOM % {"en": json.dumps(_dict("en")), "uk": json.dumps(_dict("uk"))}) + sources + \
        "\n(async () => {\n" + body + \
        "\n})().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _json(value) -> str:
    return json.dumps(value)


def test_the_row_sits_after_to_do_and_before_the_workers_in_connections():
    ids = re.findall(r'<button type="button" class="c-row" id="(srow-[a-z]+)"', _SETTINGS)
    assert ids.index("srow-todo") + 1 == ids.index("srow-inbox") == ids.index("srow-workers") - 1
    assert "inbox" in re.search(r"const SETTINGS_ROWS = (\[.*?\]);", _SETTINGS_JS, re.S).group(1)
    row = _element("srow-inbox")
    assert 'onclick="toggleSettingsRow(\'inbox\')"' in row and 'class="c-row-value"' in row and "c-chev" in row


def test_the_body_is_made_of_shared_components_only_with_no_inline_style():
    body = _element("srow-inbox-body")
    assert "style=" not in body and "<style" not in body
    assert len(re.findall(r'<button type="button" id="inbox-days-\d" aria-pressed="false"', body)) == 4
    assert re.search(r'<div class="c-seg" role="group" aria-labelledby="settings-inbox-interval">', body)
    assert 'id="inbox-apps"' in body and "<p" in body and body.count("<p") == 1   # one label line, no hint paragraph


def test_the_memory_section_holds_no_inbox_settings_element_or_code():
    memory = _element("section-memory")
    memory_js = (STATIC / "cabinet-memory.js").read_text(encoding="utf-8")
    for needle in ("memory-inbox-settings", "inbox-interval", "inbox-apps"):
        assert needle not in memory and needle not in memory_js, needle
    for needle in ("loadInboxSettings", "saveInboxInterval", "memory-inbox/settings", "memory-inbox/apps"):
        assert needle not in memory_js, needle


@_node
def test_the_value_names_the_frequency_and_the_number_of_apps():
    out = _run(f"""
      const seen = [];
      for (const apps of [[], [{_json(APP)}], [{_json(APP)}, {_json(APP_B)}]]) {{
        world(3, apps); await loadInbox(); seen.push(els["settings-inbox-value"].textContent);
      }}
      _currentLang = "uk"; world(1, [{_json(APP)}]); await loadInbox(); seen.push(els["settings-inbox-value"].textContent);
      return seen;""")
    assert out == ["every 3 days", "every 3 days · 1 app", "every 3 days · 2 apps", "щодня · 1 застосунок"]


@_node
def test_a_failed_read_leaves_the_value_and_the_apps_blank_and_nothing_throws():
    out = _run("""
      failing.add("throw " + SETTINGS); world(2, []); await loadInbox();
      const first = [els["settings-inbox-value"].textContent, els["inbox-apps"].innerHTML, seg()];
      failing.clear(); failing.add(APPS); world(2, []); await loadInbox();
      return [first, els["settings-inbox-value"].textContent, els["inbox-apps"].innerHTML];""")
    assert out[0] == ["", "", []]
    assert out[1] == "every 2 days" and out[2] == ""


@_node
def test_the_segment_shows_the_effective_value_and_only_that_one_is_on():
    out = _run("""world(7, []); await loadInbox(); return [seg(), pressed()];""")
    assert out == [[7], ["false", "false", "false", "true"]]


@_node
def test_a_click_puts_the_choice_at_once_updates_the_value_and_shows_no_saved_note():
    out = _run("""
      world(1, []); await loadInbox(); requests.length = 0;
      routes[PUT] = {triage_interval_days: 2, choices: [1, 2, 3, 7]};
      await setInboxInterval(2);
      return [requests, seg(), els["settings-inbox-value"].textContent, toasts];""")
    assert out[0] == [{"method": "PUT", "url": "/api/user/memory-inbox/settings", "body": {"triage_interval_days": 2}}]
    assert out[1] == [2] and out[2] == "every 2 days" and out[3] == []


@_node
def test_a_refused_put_restores_the_previous_segment_and_toasts():
    out = _run("""
      world(3, []); await loadInbox();
      failing.add(PUT); await setInboxInterval(7);
      const refused = [seg(), els["settings-inbox-value"].textContent, toasts.slice()];
      failing.clear(); failing.add("throw " + PUT); await setInboxInterval(1);
      return [refused, seg(), toasts.length];""")
    assert out[0] == [[3], "every 3 days", [["Could not save. Try again.", "error"]]]
    assert out[1] == [3] and out[2] == 2


@_node
def test_each_app_is_a_mini_row_with_its_name_and_connected_date_and_a_revoke_link():
    out = _run(f"""world(1, [{_json(APP)}]); await loadInbox(); return els["inbox-apps"].innerHTML;""")
    assert out.count('<div class="c-mini">') == 1 and '<div class="c-mini-main">' in out
    assert 'class="c-mini-t">Desk &#60;assistant&#62;<' in out
    assert re.search(r'class="c-mini-s">Connected Nov 2\d, 2026<', out) and "refresh" not in out.lower()
    assert 'class="c-lnk c-lnk-danger" data-grant="ga"' in out and ">Revoke<" in out and "style=" not in out


@_node
def test_no_apps_is_one_line_saying_so():
    out = _run("""world(1, []); await loadInbox(); return els["inbox-apps"].innerHTML;""")
    assert out == '<div class="c-mini-s">No app is connected.</div>'


@_node
def test_revoke_declined_sends_nothing_and_confirmed_deletes_then_lists_again():
    out = _run(f"""
      world(1, [{_json(APP)}]); await loadInbox(); requests.length = 0;
      answer = false; await revokeInboxApp("ga");
      const declined = requests.length;
      answer = true; routes["DELETE /api/user/memory-inbox/apps/ga"] = {{revoked: true}};
      routes[APPS] = {{apps: []}}; await revokeInboxApp("ga");
      return [declined, requests.map((r) => r.method + " " + r.url), toasts, asked.length, els["inbox-apps"].innerHTML];""")
    assert out[0] == 0
    assert out[1] == ["DELETE /api/user/memory-inbox/apps/ga", "GET /api/user/memory-inbox/settings",
                      "GET /api/user/memory-inbox/apps"]
    assert out[2] == [["Access revoked", "success"]] and out[3] == 2 and "No app is connected." in out[4]
    assert "refused at once" in _dict("en")["settings.inbox_revoke_confirm"]


@_node
def test_a_refused_revoke_toasts_an_error_and_keeps_the_app():
    out = _run(f"""
      world(1, [{_json(APP)}]); await loadInbox();
      failing.add("DELETE /api/user/memory-inbox/apps/ga"); await revokeInboxApp("ga");
      return [toasts, els["inbox-apps"].innerHTML.includes("Desk")];""")
    assert out == [[["Could not save. Try again.", "error"]], True]


def test_every_inbox_string_exists_in_four_languages_and_uk_fr_es_are_translated():
    dicts = {lang: _dict(lang) for lang in LANGS}
    used = set(re.findall(r't\("(settings\.inbox_\w+)"\)', _SETTINGS_JS))
    used |= {f"settings.inbox_every_{d}" for d in (1, 2, 3, 7)} | {"settings.inbox_apps_one", "settings.inbox_apps_other"}
    used |= set(re.findall(r'"#[a-z0-9-]+": "(settings\.inbox_\w+)"', _SETTINGS_JS))
    assert len(used) >= 15
    assert [(lang, k) for lang in LANGS for k in sorted(used) if not dicts[lang].get(k)] == []
    assert [(lang, k) for lang in ("uk", "fr", "es") for k in sorted(used) if dicts[lang][k] == dicts["en"][k]] == []
    assert [k for k in dicts["en"] if k.startswith("memory.inbox_settings_")] == []
    assert all(dicts["uk"].get(f"settings.inbox_apps_{form}") for form in ("one", "few", "many", "other"))
