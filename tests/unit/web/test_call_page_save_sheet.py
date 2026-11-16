"""The save feedback on the interpreter and tutor pages: a "Saving…" label with a spinner while a file request runs, then a
modal result sheet (success or failure) with Done / Save more / Back. Static checks read the page; behavioural ones run the
page's blocks under node with a fake DOM and a fake fetch (skipped without node)."""
import json

import pytest

from tests.unit.web.call_page_harness import HTML, block, dictionary, needs_node, page_script, run_node
from tests.unit.web.test_call_page_save_as_file import _CALL, _FETCH, _out, save_block

KEYS = ("save.saving", "save.done", "save.more", "save.back")

# The fake page state the Done path touches outside the live-text blocks.
_PAGE = """
const stateLog = []; const buttonLog = [];
function setState(x) { stateLog.push(x); }
function renderButton(m) { buttonLog.push(m); }
let callStarts = 0; function startCall() { callStarts++; }
const sheetEl = () => document.body.querySelector(".save-sheet");
const sheetText = () => (sheetEl() ? sheetEl().querySelector(".save-sheet-text").textContent : null);
const sheetMark = () => (sheetEl() ? sheetEl().querySelector(".save-sheet-mark").textContent : null);
const sheetBtns = () => (sheetEl() ? sheetEl().querySelectorAll(".save-sheet-btn").map((b) => b.textContent) : []);
const sheetClick = (label) => sheetEl().querySelectorAll(".save-sheet-btn").find((b) => b.textContent === label).click();
const held = () => { let release; globalThis.fetch = () => new Promise((r) => { release = (reply) => r({ status: reply.status, json: async () => reply.body }); }); return (reply) => release(reply); };
"""


def _run(body: str, mode: str | None = "interpreter") -> dict:
    return run_node(page_script(mode, save_block() + "\nshowLiveText();\n" + _PAGE + body))


def _ended(drive: bool = False) -> str:
    return _FETCH + _CALL + f"callFileToDrive = {str(drive).lower()}; endLiveText();\n"


# ---------------------------------------------------------------- static

@pytest.mark.parametrize("lang", ("en", "uk", "fr", "es"))
def test_every_locale_has_the_sheet_keys(lang):
    for key in KEYS:
        assert f'"{key}":' in dictionary(lang)


def test_the_english_labels_are_pinned():
    en = dictionary("en")
    for pair in ('"save.saving": "Saving…"', '"save.done": "Done"', '"save.more": "Save more"', '"save.back": "Back"'):
        assert pair in en


def test_the_sheet_is_a_modal_dialog_with_no_outside_dismissal():
    code = save_block()
    assert 'role", "dialog"' in code and 'aria-modal", "true"' in code
    sheet = block("      function openSaveSheet(", "\n      function closeSaveSheet")
    assert 'addEventListener("keydown"' not in sheet and 'sheet.addEventListener' not in sheet
    assert "innerHTML" not in code


# ---------------------------------------------------------------- saving state

@needs_node
def test_while_saving_the_pressed_button_shows_the_label_and_a_spinner_and_all_are_disabled():
    out = _run(_ended(drive=True) + """
const release = held();
btns()[2].click();
const during = { labels: btns().map((b) => b._text), disabled: btns().map((b) => b.disabled),
  spin: btns().map((b) => b.querySelectorAll(".save-spin").length), sheet: !!sheetEl() };
release({ status: 200, body: { ok: true, path: "p" } }); await tick();
""" + _out(during="during", after="btns().map((b) => b.querySelectorAll('.save-spin').length)"))
    assert out["during"]["labels"] == ["save.copy", "save.chat", "save.saving"]
    assert out["during"]["disabled"] == [True, True, True]
    assert out["during"]["spin"] == [0, 0, 1] and out["during"]["sheet"] is False
    assert out["after"] == [0, 0, 0]


@needs_node
def test_a_second_tap_while_saving_sends_nothing():
    out = _run(_ended() + """
const release = held();
btns()[1].click(); btns()[1].click();
release({ status: 200, body: { ok: true } }); await tick();
""" + _out(sheets="document.body.querySelectorAll('.save-sheet').length"))
    assert out["sheets"] == 1


# ---------------------------------------------------------------- the sheet

