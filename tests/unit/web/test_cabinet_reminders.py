"""The Reminders section of the Cabinet (CABINET_REDESIGN_RFC §5.5): the list as rows (label, next run, repeat), a row
that opens to Edit and Delete, one form that is placed in a row's body or above the list, and classes only in the new
markup. The page is read as text; the logic of `cabinet-reminders.js` runs under node against a small fake DOM, with
the script passed through a temp file (a Linux argument is limited to ~128 KB; `node -e` would carry the whole text)."""
import json
import re
import shutil
import subprocess

import pytest

from src.web.user_cabinet_app import CABINET_ASSETS
from tests.unit.web.cabinet_source import STATIC, cabinet_source

_HTML = cabinet_source()
_SHELL = (STATIC / "cabinet.html").read_text(encoding="utf-8")
_REM_JS = (STATIC / "cabinet-reminders.js").read_text(encoding="utf-8")
_STRINGS_JS = (STATIC / "cabinet-strings.js").read_text(encoding="utf-8")
_SECTIONS_JS = (STATIC / "cabinet-sections.js").read_text(encoding="utf-8")
LANGS = ("en", "uk", "fr", "es")
_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _reminders_section() -> str:
    start = _SHELL.index('<section id="section-reminders"')
    return _SHELL[start:_SHELL.index("</section>", start)]


# ------------------------------------------------------------------------------------------------ the markup

def test_the_script_is_served_and_loaded_after_the_sections():
    assert CABINET_ASSETS["cabinet-reminders.js"] == "application/javascript"
    order = re.findall(r'<script src="/cabinet/assets/([\w.-]+)"></script>', _SHELL)
    assert order.index("cabinet-sections.js") < order.index("cabinet-reminders.js")


def test_the_reminders_logic_lives_in_its_own_script_not_in_the_sections_script():
    for name in ("loadReminders", "renderReminders", "showReminderForm", "hideReminderForm", "editReminder",
                 "saveReminder", "deleteReminder", "onRecurrenceChange"):
        assert f"function {name}(" in _REM_JS, name
        assert f"function {name}(" not in _SECTIONS_JS, name


def test_the_new_markup_and_script_carry_no_inline_style():
    assert not re.search(r"\sstyle\s*=", _reminders_section())
    assert not re.search(r"\sstyle\s*=", _REM_JS)
    assert "settings-rem-title" not in _SHELL


def test_the_section_is_one_group_of_rows_with_a_new_button_and_one_form():
    section = _reminders_section()
    assert section.count('class="c-group"') == 1 and 'id="reminders-list"' in section
    assert section.count('id="reminder-form"') == 1
    assert re.search(r'<button[^>]*id="reminders-new"[^>]*onclick="showReminderForm\(\)"', section)
    assert 'onclick="saveReminder()"' in section and 'onclick="hideReminderForm()"' in section
    assert 'onchange="onRecurrenceChange()"' in section
    for field in ("rem-edit-id", "rem-text", "rem-instruction", "rem-due", "rem-recurrence", "rem-rrule"):
        assert f'id="{field}"' in section, field
    options = re.findall(r'<option value="([^"]*)"', section)
    assert options == ["", "FREQ=HOURLY", "FREQ=DAILY", "FREQ=WEEKLY", "FREQ=MONTHLY", "__custom__"]


def test_a_row_opens_to_edit_and_delete_through_the_old_handlers():
    assert 'onclick="toggleReminderRow(this.dataset.noteId)"' in _REM_JS
    assert 'onclick="editReminder(this.dataset.noteId)"' in _REM_JS
    assert 'onclick="deleteReminder(this.dataset.noteId)"' in _REM_JS
    assert re.search(r'class="c-btn c-dng"[^>]*onclick="deleteReminder', _REM_JS)       # Delete is the danger button


# ------------------------------------------------------------------------------------------------ the strings

def _dict_values(key: str) -> list:
    return re.findall(r'"' + re.escape(key) + r'": "([^"]+)"', _STRINGS_JS)


def _used_keys() -> set:
    return set(re.findall(r'\bt\("(rem\.\w+)"\)', _REM_JS)) | set(re.findall(r'"(rem\.\w+)"', _REM_JS))


def test_every_string_the_section_uses_exists_in_the_four_languages():
    keys = _used_keys() | {"btn.save", "settings.rem.desc"}
    assert len(keys) > 25
    for key in sorted(keys):
        assert len(_dict_values(key)) == len(LANGS), key


