"""The Cabinet drive card after the redesign: a summary that opens into problem banners and four tabs (Overview / Access /
Search index / Settings, Disconnect at the bottom of Settings), the state chip and its priority, the empty-scope state
keyed off the server's `scope.empty_means` (never a provider), the counters (three tiles, `unresolved` on its own line,
only labelled keys), the folder-picker dialog with its Save (n) button and unsaved-changes bar, the Integrations tab
layout (compact tiles, the drive card full width below them) and the i18n of every new `drive.*` key.

The static checks read the page; the behavioural ones run the drive script's own functions under node when node is
installed, over a fake DOM that keeps what the renderers write. Invented cast only."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.web.test_cabinet_drive_index_strings import LANGS, _dicts

_HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    a = _HTML.index(start)
    return _HTML[a:_HTML.index(end, a)]


CARD = _between("let driveData = null;", '// "Add a drive": the kinds')
COUNTERS = _between("// DriveSyncState.last_error categories", "let driveMirrorRoot")
INDEX = _between("let driveMirrorRoot", "function renderDeepResearchCopy")
PICKER = _between("async function saveDriveWriteFolder", "function askDisconnectDrive")
ESCAPE = re.search(r"\n(\s*function escapeHtml\(str\) \{.*?\n\s*\})\n", _HTML, re.S).group(1)

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

PRELUDE = r'''
const API = ""; const calls = []; const toasts = []; let reloads = 0; let _driveStatusSnapshot = {};
const listeners = {}; const els = {};
const fake = (id) => (els[id] = els[id] || { innerHTML: "", focus() {}, querySelectorAll() { return []; } });
const document = { body: { classList: { add() {}, remove() {}, contains() { return false; } }, contains() { return false; } },
  activeElement: null, addEventListener(ev, fn) { listeners[ev] = fn; },
  getElementById(id) { return id.startsWith("drive-pk-") && id !== "drive-pk-dialog" || id === "drive-container" || id === "drive-picker-root" ? fake(id) : (id === "drive-pk-dialog" ? fake(id) : null); } };
function t(key) { return "T:" + key; }
function showToast(msg, type) { toasts.push([msg, type]); }
function observeElements() {}
function renderAddDrive() { return ""; }
function renderDeepResearchCopy() { return "<i id=drive-dr-copy-toggle></i>"; }
function connectButton(href, label) { return href ? "<a>" + label + "</a>" : ""; }
async function loadDriveStatus() { reloads += 1; }
let respond = (method, path, body) => ({ ok: true, data: {} });
async function driveCall(method, path, body) { calls.push([method, path, body]); return respond(method, path, body); }
'''


def _run(body: str, scope: str = CARD + COUNTERS + INDEX + PICKER) -> dict:
    script = (PRELUDE + ESCAPE + "\n" + scope + "\n(async () => {\n" + body
              + "\n})().catch((e) => { console.error(e); process.exit(1); });\n")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return json.loads(out)


def _row(cid="c1", name="Work", means="nothing", folders=(), whole=False, write="none", counters=None, enabled=True,
         **over) -> dict:
    wf = None if write == "none" else {"id": write, "path": "" if write == "" else "Projects/Inbox"}
    row = {
        "connection_id": cid, "mount_name": name, "kind": "k", "provider_label": "Provider",
        "account_label": "user@example.com", "state": "active", "primary": False, "write_folder": wf,
        "scope": {"whole_drive": whole, "empty_means": means, "folders": [{"id": i, "path": p} for i, p in folders]},
        "searched_by_default": True, "indexing_enabled": enabled, "transcribe_enabled": False, "note": "",
        "reconnect": None, "reconnect_href": "",
        "indexing": {"enabled": enabled, "available": True, "counters": counters or {}, "building": False,
                     "awaiting_rebuild": False, "index_full": False, "index_kept": True, "last_error": None,
                     "last_success_at": None, "readable_chunks": 0, "model_usd_month": {}, "model_usd_caps": {},
                     "initial_build": {}, "unreadable": {"files": [], "total": 0}, "connection_id": cid},
    }
    row.update(over)
    return row


def _render(rows: list, setup: str = "", ui_open: bool = True) -> str:
    data = {"connections": rows, "add_kinds": [], "dr_copy": None}
    opened = "".join(f'driveUiOf({json.dumps(r["connection_id"])}).open = {json.dumps(ui_open)};' for r in rows)
    out = _run(f"driveData = {json.dumps(data)};{opened}\n{setup}\nrenderDriveStatus(driveData);"
               " process.stdout.write(JSON.stringify({ html: els['drive-container'].innerHTML }));")
    return out["html"]


def _card(html: str, cid: str) -> str:
    start = html.index(f'id="drive-card-{cid}"')
    nxt = html.find('<article class="card dv-card', start + 1)
    return html[start:nxt if nxt != -1 else len(html)]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --------------------------------------------------------------------------------------------- the tabs

def test_the_four_tabs_exist_and_disconnect_is_only_in_the_settings_tab():
    assert 'const DRIVE_TABS = ["overview", "access", "index", "settings"];' in CARD
    for tab in ("overview", "access", "index", "settings"):
        _, dicts = _dicts()
        assert all(dicts[lang].get(f"drive.tab_{tab}") for lang in LANGS), tab
    body = re.search(r"function renderDriveBody\(.*?\n      }\n", CARD, re.S).group(0)
    assert body.index("renderDriveSettings") < body.index("renderDriveDisconnect")
    for other in ("renderDriveOverview", "renderDriveAccess", "renderDriveIndexing"):
        assert "renderDriveDisconnect" not in body.split(other)[1].split("\n")[0], other


@node
def test_the_open_tab_is_kept_per_connection():
    rows = [_row("c1", "Work"), _row("c2", "Home")]
    html = _render(rows, 'driveUiOf("c1").tab = "access"; driveUiOf("c2").tab = "settings";')
    one, two = _card(html, "c1"), _card(html, "c2")
    assert re.search(r'id="drive-tab-c1-access" aria-selected="true"', one) and 'drive-tab-c1-overview" aria-selected="false"' in one
    assert re.search(r'id="drive-tab-c2-settings" aria-selected="true"', two)
    assert "T:drive.sees_title" in one and "T:drive.primary_title" in two and "T:drive.primary_title" not in one


@node
def test_selecting_a_tab_changes_only_that_connection_and_survives_a_redraw():
    out = _run('driveData = ' + json.dumps({"connections": [_row("c1"), _row("c2")], "add_kinds": [], "dr_copy": None})
               + '; selectDriveTab("c1", "index"); redrawDrive();'
                 ' process.stdout.write(JSON.stringify([driveUiOf("c1").tab || null, driveUiOf("c2").tab || null]));')
    assert out == ["index", None]


@node
def test_the_disconnect_link_closes_the_settings_tab_and_asks_before_it_acts():
    html = _render([_row()], 'driveUiOf("c1").tab = "settings";')
    text = html
    assert text.index('id="drive-name-c1"') < text.index("askDisconnectDrive('c1')")
    assert "disconnectDrive('c1')" not in html           # the confirmation is a second step
    html = _render([_row()], 'driveUiOf("c1").tab = "settings"; driveUiOf("c1").confirm = true;')
    assert "disconnectDrive('c1')" in html and "cancelDisconnectDrive('c1')" in html


# ---------------------------------------------------------------- the empty scope, keyed off `empty_means`

def test_the_page_never_names_a_provider_to_tell_what_an_empty_scope_means():
    assert "empty_means" in CARD
    assert not re.search(r"\.kind\s*[=!]==?|provider_label\s*[=!]==?|\.kind\.(includes|startsWith)", CARD + PICKER)


@node
def test_an_account_wide_drive_with_nothing_chosen_shows_the_warning_chip_banner_and_two_actions():
    html = _card(_render([_row(means="nothing")]), "c1")
    assert "dv-chip warn" in html and "T:drive.chip_nothing" in html
    assert "T:drive.banner_empty_title" in html and "T:drive.banner_empty_search" in html      # search is on
    assert "openDrivePicker('c1', 'scope')" in html and "toggleDriveScopeWhole('c1', true)" in html
    assert "T:drive.sees_nothing" in html and "T:drive.search_empty" in html


@node
def test_an_area_drive_with_nothing_chosen_is_not_a_problem():
    html = _card(_render([_row(means="area")]), "c1")
    assert "T:drive.banner_empty_title" not in html and "T:drive.chip_nothing" not in html
    assert "T:drive.sees_area" in html and "dv-chip ok" in html


@node
def test_a_stored_whole_drive_on_an_area_drive_renders_like_empty():
    stored = _card(_render([_row(means="area", whole=True)], 'driveUiOf("c1").tab = "access";'), "c1")
    empty = _card(_render([_row(means="area", whole=False)], 'driveUiOf("c1").tab = "access";'), "c1")
    assert stored == empty
    assert "T:drive.sees_whole" not in stored and "T:drive.stop_whole" not in stored and "T:drive.own_folder_sub" in stored


@node
def test_whole_drive_on_an_account_drive_says_everything_the_account_sees():
    html = _card(_render([_row(means="nothing", whole=True)], 'driveUiOf("c1").tab = "access";'), "c1")
    assert "T:drive.sees_whole" in html and "T:drive.whole_sub_account" in html and "toggleDriveScopeWhole('c1', false)" in html
    _, dicts = _dicts()
    assert "shared with you" in dicts["en"]["drive.whole_sub_account"] and "My Drive only" not in dicts["en"]["drive.whole_sub_account"]


@node
def test_an_unknown_meaning_is_neither_a_problem_nor_an_area():
    html = _card(_render([_row(means=None)]), "c1")
    assert "T:drive.banner_empty_title" not in html and "T:drive.sees_area" not in html and "T:drive.sees_default" in html


@node
@pytest.mark.parametrize("kwargs, parts", [
    (dict(means="nothing", folders=[("a", "Projects"), ("b", "Team handbook")]), ["T:drive.sees_n"]),
    (dict(means="nothing", folders=[("a", "Projects")]), ["T:drive.sees_one"]),
    (dict(means="nothing", whole=True), ["T:drive.sees_whole"]),
])
def test_the_summary_line_counts_what_the_bot_sees(kwargs, parts):
    html = _card(_render([_row(**kwargs)]), "c1")
    assert all(p in html for p in parts)


# ------------------------------------------------------------------------------- the chip and its priority

@node
@pytest.mark.parametrize("name, kwargs, chip", [
    ("reconnect beats everything", dict(means="nothing", reconnect={"credential_id": "k"}), "T:drive.needs_reconnect"),
    ("nothing selected beats full", dict(means="nothing", indexing_over={"index_full": True}), "T:drive.chip_nothing"),
    ("full beats indexing", dict(means="area", indexing_over={"index_full": True, "building": True}), "T:drive.chip_full"),
    ("indexing", dict(means="area", indexing_over={"building": True}, counters={"carded": 40, "pending": 60}),
     "T:drive.chip_indexing"),
    ("connected", dict(means="area"), "T:platform.connected"),
])
def test_the_chip_follows_the_priority(name, kwargs, chip):
    over = kwargs.pop("indexing_over", {})
    row = _row(**kwargs)
    row["indexing"].update(over)
    html = _card(_render([row], ui_open=False), "c1")
    shown = re.search(r'<span class="dv-chip [a-z]+">(.*?)</span>', html).group(1)
    assert shown == chip, name


@node
def test_the_indexing_chip_carries_the_derived_percent_from_searchable_over_searchable_plus_waiting():
    row = _row(means="area", counters={"carded": 312, "pending": 468, "unresolved": 3000})
    row["indexing"]["building"] = True
    html = _card(_render([row], ui_open=False), "c1")
    assert "T:drive.chip_indexing" in html and "T:drive.search_pct" in html


# ----------------------------------------------------------------------------------------- the counters

@node
def test_waiting_is_pending_only_and_unresolved_has_its_own_line():
    counters = {"carded": 10, "pending": 3, "unresolved": 500, "not_read_yet": 7, "inaccessible": 1, "failed": 2}
    html = _card(_render([_row(means="area", counters=counters)], 'driveUiOf("c1").tab = "index";'), "c1")
    tiles = re.search(r'<div class="dv-stats">(.*?)</div></div>\s*(?:<details|<div class="dv-group"|$)', html, re.S).group(1)
    values = re.findall(r'dv-stat-v">([\d,]+)</div><div class="dv-stat-l">T:(drive\.\w+)', tiles)
    assert values == [("10", "drive.index_searchable"), ("3", "drive.index_pending"), ("3", "drive.index_cant_read")]
    assert "T:drive.index_checking" in html          # {n} is filled in the real dictionary text
    assert "500" in html
    # not_read_yet is under "All counters", not a tile
    assert "T:drive.index_unread" in html.split("T:drive.all_counters")[1] and "T:drive.index_unread" not in tiles


def test_unresolved_is_never_summed_into_waiting_in_the_page():
    model = re.search(r"function driveModel\(.*?\n      }\n", CARD, re.S).group(0)
    assert "const waiting = driveNum(c.pending);" in model
    assert re.search(r"const checking = driveNum\(c\.unresolved\);", model)
    assert not re.search(r"waiting[^;\n]*unresolved|unresolved[^;\n]*waiting", model.replace("// Waiting is `pending` only", ""))
    tile = re.search(r'dv-stat"><div class="dv-stat-v">\$\{driveFmt\(c\.pending\)\}', INDEX)
    assert tile and "c.unresolved" not in INDEX.split("dv-stats")[1].split("</div></div>")[1]
    _, dicts = _dicts()
    assert all(dicts[lang].get("drive.index_checking") and "{n}" in dicts[lang]["drive.index_checking"] for lang in LANGS)


@node
def test_only_labelled_counters_are_rendered_and_the_enrichment_stats_stay_out():
    counters = {"carded": 1200, "indexed": 1000, "chunks": 8362, "profile_current": 209, "profile_none": 61,
                "passages_current": 178, "passages_none": 92, "coverage_no_passages": 26, "coverage_other": 9,
                "profile_stale": 4, "mystery_key": 5}
    html = _card(_render([_row(means="area", counters=counters)], 'driveUiOf("c1").tab = "index";'), "c1")
    more = html.split("T:drive.all_counters")[1].split("</details>")[0]
    rows = dict(re.findall(r"<dt[^>]*>T:(drive\.\w+)</dt><dd[^>]*>([\d,]+)</dd>", more))
    assert rows["drive.index_described"] == "209" and rows["drive.index_readable_content"] == "178"
    labelled = {"drive.index_searchable", "drive.index_chunks", "drive.index_indexed", "drive.index_metadata_only",
                "drive.index_pending", "drive.index_unresolved", "drive.index_unread", "drive.index_inaccessible",
                "drive.index_failed", "drive.index_deleting", "drive.index_described", "drive.index_readable_content"}
    assert set(rows) <= labelled                      # every rendered line has a label; no raw key, no `chunks`
    assert "mystery_key" not in more and "8,362" not in more and "8362" not in more


@node
def test_files_without_passage_hints_by_policy_are_never_listed_as_a_problem():
    """Sheets, books and data files get no passage hints by policy: that is no gap for the owner, so the Cabinet never
    shows the counter, at any value; search still reads it from the sync state."""
    html = _card(_render([_row(means="area", counters={"carded": 5, "passages_current": 2,
                                                       "coverage_no_passages": 30})], 'driveUiOf("c1").tab = "index";'), "c1")
    more = html.split("T:drive.all_counters")[1].split("</details>")[0]
    assert "T:drive.index_readable_content" in more
    assert "coverage" not in more and 'class="attn"' not in more and ">30<" not in more
    assert "coverage_no_passages" not in COUNTERS + INDEX


def test_there_is_no_raw_key_fallback_and_chunks_is_not_listed():
    assert "escapeHtml(key)" not in INDEX and "Object.keys(c)" not in INDEX
    assert "chunks:" not in COUNTERS and '"chunks"' not in INDEX
    assert "profile_current" in COUNTERS and "passages_current" in COUNTERS
    assert "profile_none" not in COUNTERS + INDEX and "passages_none" not in COUNTERS + INDEX


# ----------------------------------------------------------------------------------------------- escaping

@node
def test_names_paths_and_labels_are_escaped_everywhere():
    evil = '<img src=x onerror=alert(1)>'
    row = _row(name=evil, folders=[("a", evil)], write="w", account_label=evil, note=evil)
    row["write_folder"]["path"] = evil
    out = _run(LISTING + "driveData = " + json.dumps({"connections": [row], "add_kinds": [], "dr_copy": None}) + ";"
               "const seen = []; for (const tab of ['overview', 'access', 'settings']) { driveUiOf('c1').open = true; driveUiOf('c1').tab = tab;"
               " renderDriveStatus(driveData); seen.push(els['drive-container'].innerHTML); }"
               "await openDrivePicker('c1', 'scope'); seen.push(els['drive-pk-foot'].innerHTML + els['drive-pk-side'].innerHTML);"
               " process.stdout.write(JSON.stringify(seen));")
    for html in out:
        assert "<img" not in html


# ------------------------------------------------------------------------------------------- the picker

SCOPE_ROW = _row(means="nothing", folders=[("f-proj", "Projects")], write="none")
LISTING = ('respond = (method, path, body) => method === "GET" ? { ok: true, data: { folders: ['
           '{ id: "f-proj", name: "Projects", path: "Projects" }, { id: "f-inv", name: "Invoices 2026", path: "Invoices 2026" }, '
           '{ id: "f-sub", name: "Inbox", path: "Projects/Inbox" }], outer: [{ id: "f-hb", name: "Team handbook", path: "Team handbook" }] } }'
           ' : { ok: true, data: {} };')


def _picker(body: str, row=SCOPE_ROW, extra: str = "") -> dict:
    data = {"connections": [row], "add_kinds": [], "dr_copy": None}
    return _run(LISTING + extra + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "scope");\n' + body
                + "\nprocess.stdout.write(JSON.stringify({ calls: calls.filter((c) => c[0] === 'PUT'), toasts, reloads,"
                  " open: driveUiOf('c1').picker !== null, error: driveUiOf('c1').picker ? driveUiOf('c1').picker.error : null,"
                  " foot: els['drive-pk-foot'].innerHTML, list: els['drive-pk-list'].innerHTML, side: els['drive-pk-side'].innerHTML,"
                  " scopeError: driveUiOf('c1').scopeError || '', n: driveUiOf('c1').picker ? drivePickerChanges(driveUiOf('c1').picker) : 0 }));")


@node
def test_the_picker_starts_with_the_stored_selection_and_no_changes():
    out = _picker("")
    assert out["n"] == 0 and out["open"]
    assert "disabled" in out["foot"].split("T:drive.save")[0].rsplit("<button", 1)[1]      # Save is off with nothing to save
    assert out["list"].count("checked") == 1 + out["list"].count("Inbox")                  # Projects ticked; Inbox covered by it


@node
def test_a_change_enables_save_with_its_count_and_marks_covered_folders():
    out = _picker('toggleDrivePickerFolder("c1", 1);')
    assert out["n"] == 1 and "T:drive.save (1)" in out["foot"]
    assert out["calls"] == []                                  # nothing is sent until Save
    assert "T:drive.pk_included_through" in out["list"] and "Invoices 2026" in out["list"]


@node
def test_a_selected_descendant_marks_its_ancestor_and_the_parent_replaces_it():
    row = _row(means="nothing", folders=[("f-sub", "Projects/Inbox")], write="none")
    out = _picker("", row=row)
    assert "T:drive.pk_inside" in out["list"]
    out = _picker('toggleDrivePickerFolder("c1", 0);', row=row)          # tick Projects: the Inbox pick is dropped
    assert out["n"] == 2 and out["foot"].count("dv-tchip") == 1


@node
def test_save_puts_the_draft_and_closes_the_dialog_on_success():
    out = _picker('toggleDrivePickerFolder("c1", 1); await saveDrivePicker("c1");')
    assert out["calls"] == [["PUT", "/api/drive/connections/c1/scope", {"whole_drive": False, "folder_ids": ["f-proj", "f-inv"]}]]
    assert out["toasts"] == [["T:toast.drive_saved", "success"]] and out["reloads"] == 1 and not out["open"]


@node
def test_a_refused_save_shows_the_servers_line_in_the_dialog_and_keeps_it_open():
    line = "The write folder Projects would be outside the folders indexed: choose another write folder first."
    out = _picker('toggleDrivePickerFolder("c1", 1); await saveDrivePicker("c1");',
                  extra=f'const original = respond; respond = (m, p, b) => m === "PUT" ? {{ ok: false, data: {{ error: {json.dumps(line)} }} }} : original(m, p, b);')
    assert out["open"] and out["error"] == line and line in out["foot"]
    assert out["scopeError"] == "" and out["toasts"] == [] and out["reloads"] == 0
    assert out["n"] == 1 and "T:drive.save (1)" in out["foot"]       # the draft is kept for another try
    assert "becomes read-only" not in _HTML and "pk_readonly_warn" not in _HTML


@node
def test_closing_with_pending_changes_asks_and_each_close_path_does_the_same():
    for close in ('requestCloseDrivePicker("c1");', 'listeners.keydown({ key: "Escape", preventDefault() {} });'):
        out = _picker('toggleDrivePickerFolder("c1", 1);' + close)
        assert out["open"] and "T:drive.pk_confirm_one" in out["foot"] and "resumeDrivePicker('c1')" in out["foot"]
        assert "closeDrivePicker('c1')" in out["foot"] and "saveDrivePicker('c1')" in out["foot"]
        assert out["calls"] == []


@node
def test_closing_without_changes_closes_at_once_and_cancel_discards():
    assert not _picker('requestCloseDrivePicker("c1");')["open"]
    assert not _picker('toggleDrivePickerFolder("c1", 1); closeDrivePicker("c1");')["open"]       # Cancel / Discard


@node
def test_keep_editing_returns_to_the_dialog_and_two_changes_say_so():
    out = _picker('toggleDrivePickerFolder("c1", 1); toggleDrivePickerFolder("c1", 0); requestCloseDrivePicker("c1");')
    assert "T:drive.pk_confirm_many" in out["foot"]
    out = _picker('toggleDrivePickerFolder("c1", 1); requestCloseDrivePicker("c1"); resumeDrivePicker("c1");')
    assert out["open"] and "T:drive.pk_confirm_one" not in out["foot"] and "T:drive.save (1)" in out["foot"]


@node
def test_the_whole_drive_choice_follows_empty_means():
    account = _picker("")
    assert "toggleDrivePickerWhole('c1', this.checked)" in account["side"] and "T:drive.whole_sub_account" in account["side"]
    area = _picker("", row=_row(means="area", write="none"))
    assert "toggleDrivePickerWhole" not in area["side"] and "T:drive.pk_area_info" in area["side"]


@node
def test_ticking_the_whole_drive_on_an_account_drive_saves_whole_with_no_folders_and_unticking_restores_the_draft():
    out = _picker('toggleDrivePickerWhole("c1", true); await saveDrivePicker("c1");')
    assert out["calls"] == [["PUT", "/api/drive/connections/c1/scope", {"whole_drive": True, "folder_ids": []}]]
    out = _picker('toggleDrivePickerWhole("c1", true); toggleDrivePickerWhole("c1", false);')
    assert out["n"] == 0


@node
def test_an_area_drive_saves_whole_drive_false_even_when_one_was_stored():
    row = _row(means="area", whole=True, write="none")
    out = _picker('toggleDrivePickerFolder("c1", 1); await saveDrivePicker("c1");', row=row)
    assert out["calls"][0][2] == {"whole_drive": False, "folder_ids": ["f-inv"]}


@node
def test_the_other_places_source_shows_only_when_the_root_listing_has_them_and_lists_them():
    out = _picker('setDrivePickerSource("c1", "outer");')
    assert "T:drive.picker_outer" in out["side"] and "Team handbook" in out["list"] and "Invoices 2026" not in out["list"]


@node
def test_a_failing_listing_shows_its_error_in_the_dialog():
    out = _picker("", extra='respond = (m) => ({ ok: false, data: { error: "Folder not found" } });')
    assert out["error"] == "Folder not found" and "T:drive.no_folders" in out["list"]


@node
def test_the_write_picker_saves_with_save_n_and_a_refusal_stays_in_the_dialog():
    row = _row(means="nothing", folders=[("f-proj", "Projects")], write="f-sub")
    data = {"connections": [row], "add_kinds": [], "dr_copy": None}
    base = LISTING + "driveData = " + json.dumps(data) + ';\nawait openDrivePicker("c1", "write");\n'
    out = _run(base + 'setDrivePickerWrite("c1", 0); const n = drivePickerChanges(driveUiOf("c1").picker);'
               ' await saveDrivePicker("c1");'
               ' process.stdout.write(JSON.stringify({ n, calls: calls.filter((c) => c[0] === "PUT"), open: driveUiOf("c1").picker !== null }));')
    assert out["n"] == 1 and out["calls"] == [["PUT", "/api/drive/connections/c1/write-folder", {"folder_id": "f-proj"}]] and not out["open"]
    out = _run(base + 'respond = (m) => m === "PUT" ? { ok: false, data: { error: "refused line" } } : { ok: true, data: {} };'
               ' setDrivePickerWrite("c1", -1); await saveDrivePicker("c1");'
               ' process.stdout.write(JSON.stringify({ open: driveUiOf("c1").picker !== null, error: driveUiOf("c1").picker.error,'
               ' folderError: driveUiOf("c1").folderError || "" }));')
    assert out == {"open": True, "error": "refused line", "folderError": ""}


@node
def test_saving_outside_the_picker_still_saves_at_once():
    out = _run('driveData = ' + json.dumps({"connections": [_row(folders=[("a", "Projects"), ("b", "Archive")])], "add_kinds": [], "dr_copy": None})
               + '; await removeDriveScopeFolder("c1", 0); await toggleDriveScopeWhole("c1", true);'
                 ' process.stdout.write(JSON.stringify(calls));')
    assert out == [["PUT", "/api/drive/connections/c1/scope", {"whole_drive": False, "folder_ids": ["b"]}],
                   ["PUT", "/api/drive/connections/c1/scope", {"whole_drive": True, "folder_ids": []}]]


def test_the_picker_lives_outside_the_card_and_is_a_modal_dialog():
    assert '<div id="drive-picker-root"></div>' in _HTML
    assert _HTML.index('id="drive-picker-root"') > _HTML.index('id="drive-container"')
    assert 'role="dialog" aria-modal="true"' in PICKER
    assert re.search(r"\.dv-overlay \{ position: fixed;", _HTML)


# -------------------------------------------------------------------------------- the Integrations layout

def test_the_drive_card_sits_full_width_below_the_tiles():
    section = _between('<section id="section-integrations"', "<!-- Team Section -->")
    for tile in ('id="platforms-container"', 'id="gmail-container"', 'id="tasks-container"'):
        assert section.index(tile) < section.index('id="drive-container"'), tile
    grid_end = section.index('<!-- The drive card: full width below the tiles -->')
    assert section.index('id="tasks-container"') < grid_end < section.index('id="drive-container"')
    assert section.count('class="integration-bento"') == 1 and section.index('class="integration-bento"') < section.index('id="gmail-container"')


def test_the_tiles_are_compact_two_columns_and_do_not_stretch():
    css = _between("/* ─── Integrations Section", "/* Long privacy text")
    assert re.search(r"\.integration-bento \{[^}]*align-items: start;", css, re.S)
    assert "grid-template-columns: 1fr;" in css and "repeat(2, minmax(0, 1fr))" in css and "min-width: 768px" in css
    card = re.search(r"\.integration-card \{(.*?)\}", css, re.S).group(1)
    assert "padding: 20px;" in card and "text-align: left;" in card and "align-items: flex-start;" in card
    assert "padding: 40px" not in css and "text-align: center" not in css
    assert "display: contents;" in css.split(".integration-contents")[1]


# ---------------------------------------------------------------------------------------------- the i18n

def _code_keys() -> set:
    code = (CARD + INDEX + PICKER + COUNTERS).replace('"drive.tab_" + k', '"drive.tab_overview"')
    keys = set(re.findall(r'"(drive\.[\w.]+)"', code))
    keys |= {f"drive.tab_{k}" for k in ("overview", "access", "index", "settings")}
    keys |= {f"drive.index_usd_{k}" for k in ("reader", "triage", "profile", "passage", "transcribe")}
    keys |= {f"drive.err.{k}" for k in ("reconnect_needed", "throttled", "provider_error", "internal")}
    keys |= {f"drive.unreadable_reason.{k}" for k in ("no_text", "machine_text", "too_large", "user_chunk_cap", "failed")}
    return {k for k in keys if not k.endswith(".") and not k.endswith("_")}


def test_every_drive_key_the_card_and_picker_use_exists_in_all_four_languages_with_the_same_placeholders():
    _, dicts = _dicts()
    keys = _code_keys()
    assert len(keys) > 100
    for key in sorted(keys):
        placeholders = {lang: sorted(re.findall(r"\{(\w+)\}", dicts[lang].get(key, "")) ) for lang in LANGS}
        assert all(dicts[lang].get(key) for lang in LANGS), key
        assert len({tuple(v) for v in placeholders.values()}) == 1, (key, placeholders)


def test_removed_keys_are_gone_from_all_four_languages():
    _, dicts = _dicts()
    for key in ("drive.scope", "drive.scope_whole", "drive.add_folder", "drive.choose_folder", "drive.write_folder",
                "drive.write_folder_none", "drive.write_folder_root", "drive.open", "drive.select", "drive.up",
                "drive.note", "drive.transcribe_on", "drive.index_model_usage", "drive.index_initial_build",
                "drive.index_initial_build_done"):
        assert all(key not in dicts[lang] for lang in LANGS), key


def test_the_new_texts_are_translated_not_copied():
    _, dicts = _dicts()
    same_ok = {"drive.search_count", "drive.pk_saves_to", "drive.spent_of", "drive.chip_indexing"}   # numbers and placeholders only
    for key in ("drive.chip_nothing", "drive.pk_confirm_one", "drive.pk_keep", "drive.whole_sub_account",
                "drive.index_checking", "drive.index_described", "drive.index_readable_content",
                "drive.pk_title_scope", "drive.add_hint", "drive.tab_access"):
        assert key not in same_ok
        assert all(dicts[lang][key] != dicts["en"][key] for lang in ("uk", "fr", "es")), key


def test_the_disclosure_texts_are_rendered_where_they_were_inside_collapsed_details():
    assert re.search(r'<details class="disclosure"><summary>\$\{t\("drive.disclosure_summary"\)\}</summary><p>\$\{t\("drive.index_disclosure"\)\}', INDEX)
    assert "index_disclosure_dr_copy" in INDEX and "transcribe_disclosure" in INDEX and "mirror_clause" in INDEX


def test_the_add_drive_hint_names_no_provider():
    _, dicts = _dicts()
    for lang in LANGS:
        for key in ("drive.add_hint", "drive.pk_root", "drive.picker_outer"):
            assert not re.search(r"onedrive|google", dicts[lang][key], re.I), (lang, key)


_OVERLAY_CLICK = re.search(r'<div class="dv-overlay" onclick="(.*?)">', PICKER).group(1).replace("${q}", "c1")


def _click(target: str) -> str:
    """Runs the overlay's own onclick text as the browser would: `this` is the overlay, `event.target` what was hit."""
    return ('const overlay = {}; const inside = {}; new Function("event", ' + json.dumps(_OVERLAY_CLICK)
            + f').call(overlay, {{ target: {target} }});')


@node
def test_a_click_outside_the_dialog_with_pending_changes_asks_before_closing():
    out = _picker('toggleDrivePickerFolder("c1", 1);' + _click("overlay"))
    assert out["open"] and "T:drive.pk_confirm_one" in out["foot"] and "resumeDrivePicker('c1')" in out["foot"]
    assert out["calls"] == []


@node
def test_a_click_outside_the_dialog_without_changes_just_closes():
    assert not _picker(_click("overlay"))["open"]


@node
def test_a_click_inside_the_dialog_does_not_close_it():
    out = _picker('toggleDrivePickerFolder("c1", 1);' + _click("inside"))
    assert out["open"] and "T:drive.pk_confirm_one" not in out["foot"] and "T:drive.save (1)" in out["foot"]
    assert _picker(_click("inside"))["open"]