@needs_node
def test_chat_success_sheet_shows_the_check_and_the_sent_text_and_takes_focus():
    out = _run(_ended(True) + "btns()[1].click(); await tick();\n" + _out(
        text="sheetText()", mark="sheetMark()", btns="sheetBtns()", role="sheetEl().getAttribute('role')",
        modal="sheetEl().getAttribute('aria-modal')", focus="document.activeElement.textContent",
        busy="saveFile.busy", inline="result()"))
    assert out["text"] == "save.sent" and out["mark"] == "✓"
    assert out["btns"] == ["save.more", "save.done"]
    assert out["role"] == "dialog" and out["modal"] == "true" and out["focus"] == "save.more"
    assert out["busy"] is False and out["inline"] == ""


@needs_node
def test_drive_success_sheet_shows_the_path():
    out = _run(_ended(drive=True) + "btns()[2].click(); await tick();\n" + _out(text="sheetText()", btns="sheetBtns()"))
    assert out["text"] == "save.saved Inbox/2026-11-16 10.00 Call.md"
    assert out["btns"] == ["save.more", "save.done"]


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (409, {"ok": False, "reason": "not_connected"}, "save.not_connected"),
        (409, {"ok": False, "reason": "refused"}, "save.refused"),
        (409, {"ok": False, "reason": "no_chat"}, "save.no_chat"),
        (413, {}, "save.too_long"),
        (502, {}, "save.failed"),
    ],
)
@needs_node
def test_failure_sheet_per_reason_has_back_and_done_and_no_check(status, body, expected):
    out = _run(_ended(drive=True) + f"reply = {{ status: {status}, body: {json.dumps(body)} }};\n"
               + "btns()[2].click(); await tick();\n" + _out(
                   text="sheetText()", mark="sheetMark()", btns="sheetBtns()", disabled="btns().map((b) => b.disabled)"))
    assert out["text"] == expected and out["mark"] == ""
    assert out["btns"] == ["save.back", "save.done"]
    assert out["disabled"] == [False, False, False]


@needs_node
def test_the_path_goes_into_the_sheet_through_textcontent():
    out = _run(_ended(True) + """
reply = { status: 200, body: { ok: true, path: "<img src=x onerror=alert(1)>" } };
btns()[2].click(); await tick();
""" + _out(text="sheetText()", kids="sheetEl().querySelector('.save-sheet-text').children.length"))
    assert "<img" in out["text"] and out["kids"] == 0


@needs_node
def test_a_network_error_gives_a_failure_sheet():
    out = _run(_ended() + "globalThis.fetch = async () => { throw new Error('down'); };\nbtns()[1].click(); await tick();\n"
               + _out(text="sheetText()", btns="sheetBtns()"))
    assert out["text"] == "save.failed" and out["btns"] == ["save.back", "save.done"]


@needs_node
def test_back_closes_the_sheet_keeps_the_text_and_allows_a_retry():
    out = _run(_ended() + """
reply = { status: 502, body: {} };
btns()[1].click(); await tick();
sheetClick("save.back");
const afterBack = { sheet: !!sheetEl(), row: !!rowEl(), lines: liveText.lines.length, disabled: btns().map((b) => b.disabled) };
reply = { status: 200, body: { ok: true } };
btns()[1].click(); await tick();
""" + _out(afterBack="afterBack", retry="sheetText()", calls="calls.length"))
    assert out["afterBack"] == {"sheet": False, "row": True, "lines": 6, "disabled": [False, False]}
    assert out["retry"] == "save.sent" and out["calls"] == 2


# ---------------------------------------------------------------- Done

@needs_node
def test_done_returns_to_the_idle_screen_without_starting_a_call():
    out = _run(_ended() + """
const screen = document.querySelector(".screen");
btns()[1].click(); await tick();
sheetClick("save.done");
""" + _out(sheet="!!sheetEl()", row="!!rowEl()", panel="!!panelOf()", lines="liveText.lines.length",
           live="screen.classList.contains('text-live')", state="stateLog", button="buttonLog", starts="callStarts",
           flag="callFileToDrive"))
    assert out["sheet"] is False and out["row"] is False and out["panel"] is False and out["lines"] == 0
    assert out["live"] is False and out["flag"] is False
    assert out["state"] == ["save.not_a_key"] or out["state"] == ["state.idle"]
    assert out["button"] == ["idle"] and out["starts"] == 0


@needs_node
def test_done_after_a_failure_also_returns_to_idle():
    out = _run(_ended() + "reply = { status: 413, body: {} };\nbtns()[1].click(); await tick();\nsheetClick('save.done');\n"
               + _out(sheet="!!sheetEl()", row="!!rowEl()", panel="!!panelOf()", starts="callStarts"))
    assert out == {"sheet": False, "row": False, "panel": False, "starts": 0}


