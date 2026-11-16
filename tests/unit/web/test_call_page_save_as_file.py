"""The after-call buttons on the interpreter and tutor pages (VOICE_CALL_LIVE_TEXT_RFC §4.11): "Copy text",
"File to chat" and (when the start response allowed it) "File to drive", shown once the call has ended with at least one
line. The static checks read the page; the behavioural ones run the live-text, Show view and save blocks under node with
a fake DOM and a fake fetch (skipped without node)."""
import re

import pytest

from tests.unit.web.call_page_harness import HTML, block, dictionary, needs_node, page_script, run_node

KEYS = (
    "save.copy", "save.chat", "save.drive", "save.copied", "save.sent", "save.saved", "save.not_connected",
    "save.refused", "save.no_chat", "save.failed", "save.too_long",
)


def save_block() -> str:
    return block("      /* ── Save as file ──", "        Object.assign(saveFile, { row, result });\n      }")


def _run(body: str, mode: str | None = "interpreter") -> dict:
    return run_node(page_script(mode, save_block() + "\nshowLiveText();\n" + body))


def _out(**expr: str) -> str:
    return "console.log(JSON.stringify({" + ", ".join(f"{k}: ({v})" for k, v in expr.items()) + "}));"


# Two translations, an interrupted one, then an ask block (invented universe only).
_CALL = """
send(line("l1", "caller", "set", "We have a booking under RM-55120."));
send(line("l2", "lelik", "append", "Abbiamo una prenotazione con il codice RM-55120."));
send(endLine("l2", "done"));
send(line("l3", "caller", "set", "Is this Albergo Immaginario?"));
send(line("l4", "lelik", "append", "È questo l'Albergo"));
send(endLine("l4", "interrupted"));
send(line("l5", "caller", "set", "Lelik, how do I say thank you in Italian?", { kind: "ask" }));
send(line("l6", "lelik", "append", "Grazie.", { kind: "ask" }));
"""
_FETCH = """
const calls = [];
let reply = { status: 200, body: { ok: true, path: "Inbox/2026-11-16 10.00 Call.md" } };
globalThis.fetch = async (url, init) => { calls.push({ url, init, body: JSON.parse(init.body) });
  return { status: reply.status, json: async () => reply.body }; };
const rowEl = () => document.body.querySelector(".save-row");
const btns = () => (rowEl() ? rowEl().querySelectorAll(".save-btn") : []);
const labels = () => btns().map((b) => b.textContent);
const result = () => (rowEl() ? rowEl().querySelector(".save-result").textContent : null);
const tick = () => new Promise((r) => setTimeout(r, 0));
"""
# A chat or drive save answers in the result sheet; the inline `.save-result` line is Copy text's only.
_SHEET = """
const sheetOf = () => document.body.querySelector(".save-sheet");
const sheetLine = () => (sheetOf() ? sheetOf().querySelector(".save-sheet-text").textContent : null);
"""


# ---------------------------------------------------------------- static

def test_the_block_is_gated_and_free_of_unsafe_apis_and_storage():
    code = save_block()
    assert "LIVE_TEXT_MODES.includes(window.VOICE_MODE)" in code
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "localStorage", "sessionStorage", ".play("):
        assert banned not in code
    assert 'credentials: "same-origin"' in code and "/api/voice/call-text/file" in code


@pytest.mark.parametrize("lang", ("en", "uk", "fr", "es"))
def test_every_locale_has_the_save_keys(lang):
    for key in KEYS:
        assert f'"{key}":' in dictionary(lang)


def test_the_english_labels_are_pinned():
    en = dictionary("en")
    assert '"save.copy": "Copy text"' in en and '"save.chat": "File to chat"' in en
    assert '"save.drive": "File to drive"' in en


def test_the_hooks_sit_in_the_existing_entry_points():
    assert 'if (typeof showSaveRow === "function") showSaveRow();' in block("      function endLiveText() {", "\n      }\n")
    assert 'if (typeof clearSaveRow === "function") clearSaveRow();' in block("      function resetLiveText() {", "\n      }\n")
    assert HTML.count('addEventListener("pagehide"') == 1
    assert "callLiveAt = startedAt;" in block("      function startTimer() {", "\n      }\n")
    assert "callFileToDrive = startBody.file_to_drive === true;" in HTML


