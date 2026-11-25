"""The memory inbox's settings block in the Cabinet's Memory section (MCP_MEMORY_SUBMISSIONS_RFC §10.2), its page logic
run under node on the stand-in DOM of `test_cabinet_memory_ui`: the frequency select with the effective value, the
connected apps, saving on change, revoking after a confirm, and nothing left once the tab locks. The app is the invented
"Desk assistant"."""
import json
import re

from tests.unit.web.test_cabinet_memory_ui import LANGS, _MEMORY_JS, _dicts, _node, _run

SETTINGS = {"triage_interval_days": 3, "choices": [1, 2, 3, 7]}
APP = {"grant_id": "ga", "client_name": "Desk <assistant>", "granted_at": "2026-11-20T10:00:00+00:00",
       "last_refresh": None}
_SETUP = (f"const SETTINGS = {json.dumps(SETTINGS)}, APP = {json.dumps(APP)};"
          "routes = { '/api/user/memory-inbox/settings': SETTINGS, '/api/user/memory-inbox/apps': { apps: [APP] } };"
          "async function useSettings() { factsMode = 'browse'; await loadInboxSettings(); requests.length = 0; }")


def _run_settings(tmp_path, body: str):
    return _run(tmp_path, f"{_SETUP}\n{body}")


@_node
def test_the_block_shows_the_select_with_the_effective_value_and_the_apps(tmp_path):
    out = _run_settings(tmp_path, """
      factsMode = "browse"; await loadInboxSettings();
      return [els["memory-inbox-settings"].has("hidden"), els["inbox-interval"].value, els["inbox-interval"].innerHTML,
              els["inbox-apps"].innerHTML, els["inbox-settings-title"].textContent];""")
    hidden, value, options, apps, title = out
    assert hidden is False and value == "3" and title == "From your assistants — settings"
    assert re.findall(r'<option value="(\d)">([^<]*)</option>', options) == [
        ("1", "every day"), ("2", "every 2 days"), ("3", "every 3 days"), ("7", "once a week")]
    assert "Desk &#60;assistant&#62;" in apps and "Revoke access" in apps and "unknown" in apps
    assert 'data-grant="ga"' in apps


@_node
def test_no_app_connected_says_so(tmp_path):
    out = _run_settings(tmp_path, """
      routes["/api/user/memory-inbox/apps"] = { apps: [] };
      factsMode = "browse"; await loadInboxSettings();
      return els["inbox-apps"].innerHTML;""")
    assert "No app is connected." in out and "<button" not in out


@_node
def test_a_change_puts_the_chosen_value_and_says_it_is_saved(tmp_path):
    out = _run_settings(tmp_path, """
      await useSettings();
      routes["/api/user/memory-inbox/settings"] = { triage_interval_days: 7, choices: [1, 2, 3, 7] };
      els["inbox-interval"].value = "7";
      const calls = [];
      const real = authFetch;
      authFetch = async (url, init) => { calls.push([init.method, url, init.body]); return real(url, init); };
      await saveInboxInterval();
      return [calls, els["inbox-interval-note"].textContent, inboxSettings.triage_interval_days];""")
    assert out[0] == [["PUT", "/api/user/memory-inbox/settings", '{"triage_interval_days":7}']]
    assert out[1] == "Saved. It applies from the next run." and out[2] == 7


@_node
def test_a_refused_change_says_so_and_restores_the_stored_value(tmp_path):
    out = _run_settings(tmp_path, """
      await useSettings();
      authFetch = async () => ({ ok: false, status: 400, json: async () => ({ error: "invalid_interval" }),
                                 clone() { return this; } });
      els["inbox-interval"].value = "7";
      await saveInboxInterval();
      return [els["inbox-interval-note"].textContent, els["inbox-interval"].value];""")
    assert out == ["Could not save. Try again.", "3"]


@_node
def test_revoke_asks_first_then_deletes_and_lists_again(tmp_path):
    out = _run_settings(tmp_path, """
      await useSettings();
      const asked = [];
      globalThis.confirm = (m) => { asked.push(m); return false; };
      await revokeInboxApp("ga");
      const declined = requests.map((r) => [r.method, r.url]);
      globalThis.confirm = (m) => { asked.push(m); return true; };
      routes["/api/user/memory-inbox/apps"] = { apps: [] };
      await revokeInboxApp("ga");
      return [declined, requests.map((r) => [r.method, r.url]), asked.length, toasts, els["inbox-apps"].innerHTML];""")
    assert out[0] == []
    assert out[1][0] == ["DELETE", "/api/user/memory-inbox/apps/ga"]
    assert ["GET", "/api/user/memory-inbox/apps"] in out[1]
    assert out[2] == 2 and out[3] == [["Access revoked", "success"]]
    assert "No app is connected." in out[4]


@_node
def test_a_locked_tab_clears_the_block(tmp_path):
    out = _run_settings(tmp_path, """
      factsMode = "browse"; await loadInboxSettings(); forgetFacts();
      return [els["memory-inbox-settings"].has("hidden"), els["inbox-apps"].innerHTML, inboxSettings, inboxApps];""")
    assert out == [True, "", None, []]


def test_the_settings_strings_exist_in_four_languages_and_are_translated():
    dicts = _dicts()
    keys = [f"memory.inbox_settings_{k}" for k in (
        "title", "interval", "every_1", "every_2", "every_3", "every_7", "saved", "failed", "apps", "none", "granted",
        "refreshed", "unknown", "revoke", "revoke_confirm", "revoked", "hint")]
    assert [(lang, k) for lang in LANGS for k in keys if not dicts[lang].get(k)] == []
    assert [(lang, k) for lang in ("uk", "fr", "es") for k in keys if dicts[lang][k] == dicts["en"][k]] == []
    used = set(re.findall(r't\("(memory\.inbox_settings_\w+)"\)', _MEMORY_JS))
    assert [(lang, k) for lang in LANGS for k in sorted(used) if not dicts[lang].get(k)] == []