# ---------------------------------------------------------------- Save more

@needs_node
@pytest.mark.parametrize(
    "drive, first, expected",
    [
        (False, "chat", ["save.done"]),  # chat filed, no drive on this call: nothing left
        (True, "chat", ["save.more", "save.done"]),
        (True, "drive", ["save.more", "save.done"]),
    ],
)
def test_save_more_is_offered_only_while_a_target_is_unfiled(drive, first, expected):
    index = 1 if first == "chat" else 2
    out = _run(_ended(drive) + f"btns()[{index}].click(); await tick();\n" + _out(btns="sheetBtns()"))
    assert out["btns"] == expected


@needs_node
def test_after_both_targets_are_filed_save_more_is_gone():
    out = _run(_ended(True) + """
btns()[1].click(); await tick(); sheetClick("save.more");
btns()[2].click(); await tick();
""" + _out(btns="sheetBtns()"))
    assert out["btns"] == ["save.done"]


@needs_node
def test_a_filed_target_keeps_its_check_and_stays_disabled_after_save_more():
    out = _run(_ended(True) + """
btns()[1].click(); await tick();
sheetClick("save.more");
const state = { sheet: !!sheetEl(), labels: btns().map((b) => b.textContent), disabled: btns().map((b) => b.disabled), row: !!rowEl() };
btns()[1].click();
const sent = calls.length;
btns()[2].click(); await tick();
""" + _out(state="state", sent="sent", total="calls.length", targets="calls.map((c) => c.body.target)",
           after="btns().map((b) => b.textContent)", disabled="btns().map((b) => b.disabled)"))
    assert out["state"] == {"sheet": False, "labels": ["save.copy", "✓ save.chat", "save.drive"],
                            "disabled": [False, True, False], "row": True}
    assert out["sent"] == 1 and out["targets"] == ["chat", "drive"]
    assert out["after"] == ["save.copy", "✓ save.chat", "✓ save.drive"]
    assert out["disabled"] == [False, True, True]


@needs_node
def test_a_failed_target_is_not_marked_filed():
    out = _run(_ended(True) + """
reply = { status: 409, body: { ok: false, reason: "refused" } };
btns()[2].click(); await tick(); sheetClick("save.back");
""" + _out(labels="btns().map((b) => b.textContent)", disabled="btns().map((b) => b.disabled)"))
    assert out["labels"] == ["save.copy", "save.chat", "save.drive"] and out["disabled"] == [False] * 3


# ---------------------------------------------------------------- guards

@needs_node
def test_a_late_response_after_done_or_a_new_call_opens_no_sheet():
    out = _run(_ended() + """
const release = held();
btns()[1].click();
resetLiveText();
release({ status: 200, body: { ok: true } }); await tick();
const afterReset = !!sheetEl();
// a response that lands while the sheet of an earlier save is already gone and a new text exists
showLiveText();
""" + _CALL + "endLiveText();\n" + _out(afterReset="afterReset", now="!!sheetEl()", busy="saveFile.busy",
                                        disabled="btns().map((b) => !!b.disabled)"))
    assert out["afterReset"] is False and out["now"] is False and out["busy"] is False
    assert out["disabled"] == [False, False]


@needs_node
def test_a_reset_removes_an_open_sheet_and_the_filed_marks():
    out = _run(_ended() + """
btns()[1].click(); await tick();
const open = !!sheetEl();
resetLiveText();
showLiveText();
""" + _CALL + "endLiveText();\n" + _out(open="open", gone="!!sheetEl()", labels="btns().map((b) => b.textContent)"))
    assert out["open"] is True and out["gone"] is False and out["labels"] == ["save.copy", "save.chat"]


@needs_node
def test_the_main_page_never_builds_a_sheet():
    out = _run(_FETCH + _CALL + "endLiveText();\n" + _out(
        sheet="!!document.body.querySelector('.save-sheet')", row="!!rowEl()", calls="calls.length"), mode=None)
    assert out == {"sheet": False, "row": False, "calls": 0}


def test_the_main_page_markup_and_script_gate_are_unchanged():
    assert "LIVE_TEXT_MODES.includes(window.VOICE_MODE)" in save_block()
    body = HTML.split("<body", 1)[1].split("<script>", 1)[0]
    assert "save-sheet" not in body