def test_the_languages_differ_where_they_should():
    for key in ("rem.new", "rem.edit", "rem.empty", "rem.daily", "rem.confirm_delete"):
        assert len(set(_dict_values(key))) == len(LANGS), key


# ------------------------------------------------------------------------------------------------ the logic, under node

_PRELUDE = r"""
const calls = [];
const toasts = [];
const nodes = {};
const classes = () => { const s = new Set(); return { s, add: (c) => s.add(c), remove: (c) => s.delete(c),
  toggle: (c, on) => (on ? s.add(c) : s.delete(c)), contains: (c) => s.has(c) }; };
const stub = (id) => (nodes[id] = nodes[id] || {
  id, value: "", hidden: false, innerHTML: "", textContent: "", placeholder: "", parent: null, options: [],
  dataset: {}, classList: classes(), attrs: {},
  appendChild(c) { c.parent = this; }, focus() {}, setAttribute(k, v) { this.attrs[k] = v; },
  querySelector() { return null; }, querySelectorAll() { return []; },
});
const sel = stub("rem-recurrence");
sel.options = ["", "FREQ=HOURLY", "FREQ=DAILY", "FREQ=WEEKLY", "FREQ=MONTHLY", "__custom__"].map((value) => ({ value }));
stub("reminders-list").children = [];
globalThis.document = {
  getElementById: (id) => stub(id),
  querySelector: () => null,
  querySelectorAll: () => (nodes["reminders-list"].children || []).filter((w) => w.classList && w.classList.contains("editing")),
};
globalThis.LIST = [];
globalThis.confirm = () => true;
const API = "";
const authFetch = async (url, opts) => { calls.push([(opts && opts.method) || "GET", url, opts && opts.body]); return { ok: true, status: 200, json: async () => ({ reminders: globalThis.LIST }) }; };
const showToast = (msg, type) => toasts.push([msg, type]);
let _currentLang = "en";
%(strings)s
function t(key) { return (LANG_DICT[_currentLang] || LANG_DICT["en"])[key] || key; }
function escapeHtml(str) { return String(str ?? "").replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";"); }
function esc(s) { return escapeHtml(s); }
"""

_NOTE = {"note_id": "n1", "text": "Check email", "instruction": "Look at the inbox.", "due": "2026-07-01T23:30:00+00:00",
         "recurrence": "FREQ=DAILY", "recurrence_label": "every day", "workspace_file": None}


def _run(tmp_path, body: str):
    script = (_PRELUDE % {"strings": _STRINGS_JS}) + _REM_JS + "\n(async () => {\n" + body + "\n})();\n"
    path = tmp_path / "reminders_probe.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _wrapper_js() -> str:
    """A row wrapper as renderReminders builds it, with the three parts the form logic touches."""
    return """
      const mkRow = (noteId) => { const w = { dataset: { noteId }, classList: classes() };
        const btn = { classList: classes(), attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
        const body = { hidden: true }; const slot = { parent: null, appendChild(c) { c.parent = slot; } };
        w.parts = { btn, body, slot };
        w.querySelector = (s) => ({ ".c-row": btn, ".c-row-body": body, ".c-rem-slot": slot })[s] || null; return w; };
    """


@_node
@pytest.mark.parametrize(("rule", "shown"), [
    (None, "Once"), ("FREQ=HOURLY", "Hourly"), ("FREQ=DAILY", "Daily"), ("FREQ=WEEKLY", "Weekly"),
    ("FREQ=MONTHLY", "Monthly"), ("FREQ=WEEKLY;BYDAY=TU,FR", "FREQ=WEEKLY;BYDAY=TU,FR"),
])
def test_a_row_shows_label_next_run_and_repeat(tmp_path, rule, shown):
    note = {**_NOTE, "recurrence": rule, "recurrence_label": "words from the server" if rule else None}
    html = _run(tmp_path, f"""_remindersData = [{json.dumps(note)}]; renderReminders();
      process.stdout.write(JSON.stringify(nodes["reminders-list"].innerHTML));""")
    assert '<span class="c-row-title">Check email</span>' in html
    assert f'<span class="c-rem-repeat">{shown}</span>' in html
    assert re.search(r'<span class="c-rem-sub"><span>[^<]*2026[^<]*</span>', html)         # a next run, with its year