def test_the_css_uses_tokens_only_and_the_body_markup_is_unchanged():
    css = block("      /* ── Save as file (VOICE", "    </style>")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    body = HTML.split("<body", 1)[1].split("<script>", 1)[0]
    assert "save" not in body


# ---------------------------------------------------------------- mapping

@needs_node
def test_entries_map_pairs_ask_blocks_interrupted_and_lone_lines():
    out = _run(_CALL + _out(entries="callTextEntries(liveText.lines)") + """
const lone = callTextEntries([
  { line: "l1", who: "caller", text: "Café Andorinha?", status: "done" },
  { line: "l2", who: "caller", text: "Quisiera un café con leche", status: "done" },
  { line: "l3", who: "lelik", text: "I would like a coffee", status: "interrupted" },
  { line: "l4", who: "lelik", text: "Anything else?", status: "done" },
  { line: "l5", who: "caller", kind: "ask", text: "Lelik?", status: "done" },
  { line: "l6", who: "caller", text: "Is this Albergo Immaginario?", status: "done" },
  { line: "l7", who: "lelik", kind: "ask", text: "Yes.", status: "done" },
]);
console.log(JSON.stringify({ lone }));
""")
    assert out["lone"] == [
        {"source": "Café Andorinha?", "translation": "", "interrupted": False},
        {"source": "Quisiera un café con leche", "translation": "I would like a coffee", "interrupted": True},
        {"source": "", "translation": "Anything else?", "interrupted": False},
        {"question": "Lelik?", "answer": ""},
        {"source": "Is this Albergo Immaginario?", "translation": "", "interrupted": False},
        {"question": "", "answer": "Yes."},
    ]


@needs_node
def test_a_call_maps_in_display_order():
    out = _run(_CALL + _out(entries="callTextEntries(liveText.lines)", plain="callTextPlain(callTextEntries(liveText.lines))"))
    assert out["entries"] == [
        {"source": "We have a booking under RM-55120.", "translation": "Abbiamo una prenotazione con il codice RM-55120.", "interrupted": False},
        {"source": "Is this Albergo Immaginario?", "translation": "È questo l'Albergo", "interrupted": True},
        {"question": "Lelik, how do I say thank you in Italian?", "answer": "Grazie."},
    ]
    assert out["plain"] == (
        "We have a booking under RM-55120.\nAbbiamo una prenotazione con il codice RM-55120.\n\n"
        "Is this Albergo Immaginario?\nÈ questo l'Albergo …\n\nLelik, how do I say thank you in Italian?\nGrazie."
    )


# ---------------------------------------------------------------- buttons

@needs_node
def test_buttons_appear_only_after_the_call_with_a_line_and_a_new_call_starts_clean():
    out = _run(_FETCH + _CALL + """
const during = labels();
endLiveText(); const noDrive = labels();
resetLiveText(); callFileToDrive = true; showLiveText();
const beforeLines = labels();
""" + _CALL + """
endLiveText(); const withDrive = labels();
const place = document.querySelector(".screen").children.map((c) => c.className);
""" + _out(during="during", noDrive="noDrive", beforeLines="beforeLines", withDrive="withDrive", place="place"))
    assert out["during"] == []
    assert out["noDrive"] == ["save.copy", "save.chat"]
    assert out["beforeLines"] == []
    # the next call's start response sets the flag after the reset, so its drive button appears
    assert out["withDrive"] == ["save.copy", "save.chat", "save.drive"]
    assert out["place"][-2:] == ["save-row", "controls"]


@needs_node
def test_the_drive_button_shows_when_the_start_response_flag_is_set_for_this_call():
    out = _run(_FETCH + _CALL + "callFileToDrive = true; endLiveText();\n" + _out(labels="labels()"))
    assert out["labels"] == ["save.copy", "save.chat", "save.drive"]


@needs_node
def test_no_row_for_a_call_without_lines_and_none_on_the_main_page():
    empty = _run(_FETCH + "endLiveText();\n" + _out(row="!!rowEl()"))
    assert empty["row"] is False
    main = _run(_FETCH + _CALL + "endLiveText();\n" + _out(row="!!rowEl()"), mode=None)
    assert main["row"] is False


