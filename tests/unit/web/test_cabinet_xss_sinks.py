"""Server text reaches the Cabinet's DOM only escaped (same style as test_cabinet_escape_html.py): reminder
instructions (model-written, may quote external email), invite codes, linked platform ids, toast messages, and the
ids handed to inline handlers. One escaper (`escapeHtml`); `esc` delegates to it, so quotes are escaped too.
Static checks on the page source, plus behavioural checks under node when it is installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _page():
    return _HTML.read_text(encoding="utf-8")


def _function(name):
    match = re.search(rf"\n(\s*function {name}\(.*?\)\s*\{{.*?\n?\s*\}})\n", _page(), re.S)
    assert match, name
    return match.group(1)


# -- reminders ------------------------------------------------------------------------------

def test_the_reminder_instruction_is_escaped_before_its_line_breaks():
    page = _page()
    assert 'esc(r.instruction || "").replace(/\\n/g, "<br>")' in page
    assert '(r.instruction || "").replace(' not in page.replace('esc(r.instruction || "").replace(', "")


# -- one escaper ----------------------------------------------------------------------------

def test_esc_delegates_to_escape_html():
    assert re.search(r"function esc\(s\)\s*\{\s*return escapeHtml\(s\);\s*\}", _page())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_esc_escapes_quotes_too():
    script = _function("escapeHtml") + "\n" + _function("esc") + \
        "\nprocess.stdout.write(JSON.stringify(esc(`\"'<>&`)));"
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30,
                                    check=True).stdout)
    assert out == "&#34;&#39;&#60;&#62;&#38;"


# -- toasts ---------------------------------------------------------------------------------

def test_a_toast_message_is_set_as_text_never_as_markup():
    body = _function("showToast")
    assert "${msg}" not in body
    assert re.search(r"\.textContent = msg;", body)


# -- invite codes and linked ids ------------------------------------------------------------

@pytest.mark.parametrize("raw", ["${data.code}", "${c.code}", "${c.hint}", "${linkedId}", 'value="${deepLink}"'])
def test_codes_and_linked_ids_are_escaped(raw):
    assert raw not in _page()


# The invite list shows only the hint since S5 (SECRETS_AT_REST_RFC §2.5); its cell goes through the same escaper.
@pytest.mark.parametrize("escaped", ["${escapeHtml(data.code)}", '${escapeHtml(c.hint ? "\\u2026" + c.hint : "\\u2014")}',
                                     "${escapeHtml(linkedId)}",
                                     'value="${escapeHtml(deepLink)}"'])
def test_the_escaper_wraps_them(escaped):
    assert escaped in _page()


# -- ids handed to inline handlers ----------------------------------------------------------

@pytest.mark.parametrize("value", ["r.note_id", "activeJobId", "c.value"])
def test_server_ids_never_land_inside_an_inline_handler(value):
    handlers = re.findall(r'on[a-z]+="([^"]*)"', _page())
    assert [h for h in handlers if "${" + value in h] == []


@pytest.mark.parametrize("attr, handler", [
    ('data-note-id="${escapeHtml(r.note_id)}"', 'onclick="editReminder(this.dataset.noteId)"'),
    ('data-note-id="${escapeHtml(r.note_id)}"', 'onclick="deleteReminder(this.dataset.noteId)"'),
    ('data-job-id="${escapeHtml(activeJobId)}"', 'onclick="cancelGmailIndex(this.dataset.jobId, this)"'),
    ('data-domain="${escapeHtml(c.value)}"', 'onclick="setDomainFilter(this.dataset.domain)"'),
])
def test_they_travel_as_escaped_data_attributes(attr, handler):
    page = _page()
    assert attr in page and handler in page