@_node
def test_the_next_run_and_the_prefilled_time_are_in_the_users_timezone(tmp_path):
    out = _run(tmp_path, f"""_remindersZone = "Pacific/Kiritimati";
      const iso = {json.dumps(_NOTE["due"])};
      const inUtc = new Date(iso).toLocaleString(undefined, {{ dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }});
      process.stdout.write(JSON.stringify({{ when: _remWhen(iso), inUtc, input: _remInputValue(iso), none: _remWhen(null) }}));""")
    assert out["input"] == "2026-07-02T13:30"          # 23:30 UTC is 13:30 the next day at UTC+14
    assert out["when"] != out["inUtc"] and re.search(r"13:30|1:30", out["when"])
    assert out["none"] == "—"


@_node
def test_a_label_and_an_instruction_are_escaped_in_the_row(tmp_path):
    note = {**_NOTE, "text": "<img src=x onerror=alert(1)>", "instruction": "<b>x</b>\nsecond"}
    html = _run(tmp_path, f"""_remindersData = [{json.dumps(note)}]; renderReminders();
      process.stdout.write(JSON.stringify(nodes["reminders-list"].innerHTML));""")
    assert "<img" not in html and "<b>" not in html
    assert "&#60;b&#62;x&#60;/b&#62;<br>second" in html


@_node
def test_an_empty_list_is_one_neutral_line_and_the_workspace_file_one_read_only_line(tmp_path):
    out = _run(tmp_path, f"""_remindersData = []; renderReminders(); const empty = nodes["reminders-list"].innerHTML;
      _remindersData = [{json.dumps({**_NOTE, "workspace_file": "projects/morning-briefing.md"})}]; renderReminders();
      process.stdout.write(JSON.stringify({{ empty, rows: nodes["reminders-list"].innerHTML }}));""")
    assert out["empty"] == '<div class="c-rem-note">No reminders yet.</div>'
    assert "workspace file: workspace:projects/morning-briefing.md" in out["rows"]


@_node
def test_edit_prefills_the_form_in_that_rows_body_with_a_custom_rule_in_its_field(tmp_path):
    note = {**_NOTE, "recurrence": "FREQ=WEEKLY;BYDAY=TU,FR"}
    out = _run(tmp_path, _wrapper_js() + f"""
      const row = mkRow("n1"); nodes["reminders-list"].children = [row];
      _remindersData = [{json.dumps(note)}]; _remindersZone = "Europe/Lisbon";
      editReminder("n1");
      process.stdout.write(JSON.stringify({{
        id: nodes["rem-edit-id"].value, text: nodes["rem-text"].value, instruction: nodes["rem-instruction"].value,
        due: nodes["rem-due"].value, repeat: nodes["rem-recurrence"].value, rrule: nodes["rem-rrule"].value,
        ruleRowHidden: nodes["rem-rrule-row"].hidden,
        inRow: nodes["reminder-form"].parent === row.parts.slot, formHidden: nodes["reminder-form"].hidden,
        newSlotHidden: nodes["reminders-new-slot"].hidden, editing: row.classList.contains("editing"),
        rowOpen: row.parts.body.hidden === false }}));""")
    assert out == {"id": "n1", "text": "Check email", "instruction": "Look at the inbox.", "due": "2026-07-02T00:30",
                   "repeat": "__custom__", "rrule": "FREQ=WEEKLY;BYDAY=TU,FR", "ruleRowHidden": False, "inRow": True,
                   "formHidden": False, "newSlotHidden": True, "editing": True, "rowOpen": True}


@_node
def test_a_preset_rule_selects_its_option_and_leaves_the_rule_field_empty(tmp_path):
    out = _run(tmp_path, _wrapper_js() + f"""
      nodes["reminders-list"].children = [mkRow("n1")]; _remindersData = [{json.dumps(_NOTE)}];
      editReminder("n1");
      process.stdout.write(JSON.stringify([nodes["rem-recurrence"].value, nodes["rem-rrule"].value, nodes["rem-rrule-row"].hidden]));""")
    assert out == ["FREQ=DAILY", "", True]