@needs_node
def test_reset_clears_the_row_and_a_late_reply_does_not_touch_it():
    out = _run(_FETCH + _CALL + """
callFileToDrive = true; endLiveText();
let release; globalThis.fetch = (u, i) => new Promise((r) => { release = () => r({ status: 200, json: async () => ({ ok: true, path: "x" }) }); });
btns()[2].click();
const busy = btns().map((b) => b.disabled);
resetLiveText();
const cleared = !!rowEl();
release(); await tick();
""" + _out(busy="busy", cleared="cleared", row="!!rowEl()", flag="callFileToDrive"))
    assert out["busy"] == [True, True, True]
    assert out["cleared"] is False and out["row"] is False and out["flag"] is False


# ---------------------------------------------------------------- posting

@needs_node
def test_filing_to_drive_posts_the_entries_and_shows_the_path():
    out = _run(_FETCH + _SHEET + _CALL + """
callLiveAt = Date.now() - 125000; callFileToDrive = true; endLiveText();
btns()[2].click(); await tick();
""" + _out(call="calls[0]", result="sheetLine()", disabled="btns().map((b) => b.disabled)"))
    call = out["call"]
    assert call["url"] == "/api/voice/call-text/file"
    assert call["init"]["method"] == "POST" and call["init"]["credentials"] == "same-origin"
    body = call["body"]
    assert body["target"] == "drive" and body["mode"] == "interpreter"
    assert 124.9 <= body["duration_s"] <= 126
    assert len(body["entries"]) == 3 and body["entries"][2] == {
        "question": "Lelik, how do I say thank you in Italian?", "answer": "Grazie.",
    }
    assert out["result"] == "save.saved Inbox/2026-11-16 10.00 Call.md"
    # the filed drive target stays disabled for the rest of this text; the others are free again
    assert out["disabled"] == [False, False, True]


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, {"ok": True, "path": "ignored"}, "save.sent"),
        (409, {"ok": False, "reason": "no_chat"}, "save.no_chat"),
        (413, {}, "save.too_long"),
        (400, {}, "save.failed"),
        (502, {}, "save.failed"),
        (503, {}, "save.failed"),
    ],
)
@needs_node
def test_file_to_chat_result_line_per_status(status, body, expected):
    out = _run(_FETCH + _SHEET + _CALL + f"reply = {{ status: {status}, body: {__import__('json').dumps(body)} }};\n"
               + "endLiveText(); btns()[1].click(); await tick();\n" + _out(call="calls[0].body.target", result="sheetLine()"))
    assert out["call"] == "chat" and out["result"] == expected


@pytest.mark.parametrize("reason", ("not_connected", "refused"))
@needs_node
def test_file_to_drive_409_reasons(reason):
    out = _run(_FETCH + _SHEET + _CALL + f'reply = {{ status: 409, body: {{ ok: false, reason: "{reason}" }} }};\n'
               + "callFileToDrive = true; endLiveText(); btns()[2].click(); await tick();\n" + _out(result="sheetLine()"))
    assert out["result"] == f"save.{reason}"


@needs_node
def test_a_network_error_gives_the_generic_line_and_frees_the_buttons():
    out = _run(_FETCH + _SHEET + _CALL + "globalThis.fetch = async () => { throw new Error('down'); };\n"
               + "endLiveText(); btns()[1].click(); await tick();\n" + _out(result="sheetLine()", d="btns().map((b) => b.disabled)"))
    assert out["result"] == "save.failed" and out["d"] == [False, False]


@needs_node
def test_copy_writes_the_plain_text_and_reports():
    out = _run(_FETCH + _CALL + """
const written = [];
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async (s) => { written.push(s); } } }, configurable: true });
endLiveText(); btns()[0].click(); await tick();
const ok = result();
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async () => { throw new Error("no"); } } }, configurable: true });
btns()[0].click(); await tick();
""" + _out(written="written", ok="ok", bad="result()", fetched="calls.length"))
    assert out["written"][0].startswith("We have a booking under RM-55120.\nAbbiamo una prenotazione con il codice RM-55120.")
    assert out["ok"] == "save.copied" and out["bad"] == "save.failed" and out["fetched"] == 0


@needs_node
def test_text_in_the_result_goes_through_textcontent():
    out = _run(_FETCH + _SHEET + _CALL + """
reply = { status: 200, body: { ok: true, path: "<img src=x onerror=alert(1)>" } };
callFileToDrive = true; endLiveText(); btns()[2].click(); await tick();
""" + _out(result="sheetLine()", kids="sheetOf().querySelector('.save-sheet-text').children.length"))
    assert "<img" in out["result"] and out["kids"] == 0