@_node
def test_new_opens_an_empty_form_above_the_list_and_only_one_form_is_open_at_a_time(tmp_path):
    out = _run(tmp_path, _wrapper_js() + f"""
      const row = mkRow("n1"); nodes["reminders-list"].children = [row]; _remindersData = [{json.dumps(_NOTE)}];
      editReminder("n1"); const afterEdit = nodes["reminder-form"].parent === row.parts.slot;
      showReminderForm();
      const snap = {{ afterEdit, inNewSlot: nodes["reminder-form"].parent === nodes["reminders-new-slot"],
        newSlotHidden: nodes["reminders-new-slot"].hidden, formHidden: nodes["reminder-form"].hidden,
        rowEditing: row.classList.contains("editing"), id: nodes["rem-edit-id"].value, text: nodes["rem-text"].value,
        instruction: nodes["rem-instruction"].value, due: nodes["rem-due"].value, repeat: nodes["rem-recurrence"].value }};
      hideReminderForm();
      snap.closed = nodes["reminder-form"].hidden && nodes["reminders-new-slot"].hidden;
      process.stdout.write(JSON.stringify(snap));""")
    assert out == {"afterEdit": True, "inNewSlot": True, "newSlotHidden": False, "formHidden": False,
                   "rowEditing": False, "id": "", "text": "", "instruction": "", "due": "", "repeat": "",
                   "closed": True}


@_node
def test_closing_a_row_closes_the_form_inside_it(tmp_path):
    out = _run(tmp_path, _wrapper_js() + f"""
      const row = mkRow("n1"); nodes["reminders-list"].children = [row]; _remindersData = [{json.dumps(_NOTE)}];
      editReminder("n1"); toggleReminderRow("n1");
      process.stdout.write(JSON.stringify({{ bodyHidden: row.parts.body.hidden, formHidden: nodes["reminder-form"].hidden,
        expanded: row.parts.btn.attrs["aria-expanded"] }}));""")
    assert out == {"bodyHidden": True, "formHidden": True, "expanded": "false"}


@_node
def test_save_and_delete_send_the_same_requests_as_before(tmp_path):
    out = _run(tmp_path, _wrapper_js() + f"""
      nodes["reminders-list"].children = [mkRow("n1")]; _remindersData = [{json.dumps(_NOTE)}]; LIST = _remindersData;
      showReminderForm();
      nodes["rem-text"].value = " Check email "; nodes["rem-instruction"].value = "Look"; nodes["rem-due"].value = "2026-12-01T10:00";
      await saveReminder();
      editReminder("n1"); nodes["rem-recurrence"].value = "__custom__"; nodes["rem-rrule"].value = " FREQ=WEEKLY ";
      await saveReminder();
      await deleteReminder("n1");
      process.stdout.write(JSON.stringify({{ calls, toasts }}));""")
    methods = [(m, u) for m, u, _ in out["calls"]]
    assert methods == [("POST", "/api/user/reminders"), ("GET", "/api/user/reminders"),
                       ("PUT", "/api/user/reminders/n1"), ("GET", "/api/user/reminders"),
                       ("DELETE", "/api/user/reminders/n1"), ("GET", "/api/user/reminders")]
    assert json.loads(out["calls"][0][2]) == {"text": "Check email", "instruction": "Look", "due": "2026-12-01T10:00",
                                              "recurrence": ""}
    assert json.loads(out["calls"][2][2])["recurrence"] == "FREQ=WEEKLY"
    assert [t for t, _ in out["toasts"]] == ["Reminder created", "Reminder updated", "Reminder deleted"]


@_node
def test_the_form_keeps_its_validation(tmp_path):
    out = _run(tmp_path, _wrapper_js() + """
      showReminderForm(); await saveReminder();
      nodes["rem-text"].value = "a"; nodes["rem-instruction"].value = "b"; nodes["rem-due"].value = "2026-12-01T10:00";
      nodes["rem-recurrence"].value = "__custom__"; nodes["rem-rrule"].value = "  "; await saveReminder();
      process.stdout.write(JSON.stringify({ calls, toasts }));""")
    assert out["calls"] == []
    assert out["toasts"] == [["Fill in all fields", "error"], ["Enter a recurrence rule", "error"]]


@_node
@pytest.mark.parametrize("lang", ["uk", "fr", "es"])
def test_the_rows_and_buttons_follow_the_language(tmp_path, lang):
    html = _run(tmp_path, f"""_currentLang = {json.dumps(lang)}; _remindersData = [{json.dumps(_NOTE)}]; renderReminders();
      process.stdout.write(JSON.stringify(nodes["reminders-list"].innerHTML));""")
    assert f'<span class="c-rem-repeat">{_dict_values("rem.daily")[LANGS.index(lang)]}</span>' in html
    assert _dict_values("rem.edit")[LANGS.index(lang)] in html and _dict_values("rem.delete")[LANGS.index(lang)] in html
